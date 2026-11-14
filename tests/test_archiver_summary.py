"""归档摘要链路单测：整段对话进摘要、结构化回退、写入 summary 记忆。

对应诊断报告 3.3 / 4.1：summarizer 必须被注入，且摘要要吃"整段对话"，
不能只吃"标题 + 最后一句前 120 字"。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_ctm.config import load_config  # noqa: E402
from astrbot_plugin_ctm.ctm.archiver import (  # noqa: E402
    ArchiveJob,
    Archiver,
    build_summary_prompt,
)
from astrbot_plugin_ctm.ctm.memory_store import MemoryStore  # noqa: E402
from astrbot_plugin_ctm.ctm.relationship import RelationshipManager  # noqa: E402
from astrbot_plugin_ctm.ctm.store import Store  # noqa: E402

SESSION = "t:FriendMessage:u1"
TRANSCRIPT = "\n".join(
    [
        "用户：插件会不会跟表情包冲突",
        "助手：两者的入口优先级不同，但都抢 1000",
        "用户：那把 priority 改一下呢",
        "助手：可以，改成 900 就不会互相抢占，文件名是 main.py 第 51 行",
    ]
)


def _job(**overrides) -> ArchiveJob:
    base = dict(
        turn_id="turn-1",
        session_id=SESSION,
        user_text="那把 priority 改一下呢",
        response_text="可以，改成 900 就不会互相抢占",
        topic_action="soft_boundary",
        persona_mode="daily",
        emotional_salience=0.0,
        sticky_hit=False,
        occurred_at=1.0,
        old_topic_id=1,
    )
    base.update(overrides)
    return ArchiveJob(**base)


class SummaryPipelineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "ctm.db"
        self.store = Store(db)
        self.store.init()
        self.cfg = load_config({})
        self.memory = MemoryStore(self.store, self.cfg.memory)
        self.relationship = RelationshipManager(self.store, self.cfg.relationship)
        self.topic_id = self.store.create_topic(SESSION, "表情包插件冲突")

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        self._tmp.cleanup()

    async def test_summarizer_receives_full_transcript(self) -> None:
        seen: list[str] = []

        async def summarizer(prompt: str, session_id: str = "") -> str:
            seen.append(prompt)
            return (
                "在解决什么：CTM 与表情包插件同为 priority 1000，会互相抢占事件。\n"
                "已确认的结论与参数：把 main.py 第 51 行的 priority 改成 900 即可共存。\n"
                "未完成事项：无\n"
                "踩过的坑：拖走事件后插件会完全静默。"
            )

        archiver = Archiver(
            self.store, self.memory, self.relationship, self.cfg, summarizer=summarizer
        )
        await archiver._summarize_topic(  # noqa: SLF001
            _job(old_topic_id=self.topic_id, topic_transcript=TRANSCRIPT)
        )
        self.assertEqual(len(seen), 1)
        prompt = seen[0]
        self.assertIn("表情包插件冲突", prompt)
        self.assertIn("第 51 行", prompt)
        self.assertIn("表情包", prompt)
        topic = self.store.get_topic(self.topic_id)
        self.assertIn("改成 900", topic["summary"])

    async def test_summary_written_as_high_importance_memory(self) -> None:
        async def summarizer(prompt: str, session_id: str = "") -> str:
            return "在解决什么：x\n已确认的结论与参数：y\n未完成事项：无\n踩过的坑：无"

        archiver = Archiver(
            self.store, self.memory, self.relationship, self.cfg, summarizer=summarizer
        )
        await archiver._summarize_topic(  # noqa: SLF001
            _job(old_topic_id=self.topic_id, topic_transcript=TRANSCRIPT)
        )
        rows = self.store.fetch_memories(SESSION, ("summary",), limit=10)
        summaries = list(rows)
        self.assertTrue(summaries, "摘要必须写成 memories(kind=summary)")
        self.assertGreaterEqual(float(summaries[0]["importance"]), 0.8)

    async def test_fallback_summary_without_model(self) -> None:
        archiver = Archiver(self.store, self.memory, self.relationship, self.cfg)
        await archiver._summarize_topic(  # noqa: SLF001
            _job(old_topic_id=self.topic_id, topic_transcript=TRANSCRIPT)
        )
        summary = self.store.get_topic(self.topic_id)["summary"]
        for section in ("在解决什么", "已确认的结论与参数", "未完成事项", "踩过的坑"):
            self.assertIn(section, summary)
        self.assertIn("main.py", summary)

    async def test_model_failure_falls_back(self) -> None:
        async def broken(prompt: str, session_id: str = "") -> str:
            raise RuntimeError("provider 挂了")

        archiver = Archiver(
            self.store, self.memory, self.relationship, self.cfg, summarizer=broken
        )
        summary = await archiver._summarize_topic(  # noqa: SLF001
            _job(old_topic_id=self.topic_id, topic_transcript=TRANSCRIPT)
        )
        self.assertIn("在解决什么", summary)

    async def test_no_old_topic_is_noop(self) -> None:
        archiver = Archiver(self.store, self.memory, self.relationship, self.cfg)
        self.assertIsNone(
            await archiver._summarize_topic(_job(old_topic_id=None))  # noqa: SLF001
        )


class PromptTest(unittest.TestCase):
    def test_prompt_contains_title_and_transcript(self) -> None:
        prompt = build_summary_prompt("标题", "用户：一句话")
        self.assertIn("标题", prompt)
        self.assertIn("用户：一句话", prompt)
        self.assertIn("四段结构", prompt)


class ConfigDefaultsTest(unittest.TestCase):
    """报告 4.2 / 4.3 的阈值调整必须与 schema 一致。"""

    def test_new_defaults(self) -> None:
        import json

        cfg = load_config({})
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        items = schema["topic"]["items"]
        self.assertEqual(cfg.topic.judge_min_message_length, 2)
        self.assertEqual(
            cfg.topic.judge_min_message_length, items["judge_min_message_length"]["default"]
        )
        self.assertEqual(cfg.topic.min_turns_before_switch, 4)
        self.assertEqual(
            cfg.topic.min_turns_before_switch, items["min_turns_before_switch"]["default"]
        )
        self.assertEqual(cfg.memory.assertion_confidence_threshold, 0.55)
        self.assertEqual(
            cfg.memory.assertion_confidence_threshold,
            schema["memory"]["items"]["assertion_confidence_threshold"]["default"],
        )
        self.assertTrue(cfg.topic.aggregation_adaptive_enabled)
        self.assertEqual(
            cfg.topic.aggregation_adaptive_min_seconds,
            items["aggregation_adaptive_min_seconds"]["default"],
        )
        self.assertEqual(
            cfg.topic.aggregation_adaptive_max_seconds,
            items["aggregation_adaptive_max_seconds"]["default"],
        )


if __name__ == "__main__":
    unittest.main()
