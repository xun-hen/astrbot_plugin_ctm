"""联合决策：把感知、话题、记忆、关系合成一个 `TurnDecision`。

设计文档 §3.4 的核心是**顺序**，不是算法：

1. 话题动作（continue / soft_boundary / hard_switch）；
2. 人格模式（由情绪、张力、粘性命中决定）；
3. 记忆候选排序；
4. 预算装箱（调用 InjectionBuilder）；
5. 生成可观测的 `reason`。

为什么强调顺序：人格模式会影响"该不该注入安慰性情节"，而预算又会反过来
砍掉候选。如果先装箱再定人格，就会出现"预算被日常寒暄占满、
真正需要安慰的轮次反而没有安慰记忆"的倒挂。

硬切（hard_switch）只接受显式请求（`force_hard=True`），本模块不自行决定
清空上下文；文档 §2 的约束是"硬切不在 on_llm_request 内执行"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Config
from .enums import EmotionHint, PersonaMode, TopicAction
from .injection import InjectionBuilder
from .memory_store import RecallItem
from .relationship import RelationshipState

#: 判定人格模式的两个门限。数值是 v0.1 的起点，回归后再调。
TENSION_HIGH = 0.5
INTIMACY_HIGH = 0.6


@dataclass(frozen=True)
class TurnDecision:
    """一轮的完整决策（不可变；注入与归档都只读它）。

    Attributes:
        turn_id: 轮次 ID（uuid4 hex），贯穿日志、turn_logs、archive_failures。
        session_id: 会话键。
        topic_action: 本轮话题动作。
        current_topic: 当前话题标题。
        previous_topic: 上一话题标题（无则 None）。
        persona_mode: 人格模式。
        recall_items: 已装箱、将注入的记忆（按优先级排序）。
        injection_budget: Token 预算上限。
        estimated_tokens: 估算注入量（固定块 + 记忆块）。
        dropped_items: 被预算丢弃的候选项（含原因，写 turn_logs）。
        reason: 人类可读的决策理由（写日志；`include_meta` 时进 Prompt）。
        relationship: 决策时刻的关系快照；关系在归档阶段才更新，
            因此快照与注入时一致（注入不再回查数据库）。
        soft_boundary_pending: 本轮是否需要注入话题边界提示
            （由 `injection.soft_boundary_once` 决定是否在注入后清除）。
    """

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
        """导出为可 JSON 序列化的字典（写 turn_logs / 回放）。

        Returns:
            决策字典；`recall_items` 只保留 id 与分数，正文不重复存。

        TODO(v0.1): 枚举用 `.value`；时间戳不进决策（由调用方补）。
        """
        raise NotImplementedError("v0.1: TurnDecision.to_dict")


class Cognition:
    """决策器：纯函数式（除生成 turn_id 外无副作用）。"""

    def __init__(self, cfg: Config, packer: InjectionBuilder, topic_sm: Any) -> None:
        """初始化。

        Args:
            cfg: 全量配置。
            packer: 预算装箱器；注入顺序与预算口径只在它内部定义。
            topic_sm: 话题状态机。决策需要它的"要不要切"判定，但**不复制**
                该逻辑，也不在这里改状态——状态变更由消息入口在决策后统一落地。
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
        """产出一轮决策。

        Args:
            signal: PerceptionSignal。
            state: TopicState（决策前的状态）。
            candidates: MemoryStore 召回并打分的候选（未排序也可）。
            relationship: 关系快照。
            force_hard: 显式硬切（来自 `/ctm switch hard`）。

        Returns:
            TurnDecision。

        Raises:
            None: 装箱异常时降级为"无记忆 + 最小人格"并记 `ERROR_INJECTION`。

        TODO(v0.1):
            1. `_topic_action()`：force_hard → hard_switch；
               `should_soft_boundary` → soft_boundary；否则 continue；
            2. `_pick_persona()`；
            3. 候选按 score 降序（稳定排序，同分保留召回顺序）；
            4. `packer.plan()` 得到 kept/dropped/estimated；
            5. `_reason()`：拼相似度、命中规则、人格、预算；
            6. 组装 TurnDecision（state 的变更由调用方在 message handler 里做）。
        """
        raise NotImplementedError("v0.1: Cognition.decide")

    def _topic_action(self, signal: Any, state: Any, force_hard: bool) -> TopicAction:
        """决定本轮话题动作。

        Args:
            signal: 感知信号。
            state: 话题状态。
            force_hard: 显式硬切。

        Returns:
            TopicAction。

        TODO(v0.1): 依赖 TopicStateMachine.should_soft_boundary 的结果；
            注意这里**只决定动作**，不修改状态。
        """
        raise NotImplementedError("v0.1: Cognition._topic_action")

    def _pick_persona(self, signal: Any, state: Any, relationship: RelationshipState) -> PersonaMode:
        """按文档 §3.4 的映射表选人格模式。

        Args:
            signal: 感知信号。
            state: 话题状态。
            relationship: 关系快照。

        Returns:
            PersonaMode。

        优先级（高到低）：serious → comfort → playful → daily：
            * 粘性命中或 tension >= TENSION_HIGH：serious；
            * 负面情绪且 tension 低：comfort；
            * 正面情绪且 intimacy >= INTIMACY_HIGH：playful；
            * 其他：daily。

        TODO(v0.1): 按上述优先级实现，并把判定依据写进 decision.reason。
        """
        raise NotImplementedError("v0.1: Cognition._pick_persona")

    def _new_turn_id(self) -> str:
        """生成轮次 ID。

        Returns:
            uuid4 hex。

        TODO(v0.1): return uuid.uuid4().hex
        """
        raise NotImplementedError("v0.1: Cognition._new_turn_id")


def summarize_emotion(hint: EmotionHint, score: float) -> str:
    """把情绪标签压成日志短串。

    Args:
        hint: 情绪标签。
        score: 强度。

    Returns:
        形如 "negative(-0.62)"。

    TODO(v0.1): f"{hint.value}({score:+.2f})"
    """
    raise NotImplementedError("v0.1: summarize_emotion")
