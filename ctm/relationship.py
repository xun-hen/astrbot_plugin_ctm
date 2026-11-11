"""关系状态：亲密度 / 张力 / 未完成事项（慢变量，小步更新）。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..config import RelationshipConfig

LEVEL_LOW = "low"
LEVEL_MEDIUM = "medium"
LEVEL_HIGH = "high"

_PROMISE_TRIGGERS = ("答应", "我会", "一定", "保证", "明天", "后天", "今晚", "周末")
_TIME_WORDS = ("明天", "后天", "今晚", "周末", "下次", "以后")


@dataclass
class RelationshipState:
    """一个会话的关系快照。"""

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
        """读取关系状态，不存在时返回默认值。"""
        try:
            row = self._store.load_relationship(session_id)
        except Exception:  # noqa: BLE001
            row = None
        if not row:
            return RelationshipState(session_id=session_id, updated_at=time.time())
        promises = row.get("pending_promises")
        return RelationshipState(
            session_id=session_id,
            intimacy=float(row.get("intimacy", 0.5)),
            tension=float(row.get("tension", 0.0)),
            pending_promises=promises if isinstance(promises, list) else [],
            updated_at=float(row.get("updated_at") or 0.0),
        )

    def save(self, state: RelationshipState) -> None:
        """写回关系状态；失败只吞掉（下一轮会重算）。"""
        try:
            self._store.save_relationship(
                state.session_id,
                intimacy=state.intimacy,
                tension=state.tension,
                pending_promises=state.pending_promises,
            )
        except Exception:  # noqa: BLE001
            pass

    def update_after_turn(
        self, signal: Any, decision: Any, response_text: str
    ) -> RelationshipState:
        """一轮结束后更新关系（在归档阶段调用）。"""
        state = self.load(str(getattr(signal, "session_id", "")))
        now = time.time()
        state = self.decay(state, now)
        emotion = float(getattr(signal, "emotion_score", 0.0) or 0.0)
        if emotion > 0:
            state.intimacy = min(1.0, state.intimacy + 0.005 * emotion)
        elif emotion < 0:
            state.tension = min(1.0, state.tension + 0.02 * abs(emotion))
        if "？" in (response_text or "") or "?" in (response_text or ""):
            state.intimacy = min(1.0, state.intimacy + 0.002)
        if self._cfg.promise_detection_enabled:
            for promise in self.detect_promises(str(getattr(signal, "text", ""))):
                if promise["text"] not in {item.get("text") for item in state.pending_promises}:
                    state.pending_promises.append(promise)
        state.updated_at = now
        self.save(state)
        return state

    def decay(self, state: RelationshipState, now: float) -> RelationshipState:
        """按时间衰减（同一 now 重复调用不叠加）。"""
        last = state.updated_at or now
        age_days = max(0.0, (now - last) / 86400.0)
        if age_days > 0:
            state.intimacy = max(
                0.0, state.intimacy - self._cfg.intimacy_decay_per_day * age_days
            )
        state.tension = max(0.0, state.tension * self._cfg.tension_decay_per_turn)
        state.updated_at = now
        return state

    def level(self, value: float) -> str:
        """把 0..1 的数值映射成 low/medium/high。"""
        if value < 0.33:
            return LEVEL_LOW
        if value < 0.66:
            return LEVEL_MEDIUM
        return LEVEL_HIGH

    def detect_promises(self, text: str) -> list[dict[str, Any]]:
        """规则检测承诺句（触发词 + 时间词同时出现）。"""
        if not self._cfg.promise_detection_enabled:
            return []
        text = text or ""
        if not any(word in text for word in _PROMISE_TRIGGERS):
            return []
        if not any(word in text for word in _TIME_WORDS):
            return []
        return [
            {
                "text": text[:80],
                "created_at": time.time(),
                "done": False,
            }
        ]

    def pending_summary(self, state: RelationshipState, limit: int = 3) -> str:
        """把未完成事项压成一行。"""
        items = [
            str(item.get("text", "")).strip()
            for item in state.pending_promises
            if not item.get("done")
        ]
        items = [item for item in items if item][:limit]
        if not items:
            return "无"
        return "；".join(f"{index + 1}) {item}" for index, item in enumerate(items))
