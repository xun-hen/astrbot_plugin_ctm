"""handler 绑定自检的单测（纯标准库，不需要 AstrBot）。

这是 v0.4.0 静默失效的回归测试：框架把插件实例 partial 到 handler 第一个
位置参数上，模块级 `async def f(event)` 会被派发成 `f(实例, event)` → TypeError
并被框架吞掉。`bindings.audit_handlers` 必须能识别出这种写法。
"""

from __future__ import annotations

import functools
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_ctm.ctm import bindings  # noqa: E402


class _RegistryEntry:
    """注册表条目的最小替身。"""

    def __init__(self, name: str, module_path: str, handler) -> None:
        self.handler_name = name
        self.handler_module_path = module_path
        self.handler = handler


async def _method_style(self, event):  # 类方法的原函数形态
    return None


async def _module_style(event):  # v0.4.0 的写法：模块级单参函数
    return None


class InstanceBoundTest(unittest.TestCase):
    def test_partial_bound_method_is_ok(self) -> None:
        handler = functools.partial(_method_style, object())
        self.assertTrue(bindings.is_instance_bound(handler))

    def test_partial_bound_module_function_is_unbound(self) -> None:
        # 关键回归：绑定了实例但原函数第一参数是 event → 派发必然 TypeError。
        handler = functools.partial(_module_style, object())
        self.assertFalse(bindings.is_instance_bound(handler))

    def test_raw_function_is_unbound(self) -> None:
        self.assertFalse(bindings.is_instance_bound(_module_style))

    def test_signature_unavailable_is_not_fatal(self) -> None:
        # 拿不到签名（内建类型）时按 True 处理，避免误报打断插件。
        handler = functools.partial(dict, a=1)  # inspect.signature(dict) 抛 ValueError
        self.assertTrue(bindings.is_instance_bound(handler))


class AuditHandlersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = "data.plugins.demo.main"
        self.names = ("ctm_on_event", "ctm_on_llm_request")

    def test_all_ok(self) -> None:
        registry = [
            _RegistryEntry(
                "ctm_on_event", self.module, functools.partial(_method_style, object())
            ),
            _RegistryEntry(
                "ctm_on_llm_request",
                self.module,
                functools.partial(_method_style, object()),
            ),
        ]
        result = bindings.audit_handlers(registry, self.module, self.names)
        self.assertEqual(result["ctm_on_event"], bindings.STATUS_OK)
        self.assertEqual(result["ctm_on_llm_request"], bindings.STATUS_OK)

    def test_v040_style_detected(self) -> None:
        registry = [
            _RegistryEntry(
                "ctm_on_event", self.module, functools.partial(_module_style, object())
            ),
        ]
        result = bindings.audit_handlers(registry, self.module, self.names)
        self.assertEqual(result["ctm_on_event"], bindings.STATUS_UNBOUND)
        self.assertEqual(result["ctm_on_llm_request"], bindings.STATUS_MISSING)

    def test_other_module_is_ignored(self) -> None:
        registry = [
            _RegistryEntry(
                "ctm_on_event",
                "data.plugins.other.main",
                functools.partial(_method_style, object()),
            ),
        ]
        result = bindings.audit_handlers(registry, self.module, self.names)
        self.assertEqual(result["ctm_on_event"], bindings.STATUS_MISSING)

    def test_registry_failure_returns_unknown(self) -> None:
        class Boom:
            def __iter__(self):
                raise RuntimeError("registry exploded")

        result = bindings.audit_handlers(Boom(), self.module, self.names)
        self.assertEqual(
            set(result.values()), {bindings.STATUS_UNKNOWN}, "自检不能抛异常"
        )


class DescribeTest(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertIn("未自检", bindings.describe({}))

    def test_ok(self) -> None:
        text = bindings.describe({"a": bindings.STATUS_OK})
        self.assertIn("ok", text)

    def test_bad(self) -> None:
        text = bindings.describe({"a": bindings.STATUS_UNBOUND})
        self.assertIn("unbound", text)


if __name__ == "__main__":
    unittest.main()
