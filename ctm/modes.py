"""话题模式判定（纯函数，便于单测）。

四种模式与话题路由器 PRO 对齐：

| mode | 判定 | 注入 | 自动 `/new` |
| --- | --- | --- | --- |
| `off` | 否 | 否 | 否 |
| `mark` | 是 | 否（只记录/维护状态） | 否 |
| `hint` | 是 | 是 | 否 |
| `auto` | 是 | 是 | 置信度达标时切换对话 |

`debug_mode` 是"空跑"：判定照常并记日志，但不注入、不切对话、不落话题状态。
"""

from __future__ import annotations

from typing import Any

TOPIC_MODES = ("off", "mark", "hint", "auto")
DEFAULT_MODE = "hint"


def normalize_mode(value: Any) -> str:
    """归一化模式值；非法值回退默认。"""
    text = str(value or "").strip().lower()
    return text if text in TOPIC_MODES else DEFAULT_MODE


def should_judge(mode: str) -> bool:
    """是否需要做话题判定。"""
    return normalize_mode(mode) != "off"


def should_persist_state(mode: str, debug: bool) -> bool:
    """是否允许落地话题状态（debug 为空跑）。"""
    return should_judge(mode) and not debug


def should_inject(mode: str, debug: bool) -> bool:
    """是否允许改写 Prompt（mark/off/debug 都不注入）。"""
    return normalize_mode(mode) in ("hint", "auto") and not debug


def should_auto_switch(
    mode: str,
    verdict: Any,
    threshold: float,
    debug: bool,
) -> bool:
    """是否执行自动 `/new`。

    Args:
        mode: 话题模式。
        verdict: 判定结果（需有 `is_new_topic` / `confidence`）。
        threshold: 自动切换所需的最低置信度。
        debug: 调试模式（空跑）。

    Returns:
        是否切换。
    """
    if normalize_mode(mode) != "auto" or debug or verdict is None:
        return False
    if not bool(getattr(verdict, "is_new_topic", False)):
        return False
    return float(getattr(verdict, "confidence", 0.0)) >= float(threshold)
