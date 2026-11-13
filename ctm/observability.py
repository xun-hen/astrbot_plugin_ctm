"""可观测性：稳定日志码 + 进程内诊断计数（纯标准库）。"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable, Mapping

LOG_PREFIX = "[ctm]"


class LogCode(str, Enum):
    """稳定日志码。新增错误路径必须先在这里加码。"""

    DECISION = "INFO_DECISION"
    DROPPED = "INFO_DROPPED"
    NO_DECISION = "WARN_NO_DECISION"
    PREEMPTED = "WARN_PREEMPTED"
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
    if code.value.startswith("WARN"):
        level = "WARNING"
    elif code.value.startswith("ERROR"):
        level = "ERROR"
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

    def as_dict(self) -> dict[str, int]:
        """导出为可 JSON 序列化的字典。"""
        return asdict(self)

    def reset(self) -> None:
        """清零所有计数。"""
        for field_name in self.as_dict():
            setattr(self, field_name, 0)
