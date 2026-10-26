"""记忆仓库：候选召回、打分、写入与反馈。

设计文档 §3.3 的评分：

```
score = w_rel * relevance
      + w_sal * emotional_salience
      + w_rec * recency_decay
      + w_use * usage_policy
```

`recency_decay = 0.5 ** (age_days / half_life_days)`。

两个容易写错的点，先在骨架里写死口径：

1. **召回不是检索**：v0.1 没有向量库，候选就是"本会话最近 N 个话题摘要 +
   关键词/情绪匹配的断言 + 高显著性情节"，条数有上限（默认 2/3/2），
   不要每轮全表扫；
2. **usage_policy 是反馈而不是纯加分**：被用户显式纠正的记忆要降权并
   记 `corrected_count`（测试 T11），否则错误事实会被反复注入、越用越"真"。

本模块不直接写 SQL：所有持久化经 `ctm.store.Store`，行对象用 dict 传递，
避免"实体类 ↔ 持久层"循环依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import MemoryConfig
from .enums import MemoryType


@dataclass(frozen=True)
class RecallItem:
    """一条待注入的记忆。

    Attributes:
        memory_id: `memories.id`。
        memory_type: summary / assertion / episode。
        content: 注入到 Prompt 的正文。
        score: 最终分（已加权）。
        relevance: 相关性分量，0..1。
        salience: 情感显著性分量，0..1。
        recency: 新近度分量，0..1。
        usage: 使用反馈分量，0..1。
        reason: 为什么被召回（可观测；如 "topic_recent" / "sticky_keyword"）。
    """

    memory_id: int
    memory_type: MemoryType
    content: str
    score: float
    relevance: float
    salience: float
    recency: float
    usage: float
    reason: str


@dataclass(frozen=True)
class CandidateQuery:
    """召回查询参数（由 Cognition 组装，MemoryStore 执行）。

    Attributes:
        session_id: 会话键（隔离边界）。
        topic_text: 当前话题文本，用于相关性打分。
        keywords: 本轮消息关键词，用于断言/情节预筛。
        emotion_score: 本轮情绪强度，负面高时放宽情节候选。
        limit_per_type: 每类最多取多少条候选（先粗筛再打分）。
    """

    session_id: str
    topic_text: str = ""
    keywords: tuple[str, ...] = ()
    emotion_score: float = 0.0
    limit_per_type: int = 20


class MemoryStore:
    """记忆的唯一读写门面。"""

    def __init__(self, store: Any, cfg: MemoryConfig) -> None:
        """初始化。

        Args:
            store: `ctm.store.Store` 实例。
            cfg: 记忆与预算配置。
        """
        self._store = store
        self._cfg = cfg

    def recall_candidates(self, signal: Any, state: Any) -> list[RecallItem]:
        """召回本轮候选并按分数降序返回。

        Args:
            signal: PerceptionSignal。
            state: TopicState。

        Returns:
            已排序的候选；条数不超过各类上限之和。

        Raises:
            None: 查询失败时返回空列表并记 `ERROR_STORE`，本轮退化为无记忆注入。

        TODO(v0.1):
            1. `CandidateQuery`：session_id 来自 signal，topic_text 取 state.current_topic，
               keywords 用 textsim.tokenize 结果；
            2. 摘要：最近 `preserve_recent_topics` 个话题；
            3. 断言：关键词命中或情绪匹配，按 confidence 过滤；
            4. 情节：salience >= episode_salience_threshold，负面情绪时放宽；
            5. 逐条 `_score()`，排序后裁剪到 summary/assertion/episode 上限。
        """
        raise NotImplementedError("v0.1: MemoryStore.recall_candidates")

    def mark_recalled(self, memory_ids: list[int]) -> None:
        """记录"这些记忆被注入了"（usage 反馈与冷却的依据）。

        Args:
            memory_ids: 记忆主键列表。

        Returns:
            None。

        TODO(v0.1): 更新 last_recalled_at / recall_count；空列表直接返回。
        """
        raise NotImplementedError("v0.1: MemoryStore.mark_recalled")

    def update_importance(self, memory_id: int, delta: float) -> None:
        """按固定增量调整重要度。

        Args:
            memory_id: 记忆主键。
            delta: 增量，正负均可；结果裁剪到 [0, 1]。

        Returns:
            None。

        TODO(v0.1): 读-改-写在一个事务内；`delta` 来自用户 pin 或纠正反馈。
        """
        raise NotImplementedError("v0.1: MemoryStore.update_importance")

    def save_summary(self, session_id: str, topic_id: int | None, summary: str, source_turn_id: str | None) -> int:
        """写入话题摘要。

        Args:
            session_id: 会话键。
            topic_id: 关联话题；None 表示"无话题"的会话级摘要。
            summary: 摘要正文。
            source_turn_id: 产生摘要的轮次，便于回放。

        Returns:
            新记忆的主键。

        TODO(v0.1): 同一 topic 的旧 active 摘要置 archived 后再插入，避免重复。
        """
        raise NotImplementedError("v0.1: MemoryStore.save_summary")

    def save_assertion(
        self,
        session_id: str,
        content: str,
        confidence: float,
        keywords: list[str] | None,
        source_turn_id: str | None,
    ) -> int | None:
        """写入用户断言（事实/偏好）。

        Args:
            session_id: 会话键。
            content: 断言正文，如"用户喜欢猫"。
            confidence: 置信度；低于 `assertion_confidence_threshold` 时不入库。
            keywords: 归一化关键词，便于粗召回。
            source_turn_id: 来源轮次。

        Returns:
            新记忆主键；被阈值拒绝时返回 None。
        """
        raise NotImplementedError("v0.1: MemoryStore.save_assertion")

    def save_episode(
        self,
        session_id: str,
        content: str,
        salience: float,
        keywords: list[str] | None,
        source_turn_id: str | None,
    ) -> int | None:
        """写入情节记忆（有情感显著性的具体事件）。

        Args:
            session_id: 会话键。
            content: 事件描述，如"用户因为考试失利很难过，聊了很久"。
            salience: 显著性；低于 `episode_salience_threshold` 时丢弃。
            keywords: 关键词。
            source_turn_id: 来源轮次。

        Returns:
            新记忆主键；被阈值拒绝时返回 None。
        """
        raise NotImplementedError("v0.1: MemoryStore.save_episode")

    def _score(self, row: dict[str, Any], topic_text: str, now: float | None = None) -> RecallItem:
        """按文档公式给单条记忆打分。

        Args:
            row: `memories` 行（dict）。
            topic_text: 当前话题文本。
            now: 统一时间，None 取当前。

        Returns:
            RecallItem（score 由四个分量加权得到）。

        TODO(v0.1): 权重归一化；`usage` 用 recall_count/corrected_count 计算
            （未被纠正的近期召回加分，被纠正过的减分）。
        """
        raise NotImplementedError("v0.1: MemoryStore._score")

    def _recency(self, created_at: float, now: float) -> float:
        """计算新近度衰减。

        Args:
            created_at: 记忆创建时间。
            now: 当前时间。

        Returns:
            `0.5 ** (age_days / half_life_days)`。

        TODO(v0.1): age 用 `max(0, now - created_at)`，避免时钟回拨产生 >1 的值。
        """
        raise NotImplementedError("v0.1: MemoryStore._recency")

    def _relevance(self, row: dict[str, Any], topic_text: str) -> float:
        """计算相关性（走 textsim，不在这里重新实现）。

        Args:
            row: 记忆行。
            topic_text: 当前话题文本。

        Returns:
            0..1 相关性；关键词命中给强先验，否则退回文本相似度。

        TODO(v0.1): 与 textsim.topic_similarity 共用同一打分栈。
        """
        raise NotImplementedError("v0.1: MemoryStore._relevance")
