"""AstrBot 接线层导入 smoke：缺少运行时则跳过（单测不强制装 AstrBot）。

v0.4.1 起 handler 必须是 `CtmPlugin` 的类方法（框架会把实例 partial 到第一个
位置参数上）；模块级 handler 会被派发成 `f(实例, event)` → TypeError 且被框架
吞掉，表现为"加载成功但一条消息都不处理"。这里把这条约束固化成断言。
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

try:  # noqa: SIM105
    import astrbot  # noqa: F401

    HAS_ASTRBOT = True
except Exception:  # noqa: BLE001
    HAS_ASTRBOT = False

HANDLER_NAMES = (
    "ctm_on_event",
    "ctm_on_llm_request",
    "ctm_on_llm_response",
    "ctm_command",
    "ctw_command",
)


@unittest.skipUnless(HAS_ASTRBOT, "AstrBot runtime not installed")
class ImportSmokeTest(unittest.TestCase):
    def test_plugin_module_imports(self) -> None:
        import astrbot_plugin_ctm.main as plugin_main

        self.assertTrue(hasattr(plugin_main, "CtmPlugin"))
        for tool_name in ("memory_search_tool", "memory_save_tool", "memory_forget_tool"):
            self.assertTrue(hasattr(plugin_main.CtmPlugin, tool_name))

    def test_handlers_are_bound_methods(self) -> None:
        """handler 必须是 (self, ...) 的类方法，否则框架绑定时必然 TypeError。"""
        import astrbot_plugin_ctm.main as plugin_main

        for name in HANDLER_NAMES:
            handler = plugin_main.CtmPlugin.__dict__.get(name)
            self.assertIsNotNone(handler, f"缺少 handler：{name}")
            params = list(inspect.signature(handler).parameters.values())
            self.assertGreaterEqual(len(params), 2, f"{name} 至少要有 (self, event)")
            self.assertEqual(
                params[0].name,
                "self",
                f"{name} 第一参数必须是 self（框架会把插件实例绑到它上面）",
            )

    def test_no_module_level_handlers_left(self) -> None:
        """模块级 `async def f(event)` 会被误绑：不允许再出现在模块顶层。"""
        import astrbot_plugin_ctm.main as plugin_main

        for name in HANDLER_NAMES:
            self.assertFalse(
                inspect.isfunction(getattr(plugin_main, name, None)),
                f"{name} 不应再是模块级函数（v0.4.0 静默失效的根因）",
            )


if __name__ == "__main__":
    unittest.main()
