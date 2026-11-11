"""记忆抽取：Prompt 渲染、JSON 解析与安全过滤（纯标准库，可单测）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .prompts import EXTRACT_SYSTEM_PROMPT, EXTRACT_USER_TEMPLATE

VALID_SCOPES = ("user", "group")
VALID_KINDS = ("profile", "preference", "event", "task", "fact")
MAX_CONTENT_CHARS = 200
MAX_DIALOGUE_CHARS = 2400

_SENSITIVE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|secret)\b\s*[:=]\s*\S{8,}"),
    re.compile(r"(?i)(?:password|passwd|密码|口令)\s*(?:是|为|[:=：])\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b\d{17}[\dXx]\b"),
    re.compile(r"\b(?:\d[ -]?){15,19}\b"),
)

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


@dataclass(frozen=True)
class MemoryCandidate:
    """一条待入库的记忆候选。"""

    scope: str
    kind: str
    slot_key: str
    content: str
    importance: float
    confidence: float


def _clamp(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, min(1.0, float(value)))


def sanitize_memory_text(text: str) -> str:
    """归一化记忆正文：单行、去控制字符、防标签注入、截断。

    Args:
        text: 原始文本。

    Returns:
        适合注入 Prompt 的单行陈述。
    """
    cleaned = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = cleaned.replace("<", "‹").replace(">", "›")
    if len(cleaned) > MAX_CONTENT_CHARS:
        cleaned = cleaned[:MAX_CONTENT_CHARS].rstrip() + "…"
    return cleaned


def is_sensitive(text: str) -> bool:
    """判断文本是否包含密码、密钥、证件号等敏感信息。

    Args:
        text: 候选记忆正文。

    Returns:
        命中任一敏感模式时返回 True。
    """
    return any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS)


def build_prompt(
    *,
    dialogue: str,
    user_text: str,
    assistant_text: str,
    max_candidates: int,
) -> tuple[str, str]:
    """渲染抽取用的 system/user Prompt。

    Args:
        dialogue: 最近对话文本。
        user_text: 本轮用户消息。
        assistant_text: 本轮助手回复。
        max_candidates: 允许返回的最大候选数。

    Returns:
        `(system_prompt, user_prompt)`。
    """
    system_prompt = EXTRACT_SYSTEM_PROMPT.format(max_candidates=max_candidates)
    user_prompt = EXTRACT_USER_TEMPLATE.format(
        dialogue=(dialogue or "")[:MAX_DIALOGUE_CHARS],
        user_text=(user_text or "")[:MAX_DIALOGUE_CHARS],
        assistant_text=(assistant_text or "")[:MAX_DIALOGUE_CHARS],
    )
    return system_prompt, user_prompt


def parse_candidates(raw: str, max_candidates: int) -> list[MemoryCandidate]:
    """解析模型输出为校验后的候选列表。

    Args:
        raw: 模型原始输出，可带 Markdown 代码围栏。
        max_candidates: 最大接受条数。

    Returns:
        校验后的候选；无法解析时返回空列表。
    """
    text = _FENCE_RE.sub("", (raw or "").strip())
    match = _JSON_ARRAY_RE.search(text)
    if not match:
        return []
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []

    candidates: list[MemoryCandidate] = []
    for entry in payload[:max_candidates]:
        if not isinstance(entry, dict):
            continue
        content = sanitize_memory_text(str(entry.get("content", "")))
        if len(content) < 2:
            continue
        kind = str(entry.get("kind", "fact")).strip().lower()
        if kind not in VALID_KINDS:
            kind = "fact"
        scope = str(entry.get("scope", "user")).strip().lower()
        if scope not in VALID_SCOPES:
            scope = "user"
        slot_key = str(entry.get("slot_key", "")).strip()[:64]
        candidates.append(
            MemoryCandidate(
                scope=scope,
                kind=kind,
                slot_key=slot_key,
                content=content,
                importance=_clamp(entry.get("importance"), 0.5),
                confidence=_clamp(entry.get("confidence"), 0.5),
            )
        )
    return candidates
