"""消息聚合与话题判定的单测（纯标准库，不加载 AstrBot）。"""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_ctm.ctm.aggregation import Aggregator  # noqa: E402
from astrbot_plugin_ctm.ctm.topic_judge import (  # noqa: E402
    TopicJudge,
    heuristic,
)
from astrbot_plugin_ctm.ctm.topic_state import TopicNode, TopicState  # noqa: E402


class FakeEvent:
    def __init__(self, text: str, *, components: list | None = None) -> None:
        self.message_str = text
        self.message_obj = SimpleNamespace(
            message=components if components is not None else [SimpleNamespace(type="plain")]
        )
        self.stopped = False

    def stop_event(self) -> None:
        self.stopped = True


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.completion_text = text


class FakeProvider:
    def __init__(self, payload: str | None = None, *, error: bool = False) -> None:
        self.payload = payload
        self.error = error
        self.calls = 0

    async def text_chat(self, **_kwargs):
        self.calls += 1
        if self.error:
            raise RuntimeError("boom")
        return FakeResponse(self.payload or "{}")


def make_state(title: str = "电路板设计") -> TopicState:
    now = time.time()
    return TopicState(
        session_id="s1",
        stack=[TopicNode(topic_id=1, title=title, created_at=now, last_active_at=now)],
        current_topic=title,
    )


class AggregationTest(unittest.IsolatedAsyncioTestCase):
    async def test_merges_consecutive_messages(self) -> None:
        aggregator = Aggregator(
            enabled=True,
            debounce_seconds=0.05,
            max_wait_seconds=0.5,
        )
        key = ("s1", "u1")
        owner = FakeEvent("帮我看看 NAS", components=[SimpleNamespace(type="plain")])
        follower = FakeEvent(
            "好像有点响",
            components=[SimpleNamespace(type="plain"), SimpleNamespace(type="image")],
        )
        owner_task = asyncio.create_task(
            aggregator.accept(owner, key=key, text="帮我看看 NAS")
        )
        await asyncio.sleep(0.01)
        proceed = await aggregator.accept(follower, key=key, text="好像有点响")
        self.assertFalse(proceed)
        self.assertTrue(follower.stopped)
        self.assertTrue(await owner_task)
        self.assertIn("帮我看看 NAS", owner.message_str)
        self.assertIn("好像有点响", owner.message_str)
        self.assertTrue(
            any(getattr(item, "type", "") == "image" for item in owner.message_obj.message)
        )

    async def test_disabled_passes_through(self) -> None:
        aggregator = Aggregator(enabled=False)
        event = FakeEvent("hi")
        self.assertTrue(
            await aggregator.accept(event, key=("s1", "u1"), text="hi")
        )
        self.assertEqual(event.message_str, "hi")

    async def test_command_flushes_and_passes(self) -> None:
        aggregator = Aggregator(enabled=True, debounce_seconds=5.0)
        event = FakeEvent("/ctm status")
        started = time.monotonic()
        self.assertTrue(
            await aggregator.accept(
                event, key=("s1", "u1"), text="/ctm status", is_command=True
            )
        )
        self.assertLess(time.monotonic() - started, 0.5)


class TopicJudgeHeuristicTest(unittest.TestCase):
    def test_high_similarity_continue(self) -> None:
        state = make_state("电路板设计")
        result = heuristic("电路板设计", state, sim_continue=0.5)
        self.assertFalse(result.needs_llm)
        self.assertEqual(result.verdict, "continue")

    def test_explicit_new_topic(self) -> None:
        state = make_state("电路板设计")
        result = heuristic("换个话题，说说 NAS 硬盘扩容", state)
        self.assertEqual(result.verdict, "new_topic")
        self.assertFalse(result.needs_llm)

    def test_short_message_continue(self) -> None:
        state = make_state("电路板设计")
        result = heuristic("嗯", state)
        self.assertEqual(result.verdict, "continue")
        self.assertFalse(result.needs_llm)

    def test_short_low_similarity_asks_llm(self) -> None:
        state = make_state("电路板设计")
        result = heuristic("那这样做可以吗", state)
        self.assertTrue(result.needs_llm)


class TopicJudgeLLMTest(unittest.IsolatedAsyncioTestCase):
    async def _judge(self, provider, message: str, title: str = "电路板设计"):
        async def get_provider(_session_id):
            return provider

        judge = TopicJudge(provider_getter=get_provider, min_new_confidence=0.6)
        return await judge.judge(
            session_id="s1",
            message=message,
            state=make_state(title),
        )

    async def test_llm_new_topic(self) -> None:
        provider = FakeProvider(
            '{"verdict":"new_topic","confidence":0.95,"topic_name":"NAS 扩容","reason":"独立的事"}'
        )
        verdict = await self._judge(provider, "那这样做可以吗")
        self.assertTrue(verdict.is_new_topic)
        self.assertEqual(verdict.source, "llm")
        self.assertEqual(provider.calls, 1)

    async def test_llm_failure_continues(self) -> None:
        provider = FakeProvider(error=True)
        verdict = await self._judge(provider, "那这样做可以吗")
        self.assertFalse(verdict.is_new_topic)
        self.assertEqual(verdict.source, "heuristic")

    async def test_threshold_rejects_low_confidence(self) -> None:
        provider = FakeProvider(
            '{"verdict":"new_topic","confidence":0.4,"reason":"不确定"}'
        )
        verdict = await self._judge(provider, "那这样做可以吗")
        self.assertFalse(verdict.is_new_topic)

    async def test_llm_not_called_for_clear_continue(self) -> None:
        provider = FakeProvider('{"verdict":"new_topic","confidence":0.99}')
        verdict = await self._judge(provider, "电路板设计")
        self.assertFalse(verdict.is_new_topic)
        self.assertEqual(provider.calls, 0)


if __name__ == "__main__":
    unittest.main()
