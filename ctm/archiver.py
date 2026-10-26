"""异步归档：本轮结束后才发生的写操作。

设计文档 §3.7 的流水线（全部在后台单 worker 里串行执行）：

1. 若发生软边界 → 为旧话题生成摘要；
2. 断言抽取（规则优先，置信度阈值过滤）；
3. 情节识别（情绪强度 / 粘性命中）；
4. 更新关系状态；
5. 写 turn_logs。

三条工程约束：

* **不持有事件对象**：`ArchiveJob` 只带文本与不可变快照。事件对象上挂着平台
  连接与消息链，进队列会让"归档慢"变成"内存泄漏 + 连接被拖住"。
* **失败不阻塞下一轮**：重试 2 次（指数退避）后落 `archive_failures`，
  然后丢弃该 job。归档是"尽力而为"，不是事务。
* **卸载要 flush**：`stop(timeout=5)` 先关队列入口，再等 worker 收尾，
  超时就取消并记 WARN（宁可丢一轮归档，不可卡住 AstrBot 卸载）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..config import Config
from .memory_store import MemoryStore
from .relationship import RelationshipManager
from .store import Store

#: 队列上限。超过后丢弃并记 WARN_ARCHIVE_DROPPED——背压不能反压到消息入口。
MAX_QUEUE = 256
#: 单阶段重试次数（不含首次）。
MAX_RETRIES = 2
#: 指数退避基数（秒）。
BACKOFF_BASE = 0.5


@dataclass(frozen=True)
class ArchiveJob:
    """一轮归档任务（只含数据，不含事件对象）。

    Attributes:
        turn_id: 轮次 ID。
        session_id: 会话键。
        user_text: 本轮用户文本（聚合后）。
        response_text: 模型回复文本。
        topic_action: 本轮话题动作（决定是否要给旧话题写摘要）。
        persona_mode: 人格模式（写日志用）。
        emotional_salience: 本轮情绪强度绝对值，用于情节判定。
        sticky_hit: 是否粘性命中。
        occurred_at: 轮次发生时间。
        old_topic_id: 若发生软边界，被压栈的旧话题主键。
        decision_json: `TurnDecision.to_dict()` 的序列化结果（写 turn_logs 用）。
        injected_tokens: 本轮实际注入估算（写 turn_logs 用）。
        dropped_json: 被丢弃候选的序列化结果。
    """

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
        """初始化。

        Args:
            store: 持久层。
            memory: 记忆写入门面。
            relationship: 关系状态门面。
            cfg: 全量配置。
            summarizer: 可选的摘要函数 `async (texts) -> str`；None 时用规则摘要。
                由插件层注入（通常复用当前会话的轻量模型），便于单测替换。
        """
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
        """启动 worker。

        Returns:
            None。

        TODO(v0.1): 幂等；`self._worker = asyncio.create_task(self._run())`。
        """
        raise NotImplementedError("v0.1: Archiver.start")

    async def stop(self, timeout: float = 5.0) -> None:
        """停止 worker 并 flush 队列。

        Args:
            timeout: 等待收尾的秒数；超时取消并记 WARN。

        Returns:
            None。

        TODO(v0.1): 先置 `_closing=True` 拒绝新任务，再 `queue.join()` 超时等待，
            最后 cancel worker；重复调用幂等。
        """
        raise NotImplementedError("v0.1: Archiver.stop")

    async def enqueue(self, job: ArchiveJob) -> bool:
        """入队（非阻塞）。

        Args:
            job: 归档任务。

        Returns:
            入队成功返回 True；队列满或已关闭时返回 False 并记
            `WARN_ARCHIVE_DROPPED`（**不抛异常**）。

        TODO(v0.1): `put_nowait` + except QueueFull。
        """
        raise NotImplementedError("v0.1: Archiver.enqueue")

    async def archive_async(self, job: ArchiveJob) -> None:
        """外部唯一入口（同步语义的兼容名）。

        Args:
            job: 归档任务。

        Returns:
            None。

        TODO(v0.1): 直接转调 `enqueue`，保留文档 §3.7 的方法名。
        """
        raise NotImplementedError("v0.1: Archiver.archive_async")

    # ------------------------------------------------------------------ worker
    async def _run(self) -> None:
        """worker 主循环。

        Returns:
            None。

        TODO(v0.1): 循环 `job = await queue.get()`，`_archive_one(job)` 后
            `task_done()`；逐 job 捕获异常，worker 本身永不退出。
        """
        raise NotImplementedError("v0.1: Archiver._run")

    async def _archive_one(self, job: ArchiveJob) -> None:
        """执行一轮归档流水线。

        Args:
            job: 归档任务。

        Returns:
            None。

        Raises:
            None: 内部阶段各自捕获，最终失败落 archive_failures。

        TODO(v0.1): 按文档 §3.7 的五步执行；每阶段独立 try/except，
            单个阶段失败不阻止后续阶段（例如摘要失败仍应更新关系）。
        """
        raise NotImplementedError("v0.1: Archiver._archive_one")

    async def _with_retry(self, stage: str, job: ArchiveJob, fn: Any) -> Any:
        """带指数退避的重试包装。

        Args:
            stage: 阶段名（写 archive_failures 用）。
            job: 任务。
            fn: 无参可等待函数。

        Returns:
            函数返回值；最终失败返回 None。

        TODO(v0.1): 重试 `MAX_RETRIES` 次，间隔 `BACKOFF_BASE * 2**attempt`。
        """
        raise NotImplementedError("v0.1: Archiver._with_retry")

    # ------------------------------------------------------------------ stages
    async def _summarize_topic(self, job: ArchiveJob) -> str | None:
        """为被软切掉的旧话题生成摘要。

        Args:
            job: 归档任务（old_topic_id 非空时才执行）。

        Returns:
            摘要文本；无需摘要或失败返回 None。

        TODO(v0.1): 从 store 取旧话题的最近消息文本，调 summarizer 或规则截断；
            写回 topics.summary 并同步一条 memory(summary)。
        """
        raise NotImplementedError("v0.1: Archiver._summarize_topic")

    async def _extract_assertions(self, job: ArchiveJob) -> list[dict[str, Any]]:
        """规则抽取用户断言。

        Args:
            job: 归档任务。

        Returns:
            断言列表，元素形如 `{"content": "...", "confidence": 0.8, "keywords": [...]}`；
            置信度低于阈值的不返回（只记 DEBUG）。

        TODO(v0.1): 规则模板（"我喜欢/我不喜欢/我是/我住在/我养了"）；
            置信度 = 模板强度 × 句子完整度。
        """
        raise NotImplementedError("v0.1: Archiver._extract_assertions")

    async def _detect_episode(self, job: ArchiveJob) -> dict[str, Any] | None:
        """判定是否形成情节记忆。

        Args:
            job: 归档任务。

        Returns:
            `{"content": "...", "salience": 0.7, "keywords": [...]}`；不构成情节返回 None。

        TODO(v0.1): `abs(emotional_salience) >= episode_salience_threshold`
            或 sticky_hit；正文由本轮用户文本 + 回复摘要拼成，不是全文照抄。
        """
        raise NotImplementedError("v0.1: Archiver._detect_episode")

    async def _update_relationship(self, job: ArchiveJob) -> None:
        """更新关系状态。

        Args:
            job: 归档任务。

        Returns:
            None。

        TODO(v0.1): 委托 RelationshipManager.update_after_turn 并 save；
            关系模块关闭时直接返回。
        """
        raise NotImplementedError("v0.1: Archiver._update_relationship")

    async def _log_turn(self, job: ArchiveJob) -> None:
        """写 turn_logs。

        Args:
            job: 归档任务。

        Returns:
            None。

        TODO(v0.1): 决策 JSON 直接用 job.decision_json / job.dropped_json /
            job.injected_tokens，不要在这里重新访问决策对象（它已随事件回收）。
        """
        raise NotImplementedError("v0.1: Archiver._log_turn")
