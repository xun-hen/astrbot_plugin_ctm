"""CTM 核心单测：不加载 AstrBot，纯标准库 + 临时 SQLite。"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_ctm.config import load_config  # noqa: E402
from astrbot_plugin_ctm.ctm.archiver import ArchiveJob, Archiver  # noqa: E402
from astrbot_plugin_ctm.ctm.cognition import Cognition  # noqa: E402
from astrbot_plugin_ctm.ctm.enums import PersonaMode, TopicAction  # noqa: E402
from astrbot_plugin_ctm.ctm.injection import InjectionBuilder  # noqa: E402
from astrbot_plugin_ctm.ctm.longterm.config import LongTermConfig  # noqa: E402
from astrbot_plugin_ctm.ctm.longterm.extractor import is_sensitive, parse_candidates  # noqa: E402
from astrbot_plugin_ctm.ctm.longterm.manager import LongTermMemory  # noqa: E402
from astrbot_plugin_ctm.ctm.longterm.store import LongTermStore  # noqa: E402
from astrbot_plugin_ctm.ctm.memory_store import MemoryStore  # noqa: E402
from astrbot_plugin_ctm.ctm.perception import Perception  # noqa: E402
from astrbot_plugin_ctm.ctm.relationship import RelationshipManager  # noqa: E402
from astrbot_plugin_ctm.ctm.store import Store  # noqa: E402
from astrbot_plugin_ctm.ctm.textsim import jaccard, tokenize, topic_similarity  # noqa: E402
from astrbot_plugin_ctm.ctm.topic_state import TopicStateMachine  # noqa: E402


class FakeSender:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id


class FakeMessage:
    message_id = "m1"


class FakeEvent:
    def __init__(
        self,
        text: str = "你好",
        *,
        umo: str = "test:friend:10001",
        sender: str = "10001",
        group: str = "",
    ) -> None:
        self.unified_msg_origin = umo
        self.message_str = text
        self.message_obj = FakeMessage()
        self._sender = sender
        self._group = group

    def get_sender_id(self) -> str:
        return self._sender

    def get_group_id(self) -> str:
        return self._group

    def get_platform_id(self) -> str:
        return "test"

    def is_admin(self) -> bool:
        return True


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.completion_text = text


class FakeProvider:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    async def text_chat(self, **kwargs):
        self.calls += 1
        return FakeResponse(self.payload)


def _tmp_db() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    return tmp, Path(tmp.name) / "ctm.db"


class ConfigTest(unittest.TestCase):
    def test_defaults_match_schema(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        cfg = load_config({})

        def default_of(group: str, key: str):
            return schema[group]["items"][key].get("default")

        self.assertEqual(cfg.enabled, default_of("basic", "enabled"))
        self.assertEqual(cfg.longterm.enabled, default_of("longterm", "enabled"))
        self.assertEqual(
            cfg.longterm.extract_every_n_turns,
            schema["longterm"]["items"]["extract"]["items"]["every_n_turns"]["default"],
        )
        self.assertEqual(
            cfg.memory.injection_budget_tokens,
            default_of("memory", "injection_budget_tokens"),
        )
        self.assertAlmostEqual(cfg.memory.weights.total(), 1.0, places=6)
        for key in (
            "aggregation_enabled",
            "aggregation_debounce_seconds",
            "judge_enabled",
            "judge_min_confidence",
            "judge_time_gap_minutes",
            "detect_in_hook_enabled",
        ):
            self.assertEqual(
                getattr(cfg.topic, key),
                schema["topic"]["items"][key]["default"],
                f"topic.{key} 与 schema 默认值不一致",
            )

    def test_invalid_values_fall_back(self) -> None:
        cfg = load_config(
            {
                "basic": {"enabled": "yes"},
                "topic": {"auto_switch_strategy": "nope", "topic_threshold": -3},
                "memory": {"injection_budget_tokens": 10**9},
            }
        )
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.topic.auto_switch_strategy, "soft")
        self.assertEqual(cfg.topic.topic_threshold, 0.0)
        self.assertLessEqual(cfg.memory.injection_budget_tokens, 8000)


class TextSimTest(unittest.TestCase):
    def test_similarity(self) -> None:
        self.assertGreater(
            topic_similarity("周末去杭州出差", "下周去杭州"), topic_similarity("周末去杭州出差", "今天吃什么")
        )
        self.assertGreater(jaccard(tokenize("杭州出差"), tokenize("杭州 出差")), 0.0)


class StoreTest(unittest.TestCase):
    def test_init_and_topics(self) -> None:
        tmp, path = _tmp_db()
        store = Store(path)
        try:
            store.init()
            store.ensure_session("s1", "u1")
            topic_id = store.create_topic("s1", "聊工作")
            store.save_topic_stack("s1", [topic_id])
            stack = store.load_topic_stack("s1")
            self.assertEqual(len(stack), 1)
            self.assertEqual(stack[0]["title"], "聊工作")
            self.assertTrue(store.health()["ok"])
            store.insert_memory(
                session_id="s1",
                memory_type="assertion",
                content="用户喜欢猫",
                keywords=["猫"],
                confidence=0.9,
            )
            rows = store.fetch_memories("s1", ["assertion"])
            self.assertEqual(rows[0]["content"], "用户喜欢猫")
        finally:
            store.close()
            tmp.cleanup()


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.path = _tmp_db()
        self.store = Store(self.path)
        self.store.init()
        self.cfg = load_config({})
        self.perception = Perception(self.cfg.topic)
        self.topic_sm = TopicStateMachine(self.store, self.cfg.topic)
        self.memory = MemoryStore(self.store, self.cfg.memory)
        self.relationship = RelationshipManager(self.store, self.cfg.relationship)
        self.builder = InjectionBuilder(self.cfg)
        self.cognition = Cognition(self.cfg, self.builder, self.topic_sm)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_soft_boundary_and_decision(self) -> None:
        event = FakeEvent("我想聊聊工作")
        signal = self.perception.extract(event)
        state = self.topic_sm.load(signal.session_id)
        state.turns_since_switch = 10
        self.topic_sm.soft_boundary(signal, state, "工作")
        self.assertEqual(state.current_topic, "工作")
        self.assertTrue(state.soft_boundary_pending)
        event2 = FakeEvent("另外我想问问周末去杭州的行程")
        signal2 = self.perception.extract(event2)
        decision = self.cognition.decide(
            signal2, state, [], self.relationship.load(signal.session_id)
        )
        self.assertIn(decision.topic_action, {TopicAction.SOFT_BOUNDARY, TopicAction.CONTINUE})
        self.assertIn(decision.persona_mode, set(PersonaMode))

    def test_memory_recall_and_injection(self) -> None:
        signal = self.perception.extract(FakeEvent("你好"))
        self.memory.save_assertion(signal.session_id, "用户喜欢喝美式咖啡", 0.9, ["咖啡"], "t1")
        state = self.topic_sm.load(signal.session_id)
        candidates = self.memory.recall_candidates(signal, state)
        self.assertTrue(candidates)
        decision = self.cognition.decide(
            signal, state, candidates, self.relationship.load(signal.session_id)
        )
        payload = self.builder.build(decision)
        self.assertIn("记忆包", payload)
        self.assertIn("话题状态", payload)


class ArchiverTest(unittest.IsolatedAsyncioTestCase):
    async def test_archive_retry_and_failure_record(self) -> None:
        tmp, path = _tmp_db()
        store = Store(path)
        store.init()
        cfg = load_config({})
        memory = MemoryStore(store, cfg.memory)
        relationship = RelationshipManager(store, cfg.relationship)
        archiver = Archiver(store, memory, relationship, cfg)
        archiver.start()
        try:
            job = ArchiveJob(
                turn_id="turn1",
                session_id="s1",
                user_text="我今天很难过",
                response_text="抱抱",
                topic_action="continue",
                persona_mode="comfort",
                emotional_salience=-0.8,
                sticky_hit=True,
                occurred_at=0.0,
            )
            self.assertTrue(await archiver.enqueue(job))
            await asyncio.sleep(0.2)
            rows = store.fetch_memories("s1", ["episode"])
            self.assertTrue(rows)
            logs = store.list_turn_logs("s1")
            self.assertTrue(logs)
        finally:
            await archiver.stop(1.0)
            store.close()
            tmp.cleanup()


class LongTermTest(unittest.IsolatedAsyncioTestCase):
    async def test_parse_and_sensitive(self) -> None:
        raw = '```json\n[{"scope":"user","kind":"profile","slot_key":"name","content":"小明","importance":0.9,"confidence":0.9}]\n```'
        candidates = parse_candidates(raw, 5)
        self.assertEqual(candidates[0].slot_key, "name")
        self.assertTrue(is_sensitive("我的密码是 abc12345"))
        self.assertFalse(is_sensitive("我喜欢猫"))

    async def test_save_recall_forget(self) -> None:
        tmp, path = _tmp_db()
        store = LongTermStore(path)
        store.init()
        cfg = LongTermConfig()
        provider = FakeProvider("[]")
        manager = LongTermMemory(store, cfg, lambda _session: _async(provider))
        await manager.start()
        try:
            item = manager.save(
                session_id="test:friend:10001",
                platform_id="test",
                sender_id="10001",
                group_id="",
                is_group=False,
                content="用户称呼是小明",
                kind="profile",
                slot_key="name",
            )
            self.assertIsNotNone(item)
            text = await manager.recall(
                session_id="test:friend:10001",
                platform_id="test",
                sender_id="10001",
                group_id="",
                is_group=False,
                query="我叫什么",
            )
            self.assertIn("小明", text)
            deleted = manager.forget(
                session_id="test:friend:10001",
                platform_id="test",
                sender_id="10001",
                group_id="",
                is_group=False,
                query="小明",
            )
            self.assertGreaterEqual(deleted, 1)
        finally:
            await manager.close()
            store.close()
            tmp.cleanup()

    async def test_extraction_worker(self) -> None:
        tmp, path = _tmp_db()
        store = LongTermStore(path)
        store.init()
        cfg = LongTermConfig(extract_every_n_turns=1, extract_min_chars=2)
        provider = FakeProvider(
            '[{"scope":"user","kind":"profile","slot_key":"city","content":"住在杭州",'
            '"importance":0.8,"confidence":0.9}]'
        )
        manager = LongTermMemory(store, cfg, lambda _session: _async(provider))
        await manager.start()
        try:
            manager.observe_turn(
                session_id="test:friend:10001",
                platform_id="test",
                sender_id="10001",
                group_id="",
                is_group=False,
                user_text="我住在杭州",
                assistant_text="好的，记住了",
            )
            await asyncio.sleep(0.4)
            rows = store.list_scope("user", "test:10001")
            self.assertTrue(any("杭州" in row["content"] for row in rows))
            self.assertGreaterEqual(provider.calls, 1)
        finally:
            await manager.close()
            store.close()
            tmp.cleanup()


async def _async(value):
    return value


if __name__ == "__main__":
    unittest.main()
