"""长期记忆持久层：独立表前缀 `lt_`，与 CTM 既有 `memories` 表互不影响。

单连接 + `threading.Lock` 串行化；写路径只在后台 worker 中发生，读路径是
短查询（带索引），不阻塞事件循环。

时间统一存 Unix 秒（REAL）。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lt_memories (
  id TEXT PRIMARY KEY,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  platform_id TEXT DEFAULT '',
  source_session TEXT DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'fact',
  slot_key TEXT,
  content TEXT NOT NULL,
  importance REAL NOT NULL DEFAULT 0.5,
  confidence REAL NOT NULL DEFAULT 0.6,
  status TEXT NOT NULL DEFAULT 'active',
  source TEXT NOT NULL DEFAULT 'auto',
  dedupe_hash TEXT NOT NULL UNIQUE,
  evidence_count INTEGER NOT NULL DEFAULT 1,
  use_count INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  last_used_at REAL,
  expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_lt_scope_status
  ON lt_memories(scope_type, scope_id, status);
CREATE INDEX IF NOT EXISTS idx_lt_scope_updated
  ON lt_memories(scope_type, scope_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_lt_kind ON lt_memories(kind);

CREATE TABLE IF NOT EXISTS lt_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id TEXT DEFAULT '',
  scope_type TEXT DEFAULT '',
  scope_id TEXT DEFAULT '',
  action TEXT NOT NULL,
  actor TEXT DEFAULT 'auto',
  detail TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lt_audit_memory ON lt_audit(memory_id, created_at DESC);
"""


class LongTermStore:
    """`lt_memories` / `lt_audit` 的唯一 SQL 出入口。"""

    def __init__(self, db_path: Path) -> None:
        """初始化（不连接）。

        Args:
            db_path: 数据库绝对路径；目录不存在时 `init()` 会创建。
        """
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def init(self) -> None:
        """建立连接并建表；失败抛出 `sqlite3.Error`，由插件层降级。"""
        with self._lock:
            if self._conn is not None:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn

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

    def health(self) -> dict[str, Any]:
        """返回健康状态，供 `/ctm health` 使用。"""
        with self._lock:
            if self._conn is None:
                return {"ok": False, "error": "not_initialized", "path": str(self._path)}
            try:
                self._conn.execute("SELECT 1").fetchone()
                tables = self._conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'lt_%'"
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

    # ------------------------------------------------------------------ memories
    def insert(self, row: dict[str, Any]) -> dict[str, Any]:
        """插入一条记忆并返回落库后的行。"""
        with self._lock:
            conn = self._require()
            conn.execute(
                """
                INSERT INTO lt_memories (
                  id, scope_type, scope_id, platform_id, source_session, kind,
                  slot_key, content, importance, confidence, status, source,
                  dedupe_hash, evidence_count, use_count, created_at, updated_at,
                  last_used_at, expires_at
                ) VALUES (
                  :id, :scope_type, :scope_id, :platform_id, :source_session, :kind,
                  :slot_key, :content, :importance, :confidence, :status, :source,
                  :dedupe_hash, :evidence_count, :use_count, :created_at, :updated_at,
                  :last_used_at, :expires_at
                )
                """,
                row,
            )
            conn.commit()
        return row

    def get(self, item_id: str) -> dict[str, Any] | None:
        """按 id 取一条记忆。"""
        with self._lock:
            row = self._require().execute(
                "SELECT * FROM lt_memories WHERE id = ?", (item_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_by_hash(self, dedupe_hash: str) -> dict[str, Any] | None:
        """按去重哈希取一条记忆。"""
        with self._lock:
            row = self._require().execute(
                "SELECT * FROM lt_memories WHERE dedupe_hash = ?", (dedupe_hash,)
            ).fetchone()
        return dict(row) if row else None

    def find_by_slot(
        self,
        scope_type: str,
        scope_id: str,
        kind: str,
        slot_key: str,
    ) -> dict[str, Any] | None:
        """按结构化槽位查找现有记忆。"""
        with self._lock:
            row = self._require().execute(
                """
                SELECT * FROM lt_memories
                WHERE scope_type = ? AND scope_id = ? AND kind = ?
                  AND slot_key = ? AND status != 'archived'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (scope_type, scope_id, kind, slot_key),
            ).fetchone()
        return dict(row) if row else None

    def list_scope(
        self,
        scope_type: str,
        scope_id: str,
        *,
        statuses: Iterable[str] = ("active",),
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """列出某个作用域的记忆（按更新时间倒序）。"""
        statuses = tuple(statuses)
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._require().execute(
                f"""
                SELECT * FROM lt_memories
                WHERE scope_type = ? AND scope_id = ? AND status IN ({placeholders})
                ORDER BY updated_at DESC LIMIT ?
                """,
                (scope_type, scope_id, *statuses, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_scope(
        self,
        scope_type: str,
        scope_id: str,
        *,
        statuses: tuple[str, ...] = ("active",),
    ) -> int:
        """统计某个作用域的记忆条数。"""
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            row = self._require().execute(
                f"""
                SELECT COUNT(*) FROM lt_memories
                WHERE scope_type = ? AND scope_id = ? AND status IN ({placeholders})
                """,
                (scope_type, scope_id, *statuses),
            ).fetchone()
        return int(row[0]) if row else 0

    def update(self, item_id: str, **fields: Any) -> None:
        """更新指定列；自动刷新 `updated_at`（调用方显式传入则尊重）。"""
        values = {key: value for key, value in fields.items() if value is not None}
        if not values:
            return
        values.setdefault("updated_at", time.time())
        assignments = ", ".join(f"{key} = :{key}" for key in values)
        values["id"] = item_id
        with self._lock:
            conn = self._require()
            conn.execute(
                f"UPDATE lt_memories SET {assignments} WHERE id = :id",
                values,
            )
            conn.commit()

    def delete(self, item_id: str) -> bool:
        """删除一条记忆。"""
        with self._lock:
            conn = self._require()
            cursor = conn.execute("DELETE FROM lt_memories WHERE id = ?", (item_id,))
            conn.commit()
        return bool(cursor.rowcount)

    def delete_scope(self, scope_type: str, scope_id: str) -> int:
        """清空一个作用域。"""
        with self._lock:
            conn = self._require()
            cursor = conn.execute(
                "DELETE FROM lt_memories WHERE scope_type = ? AND scope_id = ?",
                (scope_type, scope_id),
            )
            conn.commit()
        return int(cursor.rowcount or 0)

    def touch(self, item_ids: list[str]) -> None:
        """记录召回使用：`use_count+1`、`last_used_at=now`，但不改 `updated_at`。"""
        ids = [item_id for item_id in item_ids if item_id]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            conn = self._require()
            conn.execute(
                f"""
                UPDATE lt_memories
                SET use_count = use_count + 1, last_used_at = ?
                WHERE id IN ({placeholders})
                """,
                (time.time(), *ids),
            )
            conn.commit()

    def list_scopes(self, limit: int = 100) -> list[dict[str, Any]]:
        """列出有记忆的作用域及条数（供导出/清空使用）。"""
        with self._lock:
            rows = self._require().execute(
                """
                SELECT scope_type, scope_id, COUNT(*) AS count
                FROM lt_memories WHERE status != 'archived'
                GROUP BY scope_type, scope_id ORDER BY count DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "scope_type": row["scope_type"],
                "scope_id": row["scope_id"],
                "count": int(row["count"]),
            }
            for row in rows
        ]

    def recent_items(self, limit: int = 50) -> list[dict[str, Any]]:
        """全局最近更新的长期记忆（页面用）。"""
        with self._lock:
            rows = self._require().execute(
                """
                SELECT id, scope_type, scope_id, kind, slot_key, content,
                       importance, confidence, status, updated_at
                FROM lt_memories WHERE status != 'archived'
                ORDER BY updated_at DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_total(self) -> int:
        """长期记忆总条数。"""
        with self._lock:
            row = self._require().execute(
                "SELECT COUNT(*) FROM lt_memories WHERE status != 'archived'"
            ).fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------ audit
    def audit(
        self,
        *,
        memory_id: str = "",
        scope_type: str = "",
        scope_id: str = "",
        action: str,
        actor: str = "auto",
        detail: str | None = None,
    ) -> None:
        """追加一条审计记录；失败不影响主流程。"""
        try:
            with self._lock:
                conn = self._require()
                conn.execute(
                    """
                    INSERT INTO lt_audit
                      (memory_id, scope_type, scope_id, action, actor, detail, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (memory_id, scope_type, scope_id, action, actor, detail, time.time()),
                )
                conn.commit()
        except sqlite3.Error:
            pass

    def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        """列出最近的审计记录。"""
        with self._lock:
            rows = self._require().execute(
                "SELECT * FROM lt_audit ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ internal
    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise sqlite3.Error("long-term store is not initialized")
        return self._conn
