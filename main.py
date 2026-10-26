"""AstrBot 接线层：消息入口 → 联合决策 → 单次注入 → 异步归档。

## 为什么 handler 是**模块级函数**而逻辑在实例方法里

AstrBot 派发 handler 时调用的是 `handler(event, ...)` 绑定形式；但在部分环境
（实测过 4.28.1 + webchat）注册进去的是**未绑定的类函数**，此时 `self` 是事件
对象，任何 `self.xxx` 都抛 `AttributeError`，而派发层会 `stop_event()`，
同一条消息后面的插件全部不执行。所以入口用"模块级函数 + 模块级单例"，
业务逻辑仍然是实例方法，便于单测直接构造实例。

## 每轮时序（设计文档 §5）

```
消息事件 ──▶ on_message       聚合（v0.1 占位）→ Perception → 决策 → set_extra
          ──▶ on_llm_request  读取决策 → build（预算装箱）→ append system_prompt
          ──▶ LLM
          ──▶ on_llm_response 入队归档（不等结果）
```

四条不可违反的约定：

1. 两个 handler 都**吞掉所有异常**（`CancelledError` 除外），绝不让异常外泄到
   派发层——插件出错不能变成"用户消息没人回"；
2. `on_llm_request` 只追加、不 return 阻断；硬切不在这一层执行；
3. 决策通过 `event.set_extra("ctm_decision", ...)` 传递，缺失时**不注入**并记
   `WARN_NO_DECISION`，而不是临时重算（重算会与消息入口的感知不一致）；
4. 注入量默认不超过原 system_prompt 的 25%，超了截断并记
   `WARN_PROMPT_GROWTH_CAPPED`。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star

from .config import PLUGIN_NAME, Config, load_config
from .ctm import adapters as adapters_mod
from .ctm.archiver import Archiver
from .ctm.cognition import Cognition, TurnDecision
from .ctm.commands import CtmCommands
from .ctm.injection import InjectionBuilder
from .ctm.memory_store import MemoryStore
from .ctm.observability import Diag
from .ctm.perception import Perception, PerceptionSignal
from .ctm.relationship import RelationshipManager
from .ctm.store import Store
from .ctm.topic_state import TopicState, TopicStateMachine

MESSAGE_PRIORITY = 1000
"""消息入口优先级：插件层顶格，而不是"比别人高一点"。

理由同 astrbot_plugin_msg_aggregator：1~100 是常规区间，1000 表示"处在插件层
的顶层"。但优先级再高也只是"尽量先跑"——真正的兜底是 `on_llm_request`
不依赖消息入口一定先执行：拿不到决策就不注入、并记 WARN。
"""

DECISION_EXTRA = "ctm_decision"
"""事件上的决策键。同一事件对象贯穿"消息派发 → LLM 请求"，随事件回收。"""

_INSTANCE: "CtmPlugin | None" = None
"""模块级单例；`terminate()` 时置空，防止 handler 拿到半销毁实例。"""


def _get_instance() -> "CtmPlugin | None":
    """返回模块级单例。

    Returns:
        插件实例；尚未构造时返回 None（handler 会直接放行）。
    """
    return _INSTANCE


@filter.event_message_type(filter.EventMessageType.ALL, priority=MESSAGE_PRIORITY)
async def ctm_on_event(event: AstrMessageEvent) -> None:
    """消息入口：感知、决策、挂决策；不阻断事件。

    Args:
        event: 消息事件。

    Returns:
        None。
    """
    plugin = _get_instance()
    if plugin is None:
        return
    try:
        await plugin.on_message(event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 绝不让异常外泄到派发层
        logger.exception(f"[{PLUGIN_NAME}] 消息入口异常（已兜住）：{exc}")


@filter.on_llm_request()
async def ctm_on_llm_request(event: AstrMessageEvent, req: ProviderRequest) -> None:
    """LLM 请求前：唯一注入点。

    Args:
        event: 消息事件。
        req: 待发送的请求对象；只追加 `system_prompt`。

    Returns:
        None。**不 return 结果、不阻断请求**。
    """
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
    """LLM 响应后：投递归档任务。

    Args:
        event: 消息事件。
        response: 模型响应。

    Returns:
        None。
    """
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
    """`/ctm` 管理命令入口。

    Args:
        event: 消息事件。

    Yields:
        MessageEventResult：命令输出。
    """
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

        self._raw_config: dict[str, Any] = config if isinstance(config, dict) else {}
        self.cfg: Config = load_config(config)  # TODO(v0.1) 实现后才有值
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
        self.archiver = Archiver(self.store, self.memory, self.relationship, self.cfg)
        self.adapter: adapters_mod.MemoryAdapter | None = None
        self._started = False

        logger.info(f"[{PLUGIN_NAME}] 实例已构造 | 数据目录={self.data_dir}")

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        """插件加载：建库、选适配器、起归档 worker。

        Returns:
            None。

        TODO(v0.1):
            1. `store.init()`；失败时记 ERROR 并把 `enabled` 降级为 False；
            2. `adapter = adapters.create_adapter(...)`；
            3. `archiver.start()`；
            4. 打印生效配置（脱敏）；
            5. 探测重复注入（`_detect_duplicate_injection`）。
        """
        self._started = True
        logger.info(f"[{PLUGIN_NAME}] initialize（v0.1 骨架：装配逻辑待实现）")

    async def terminate(self) -> None:
        """插件卸载：flush 归档、checkpoint、关连接。

        Returns:
            None。

        TODO(v0.1): `archiver.stop(5.0)` → `store.checkpoint()` → `store.close()`；
            `_INSTANCE = None`（防止 handler 拿到半销毁实例）。
        """
        self._started = False
        logger.info(f"[{PLUGIN_NAME}] terminate（v0.1 骨架：收尾逻辑待实现）")

    # ------------------------------------------------------------------ 消息入口
    async def on_message(self, event: AstrMessageEvent) -> None:
        """处理一条消息：感知 → 召回 → 决策 → 挂到事件上。

        Args:
            event: 消息事件。

        Returns:
            None。

        TODO(v0.1):
            1. 聚合接入点：v0.1 逐条处理；接入聚合层后用合并文本构造信号
               （注意不要与 msg_aggregator 同装，见 DESIGN.md §7）；
            2. `signal.is_command` 直接放行（指令不参与判定）；
            3. `topic_sm.load` → `memory.recall_candidates` → `relationship.load`；
            4. `cognition.decide`；
            5. `_apply_topic_action` 落地状态并保存；
            6. `event.set_extra(DECISION_EXTRA, decision)`；
            7. 累计 `diag` 并记 INFO_DECISION。
        """
        raise NotImplementedError("v0.1: CtmPlugin.on_message")

    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """唯一注入点。

        Args:
            event: 消息事件。
            req: 请求对象。

        Returns:
            None。

        TODO(v0.1):
            1. 取 `event.get_extra(DECISION_EXTRA)`；缺失 → WARN_NO_DECISION + 返回；
            2. `builder.build(decision)`；
            3. `builder.cap_by_ratio(payload, req.system_prompt,
               cfg.injection.max_prompt_growth_ratio)`；
            4. 追加到 `req.system_prompt`（空串时直接赋值，避免开头空行）；
            5. 记录注入量 / 丢弃项 / 决策日志；
            6. `soft_boundary_once` 时清除 `soft_boundary_pending`；
            7. 异常 → 只追加最小关系块，记 ERROR_INJECTION。
        """
        raise NotImplementedError("v0.1: CtmPlugin.on_llm_request")

    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        """响应后投递归档。

        Args:
            event: 消息事件。
            response: 模型响应。

        Returns:
            None。

        TODO(v0.1):
            1. 取决策；缺失直接返回（这条消息不归 CTM 管）；
            2. 组装 `ArchiveJob`（只放文本与快照，**不放 event 对象**）；
            3. `await archiver.enqueue(job)`；队列满记 WARN_ARCHIVE_DROPPED；
            4. `event.set_extra(DECISION_EXTRA, None)` 释放引用。
        """
        raise NotImplementedError("v0.1: CtmPlugin.on_llm_response")

    # ------------------------------------------------------------------ 状态落地
    def _apply_topic_action(
        self,
        signal: PerceptionSignal,
        state: TopicState,
        decision: TurnDecision,
        *,
        force_hard: bool = False,
    ) -> TopicState:
        """把决策里的话题动作落到状态机上并保存。

        Args:
            signal: 感知信号。
            state: 决策前状态。
            decision: 决策。
            force_hard: 显式硬切（来自命令）。

        Returns:
            新状态。

        TODO(v0.1):
            * soft_boundary → `topic_sm.soft_boundary(signal, state, title_for(text))`；
            * hard_switch 且 force_hard → `topic_sm.hard_switch(...)`；
            * 其他 → `turns_since_switch += 1`（每轮 +1），更新栈顶 last_active_at；
            * 统一 `topic_sm.save(new_state)`（一次事务，避免半更新）。
        """
        raise NotImplementedError("v0.1: CtmPlugin._apply_topic_action")

    # ------------------------------------------------------------------ 命令
    async def on_command(self, event: AstrMessageEvent):
        """执行 `/ctm` 子命令（仅管理员）。

        Args:
            event: 消息事件。

        Yields:
            MessageEventResult：命令输出。
        """
        if not event.is_admin():
            yield event.plain_result("此命令仅管理员可用。")
            return
        text = await self.commands.dispatch(event, event.message_str)
        yield event.plain_result(text)

    # ------------------------------------------------------------------ 内部
    def _resolve_db_path(self) -> Path:
        """把配置里的 db_path 解析成绝对路径。

        Returns:
            绝对路径；相对路径相对于插件数据目录（而不是 AstrBot 的 data 根），
            这样备份/卸载边界清楚。

        TODO(v0.1): 用 Path.is_absolute 判断；`..` 穿越时拒绝并回退默认文件名。
        """
        raise NotImplementedError("v0.1: CtmPlugin._resolve_db_path")

    def _data_dir(self) -> str:
        """插件数据目录。

        Returns:
            `data/plugin_data/astrbot_plugin_ctm`；取不到时回退 `~/.astrbot/data/plugin_data`。
        """
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            base = Path(get_astrbot_plugin_data_path())
        except Exception:  # noqa: BLE001 - 老版本/测试环境
            base = Path.home() / ".astrbot" / "data" / "plugin_data"
        return str(base / PLUGIN_NAME)

    def _detect_duplicate_injection(self) -> None:
        """探测已安装的同类记忆插件，提示关闭其自动注入。

        Returns:
            None。

        TODO(v0.1): 遍历 `context.get_all_stars()`，命中已知记忆插件名时记
            WARN_DUPLICATE_INJECTION（只告警，不擅自改别人的配置）。
        """
        raise NotImplementedError("v0.1: CtmPlugin._detect_duplicate_injection")

    def _clear_soft_boundary(self, session_id: str) -> None:
        """清除"边界提示待注入"标记。

        Args:
            session_id: 会话键。

        Returns:
            None。

        TODO(v0.1): 读 - 改 - 写 session_state；失败只记日志。
        """
        raise NotImplementedError("v0.1: CtmPlugin._clear_soft_boundary")

    def _minimal_relationship_line(self, decision: TurnDecision) -> str:
        """注入失败时的最小兜底文本。

        Args:
            decision: 决策（可能带关系快照）。

        Returns:
            单行关系摘要；`relationship` 为空时返回空串（宁可不注入，也不编造）。

        TODO(v0.1): 用 relationship.intimacy/tension 生成一行；不要带记忆正文。
        """
        raise NotImplementedError("v0.1: CtmPlugin._minimal_relationship_line")

    def _persist_turn_log(self, decision: TurnDecision, injected_tokens: int) -> None:
        """记录本轮决策日志。

        Args:
            decision: 决策。
            injected_tokens: 实际注入估算。

        Returns:
            None。

        TODO(v0.1): 经 Archiver 串行写（避免事件循环里做 IO）；
            在此之前进 `turn_logs` 允许晚一拍，但 turn_id 必须稳定。
        """
        raise NotImplementedError("v0.1: CtmPlugin._persist_turn_log")
