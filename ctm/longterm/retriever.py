"""召回打分与 Prompt 块渲染（纯标准库）。"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .config import LongTermConfig
from .prompts import KIND_LABELS, MEMORY_CONTEXT_FOOTER, MEMORY_CONTEXT_HEADER

_WORD_RE = re.compile(r"[a-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_MAX_ITEM_CHARS = 200


def estimate_tokens(text: str) -> int:
    """估算中英混排文本的 Token 数（仅用于预算，不用于计费）。

    Args:
        text: 任意文本。

    Returns:
        估算值；空文本返回 0。
    """
    if not text:
        return 0
    cjk_chars = sum(len(run) for run in _CJK_RUN_RE.findall(text))
    latin_words = len(_WORD_RE.findall(text.lower()))
    return max(1, math.ceil((cjk_chars * 0.6) + (latin_words * 1.3)))


def tokenize(text: str) -> set[str]:
    """把文本切成词面 Token（中文用字符 + 二元组近似）。

    Args:
        text: 任意文本。

    Returns:
        小写 Token 集合。
    """
    lowered = (text or "").lower()
    tokens: set[str] = set(_WORD_RE.findall(lowered))
    for run in _CJK_RUN_RE.findall(lowered):
        for char in run:
            tokens.add(char)
        for index in range(len(run) - 1):
            tokens.add(run[index : index + 2])
    return tokens


@dataclass
class RecallResult:
    """一次召回的结果。"""

    rows: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    tokens: int = 0

    @property
    def empty(self) -> bool:
        """是否没有命中任何记忆。"""
        return not self.rows


class Retriever:
    """按词面、重要度与新近度排序并装箱。"""

    def __init__(self, cfg: LongTermConfig) -> None:
        """初始化。

        Args:
            cfg: 长期记忆配置。
        """
        self.cfg = cfg

    def score(
        self,
        row: dict[str, Any],
        *,
        query_tokens: set[str],
        now: float,
    ) -> float:
        """给一条候选打分。

        Args:
            row: `lt_memories` 行（dict）。
            query_tokens: 当前消息的 Token 集合。
            now: 当前 Unix 秒。

        Returns:
            0..1 的分数。
        """
        item_tokens = tokenize(f"{row.get('slot_key') or ''} {row.get('content') or ''}")
        overlap = len(query_tokens & item_tokens)
        lexical = overlap / max(1, len(query_tokens)) if query_tokens else 0.0

        age_days = max(0.0, (now - float(row.get("updated_at") or now)) / 86400.0)
        recency = 0.5 ** (age_days / max(1.0, self.cfg.recall_half_life_days))

        weights = (
            self.cfg.w_lexical,
            self.cfg.w_importance,
            self.cfg.w_recency,
        )
        total = sum(weights) or 1.0
        score = (
            lexical * self.cfg.w_lexical
            + float(row.get("importance") or 0.0) * self.cfg.w_importance
            + recency * self.cfg.w_recency
        ) / total

        if row.get("kind") == "profile" and float(row.get("importance") or 0) >= 0.8:
            score = max(score, 0.62)
        return score

    def select(
        self,
        rows: list[dict[str, Any]],
        *,
        query: str,
        now: float | None = None,
    ) -> RecallResult:
        """排序、去重并按 Token 预算装箱。

        Args:
            rows: 候选记忆行。
            query: 当前用户消息。
            now: 当前 Unix 秒；None 时取系统时间。

        Returns:
            渲染好的召回结果。
        """
        now = time.time() if now is None else now
        query_tokens = tokenize(query)
        scored = [
            (self.score(row, query_tokens=query_tokens, now=now), row) for row in rows
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)

        selected: list[dict[str, Any]] = []
        seen_slots: set[tuple[str, str]] = set()
        tokens_used = 0
        profile_count = 0

        for score, row in scored:
            if len(selected) >= self.cfg.recall_max_items:
                break
            if not self.cfg.recall_inject_profile and row.get("kind") == "profile":
                continue
            if (
                row.get("kind") == "profile"
                and profile_count >= self.cfg.recall_max_profile_items
            ):
                continue
            if score < self.cfg.recall_min_score and row.get("kind") != "profile":
                continue
            slot = (str(row.get("kind")), str(row.get("slot_key") or ""))
            if row.get("slot_key") and slot in seen_slots:
                continue
            cost = estimate_tokens(self.format_item(row))
            if tokens_used + cost > self.cfg.recall_max_tokens:
                continue
            seen_slots.add(slot)
            tokens_used += cost
            selected.append(row)
            if row.get("kind") == "profile":
                profile_count += 1

        if not selected:
            return RecallResult()

        lines = [MEMORY_CONTEXT_HEADER]
        lines.extend(self.format_item(row) for row in selected)
        lines.append(MEMORY_CONTEXT_FOOTER)
        text = "\n".join(lines)
        return RecallResult(rows=selected, text=text, tokens=estimate_tokens(text))

    @staticmethod
    def format_item(row: dict[str, Any]) -> str:
        """把一条记忆渲染成单行 Prompt 条目。

        Args:
            row: `lt_memories` 行。

        Returns:
            不带换行的列表项。
        """
        label = KIND_LABELS.get(str(row.get("kind")), str(row.get("kind")))
        content = str(row.get("content") or "")[:_MAX_ITEM_CHARS]
        slot = row.get("slot_key")
        body = f"{slot}: {content}" if slot else content
        updated = float(row.get("updated_at") or 0)
        date_text = (
            time.strftime("%Y-%m-%d", time.localtime(updated)) if updated else "unknown"
        )
        return f"- [{label}] {body} (since {date_text})"
