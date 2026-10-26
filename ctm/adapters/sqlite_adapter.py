"""默认后端：直接委托给本地 SQLite。

它不是"降级实现"，而是**基准实现**：外部后端的所有行为都应与它对齐，
回归测试也以它为对照。
"""

from __future__ import annotations

from typing import Any

from ..memory_store import RecallItem
from ..store import Store


class SQLiteAdapter:
    """把 `Store` 包装成 `MemoryAdapter`。"""

    name = "sqlite"

    def __init__(self, store: Store) -> None:
        """初始化。

        Args:
            store: 持久层。
        """
        self._store = store

    async def save_summary(
        self,
        session_id: str,
        topic: str,
        summary: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """写摘要到 memories 表（type=summary）。

        Args:
            session_id: 会话键。
            topic: 话题标题。
            summary: 摘要正文。
            meta: 附加信息（turn_id 等）。

        Returns:
            None。

        Raises:
            AdapterUnavailable: 写入失败时包装成适配器错误。

        TODO(v0.1): `store.insert_memory(...)`；topic_id 从 meta 取。
        """
        raise NotImplementedError("v0.1: SQLiteAdapter.save_summary")

    async def recall(self, session_id: str, query: str, limit: int) -> list[RecallItem]:
        """按会话取最近记忆并转成 RecallItem。

        Args:
            session_id: 会话键。
            query: 查询文本（SQLite 后端不参与打分，忽略）。
            limit: 上限。

        Returns:
            候选列表。

        Raises:
            AdapterUnavailable: 查询失败时包装成适配器错误。

        TODO(v0.1): fetch_memories 三种类型各 limit；分数留给 MemoryStore 重排。
        """
        raise NotImplementedError("v0.1: SQLiteAdapter.recall")

    async def health(self) -> bool:
        """检查数据库可用。

        Returns:
            `store.health()["ok"]`。

        TODO(v0.1): 捕获所有异常返回 False。
        """
        raise NotImplementedError("v0.1: SQLiteAdapter.health")
