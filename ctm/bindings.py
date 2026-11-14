"""handler 绑定自检：AstrBot 把插件实例 partial 到 handler 第一个位置参数上。

装载插件时框架会做（`astrbot/core/star/star_manager.py`，4.26 / 4.28 / 4.29 一致）::

    related_handlers = star_handlers_registry.get_handlers_by_module_name(
        metadata.module_path
    )
    for handler in related_handlers:
        handler.handler = functools.partial(handler.handler, metadata.star_cls)

因此 handler **必须是插件类的方法**（第一参数 `self`）。若写成模块级函数
`async def f(event)`，派发时会被调用成 `f(实例, event)` → `TypeError`；该异常被
`astrbot/core/pipeline/context_utils.py` 的 `call_handler` 吞掉，只留一行框架
ERROR 日志（`Plugin Handler arguments do not match its definition.`），然后照常
走 LLM。表现就是：插件加载成功、启动日志正常、配置生效，但**一条消息都没处理、
库里 0 行、插件日志 0 行**——v0.4.0 正是踩了这个坑。

本模块是纯标准库，不 import astrbot，便于单测与启动自检复用。
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Iterable

#: 本插件必须由框架派发的 handler 名（缺一即不可用）。
HANDLER_NAMES: tuple[str, ...] = (
    "ctm_on_event",
    "ctm_on_llm_request",
    "ctm_on_llm_response",
    "ctm_command",
    "ctw_command",
)

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_UNBOUND = "unbound"
STATUS_UNKNOWN = "unknown"


def is_instance_bound(handler: Any) -> bool:
    """判断框架是否已把插件实例绑到 handler 的第一个位置参数上。

    Args:
        handler: 注册表里的 handler（可能是 `functools.partial`）。

    Returns:
        True 表示第一个参数是 `self`/`cls`（类方法，绑定正确）；
        False 表示形如模块级单参函数，派发时必然 TypeError。

    Notes:
        绑定后 `inspect.signature(handler)` 会把 `self` 吃掉，单看签名分不出
        "方法被绑好"和"模块级函数被误绑"，所以这里看 **绑定前的原函数**。
        拿不到签名（内建/C 扩展）时按 True 处理，避免误报。
    """
    if not isinstance(handler, functools.partial):
        return False
    func = getattr(handler, "func", None)
    if func is None:
        return False
    try:
        params = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return True
    if not params:
        return False
    return params[0].name in ("self", "cls")


def audit_handlers(
    registry: Iterable[Any],
    module_path: str,
    names: tuple[str, ...] = HANDLER_NAMES,
) -> dict[str, str]:
    """核对 handler 是否都已注册且绑定到本实例。

    Args:
        registry: 框架的 handler 注册表（可迭代出带 `handler_name` /
            `handler_module_path` / `handler` 的对象）。
        module_path: 本插件模块路径（`type(plugin).__module__`）。
        names: 需要核对的 handler 名。

    Returns:
        `{handler 名: 状态}`，状态见 `STATUS_*`。注册表不可用时全部返回
        `STATUS_UNKNOWN`，绝不抛异常。
    """
    try:
        found: dict[str, Any] = {}
        for metadata in registry:
            name = getattr(metadata, "handler_name", "")
            if (
                name in names
                and getattr(metadata, "handler_module_path", "") == module_path
            ):
                found[name] = getattr(metadata, "handler", None)
    except Exception:  # noqa: BLE001 - 自检不能影响插件运行
        return dict.fromkeys(names, STATUS_UNKNOWN)

    result: dict[str, str] = {}
    for name in names:
        handler = found.get(name)
        if handler is None:
            result[name] = STATUS_MISSING
        elif is_instance_bound(handler):
            result[name] = STATUS_OK
        else:
            result[name] = STATUS_UNBOUND
    return result


def describe(binding: dict[str, str]) -> str:
    """把自检结果压成一行短文本，供 `/ctm health` 与日志使用。"""
    if not binding:
        return "未自检"
    bad = {name: status for name, status in binding.items() if status != STATUS_OK}
    if not bad:
        return f"ok（{len(binding)} 个 handler 已绑定本实例）"
    return "异常：" + ", ".join(f"{name}={status}" for name, status in bad.items())
