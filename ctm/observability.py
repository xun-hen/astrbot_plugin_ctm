"""可观测性：稳定日志码 + 进程内诊断计数（纯标准库）。"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable, Mapping

LOG_PREFIX = "[ctm]"


class LogCode(str, Enum):
    """稳定日志码。新增错误路径必须先在这里加码。

    前缀决定级别：`INFO_` / `DEBUG_` / `WARN_` / `ERROR_`。静默分支一律用
    `DEBUG_SKIP`，保证「消息进来了但没有决策」这件事永远可查、又不刷屏。
    """

    ENTRY = "INFO_ENTRY"
    DECISION = "INFO_DECISION"
    DROPPED = "INFO_DROPPED"
    LLM_REQUEST = "DEBUG_LLM_REQUEST"
    SKIP = "DEBUG_SKIP"
    NO_DECISION = "WARN_NO_DECISION"
    PREEMPTED = "WARN_PREEMPTED"
    AGGREGATION = "WARN_AGGREGATION"
    HANDLER_BINDING = "WARN_HANDLER_BINDING"
    PLUGIN_SET = "WARN_PLUGIN_SET"
    INJECTION_ERROR = "ERROR_INJECTION"
    BUDGET_OVERFLOW = "WARN_BUDGET_OVERFLOW"
    DUPLICATE_INJECTION = "WARN_DUPLICATE_INJECTION"
    PROMPT_GROWTH_CAPPED = "WARN_PROMPT_GROWTH_CAPPED"
    ADAPTER_FALLBACK = "WARN_ADAPTER_FALLBACK"
    STORE_ERROR = "ERROR_STORE"
    ARCHIVE_FAILED = "ERROR_ARCHIVE"
    ARCHIVE_DROPPED = "WARN_ARCHIVE_DROPPED"


_Emitter = Callable[[str, Mapping[str, Any]], None]
_emit: _Emitter | None = None


def redact(text: str) -> str:
    """按 `len=xx sha1=abcdef` 脱敏记忆内容。"""
    raw = text or ""
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"len={len(raw)} sha1={digest}"


def configure(emit: _Emitter) -> None:
    """绑定日志出口（由 main.py 在 initialize 时调用）。"""
    global _emit
    _emit = emit


def log_event(
    code: LogCode,
    *,
    session_id: str = "-",
    emit: _Emitter | None = None,
    **fields: Any,
) -> None:
    """输出一条结构化日志；异常一律吞掉。"""
    emitter = emit or _emit
    if emitter is None:
        return
    level = "INFO"
    if code.value.startswith("ERROR"):
        level = "ERROR"
    elif code.value.startswith("WARN"):
        level = "WARNING"
    elif code.value.startswith("DEBUG"):
        level = "DEBUG"
    payload = {
        "code": code.value,
        "session_id": session_id,
        **fields,
    }
    try:
        emitter(level, payload)
    except Exception:  # noqa: BLE001
        pass


@dataclass
class Diag:
    """进程内诊断计数。"""

    turns: int = 0
    decisions: int = 0
    no_decision: int = 0
    soft_switches: int = 0
    hard_switches: int = 0
    recall_items: int = 0
    dropped_items: int = 0
    injected_tokens: int = 0
    injection_errors: int = 0
    archive_failures: int = 0
    adapter_fallbacks: int = 0
    # 入口可观测性：`entries` 与 `decisions` 的差值就是"进来了但没产出决策"的量。
    entries: int = 0
    llm_requests: int = 0
    skips: int = 0
    aggregation_anomalies: int = 0
    # 心跳：最近一次入口消息 / 决策的时间戳（Unix 秒），供自检判断"多久没动静"。
    last_entry_at: float = 0.0
    last_decision_at: float = 0.0

    def as_dict(self) -> dict[str, int | float]:
        """导出为可 JSON 序列化的字典。"""
        return asdict(self)

    def reset(self) -> None:
        """清零所有计数。"""
        for field_name in self.as_dict():
            setattr(self, field_name, 0)
