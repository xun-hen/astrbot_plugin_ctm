"""可观测性：结构化日志码 + 进程内诊断计数。

为什么要有"日志码"而不是随手写中文日志：设计文档 §5 的异常路径（缺决策、
注入失败、预算溢出、归档失败）必须**可被查询**，否则排障只能靠通读日志。
日志码是唯一稳定的检索键，文案可以改，码不能改。

本模块不依赖 AstrBot（只依赖标准库），但实际日志会走 AstrBot 的 logger
（由 `main.py` 注入 `get_logger()` 的结果），这样单测里可以替换成捕获器。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping

LOG_PREFIX = "[ctm]"


class LogCode(str, Enum):
    """稳定日志码。新增错误路径必须先在这里加码。"""

    # 决策链路
    DECISION = "INFO_DECISION"
    DROPPED = "INFO_DROPPED"
    NO_DECISION = "WARN_NO_DECISION"
    # 注入链路
    INJECTION_ERROR = "ERROR_INJECTION"
    BUDGET_OVERFLOW = "WARN_BUDGET_OVERFLOW"
    DUPLICATE_INJECTION = "WARN_DUPLICATE_INJECTION"
    PROMPT_GROWTH_CAPPED = "WARN_PROMPT_GROWTH_CAPPED"
    # 存储 / 适配器
    ADAPTER_FALLBACK = "WARN_ADAPTER_FALLBACK"
    STORE_ERROR = "ERROR_STORE"
    ARCHIVE_FAILED = "ERROR_ARCHIVE"
    ARCHIVE_DROPPED = "WARN_ARCHIVE_DROPPED"


_Emitter = Callable[[str, Mapping[str, Any]], None]
"""日志出口签名：(level, fields)。由 main.py 绑定到真实 logger。"""


def redact(text: str) -> str:
    """按配置脱敏记忆内容。

    Args:
        text: 原始文本。

    Returns:
        脱敏后的文本；开启 log_redact 时只保留长度与哈希前缀。

    TODO(v0.2): 实现 `len=xx sha1=abcdef` 形式；默认参数从 cfg 传入，
        当前签名先固定（调用方恒经 `log_event` 拿脱敏结果）。
    """
    raise NotImplementedError("v0.2: redact")


def log_event(
    code: LogCode,
    *,
    session_id: str = "-",
    emit: _Emitter | None = None,
    **fields: Any,
) -> None:
    """输出一条结构化日志。

    Args:
        code: 稳定日志码。
        session_id: 会话标识；写 "-" 表示与具体会话无关（如启动日志）。
        emit: 日志出口；None 时走模块级 logger（由 `configure()` 注入）。
        **fields: 结构化字段（turn_id、budget、dropped 等）。

    Returns:
        None。

    TODO(v0.1): 统一格式 `[ctm] CODE key=value ...`；异常一律吞掉，
        日志失败绝不影响主流程。
    """
    raise NotImplementedError("v0.1: log_event")


def configure(emit: _Emitter) -> None:
    """绑定日志出口（由 main.py 在 initialize 时调用）。

    Args:
        emit: 接收 (level, fields) 的回调；级别取自日志码前缀。

    Returns:
        None。

    TODO(v0.1): 保存到模块级变量；重复调用时覆盖（重载场景）。
    """
    raise NotImplementedError("v0.1: configure")


@dataclass
class Diag:
    """进程内诊断计数，供 `/ctm status` 与 `ctm_diag` 使用。

    只做同步累加（单线程事件循环内），不落盘；落盘口径见设计文档 §7。

    Attributes:
        turns: 参与判定的轮数。
        decisions: 成功产出 TurnDecision 的次数。
        no_decision: on_llm_request 时找不到决策的次数。
        soft_switches: 软边界次数。
        hard_switches: 硬切次数。
        recall_items: 累计注入的记忆条数。
        dropped_items: 累计被预算丢弃的条数。
        injected_tokens: 累计注入 Token 估算值。
        injection_errors: 注入异常次数。
        archive_failures: 归档最终失败次数。
        adapter_fallbacks: 适配器回退次数。
    """

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
        """导出为可 JSON 序列化的字典。

        Returns:
            字段名到计数的映射。

        TODO(v0.1): 用 dataclasses.asdict 实现。
        """
        raise NotImplementedError("v0.1: Diag.as_dict")

    def reset(self) -> None:
        """清零所有计数。

        Returns:
            None。

        TODO(v0.1): 逐字段归零（避免 asdict 后再赋值带来的遗漏）。
        """
        raise NotImplementedError("v0.1: Diag.reset")
