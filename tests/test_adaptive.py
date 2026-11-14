"""自适应防抖单测（纯标准库）。

覆盖：关闭即回退、冷启动、连发用户、单发用户、上下限夹取、与聚合器的接线。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_ctm.ctm.adaptive import (  # noqa: E402
    AdaptiveConfig,
    AdaptiveDebounce,
    _percentile,
)
from astrbot_plugin_ctm.ctm.aggregation import Aggregator  # noqa: E402

KEY = ("s:Friend:u1", "u1")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _learn(gaps: list[float], *, cfg: AdaptiveConfig | None = None):
    clock = _Clock()
    learner = AdaptiveDebounce(cfg or AdaptiveConfig(), clock=clock)
    learner.observe(KEY)
    for gap in gaps:
        clock.advance(gap)
        learner.observe(KEY)
    return learner, clock


class PercentileTest(unittest.TestCase):
    def test_extremes(self) -> None:
        self.assertEqual(_percentile([1, 2, 3], 0.0), 1.0)
        self.assertEqual(_percentile([1, 2, 3], 1.0), 3.0)
        self.assertEqual(_percentile([2], 0.5), 2.0)
        self.assertEqual(_percentile([], 0.5), 0.0)

    def test_median(self) -> None:
        self.assertAlmostEqual(_percentile([1, 3, 9], 0.5), 3.0)


class WindowTest(unittest.TestCase):
    def test_disabled_returns_base(self) -> None:
        learner, _ = _learn([0.5, 0.5, 0.5], cfg=AdaptiveConfig(enabled=False))
        self.assertAlmostEqual(learner.window(KEY, base=2.5), 2.5)

    def test_cold_start_uses_base(self) -> None:
        learner = AdaptiveDebounce(AdaptiveConfig(), clock=_Clock())
        learner.observe(KEY)
        self.assertAlmostEqual(learner.window(KEY, base=2.0), 2.0)

    def test_burst_user_gets_wider_window(self) -> None:
        # 用户习惯每 0.9s 补一句：窗口应明显高于下限（0.9*1.35≈1.22）
        learner, _ = _learn([0.9, 0.9, 0.8, 1.0])
        window = learner.window(KEY)
        self.assertGreater(window, 0.8, "连发用户不应退回最小值")
        self.assertLess(window, 1.6)

    def test_very_fast_burst_pinned_to_floor(self) -> None:
        # 0.5s 连发：中位数*1.35 < 下限，夹到下限即可（0.8s 已足够合并）
        learner, _ = _learn([0.5, 0.5, 0.6, 0.5])
        self.assertAlmostEqual(learner.window(KEY), AdaptiveConfig().min_seconds)

    def test_single_sender_gets_minimum(self) -> None:
        # 间隔都远超 horizon（不是连发），应尽快回，不白等基础窗口
        learner, _ = _learn([60.0, 200.0, 90.0, 120.0])
        self.assertAlmostEqual(learner.window(KEY), AdaptiveConfig().min_seconds)

    def test_window_clamped_to_upper_bound(self) -> None:
        learner, _ = _learn([5.0, 5.0, 5.0], cfg=AdaptiveConfig(max_seconds=6.0))
        self.assertLessEqual(learner.window(KEY), 6.0)

    def test_window_clamped_to_lower_bound(self) -> None:
        learner, _ = _learn([0.1, 0.1, 0.1])
        self.assertGreaterEqual(learner.window(KEY), 0.8)

    def test_horizon_gaps_ignored(self) -> None:
        # 一长一短混排：只统计短间隔
        learner, _ = _learn([600.0, 0.4, 0.4])
        self.assertLess(learner.window(KEY), 2.0)

    def test_stats_reports_learning(self) -> None:
        learner, _ = _learn([0.6, 0.6, 0.6])
        stats = learner.stats()
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["sender"], "u1")
        self.assertEqual(stats[0]["gaps"], 3)
        self.assertIsNotNone(stats[0]["median_gap_seconds"])
        self.assertGreater(stats[0]["window_seconds"], 0)

    def test_clear(self) -> None:
        learner, _ = _learn([0.5, 0.5])
        learner.clear()
        self.assertEqual(learner.stats(), [])


class _Event:
    def __init__(self, name: str) -> None:
        self.name = name
        self.message_str = "x"
        self.message_obj = None
        self.stopped = False

    def stop_event(self) -> None:
        self.stopped = True


class AggregatorWiringTest(unittest.TestCase):
    def test_active_round_exposes_adaptive_window(self) -> None:
        async def scenario() -> dict:
            clock = _Clock()
            learner = AdaptiveDebounce(AdaptiveConfig(min_seconds=0.8), clock=clock)
            agg = Aggregator(
                enabled=True,
                debounce_seconds=2.0,
                max_wait_seconds=12.0,
                clock=clock,
                adaptive=learner,
            )
            # 先学两轮连发节奏
            for _ in range(3):
                clock.advance(0.4)
                learner.observe(KEY, clock())
            agg.accept_task = asyncio.create_task(
                agg.accept(_Event("owner"), key=KEY, text="第一句")
            )
            await asyncio.sleep(0)  # 让 owner 进入等待
            rounds = agg.active_rounds()
            agg._rounds.pop(KEY, None)  # 清理，避免影响后续
            agg.accept_task.cancel()
            try:
                await agg.accept_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            return rounds[0] if rounds else {}

        round_info = asyncio.run(scenario())
        self.assertIn("window_seconds", round_info)
        self.assertGreater(round_info["window_seconds"], 0)
        self.assertLess(round_info["window_seconds"], 1.5)

    def test_no_adaptive_uses_fixed_debounce(self) -> None:
        async def scenario() -> dict:
            clock = _Clock()
            agg = Aggregator(
                enabled=True, debounce_seconds=2.0, max_wait_seconds=12.0, clock=clock
            )
            task = asyncio.create_task(
                agg.accept(_Event("owner"), key=KEY, text="第一句")
            )
            await asyncio.sleep(0)
            rounds = agg.active_rounds()
            agg._rounds.pop(KEY, None)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            return rounds[0] if rounds else {}

        self.assertAlmostEqual(asyncio.run(scenario())["window_seconds"], 2.0)


if __name__ == "__main__":
    unittest.main()
