"""适配器工厂：按配置选择后端，失败回退 SQLite。

设计文档 §3.8 的选择逻辑：

| backend | 行为 |
| --- | --- |
| sqlite | 直接用本地库 |
| living_memory | 插件存在且健康 → 用；否则回退 SQLite 并记 WARN |
| memory_companion | 同上 |
| 非法值 | 回退 SQLite 并记 WARN |

工厂**永不抛异常**：任何时候都返回一个可用的 adapter。这样"换后端"最坏是
退化成本地库，而不是插件起不来。
"""

from __future__ import annotations

from typing import Any

from ...config import AdapterConfig
from .base import AdapterUnavailable, MemoryAdapter
from .living_memory_adapter import LivingMemoryAdapter
from .memory_companion_adapter import MemoryCompanionAdapter
from .sqlite_adapter import SQLiteAdapter

__all__ = [
    "AdapterUnavailable",
    "MemoryAdapter",
    "SQLiteAdapter",
    "LivingMemoryAdapter",
    "MemoryCompanionAdapter",
    "create_adapter",
]


def create_adapter(cfg: AdapterConfig, store: Any, context: Any = None) -> MemoryAdapter:
    """按配置创建适配器。

    Args:
        cfg: 适配器配置。
        store: SQLite 持久层（回退用）。
        context: AstrBot Context（外部适配器需要）。

    Returns:
        可用的 MemoryAdapter；外部后端不可用时返回 SQLiteAdapter。

    TODO(v0.1):
        1. backend == sqlite → SQLiteAdapter；
        2. living_memory / memory_companion → 构造后 `await health()` 探测；
        3. 探测失败且 fallback_sqlite → SQLiteAdapter + WARN_ADAPTER_FALLBACK；
        4. 探测失败且未开回退 → 仍返回 SQLiteAdapter（文档 §3.8 的"保证不崩"），
           但把配置有效性标为 false（由 /ctm health 展示）。
    """
    raise NotImplementedError("v0.1: create_adapter")
