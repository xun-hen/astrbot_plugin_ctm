"""记忆仓库：候选召回、打分、写入与反馈（纯标准库）。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..config import MemoryConfig
from .enums import MemoryType
from .textsim import tokenize, topic_similarity


@dataclass(frozen=True)
class RecallItem:
    """一条待注入的记忆。"""

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
    """召回查询参数。"""

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
        """召回本轮候选并按分数降序返回。"""
        try:
            session_id = str(getattr(signal, "session_id", ""))
            topic_text = str(getattr(state, "current_topic", "") or "")
            emotion = float(getattr(signal, "emotion_score", 0.0) or 0.0)
            rows = self._store.fetch_memories(
                session_id,
                [
                    MemoryType.SUMMARY.value,
                    MemoryType.ASSERTION.value,
                    MemoryType.EPISODE.value,
                ],
                limit=max(1, self._cfg.summary_limit + self._cfg.assertion_limit + self._cfg.episode_limit) * 4,
            )
        except Exception:  # noqa: BLE001
            return []

        now = time.time()
        limits = {
            MemoryType.SUMMARY.value: self._cfg.summary_limit,
            MemoryType.ASSERTION.value: self._cfg.assertion_limit,
            MemoryType.EPISODE.value: self._cfg.episode_limit,
        }
        counts = {key: 0 for key in limits}
        items: list[RecallItem] = []
        for row in rows:
            memory_type = str(row.get("type", ""))
            if memory_type not in limits or counts[memory_type] >= limits[memory_type]:
                continue
            if memory_type == MemoryType.ASSERTION.value and float(
                row.get("confidence", 0.0)
            ) < self._cfg.assertion_confidence_threshold:
                continue
            if memory_type == MemoryType.EPISODE.value:
                salience = float(row.get("emotional_salience", 0.0))
                threshold = self._cfg.episode_salience_threshold
                if emotion < -0.3:
                    threshold *= 0.8
                if salience < threshold:
                    continue
            item = self._score(row, topic_text, now)
            if item.score <= 0:
                continue
            counts[memory_type] += 1
            items.append(item)
        items.sort(key=lambda item: item.score, reverse=True)
        return items

    def mark_recalled(self, memory_ids: list[int]) -> None:
        """记录"这些记忆被注入了"。"""
        if not memory_ids:
            return
        try:
            self._store.mark_recalled(memory_ids)
        except Exception:  # noqa: BLE001
            pass

    def update_importance(self, memory_id: int, delta: float) -> None:
        """按固定增量调整重要度（结果裁剪到 [0,1]）。"""
        try:
            self._store.adjust_importance(int(memory_id), float(delta))
        except Exception:  # noqa: BLE001
            pass

    def save_summary(
        self,
        session_id: str,
        topic_id: int | None,
        summary: str,
        source_turn_id: str | None,
        importance: float = 0.6,
    ) -> int:
        """写入话题摘要（同一 topic 的旧摘要不删除，按时间倒序取最新）。

        Args:
            importance: 重要度；摘要（整段对话的结论）默认高于普通记忆，
                归档器传入 0.8 以提高后续召回权重。
        """
        return int(
            self._store.insert_memory(
                session_id=session_id,
                memory_type=MemoryType.SUMMARY.value,
                content=summary,
                importance=max(0.0, min(1.0, float(importance))),
                confidence=0.9,
                source_turn_id=source_turn_id,
            )
        )

    def save_assertion(
        self,
        session_id: str,
        content: str,
        confidence: float,
        keywords: list[str] | None,
        source_turn_id: str | None,
    ) -> int | None:
        """写入用户断言；低于阈值返回 None。"""
        if confidence < self._cfg.assertion_confidence_threshold:
            return None
        return int(
            self._store.insert_memory(
                session_id=session_id,
                memory_type=MemoryType.ASSERTION.value,
                content=content,
                keywords=keywords or [],
                importance=max(0.3, min(1.0, confidence)),
                confidence=confidence,
                source_turn_id=source_turn_id,
            )
        )

    def save_episode(
        self,
        session_id: str,
        content: str,
        salience: float,
        keywords: list[str] | None,
        source_turn_id: str | None,
    ) -> int | None:
        """写入情节记忆；低于阈值返回 None。"""
        if salience < self._cfg.episode_salience_threshold:
            return None
        return int(
            self._store.insert_memory(
                session_id=session_id,
                memory_type=MemoryType.EPISODE.value,
                content=content,
                keywords=keywords or [],
                salience=salience,
                importance=max(0.4, min(1.0, salience)),
                confidence=0.8,
                source_turn_id=source_turn_id,
            )
        )

    def _score(
        self, row: dict[str, Any], topic_text: str, now: float | None = None
    ) -> RecallItem:
        """按文档公式给单条记忆打分。"""
        now = time.time() if now is None else now
        relevance = self._relevance(row, topic_text)
        salience = max(0.0, min(1.0, float(row.get("emotional_salience", 0.0) or 0.0)))
        recency = self._recency(float(row.get("created_at", 0.0) or 0.0), now)
        recall_count = int(row.get("recall_count", 0) or 0)
        corrected = int(row.get("corrected_count", 0) or 0)
        usage = min(1.0, recall_count / 5.0) * (1.0 / (1.0 + corrected))
        weights = self._cfg.weights
        total = weights.total() or 1.0
        score = (
            relevance * weights.relevance
            + salience * weights.salience
            + recency * weights.recency
            + usage * weights.usage
        ) / total
        return RecallItem(
            memory_id=int(row["id"]),
            memory_type=MemoryType(str(row.get("type"))),
            content=str(row.get("content", "")),
            score=max(0.0, min(1.0, score)),
            relevance=relevance,
            salience=salience,
            recency=recency,
            usage=usage,
            reason=f"rel={relevance:.2f},sal={salience:.2f},rec={recency:.2f}",
        )

    def _recency(self, created_at: float, now: float) -> float:
        """计算新近度衰减。"""
        age_days = max(0.0, (now - created_at) / 86400.0)
        half_life = max(1.0, self._cfg.recency_half_life_days)
        return 0.5 ** (age_days / half_life)

    def _relevance(self, row: dict[str, Any], topic_text: str) -> float:
        """计算相关性：关键词命中给强先验，否则退回文本相似度。"""
        content = str(row.get("content", ""))
        keywords = row.get("keywords")
        if isinstance(keywords, list) and topic_text:
            topic_tokens = tokenize(topic_text)
            hits = len({word for word in keywords if word} & topic_tokens)
            if hits:
                return min(1.0, 0.6 + 0.1 * hits)
        return topic_similarity(topic_text, content)
