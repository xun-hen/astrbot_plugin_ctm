"""AstrBot 接线层：消息入口 → 联合决策 → 单次注入 → 异步归档。

**接线约定（v0.4.1 起，硬约束）**：所有 `@filter.*` handler 必须是
`CtmPlugin` 的**类方法**（第一参数 `self`）。AstrBot 装载插件时会把插件实例
`functools.partial` 绑到 handler 的第一个位置参数上
（`astrbot/core/star/star_manager.py`，4.26/4.28/4.29 一致）；模块级
`async def f(event)` 会被派发成 `f(实例, event)` → `TypeError`，而该异常被
`context_utils.call_handler` 吞掉只留一行框架 ERROR 日志，然后照常走 LLM。
外部表现：加载成功、启动日志正常、配置生效，但一条消息都不处理、库 0 行、
插件日志 0 行（v0.4.0 的现场）。启动自检见 `ctm/bindings.py`。

所有 hook 都吞掉异常（CancelledError 除外），插件出错不能变成"用户消息没人回"。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart

from .config import PLUGIN_NAME, Config, load_config
from .ctm import bindings, modes
from .ctm.adaptive import AdaptiveConfig, AdaptiveDebounce
from .ctm.aggregation import Aggregator
from .ctm.archiver import ArchiveJob, Archiver
from .ctm.cognition import Cognition, TurnDecision
from .ctm.commands import CtmCommands
from .ctm.enums import TopicAction
from .ctm.injection import InjectionBuilder
from .ctm.longterm.manager import LongTermMemory
from .ctm.longterm.store import LongTermStore
from .ctm.memory_store import MemoryStore
from .ctm.observability import Diag, LogCode, configure, log_event
from .ctm.perception import Perception
from .ctm.relationship import RelationshipManager
from .ctm.store import Store
from .ctm.topic_judge import TopicJudge
from .ctm.topic_state import TopicStateMachine

MESSAGE_PRIORITY = 1000
DECISION_EXTRA = "ctm_decision"
#: 归档摘要用的会话轮次窗口（太长的旧话题只保留最近这些轮，避免摘要 prompt 爆掉）
TRANSCRIPT_MAX_TURNS = 24
TRANSCRIPT_MAX_CHARS = 4000
#: 话题摘要的固定要求：保留可复用的具体信息，而不是形容词。
SUMMARY_SYSTEM_PROMPT = """你是对话归档员。把给定的对话片段压缩成一段日后能检索、能复用的摘要。

要求：
1. 四段结构，用中文小标题：在解决什么 / 已确认的结论与参数 / 未完成事项 / 踩过的坑；
2. **优先保留具体信息**：文件名、配置项名、命令、数字、版本号、报错关键词；
3. 200–400 字，不要客套话，不要编造片段里没有的内容；
4. 某段没有内容就写「无」。

只输出摘要正文，不要解释你在做什么。"""


class CtmPlugin(Star):
    """对话认知中枢插件主体。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        """初始化（只装配，不连接数据库）。

        Args:
            context: AstrBot 上下文。
            config: 插件配置（通常是 `AstrBotConfig`，dict 子类）。
        """
        super().__init__(context)
        self.cfg: Config = load_config(config)
        self.diag = Diag()
        self.data_dir = Path(self._data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 先绑好日志出口：即使 initialize() 提前返回（配置未启用等），
        # 入口日志也一定能打出来——"零日志"本身不能成为故障的一部分。
        configure(self._emit_log)

        self.store = Store(self._resolve_db_path())
        self.perception = Perception(self.cfg.topic)
        self.topic_sm = TopicStateMachine(self.store, self.cfg.topic)
        self.memory = MemoryStore(self.store, self.cfg.memory)
        self.relationship = RelationshipManager(self.store, self.cfg.relationship)
        self.builder = InjectionBuilder(self.cfg)
        self.cognition = Cognition(self.cfg, self.builder, self.topic_sm)
        self.commands = CtmCommands(self)
        self.archiver = Archiver(
            self.store,
            self.memory,
            self.relationship,
            self.cfg,
            summarizer=self._summarize_with_model,
        )
        self.lt: LongTermMemory | None = None
        self._lt_store: LongTermStore | None = None
        self._started = False
        self._pending_old_topic: dict[str, int | None] = {}
        self._pending_emotion: dict[str, float] = {}
        self._pending_sticky: dict[str, bool] = {}
        self._recent: dict[str, deque[str]] = {}
        self._last_assistant: dict[str, str] = {}
        self._topic_disabled: set[str] = set()
        self._fallback_reason = "not_tried"
        self._handler_binding: dict[str, str] = {}
        self._plugin_set_state = "unknown"
        self._started_at = 0.0
        self._turn_buffer: dict[str, deque[tuple[str, str]]] = {}
        # 自适应防抖：按 (会话, 用户) 的发消息节奏调窗口；与聚合器共用同一时钟。
        self.adaptive = AdaptiveDebounce(
            AdaptiveConfig(
                enabled=self.cfg.topic.aggregation_adaptive_enabled,
                min_seconds=self.cfg.topic.aggregation_adaptive_min_seconds,
                max_seconds=self.cfg.topic.aggregation_adaptive_max_seconds,
                base_seconds=self.cfg.topic.aggregation_debounce_seconds,
            ),
            clock=time.monotonic,
        )
        self.aggregation = Aggregator(
            enabled=self.cfg.topic.aggregation_enabled,
            debounce_seconds=self.cfg.topic.aggregation_debounce_seconds,
            max_wait_seconds=self.cfg.topic.aggregation_max_wait_seconds,
            clock=time.monotonic,
            on_anomaly=self._agg_anomaly,
            adaptive=self.adaptive
            if self.cfg.topic.aggregation_enabled
            and self.cfg.topic.aggregation_adaptive_enabled
            else None,
        )
        self.topic_judge: TopicJudge | None = None
        if self.cfg.topic.judge_enabled:
            self.topic_judge = TopicJudge(
                provider_getter=self._get_judge_provider,
                provider_id=self.cfg.topic.judge_provider_id,
                timeout_seconds=self.cfg.topic.judge_timeout_seconds,
                max_checks_per_hour=self.cfg.topic.judge_max_checks_per_hour,
                history_turns=self.cfg.topic.judge_history_turns,
                include_assistant_reply=self.cfg.topic.judge_include_assistant_reply,
                always_llm=self.cfg.topic.judge_always_llm,
                sim_continue=self.cfg.topic.judge_sim_continue,
                sim_new_topic=self.cfg.topic.judge_sim_new_topic,
                time_gap_minutes=self.cfg.topic.judge_time_gap_minutes,
                min_message_length=self.cfg.topic.judge_min_message_length,
                short_message_length=self.cfg.topic.judge_short_message_length,
                min_new_confidence=self.cfg.topic.judge_min_confidence,
            )
        logger.info(f"[{PLUGIN_NAME}] 实例已构造 | 数据目录={self.data_dir}")

    # ------------------------------------------------------------------ handler 接线
    # 必须是类方法（见模块 docstring）：框架会把实例 partial 到第一参数上。
    @filter.event_message_type(filter.EventMessageType.ALL, priority=MESSAGE_PRIORITY)
    async def ctm_on_event(self, event: AstrMessageEvent) -> None:
        """消息入口：感知、决策、挂决策；不阻断事件。"""
        try:
            await self.on_message(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 消息入口异常（已兜住）：{exc}")

    @filter.on_llm_request()
    async def ctm_on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """LLM 请求前：唯一注入点。"""
        try:
            await self.on_llm_request(event, req)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 注入阶段异常（已兜住）：{exc}")

    @filter.on_llm_response()
    async def ctm_on_llm_response(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        """LLM 响应后：投递归档任务。"""
        try:
            await self.on_llm_response(event, response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 归档投递异常（已兜住）：{exc}")

    @filter.command("ctm")
    async def ctm_command(self, event: AstrMessageEvent):
        """`/ctm` 管理命令入口。"""
        async for result in self.on_command(event):
            yield result

    @filter.command("topicweave", alias={"话题织机"})
    async def ctw_command(self, event: AstrMessageEvent):
        """`/topicweave` 别名入口（与 `/ctm` 等价）。"""
        async for result in self.on_command(event):
            yield result

    def _verify_plugin_set(self) -> str:
        """自检：本插件是否在 AstrBot 全局 `plugin_set` 白名单里。

        `plugin_set` 是**全局白名单**（`["*"]` 表示全部启用，空列表表示全不启用）。
        不在名单里的插件，handler 会在 `waking_check` 阶段被直接丢弃（只有 DEBUG
        日志），表现与"插件没装"完全一样：零日志、零数据、回复正常。这是真机上
        除 handler 绑定之外的第二个静默门禁。

        Returns:
            `ok` / `not_listed` / `empty` / `unknown`。
        """
        try:
            getter = getattr(self.context, "get_config", None)
            raw = getter() if callable(getter) else None
        except Exception:  # noqa: BLE001 - 自检不能影响插件运行
            return "unknown"
        if not isinstance(raw, dict):
            return "unknown"
        plugin_set = raw.get("plugin_set")
        if not isinstance(plugin_set, list):
            return "unknown"
        if "*" in plugin_set:
            return "ok"
        if not plugin_set:
            return "empty"
        return "ok" if PLUGIN_NAME in plugin_set else "not_listed"

    def _verify_handler_binding(self) -> dict[str, str]:
        """启动自检：handler 是否都已注册并被框架绑定到本实例。

        这一类故障（见模块 docstring）在运行期完全静默，只能在启动时核对。
        自检失败只记 WARN，不阻断插件。

        Returns:
            `{handler 名: ok|missing|unbound|unknown}`。
        """
        try:
            from astrbot.core.star.star_handler import star_handlers_registry
        except Exception:  # noqa: BLE001 - 老版本/无框架时跳过
            return dict.fromkeys(bindings.HANDLER_NAMES, bindings.STATUS_UNKNOWN)
        result = bindings.audit_handlers(
            star_handlers_registry,
            type(self).__module__,
            bindings.HANDLER_NAMES,
        )
        bad = {
            name: status
            for name, status in result.items()
            if status != bindings.STATUS_OK
        }
        if bad:
            log_event(
                LogCode.HANDLER_BINDING,
                error=",".join(f"{name}={status}" for name, status in bad.items()),
            )
            logger.warning(
                f"[{PLUGIN_NAME}] handler 绑定自检异常：{bad}。"
                "handler 必须是本插件的类方法（第一参数 self），"
                "否则 AstrBot 派发时会 TypeError 且被框架吞掉（表现为零日志、零数据）。"
            )
        else:
            logger.info(
                f"[{PLUGIN_NAME}] handler 绑定自检通过："
                f"{len(result)} 个 handler 均已绑定本实例。"
            )
        return result


    # ------------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        """插件加载：自检 handler 绑定、建库、起归档 worker、起长期记忆 worker。"""
        configure(self._emit_log)
        self._started_at = time.time()
        self._handler_binding = self._verify_handler_binding()
        self._plugin_set_state = self._verify_plugin_set()
        if self._plugin_set_state in ("not_listed", "empty"):
            logger.warning(
                f"[{PLUGIN_NAME}] 本插件不在 AstrBot 全局 plugin_set 白名单里"
                f"（当前状态 {self._plugin_set_state}）。白名单是全局开关，"
                "不在名单内的插件 handler 会在消息派发阶段被静默丢弃："
                "表现为零日志、零数据、回复正常。"
                f"修复：WebUI → 配置 → 插件 → 把 {PLUGIN_NAME} 加入 plugin_set"
                "（或把 plugin_set 设为 [\"*\"]）。"
            )
            log_event(
                LogCode.PLUGIN_SET,
                reason=self._plugin_set_state,
            )
        if not self.cfg.enabled:
            logger.warning(
                f"[{PLUGIN_NAME}] 配置 basic.enabled=false，插件不工作"
                "（面板 → 插件配置 → 启用认知中枢）。这条 WARN 是故意留的："
                "关掉总开关时不能只剩静默。"
            )
            return
        try:
            self.store.init()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 数据库初始化失败：{exc}")
            return
        try:
            self.archiver.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 归档 worker 启动失败：{exc}")
        if self.cfg.longterm.enabled:
            try:
                self._lt_store = LongTermStore(self._resolve_db_path())
                self._lt_store.init()
                self.lt = LongTermMemory(
                    self._lt_store,
                    self.cfg.longterm,
                    self._get_provider,
                )
                await self.lt.start()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 长期记忆初始化失败：{exc}")
                self.lt = None
        self._started = True
        try:
            from .ctm.web_api import register_page_api

            register_page_api(self.context, self)
        except Exception as exc:  # noqa: BLE001 - 页面不可用不能拖垮插件
            logger.warning(f"[{PLUGIN_NAME}] 页面后端注册失败：{exc}")
        logger.info(
            f"[{PLUGIN_NAME}] 已启动 | db={self._resolve_db_path()} | "
            f"longterm={'on' if self.lt else 'off'}"
        )

    async def terminate(self) -> None:
        """插件卸载：flush 归档、checkpoint、关连接。

        每一步都**限时**：卸载卡住会把 AstrBot 的插件管理锁一起占死，
        之后所有 reload / 禁用 / 热更新都会一直超时（真机上踩过：
        归档或长期记忆 worker 卡在 provider 请求里，`await worker` 永不返回）。
        """
        self._started = False
        try:
            self.aggregation.close()
        except Exception:  # noqa: BLE001
            pass
        await self._await_bounded("archiver.stop", self.archiver.stop(3.0), 6.0)
        if self.lt is not None:
            await self._await_bounded("longterm.close", self.lt.close(), 3.0)
        if self._lt_store is not None:
            self._sync_bounded("lt_store.close", self._lt_store.close)
        self._sync_bounded("store.close", self._close_store)
        logger.info(f"[{PLUGIN_NAME}] 已卸载。")

    @staticmethod
    async def _await_bounded(label: str, coro: Any, timeout: float) -> None:
        """等待一个协程，超时即放弃（卸载绝不能被单步拖死）。"""
        try:
            await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[{PLUGIN_NAME}] 卸载步骤 {label} 超时 {timeout}s，已放弃等待。")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 卸载步骤 {label} 失败：{exc}")

    @staticmethod
    def _sync_bounded(label: str, fn: Any) -> None:
        """执行同步清理，异常只记日志。"""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 卸载步骤 {label} 失败：{exc}")

    def _close_store(self) -> None:
        """checkpoint + 关闭 SQLite（幂等）。"""
        self.store.checkpoint()
        self.store.close()

    # ------------------------------------------------------------------ 消息入口
    async def on_message(self, event: AstrMessageEvent) -> None:
        """处理一条消息：感知 → 召回 → 决策 → 挂到事件上。

        入口先打一条 `INFO_ENTRY`（含会话、文本长度、模式），每个静默返回点再打
        `DEBUG_SKIP`（带 reason）。这样"消息进来没 / 卡在哪一步"永远可查——
        v0.4.0 的故障就是因为四个返回点都不留痕、只能靠查库才发现。
        """
        session_key = str(getattr(event, "unified_msg_origin", "") or "")
        self.diag.entries += 1
        self.diag.last_entry_at = time.time()
        log_event(
            LogCode.ENTRY,
            session_id=session_key,
            text_len=len(str(getattr(event, "message_str", "") or "")),
            mode=self.cfg.topic.mode,
            enabled=self.cfg.enabled,
            aggregate=self.cfg.topic.aggregation_enabled,
        )
        if not self.cfg.enabled:
            self._skip("disabled", session_id=session_key)
            return
        if self.cfg.topic.mode == "off" or session_key in self._topic_disabled:
            self._skip(
                "mode_off" if self.cfg.topic.mode == "off" else "session_off",
                session_id=session_key,
                mode=self.cfg.topic.mode,
            )
            return
        signal = self.perception.extract(event)
        if signal.is_command:
            self._skip("command", session_id=signal.session_id)
            await self.aggregation.accept(
                event,
                key=(signal.session_id, signal.user_id),
                text=signal.text,
                is_command=True,
            )
            return
        if not signal.text:
            self._skip(
                "empty_text",
                session_id=signal.session_id,
                has_message_str=hasattr(event, "message_str"),
                message_str_type=type(
                    getattr(event, "message_str", None)
                ).__name__,
                has_message_obj=hasattr(event, "message_obj"),
            )
            return
        if self.cfg.topic.aggregation_enabled:
            proceed = await self.aggregation.accept(
                event,
                key=(signal.session_id, signal.user_id),
                text=signal.text,
            )
            if not proceed:
                self._skip(
                    "aggregating_follower",
                    session_id=signal.session_id,
                )
                return
            # owner 恢复后事件正文已合并，重新感知一次。
            signal = self.perception.extract(event)
            if not signal.text:
                self._skip("empty_after_aggregate", session_id=signal.session_id)
                return
        session_id = signal.session_id
        recent = self._recent.setdefault(
            session_id,
            deque(maxlen=max(1, self.cfg.topic.judge_history_turns) + 1),
        )
        recent_list = list(recent)
        recent.append(signal.text)
        state = self.topic_sm.load(session_id)
        self.store.ensure_session(session_id, signal.user_id)
        if not state.stack:
            state = self._ensure_initial_topic(signal, state)
        relationship = self.relationship.load(session_id)
        candidates = self.memory.recall_candidates(signal, state)
        old_topic = state.focus()
        verdict = None
        if self.topic_judge is not None:
            try:
                verdict = await self.topic_judge.judge(
                    session_id=session_id,
                    message=signal.text,
                    state=state,
                    recent=recent_list,
                    last_assistant=self._last_assistant.get(session_id, ""),
                )
            except Exception:  # noqa: BLE001
                verdict = None
        debug = self.cfg.topic.debug_mode
        switched = False
        try:
            decision = self.cognition.decide(
                signal, state, candidates, relationship, verdict=verdict
            )
        except Exception as exc:  # noqa: BLE001
            log_event(LogCode.STORE_ERROR, session_id=session_id, error=str(exc))
            return
        if modes.should_auto_switch(
            self.cfg.topic.mode,
            verdict,
            self.cfg.topic.auto_threshold,
            debug,
        ):
            await self._auto_new_conversation(event, signal, state)
            switched = True
            state = self.topic_sm.load(session_id)
            decision = self.cognition.decide(
                signal,
                state,
                [],
                relationship,
                forced_action=TopicAction.CONTINUE,
            )
        # 软切与硬切都要归档旧话题：之前硬切把 old_topic_id 置空，导致
        # `auto` 模式是细节丢失最严重的一条路（见诊断报告 3.2）。
        archive_old_topic = None
        if old_topic is not None and (
            switched
            or decision.topic_action
            in (TopicAction.SOFT_BOUNDARY, TopicAction.HARD_SWITCH)
        ):
            archive_old_topic = old_topic.topic_id
        if modes.should_persist_state(self.cfg.topic.mode, debug):
            self._apply_topic_action(signal, state, decision, force_hard=False)
            self._pending_old_topic[session_id] = archive_old_topic
        else:
            self._pending_old_topic[session_id] = None
        self._pending_emotion[session_id] = signal.emotion_score
        self._pending_sticky[session_id] = signal.sticky_hit
        event.set_extra(DECISION_EXTRA, decision)
        self.diag.turns += 1
        self.diag.decisions += 1
        self.diag.last_decision_at = time.time()
        self.diag.soft_switches += int(decision.topic_action == TopicAction.SOFT_BOUNDARY)
        self.diag.hard_switches += int(
            decision.topic_action == TopicAction.HARD_SWITCH or switched
        )
        self.diag.recall_items += len(decision.recall_items)
        self.diag.dropped_items += len(decision.dropped_items)
        self.diag.injected_tokens += decision.estimated_tokens
        log_event(
            LogCode.DECISION,
            session_id=session_id,
            turn_id=decision.turn_id,
            action=decision.topic_action.value,
            persona=decision.persona_mode.value,
            tokens=decision.estimated_tokens,
        )

    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """唯一注入点：常驻注入 CTM 决策块 + 长期记忆块。"""
        if not self.cfg.enabled:
            self._skip("llm_request_disabled", session_id=str(getattr(
                event, "unified_msg_origin", "") or ""))
            return
        session_id = event.unified_msg_origin
        self.diag.llm_requests += 1
        log_event(
            LogCode.LLM_REQUEST,
            session_id=session_id,
            mode=self.cfg.topic.mode,
            prompt_len=len(str(getattr(req, "prompt", "") or "")),
            has_decision=isinstance(event.get_extra(DECISION_EXTRA), TurnDecision),
        )
        await self._inject_longterm(event, req)
        if not modes.should_inject(
            self.cfg.topic.mode, self.cfg.topic.debug_mode
        ) or session_id in self._topic_disabled:
            self._skip(
                "inject_off",
                session_id=session_id,
                mode=self.cfg.topic.mode,
                debug=self.cfg.topic.debug_mode,
            )
            return
        decision = event.get_extra(DECISION_EXTRA)
        if not isinstance(decision, TurnDecision):
            decision = await self._fallback_decision(event)
        if decision is None:
            # 曾经这里有个 `if self.diag.decisions:` 门禁——计数为 0 恰恰是
            # 最需要留痕的时刻（"入口被抢占"整类故障因此完全静默）。
            self.diag.no_decision += 1
            log_event(
                LogCode.NO_DECISION,
                session_id=session_id,
                reason=self._fallback_reason,
            )
            return
        self._inject_decision(req, decision, session_id)

    async def _fallback_decision(
        self, event: AstrMessageEvent
    ) -> TurnDecision | None:
        """消息入口被抢占时的兜底判定（只注入，不改话题状态）。

        与 v0.4.0 的差别：**没有话题栈也要兜底**。栈只由 `on_message` 建立，
        而兜底本来就是给"on_message 没跑到"准备的；旧逻辑 `if not state.stack:
        return None` 会形成死锁——入口被抢占 → 栈永远为空 → 兜底永远放弃。

        返回 None 时把原因写进 `self._fallback_reason`，由调用方记日志。
        """
        self._fallback_reason = "unknown"
        if not self.cfg.topic.detect_in_hook_enabled:
            self._fallback_reason = "fallback_disabled"
            return None
        if self.topic_judge is None:
            self._fallback_reason = "judge_disabled"
            return None
        if not modes.should_inject(
            self.cfg.topic.mode, self.cfg.topic.debug_mode
        ):
            self._fallback_reason = "inject_off"
            return None
        if event.unified_msg_origin in self._topic_disabled:
            self._fallback_reason = "session_off"
            return None
        signal = self.perception.extract(event)
        if not signal.text:
            self._fallback_reason = "empty_text"
            return None
        if signal.is_command:
            self._fallback_reason = "command"
            return None
        session_id = signal.session_id
        state = self.topic_sm.load(session_id)
        if not state.stack:
            state = self._ephemeral_state(signal, state)
            self._fallback_reason = "empty_stack_recovered"
        relationship = self.relationship.load(session_id)
        recent_items = list(self._recent.get(session_id, ()))
        recent = recent_items[-max(1, self.cfg.topic.judge_history_turns) :]
        try:
            verdict = await self.topic_judge.judge(
                session_id=session_id,
                message=signal.text,
                state=state,
                recent=recent,
                last_assistant=self._last_assistant.get(session_id, ""),
            )
            decision = self.cognition.decide(
                signal, state, [], relationship, verdict=verdict
            )
        except Exception as exc:  # noqa: BLE001
            self._fallback_reason = f"decide_error:{exc}"
            return None
        if self._fallback_reason == "unknown":
            self._fallback_reason = "ok"
        log_event(
            LogCode.PREEMPTED,
            session_id=session_id,
            action=decision.topic_action.value,
            reason=self._fallback_reason,
        )
        return decision

    def _ephemeral_state(self, signal: Any, state: Any) -> Any:
        """构造只存在于内存的话题状态（不写库），供空栈兜底判定使用。"""
        from .ctm.topic_state import TopicNode, TopicState

        now = time.time()
        try:
            title = self.topic_sm.title_for(getattr(signal, "text", ""))
        except Exception:  # noqa: BLE001
            title = ""
        title = (title or "").strip() or "（临时话题）"
        node = TopicNode(
            topic_id=-1,
            title=title,
            created_at=now,
            last_active_at=now,
        )
        return TopicState(
            session_id=str(getattr(state, "session_id", "") or ""),
            stack=[node],
            current_topic=title,
            previous_topic=getattr(state, "previous_topic", None),
            turns_since_switch=int(getattr(state, "turns_since_switch", 0) or 0),
            last_switch_at=float(getattr(state, "last_switch_at", 0.0) or 0.0),
            soft_boundary_pending=bool(
                getattr(state, "soft_boundary_pending", False)
            ),
        )

    def _inject_decision(
        self,
        req: ProviderRequest,
        decision: TurnDecision,
        session_id: str,
    ) -> None:
        """把决策渲染成 Prompt 块并追加（唯一渲染点）。"""
        try:
            built = self.builder.build(decision)
            payload = self.builder.cap_by_ratio(
                built,
                req.system_prompt or "",
                self.cfg.injection.max_prompt_growth_ratio,
            )
        except Exception as exc:  # noqa: BLE001
            self.diag.injection_errors += 1
            log_event(LogCode.INJECTION_ERROR, session_id=session_id, error=str(exc))
            return
        if payload != built:
            # 注入被增幅上限裁剪：这不是故障，但要让"为什么只有这么点内容"可查。
            log_event(
                LogCode.PROMPT_GROWTH_CAPPED,
                session_id=session_id,
                before_chars=len(built),
                after_chars=len(payload),
                ratio=self.cfg.injection.max_prompt_growth_ratio,
            )
        if decision.dropped_items and not decision.recall_items:
            # 预算装不下任何一条：把预算调大或降低召回条数。
            log_event(
                LogCode.BUDGET_OVERFLOW,
                session_id=session_id,
                dropped=len(decision.dropped_items),
                budget=decision.injection_budget,
            )
        if not payload.strip():
            self._skip("empty_injection", session_id=session_id)
            return
        if req.system_prompt:
            req.system_prompt = f"{req.system_prompt}\n{payload}"
        else:
            req.system_prompt = payload
        self.memory.mark_recalled([item.memory_id for item in decision.recall_items])
        if decision.recall_items:
            log_event(
                LogCode.DROPPED,
                session_id=session_id,
                kept=len(decision.recall_items),
                dropped=len(decision.dropped_items),
            )

    async def on_llm_response(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        """响应后投递归档；同时调度长期记忆抽取。"""
        if not self.cfg.enabled:
            return
        session_id = event.unified_msg_origin
        if self.cfg.topic.debug_mode:
            return
        response_text = getattr(response, "completion_text", "") or ""
        user_text = event.message_str or ""
        self._last_assistant[session_id] = response_text[:500]
        decision = event.get_extra(DECISION_EXTRA)
        if isinstance(decision, TurnDecision):
            # 摘要要用"切分点之前"的整段对话，所以先取轨迹、后记本轮。
            job = ArchiveJob(
                turn_id=decision.turn_id,
                session_id=session_id,
                user_text=user_text,
                response_text=response_text,
                topic_action=decision.topic_action.value,
                persona_mode=decision.persona_mode.value,
                emotional_salience=float(
                    self._pending_emotion.pop(session_id, 0.0)
                ),
                sticky_hit=self._pending_sticky.pop(session_id, False),
                occurred_at=time.time(),
                old_topic_id=self._pending_old_topic.pop(session_id, None),
                decision_json=json.dumps(decision.to_dict(), ensure_ascii=False),
                injected_tokens=decision.estimated_tokens,
                dropped_json=json.dumps(decision.dropped_items, ensure_ascii=False),
                topic_transcript=self._transcript(session_id),
            )
            try:
                if not await self.archiver.enqueue(job):
                    log_event(LogCode.ARCHIVE_DROPPED, session_id=session_id)
            except Exception:  # noqa: BLE001
                pass
            event.set_extra(DECISION_EXTRA, None)
        self._remember_turn(session_id, user_text, response_text)
        self._observe_longterm(event, response_text)

    # ------------------------------------------------------------------ 归档辅助
    async def _summarize_with_model(self, prompt: str, session_id: str = "") -> str:
        """用当前会话模型生成话题摘要；失败返回空串（由归档器回退到规则摘要）。"""
        provider = await self._resolve_provider(
            session_id, self.cfg.topic.judge_provider_id
        )
        if provider is None:
            return ""
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    system_prompt=SUMMARY_SYSTEM_PROMPT,
                    contexts=[{"role": "user", "content": prompt}],
                    request_max_retries=1,
                ),
                timeout=max(3.0, float(self.cfg.topic.judge_timeout_seconds) * 2),
            )
        except Exception as exc:  # noqa: BLE001 - 摘要失败不能影响归档其余阶段
            log_event(LogCode.STORE_ERROR, session_id=session_id, error=f"summarize:{exc}")
            return ""
        return str(getattr(response, "completion_text", "") or "").strip()

    def _remember_turn(self, session_id: str, user_text: str, assistant_text: str) -> None:
        """把一轮对话放进滚动缓冲，供切分时生成整段摘要。"""
        bucket = self._turn_buffer.get(session_id)
        if bucket is None:
            bucket = deque(maxlen=TRANSCRIPT_MAX_TURNS)
            self._turn_buffer[session_id] = bucket
        bucket.append((user_text or "", assistant_text or ""))

    def _transcript(self, session_id: str) -> str:
        """把当前缓冲拼成"用户/助手"交替的文本（不含本轮，调用方先取再记）。"""
        bucket = self._turn_buffer.get(session_id)
        if not bucket:
            return ""
        parts: list[str] = []
        for user_text, assistant_text in bucket:
            if user_text:
                parts.append(f"用户：{user_text}")
            if assistant_text:
                parts.append(f"助手：{assistant_text}")
        text = "\n".join(parts)
        return text[-TRANSCRIPT_MAX_CHARS:]

    # ------------------------------------------------------------------ 状态落地
    def _ensure_initial_topic(self, signal: Any, state: Any) -> Any:
        """空栈会话的初始话题（否则永远不会触发软边界）。"""
        try:
            title = self.topic_sm.title_for(getattr(signal, "text", ""))
            topic_id = self.store.create_topic(state.session_id, title)
            from .ctm.topic_state import TopicNode

            node = TopicNode(
                topic_id=topic_id,
                title=title,
                created_at=time.time(),
                last_active_at=time.time(),
            )
            state.stack.append(node)
            state.current_topic = title
            self.topic_sm.save(state)
        except Exception as exc:  # noqa: BLE001
            log_event(LogCode.STORE_ERROR, session_id=state.session_id, error=str(exc))
        return state

    async def _auto_new_conversation(
        self,
        event: AstrMessageEvent,
        signal: Any,
        state: Any,
    ) -> None:
        """mode=auto：新建对话（等价 /new），重置话题栈并建档。"""
        try:
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr is not None:
                await conv_mgr.new_conversation(
                    event.unified_msg_origin, event.get_platform_id()
                )
        except Exception as exc:  # noqa: BLE001
            log_event(LogCode.STORE_ERROR, session_id=signal.session_id, error=str(exc))
        try:
            self._turn_buffer.pop(signal.session_id, None)
            self.topic_sm.hard_switch(signal, state)
            self._ensure_initial_topic(signal, state)
        except Exception as exc:  # noqa: BLE001
            log_event(LogCode.STORE_ERROR, session_id=signal.session_id, error=str(exc))
        if self.cfg.topic.new_conversation_notice:
            try:
                from astrbot.api.event import MessageChain
                from astrbot.api.message_components import Plain

                await event.send(MessageChain([Plain("（已开启新话题）")]))
            except Exception:  # noqa: BLE001
                pass

    async def manual_new_topic(
        self,
        event: AstrMessageEvent,
        title: str = "",
    ) -> str:
        """`/ctm topic new`：手动新建对话并重置话题。"""
        signal = self.perception.extract(event)
        try:
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr is not None:
                await conv_mgr.new_conversation(
                    event.unified_msg_origin, event.get_platform_id()
                )
            state = self.topic_sm.load(signal.session_id)
            previous = state.focus()
            if previous is not None:
                # 手动 /new 也要给旧话题留摘要（报告 3.2：否则细节全丢）
                self._pending_old_topic[signal.session_id] = previous.topic_id
            self._turn_buffer.pop(signal.session_id, None)
            self.topic_sm.hard_switch(signal, state)
            new_title = (title or "").strip() or "新话题"
            topic_id = self.store.create_topic(signal.session_id, new_title)
            from .ctm.topic_state import TopicNode

            now = time.time()
            state.stack = [
                TopicNode(
                    topic_id=topic_id,
                    title=new_title,
                    created_at=now,
                    last_active_at=now,
                )
            ]
            state.current_topic = new_title
            state.soft_boundary_pending = True
            self.topic_sm.save(state)
        except Exception as exc:  # noqa: BLE001
            return f"新建话题失败：{exc}"
        return f"已开启新对话，话题：{new_title}"

    def _apply_topic_action(
        self,
        signal: Any,
        state: Any,
        decision: TurnDecision,
        *,
        force_hard: bool = False,
    ) -> None:
        """把决策里的话题动作落到状态机上并保存。"""
        try:
            if force_hard or decision.topic_action == TopicAction.HARD_SWITCH:
                self.topic_sm.hard_switch(signal, state)
                return
            if decision.topic_action == TopicAction.SOFT_BOUNDARY:
                title = self.topic_sm.title_for(signal.text)
                self.topic_sm.soft_boundary(signal, state, title)
                return
            state.turns_since_switch += 1
            focus = state.focus()
            if focus is not None:
                focus.last_active_at = time.time()
                self.store.update_topic(
                    focus.topic_id, last_active_at=focus.last_active_at
                )
            self.topic_sm.save(state)
        except Exception as exc:  # noqa: BLE001
            log_event(LogCode.STORE_ERROR, session_id=signal.session_id, error=str(exc))

    # ------------------------------------------------------------------ 长期记忆
    async def _inject_longterm(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """召回长期记忆并作为临时内容块注入。"""
        if self.lt is None:
            return
        group_id = str(event.get_group_id() or "")
        try:
            text = await self.lt.recall(
                session_id=event.unified_msg_origin,
                platform_id=event.get_platform_id(),
                sender_id=event.get_sender_id(),
                group_id=group_id,
                is_group=bool(group_id),
                query=(req.prompt or event.message_str or "").strip(),
            )
        except Exception:  # noqa: BLE001
            return
        if not text:
            return
        try:
            part = TextPart(text=text).mark_as_temp()
            if req.extra_user_content_parts is None:
                req.extra_user_content_parts = []
            req.extra_user_content_parts.append(part)
        except Exception:  # noqa: BLE001
            req.system_prompt = f"{req.system_prompt or ''}\n{text}"

    def _observe_longterm(self, event: AstrMessageEvent, response_text: str) -> None:
        if self.lt is None:
            return
        try:
            self.lt.observe_turn(
                session_id=event.unified_msg_origin,
                platform_id=event.get_platform_id(),
                sender_id=event.get_sender_id(),
                group_id=str(event.get_group_id() or ""),
                is_group=bool(event.get_group_id()),
                user_text=user_text,
                assistant_text=response_text,
            )
        except Exception:  # noqa: BLE001
            pass

    async def _get_provider(self, session_id: str) -> Any:
        """解析长期记忆抽取用的 chat provider。"""
        return await self._resolve_provider(
            session_id, self.cfg.longterm.extract_provider_id
        )

    async def _get_judge_provider(self, session_id: str) -> Any:
        """解析话题判定用的 chat provider。"""
        return await self._resolve_provider(
            session_id, self.cfg.topic.judge_provider_id
        )

    async def _resolve_provider(self, session_id: str, provider_id: str = "") -> Any:
        """解析 chat provider（兼容 4.26 同步接口与 4.29 异步接口）。"""

        try:
            if provider_id:
                manager = getattr(self.context, "provider_manager", None)
                if manager is not None:
                    provider = await manager.get_provider_by_id(provider_id)
                    if provider is not None:
                        return provider
            getter = getattr(self.context, "get_using_provider_async", None)
            if callable(getter):
                return await getter(session_id)
            return self.context.get_using_provider(session_id)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ 命令
    async def on_command(self, event: AstrMessageEvent):
        """执行 `/ctm` 子命令（仅管理员）。"""
        if not event.is_admin():
            yield event.plain_result("此命令仅管理员可用。")
            return
        text = await self.commands.dispatch(event, event.message_str)
        yield event.plain_result(text)

    # ------------------------------------------------------------------ LLM 工具
    @filter.llm_tool(name="memory_search")
    async def memory_search_tool(
        self,
        event: AstrMessageEvent,
        query: str,
        scope: str = "auto",
        limit: int = 8,
    ) -> str:
        """检索长期记忆（用户画像、偏好、约定、事件）。

        Args:
            query(string): 检索关键词或短句
            scope(string): auto / user / group
            limit(number): 返回条数 1-20
        """
        if self.lt is None or not self.cfg.longterm.tools_enabled:
            return "长期记忆未启用。"
        rows = self.lt.search(
            session_id=event.unified_msg_origin,
            platform_id=event.get_platform_id(),
            sender_id=event.get_sender_id(),
            group_id=str(event.get_group_id() or ""),
            is_group=bool(event.get_group_id()),
            query=query,
            scope=scope,
            limit=limit,
        )
        if not rows:
            return "没有找到相关记忆。"
        return "\n".join(
            f"- [{row['kind']}] {row['content']} (id:{str(row['id'])[:8]})"
            for row in rows
        )

    @filter.llm_tool(name="memory_save")
    async def memory_save_tool(
        self,
        event: AstrMessageEvent,
        content: str,
        kind: str = "fact",
        slot_key: str = "",
    ) -> str:
        """保存一条长期记忆（仅当用户明确要求记住，或信息稳定且长期有用）。

        Args:
            content(string): 一句自包含陈述（<=80 字）
            kind(string): profile / preference / event / task / fact
            slot_key(string): 可选槽位，如 name / city
        """
        if self.lt is None or not self.cfg.longterm.tools_enabled:
            return "长期记忆未启用。"
        item = self.lt.save(
            session_id=event.unified_msg_origin,
            platform_id=event.get_platform_id(),
            sender_id=event.get_sender_id(),
            group_id=str(event.get_group_id() or ""),
            is_group=bool(event.get_group_id()),
            content=content,
            kind=kind,
            slot_key=slot_key,
        )
        if item is None:
            return "记忆被拒绝（空、重复或敏感内容）。"
        return f"已记住（id:{str(item['id'])[:8]}）。"

    @filter.llm_tool(name="memory_forget")
    async def memory_forget_tool(
        self,
        event: AstrMessageEvent,
        memory_id: str = "",
        query: str = "",
    ) -> str:
        """删除长期记忆（用户要求遗忘或纠正错误信息时）。

        Args:
            memory_id(string): memory_search 返回的 id 前缀或完整 id
            query(string): 按内容包含匹配删除
        """
        if self.lt is None or not self.cfg.longterm.tools_enabled:
            return "长期记忆未启用。"
        deleted = self.lt.forget(
            session_id=event.unified_msg_origin,
            platform_id=event.get_platform_id(),
            sender_id=event.get_sender_id(),
            group_id=str(event.get_group_id() or ""),
            is_group=bool(event.get_group_id()),
            memory_id=memory_id,
            query=query,
        )
        return f"已删除 {deleted} 条记忆。" if deleted else "没有找到匹配记忆。"

    # ------------------------------------------------------------------ 内部
    def _resolve_db_path(self) -> Path:
        """把配置里的 db_path 解析成绝对路径（相对路径基于插件数据目录）。"""
        raw = Path(self.cfg.db_path)
        if raw.is_absolute():
            return raw
        candidate = (self.data_dir / raw).resolve()
        if self.data_dir.resolve() not in candidate.parents and candidate != self.data_dir.resolve():
            return self.data_dir / "ctm.sqlite3"
        return candidate

    def _data_dir(self) -> str:
        """插件数据目录。"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            base = Path(get_astrbot_plugin_data_path())
        except Exception:  # noqa: BLE001
            base = Path.home() / ".astrbot" / "data" / "plugin_data"
        return str(base / PLUGIN_NAME)

    def _emit_log(self, level: str, fields: dict[str, Any]) -> None:
        """把结构化日志转给 AstrBot logger。"""
        payload = " ".join(f"{key}={value}" for key, value in fields.items())
        message = f"[{PLUGIN_NAME}] {payload}"
        if level == "ERROR":
            logger.error(message)
        elif level == "WARNING":
            logger.warning(message)
        elif level == "DEBUG":
            logger.debug(message)
        else:
            logger.info(message)

    def _skip(self, reason: str, *, session_id: str = "-", **fields: Any) -> None:
        r"""记录一次"静默返回"。

        级别是 DEBUG（不刷屏），但**一定留痕**：用
        `grep -F '[astrbot_plugin_ctm]' | grep DEBUG_SKIP`（面板日志把级别调到
        DEBUG）就能看到每条消息卡在哪一步。

        Args:
            reason: 稳定的原因码（`disabled` / `mode_off` / `empty_text` / ...）。
            session_id: 会话标识。
            fields: 附加诊断字段。
        """
        self.diag.skips += 1
        log_event(LogCode.SKIP, session_id=session_id, reason=reason, **fields)

    def _agg_anomaly(self, reason: str, key: tuple[str, str]) -> None:
        """聚合器异常（owner 等待超时、轮次溢出）→ 计数并记 WARN。"""
        self.diag.aggregation_anomalies += 1
        log_event(LogCode.AGGREGATION, session_id=key[0] if key else "-", reason=reason)
