"""AstrBot 接线层：消息入口 → 联合决策 → 单次注入 → 异步归档。

模块级 handler 通过模块级单例转发到插件实例，逻辑保留在实例方法里，便于单测。
所有 hook 都吞掉异常（CancelledError 除外），插件出错不能变成"用户消息没人回"。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart

from .config import PLUGIN_NAME, Config, load_config
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
from .ctm.topic_state import TopicStateMachine

MESSAGE_PRIORITY = 1000
DECISION_EXTRA = "ctm_decision"

_INSTANCE: "CtmPlugin | None" = None


def _get_instance() -> "CtmPlugin | None":
    """返回模块级单例；尚未构造时返回 None。"""
    return _INSTANCE


@filter.event_message_type(filter.EventMessageType.ALL, priority=MESSAGE_PRIORITY)
async def ctm_on_event(event: AstrMessageEvent) -> None:
    """消息入口：感知、决策、挂决策；不阻断事件。"""
    plugin = _get_instance()
    if plugin is None:
        return
    try:
        await plugin.on_message(event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[{PLUGIN_NAME}] 消息入口异常（已兜住）：{exc}")


@filter.on_llm_request()
async def ctm_on_llm_request(event: AstrMessageEvent, req: ProviderRequest) -> None:
    """LLM 请求前：唯一注入点。"""
    plugin = _get_instance()
    if plugin is None:
        return
    try:
        await plugin.on_llm_request(event, req)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[{PLUGIN_NAME}] 注入阶段异常（已兜住）：{exc}")


@filter.on_llm_response()
async def ctm_on_llm_response(event: AstrMessageEvent, response: LLMResponse) -> None:
    """LLM 响应后：投递归档任务。"""
    plugin = _get_instance()
    if plugin is None:
        return
    try:
        await plugin.on_llm_response(event, response)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[{PLUGIN_NAME}] 归档投递异常（已兜住）：{exc}")


@filter.command("ctm")
async def ctm_command(event: AstrMessageEvent):
    """`/ctm` 管理命令入口。"""
    plugin = _get_instance()
    if plugin is None:
        return
    async for result in plugin.on_command(event):
        yield result


class CtmPlugin(Star):
    """对话认知中枢插件主体。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        """初始化（只装配，不连接数据库）。

        Args:
            context: AstrBot 上下文。
            config: 插件配置（通常是 `AstrBotConfig`，dict 子类）。
        """
        super().__init__(context)
        global _INSTANCE
        _INSTANCE = self

        self.cfg: Config = load_config(config)
        self.diag = Diag()
        self.data_dir = Path(self._data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.store = Store(self._resolve_db_path())
        self.perception = Perception(self.cfg.topic)
        self.topic_sm = TopicStateMachine(self.store, self.cfg.topic)
        self.memory = MemoryStore(self.store, self.cfg.memory)
        self.relationship = RelationshipManager(self.store, self.cfg.relationship)
        self.builder = InjectionBuilder(self.cfg)
        self.cognition = Cognition(self.cfg, self.builder, self.topic_sm)
        self.commands = CtmCommands(self)
        self.archiver = Archiver(
            self.store, self.memory, self.relationship, self.cfg
        )
        self.lt: LongTermMemory | None = None
        self._lt_store: LongTermStore | None = None
        self._started = False
        self._pending_old_topic: dict[str, int | None] = {}
        self._pending_emotion: dict[str, float] = {}
        self._pending_sticky: dict[str, bool] = {}
        logger.info(f"[{PLUGIN_NAME}] 实例已构造 | 数据目录={self.data_dir}")

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        """插件加载：建库、起归档 worker、起长期记忆 worker。"""
        if not self.cfg.enabled:
            logger.info(f"[{PLUGIN_NAME}] 配置未启用，插件不工作。")
            return
        configure(self._emit_log)
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
        logger.info(
            f"[{PLUGIN_NAME}] 已启动 | db={self._resolve_db_path()} | "
            f"longterm={'on' if self.lt else 'off'}"
        )

    async def terminate(self) -> None:
        """插件卸载：flush 归档、checkpoint、关连接。"""
        global _INSTANCE
        self._started = False
        try:
            await self.archiver.stop(5.0)
        except Exception:  # noqa: BLE001
            pass
        if self.lt is not None:
            try:
                await self.lt.close()
            except Exception:  # noqa: BLE001
                pass
        if self._lt_store is not None:
            self._lt_store.close()
        try:
            self.store.checkpoint()
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        _INSTANCE = None
        logger.info(f"[{PLUGIN_NAME}] 已卸载。")

    # ------------------------------------------------------------------ 消息入口
    async def on_message(self, event: AstrMessageEvent) -> None:
        """处理一条消息：感知 → 召回 → 决策 → 挂到事件上。"""
        if not self.cfg.enabled:
            return
        signal = self.perception.extract(event)
        if signal.is_command or not signal.text:
            return
        session_id = signal.session_id
        state = self.topic_sm.load(session_id)
        self.store.ensure_session(session_id, signal.user_id)
        if not state.stack:
            state = self._ensure_initial_topic(signal, state)
        relationship = self.relationship.load(session_id)
        candidates = self.memory.recall_candidates(signal, state)
        old_topic = state.focus()
        try:
            decision = self.cognition.decide(signal, state, candidates, relationship)
        except Exception as exc:  # noqa: BLE001
            log_event(LogCode.STORE_ERROR, session_id=session_id, error=str(exc))
            return
        self._apply_topic_action(signal, state, decision, force_hard=False)
        self._pending_old_topic[session_id] = (
            old_topic.topic_id
            if old_topic and decision.topic_action == TopicAction.SOFT_BOUNDARY
            else None
        )
        self._pending_emotion[session_id] = signal.emotion_score
        self._pending_sticky[session_id] = signal.sticky_hit
        event.set_extra(DECISION_EXTRA, decision)
        self.diag.turns += 1
        self.diag.decisions += 1
        self.diag.soft_switches += int(decision.topic_action == TopicAction.SOFT_BOUNDARY)
        self.diag.hard_switches += int(decision.topic_action == TopicAction.HARD_SWITCH)
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
            return
        session_id = event.unified_msg_origin
        await self._inject_longterm(event, req)
        decision = event.get_extra(DECISION_EXTRA)
        if decision is None:
            if self.diag.decisions:
                self.diag.no_decision += 1
                log_event(LogCode.NO_DECISION, session_id=session_id)
            return
        if not isinstance(decision, TurnDecision):
            return
        try:
            payload = self.builder.build(decision)
            payload = self.builder.cap_by_ratio(
                payload,
                req.system_prompt or "",
                self.cfg.injection.max_prompt_growth_ratio,
            )
        except Exception as exc:  # noqa: BLE001
            self.diag.injection_errors += 1
            log_event(LogCode.INJECTION_ERROR, session_id=session_id, error=str(exc))
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
        response_text = getattr(response, "completion_text", "") or ""
        decision = event.get_extra(DECISION_EXTRA)
        if isinstance(decision, TurnDecision):
            job = ArchiveJob(
                turn_id=decision.turn_id,
                session_id=session_id,
                user_text=event.message_str or "",
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
            )
            try:
                if not await self.archiver.enqueue(job):
                    log_event(LogCode.ARCHIVE_DROPPED, session_id=session_id)
            except Exception:  # noqa: BLE001
                pass
            event.set_extra(DECISION_EXTRA, None)
        self._observe_longterm(event, response_text)

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
                user_text=event.message_str or "",
                assistant_text=response_text,
            )
        except Exception:  # noqa: BLE001
            pass

    async def _get_provider(self, session_id: str) -> Any:
        """解析 chat provider（兼容 4.26 的同步接口与 4.29 的异步接口）。"""
        provider_id = self.cfg.longterm.extract_provider_id
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
        else:
            logger.info(message)
