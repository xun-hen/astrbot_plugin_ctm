"""长期记忆门面：召回、异步入队抽取、管理操作。

约束：

* 召回失败只返回 None，绝不抛出到 hook；
* 抽取在单 worker 中串行执行，队列有界，小时预算可配；
* 作用域：`user = platform:sender`、`group = platform:group`，群聊可同时读两者；
* 注入内容由插件层写入 `extra_user_content_parts` 并标记为临时，不落历史。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import time
import uuid
from collections import deque
from dataclasses import replace
from typing import Any, Awaitable, Callable

from .config import LongTermConfig
from .extractor import (
    MemoryCandidate,
    build_prompt,
    is_sensitive,
    parse_candidates,
    sanitize_memory_text,
)
from .retriever import Retriever, estimate_tokens
from .store import LongTermStore

QUEUE_MAXSIZE = 256
EXTRACT_TIMEOUT_SECONDS = 30.0
RECALL_CANDIDATE_LIMIT = 200
VALID_KINDS = ("profile", "preference", "event", "task", "fact")

_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)

ProviderGetter = Callable[[str], Awaitable[Any]]


def _normalize(text: str) -> str:
    return _PUNCT_RE.sub("", (text or "").lower())


def _dedupe_hash(
    scope_type: str,
    scope_id: str,
    kind: str,
    slot_key: str,
    content: str,
) -> str:
    raw = f"{scope_type}|{scope_id}|{kind}|{slot_key or ''}|{_normalize(content)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class LongTermMemory:
    """长期记忆的统一入口。"""

    def __init__(
        self,
        store: LongTermStore,
        cfg: LongTermConfig,
        provider_getter: ProviderGetter,
    ) -> None:
        """初始化。

        Args:
            store: 持久层。
            cfg: 配置快照。
            provider_getter: 异步函数，接收 `session_id` 返回 chat provider。
        """
        self.store = store
        self.cfg = cfg
        self._provider_getter = provider_getter
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._worker: asyncio.Task | None = None
        self._closed = False
        self._turns: dict[str, int] = {}
        self._calls: dict[str, deque[float]] = {}

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """启动后台抽取 worker。"""
        if self._closed:
            return
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop())

    async def close(self) -> None:
        """取消 worker；积压任务直接丢弃（学习信号可丢，不能阻塞卸载）。"""
        self._closed = True
        if self._worker is None:
            return
        self._worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._worker
        self._worker = None
        self._queue = None

    # ------------------------------------------------------------------ recall
    async def recall(
        self,
        *,
        session_id: str,
        platform_id: str,
        sender_id: str,
        group_id: str,
        is_group: bool,
        query: str,
    ) -> str | None:
        """召回本轮相关记忆并渲染为 Prompt 块。

        Args:
            session_id: 会话键（unified_msg_origin）。
            platform_id: 平台实例 ID。
            sender_id: 发送者 ID。
            group_id: 群 ID；私聊为空。
            is_group: 是否群聊。
            query: 用户当前消息。

        Returns:
            渲染后的记忆块；无命中或失败时返回 None。
        """
        if not (self.cfg.enabled and self.cfg.recall_enabled):
            return None
        if len(query or "") < 2:
            return None
        try:
            return await asyncio.wait_for(
                self._build_recall_text(
                    platform_id=platform_id,
                    sender_id=sender_id,
                    group_id=group_id,
                    is_group=is_group,
                    query=query,
                ),
                timeout=self.cfg.recall_timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            return None
        except Exception:  # noqa: BLE001 - 召回绝不影响回复
            return None

    async def _build_recall_text(
        self,
        *,
        platform_id: str,
        sender_id: str,
        group_id: str,
        is_group: bool,
        query: str,
    ) -> str | None:
        scopes = self._scopes(
            platform_id, sender_id, group_id, is_group, self.cfg
        )
        if not scopes:
            return None
        now = time.time()
        rows: list[dict[str, Any]] = []
        for scope_type, scope_id in scopes:
            rows.extend(
                self.store.list_scope(
                    scope_type,
                    scope_id,
                    statuses=("active",),
                    limit=RECALL_CANDIDATE_LIMIT,
                )
            )
        rows = [row for row in rows if self._not_expired(row, now)]
        if not rows:
            return None
        result = Retriever(self.cfg).select(rows, query=query, now=now)
        if result.empty:
            return None
        self.store.touch([str(row["id"]) for row in result.rows])
        return result.text

    # ------------------------------------------------------------------ write
    def observe_turn(
        self,
        *,
        session_id: str,
        platform_id: str,
        sender_id: str,
        group_id: str,
        is_group: bool,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """在回复完成后调度一次抽取（同步、非阻塞）。

        Args:
            session_id: 会话键。
            platform_id: 平台实例 ID。
            sender_id: 发送者 ID。
            group_id: 群 ID。
            is_group: 是否群聊。
            user_text: 用户消息。
            assistant_text: 助手回复。
        """
        try:
            if not (self.cfg.enabled and self.cfg.extract_enabled):
                return
            if len(user_text or "") + len(assistant_text or "") < (
                self.cfg.extract_min_chars
            ):
                return
            counter = self._turns.get(session_id, 0) + 1
            self._turns[session_id] = counter
            if len(self._turns) > 5000:
                self._turns.clear()
            if counter % self.cfg.extract_every_n_turns != 0:
                return
            scopes = self._scopes(
                platform_id, sender_id, group_id, is_group, self.cfg
            )
            if not scopes:
                return
            job = {
                "session_id": session_id,
                "platform_id": platform_id,
                "sender_id": sender_id,
                "group_id": group_id,
                "scopes": scopes,
                "user_text": user_text,
                "assistant_text": assistant_text,
            }
            self._enqueue(job)
        except Exception:  # noqa: BLE001
            return

    def _enqueue(self, job: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            return
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._worker_loop())

    async def _worker_loop(self) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            try:
                await self._process_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._queue.task_done()

    async def _process_job(self, job: dict[str, Any]) -> None:
        session_id = str(job["session_id"])
        if not self._allow_call(session_id):
            return
        provider = await self._provider_getter(session_id)
        if provider is None:
            return
        system_prompt, user_prompt = build_prompt(
            dialogue="",
            user_text=str(job["user_text"]),
            assistant_text=str(job["assistant_text"]),
            max_candidates=self.cfg.extract_max_candidates,
        )
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    system_prompt=system_prompt,
                    contexts=[{"role": "user", "content": user_prompt}],
                    request_max_retries=1,
                ),
                timeout=EXTRACT_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001
            return
        candidates = parse_candidates(
            getattr(response, "completion_text", "") or "",
            self.cfg.extract_max_candidates,
        )
        if self.cfg.block_sensitive:
            candidates = [
                candidate
                for candidate in candidates
                if not is_sensitive(candidate.content)
            ]
        for candidate in candidates:
            try:
                self._apply_candidate(job, candidate)
            except Exception:  # noqa: BLE001
                continue
        for scope_type, scope_id in job["scopes"]:
            self._prune_scope(scope_type, scope_id)

    def _allow_call(self, session_id: str) -> bool:
        if self.cfg.extract_max_calls_per_hour <= 0:
            return False
        now = time.monotonic()
        events = self._calls.setdefault(session_id, deque())
        while events and now - events[0] > 3600:
            events.popleft()
        if len(events) >= self.cfg.extract_max_calls_per_hour:
            return False
        events.append(now)
        return True

    def _apply_candidate(
        self,
        job: dict[str, Any],
        candidate: MemoryCandidate,
    ) -> None:
        content = sanitize_memory_text(candidate.content)
        if len(content) < 2:
            return
        scope_type, scope_id = self._candidate_scope(job, candidate.scope)
        if not scope_id:
            return
        confidence = candidate.confidence
        if confidence < self.cfg.extract_confidence_threshold:
            return
        status = (
            "active"
            if confidence >= self.cfg.extract_promote_threshold
            else "candidate"
        )
        dedupe = _dedupe_hash(
            scope_type, scope_id, candidate.kind, candidate.slot_key, content
        )
        existing = self.store.get_by_hash(dedupe)
        if existing is not None:
            new_confidence = min(1.0, max(existing["confidence"], confidence) + 0.03)
            new_status = existing["status"]
            if new_status == "candidate" and (
                new_confidence >= self.cfg.extract_promote_threshold
                or int(existing["evidence_count"]) + 1 >= 2
            ):
                new_status = "active"
            self.store.update(
                str(existing["id"]),
                evidence_count=int(existing["evidence_count"]) + 1,
                confidence=new_confidence,
                importance=max(float(existing["importance"]), candidate.importance),
                status=new_status,
            )
            if new_status != existing["status"]:
                self.store.audit(
                    memory_id=str(existing["id"]),
                    scope_type=scope_type,
                    scope_id=scope_id,
                    action="promote",
                )
            return

        if candidate.slot_key:
            slot_row = self.store.find_by_slot(
                scope_type, scope_id, candidate.kind, candidate.slot_key
            )
            if slot_row is not None:
                if confidence >= float(slot_row["confidence"]) - 0.1:
                    self.store.update(
                        str(slot_row["id"]),
                        content=content,
                        confidence=max(confidence, float(slot_row["confidence"])),
                        evidence_count=int(slot_row["evidence_count"]) + 1,
                        dedupe_hash=_dedupe_hash(
                            scope_type,
                            scope_id,
                            candidate.kind,
                            candidate.slot_key,
                            content,
                        ),
                    )
                    self.store.audit(
                        memory_id=str(slot_row["id"]),
                        scope_type=scope_type,
                        scope_id=scope_id,
                        action="update",
                        detail=json.dumps({"content": content}, ensure_ascii=False),
                    )
                return

        now = time.time()
        row = {
            "id": str(uuid.uuid4()),
            "scope_type": scope_type,
            "scope_id": scope_id,
            "platform_id": str(job["platform_id"]),
            "source_session": str(job["session_id"]),
            "kind": candidate.kind,
            "slot_key": candidate.slot_key or None,
            "content": content,
            "importance": candidate.importance,
            "confidence": confidence,
            "status": status,
            "source": "auto",
            "dedupe_hash": dedupe,
            "evidence_count": 1,
            "use_count": 0,
            "created_at": now,
            "updated_at": now,
            "last_used_at": None,
            "expires_at": None,
        }
        self.store.insert(row)
        self.store.audit(
            memory_id=row["id"],
            scope_type=scope_type,
            scope_id=scope_id,
            action="create",
            detail=json.dumps({"content": content}, ensure_ascii=False),
        )

    def _prune_scope(self, scope_type: str, scope_id: str) -> None:
        rows = self.store.list_scope(
            scope_type,
            scope_id,
            statuses=("active", "candidate"),
            limit=self.cfg.max_items_per_scope + 200,
        )
        if not rows:
            return
        now = time.time()
        cutoff = (
            now - self.cfg.retention_days * 86400
            if self.cfg.retention_days > 0
            else None
        )
        scored: list[tuple[float, dict[str, Any], bool]] = []
        for row in rows:
            updated = float(row.get("updated_at") or now)
            age_days = max(0.0, (now - updated) / 86400.0)
            recency = 0.5 ** (age_days / max(1.0, self.cfg.recall_half_life_days))
            score = (
                float(row.get("importance") or 0) * 0.6
                + recency * 0.3
                + min(int(row.get("use_count") or 0), 5) / 5.0 * 0.1
            )
            expired = cutoff is not None and updated < cutoff
            scored.append((score, row, expired))
        scored.sort(key=lambda entry: entry[0])
        expired_rows = [entry for entry in scored if entry[2]]
        live_rows = [entry for entry in scored if not entry[2]]
        overflow = max(0, len(live_rows) - self.cfg.max_items_per_scope)
        for score, row, expired in expired_rows + live_rows[:overflow]:
            self.store.delete(str(row["id"]))
            self.store.audit(
                memory_id=str(row["id"]),
                scope_type=scope_type,
                scope_id=scope_id,
                action="prune",
                detail=json.dumps(
                    {"score": round(score, 3), "expired": expired},
                    ensure_ascii=False,
                ),
            )

    # ------------------------------------------------------------------ management
    def search(
        self,
        *,
        session_id: str,
        platform_id: str,
        sender_id: str,
        group_id: str,
        is_group: bool,
        query: str,
        scope: str = "auto",
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """检索当前用户可读的记忆。"""
        scopes = self._scopes(platform_id, sender_id, group_id, is_group, self.cfg)
        if scope in {"user", "group"}:
            scopes = [pair for pair in scopes if pair[0] == scope]
        rows: list[dict[str, Any]] = []
        for scope_type, scope_id in scopes:
            rows.extend(
                self.store.list_scope(
                    scope_type,
                    scope_id,
                    statuses=("active",),
                    limit=RECALL_CANDIDATE_LIMIT,
                )
            )
        if not rows:
            return []
        cfg = replace(
            self.cfg,
            recall_max_items=max(1, min(limit, 50)),
            recall_max_tokens=4000,
        )
        return Retriever(cfg).select(rows, query=query).rows

    def save(
        self,
        *,
        session_id: str,
        platform_id: str,
        sender_id: str,
        group_id: str,
        is_group: bool,
        content: str,
        kind: str = "fact",
        slot_key: str = "",
        scope: str = "user",
        importance: float = 0.6,
    ) -> dict[str, Any] | None:
        """显式保存一条记忆（工具/命令调用）。"""
        if not self.cfg.enabled:
            return None
        if self.cfg.block_sensitive and is_sensitive(content):
            return None
        cleaned = sanitize_memory_text(content)
        if len(cleaned) < 2:
            return None
        scopes = self._scopes(platform_id, sender_id, group_id, is_group, self.cfg)
        if scope in {"user", "group"}:
            scopes = [pair for pair in scopes if pair[0] == scope]
        if not scopes:
            return None
        scope_type, scope_id = scopes[0]
        kind = kind if kind in VALID_KINDS else "fact"
        dedupe = _dedupe_hash(scope_type, scope_id, kind, slot_key, cleaned)
        existing = self.store.get_by_hash(dedupe)
        if existing is not None:
            self.store.update(
                str(existing["id"]),
                evidence_count=int(existing["evidence_count"]) + 1,
                confidence=min(1.0, float(existing["confidence"]) + 0.05),
                status="active",
            )
            return self.store.get(str(existing["id"]))
        now = time.time()
        row = {
            "id": str(uuid.uuid4()),
            "scope_type": scope_type,
            "scope_id": scope_id,
            "platform_id": platform_id,
            "source_session": session_id,
            "kind": kind,
            "slot_key": slot_key or None,
            "content": cleaned,
            "importance": max(0.0, min(1.0, importance)),
            "confidence": 0.95,
            "status": "active",
            "source": "tool",
            "dedupe_hash": dedupe,
            "evidence_count": 1,
            "use_count": 0,
            "created_at": now,
            "updated_at": now,
            "last_used_at": None,
            "expires_at": None,
        }
        self.store.insert(row)
        self.store.audit(
            memory_id=row["id"],
            scope_type=scope_type,
            scope_id=scope_id,
            action="create",
            actor="tool",
            detail=json.dumps({"content": cleaned}, ensure_ascii=False),
        )
        return row

    def forget(
        self,
        *,
        session_id: str,
        platform_id: str,
        sender_id: str,
        group_id: str,
        is_group: bool,
        memory_id: str = "",
        slot_key: str = "",
        query: str = "",
        scope: str = "auto",
    ) -> int:
        """删除记忆；返回删除条数。"""
        scopes = self._scopes(platform_id, sender_id, group_id, is_group, self.cfg)
        if memory_id:
            row = self.store.get(memory_id)
            if row is None:
                return 0
            if (row["scope_type"], row["scope_id"]) not in scopes:
                return 0
            self.store.delete(memory_id)
            self.store.audit(
                memory_id=memory_id,
                scope_type=str(row["scope_type"]),
                scope_id=str(row["scope_id"]),
                action="forget",
                actor="tool",
            )
            return 1
        if scope in {"user", "group"}:
            scopes = [pair for pair in scopes if pair[0] == scope]
        targets: list[dict[str, Any]] = []
        for scope_type, scope_id in scopes:
            for row in self.store.list_scope(
                scope_type,
                scope_id,
                statuses=("active", "candidate"),
                limit=RECALL_CANDIDATE_LIMIT,
            ):
                if slot_key and row.get("slot_key") != slot_key:
                    continue
                if query and query.lower() not in str(row.get("content", "")).lower():
                    continue
                targets.append(row)
        for row in targets:
            self.store.delete(str(row["id"]))
            self.store.audit(
                memory_id=str(row["id"]),
                scope_type=str(row["scope_type"]),
                scope_id=str(row["scope_id"]),
                action="forget",
                actor="tool",
            )
        return len(targets)

    def list_items(
        self,
        *,
        platform_id: str,
        sender_id: str,
        group_id: str,
        is_group: bool,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """列出当前用户可读的记忆（命令展示用）。"""
        scopes = self._scopes(platform_id, sender_id, group_id, is_group, self.cfg)
        rows: list[dict[str, Any]] = []
        for scope_type, scope_id in scopes:
            rows.extend(
                self.store.list_scope(
                    scope_type,
                    scope_id,
                    statuses=("active", "candidate"),
                    limit=limit,
                )
            )
        rows.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=True)
        return rows[:limit]

    def stats(self) -> dict[str, Any]:
        """返回存储巡检信息。"""
        scopes = self.store.list_scopes(limit=50)
        return {
            "scopes": len(scopes),
            "items": sum(int(item["count"]) for item in scopes),
            "top": scopes[:5],
        }

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _scopes(
        platform_id: str,
        sender_id: str,
        group_id: str,
        is_group: bool,
        cfg: LongTermConfig,
    ) -> list[tuple[str, str]]:
        scopes: list[tuple[str, str]] = []
        if is_group and cfg.recall_include_group_scope and group_id:
            scopes.append(("group", f"{platform_id}:{group_id}"))
        if sender_id:
            scopes.append(("user", f"{platform_id}:{sender_id}"))
        return scopes

    @staticmethod
    def _candidate_scope(
        job: dict[str, Any],
        candidate_scope: str,
    ) -> tuple[str, str]:
        platform_id = str(job.get("platform_id") or "")
        if candidate_scope == "group" and job.get("group_id"):
            return "group", f"{platform_id}:{job['group_id']}"
        sender_id = str(job.get("sender_id") or "")
        if sender_id:
            return "user", f"{platform_id}:{sender_id}"
        return "", ""

    @staticmethod
    def _not_expired(row: dict[str, Any], now: float) -> bool:
        expires = row.get("expires_at")
        return expires is None or float(expires) > now


__all__ = ["LongTermConfig", "LongTermMemory", "LongTermStore", "estimate_tokens"]
