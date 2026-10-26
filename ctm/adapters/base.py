"""记忆后端的窄接口。

为什么只留三个方法：适配器的职责是"把 CTM 的记忆包放进/取出外部记忆系统"，
不是重新实现召回策略。评分、预算、人格全部在 CTM 内部完成；外部后端只负责
存取与粗召回。这样换后端不会改变人格表现，也便于 A/B。

失败语义：所有方法都**不得抛异常给主流程**。错误以 `AdapterUnavailable`
表达，由 `adapters.resolve()` 决定是否回退 SQLite。
"""

from __future__ import annotations

from typing import Any, Protocol

from ..memory_store import RecallItem


class MemoryAdapter(Protocol):
    """外部记忆后端契约。"""

    #: 后端名（sqlite / living_memory / memory_companion），用于日志与状态页。
    name: str

    async def save_summary(
        self,
        session_id: str,
        topic: str,
        summary: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """保存一条话题摘要。

        Args:
            session_id: 会话键（隔离边界）。
            topic: 话题标题。
            summary: 摘要正文。
            meta: 附加信息（时间戳、来源 turn_id 等）。

        Returns:
            None。

        Raises:
            AdapterUnavailable: 后端不可用或写入失败。
        """
        ...

    async def recall(self, session_id: str, query: str, limit: int) -> list[RecallItem]:
        """按查询召回候选（只做粗召回，打分由 CTM 做）。

        Args:
            session_id: 会话键。
            query: 查询文本。
            limit: 上限。

        Returns:
            候选列表；后端无数据时返回空列表。

        Raises:
            AdapterUnavailable: 后端不可用。
        """
        ...

    async def health(self) -> bool:
        """探活。

        Returns:
            可用返回 True。实现不得抛异常。
        """
        ...


class AdapterUnavailable(RuntimeError):
    """适配器不可用（缺插件、缺配置、健康检查失败）。"""
