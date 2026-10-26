"""SQLite 持久层：整个插件唯一的 SQL 出入口。

为什么收敛到一个类：

* 表只有 7 张（见 `ctm/sql/001_init.sql`），但写入路径分散在话题机、记忆、
  关系、归档四个模块。把 SQL 散开，事务边界就没人负责；
* 归档要求"单 worker 串行写"，读要求"不阻塞事件循环"。一个连接 + 显式
  事务方法，比每个模块各开连接更容易审计；
* 行对象用 `dict` 传递，`ctm` 上层模块不依赖 sqlite3.Row，单测可直接造假。

线程模型（v0.1 约定，见设计文档 §9）：

* 连接在 `init()` 建立，`check_same_thread=False` + `threading.Lock` 串行化；
* 主循环只做读（决策路径、命令），写全部发生在 Archiver 单 worker；
* 若将来读延迟超过预算，再把读挪到 `asyncio.to_thread`，**不要**改成多连接写
  （WAL 下多写者会互相 `database is locked`）。

相对路径约定：`db_path` 由插件层解析成绝对路径后传入；本类只认绝对路径。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Sequence

#: 当前 schema 版本，写入 `PRAGMA user_version`；与 sql/ 下的文件名前缀对应。
SCHEMA_VERSION = 1

#: DDL 文件位置（随包分发，不依赖 cwd）。
SCHEMA_PATH = Path(__file__).with_name("sql") / "001_init.sql"


class Store:
    """SQLite 门面。"""

    def __init__(self, db_path: Path) -> None:
        """初始化（不连接）。

        Args:
            db_path: 数据库绝对路径；目录不存在时 `init()` 会创建。
        """
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ 生命周期
    def init(self) -> None:
        """建立连接并应用迁移。

        Returns:
            None。

        Raises:
            sqlite3.Error: 打开或迁移失败（插件层负责降级并告警）。

        TODO(v0.1):
            1. 建父目录；
            2. `sqlite3.connect(path)`，`row_factory=sqlite3.Row`；
            3. `PRAGMA journal_mode=WAL` / `foreign_keys=ON` / `busy_timeout=5000`；
            4. `migrate()`。
        """
        raise NotImplementedError("v0.1: Store.init")

    def migrate(self) -> None:
        """按 `PRAGMA user_version` 应用 DDL。

        Returns:
            None。

        TODO(v0.1): user_version == SCHEMA_VERSION 时直接返回；
            否则在一个事务里执行 `SCHEMA_PATH`，再写 user_version。
            版本高于代码时拒绝启动并报错（防止旧代码写新库）。
        """
        raise NotImplementedError("v0.1: Store.migrate")

    def close(self) -> None:
        """关闭连接（先 checkpoint）。

        Returns:
            None。

        TODO(v0.1): `PRAGMA wal_checkpoint(TRUNCATE)` 后 close；幂等。
        """
        raise NotImplementedError("v0.1: Store.close")

    def checkpoint(self) -> None:
        """手动 WAL checkpoint（运维/备份前调用）。

        Returns:
            None。

        TODO(v0.1): 执行 `PRAGMA wal_checkpoint(TRUNCATE)`；失败只记 WARN。
        """
        raise NotImplementedError("v0.1: Store.checkpoint")

    def health(self) -> dict[str, Any]:
        """健康检查。

        Returns:
            形如 `{"ok": True, "path": "...", "size_bytes": 0, "tables": 7}`；
            数据库不可读时 `ok=False` 并带 error。

        TODO(v0.1): 文件存在性 + `SELECT 1` + `sqlite_master` 计数。
        """
        raise NotImplementedError("v0.1: Store.health")

    # ------------------------------------------------------------------ sessions
    def ensure_session(self, session_id: str, user_id: str = "") -> None:
        """确保会话行存在（幂等）。

        Args:
            session_id: 会话键（unified_msg_origin）。
            user_id: 发送者 ID；未知时为空串。

        Returns:
            None。

        TODO(v0.1): `INSERT ... ON CONFLICT(session_id) DO UPDATE SET updated_at`。
        """
        raise NotImplementedError("v0.1: Store.ensure_session")

    def load_session_state(self, session_id: str) -> dict[str, Any]:
        """读取会话级话题状态。

        Args:
            session_id: 会话键。

        Returns:
            `{"turns_since_switch": 0, "last_switch_at": 0.0, "soft_boundary_pending": False}`；
            无记录时返回默认值。

        TODO(v0.1): LEFT JOIN 或两次查询；把 INTEGER 0/1 还原成 bool。
        """
        raise NotImplementedError("v0.1: Store.load_session_state")

    def save_session_state(
        self,
        session_id: str,
        *,
        turns_since_switch: int,
        last_switch_at: float,
        soft_boundary_pending: bool,
    ) -> None:
        """写入会话级话题状态（幂等 UPSERT）。

        Args:
            session_id: 会话键。
            turns_since_switch: 距上次切换的轮数。
            last_switch_at: 上次切换时间。
            soft_boundary_pending: 边界提示是否待注入。

        Returns:
            None。

        TODO(v0.1): UPSERT 到 session_state；bool 存 0/1。
        """
        raise NotImplementedError("v0.1: Store.save_session_state")

    # ------------------------------------------------------------------ topics
    def create_topic(
        self,
        session_id: str,
        title: str,
        summary: str = "",
        topic_key: str | None = None,
    ) -> int:
        """新建话题。

        Args:
            session_id: 会话键。
            title: 标题。
            summary: 摘要（可空）。
            topic_key: 归一化键（可空）。

        Returns:
            新行主键。

        TODO(v0.1): 插入 topics，`last_active_at=created_at=time.time()`。
        """
        raise NotImplementedError("v0.1: Store.create_topic")

    def update_topic(
        self,
        topic_id: int,
        *,
        title: str | None = None,
        summary: str | None = None,
        status: str | None = None,
        last_active_at: float | None = None,
    ) -> None:
        """按需更新话题字段。

        Args:
            topic_id: 主键。
            title: 新标题；None 表示不改。
            summary: 新摘要。
            status: active / archived / popped。
            last_active_at: 活跃时间。

        Returns:
            None。

        TODO(v0.1): 动态拼 SET（只更新非 None 字段），全 None 时直接返回。
        """
        raise NotImplementedError("v0.1: Store.update_topic")

    def get_topic(self, topic_id: int) -> dict[str, Any] | None:
        """按主键取话题。

        Args:
            topic_id: 主键。

        Returns:
            行字典；不存在返回 None。

        TODO(v0.1): SELECT * WHERE id=?；Row → dict。
        """
        raise NotImplementedError("v0.1: Store.get_topic")

    def list_topics(
        self,
        session_id: str,
        *,
        limit: int = 10,
        statuses: Sequence[str] = ("active", "archived"),
    ) -> list[dict[str, Any]]:
        """列出会话的最近话题（按 last_active_at 倒序）。

        Args:
            session_id: 会话键。
            limit: 上限。
            statuses: 过滤状态。

        Returns:
            行字典列表；空表返回空列表。

        TODO(v0.1): IN 占位符按 statuses 长度动态拼。
        """
        raise NotImplementedError("v0.1: Store.list_topics")

    def load_topic_stack(self, session_id: str) -> list[dict[str, Any]]:
        """读取话题栈（position 升序，栈顶在最后）。

        Args:
            session_id: 会话键。

        Returns:
            话题行列表，附带 `position` 字段。

        TODO(v0.1): topic_stack JOIN topics ON topic_id。
        """
        raise NotImplementedError("v0.1: Store.load_topic_stack")

    def save_topic_stack(self, session_id: str, topic_ids: Sequence[int]) -> None:
        """整体替换话题栈（事务内先删后插）。

        Args:
            session_id: 会话键。
            topic_ids: 从栈底到栈顶的话题主键。

        Returns:
            None。

        TODO(v0.1): DELETE 后按索引写入 position；空列表等价清栈。
        """
        raise NotImplementedError("v0.1: Store.save_topic_stack")

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
        """插入一条记忆。

        Args:
            session_id: 会话键。
            memory_type: summary / assertion / episode。
            content: 正文。
            keywords: 关键词（存 JSON 文本）。
            salience: 情感显著性 0..1。
            importance: 重要度 0..1。
            confidence: 置信度 0..1。
            source_turn_id: 来源轮次。

        Returns:
            新行主键。

        TODO(v0.1): keywords 用 `json.dumps(ensure_ascii=False)`。
        """
        raise NotImplementedError("v0.1: Store.insert_memory")

    def fetch_memories(
        self,
        session_id: str,
        memory_types: Sequence[str],
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """按类型粗筛记忆候选。

        Args:
            session_id: 会话键。
            memory_types: 类型白名单。
            limit: 每类上限（实现时按类型分组查询）。

        Returns:
            行字典列表（keywords 已反序列化为 list）。

        TODO(v0.1): 每个类型一条 `ORDER BY importance DESC, created_at DESC LIMIT ?`；
            JSON 解析失败时 keywords 置空，不能整行丢弃。
        """
        raise NotImplementedError("v0.1: Store.fetch_memories")

    def mark_recalled(self, memory_ids: Sequence[int], *, at: float | None = None) -> None:
        """更新召回时间与次数。

        Args:
            memory_ids: 主键列表。
            at: 召回时间；None 取当前。

        Returns:
            None。

        TODO(v0.1): `UPDATE ... SET last_recalled_at=?, recall_count=recall_count+1`；
            空列表直接返回。
        """
        raise NotImplementedError("v0.1: Store.mark_recalled")

    def adjust_importance(self, memory_id: int, delta: float) -> None:
        """调整重要度（裁剪到 [0,1]）。

        Args:
            memory_id: 主键。
            delta: 增量。

        Returns:
            None。

        TODO(v0.1): 用 `MAX(0.0, MIN(1.0, importance + ?))` 在 SQL 里夹取。
        """
        raise NotImplementedError("v0.1: Store.adjust_importance")

    def mark_corrected(self, memory_id: int) -> None:
        """记录一次用户纠正（降权依据）。

        Args:
            memory_id: 主键。

        Returns:
            None。

        TODO(v0.1): `corrected_count += 1`，同时把 importance 减 0.1（下限 0）。
        """
        raise NotImplementedError("v0.1: Store.mark_corrected")

    # ------------------------------------------------------------------ relationship
    def load_relationship(self, session_id: str) -> dict[str, Any] | None:
        """读取关系状态行。

        Args:
            session_id: 会话键。

        Returns:
            行字典（pending_promises 已解析）；无记录返回 None。

        TODO(v0.1): JSON 解析失败时 pending_promises 置 [] 并记 WARN。
        """
        raise NotImplementedError("v0.1: Store.load_relationship")

    def save_relationship(
        self,
        session_id: str,
        *,
        intimacy: float,
        tension: float,
        pending_promises: Sequence[dict[str, Any]],
    ) -> None:
        """写入关系状态（UPSERT）。

        Args:
            session_id: 会话键。
            intimacy: 0..1。
            tension: 0..1。
            pending_promises: 未完成事项列表（存 JSON）。

        Returns:
            None。

        TODO(v0.1): 夹取取值范围；`updated_at=time.time()`。
        """
        raise NotImplementedError("v0.1: Store.save_relationship")

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
        """写一轮决策日志。

        Args:
            turn_id: 轮次 ID。
            session_id: 会话键。
            decision_json: 决策 JSON（由 TurnDecision.to_dict 序列化）。
            injected_tokens: 实际注入估算 Token。
            dropped_json: 丢弃项 JSON。

        Returns:
            None。

        TODO(v0.1): 主循环内不直接调用——经 Archiver 串行写；
            或者接受"日志晚一拍"的代价，见设计文档 §5。
        """
        raise NotImplementedError("v0.1: Store.insert_turn_log")

    def get_turn_log(self, turn_id: str) -> dict[str, Any] | None:
        """按 turn_id 取日志（`/ctm replay` 用）。

        Args:
            turn_id: 轮次 ID。

        Returns:
            行字典；不存在返回 None。

        TODO(v0.1): SELECT ... WHERE turn_id=? ORDER BY id DESC LIMIT 1。
        """
        raise NotImplementedError("v0.1: Store.get_turn_log")

    def list_turn_logs(self, session_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """列出会话最近的决策日志。

        Args:
            session_id: 会话键。
            limit: 上限。

        Returns:
            行字典列表，按时间倒序。

        TODO(v0.1): 索引 idx_turn_logs_session 覆盖该查询。
        """
        raise NotImplementedError("v0.1: Store.list_turn_logs")

    def insert_archive_failure(
        self,
        *,
        turn_id: str | None,
        stage: str,
        error: str,
        payload: str | None = None,
    ) -> None:
        """记录归档最终失败。

        Args:
            turn_id: 轮次 ID（可空）。
            stage: 失败阶段（summarize / assert / episode / relationship / log）。
            error: 错误摘要。
            payload: 原始数据（截断后存）。

        Returns:
            None。

        TODO(v0.1): 截断 payload 到 8KB，避免单条失败撑爆库。
        """
        raise NotImplementedError("v0.1: Store.insert_archive_failure")

    def purge_turn_logs(self, older_than: float) -> int:
        """清理过期决策日志。

        Args:
            older_than: 时间戳；早于它的记录被删除。

        Returns:
            删除行数。

        TODO(v0.1): 每日维护任务调用；删除前先 checkpoint 视情况。
        """
        raise NotImplementedError("v0.1: Store.purge_turn_logs")

    # ------------------------------------------------------------------ export
    def export_session(self, session_id: str) -> dict[str, Any]:
        """导出单个会话的全部数据（JSON 可序列化）。

        Args:
            session_id: 会话键。

        Returns:
            含 topics / memories / relationship / turn_logs / session_state 的字典。

        TODO(v0.1): 分表读取后组装；ID 保持原值（导入时映射见 v1.0 迁移工具）。
        """
        raise NotImplementedError("v0.1: Store.export_session")

    # ------------------------------------------------------------------ internals
    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """把 Row 转成 dict。

        Args:
            row: sqlite3.Row。

        Returns:
            dict 行。

        TODO(v0.1): return dict(row)
        """
        raise NotImplementedError("v0.1: Store._row_to_dict")

    def _decode_keywords(self, raw: str | None) -> list[str]:
        """解析 keywords 列。

        Args:
            raw: JSON 文本。

        Returns:
            关键词列表；解析失败返回 []。

        TODO(v0.1): try json.loads 且校验 list[str]。
        """
        raise NotImplementedError("v0.1: Store._decode_keywords")

    def _now(self) -> float:
        """统一时间来源。

        Returns:
            Unix 秒。

        TODO(v0.1): return time.time()
        """
        raise NotImplementedError("v0.1: Store._now")


def dumps(value: Any) -> str:
    """紧凑 JSON 序列化（中文不转义）。

    Args:
        value: 任意可序列化对象。

    Returns:
        JSON 文本。

    TODO(v0.1): json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    """
    raise NotImplementedError("v0.1: dumps")


def loads(text: str | None, default: Any) -> Any:
    """容错 JSON 反序列化。

    Args:
        text: JSON 文本或 None。
        default: 解析失败时的返回值。

    Returns:
        解析结果或 default。

    TODO(v0.1): try/except (ValueError, TypeError)。
    """
    raise NotImplementedError("v0.1: loads")
