"""异步归档：本轮结束后才发生的写操作（单 worker 串行）。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from ..config import Config
from .enums import TopicAction
from .memory_store import MemoryStore
from .relationship import RelationshipManager
from .store import Store

MAX_QUEUE = 256
MAX_RETRIES = 2
BACKOFF_BASE = 0.5

_ASSERTION_TEMPLATES = (
    ("我喜欢", 0.85),
    ("我不喜欢", 0.85),
    ("我爱吃", 0.85),
    ("我住在", 0.9),
    ("我是", 0.8),
    ("我叫", 0.95),
    ("我养了", 0.85),
)


@dataclass(frozen=True)
class ArchiveJob:
    """一轮归档任务（只含数据，不含事件对象）。"""

    turn_id: str
    session_id: str
    user_text: str
    response_text: str
    topic_action: str
    persona_mode: str
    emotional_salience: float
    sticky_hit: bool
    occurred_at: float
    old_topic_id: int | None = None
    decision_json: str = ""
    injected_tokens: int = 0
    dropped_json: str = ""
    #: 切分点之前该话题的整段对话（"用户：… / 助手：…"），供摘要使用。
    topic_transcript: str = ""


class Archiver:
    """单 worker 异步归档器。"""

    def __init__(
        self,
        store: Store,
        memory: MemoryStore,
        relationship: RelationshipManager,
        cfg: Config,
        *,
        summarizer: Any = None,
    ) -> None:
        """初始化。"""
        self._store = store
        self._memory = memory
        self._relationship = relationship
        self._cfg = cfg
        self._summarizer = summarizer
        self._queue: asyncio.Queue[ArchiveJob] = asyncio.Queue(maxsize=MAX_QUEUE)
        self._worker: asyncio.Task[None] | None = None
        self._closing = False

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """启动 worker（幂等）。"""
        if self._worker is None or self._worker.done():
            self._closing = False
            self._worker = asyncio.create_task(self._run())

    async def stop(self, timeout: float = 5.0) -> None:
        """停止 worker 并 flush 队列。"""
        self._closing = True
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    async def enqueue(self, job: ArchiveJob) -> bool:
        """入队（非阻塞）；队列满或已关闭返回 False。"""
        if self._closing:
            return False
        try:
            self._queue.put_nowait(job)
            return True
        except asyncio.QueueFull:
            return False

    async def archive_async(self, job: ArchiveJob) -> None:
        """外部入口（同步语义的兼容名）。"""
        await self.enqueue(job)

    # ------------------------------------------------------------------ worker
    async def _run(self) -> None:
        """worker 主循环；worker 自身永不退出。"""
        while True:
            job = await self._queue.get()
            try:
                await self._archive_one(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._queue.task_done()

    async def _archive_one(self, job: ArchiveJob) -> None:
        """执行一轮归档流水线（阶段失败互不影响）。"""
        await self._with_retry("summarize", job, lambda: self._summarize_topic(job))
        await self._with_retry("assert", job, lambda: self._extract_assertions(job))
        await self._with_retry("episode", job, lambda: self._detect_episode(job))
        await self._with_retry(
            "relationship", job, lambda: self._update_relationship(job)
        )
        await self._with_retry("log", job, lambda: self._log_turn(job))

    async def _with_retry(self, stage: str, job: ArchiveJob, fn: Any) -> Any:
        """带指数退避的重试包装；最终失败落 archive_failures。"""
        for attempt in range(MAX_RETRIES + 1):
            try:
                return await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if attempt >= MAX_RETRIES:
                    try:
                        self._store.insert_archive_failure(
                            turn_id=job.turn_id,
                            stage=stage,
                            error=str(exc),
                            payload=job.decision_json,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return None
                await asyncio.sleep(BACKOFF_BASE * (2**attempt))
        return None

    # ------------------------------------------------------------------ stages
    async def _summarize_topic(self, job: ArchiveJob) -> str | None:
        """为被切分（软切/硬切）的旧话题生成摘要并写回。

        v0.5.0 之前只有软切会调用到这里，且只喂"标题 + 最后一句前 120 字"；
        现在两个入口都会调用，输入改为切分点之前该话题的**整段对话**。
        """
        if not job.old_topic_id:
            return None
        topic_id = int(job.old_topic_id)
        topic = self._store.get_topic(topic_id)
        if not topic:
            return None
        title = str(topic.get("title") or "")
        transcript = (job.topic_transcript or "").strip()
        if not transcript:
            transcript = f"用户：{job.user_text[:500]}" if job.user_text else ""
        summary = self._fallback_summary(title, transcript)
        if self._summarizer is not None and transcript:
            prompt = build_summary_prompt(title, transcript)
            try:
                generated = await self._summarizer(prompt, job.session_id)
            except TypeError:
                # 兼容只接受一个参数的旧回调
                try:
                    generated = await self._summarizer(prompt)
                except Exception:  # noqa: BLE001
                    generated = ""
            except Exception:  # noqa: BLE001
                generated = ""
            if isinstance(generated, str) and len(generated.strip()) >= 40:
                summary = generated.strip()[:1200]
        self._store.update_topic(topic_id, summary=summary)
        self._memory.save_summary(
            job.session_id, topic_id, summary, job.turn_id, importance=0.8
        )
        return summary

    @staticmethod
    def _fallback_summary(title: str, transcript: str) -> str:
        """无模型/模型失败时的确定性摘要：保留可检索的具体信息。"""
        lines = [line.strip() for line in transcript.splitlines() if line.strip()]
        user_lines = [line for line in lines if line.startswith("用户：")]
        assistant_lines = [line for line in lines if line.startswith("助手：")]
        tail = " / ".join(line[2:].strip() for line in user_lines[-3:])[:300]
        said = (assistant_lines[-1][3:].strip() if assistant_lines else "")[:200]
        parts = [f"【{title or '未命名话题'}】"]
        if user_lines:
            parts.append(f"在解决什么：{tail or '未记录'}")
        if said:
            parts.append(f"已确认的结论与参数：{said}")
        parts.append("未完成事项：无" if len(user_lines) < 2 else "未完成事项：详见上下文")
        parts.append("踩过的坑：无")
        parts.append(f"（共 {len(user_lines)} 轮对话）")
        return "\n".join(parts)

    async def _extract_assertions(self, job: ArchiveJob) -> list[dict[str, Any]]:
        """规则抽取用户断言。"""
        results: list[dict[str, Any]] = []
        text = job.user_text or ""
        for template, base_confidence in _ASSERTION_TEMPLATES:
            index = text.find(template)
            if index < 0:
                continue
            value = text[index + len(template) :].strip(" 。！？,.!?")
            if not value:
                continue
            confidence = min(1.0, base_confidence)
            if confidence < self._cfg.memory.assertion_confidence_threshold:
                continue
            content = f"用户{template}{value}"
            self._memory.save_assertion(
                job.session_id,
                content,
                confidence,
                [value[:8]],
                job.turn_id,
            )
            results.append(
                {
                    "content": content,
                    "confidence": confidence,
                    "keywords": [value[:8]],
                }
            )
        return results

    async def _detect_episode(self, job: ArchiveJob) -> dict[str, Any] | None:
        """判定是否形成情节记忆。"""
        salience = abs(job.emotional_salience)
        if salience < self._cfg.memory.episode_salience_threshold and not job.sticky_hit:
            return None
        content = f"用户提到：{job.user_text[:80]}（情绪 {job.emotional_salience:+.2f}）"
        self._memory.save_episode(
            job.session_id,
            content,
            max(salience, 0.6),
            [],
            job.turn_id,
        )
        return {"content": content, "salience": max(salience, 0.6), "keywords": []}

    async def _update_relationship(self, job: ArchiveJob) -> None:
        """更新关系状态。"""
        if not self._cfg.relationship.enabled:
            return
        signal = SimpleNamespace(
            session_id=job.session_id,
            text=job.user_text,
            emotion_score=job.emotional_salience,
        )
        self._relationship.update_after_turn(signal, None, job.response_text)

    async def _log_turn(self, job: ArchiveJob) -> None:
        """写 turn_logs。"""
        self._store.insert_turn_log(
            turn_id=job.turn_id,
            session_id=job.session_id,
            decision_json=job.decision_json or "{}",
            injected_tokens=job.injected_tokens,
            dropped_json=job.dropped_json or None,
        )

def build_summary_prompt(title: str, transcript: str) -> str:
    """把话题标题 + 整段对话拼成摘要 prompt（模块级，便于单测）。"""
    header = f"话题标题：{title or '（未命名）'}\n"
    body = (
        "对话片段（按时间顺序，用户/助手交替）：\n"
        + transcript
        + "\n\n请按系统提示的四段结构输出摘要。"
    )
    return header + body
