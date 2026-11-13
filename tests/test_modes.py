"""四种话题模式的纯函数单测。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_ctm.ctm import modes  # noqa: E402
from astrbot_plugin_ctm.ctm.topic_judge import Verdict  # noqa: E402


def verdict(new: bool, confidence: float) -> Verdict:
    return Verdict(
        verdict="new_topic" if new else "continue",
        confidence=confidence,
        source="llm",
    )


class ModesTest(unittest.TestCase):
    def test_normalize(self) -> None:
        self.assertEqual(modes.normalize_mode("AUTO"), "auto")
        self.assertEqual(modes.normalize_mode("nope"), "hint")
        self.assertEqual(modes.normalize_mode(None), "hint")

    def test_judge(self) -> None:
        self.assertFalse(modes.should_judge("off"))
        for mode in ("mark", "hint", "auto"):
            self.assertTrue(modes.should_judge(mode))

    def test_inject(self) -> None:
        self.assertTrue(modes.should_inject("hint", False))
        self.assertTrue(modes.should_inject("auto", False))
        self.assertFalse(modes.should_inject("mark", False))
        self.assertFalse(modes.should_inject("off", False))
        self.assertFalse(modes.should_inject("hint", True))

    def test_persist(self) -> None:
        self.assertTrue(modes.should_persist_state("mark", False))
        self.assertFalse(modes.should_persist_state("mark", True))
        self.assertFalse(modes.should_persist_state("off", False))

    def test_auto_switch(self) -> None:
        self.assertTrue(
            modes.should_auto_switch("auto", verdict(True, 0.9), 0.8, False)
        )
        self.assertFalse(
            modes.should_auto_switch("auto", verdict(True, 0.7), 0.8, False)
        )
        self.assertFalse(
            modes.should_auto_switch("auto", verdict(True, 0.9), 0.8, True)
        )
        self.assertFalse(
            modes.should_auto_switch("hint", verdict(True, 0.9), 0.8, False)
        )
        self.assertFalse(
            modes.should_auto_switch("auto", verdict(False, 0.9), 0.8, False)
        )


if __name__ == "__main__":
    unittest.main()
