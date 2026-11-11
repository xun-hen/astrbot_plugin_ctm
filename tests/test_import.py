"""AstrBot 接线层导入 smoke：缺少运行时则跳过（单测不强制装 AstrBot）。"""

from __future__ import annotations

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


@unittest.skipUnless(HAS_ASTRBOT, "AstrBot runtime not installed")
class ImportSmokeTest(unittest.TestCase):
    def test_plugin_module_imports(self) -> None:
        import astrbot_plugin_ctm.main as plugin_main

        self.assertTrue(callable(plugin_main.ctm_on_event))
        self.assertTrue(callable(plugin_main.ctm_on_llm_request))
        self.assertTrue(callable(plugin_main.ctm_on_llm_response))
        self.assertTrue(callable(plugin_main.ctm_command))
        for tool_name in ("memory_search_tool", "memory_save_tool", "memory_forget_tool"):
            self.assertTrue(hasattr(plugin_main.CtmPlugin, tool_name))


if __name__ == "__main__":
    unittest.main()
