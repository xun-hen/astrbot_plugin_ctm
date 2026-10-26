"""话题状态机：软边界（压栈）、回溯（弹栈）、硬切（显式命令）。

设计文档 §3.2 的核心判断：**话题变化不等于上下文清空**。
默认策略下，"换话题"只是把当前话题压栈并给模型一条"边界提示"，
历史消息原样保留；因此用户回溯旧话题时，模型不会声称"没聊过"。

三条硬约束（对应 §2 与 §11）：

* 粘性命中时**禁止**自动切话题（宁可粘在旧话题，也不切断用户正在说的事）；
* 指令不参与判定；
* **硬切永远不在这里的调用链里自动发生**——只有显式 `/ctm switch hard`
  或配置 `auto_switch_strategy=hard` 且非陪伴场景才执行，且必须在
  `on_llm_request` 之外执行（否则本轮已召回的上下文会先被清掉）。

关于持久化的一个已知缺口：会话级状态里 `turns_since_switch` / `last_switch_at` /
`soft_boundary_pending` 需要逐轮持久化，而设计文档 §4 的 DDL 没有对应表。
本骨架在 `ctm/sql/001_init.sql` 里补了 `session_state`（见文档 D1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..config import TopicConfig
from .enums import TopicStatus
from .textsim import SimilarityScorer


@dataclass
class TopicNode:
    """话题栈里的一个节点。

    Attributes:
        topic_id: 数据库主键。
        title: 话题标题（短，用于注入与相似度比较）。
        summary: 归档摘要；活跃话题为空。
        status: active / archived / popped。
        created_at: 创建时间。
        last_active_at: 最近一次活跃时间。
        topic_key: 归一化键，用于"同一话题再次出现"去重（可空）。
    """

    topic_id: int
    title: str
    summary: str = ""
    status: TopicStatus = TopicStatus.ACTIVE
    created_at: float = 0.0
    last_active_at: float = 0.0
    topic_key: str | None = None


@dataclass
class TopicState:
    """一个会话的话题栈快照。

    不变量：`stack[-1]` 就是当前焦点；`current_topic` 恒等于栈顶标题，
    两者不一致时以栈顶为准（`focus()` 是唯一读取入口）。

    Attributes:
        session_id: 会话键。
        stack: 话题栈，栈顶为当前焦点。
        current_topic: 当前话题标题；无话题时为空串。
        previous_topic: 上一个话题标题；无则为 None。
        turns_since_switch: 距最近一次切换的轮数。
        last_switch_at: 最近一次切换时间（Unix 秒）。
        soft_boundary_pending: 软边界提示是否尚未注入。
    """

    session_id: str
    stack: list[TopicNode] = field(default_factory=list)
    current_topic: str = ""
    previous_topic: str | None = None
    turns_since_switch: int = 0
    last_switch_at: float = 0.0
    soft_boundary_pending: bool = False

    def focus(self) -> TopicNode | None:
        """返回当前焦点话题节点。

        Returns:
            栈顶节点；空栈返回 None。

        TODO(v0.1): return self.stack[-1] if self.stack else None
        """
        raise NotImplementedError("v0.1: TopicState.focus")

    def titles(self, limit: int | None = None) -> list[str]:
        """返回自栈顶向下的标题列表（用于注入候选）。

        Args:
            limit: 最多返回多少个；None 表示全部。

        Returns:
            标题列表，栈顶在前。

        TODO(v0.1): 反向切片后取 title。
        """
        raise NotImplementedError("v0.1: TopicState.titles")


class TopicStateMachine:
    """话题栈的唯一写入者。"""

    def __init__(
        self,
        store: Any,
        cfg: TopicConfig,
        scorer: SimilarityScorer | None = None,
    ) -> None:
        """初始化。

        Args:
            store: `ctm.store.Store` 实例（按 duck typing 使用，便于单测替身）。
            cfg: 话题配置。
            scorer: 相似度打分器；None 时用词法兜底。
        """
        self._store = store
        self._cfg = cfg
        self._scorer = scorer

    def load(self, session_id: str) -> TopicState:
        """从库中恢复会话话题栈。

        Args:
            session_id: 会话键。

        Returns:
            TopicState；新会话返回空栈而不是抛异常。

        Raises:
            None: 读失败时返回空状态并记 `ERROR_STORE`（判定退化为"延续"）。

        TODO(v0.1): 读 topic_stack 联表 topics + session_state，按 position 升序重建。
        """
        raise NotImplementedError("v0.1: TopicStateMachine.load")

    def save(self, state: TopicState) -> None:
        """把状态写回库。

        Args:
            state: 待保存状态。

        Returns:
            None。

        TODO(v0.1): 栈写入 topic_stack（先删后插，事务），计数器写 session_state。
            该方法在设计文档 §3.2 的方法表里没有列出，但实现必须有；不要把它
            藏进 load/soft_boundary 内部——那样"什么时候落盘"会变得不可见。
        """
        raise NotImplementedError("v0.1: TopicStateMachine.save")

    def should_soft_boundary(self, signal: Any, state: TopicState) -> bool:
        """判定本轮是否触发软边界（设计文档 §3.2 的算法）。

        Args:
            signal: PerceptionSignal。
            state: 当前话题状态。

        Returns:
            是否软切。

        判定顺序（任一命中即 False）：
            1. `auto_switch_enabled` 为假；
            2. 指令消息；
            3. 粘性命中；
            4. `turns_since_switch < min_turns_before_switch`；
            5. 距上次切换不足 `switch_cooldown_seconds`；
            6. `topic_similarity(current_topic, text) >= topic_threshold`。

        通过以上检查后，**双信号**才切：命中新意图标记，或
        相似度低于 `topic_threshold * 0.7`（措辞明显不同）。
            只有相似度低而没有任何新意图线索时，判为"还在说同一件事"。

        TODO(v0.1): 按上述顺序实现；第 6 步把 current_topic 与上一轮摘要拼接后比较。
        """
        raise NotImplementedError("v0.1: TopicStateMachine.should_soft_boundary")

    def soft_boundary(self, signal: Any, state: TopicState, new_topic_title: str) -> TopicState:
        """压栈并建立新话题（不清空上下文）。

        Args:
            signal: 触发信号。
            state: 旧状态。
            new_topic_title: 新话题标题（由 `title_for()` 或归档摘要生成）。

        Returns:
            新状态：旧话题置 archived 并压栈，新话题置 active 成为栈顶，
            `previous_topic` 指向旧标题，计数与时间戳复位，
            `soft_boundary_pending=True`。

        TODO(v0.1): 事务内完成；栈保留 position 连续；旧话题的 summary 由
            Archiver 异步补写（这里只改 status 与时间，不 await 摘要）。
        """
        raise NotImplementedError("v0.1: TopicStateMachine.soft_boundary")

    def should_pop_back(self, signal: Any, state: TopicState) -> bool:
        """判定用户是否在回溯旧话题（弹栈的触发器）。

        Args:
            signal: 感知信号。
            state: 当前状态。

        Returns:
            应该弹栈返回 True。

        这是设计文档没写死的一处（见 DESIGN.md GAP-2）。v0.1 的判定意向：
            1. 当前消息与栈顶话题相似度低；
            2. 但与栈中更靠下的某个话题相似度高（>= topic_threshold）；
            3. 且不与"新话题"信号冲突（未命中新意图标记）。
        没有第 2 条就会变成"随便什么旧话题都弹栈"。

        TODO(v0.1): 逐层比较栈中话题标题/摘要，取最高分；栈深 < 2 时恒为 False。
        """
        raise NotImplementedError("v0.1: TopicStateMachine.should_pop_back")

    def pop_to_previous(self, signal: Any, state: TopicState) -> TopicState:
        """用户回溯旧话题：弹栈并把被弹话题恢复为活跃。

        Args:
            signal: 触发信号。
            state: 当前状态。

        Returns:
            新状态；空栈时原样返回（不报错，回溯失败最多算延续）。

        TODO(v0.1): 栈顶（较新的那个）置 popped 并移出栈，下一节点置 active；
            `previous_topic` 指向刚弹掉的标题；同样置 `soft_boundary_pending=True`，
            让模型知道"这是在接旧话题"，而不是"没聊过"。
        """
        raise NotImplementedError("v0.1: TopicStateMachine.pop_to_previous")

    def hard_switch(self, signal: Any, state: TopicState) -> TopicState:
        """执行硬切换（等价 `/new`）。

        Args:
            signal: 触发信号。
            state: 当前状态。

        Returns:
            清空栈后的状态（新话题，摘要为空）。

        Raises:
            RuntimeError: 在不应硬切的场景被调用时拒绝执行。

        约束：**只在消息入口或显式命令里调用**，绝不在 `on_llm_request` 里调用；
        调用前应确认 `strategy=hard` 或 `force=True`（显式命令）。
        旧话题留在 topics 表里（status=archived），不删除。
        """
        raise NotImplementedError("v0.1: TopicStateMachine.hard_switch")

    def title_for(self, text: str) -> str:
        """为新话题取标题。

        Args:
            text: 触发消息。

        Returns:
            标题；v0.1 用"截断 + 去标点"的规则标题，v0.3 起可接轻量模型。

        TODO(v0.1): 取前 12~16 字，空白折叠，过长加省略号。
        """
        raise NotImplementedError("v0.1: TopicStateMachine.title_for")

    def is_sticky(self, signal: Any) -> bool:
        """判断粘性命中（供 Cognition 与 Archiver 复用）。

        Args:
            signal: PerceptionSignal。

        Returns:
            命中返回 True。

        TODO(v0.1): 直接返回 signal.sticky_hit（规则在 Perception 里做）。
        """
        raise NotImplementedError("v0.1: TopicStateMachine.is_sticky")


def detect_new_intent(text: str, markers: Iterable[str]) -> bool:
    """规则判断是否出现"新意图"线索。

    Args:
        text: 消息文本。
        markers: 新意图标记词，如"另外/对了/换个话题"。

    Returns:
        出现任一标记返回 True。

    TODO(v0.1): strip 后逐词 `in`；标记为空时返回 False。
    """
    raise NotImplementedError("v0.1: detect_new_intent")


def restore_clock(now: float | None = None) -> float:
    """统一的时间来源，便于测试注入。

    Args:
        now: 显式时间；None 时取当前时间。

    Returns:
        Unix 秒。

    TODO(v0.1): return time.time() if now is None else float(now)
    """
    raise NotImplementedError("v0.1: restore_clock")
