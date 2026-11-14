"""自适应防抖：按"这个用户平时怎么发消息"决定这一轮等多久。

动机：固定 2 秒对两类人都不合适——

* **爱连发的人**（一条消息拆三段）：2 秒常常不够，第二段到达时第一轮已经回完了；
* **习惯一句一条的人**：每次都要白等 2 秒才回复，纯延迟。

做法：按 `(会话, 用户)` 记录最近 `window` 条消息的时间戳，只看 **间隔不超过
`horizon_seconds` 的相邻消息**（更长的间隔是"新一轮对话"，不该算进节奏），
取中位数 × `multiplier` 作为窗口，再夹到 `[min_seconds, max_seconds]`。

* 样本不足（少于 `min_samples` 个间隔）→ 用配置的基础值（冷启动）；
* 有足够历史但**从未连发**（一个间隔都没落在 horizon 内）→ 用下限，回得更快；
* 永远不超过聚合的总等待上限（那由 `Aggregator` 再夹一次）。

纯标准库、无 IO，便于单测与复用。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable

DEFAULT_WINDOW = 12
DEFAULT_HORIZON_SECONDS = 15.0
DEFAULT_MULTIPLIER = 1.35
DEFAULT_MIN_SAMPLES = 2
#: 最多同时跟踪多少个 (会话, 用户)；超出时淘汰最久没说话的那个，避免字典无限增长。
MAX_TRACKED_KEYS = 512


@dataclass(frozen=True)
class AdaptiveConfig:
    """自适应策略参数（与 topic.aggregation_adaptive_* 对应）。"""

    enabled: bool = True
    min_seconds: float = 0.8
    max_seconds: float = 6.0
    base_seconds: float = 2.0
    window: int = DEFAULT_WINDOW
    horizon_seconds: float = DEFAULT_HORIZON_SECONDS
    multiplier: float = DEFAULT_MULTIPLIER
    min_samples: int = DEFAULT_MIN_SAMPLES


def _percentile(values: Iterable[float], ratio: float) -> float:
    """线性插值分位数（输入无需有序；空序列返回 0）。"""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    position = max(0.0, min(1.0, ratio)) * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return float(ordered[low]) * (1 - weight) + float(ordered[high]) * weight


class AdaptiveDebounce:
    """按 key（通常是 `(umo, sender_id)`）学习发消息节奏。"""

    def __init__(
        self,
        cfg: AdaptiveConfig | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """初始化。

        Args:
            cfg: 策略参数；None 用默认值。
            clock: 单调时钟，测试可注入；必须与 `Aggregator` 用同一个。
        """
        import time as _time

        self.cfg = cfg or AdaptiveConfig()
        self._clock = clock or _time.monotonic
        self._stamps: dict[tuple[str, str], deque[float]] = {}

    # ------------------------------------------------------------------ 观测
    def observe(self, key: tuple[str, str], now: float | None = None) -> None:
        """记录一条消息的到达时间。"""
        stamp = self._clock() if now is None else float(now)
        bucket = self._stamps.get(key)
        if bucket is None:
            if len(self._stamps) >= MAX_TRACKED_KEYS:
                self._evict_oldest()
            bucket = deque(maxlen=max(2, int(self.cfg.window)))
            self._stamps[key] = bucket
        bucket.append(stamp)

    def _evict_oldest(self) -> None:
        """淘汰最久没有消息的 key（只在超过上限时调用）。"""
        if not self._stamps:
            return
        oldest_key = min(self._stamps, key=lambda item: self._stamps[item][-1])
        self._stamps.pop(oldest_key, None)

    def gaps(self, key: tuple[str, str]) -> list[float]:
        """返回"节奏间隔"列表（只保留 <= horizon 的相邻间隔）。"""
        bucket = self._stamps.get(key)
        if not bucket or len(bucket) < 2:
            return []
        stamps = list(bucket)
        result: list[float] = []
        for previous, current in zip(stamps, stamps[1:]):
            gap = current - previous
            if 0.0 < gap <= self.cfg.horizon_seconds:
                result.append(gap)
        return result

    # ------------------------------------------------------------------ 决策
    def window(self, key: tuple[str, str], base: float | None = None) -> float:
        """返回本轮应使用的防抖窗口（秒）。

        Args:
            key: `(会话, 用户)`。
            base: 冷启动/兜底窗口；None 用 `cfg.base_seconds`。

        Returns:
            夹在 `[min_seconds, max_seconds]` 内的窗口。
        """
        base_window = float(self.cfg.base_seconds if base is None else base)
        if not self.cfg.enabled:
            return base_window
        low = min(self.cfg.min_seconds, self.cfg.max_seconds)
        high = max(self.cfg.min_seconds, self.cfg.max_seconds)
        gaps = self.gaps(key)
        bucket = self._stamps.get(key)
        history = len(bucket) if bucket else 0

        if len(gaps) >= max(1, int(self.cfg.min_samples)):
            suggestion = _percentile(gaps, 0.5) * float(self.cfg.multiplier)
        elif history >= 4 and not gaps:
            # 有足够历史却从未连发：这个人不需要等，直接用下限。
            suggestion = low
        else:
            suggestion = base_window
        return max(low, min(high, suggestion))

    # ------------------------------------------------------------------ 诊断
    def stats(self, limit: int = 20) -> list[dict[str, Any]]:
        """导出每个 key 的学习结果（页面/`/ctm doctor` 用）。"""
        rows: list[dict[str, Any]] = []
        for key, bucket in list(self._stamps.items())[-max(1, limit) :]:
            gaps = self.gaps(key)
            rows.append(
                {
                    "session": key[0],
                    "sender": key[1],
                    "samples": len(bucket),
                    "gaps": len(gaps),
                    "median_gap_seconds": round(_percentile(gaps, 0.5), 2)
                    if gaps
                    else None,
                    "window_seconds": round(self.window(key), 2),
                }
            )
        return rows

    def clear(self) -> None:
        """清空学习结果（插件重载/会话重置时用）。"""
        self._stamps.clear()
