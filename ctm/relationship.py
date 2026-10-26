"""关系状态：亲密度 / 张力 / 未完成事项。

这是慢变量，不是情绪计。设计文档 §3.6 的更新规则刻意很小步：

* 正向互动 `intimacy += 0.005`，冲突未缓解 `tension += 0.02`；
* 每轮 `tension *= 0.98`，每天 `intimacy -= 0.01`；
* 承诺（"答应/我会/明天…"）写入 `pending_promises`，用户确认完成后移除。

之所以不直接读情绪分去改关系：单轮情绪噪声太大，一次"烦"就把关系扣分，
会让模型对用户过度警觉，这是陪伴场景最常见的翻车方式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import RelationshipConfig

#: 亲密度/张力的分级边界，供注入模板展示。
LEVEL_LOW = "low"
LEVEL_MEDIUM = "medium"
LEVEL_HIGH = "high"


@dataclass
class RelationshipState:
    """一个会话的关系快照。

    Attributes:
        session_id: 会话键。
        intimacy: 亲密度 0..1，初始 0.5。
        tension: 当前张力 0..1，初始 0。
        pending_promises: 未完成事项，元素形如
            `{"text": "明天提醒用户吃药", "created_at": 1700000000.0, "done": False}`。
        updated_at: 最近更新时间。
    """

    session_id: str
    intimacy: float = 0.5
    tension: float = 0.0
    pending_promises: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = 0.0


class RelationshipManager:
    """关系状态的唯一读写者。"""

    def __init__(self, store: Any, cfg: RelationshipConfig) -> None:
        """初始化。

        Args:
            store: `ctm.store.Store` 实例。
            cfg: 关系配置。
        """
        self._store = store
        self._cfg = cfg

    def load(self, session_id: str) -> RelationshipState:
        """读取关系状态，不存在时返回默认值。

        Args:
            session_id: 会话键。

        Returns:
            RelationshipState（intimacy=0.5, tension=0）；读库失败同样返回默认值。

        TODO(v0.1): `pending_promises` 列是 JSON 文本，解析失败时置空并记 WARN，
            不要把坏数据当成异常抛给主流程。
        """
        raise NotImplementedError("v0.1: RelationshipManager.load")

    def save(self, state: RelationshipState) -> None:
        """写回关系状态。

        Args:
            state: 待保存状态。

        Returns:
            None。

        TODO(v0.1): 单条 UPSERT；写失败只记日志（下一轮还会重算）。
        """
        raise NotImplementedError("v0.1: RelationshipManager.save")

    def update_after_turn(self, signal: Any, decision: Any, response_text: str) -> RelationshipState:
        """一轮结束后更新关系（在 Archiver 里调用，不在请求前调用）。

        Args:
            signal: PerceptionSignal。
            decision: TurnDecision。
            response_text: 模型回复文本。

        Returns:
            更新后的状态。

        TODO(v0.1): 先 `decay()`，再按情绪与互动质量做 ±小步更新；
            回复文本以问句/共情词收尾时额外给正分（"回应积极"的代理指标）。
        """
        raise NotImplementedError("v0.1: RelationshipManager.update_after_turn")

    def decay(self, state: RelationshipState, now: float) -> RelationshipState:
        """按时间衰减。

        Args:
            state: 状态。
            now: 当前时间。

        Returns:
            衰减后的状态；`tension *= cfg.tension_decay_per_turn`，
            `intimacy` 按整天数扣 `cfg.intimacy_decay_per_day`。

        TODO(v0.1): 幂等——同一 now 重复调用不叠加衰减。
        """
        raise NotImplementedError("v0.1: RelationshipManager.decay")

    def level(self, value: float) -> str:
        """把 0..1 的数值映射成 low/medium/high。

        Args:
            value: 亲密度或张力。

        Returns:
            分级字符串。

        TODO(v0.1): <0.33 low，<0.66 medium，其余 high。
        """
        raise NotImplementedError("v0.1: RelationshipManager.level")

    def detect_promises(self, text: str) -> list[dict[str, Any]]:
        """规则检测承诺句。

        Args:
            text: 单条消息或拼接后的本轮文本。

        Returns:
            新承诺列表；`promise_detection_enabled=False` 时恒为空。

        TODO(v0.1): 触发词（答应/我会/明天/一定）+ 时间词（明天/后天/今晚/周末），
            两者同时出现才算承诺，避免"我会觉得"这类误命中。
        """
        raise NotImplementedError("v0.1: RelationshipManager.detect_promises")

    def pending_summary(self, state: RelationshipState, limit: int = 3) -> str:
        """把未完成事项压成一行，供注入模板使用。

        Args:
            state: 关系状态。
            limit: 最多展示几条。

        Returns:
            形如 "1) 明天提醒吃药；2) ..."；无事项返回 "无"。

        TODO(v0.1): 取未完成项、截断、编号拼接。
        """
        raise NotImplementedError("v0.1: RelationshipManager.pending_summary")
