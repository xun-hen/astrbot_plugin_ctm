"""文本相似度：话题判定与记忆相关性共用的打分栈（纯标准库）。

中文按「单字 + 2-gram」近似，英文按词；不引入分词器，保证可复现。
"""

from __future__ import annotations

import re
from typing import Protocol

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


class SimilarityScorer(Protocol):
    """相似度打分的窄接口。"""

    def score(self, topic_text: str, message_text: str) -> float:
        """返回 0..1 的相似度。

        Args:
            topic_text: 当前话题的标题或摘要（可拼接）。
            message_text: 本轮用户消息。

        Returns:
            相似度，0 表示无关，1 表示同一话题。
        """
        ...


def tokenize(text: str) -> set[str]:
    """把文本切成可比较的单元集合。

    Args:
        text: 任意文本。

    Returns:
        小写英文词、单字与中文 2-gram 的并集。
    """
    tokens = {match.lower() for match in _TOKEN_RE.findall(text or "")}
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", text or "")
    for run in cjk_runs:
        for index in range(len(run) - 1):
            tokens.add(run[index : index + 2])
    return tokens


def jaccard(left: set[str], right: set[str]) -> float:
    """集合 Jaccard 相似度。

    Args:
        left: 集合 A。
        right: 集合 B。

    Returns:
        |A∩B| / |A∪B|；任一为空返回 0.0。
    """
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def char_ngram_similarity(left: str, right: str, n: int = 2) -> float:
    """字符 n-gram 相似度。

    Args:
        left: 文本 A。
        right: 文本 B。
        n: n-gram 长度，默认 2。

    Returns:
        两组 n-gram 的 Jaccard；文本短于 n 时退化为字符集合比较。
    """
    left_clean = re.sub(r"\s+", "", left or "")
    right_clean = re.sub(r"\s+", "", right or "")
    if not left_clean or not right_clean:
        return 0.0
    if len(left_clean) < n or len(right_clean) < n:
        return jaccard(set(left_clean), set(right_clean))
    left_grams = {left_clean[i : i + n] for i in range(len(left_clean) - n + 1)}
    right_grams = {right_clean[i : i + n] for i in range(len(right_clean) - n + 1)}
    return jaccard(left_grams, right_grams)


def topic_similarity(
    topic_text: str,
    message_text: str,
    scorer: SimilarityScorer | None = None,
) -> float:
    """话题相似度的统一入口。

    Args:
        topic_text: 当前话题文本。
        message_text: 本轮消息。
        scorer: 可选打分器；None 时使用 `LexicalScorer`。

    Returns:
        0..1 相似度。
    """
    topic_text = topic_text or ""
    message_text = message_text or ""
    if not topic_text.strip() or not message_text.strip():
        return 0.0
    if scorer is not None:
        return max(0.0, min(1.0, scorer.score(topic_text, message_text)))
    return LexicalScorer().score(topic_text, message_text)


class LexicalScorer:
    """无外部依赖的默认打分器（Jaccard + 字符 2-gram 加权）。"""

    JACCARD_WEIGHT = 0.6

    def score(self, topic_text: str, message_text: str) -> float:
        """按词法特征打分。

        Args:
            topic_text: 话题文本。
            message_text: 消息文本。

        Returns:
            `0.6 * jaccard + 0.4 * char_ngram_similarity`，裁剪到 [0, 1]。
        """
        if not (topic_text or "").strip():
            return 0.0
        jac = jaccard(tokenize(topic_text), tokenize(message_text))
        ngram = char_ngram_similarity(topic_text, message_text)
        return max(0.0, min(1.0, self.JACCARD_WEIGHT * jac + (1 - self.JACCARD_WEIGHT) * ngram))
