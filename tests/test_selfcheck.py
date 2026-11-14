"""自检模块单测：纯标准库（不加载 AstrBot，用替身拼出各种失效现场）。

覆盖四道静默门禁：全局白名单、handler 绑定、入口心跳、数据库。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_ctm.ctm import selfcheck  # noqa: E402


class _Diag:
    def __init__(self, **kwargs) -> None:
        self.entries = 0
        self.llm_requests = 0
        self.last_entry_at = 0.0
        self.last_decision_at = 0.0
        self.aggregation_anomalies = 0
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Store:
    def __init__(self, ok: bool = True) -> None:
        self._ok = ok

    def health(self):
        if self._ok:
            return {
                "ok": True,
                "path": "/tmp/ctm.sqlite3",
                "size_bytes": 4096,
                "tables": 11,
            }
        return {"ok": False, "error": "unable to open database file", "path": "/x"}


class _Plugin:
    """只暴露自检用到的属性。"""

    def __init__(
        self,
        *,
        entries: int = 1,
        llm_requests: int = 1,
        plugin_set: str = "ok",
        binding: dict | None = None,
        store_ok: bool = True,
        uptime: float = 3600.0,
        aggregation: bool = True,
        anomalies: int = 0,
    ) -> None:
        self.diag = _Diag(
            entries=entries,
            llm_requests=llm_requests,
            last_entry_at=time.time() - 30 if entries else 0.0,
            aggregation_anomalies=anomalies,
        )
        self._plugin_set_state = plugin_set
        self._handler_binding = (
            {name: "ok" for name in ("ctm_on_event", "ctm_on_llm_request")}
            if binding is None
            else binding
        )
        self.store = _Store(store_ok)
        self.cfg = SimpleNamespace(
            topic=SimpleNamespace(
                aggregation_enabled=aggregation,
                aggregation_debounce_seconds=2.0,
                aggregation_max_wait_seconds=12.0,
                aggregation_adaptive_enabled=True,
                aggregation_adaptive_min_seconds=0.8,
                aggregation_adaptive_max_seconds=6.0,
            )
        )
        self._started_at = time.time() - uptime
        self._topic_disabled: set[str] = set()


def _by_id(result: dict) -> dict[str, dict]:
    return {item["id"]: item for item in result["items"]}


class RunTest(unittest.TestCase):
    def test_healthy_plugin_passes(self) -> None:
        result = selfcheck.run(_Plugin())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["summary"]["error"], 0)
        self.assertNotIn(
            "error", {item["level"] for item in result["items"]}, result["items"]
        )

    def test_plugin_set_missing_is_error(self) -> None:
        result = selfcheck.run(_Plugin(plugin_set="not_listed"))
        self.assertFalse(result["ok"])
        item = _by_id(result)["plugin_set"]
        self.assertEqual(item["level"], "error")
        self.assertIn("plugin_set", item["detail"])
        self.assertIn("配置", item["fix"])

    def test_plugin_set_empty_list_is_error(self) -> None:
        item = _by_id(selfcheck.run(_Plugin(plugin_set="empty")))["plugin_set"]
        self.assertEqual(item["level"], "error")

    def test_dispatch_preempted_detected(self) -> None:
        """请求钩子在跑、消息入口 0 次 = 派发被拦，这是最典型的静默现场。"""
        result = selfcheck.run(_Plugin(entries=0, llm_requests=5))
        self.assertFalse(result["ok"])
        item = _by_id(result)["dispatch"]
        self.assertEqual(item["level"], "error")
        self.assertIn("拦截", item["title"])

    def test_dispatch_idle_short_is_info(self) -> None:
        item = _by_id(
            selfcheck.run(_Plugin(entries=0, llm_requests=0, uptime=5.0))
        )["dispatch"]
        self.assertEqual(item["level"], "info")

    def test_dispatch_idle_long_is_warn(self) -> None:
        item = _by_id(
            selfcheck.run(_Plugin(entries=0, llm_requests=0, uptime=7200.0))
        )["dispatch"]
        self.assertEqual(item["level"], "warn")

    def test_unbound_handler_is_error(self) -> None:
        result = selfcheck.run(
            _Plugin(binding={"ctm_on_event": "unbound", "ctm_on_llm_request": "ok"})
        )
        self.assertFalse(result["ok"])
        item = _by_id(result)["handlers"]
        self.assertEqual(item["level"], "error")
        self.assertIn("ctm_on_event=unbound", item["detail"])
        self.assertIn("self", item["fix"])

    def test_store_failure_is_error(self) -> None:
        item = _by_id(selfcheck.run(_Plugin(store_ok=False)))["store"]
        self.assertEqual(item["level"], "error")

    def test_aggregation_anomalies_is_warn(self) -> None:
        item = _by_id(selfcheck.run(_Plugin(anomalies=3)))["aggregation"]
        self.assertEqual(item["level"], "warn")

    def test_session_disabled_is_warn(self) -> None:
        plugin = _Plugin()
        plugin._topic_disabled = {"t:Friend:u1"}  # noqa: SLF001
        result = selfcheck.run(plugin, "t:Friend:u1")
        item = _by_id(result)["session"]
        self.assertEqual(item["level"], "warn")

    def test_broken_plugin_does_not_raise(self) -> None:
        class Boom:
            pass

        result = selfcheck.run(Boom())
        self.assertIsInstance(result["items"], list)
        self.assertGreaterEqual(len(result["items"]), 1)


class FormatTest(unittest.TestCase):
    def test_error_lines_and_fix(self) -> None:
        text = selfcheck.format_text(selfcheck.run(_Plugin(plugin_set="not_listed")))
        self.assertIn("[XX]", text)
        self.assertIn("→", text)
        self.assertIn("阻断", text)

    def test_healthy_says_no_action(self) -> None:
        text = selfcheck.format_text(selfcheck.run(_Plugin()))
        self.assertIn("链路正常", text)

    def test_version_present(self) -> None:
        self.assertTrue(selfcheck.read_plugin_version())


if __name__ == "__main__":
    unittest.main()
