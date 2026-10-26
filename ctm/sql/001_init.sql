-- CTM (Cognitive Turn Manager) v0.1 schema
--
-- 与设计文档 §4 的差异只有一处（D1）：
--   * 新增 session_state 表。原因是话题状态机需要逐轮持久化
--     turns_since_switch / last_switch_at / soft_boundary_pending，
--     而 §4 的 DDL 没有落这三项——不落地就只能放在内存，
--     重启后 min_turns / cooldown 判定会全部失真。
--
-- 其他约定：
--   * 时间一律存 Unix 秒（REAL），不做时区转换；
--   * 状态/类型列存字符串（与 ctm.enums 的 value 一致），不用整数枚举；
--   * keywords / pending_promises 存 JSON 文本，读取失败按空值处理。

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  user_id TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS session_state (
  session_id TEXT PRIMARY KEY,
  turns_since_switch INTEGER NOT NULL DEFAULT 0,
  last_switch_at REAL NOT NULL DEFAULT 0,
  soft_boundary_pending INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(session_id)
);

CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  topic_key TEXT,
  title TEXT NOT NULL,
  summary TEXT,
  status TEXT NOT NULL DEFAULT 'active',   -- active/archived/popped
  created_at REAL NOT NULL,
  last_active_at REAL NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_topics_session_status ON topics(session_id, status);

CREATE TABLE IF NOT EXISTS topic_stack (
  session_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  topic_id INTEGER NOT NULL,
  pushed_at REAL NOT NULL,
  PRIMARY KEY (session_id, position),
  FOREIGN KEY(topic_id) REFERENCES topics(id)
);

CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  type TEXT NOT NULL,                      -- summary/assertion/episode
  content TEXT NOT NULL,
  keywords TEXT,
  emotional_salience REAL DEFAULT 0,
  importance REAL DEFAULT 0.5,
  confidence REAL DEFAULT 1.0,
  created_at REAL NOT NULL,
  last_recalled_at REAL,
  recall_count INTEGER DEFAULT 0,
  corrected_count INTEGER DEFAULT 0,
  source_turn_id TEXT,
  FOREIGN KEY(session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_memories_session_type ON memories(session_id, type);
CREATE INDEX IF NOT EXISTS idx_memories_recalled ON memories(last_recalled_at);
-- §9 的优化：候选检索先按 session_id + type + last_recalled_at 过滤。
CREATE INDEX IF NOT EXISTS idx_memories_session_type_recalled
  ON memories(session_id, type, last_recalled_at);

CREATE TABLE IF NOT EXISTS relationship_state (
  session_id TEXT PRIMARY KEY,
  intimacy REAL NOT NULL DEFAULT 0.5,
  tension REAL NOT NULL DEFAULT 0.0,
  pending_promises TEXT,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS turn_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  turn_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  decision_json TEXT NOT NULL,
  injected_tokens INTEGER,
  dropped_items TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turn_logs_session ON turn_logs(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_turn_logs_turn_id ON turn_logs(turn_id);

CREATE TABLE IF NOT EXISTS archive_failures (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  turn_id TEXT,
  stage TEXT,
  error TEXT,
  payload TEXT,
  created_at REAL NOT NULL
);
