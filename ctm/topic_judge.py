"""话题判定：启发式（免费）+ 可选 LLM 兜底（借鉴话题路由器 PRO，MIT）。

原则与 PRO 一致：**能白判就白判，判不准才问模型，问不出来就当延续。**
误判代价不对称——把"延续"判成"新话题"会把用户正在聊的上下文切走，
所以只有两个信号都指向新话题、且置信度足够时才输出新话题。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

VERDICT_CONTINUE = "continue"
VERDICT_NEW_TOPIC = "new_topic"

CONTINUATION_MARKERS = (
    "继续",
    "接着",
    "然后",
    "还有",
    "另外",
    "再",
    "刚才",
    "刚刚",
    "上面",
    "前面",
    "之前",
    "那个",
    "这个",
    "它",
    "他",
    "她",
    "同样",
    "还是",
    "改成",
    "换成",
    "再来",
    "补充",
    "不对",
    "但是",
    "不过",
    "所以呢",
)

NEW_TOPIC_MARKERS = (
    "换个话题",
    "换一个话题",
    "另一个问题",
    "另一个话题",
    "顺便问",
    "顺便说",
    "再问你一个",
    "问个别的事",
    "说个别的事",
    "不聊这个",
    "不说这个",
    "先不说这个",
    "突然想到",
    "另外一件事",
    "还有个事",
    "还有个问题",
    "新问题",
    "求助一个",
    "new topic",
    "change the subject",
)

PRONOUNS = ("它", "他", "她", "这个", "那个", "这些", "那些", "上面", "前面", "刚才")

_WS_RE = re.compile(r"\s+")
_DIGIT_RE = re.compile(r"\d+")
_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff]+")

CLASSIFIER_SYSTEM_PROMPT = """你是一个话题切分器。你要判断用户最新的一句话，是在继续前面正在聊的那件事，还是开启了另一件不相干的事。

判断标准：
- 如果新消息在追问、补充、纠正、延续当前话题，判 continue。
- 如果新消息讨论的是另一件独立的事，与当前话题没有承接关系，判 new_topic。
- 只有代词的短句（如「那它呢」）属于 continue。
- 拿不准时一律判 continue。

只输出一个 JSON 对象，不要输出任何其他文字、解释或 markdown 代码块，格式：
{"verdict":"continue"|"new_topic","confidence":0.0到1.0,"topic_name":"当前话题的简短名称","topic_summary":"一句话说明这一段在聊什么","reason":"不超过30字的理由"}

当 verdict 是 new_topic 时，topic_name 与 topic_summary 描述的是新话题；当 verdict 是 continue 时，描述的是延续后的当前话题。"""


@dataclass(frozen=True)
class HeuristicResult:
    """启发式判定结果。"""

    verdict: str
    confidence: float
    similarity: float
    needs_llm: bool
    reason: str


@dataclass(frozen=True)
class Verdict:
    """最终判定结果。"""

    verdict: str
    confidence: float
    source: str
    similarity: float = 0.0
    reason: str = ""
    topic_label: str = ""
    topic_summary: str = ""

    @property
    def is_new_topic(self) -> bool:
        """是否判为新话题。"""
        return self.verdict == VERDICT_NEW_TOPIC


def normalize(text: str) -> str:
    """去空白/标点、数字折叠，便于比较。"""
    cleaned = _PUNCT_RE.sub("", _WS_RE.sub("", (text or "").lower()))
    return _DIGIT_RE.sub("0", cleaned)


def bigrams(text: str) -> set[str]:
    """字符 2-gram 集合；长度不足 2 时退化为单字集合。"""
    if len(text) < 2:
        return {text} if text else set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


def similarity(left: str, right: str) -> float:
    """字符 2-gram Jaccard 相似度。"""
    a = bigrams(normalize(left))
    b = bigrams(normalize(right))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _state_text(state: Any) -> str:
    """从 TopicState 取当前话题的标题 + 摘要。"""
    focus = state.focus() if hasattr(state, "focus") else None
    title = getattr(focus, "title", None) or getattr(state, "current_topic", "") or ""
    summary = getattr(focus, "summary", "") or ""
    return f"{title} {summary}".strip()


def heuristic(
    message: str,
    state: Any,
    *,
    now: float | None = None,
    sim_continue: float = 0.5,
    sim_new_topic: float = 0.12,
    time_gap_minutes: int = 45,
    min_message_length: int = 6,
    short_message_length: int = 25,
    always_llm: bool = False,
) -> HeuristicResult:
    """零成本的一层判定。

    Args:
        message: 新消息正文。
        state: `TopicState`。
        now: 时间戳，测试可注入。
        sim_continue: 高于它直接判延续。
        sim_new_topic: 低于它且无承接词才判新话题。
        time_gap_minutes: 间隔超过它时交给模型判（不凭时间硬切）。
        min_message_length: 短于此不判定。
        short_message_length: 短句字面不重叠时交给模型。
        always_llm: 每轮都问模型。

    Returns:
        HeuristicResult。
    """
    now = time.time() if now is None else now
    text = (message or "").strip()
    norm = normalize(text)
    topic_text = _state_text(state)
    has_topic = bool(topic_text)
    focus = state.focus() if hasattr(state, "focus") else None
    last_ts = float(getattr(focus, "last_active_at", 0.0) or 0.0)
    gap_minutes = (now - last_ts) / 60 if last_ts else 0.0

    if not has_topic:
        return HeuristicResult(
            verdict=VERDICT_NEW_TOPIC,
            confidence=0.9,
            similarity=0.0,
            needs_llm=False,
            reason="会话内还没有话题，先建档",
        )

    if always_llm:
        return HeuristicResult(
            verdict=VERDICT_CONTINUE,
            confidence=0.5,
            similarity=similarity(text, topic_text),
            needs_llm=True,
            reason="配置要求每轮都问模型",
        )

    sim = similarity(text, topic_text)

    # 长时间未回复：不凭时间硬切，交模型判"下一句是否延续"。
    if last_ts and gap_minutes >= time_gap_minutes:
        if sim >= sim_continue:
            return HeuristicResult(
                verdict=VERDICT_CONTINUE,
                confidence=min(0.95, 0.6 + sim / 2),
                similarity=sim,
                needs_llm=False,
                reason=f"间隔 {gap_minutes:.0f} 分钟但字面高度重叠（{sim:.2f}）",
            )
        return HeuristicResult(
            verdict=VERDICT_CONTINUE,
            confidence=0.5,
            similarity=sim,
            needs_llm=True,
            reason=f"间隔 {gap_minutes:.0f} 分钟：交模型判断是否延续",
        )

    if len(norm) < max(1, min_message_length):
        return HeuristicResult(
            verdict=VERDICT_CONTINUE,
            confidence=0.75 if any(word in text for word in PRONOUNS) else 0.65,
            similarity=sim,
            needs_llm=False,
            reason=f"短消息（{len(norm)} 字），按延续处理",
        )

    lowered = text.lower()
    explicit_new = next((word for word in NEW_TOPIC_MARKERS if word in lowered), "")
    if explicit_new:
        return HeuristicResult(
            verdict=VERDICT_NEW_TOPIC,
            confidence=0.95,
            similarity=sim,
            needs_llm=False,
            reason=f"命中显式换话题词「{explicit_new}」",
        )

    if sim >= sim_continue:
        return HeuristicResult(
            verdict=VERDICT_CONTINUE,
            confidence=min(0.95, 0.6 + sim / 2),
            similarity=sim,
            needs_llm=False,
            reason=f"字面相似度 {sim:.2f}，明显同一件事",
        )

    marker = next(
        (
            word
            for word in CONTINUATION_MARKERS
            if text.startswith(word) or f"，{word}" in text or f"。{word}" in text
        ),
        "",
    )

    if len(norm) <= 12 and any(word in text for word in PRONOUNS):
        return HeuristicResult(
            verdict=VERDICT_CONTINUE,
            confidence=0.75,
            similarity=sim,
            needs_llm=False,
            reason="极短且含指代词，判为延续",
        )

    if len(norm) <= max(min_message_length, short_message_length) and not marker:
        return HeuristicResult(
            verdict=VERDICT_CONTINUE,
            confidence=0.5,
            similarity=sim,
            needs_llm=True,
            reason=f"短句（{len(norm)} 字）字面不重叠（{sim:.2f}），交模型判语义",
        )

    if marker and sim > sim_new_topic:
        return HeuristicResult(
            verdict=VERDICT_CONTINUE,
            confidence=0.6,
            similarity=sim,
            needs_llm=False,
            reason=f"命中承接词「{marker}」且有一定重叠（{sim:.2f}）",
        )

    if sim <= sim_new_topic and not marker:
        return HeuristicResult(
            verdict=VERDICT_NEW_TOPIC,
            confidence=0.7,
            similarity=sim,
            needs_llm=False,
            reason=f"与当前话题几乎不重叠（{sim:.2f}）",
        )

    return HeuristicResult(
        verdict=VERDICT_CONTINUE,
        confidence=0.5,
        similarity=sim,
        needs_llm=True,
        reason=f"落在模糊区间（{sim:.2f}），交给模型",
    )


def build_user_prompt(
    message: str,
    state: Any,
    recent: list[str],
    history_turns: int = 3,
    last_assistant: str = "",
    idle_minutes: float = 0.0,
) -> str:
    """拼出判定模型的用户侧内容。"""
    focus = state.focus() if hasattr(state, "focus") else None
    label = (getattr(focus, "title", "") or getattr(state, "current_topic", "")).strip()
    summary = (getattr(focus, "summary", "") or "").strip()
    lines = [f"当前话题：{label or '（还没有话题）'}"]
    if summary:
        lines.append(f"话题说明：{summary}")
    tail = [item for item in (recent or []) if item][-max(0, history_turns) :]
    if tail:
        lines.append("最近几条用户消息（旧到新）：")
        lines.extend(f"- {item[:200]}" for item in tail)
    if last_assistant:
        lines.append(f"机器人上一句回复：{last_assistant[:300]}")
    if idle_minutes > 0:
        lines.append(
            f"距上一条消息已过 {idle_minutes:.0f} 分钟"
            "（隔得久不代表换话题：若在接着做同一件事，判 continue）"
        )
    lines.append(f"用户最新消息：{message[:800]}")
    lines.append("请输出判定 JSON。")
    return "\n".join(lines)


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出第一个 JSON 对象。"""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_verdict(text: str, fallback: HeuristicResult) -> Verdict | None:
    """把模型输出解析成 Verdict。"""
    data = extract_json(text)
    if data is None:
        return None
    raw = str(data.get("verdict", "")).strip().lower()
    if raw in {"new_topic", "new", "newtopic", "switch", "break"}:
        verdict = VERDICT_NEW_TOPIC
    elif raw in {"continue", "continues", "same", "same_topic", "follow_up"}:
        verdict = VERDICT_CONTINUE
    else:
        return None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return Verdict(
        verdict=verdict,
        confidence=max(0.0, min(1.0, confidence)),
        source="llm",
        similarity=fallback.similarity,
        reason=str(data.get("reason") or "模型判定")[:120],
        topic_label=str(data.get("topic_name") or data.get("topic") or "").strip()[:40],
        topic_summary=str(data.get("topic_summary") or data.get("summary") or "").strip()[
            :200
        ],
    )


def combine(fallback: HeuristicResult, llm: Verdict | None) -> Verdict:
    """合并启发式与模型结果（不对称原则：只有双信号才判新话题）。"""
    if llm is None:
        return Verdict(
            verdict=fallback.verdict,
            confidence=fallback.confidence,
            source="heuristic",
            similarity=fallback.similarity,
            reason=fallback.reason,
        )
    if llm.verdict == VERDICT_NEW_TOPIC:
        if fallback.verdict == VERDICT_NEW_TOPIC:
            return Verdict(
                verdict=VERDICT_NEW_TOPIC,
                confidence=min(llm.confidence, fallback.confidence),
                source="llm",
                similarity=fallback.similarity,
                reason=f"{llm.reason}（启发式也判新话题）",
                topic_label=llm.topic_label,
                topic_summary=llm.topic_summary,
            )
        if llm.confidence >= 0.8:
            return Verdict(
                verdict=VERDICT_NEW_TOPIC,
                confidence=min(llm.confidence, 0.8),
                source="llm",
                similarity=fallback.similarity,
                reason=f"{llm.reason}（启发式判延续，模型很确定）",
                topic_label=llm.topic_label,
                topic_summary=llm.topic_summary,
            )
        return Verdict(
            verdict=VERDICT_CONTINUE,
            confidence=0.7,
            source="llm",
            similarity=fallback.similarity,
            reason=f"模型倾向新话题但置信度仅 {llm.confidence:.2f}，保守按延续",
        )
    return Verdict(
        verdict=VERDICT_CONTINUE,
        confidence=max(0.5, llm.confidence),
        source="llm",
        similarity=fallback.similarity,
        reason=llm.reason,
        topic_label=llm.topic_label,
        topic_summary=llm.topic_summary,
    )


def apply_threshold(verdict: Verdict, floor: float) -> Verdict:
    """低于门槛的新话题判定压回延续。"""
    if verdict.is_new_topic and verdict.confidence < floor:
        return Verdict(
            verdict=VERDICT_CONTINUE,
            confidence=1.0 - verdict.confidence,
            source=verdict.source,
            similarity=verdict.similarity,
            reason=f"新话题置信度 {verdict.confidence:.2f} < {floor:.2f}，保守按延续",
        )
    return verdict


ProviderGetter = Callable[[str], Awaitable[Any]]


class TopicJudge:
    """启发式 + 可选 LLM 的话题判定门面。"""

    def __init__(
        self,
        *,
        provider_getter: ProviderGetter,
        provider_id: str = "",
        timeout_seconds: float = 8.0,
        max_checks_per_hour: int = 30,
        history_turns: int = 3,
        include_assistant_reply: bool = True,
        always_llm: bool = False,
        sim_continue: float = 0.5,
        sim_new_topic: float = 0.12,
        time_gap_minutes: int = 45,
        min_message_length: int = 6,
        short_message_length: int = 25,
        min_new_confidence: float = 0.6,
    ) -> None:
        """初始化。

        Args:
            provider_getter: 异步函数，接收会话 ID 返回 chat provider。
            provider_id: 指定判定模型；空串表示沿用当前会话模型。
            timeout_seconds: 模型调用超时，超时按延续。
            max_checks_per_hour: 每会话每小时模型判定上限，0 不限。
            history_turns: 提示词里带的最近用户消息条数。
            include_assistant_reply: 是否带机器人上一句回复。
            always_llm: 每轮都问模型。
            sim_continue: 直接判延续的相似度阈值。
            sim_new_topic: 直接判新话题的相似度阈值。
            time_gap_minutes: 长间隔交模型判的阈值。
            min_message_length: 短于此不判定。
            short_message_length: 短句交模型判的长度上限。
            min_new_confidence: 新话题的最低置信度。
        """
        self._provider_getter = provider_getter
        self.provider_id = provider_id
        self.timeout_seconds = timeout_seconds
        self.max_checks_per_hour = max_checks_per_hour
        self.history_turns = history_turns
        self.include_assistant_reply = include_assistant_reply
        self.always_llm = always_llm
        self.sim_continue = sim_continue
        self.sim_new_topic = sim_new_topic
        self.time_gap_minutes = time_gap_minutes
        self.min_message_length = min_message_length
        self.short_message_length = short_message_length
        self.min_new_confidence = min_new_confidence
        self._checks: dict[str, deque[float]] = {}

    async def judge(
        self,
        *,
        session_id: str,
        message: str,
        state: Any,
        recent: list[str] | None = None,
        last_assistant: str = "",
    ) -> Verdict:
        """产出一轮话题判定。

        Args:
            session_id: 会话键（限流用）。
            message: 用户最新消息。
            state: 当前话题状态。
            recent: 最近用户消息（旧到新）。
            last_assistant: 机器人上一句回复。

        Returns:
            Verdict；任何失败都退化成启发式/延续。
        """
        fallback = heuristic(
            message,
            state,
            sim_continue=self.sim_continue,
            sim_new_topic=self.sim_new_topic,
            time_gap_minutes=self.time_gap_minutes,
            min_message_length=self.min_message_length,
            short_message_length=self.short_message_length,
            always_llm=self.always_llm,
        )
        if not fallback.needs_llm:
            return combine(fallback, None)
        if not self._allow_check(session_id):
            return combine(fallback, None)
        provider = await self._provider_getter(session_id)
        if provider is None:
            return combine(fallback, None)
        prompt = build_user_prompt(
            message,
            state,
            list(recent or []),
            self.history_turns,
            last_assistant if self.include_assistant_reply else "",
            _idle_minutes(state),
        )
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    system_prompt=CLASSIFIER_SYSTEM_PROMPT,
                    contexts=[{"role": "user", "content": prompt}],
                    request_max_retries=1,
                ),
                timeout=max(1.0, self.timeout_seconds),
            )
        except Exception:  # noqa: BLE001 - 判定失败绝不影响回复
            return combine(fallback, None)
        llm = parse_verdict(
            getattr(response, "completion_text", "") or "",
            fallback,
        )
        return apply_threshold(combine(fallback, llm), self.min_new_confidence)

    def _allow_check(self, session_id: str) -> bool:
        if self.max_checks_per_hour <= 0:
            return True
        now = time.monotonic()
        events = self._checks.setdefault(session_id, deque())
        while events and now - events[0] > 3600:
            events.popleft()
        if len(events) >= self.max_checks_per_hour:
            return False
        events.append(now)
        return True


def _idle_minutes(state: Any) -> float:
    focus = state.focus() if hasattr(state, "focus") else None
    last_ts = float(getattr(focus, "last_active_at", 0.0) or 0.0)
    if not last_ts:
        return 0.0
    return max(0.0, (time.time() - last_ts) / 60)
