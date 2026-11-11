"""感知层：把一条平台消息压成 `PerceptionSignal`（纯规则、无 IO）。"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..config import TopicConfig
from .enums import EmotionHint

NEGATIVE_WORDS: tuple[str, ...] = (
    "难过",
    "伤心",
    "生气",
    "愤怒",
    "焦虑",
    "害怕",
    "崩溃",
    "疲惫",
    "累了",
    "烦躁",
    "烦",
    "讨厌",
    "孤独",
    "委屈",
    "失望",
    "痛苦",
    "压力",
    "失眠",
)
POSITIVE_WORDS: tuple[str, ...] = (
    "开心",
    "高兴",
    "喜欢",
    "爱你",
    "谢谢",
    "太好了",
    "幸福",
    "激动",
    "期待",
    "感动",
    "满意",
    "哈哈",
)

COMMAND_PREFIXES_DEFAULT: tuple[str, ...] = ("/",)


@dataclass(frozen=True)
class PerceptionSignal:
    """单条消息的感知结果（不可变）。"""

    session_id: str
    user_id: str
    message_id: str
    text: str
    timestamp: float
    is_command: bool = False
    sticky_hit: bool = False
    sticky_keywords: tuple[str, ...] = ()
    emotion_hint: EmotionHint = EmotionHint.UNKNOWN
    emotion_score: float = 0.0
    length: int = 0
    has_question: bool = False


@dataclass(frozen=True)
class PerceptionConfig:
    """感知层自己的配置视图。"""

    sticky_keywords: tuple[str, ...] = ()
    sticky_regex: tuple[str, ...] = ()
    command_prefixes: tuple[str, ...] = COMMAND_PREFIXES_DEFAULT
    min_length_for_judgement: int = 6


def _text_of(event: Any) -> str:
    """尽力从事件对象取纯文本，缺失时返回空串。"""
    text = getattr(event, "message_str", None)
    if isinstance(text, str):
        return text
    getter = getattr(event, "get_message_str", None)
    if callable(getter):
        try:
            value = getter()
            return value if isinstance(value, str) else ""
        except Exception:  # noqa: BLE001
            return ""
    return ""


class Perception:
    """规则感知器。"""

    def __init__(
        self,
        cfg: TopicConfig,
        command_prefixes: Sequence[str] = COMMAND_PREFIXES_DEFAULT,
    ) -> None:
        """初始化并预编译粘性正则；坏正则跳过，不影响启动。

        Args:
            cfg: 话题配置。
            command_prefixes: 指令前缀。
        """
        self._cfg = cfg
        self._prefixes = tuple(command_prefixes)
        self._sticky_regexes: list[re.Pattern[str]] = []
        for pattern in cfg.sticky_regex:
            try:
                self._sticky_regexes.append(re.compile(pattern))
            except re.error:
                continue

    def extract(self, event: Any, *, now: float | None = None) -> PerceptionSignal:
        """把事件压成感知信号。

        Args:
            event: AstrBot 消息事件或测试替身。
            now: 时间戳；缺省取当前时间。

        Returns:
            `PerceptionSignal`。
        """
        text = _text_of(event).strip()
        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        user_id = ""
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                user_id = str(getter() or "")
            except Exception:  # noqa: BLE001
                user_id = ""
        message_id = ""
        message_obj = getattr(event, "message_obj", None)
        if message_obj is not None:
            message_id = str(getattr(message_obj, "message_id", "") or "")
        sticky, keywords = self._check_sticky(text, self._cfg.sticky_keywords)
        hint, score = self._detect_emotion(text)
        return PerceptionSignal(
            session_id=session_id,
            user_id=user_id,
            message_id=message_id,
            text=text,
            timestamp=time.time() if now is None else float(now),
            is_command=self._looks_like_command(text),
            sticky_hit=sticky,
            sticky_keywords=keywords,
            emotion_hint=hint,
            emotion_score=score,
            length=len(re.sub(r"\s+", "", text)),
            has_question=self._is_question(text),
        )

    def _detect_emotion(self, text: str) -> tuple[EmotionHint, float]:
        """词表 + 标点粗判情绪。

        Args:
            text: 消息文本。

        Returns:
            `(情绪标签, 强度)`，强度 -1..1。
        """
        negative = sum(text.count(word) for word in NEGATIVE_WORDS)
        positive = sum(text.count(word) for word in POSITIVE_WORDS)
        if negative == positive:
            return EmotionHint.NEUTRAL, 0.0
        total = negative + positive
        raw = (positive - negative) / total
        if "！" in text or "!" in text:
            raw *= 1.2
        score = max(-1.0, min(1.0, raw))
        hint = EmotionHint.NEGATIVE if score < 0 else EmotionHint.POSITIVE
        return hint, score

    def _check_sticky(
        self,
        text: str,
        keywords: Sequence[str] | None = None,
    ) -> tuple[bool, tuple[str, ...]]:
        """检查粘性关键词与正则。

        Args:
            text: 消息文本。
            keywords: 覆盖默认关键词列表（测试用）。

        Returns:
            `(是否命中, 命中规则)`。
        """
        hits: list[str] = []
        custom_keywords = self._cfg.sticky_keywords if keywords is None else keywords
        hits.extend(word for word in custom_keywords if word and word in text)
        hits.extend(
            pattern.pattern for pattern in self._sticky_regexes if pattern.search(text)
        )
        return bool(hits), tuple(hits)

    def _looks_like_command(self, text: str) -> bool:
        """判断是否指令消息。"""
        stripped = (text or "").lstrip()
        if not stripped:
            return False
        return any(stripped.startswith(prefix) for prefix in self._prefixes)

    def _is_question(self, text: str) -> bool:
        """判断是否提问。"""
        stripped = (text or "").strip()
        if not stripped:
            return False
        if "？" in stripped or "?" in stripped:
            return True
        return stripped[-1:] in {"吗", "呢", "吧", "么"}


def iter_rule_hits(patterns: Iterable[re.Pattern[str]], text: str) -> list[str]:
    """返回所有命中的正则模式字符串。"""
    return [pattern.pattern for pattern in patterns if pattern.search(text)]
