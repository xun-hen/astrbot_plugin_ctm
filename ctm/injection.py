"""注入构建：唯一一次 Prompt 改写，以及 Token 预算装箱。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from ..config import Config, MemoryConfig
from .enums import MemoryType, PersonaMode, TopicAction
from .memory_store import RecallItem
from .relationship import RelationshipState

if TYPE_CHECKING:
    from .cognition import TurnDecision

TEMPLATE = """【话题状态】
当前话题：{current_topic}
上一话题：{previous_topic}
切换类型：{topic_action}
要求：若为 soft_boundary，请保留此前的情感上下文与未完成事项；
用户回溯旧话题时应自然衔接，不要声称"没聊过"。

【关系状态】
亲密度：{intimacy_level}（{intimacy:.2f}）
当前张力：{tension_level}（{tension:.2f}）
未完成事项：{pending_promises}

【记忆包】
[话题摘要]
{summaries}
[用户断言]
{assertions}
[相关情节]
{episodes}

【人格提示】
当前模式：{persona_mode}
行为约束：{persona_constraint}
"""

META_TEMPLATE = """
【注入元信息】
turn_id={turn_id}; budget={used}/{total}; dropped={dropped}
"""

PERSONA_CONSTRAINTS: dict[PersonaMode, str] = {
    PersonaMode.DAILY: "保持自然、简洁；不主动翻旧账，不强行抒情。",
    PersonaMode.COMFORT: "先接住情绪再给建议；不要讲大道理，不要否定对方的感受。",
    PersonaMode.PLAYFUL: "可以适度玩笑与调侃；但先确认对方此刻接得住玩笑。",
    PersonaMode.SERIOUS: "收敛玩笑；涉及承诺、约定、冲突时逐条确认，不臆测。",
}

PRIORITY_ORDER: tuple[str, ...] = (
    "relationship",
    "boundary",
    "summaries",
    "assertions",
    "episodes",
)

EMPTY_PLACEHOLDER = "（无）"


@dataclass(frozen=True)
class PackResult:
    """装箱结果。"""

    kept: list[RecallItem]
    dropped: list[dict[str, Any]]
    used_tokens: int
    fixed_tokens: int


class InjectionBuilder:
    """渲染与装箱。"""

    def __init__(self, cfg: Config) -> None:
        """初始化。

        Args:
            cfg: 全量配置。
        """
        self._cfg = cfg

    def plan(
        self,
        *,
        current_topic: str,
        previous_topic: str | None,
        topic_action: TopicAction,
        persona_mode: PersonaMode,
        relationship: RelationshipState | None,
        candidates: Sequence[RecallItem],
        budget: int | None = None,
    ) -> PackResult:
        """先算固定块、再按优先级装箱。"""
        total_budget = (
            self._cfg.memory.injection_budget_tokens if budget is None else budget
        )
        fixed = self._fixed_text(
            current_topic=current_topic,
            previous_topic=previous_topic,
            topic_action=topic_action,
            persona_mode=persona_mode,
            relationship=relationship,
        )
        fixed_tokens = self.estimate_tokens(fixed)
        if fixed_tokens > total_budget:
            fixed = self._fixed_text(
                current_topic=current_topic,
                previous_topic=previous_topic,
                topic_action=topic_action,
                persona_mode=persona_mode,
                relationship=None,
            )
            fixed_tokens = self.estimate_tokens(fixed)
        remaining = max(0, total_budget - fixed_tokens)
        packed = self.pack(candidates, remaining, self._cfg.memory)
        return PackResult(
            kept=packed.kept,
            dropped=packed.dropped,
            used_tokens=fixed_tokens + packed.used_tokens,
            fixed_tokens=fixed_tokens,
        )

    def build(self, decision: "TurnDecision") -> str:
        """把决策渲染成可追加的 Prompt 片段。"""
        summaries, assertions, episodes = render_recall_section(decision.recall_items)
        relationship = decision.relationship
        text = TEMPLATE.format(
            current_topic=decision.current_topic or "（新会话）",
            previous_topic=decision.previous_topic or "无",
            topic_action=decision.topic_action.value,
            intimacy_level=self._level(relationship.intimacy if relationship else 0.5),
            intimacy=relationship.intimacy if relationship else 0.5,
            tension_level=self._level(relationship.tension if relationship else 0.0),
            tension=relationship.tension if relationship else 0.0,
            pending_promises="无",
            summaries=summaries,
            assertions=assertions,
            episodes=episodes,
            persona_mode=decision.persona_mode.value,
            persona_constraint=PERSONA_CONSTRAINTS.get(decision.persona_mode, ""),
        )
        if relationship is None:
            start = text.find("【关系状态】")
            end = text.find("【记忆包】")
            if start != -1 and end != -1:
                text = text[:start] + text[end:]
        if self._cfg.injection.include_meta:
            text += META_TEMPLATE.format(
                turn_id=decision.turn_id,
                used=decision.estimated_tokens,
                total=decision.injection_budget,
                dropped=len(decision.dropped_items),
            )
        return text

    def pack(
        self,
        candidates: Sequence[RecallItem],
        budget: int,
        limits: MemoryConfig,
    ) -> PackResult:
        """纯预算装箱。"""
        type_limits = group_limits(limits)
        counts: dict[MemoryType, int] = {key: 0 for key in type_limits}
        kept: list[RecallItem] = []
        dropped: list[dict[str, Any]] = []
        used = 0
        for item in candidates:
            limit = type_limits.get(item.memory_type, 0)
            if counts[item.memory_type] >= limit:
                dropped.append(
                    {
                        "memory_id": item.memory_id,
                        "type": item.memory_type.value,
                        "reason": "limit",
                        "tokens": 0,
                    }
                )
                continue
            cost = self.estimate_tokens(item.content)
            if used + cost > budget:
                dropped.append(
                    {
                        "memory_id": item.memory_id,
                        "type": item.memory_type.value,
                        "reason": "budget",
                        "tokens": cost,
                    }
                )
                continue
            counts[item.memory_type] += 1
            used += cost
            kept.append(item)
        return PackResult(kept=kept, dropped=dropped, used_tokens=used, fixed_tokens=0)

    def estimate_tokens(self, text: str) -> int:
        """估算文本 Token 数（中英统一口径：每 4 字符 1 token，下限 1）。"""
        if not text:
            return 0
        return max(1, math.ceil(len(text) / 4))

    def cap_by_ratio(self, payload: str, base_prompt: str, ratio: float) -> str:
        """按"注入不超过原 Prompt 比例"按行截断。"""
        if ratio <= 0:
            return payload
        if not (base_prompt or "").strip():
            # 空 system_prompt 时没有"原量"可比，按固定下限放行，避免把首行也截掉。
            allowed = 240
        else:
            allowed = max(64, int(self.estimate_tokens(base_prompt) * ratio))
        if self.estimate_tokens(payload) <= allowed:
            return payload
        lines = payload.splitlines()
        kept: list[str] = []
        used = 0
        for line in lines:
            cost = self.estimate_tokens(line)
            if used + cost > allowed:
                break
            kept.append(line)
            used += cost
        return "\n".join(kept)

    def _fixed_text(
        self,
        *,
        current_topic: str,
        previous_topic: str | None,
        topic_action: TopicAction,
        persona_mode: PersonaMode,
        relationship: RelationshipState | None,
    ) -> str:
        return (
            f"【话题状态】{current_topic}\n"
            f"上一话题：{previous_topic or '无'}\n"
            f"切换类型：{topic_action.value}\n"
            f"【人格提示】{persona_mode.value}\n"
            f"约束：{PERSONA_CONSTRAINTS.get(persona_mode, '')}\n"
            f"【关系状态】亲密度 {relationship.intimacy if relationship else 0.5:.2f}；"
            f"张力 {relationship.tension if relationship else 0.0:.2f}\n"
        )

    @staticmethod
    def _level(value: float) -> str:
        if value < 0.33:
            return "low"
        if value < 0.66:
            return "medium"
        return "high"


def render_recall_section(items: Sequence[RecallItem]) -> tuple[str, str, str]:
    """把记忆条目按类型渲染成"摘要 / 断言 / 情节"三段。"""
    groups: dict[MemoryType, list[str]] = {
        MemoryType.SUMMARY: [],
        MemoryType.ASSERTION: [],
        MemoryType.EPISODE: [],
    }
    for item in items:
        content = " ".join(item.content.splitlines()).strip()
        if content:
            groups.setdefault(item.memory_type, []).append(f"- {content}")
    sections = []
    for memory_type in (
        MemoryType.SUMMARY,
        MemoryType.ASSERTION,
        MemoryType.EPISODE,
    ):
        lines = groups.get(memory_type, [])
        sections.append("\n".join(lines) if lines else EMPTY_PLACEHOLDER)
    return sections[0], sections[1], sections[2]


def group_limits(limits: MemoryConfig) -> dict[MemoryType, int]:
    """把配置里的三个上限映射成按类型查表。"""
    return {
        MemoryType.SUMMARY: max(0, limits.summary_limit),
        MemoryType.ASSERTION: max(0, limits.assertion_limit),
        MemoryType.EPISODE: max(0, limits.episode_limit),
    }
