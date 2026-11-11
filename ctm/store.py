"""SQLite 持久层：整个插件唯一的 SQL 出入口（纯标准库）。

线程模型：单连接 + `threading.Lock` 串行化；写集中在 Archiver 单 worker，
读是短查询。时间统一 Unix 秒。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Sequence

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).with_name("sql") / "001_init.sql"


def dumps(value: Any) -> str:
    """紧凑 JSON 序列化（中文不转义）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(text: str | None, default: Any) -> Any:
    """容错 JSON 反序列化。"""
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


class Store:
    """SQLite 门面。"""

    def __init__(self, db_path: Path) -> None:
        """初始化（不连接）。

        Args:
            db_path: 数据库绝对路径。
        """
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ 生命周期
    def init(self) -> None:
        """建立连接并应用迁移。"""
        with self._lock:
            if self._conn is not None:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._conn = conn
            self.migrate()

    def migrate(self) -> None:
        """按 `PRAGMA user_version` 应用 DDL。"""
        conn = self._require()
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise sqlite3.Error(
                f"database schema {current} is newer than code {SCHEMA_VERSION}"
            )
        if current == SCHEMA_VERSION:
            return
        script = SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()

    def close(self) -> None:
        """checkpoint 后关闭连接；幂等。"""
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()
            self._conn = None

    def checkpoint(self) -> None:
        """手动 WAL checkpoint。"""
        with self._lock:
            try:
                self._require().execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass

    def health(self) -> dict[str, Any]:
        """健康检查。"""
        with self._lock:
            if self._conn is None:
                return {"ok": False, "error": "not_initialized", "path": str(self._path)}
            try:
                self._conn.execute("SELECT 1").fetchone()
                tables = self._conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
                size = self._path.stat().st_size if self._path.exists() else 0
                return {
                    "ok": True,
                    "path": str(self._path),
                    "size_bytes": size,
                    "tables": int(tables),
                }
            except sqlite3.Error as exc:
                return {"ok": False, "error": str(exc), "path": str(self._path)}

    # ------------------------------------------------------------------ sessions
    def ensure_session(self, session_id: str, user_id: str = "") -> None:
        """确保会话行存在（幂等）。"""
        now = self._now()
        with self._lock:
            conn = self._require()
            conn.execute(
                """
                INSERT INTO sessions(session_id, user_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (session_id, user_id, now, now),
            )
            conn.commit()

    def load_session_state(self, session_id: str) -> dict[str, Any]:
        """读取会话级话题状态。"""
        with self._lock:
            row = self._require().execute(
                "SELECT * FROM session_state WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return {
                "turns_since_switch": 0,
                "last_switch_at": 0.0,
                "soft_boundary_pending": False,
            }
        data = self._row_to_dict(row)
        data["soft_boundary_pending"] = bool(data.get("soft_boundary_pending"))
        return data

    def save_session_state(
        self,
        session_id: str,
        *,
        turns_since_switch: int,
        last_switch_at: float,
        soft_boundary_pending: bool,
    ) -> None:
        """写入会话级话题状态（UPSERT）。"""
        with self._lock:
            conn = self._require()
            conn.execute(
                """
                INSERT INTO session_state(
                  session_id, turns_since_switch, last_switch_at,
                  soft_boundary_pending, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  turns_since_switch=excluded.turns_since_switch,
                  last_switch_at=excluded.last_switch_at,
                  soft_boundary_pending=excluded.soft_boundary_pending,
                  updated_at=excluded.updated_at
                """,
                (
                    session_id,
                    int(turns_since_switch),
                    float(last_switch_at),
                    1 if soft_boundary_pending else 0,
                    self._now(),
                ),
            )
            conn.commit()

    # ------------------------------------------------------------------ topics
    def create_topic(
        self,
        session_id: str,
        title: str,
        summary: str = "",
        topic_key: str | None = None,
    ) -> int:
        """新建话题，返回主键。"""
        self.ensure_session(session_id)
        now = self._now()
        with self._lock:
            conn = self._require()
            cursor = conn.execute(
                """
                INSERT INTO topics(
                  session_id, topic_key, title, summary, status,
                  created_at, last_active_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (session_id, topic_key, title, summary, now, now),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def update_topic(
        self,
        topic_id: int,
        *,
        title: str | None = None,
        summary: str | None = None,
        status: str | None = None,
        last_active_at: float | None = None,
    ) -> None:
        """按需更新话题字段。"""
        values: dict[str, Any] = {}
        if title is not None:
            values["title"] = title
        if summary is not None:
            values["summary"] = summary
        if status is not None:
            values["status"] = status
        if last_active_at is not None:
            values["last_active_at"] = float(last_active_at)
        if not values:
            return
        assignments = ", ".join(f"{key}=:{key}" for key in values)
        values["topic_id"] = topic_id
        with self._lock:
            conn = self._require()
            conn.execute(f"UPDATE topics SET {assignments} WHERE id=:topic_id", values)
            conn.commit()

    def get_topic(self, topic_id: int) -> dict[str, Any] | None:
        """按主键取话题。"""
        with self._lock:
            row = self._require().execute(
                "SELECT * FROM topics WHERE id = ?", (topic_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_topics(
        self,
        session_id: str,
        *,
        limit: int = 10,
        statuses: Sequence[str] = ("active", "archived"),
    ) -> list[dict[str, Any]]:
        """列出会话的最近话题。"""
        statuses = tuple(statuses)
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._require().execute(
                f"""
                SELECT * FROM topics
                WHERE session_id = ? AND status IN ({placeholders})
                ORDER BY last_active_at DESC LIMIT ?
                """,
                (session_id, *statuses, limit),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def load_topic_stack(self, session_id: str) -> list[dict[str, Any]]:
        """读取话题栈（position 升序）。"""
        with self._lock:
            rows = self._require().execute(
                """
                SELECT t.*, s.position AS position
                FROM topic_stack s JOIN topics t ON t.id = s.topic_id
                WHERE s.session_id = ? ORDER BY s.position ASC
                """,
                (session_id,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def save_topic_stack(self, session_id: str, topic_ids: Sequence[int]) -> None:
        """整体替换话题栈（先删后插，事务）。"""
        with self._lock:
            conn = self._require()
            try:
                conn.execute("BEGIN")
                conn.execute("DELETE FROM topic_stack WHERE session_id = ?", (session_id,))
                for position, topic_id in enumerate(topic_ids):
                    conn.execute(
                        """
                        INSERT INTO topic_stack(session_id, position, topic_id, pushed_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (session_id, position, int(topic_id), self._now()),
                    )
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
                raise

    # ------------------------------------------------------------------ memories
    def insert_memory(
        self,
        *,
        session_id: str,
        memory_type: str,
        content: str,
        keywords: Sequence[str] = (),
        salience: float = 0.0,
        importance: float = 0.5,
        confidence: float = 1.0,
        source_turn_id: str | None = None,
    ) -> int:
        """插入一条记忆，返回主键。"""
        self.ensure_session(session_id)
        with self._lock:
            conn = self._require()
            cursor = conn.execute(
                """
                INSERT INTO memories(
                  session_id, type, content, keywords, emotional_salience,
                  importance, confidence, created_at, source_turn_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    memory_type,
                    content,
                    dumps(list(keywords)),
                    float(salience),
                    float(importance),
                    float(confidence),
                    self._now(),
                    source_turn_id,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def fetch_memories(
        self,
        session_id: str,
        memory_types: Sequence[str],
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """按类型粗筛记忆候选。"""
        rows: list[dict[str, Any]] = []
        with self._lock:
            conn = self._require()
            for memory_type in memory_types:
                fetched = conn.execute(
                    """
                    SELECT * FROM memories
                    WHERE session_id = ? AND type = ?
                    ORDER BY importance DESC, created_at DESC LIMIT ?
                    """,
                    (session_id, memory_type, limit),
                ).fetchall()
                for row in fetched:
                    data = self._row_to_dict(row)
                    data["keywords"] = self._decode_keywords(row["keywords"])
                    rows.append(data)
        return rows

    def mark_recalled(
        self, memory_ids: Sequence[int], *, at: float | None = None
    ) -> None:
        """更新召回时间与次数。"""
        ids = [int(memory_id) for memory_id in memory_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            conn = self._require()
            conn.execute(
                f"""
                UPDATE memories
                SET last_recalled_at = ?, recall_count = recall_count + 1
                WHERE id IN ({placeholders})
                """,
                (self._now() if at is None else float(at), *ids),
            )
            conn.commit()

    def adjust_importance(self, memory_id: int, delta: float) -> None:
        """调整重要度（裁剪到 [0,1]）。"""
        with self._lock:
            conn = self._require()
            conn.execute(
                """
                UPDATE memories
                SET importance = MAX(0.0, MIN(1.0, importance + ?))
                WHERE id = ?
                """,
                (float(delta), int(memory_id)),
            )
            conn.commit()

    def mark_corrected(self, memory_id: int) -> None:
        """记录一次用户纠正：计数 +1、重要度 -0.1。"""
        with self._lock:
            conn = self._require()
            conn.execute(
                """
                UPDATE memories
                SET corrected_count = corrected_count + 1,
                    importance = MAX(0.0, importance - 0.1)
                WHERE id = ?
                """,
                (int(memory_id),),
            )
            conn.commit()

    def delete_memory(self, memory_id: int) -> bool:
        """删除一条记忆。"""
        with self._lock:
            conn = self._require()
            cursor = conn.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
            conn.commit()
        return bool(cursor.rowcount)

    def update_memory_content(self, memory_id: int, content: str) -> None:
        """人工修正记忆正文。"""
        with self._lock:
            conn = self._require()
            conn.execute(
                "UPDATE memories SET content = ? WHERE id = ?",
                (content, int(memory_id)),
            )
            conn.commit()

    # ------------------------------------------------------------------ relationship
    def load_relationship(self, session_id: str) -> dict[str, Any] | None:
        """读取关系状态行。"""
        with self._lock:
            row = self._require().execute(
                "SELECT * FROM relationship_state WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        data = self._row_to_dict(row)
        data["pending_promises"] = loads(row["pending_promises"], [])
        return data

    def save_relationship(
        self,
        session_id: str,
        *,
        intimacy: float,
        tension: float,
        pending_promises: Sequence[dict[str, Any]],
    ) -> None:
        """写入关系状态（UPSERT）。"""
        with self._lock:
            conn = self._require()
            conn.execute(
                """
                INSERT INTO relationship_state(
                  session_id, intimacy, tension, pending_promises, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  intimacy=excluded.intimacy,
                  tension=excluded.tension,
                  pending_promises=excluded.pending_promises,
                  updated_at=excluded.updated_at
                """,
                (
                    session_id,
                    max(0.0, min(1.0, float(intimacy))),
                    max(0.0, min(1.0, float(tension))),
                    dumps(list(pending_promises)),
                    self._now(),
                ),
            )
            conn.commit()

    # ------------------------------------------------------------------ logs
    def insert_turn_log(
        self,
        *,
        turn_id: str,
        session_id: str,
        decision_json: str,
        injected_tokens: int,
        dropped_json: str | None = None,
    ) -> None:
        """写一轮决策日志。"""
        with self._lock:
            conn = self._require()
            conn.execute(
                """
                INSERT INTO turn_logs(
                  turn_id, session_id, decision_json, injected_tokens,
                  dropped_items, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    session_id,
                    decision_json,
                    int(injected_tokens),
                    dropped_json,
                    self._now(),
                ),
            )
            conn.commit()

    def get_turn_log(self, turn_id: str) -> dict[str, Any] | None:
        """按 turn_id 取日志。"""
        with self._lock:
            row = self._require().execute(
                "SELECT * FROM turn_logs WHERE turn_id = ? ORDER BY id DESC LIMIT 1",
                (turn_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_turn_logs(
        self, session_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """列出会话最近的决策日志。"""
        with self._lock:
            rows = self._require().execute(
                """
                SELECT * FROM turn_logs WHERE session_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def insert_archive_failure(
        self,
        *,
        turn_id: str | None,
        stage: str,
        error: str,
        payload: str | None = None,
    ) -> None:
        """记录归档最终失败（payload 截断到 8KB）。"""
        with self._lock:
            conn = self._require()
            conn.execute(
                """
                INSERT INTO archive_failures(
                  turn_id, stage, error, payload, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    stage,
                    error[:2000],
                    (payload or "")[:8192],
                    self._now(),
                ),
            )
            conn.commit()

    def purge_turn_logs(self, older_than: float) -> int:
        """清理过期决策日志，返回删除行数。"""
        with self._lock:
            conn = self._require()
            cursor = conn.execute(
                "DELETE FROM turn_logs WHERE created_at < ?", (float(older_than),)
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------ export
    def export_session(self, session_id: str) -> dict[str, Any]:
        """导出单个会话的全部数据。"""
        with self._lock:
            conn = self._require()
            topics = [
                self._row_to_dict(row)
                for row in conn.execute(
                    "SELECT * FROM topics WHERE session_id = ? ORDER BY id", (session_id,)
                ).fetchall()
            ]
            memories = []
            for row in conn.execute(
                "SELECT * FROM memories WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall():
                data = self._row_to_dict(row)
                data["keywords"] = self._decode_keywords(row["keywords"])
                memories.append(data)
            logs = [
                self._row_to_dict(row)
                for row in conn.execute(
                    "SELECT * FROM turn_logs WHERE session_id = ? ORDER BY id",
                    (session_id,),
                ).fetchall()
            ]
        return {
            "session_id": session_id,
            "session_state": self.load_session_state(session_id),
            "topics": topics,
            "memories": memories,
            "relationship": self.load_relationship(session_id),
            "turn_logs": logs,
            "exported_at": self._now(),
        }

    # ------------------------------------------------------------------ internals
    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """把 Row 转成 dict。"""
        return dict(row)

    def _decode_keywords(self, raw: str | None) -> list[str]:
        """解析 keywords 列。"""
        value = loads(raw, [])
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]

    def _now(self) -> float:
        """统一时间来源。"""
        return time.time()

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise sqlite3.Error("store is not initialized")
        return self._conn
