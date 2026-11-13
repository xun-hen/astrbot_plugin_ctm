"""话题状态机：软边界（压栈）、回溯（弹栈）、硬切（显式命令）。"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..config import TopicConfig
from .enums import TopicStatus
from .textsim import SimilarityScorer, topic_similarity

_PUNCT_RE = re.compile(r"[\s，。,.!?！？、；;：:\"'“”‘’（）()【】\[\]…~]+")


@dataclass
class TopicNode:
    """话题栈里的一个节点。"""

    topic_id: int
    title: str
    summary: str = ""
    status: TopicStatus = TopicStatus.ACTIVE
    created_at: float = 0.0
    last_active_at: float = 0.0
    topic_key: str | None = None


@dataclass
class TopicState:
    """一个会话的话题栈快照（栈顶为当前焦点）。"""

    session_id: str
    stack: list[TopicNode] = field(default_factory=list)
    current_topic: str = ""
    previous_topic: str | None = None
    turns_since_switch: int = 0
    last_switch_at: float = 0.0
    soft_boundary_pending: bool = False

    def focus(self) -> TopicNode | None:
        """返回当前焦点话题节点。"""
        return self.stack[-1] if self.stack else None

    def titles(self, limit: int | None = None) -> list[str]:
        """返回自栈顶向下的标题列表。"""
        titles = [node.title for node in reversed(self.stack)]
        return titles if limit is None else titles[:limit]


class TopicStateMachine:
    """话题栈的唯一写入者。"""

    def __init__(
        self,
        store: Any,
        cfg: TopicConfig,
        scorer: SimilarityScorer | None = None,
    ) -> None:
        """初始化。

        Args:
            store: `ctm.store.Store` 实例。
            cfg: 话题配置。
            scorer: 相似度打分器；None 时用词法兜底。
        """
        self._store = store
        self._cfg = cfg
        self._scorer = scorer

    def load(self, session_id: str) -> TopicState:
        """从库中恢复会话话题栈；读失败返回空状态。"""
        try:
            rows = self._store.load_topic_stack(session_id)
            session_state = self._store.load_session_state(session_id)
        except Exception:  # noqa: BLE001
            return TopicState(session_id=session_id)
        stack = [
            TopicNode(
                topic_id=int(row["id"]),
                title=str(row.get("title") or ""),
                summary=str(row.get("summary") or ""),
                status=TopicStatus(str(row.get("status") or "active")),
                created_at=float(row.get("created_at") or 0),
                last_active_at=float(row.get("last_active_at") or 0),
                topic_key=row.get("topic_key"),
            )
            for row in rows
        ]
        return TopicState(
            session_id=session_id,
            stack=stack,
            current_topic=stack[-1].title if stack else "",
            turns_since_switch=int(session_state.get("turns_since_switch", 0)),
            last_switch_at=float(session_state.get("last_switch_at", 0.0)),
            soft_boundary_pending=bool(
                session_state.get("soft_boundary_pending", False)
            ),
        )

    def save(self, state: TopicState) -> None:
        """把状态写回库（一次事务）。"""
        self._store.save_topic_stack(
            state.session_id, [node.topic_id for node in state.stack]
        )
        self._store.save_session_state(
            state.session_id,
            turns_since_switch=state.turns_since_switch,
            last_switch_at=state.last_switch_at,
            soft_boundary_pending=state.soft_boundary_pending,
        )

    def should_soft_boundary(
        self,
        signal: Any,
        state: TopicState,
        verdict: Any | None = None,
    ) -> bool:
        """判定本轮是否触发软边界。

        Args:
            signal: PerceptionSignal。
            state: 当前话题状态。
            verdict: 话题判定结果（`topic_judge.Verdict`）；None 时退回纯启发式。

        Returns:
            是否软切。
        """
        cfg = self._cfg
        if not cfg.auto_switch_enabled:
            return False
        if getattr(signal, "is_command", False):
            return False
        if self.is_sticky(signal):
            return False
        if int(getattr(signal, "length", 0) or 0) < cfg.judge_min_message_length:
            return False
        if state.turns_since_switch < cfg.min_turns_before_switch:
            return False
        now = restore_clock()
        if state.last_switch_at and now - state.last_switch_at < cfg.switch_cooldown_seconds:
            return False
        if not state.stack:
            return False
        if verdict is not None:
            return bool(getattr(verdict, "is_new_topic", False))
        current = state.focus()
        focus_text = f"{current.title} {current.summary}".strip() if current else ""
        similarity = topic_similarity(focus_text, signal.text, self._scorer)
        if similarity >= cfg.topic_threshold:
            return False
        new_intent = detect_new_intent(signal.text, cfg.new_intent_markers)
        return new_intent or similarity < cfg.topic_threshold * 0.7

    def soft_boundary(
        self, signal: Any, state: TopicState, new_topic_title: str
    ) -> TopicState:
        """压栈并建立新话题（不清空上下文）。"""
        now = restore_clock()
        old = state.focus()
        title = new_topic_title.strip() or self.title_for(getattr(signal, "text", ""))
        new_id = self._store.create_topic(state.session_id, title)
        if old is not None:
            self._store.update_topic(
                old.topic_id,
                status=TopicStatus.ARCHIVED.value,
                last_active_at=now,
            )
        node = TopicNode(
            topic_id=new_id,
            title=title,
            status=TopicStatus.ACTIVE,
            created_at=now,
            last_active_at=now,
        )
        state.stack.append(node)
        state.current_topic = title
        state.previous_topic = old.title if old else None
        state.turns_since_switch = 0
        state.last_switch_at = now
        state.soft_boundary_pending = True
        self.save(state)
        return state

    def should_pop_back(self, signal: Any, state: TopicState) -> bool:
        """判定用户是否在回溯旧话题。"""
        if len(state.stack) < 2:
            return False
        if detect_new_intent(signal.text, self._cfg.new_intent_markers):
            return False
        current = state.focus()
        focus_text = f"{current.title} {current.summary}".strip() if current else ""
        if topic_similarity(focus_text, signal.text, self._scorer) >= self._cfg.topic_threshold:
            return False
        best = 0.0
        for node in state.stack[:-1]:
            best = max(
                best,
                topic_similarity(f"{node.title} {node.summary}", signal.text, self._scorer),
            )
        return best >= self._cfg.topic_threshold

    def pop_to_previous(self, signal: Any, state: TopicState) -> TopicState:
        """用户回溯旧话题：弹栈并把被弹话题恢复为活跃。"""
        if len(state.stack) < 2:
            return state
        now = restore_clock()
        popped = state.stack.pop()
        self._store.update_topic(
            popped.topic_id,
            status=TopicStatus.POPPED.value,
            last_active_at=now,
        )
        restored = state.focus()
        if restored is not None:
            restored.status = TopicStatus.ACTIVE
            self._store.update_topic(
                restored.topic_id,
                status=TopicStatus.ACTIVE.value,
                last_active_at=now,
            )
            state.current_topic = restored.title
        state.previous_topic = popped.title
        state.turns_since_switch = 0
        state.last_switch_at = now
        state.soft_boundary_pending = True
        self.save(state)
        return state

    def hard_switch(self, signal: Any, state: TopicState) -> TopicState:
        """执行硬切换（清空栈，旧话题保留为 archived）。"""
        now = restore_clock()
        for node in state.stack:
            self._store.update_topic(
                node.topic_id,
                status=TopicStatus.ARCHIVED.value,
                last_active_at=now,
            )
        state.stack = []
        state.current_topic = ""
        state.previous_topic = None
        state.turns_since_switch = 0
        state.last_switch_at = now
        state.soft_boundary_pending = False
        self.save(state)
        return state

    def title_for(self, text: str) -> str:
        """为新话题取规则标题。"""
        cleaned = _PUNCT_RE.sub(" ", text or "").strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if not cleaned:
            return "新话题"
        return cleaned[:16] + ("…" if len(cleaned) > 16 else "")

    def is_sticky(self, signal: Any) -> bool:
        """判断粘性命中。"""
        return bool(getattr(signal, "sticky_hit", False))


def detect_new_intent(text: str, markers: Iterable[str]) -> bool:
    """规则判断是否出现"新意图"线索。"""
    stripped = (text or "").strip()
    if not stripped:
        return False
    return any(marker and marker in stripped for marker in markers)


def restore_clock(now: float | None = None) -> float:
    """统一的时间来源，便于测试注入。"""
    return time.time() if now is None else float(now)
