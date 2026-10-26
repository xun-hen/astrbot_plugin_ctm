"""LivingMemory 适配器（可选）。

对接 astrbot_plugin_living_memory 的长期记忆接口。本适配器的一切都建立在
"插件存在且暴露了预期工具"之上；不存在就抛 `AdapterUnavailable`，
由工厂回退 SQLite——**不允许 import 期依赖对方插件**。

数据边界：只发送摘要文本与最小元数据（session_id / 时间），不发送原始聊天记录。
"""

from __future__ import annotations

from typing import Any

from ..memory_store import RecallItem


class LivingMemoryAdapter:
    """通过 LivingMemory 工具读写长期记忆。"""

    name = "living_memory"

    def __init__(self, context: Any, store: Any = None) -> None:
        """初始化。

        Args:
            context: AstrBot `Context`，用于查找目标插件与调用工具。
            store: 回退用的 SQLite Store（工厂在回退时使用）。
        """
        self._context = context
        self._store = store

    def _find_plugin(self) -> Any | None:
        """查找 LiveMemory 插件实例。

        Returns:
            插件实例；未安装返回 None。

        TODO(v0.2): 依次尝试 `context.get_registered_stars()` / `star_map`，
            兼容不同 AstrBot 版本的查找方式；找不到时不要抛，交给 health() 报 False。
        """
        raise NotImplementedError("v0.2: LivingMemoryAdapter._find_plugin")

    async def save_summary(
        self,
        session_id: str,
        topic: str,
        summary: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """调用 `memorize_long_term_memory` 写入摘要。

        Args:
            session_id: 会话键。
            topic: 话题标题。
            summary: 摘要正文。
            meta: 附加信息。

        Returns:
            None。

        Raises:
            AdapterUnavailable: 插件缺失或调用失败。

        TODO(v0.3): 通过插件工具/公开方法写入；超时 3s；
            失败抛 AdapterUnavailable（由工厂回退）。
        """
        raise NotImplementedError("v0.3: LivingMemoryAdapter.save_summary")

    async def recall(self, session_id: str, query: str, limit: int) -> list[RecallItem]:
        """调用 `recall_long_term_memory` 取候选。

        Args:
            session_id: 会话键。
            query: 查询文本。
            limit: 上限。

        Returns:
            候选列表；外部结果需要映射成 RecallItem（score 先置 0，由 CTM 重排）。

        Raises:
            AdapterUnavailable: 插件缺失或调用失败。

        TODO(v0.3): 映射字段；对返回条数做上限裁剪；异常包装。
        """
        raise NotImplementedError("v0.3: LivingMemoryAdapter.recall")

    async def health(self) -> bool:
        """探活。

        Returns:
            插件存在且工具可调用返回 True。

        TODO(v0.3): 捕获所有异常返回 False。
        """
        raise NotImplementedError("v0.3: LivingMemoryAdapter.health")
