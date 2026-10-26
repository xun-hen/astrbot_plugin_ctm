"""memory_companion 适配器（可选）。

memory_companion 通过事件/工具读写记忆包，接口与 LivingMemory 不同：
它更接近"注入包"而不是"长期存储"，因此这里只对接其公开的读写工具，
不复制它的内部表结构。

与 LivingMemory 相同的硬约束：不 import 对方、不假设对方已加载、
失败只抛 `AdapterUnavailable`。
"""

from __future__ import annotations

from typing import Any

from ..memory_store import RecallItem


class MemoryCompanionAdapter:
    """通过 memory_companion 的工具读写记忆。"""

    name = "memory_companion"

    def __init__(self, context: Any, store: Any = None) -> None:
        """初始化。

        Args:
            context: AstrBot `Context`。
            store: 回退用的 SQLite Store。
        """
        self._context = context
        self._store = store

    def _find_plugin(self) -> Any | None:
        """查找 memory_companion 插件实例。

        Returns:
            实例或 None。

        TODO(v0.3): 与 LivingMemoryAdapter 共用查找策略（v0.3 抽到 adapters 包内）。
        """
        raise NotImplementedError("v0.3: MemoryCompanionAdapter._find_plugin")

    async def save_summary(
        self,
        session_id: str,
        topic: str,
        summary: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """把摘要写入对方的记忆包。

        Args:
            session_id: 会话键。
            topic: 话题标题。
            summary: 摘要正文。
            meta: 附加信息。

        Returns:
            None。

        Raises:
            AdapterUnavailable: 插件缺失或调用失败。

        TODO(v0.3): 走对方公开工具；失败包装。
        """
        raise NotImplementedError("v0.3: MemoryCompanionAdapter.save_summary")

    async def recall(self, session_id: str, query: str, limit: int) -> list[RecallItem]:
        """从对方记忆包召回。

        Args:
            session_id: 会话键。
            query: 查询文本。
            limit: 上限。

        Returns:
            候选列表。

        Raises:
            AdapterUnavailable: 插件缺失或调用失败。

        TODO(v0.3): 映射成 RecallItem；外部 score 不可信时统一置 0。
        """
        raise NotImplementedError("v0.3: MemoryCompanionAdapter.recall")

    async def health(self) -> bool:
        """探活。

        Returns:
            可用返回 True。

        TODO(v0.3): 捕获所有异常返回 False。
        """
        raise NotImplementedError("v0.3: MemoryCompanionAdapter.health")
