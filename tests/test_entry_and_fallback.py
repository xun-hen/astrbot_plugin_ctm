"""入口可观测性与兜底判定的单测（需要 AstrBot 运行时，缺则跳过）。

覆盖 v0.4.0 故障报告 §5 提出的四点修补：
1. 入口必有 INFO 日志（`INFO_ENTRY`）；
2. 四个静默 return 各留一条 `DEBUG_SKIP` 带 reason；
3. 无话题栈时兜底仍能产出决策（不再死锁）；
4. `on_llm_request` 不再因 `diag.decisions == 0` 而不留痕。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

try:  # noqa: SIM105
    import astrbot  # noqa: F401

    HAS_ASTRBOT = True
except Exception:  # noqa: BLE001
    HAS_ASTRBOT = False


class _FakeContext:
    conversation_manager = None
    provider_manager = None

    def __init__(self, plugin_set=None) -> None:
        self._plugin_set = plugin_set

    def get_config(self, umo=None):
        if self._plugin_set is None:
            return None
        return {"plugin_set": self._plugin_set}

    def register_web_api(self, *args, **kwargs):
        return None

    def get_using_provider(self, *args, **kwargs):
        return None


class _FakeMessage:
    message_id = "t1"
    message: list = []


class _FakeEvent:
    plugins_name = None

    def __init__(self, text: str = "今天先聊工作安排", umo: str = "t:FriendMessage:u1"):
        self.unified_msg_origin = umo
        self.message_str = text
        self.message_obj = _FakeMessage()
        self._extras: dict = {}
        self.stopped = False

    def get_sender_id(self) -> str:
        return "u1"

    def get_group_id(self) -> str:
        return ""

    def get_platform_id(self) -> str:
        return "t"

    def is_admin(self) -> bool:
        return True

    def set_extra(self, key, value) -> None:
        self._extras[key] = value

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def stop_event(self) -> None:
        self.stopped = True

    def is_stopped(self) -> bool:
        return self.stopped


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.completion_text = text


class _FakeRequest:
    def __init__(self) -> None:
        self.system_prompt = "你是助手。"
        self.prompt = "今天先聊工作安排"
        self.extra_user_content_parts: list = []


class _StubJudge:
    """话题判定替身：固定返回"延续"。"""

    def __init__(self, verdict=None, raises: bool = False) -> None:
        self._verdict = verdict or SimpleNamespace(is_new_topic=False, confidence=0.1)
        self._raises = raises

    async def judge(self, **_kwargs):
        if self._raises:
            raise RuntimeError("judge boom")
        return self._verdict


@unittest.skipUnless(HAS_ASTRBOT, "AstrBot runtime not installed")
class EntryObservabilityTest(unittest.IsolatedAsyncioTestCase):
    def _config(self, *, enabled=True, mode="hint", db_path="", topic=None):
        topic_cfg = {
            "mode": mode,
            "aggregation_enabled": False,
            "judge_enabled": False,
            "debug_mode": False,
        }
        topic_cfg.update(topic or {})
        return {
            "basic": {"enabled": enabled, "db_path": db_path},
            "topic": topic_cfg,
            "longterm": {"enabled": False},
        }

    async def asyncSetUp(self) -> None:
        import astrbot_plugin_ctm.main as plugin_main

        self.module = plugin_main
        self.captured: list[tuple[str, dict]] = []
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = str(Path(self._tmp.name) / "ctm.db")
        self.plugin = plugin_main.CtmPlugin(
            _FakeContext(), self._config(db_path=self.db_path)
        )
        self._plugins = [self.plugin]

    async def asyncTearDown(self) -> None:
        for plugin in self._plugins:
            try:
                plugin.store.close()
            except Exception:  # noqa: BLE001
                pass
        self._tmp.cleanup()

    def arm(self, plugin) -> None:
        """只建表 + 装上日志采集器（不启归档/长期记忆 worker，保持测试快）。"""
        from astrbot_plugin_ctm.ctm import observability

        plugin.store.init()
        observability.configure(
            lambda level, fields: self.captured.append((level, dict(fields)))
        )

    def codes(self) -> list[str]:
        return [fields.get("code", "") for _level, fields in self.captured]

    def skips(self) -> list[str]:
        return [
            fields.get("reason", "")
            for _level, fields in self.captured
            if fields.get("code") == "DEBUG_SKIP"
        ]

    async def test_entry_logged_and_state_written(self) -> None:
        self.arm(self.plugin)
        await self.plugin.on_message(_FakeEvent())
        self.assertIn("INFO_ENTRY", self.codes())
        self.assertIn("INFO_DECISION", self.codes())
        self.assertEqual(self.plugin.diag.entries, 1)
        self.assertEqual(self.plugin.diag.decisions, 1)
        rows = (
            self.plugin.store._require()  # noqa: SLF001
            .execute("SELECT COUNT(*) FROM sessions")
            .fetchone()[0]
        )
        self.assertEqual(rows, 1, "on_message 进入主体后必须写 sessions")

    async def test_disabled_still_logs_entry_and_reason(self) -> None:
        import astrbot_plugin_ctm.main as plugin_main

        plugin = plugin_main.CtmPlugin(
            _FakeContext(), self._config(enabled=False, db_path=self.db_path)
        )
        self._plugins.append(plugin)
        self.arm(plugin)
        await plugin.on_message(_FakeEvent())
        self.assertEqual(plugin.diag.entries, 1)
        self.assertIn("disabled", self.skips())

    async def test_mode_off_skip_reason(self) -> None:
        import astrbot_plugin_ctm.main as plugin_main

        plugin = plugin_main.CtmPlugin(
            _FakeContext(), self._config(mode="off", db_path=self.db_path)
        )
        self._plugins.append(plugin)
        self.arm(plugin)
        await plugin.on_message(_FakeEvent())
        self.assertIn("mode_off", self.skips())

    async def test_empty_text_skip_reason(self) -> None:
        self.arm(self.plugin)
        await self.plugin.on_message(_FakeEvent(text="   "))
        self.assertIn("empty_text", self.skips())

    async def test_command_skip_reason(self) -> None:
        self.arm(self.plugin)
        await self.plugin.on_message(_FakeEvent(text="/ctm status"))
        self.assertIn("command", self.skips())

    async def test_fallback_works_without_topic_stack(self) -> None:
        """v0.4.0 的死锁：入口被抢占 → 栈永远为空 → 兜底永远 None。"""
        self.arm(self.plugin)
        self.plugin.topic_judge = _StubJudge()
        decision = await self.plugin._fallback_decision(_FakeEvent())  # noqa: SLF001
        self.assertIsNotNone(decision, "空栈时兜底也必须产出决策")
        self.assertEqual(self.plugin._fallback_reason, "empty_stack_recovered")  # noqa: SLF001
        self.assertIn("WARN_PREEMPTED", self.codes())
        self.assertEqual(
            self.plugin.topic_sm.load("t:FriendMessage:u1").stack,
            [],
            "兜底只注入，不允许落话题状态",
        )

    async def test_no_decision_logged_even_when_decisions_is_zero(self) -> None:
        """旧代码 `if self.diag.decisions:` 门禁会吞掉这条最需要的日志。"""
        self.arm(self.plugin)
        self.plugin.topic_judge = _StubJudge(raises=True)
        req = _FakeRequest()
        await self.plugin.on_llm_request(_FakeEvent(), req)
        self.assertEqual(self.plugin.diag.decisions, 0)
        self.assertIn("WARN_NO_DECISION", self.codes())
        no_decision = [
            fields
            for _level, fields in self.captured
            if fields.get("code") == "WARN_NO_DECISION"
        ]
        self.assertTrue(no_decision and no_decision[0].get("reason", "").startswith("decide_error"))

    async def test_summarizer_is_injected_into_archiver(self) -> None:
        """报告 3.3：main.py 必须把 summarizer 传给 Archiver，否则摘要永远是标题+120 字。"""
        self.assertIsNotNone(
            getattr(self.plugin.archiver, "_summarizer", None),
            "Archiver 必须拿到 summarizer（否则只会写规则回退摘要）",
        )

    async def test_transcript_buffer_collects_turns(self) -> None:
        self.arm(self.plugin)
        self.plugin._remember_turn("s1", "用户第一句", "助手第一句")
        self.plugin._remember_turn("s1", "用户第二句", "助手第二句")
        text = self.plugin._transcript("s1")
        self.assertIn("用户：用户第一句", text)
        self.assertIn("助手：助手第二句", text)
        self.assertLessEqual(len(text), 4000)

    async def test_manual_new_topic_marks_old_topic_for_archive(self) -> None:
        self.arm(self.plugin)
        event = _FakeEvent(umo="s2")
        self.plugin._remember_turn("s2", "旧话题内容", "旧话题回复")
        await self.plugin.on_message(_FakeEvent(text="先建一个话题", umo="s2"))
        await self.plugin.manual_new_topic(_FakeEvent(umo="s2"), "新话题")
        self.assertIsNotNone(self.plugin._pending_old_topic.get("s2"))
        self.assertNotIn("s2", self.plugin._turn_buffer, "硬切后轨迹应重置")

    async def test_soft_switch_produces_topic_summary(self) -> None:
        """诊断报告 4.1 / 五：软切后 topics.summary 与 memories(summary) 必须有内容。"""
        import astrbot_plugin_ctm.main as plugin_main

        umo = "s3:FriendMessage:u1"
        plugin = plugin_main.CtmPlugin(
            _FakeContext(),
            self._config(
                db_path=self.db_path,
                topic={"min_turns_before_switch": 1, "judge_min_message_length": 2},
            ),
        )
        self._plugins.append(plugin)
        self.arm(plugin)
        plugin.archiver.start()
        try:
            # 第 1 轮：建立话题，并把这一轮记进轨迹缓冲
            first = _FakeEvent(text="表情包插件会跟对话中枢冲突吗", umo=umo)
            await plugin.on_message(first)
            await plugin.on_llm_response(
                first,
                _FakeResponse("两者都抢 priority 1000，把 main.py 第 51 行改成 900 即可共存"),
            )
            # 第 2 轮：显式换话题 → 软切，旧话题进归档
            second = _FakeEvent(text="换个话题，聊聊周末去杭州的行程", umo=umo)
            await plugin.on_message(second)
            await plugin.on_llm_response(second, _FakeResponse("好，先定高铁票"))
            await asyncio.sleep(0.6)

            topics = plugin.store.recent_topics(20)
            archived = [row for row in topics if row.get("summary")]
            self.assertTrue(archived, "切分后旧话题必须有摘要")
            self.assertIn("priority", archived[0]["summary"])
            summaries = plugin.store.fetch_memories(umo, ("summary",), limit=10)
            self.assertTrue(summaries, "摘要必须同时写成 memories(summary)")
        finally:
            await plugin.archiver.stop(2.0)

    async def test_llm_request_logged(self) -> None:
        self.arm(self.plugin)
        await self.plugin.on_llm_request(_FakeEvent(), _FakeRequest())
        self.assertIn("DEBUG_LLM_REQUEST", self.codes())
        self.assertEqual(self.plugin.diag.llm_requests, 1)


@unittest.skipUnless(HAS_ASTRBOT, "AstrBot runtime not installed")
class PluginSetSelfCheckTest(unittest.TestCase):
    """`plugin_set` 白名单是第二个静默门禁（真机现场）。"""

    def _state(self, plugin_set):
        import astrbot_plugin_ctm.main as plugin_main

        plugin = plugin_main.CtmPlugin(_FakeContext(plugin_set), {})
        return plugin._verify_plugin_set()  # noqa: SLF001

    def test_star_means_all_enabled(self) -> None:
        self.assertEqual(self._state(["*"]), "ok")

    def test_listed_plugin_is_ok(self) -> None:
        self.assertEqual(self._state(["other", "astrbot_plugin_ctm"]), "ok")

    def test_missing_plugin_is_not_listed(self) -> None:
        self.assertEqual(self._state(["other", "another"]), "not_listed")

    def test_empty_list_blocks_everything(self) -> None:
        self.assertEqual(self._state([]), "empty")

    def test_no_context_config_is_unknown(self) -> None:
        self.assertEqual(self._state(None), "unknown")


if __name__ == "__main__":
    unittest.main()
