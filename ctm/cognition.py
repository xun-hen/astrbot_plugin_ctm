"""联合决策：把感知、话题、记忆、关系合成一个 `TurnDecision`。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from ..config import Config
from .enums import EmotionHint, PersonaMode, TopicAction
from .injection import InjectionBuilder
from .memory_store import RecallItem
from .relationship import RelationshipState

TENSION_HIGH = 0.5
INTIMACY_HIGH = 0.6


@dataclass(frozen=True)
class TurnDecision:
    """一轮的完整决策（不可变）。"""

    turn_id: str
    session_id: str
    topic_action: TopicAction
    current_topic: str
    previous_topic: str | None
    persona_mode: PersonaMode
    recall_items: list[RecallItem]
    injection_budget: int
    estimated_tokens: int
    dropped_items: list[dict[str, Any]]
    reason: str
    relationship: RelationshipState | None = None
    soft_boundary_pending: bool = False

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的字典。"""
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "topic_action": self.topic_action.value,
            "current_topic": self.current_topic,
            "previous_topic": self.previous_topic,
            "persona_mode": self.persona_mode.value,
            "recall_items": [
                {
                    "memory_id": item.memory_id,
                    "type": item.memory_type.value,
                    "score": round(item.score, 4),
                }
                for item in self.recall_items
            ],
            "injection_budget": self.injection_budget,
            "estimated_tokens": self.estimated_tokens,
            "dropped_items": self.dropped_items,
            "reason": self.reason,
            "soft_boundary_pending": self.soft_boundary_pending,
        }


class Cognition:
    """决策器：除生成 turn_id 外无副作用。"""

    def __init__(self, cfg: Config, packer: InjectionBuilder, topic_sm: Any) -> None:
        """初始化。

        Args:
            cfg: 全量配置。
            packer: 预算装箱器。
            topic_sm: 话题状态机（只读判定，不改状态）。
        """
        self._cfg = cfg
        self._packer = packer
        self._topic_sm = topic_sm

    def decide(
        self,
        signal: Any,
        state: Any,
        candidates: list[RecallItem],
        relationship: RelationshipState,
        *,
        force_hard: bool = False,
    ) -> TurnDecision:
        """产出一轮决策。"""
        action = self._topic_action(signal, state, force_hard)
        persona = self._pick_persona(signal, state, relationship)
        ordered = sorted(candidates, key=lambda item: item.score, reverse=True)
        try:
            plan = self._packer.plan(
                current_topic=str(getattr(state, "current_topic", "") or ""),
                previous_topic=getattr(state, "previous_topic", None),
                topic_action=action,
                persona_mode=persona,
                relationship=relationship,
                candidates=ordered,
            )
        except Exception:  # noqa: BLE001
            plan = None
        budget = self._cfg.memory.injection_budget_tokens
        kept = list(plan.kept) if plan else []
        dropped = list(plan.dropped) if plan else []
        estimated = plan.used_tokens if plan else 0
        reason = (
            f"action={action.value}; persona={persona.value}; "
            f"kept={len(kept)}; dropped={len(dropped)}; "
            f"tokens={estimated}/{budget}; "
            f"emotion={summarize_emotion(signal.emotion_hint, signal.emotion_score)}"
        )
        return TurnDecision(
            turn_id=self._new_turn_id(),
            session_id=str(getattr(signal, "session_id", "")),
            topic_action=action,
            current_topic=str(getattr(state, "current_topic", "") or ""),
            previous_topic=getattr(state, "previous_topic", None),
            persona_mode=persona,
            recall_items=kept,
            injection_budget=budget,
            estimated_tokens=estimated,
            dropped_items=dropped,
            reason=reason,
            relationship=relationship,
            soft_boundary_pending=bool(
                getattr(state, "soft_boundary_pending", False)
                or action == TopicAction.SOFT_BOUNDARY
            ),
        )

    def _topic_action(
        self, signal: Any, state: Any, force_hard: bool
    ) -> TopicAction:
        """决定本轮话题动作（只决定，不修改状态）。"""
        if force_hard:
            return TopicAction.HARD_SWITCH
        try:
            if self._topic_sm.should_soft_boundary(signal, state):
                if self._cfg.topic.auto_switch_strategy == "hard":
                    return TopicAction.HARD_SWITCH
                return TopicAction.SOFT_BOUNDARY
        except Exception:  # noqa: BLE001
            return TopicAction.CONTINUE
        return TopicAction.CONTINUE

    def _pick_persona(
        self, signal: Any, state: Any, relationship: RelationshipState
    ) -> PersonaMode:
        """按优先级选择人格模式。"""
        if getattr(signal, "sticky_hit", False) or relationship.tension >= TENSION_HIGH:
            return PersonaMode.SERIOUS
        if (
            getattr(signal, "emotion_hint", EmotionHint.UNKNOWN)
            == EmotionHint.NEGATIVE
            and relationship.tension < TENSION_HIGH
        ):
            return PersonaMode.COMFORT
        if (
            getattr(signal, "emotion_hint", EmotionHint.UNKNOWN) == EmotionHint.POSITIVE
            and relationship.intimacy >= INTIMACY_HIGH
        ):
            return PersonaMode.PLAYFUL
        return PersonaMode.DAILY

    def _new_turn_id(self) -> str:
        """生成轮次 ID。"""
        return uuid.uuid4().hex


def summarize_emotion(hint: EmotionHint, score: float) -> str:
    """把情绪标签压成日志短串。"""
    return f"{hint.value}({score:+.2f})"
