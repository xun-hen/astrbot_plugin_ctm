"""注入构建：唯一一次 Prompt 改写，以及 Token 预算装箱。

设计文档 §3.5 的两条硬规则：

1. **单点注入**：整个系统只有这里往 `ProviderRequest.system_prompt` 追加内容。
   其他记忆插件若仍自动注入，记 `WARN_DUPLICATE_INJECTION`，不要试图"再叠一层
   去纠正它"——两层注入互相看不到对方，永远不可能收敛。
2. **预算优先于完整**：固定块（话题 + 关系 + 人格）先占预算，剩余按
   relationship → boundary → summaries → assertions → episodes 的顺序贪心加入；
   放不下的进 `dropped_items`，附原因，不静默丢。

固定块如果自己就超预算：截断低优先级固定块（先砍关系块里的未完成事项、
再砍边界提示），记 `WARN_BUDGET_OVERFLOW`，**不要**截断当前话题名——
那条信息是模型接话的地基。

模板里的"注入元信息"默认关闭（`injection.include_meta=false`），只写日志；
turn_id 在日志与 turn_logs 里都有，不必进 Prompt 占 Token。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from ..config import Config, MemoryConfig
from .enums import MemoryType, PersonaMode, TopicAction
from .memory_store import RecallItem
from .relationship import RelationshipState

if TYPE_CHECKING:  # 避免 cognition ↔ injection 的运行时循环
    from .cognition import TurnDecision

#: 固定块与记忆块的注入模板。占位符与 `build()` 的渲染字段一一对应。
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

#: 人格模式的行为约束（进 Prompt 的短句；不要写成长篇人设）。
PERSONA_CONSTRAINTS: dict[PersonaMode, str] = {
    PersonaMode.DAILY: "保持自然、简洁；不主动翻旧账，不强行抒情。",
    PersonaMode.COMFORT: "先接住情绪再给建议；不要讲大道理，不要否定对方的感受。",
    PersonaMode.PLAYFUL: "可以适度玩笑与调侃；但先确认对方此刻接得住玩笑。",
    PersonaMode.SERIOUS: "收敛玩笑；涉及承诺、约定、冲突时逐条确认，不臆测。",
}

#: 装箱优先级（从高到低）。固定块永远先于这里的所有条目。
PRIORITY_ORDER: tuple[str, ...] = (
    "relationship",
    "boundary",
    "summaries",
    "assertions",
    "episodes",
)

#: 无候选时写入记忆包占位，保持模板结构稳定（也便于回归对比）。
EMPTY_PLACEHOLDER = "（无）"


@dataclass(frozen=True)
class PackResult:
    """装箱结果。

    Attributes:
        kept: 进入 Prompt 的记忆条目（按渲染顺序）。
        dropped: 被丢弃的条目及原因，形如
            `{"memory_id": 3, "type": "assertion", "reason": "budget", "tokens": 42}`。
        used_tokens: 记忆块 + 固定块的总估算 Token。
        fixed_tokens: 固定块估算 Token（预算不足时用于判断该砍谁）。
    """

    kept: list[RecallItem]
    dropped: list[dict[str, Any]]
    used_tokens: int
    fixed_tokens: int


class InjectionBuilder:
    """渲染与装箱。"""

    def __init__(self, cfg: Config) -> None:
        """初始化。

        Args:
            cfg: 全量配置（读取 memory 预算与 injection 开关）。
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
        """先算固定块、再按优先级装箱（Cognition 在决策阶段调用）。

        Args:
            current_topic: 当前话题标题。
            previous_topic: 上一话题标题。
            topic_action: 本轮话题动作。
            persona_mode: 人格模式。
            relationship: 关系快照；None 表示关系模块关闭。
            candidates: 已排序候选。
            budget: 预算覆盖值；None 用配置。

        Returns:
            PackResult。

        TODO(v0.1):
            1. 渲染关系块 / 边界提示 / 人格块，`estimate_tokens` 求和得 fixed；
            2. 若 fixed > budget：按"先砍未完成事项、再砍边界提示"截断并记
               WARN_BUDGET_OVERFLOW；
            3. 剩余预算按 PRIORITY_ORDER 依次尝试加入候选；
            4. 超预算的候选进 dropped（reason 区分 "limit" 与 "budget"）。
        """
        raise NotImplementedError("v0.1: InjectionBuilder.plan")

    def build(self, decision: "TurnDecision") -> str:
        """把决策渲染成可追加的 Prompt 片段。

        Args:
            decision: 决策（含已装箱的 recall_items 与关系快照）。

        Returns:
            完整注入片段；关系关闭时删去【关系状态】块；
            `include_meta` 为假时不渲染【注入元信息】。

        Raises:
            None: 渲染失败时返回最小关系块（文档 §5 的降级路径），
            并记 `ERROR_INJECTION`。

        TODO(v0.1): 逐段渲染；记忆为空时用 EMPTY_PLACEHOLDER；
            模板占位与字段必须一一对应（用一条单测锁死）。
        """
        raise NotImplementedError("v0.1: InjectionBuilder.build")

    def pack(
        self,
        candidates: Sequence[RecallItem],
        budget: int,
        limits: MemoryConfig,
    ) -> PackResult:
        """纯预算装箱（不含固定块；`plan()` 内部会调用它）。

        Args:
            candidates: 已排序候选。
            budget: 可用于记忆块的 Token。
            limits: 各类条数上限（summary / assertion / episode）。

        Returns:
            PackResult（fixed_tokens 恒为 0）。

        TODO(v0.1): 贪心：逐条估算正文 Token，超限跳过并记录；
            先按类型上限截断，再按预算截断。
        """
        raise NotImplementedError("v0.1: InjectionBuilder.pack")

    def estimate_tokens(self, text: str) -> int:
        """估算文本 Token 数。

        Args:
            text: 任意文本。

        Returns:
            `ceil(len(text) / 4)`，下限 1（空串返回 0）。

        TODO(v0.1): 中文按 1 字 ≈ 1 token 更接近真实值，可配置 tokenizer；
            在接入真实 tokenizer 前，统一口径比"精确"更重要。
        """
        raise NotImplementedError("v0.1: InjectionBuilder.estimate_tokens")

    def cap_by_ratio(self, payload: str, base_prompt: str, ratio: float) -> str:
        """按"注入不超过原 Prompt 比例"截断。

        Args:
            payload: 待注入文本。
            base_prompt: 原 system_prompt。
            ratio: 比例上限；<=0 表示不限制。

        Returns:
            截断后的文本（按行截，避免把一句话砍成两半）；未截断时原样返回。

        TODO(v0.1): 估算原 Prompt Token，允许量 = ratio × 原量；
            截断时保留模板骨架、从低优先级行开始删，记 WARN_PROMPT_GROWTH_CAPPED。
        """
        raise NotImplementedError("v0.1: InjectionBuilder.cap_by_ratio")


def render_recall_section(items: Sequence[RecallItem]) -> tuple[str, str, str]:
    """把记忆条目按类型渲染成"摘要 / 断言 / 情节"三段。

    Args:
        items: 已装箱条目。

    Returns:
        (summaries, assertions, episodes) 三段文本；空段用 EMPTY_PLACEHOLDER。

    TODO(v0.1): 逐条 "- " 前缀；content 里的换行折叠成空格。
    """
    raise NotImplementedError("v0.1: render_recall_section")


def group_limits(limits: MemoryConfig) -> dict[MemoryType, int]:
    """把配置里的三个上限映射成按类型查表。

    Args:
        limits: 记忆配置。

    Returns:
        {SUMMARY: n, ASSERTION: n, EPISODE: n}。

    TODO(v0.1): 直接构造字典。
    """
    raise NotImplementedError("v0.1: group_limits")
