"""文本相似度：话题判定与记忆相关性共用的打分栈。

设计文档 §3.2 要求三级实现，优先级从高到低：

1. 配置了 Embedding Provider → 余弦相似度；
2. 否则 → 关键词 Jaccard + 字符 n-gram 加权；
3. 兜底 → 标题与消息的包含关系。

本模块只定义契约与词法实现。Embedding 是 v1.0 的事，但**接口现在就留好**：
上层只依赖 `SimilarityScorer.score()`，将来换实现不改判定代码。

注意：中文没有空格，Jaccard 的"词"在这里指 **2-gram 字符片段**加英文单词，
不引入分词器（避免第三方依赖，也避免分词差异带来的不可复现）。
"""

from __future__ import annotations

import re
from typing import Protocol

_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


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
        小写英文词 + 单字 + 单字 2-gram 的并集（中文用 2-gram 近似"词"）。

    TODO(v0.1): 去标点/空白，保留数字；英文按词，中文按字与 bigram。
    """
    raise NotImplementedError("v0.1: tokenize")


def jaccard(left: set[str], right: set[str]) -> float:
    """集合 Jaccard 相似度。

    Args:
        left: 集合 A。
        right: 集合 B。

    Returns:
        |A∩B| / |A∪B|；任一为空返回 0.0。

    TODO(v0.1): 空集短路，避免除零。
    """
    raise NotImplementedError("v0.1: jaccard")


def char_ngram_similarity(left: str, right: str, n: int = 2) -> float:
    """字符 n-gram 相似度（中文短文本比 Jaccard 更稳）。

    Args:
        left: 文本 A。
        right: 文本 B。
        n: n-gram 长度，默认 2。

    Returns:
        两组 n-gram 的 Jaccard；文本短于 n 时退化为字符集合比较。

    TODO(v0.1): 去掉空白后切片，空串返回 0.0。
    """
    raise NotImplementedError("v0.1: char_ngram_similarity")


def topic_similarity(topic_text: str, message_text: str, scorer: SimilarityScorer | None = None) -> float:
    """话题相似度的统一入口（按文档的三级策略）。

    Args:
        topic_text: 当前话题文本。
        message_text: 本轮消息。
        scorer: 可选打分器；None 时使用 `LexicalScorer`。

    Returns:
        0..1 相似度。

    TODO(v0.1): 依次尝试 scorer → 词法加权（0.6×Jaccard + 0.4×2-gram）→ 包含关系。
    """
    raise NotImplementedError("v0.1: topic_similarity")


class LexicalScorer:
    """无外部依赖的默认打分器（Jaccard + 字符 2-gram 加权）。"""

    #: Jaccard 的权重；剩余给字符 n-gram。数值待回归对比后定稿。
    JACCARD_WEIGHT = 0.6

    def score(self, topic_text: str, message_text: str) -> float:
        """按词法特征打分。

        Args:
            topic_text: 话题文本。
            message_text: 消息文本。

        Returns:
            `0.6 * jaccard + 0.4 * char_ngram_similarity`，裁剪到 [0, 1]。

        TODO(v0.1): 实现加权；topic_text 为空时返回 0.0（视为新会话）。
        """
        raise NotImplementedError("v0.1: LexicalScorer.score")
