"""消息聚合：把同一会话里连发的几条消息缓冲成一轮。

机制与 `astrbot_plugin_msg_aggregator` / 话题路由器 PRO 的 `aggregation/` 相同：

1. 第一条消息成为 **owner**，`on_message` 里 `await flush.wait()` 挂起，先不放行下游；
2. 后续消息（同会话、同发送者）把正文追加到本轮，然后 `stop_event()` 静默；
3. 防抖计时器到期（或达到总等待上限）→ `flush.set()`，owner 恢复；
4. owner 把合并文本写回 `event.message_str`，管线继续，于是 LLM 只被调用一次。

与完整版相比，这里刻意裁掉了：输入状态感知、撤回过滤、附件转存、统计页面、
跨进程锁。图片等非文本组件直接追加到 owner 的消息链上，不复制文件。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .adaptive import AdaptiveDebounce

DEFAULT_SEPARATOR = "\n"
MAX_ROUNDS = 256
#: owner 等待兜底的额外宽限；超时即放行，避免定时器异常时永久挂住消息。
OWNER_GRACE_SECONDS = 5.0


@dataclass
class Round:
    """一轮聚合。"""

    key: tuple[str, str]
    owner_event: Any
    texts: list[str] = field(default_factory=list)
    extra_components: list[Any] = field(default_factory=list)
    created_at: float = 0.0
    last_at: float = 0.0
    wait_seconds: float = 0.0
    flush: asyncio.Event = field(default_factory=asyncio.Event)
    timer: asyncio.Task[None] | None = None
    closed: bool = False


class Aggregator:
    """按 `(umo, sender)` 分桶的防抖聚合器。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        debounce_seconds: float = 2.0,
        max_wait_seconds: float = 12.0,
        separator: str = DEFAULT_SEPARATOR,
        clock: Callable[[], float] = time.monotonic,
        owner_grace_seconds: float = OWNER_GRACE_SECONDS,
        on_anomaly: Callable[[str, tuple[str, str]], None] | None = None,
        adaptive: AdaptiveDebounce | None = None,
    ) -> None:
        """初始化。

        Args:
            enabled: 是否启用聚合；关闭时 `accept()` 立即放行。
            debounce_seconds: 最后一条消息之后的等待秒数。
            max_wait_seconds: 从第一条消息算起的总等待上限。
            separator: 合并文本的分隔符。
            clock: 单调时钟，测试可注入。
            owner_grace_seconds: owner 等待 flush 的额外宽限；超过
                `max_wait_seconds + owner_grace_seconds` 仍未 flush 就强制放行，
                避免"owner 永久挂住、消息静默不回"。
            on_anomaly: 异常回调 `(原因, key)`，用于记日志/计数。
            adaptive: 自适应防抖策略；None 表示固定窗口（按 `debounce_seconds`）。
        """
        self.enabled = enabled
        self.debounce_seconds = max(0.0, float(debounce_seconds))
        self.max_wait_seconds = max(self.debounce_seconds, float(max_wait_seconds))
        self.owner_grace_seconds = max(0.0, float(owner_grace_seconds))
        self.separator = separator
        self._clock = clock
        self._on_anomaly = on_anomaly
        self._adaptive = adaptive
        self._rounds: dict[tuple[str, str], Round] = {}
        self._closed = False

    async def accept(
        self,
        event: Any,
        *,
        key: tuple[str, str],
        text: str,
        is_command: bool = False,
    ) -> bool:
        """接收一条消息。

        Args:
            event: 消息事件。
            key: `(unified_msg_origin, sender_id)`。
            text: 本条消息的纯文本。
            is_command: 指令消息；指令不参与聚合，并会立即结算当前轮。

        Returns:
            True 表示调用方应继续处理该事件（owner 已恢复）；
            False 表示该事件已被消费（正在缓冲的后续消息），调用方应直接返回。
        """
        if self._closed or not self.enabled:
            return True
        if is_command:
            self._flush_key(key)
            return True

        clean = (text or "").strip()
        now = self._clock()
        if self._adaptive is not None:
            self._adaptive.observe(key, now)
        window = (
            self._adaptive.window(key, base=self.debounce_seconds)
            if self._adaptive is not None
            else self.debounce_seconds
        )
        rnd = self._rounds.get(key)
        if rnd is None or rnd.closed:
            if len(self._rounds) >= MAX_ROUNDS:
                # 极端情况下不让字典无限增长：结算最老的一轮。
                self._notify("rounds_overflow", key)
                self._flush_oldest()
            rnd = Round(
                key=key,
                owner_event=event,
                texts=[clean] if clean else [],
                created_at=now,
                last_at=now,
                wait_seconds=window,
            )
            self._rounds[key] = rnd
            self._arm(rnd)
        else:
            if clean:
                rnd.texts.append(clean)
            self._append_extra_components(rnd, event)
            rnd.last_at = now
            self._arm(rnd)

        if rnd.owner_event is event:
            try:
                # 兜底：定时器被取消/事件循环异常时 flush 可能永不被 set，
                # owner 会永久挂在这里 = 用户消息静默不回。超时强制放行。
                await asyncio.wait_for(
                    rnd.flush.wait(),
                    timeout=self.max_wait_seconds + self.owner_grace_seconds,
                )
            except asyncio.TimeoutError:
                self._notify("owner_wait_timeout", key)
            except asyncio.CancelledError:
                self._abort(key, rnd)
                raise
            self._reconstruct(rnd)
            self._finish(key, rnd)
            return True

        self._silence(event)
        return False

    def active_rounds(self) -> list[dict[str, Any]]:
        """返回当前进行中的聚合轮次（页面用）。"""
        now = self._clock()
        rows = []
        for rnd in self._rounds.values():
            if rnd.closed:
                continue
            rows.append(
                {
                    "session": rnd.key[0],
                    "sender": rnd.key[1],
                    "items": len(rnd.texts),
                    "age_seconds": round(max(0.0, now - rnd.created_at), 1),
                    "window_seconds": round(float(rnd.wait_seconds or 0.0), 2),
                }
            )
        return rows

    def adaptive_stats(self) -> list[dict[str, Any]]:
        """导出自适应窗口的学习结果（空列表 = 未启用）。"""
        if self._adaptive is None:
            return []
        return self._adaptive.stats()

    def close(self) -> None:
        """插件卸载：结算所有进行中的轮次并停止计时器。"""
        self._closed = True
        for key in list(self._rounds):
            self._flush_key(key)
        for rnd in list(self._rounds.values()):
            if rnd.timer is not None:
                rnd.timer.cancel()

    # ------------------------------------------------------------------ internal
    def _notify(self, reason: str, key: tuple[str, str]) -> None:
        """把异常交给外部回调（不回抛，不影响消息流）。"""
        if self._on_anomaly is None:
            return
        try:
            self._on_anomaly(reason, key)
        except Exception:  # noqa: BLE001
            pass

    def _append_extra_components(self, rnd: Round, event: Any) -> None:
        message_obj = getattr(event, "message_obj", None)
        components = getattr(message_obj, "message", None)
        if not isinstance(components, list):
            return
        for component in components:
            if getattr(component, "type", "") != "plain":
                rnd.extra_components.append(component)

    def _arm(self, rnd: Round) -> None:
        if rnd.timer is not None:
            rnd.timer.cancel()
        now = self._clock()
        remaining = self.max_wait_seconds - (now - rnd.created_at)
        window = float(rnd.wait_seconds or self.debounce_seconds)
        wait = max(0.0, min(window, remaining))
        rnd.timer = asyncio.create_task(self._wait(rnd, wait))

    async def _wait(self, rnd: Round, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        if not rnd.closed:
            rnd.flush.set()

    def _reconstruct(self, rnd: Round) -> None:
        """把合并文本写回 owner 事件；图片等组件追加到消息链。"""
        merged = self.separator.join(rnd.texts).strip()
        event = rnd.owner_event
        try:
            event.message_str = merged
        except Exception:  # noqa: BLE001
            pass
        message_obj = getattr(event, "message_obj", None)
        if message_obj is None:
            return
        try:
            message_obj.message_str = merged
        except Exception:  # noqa: BLE001
            pass
        components = getattr(message_obj, "message", None)
        if isinstance(components, list) and rnd.extra_components:
            message_obj.message = [*components, *rnd.extra_components]

    def _finish(self, key: tuple[str, str], rnd: Round) -> None:
        rnd.closed = True
        if rnd.timer is not None:
            rnd.timer.cancel()
        self._rounds.pop(key, None)

    def _abort(self, key: tuple[str, str], rnd: Round) -> None:
        rnd.closed = True
        if rnd.timer is not None:
            rnd.timer.cancel()
        self._rounds.pop(key, None)

    def _flush_key(self, key: tuple[str, str]) -> None:
        rnd = self._rounds.get(key)
        if rnd is not None and not rnd.closed:
            rnd.flush.set()

    def _flush_oldest(self) -> None:
        if not self._rounds:
            return
        oldest_key = min(self._rounds, key=lambda item: self._rounds[item].created_at)
        self._flush_key(oldest_key)

    @staticmethod
    def _silence(event: Any) -> None:
        stopper = getattr(event, "stop_event", None)
        if callable(stopper):
            try:
                stopper()
            except Exception:  # noqa: BLE001
                pass
