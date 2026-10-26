"""感知层：把一条平台消息压成 `PerceptionSignal`。

边界（设计文档 §3.1）：

* **不调用 LLM、不查库、不 await**。目标 P99 < 5ms，所以只做词表与正则；
* 只产出"标签"，不做判断：情绪是粗粒度提示，粘性命中只标记事实，
  是否因此降灵敏度由 `TopicStateMachine` 决定；
* 事件对象按 duck typing 处理（`unified_msg_origin` / `message_str` /
  `message_obj` / `get_sender_id`），缺失字段一律降级为空串，
  **不因为平台差异抛异常**——感知失败最多让本轮判定保守，不能阻断消息。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..config import TopicConfig
from .enums import EmotionHint

# 内置情绪词表（规则优先；可选轻量模型是后续增强，不进 v0.1）。
# 命中的词条数用于估分，不追求语言学正确性：它只影响人格模式，不影响安全判定。
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

_NEGATIVE_RE = re.compile("|".join(map(re.escape, NEGATIVE_WORDS)))
_POSITIVE_RE = re.compile("|".join(map(re.escape, POSITIVE_WORDS)))

COMMAND_PREFIXES_DEFAULT: tuple[str, ...] = ("/",)


@dataclass(frozen=True)
class PerceptionSignal:
    """单条消息的感知结果（不可变，可安全跨 await 传递）。

    Attributes:
        session_id: AstrBot 的 unified_msg_origin，作为一切隔离的键。
        user_id: 发送者 ID。
        message_id: 平台消息 ID；撤回过滤与去重用。
        text: 纯文本（不含图片占位符之外的内容）。
        timestamp: 收到时间（Unix 秒）。
        is_command: 是否指令；指令一律不参与判定。
        sticky_hit: 是否命中粘性关键词/正则。
        sticky_keywords: 命中的具体规则（用于日志与"为什么没切"）。
        emotion_hint: 粗粒度情绪。
        emotion_score: -1..1，负值为负面。
        length: 文本长度（去空白后）。
        has_question: 是否含问号或疑问词（帮助判断"换话题还是在追问"）。
    """

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
    """感知层自己的配置视图（从 `TopicConfig` 投影而来，避免全量配置穿越）。

    Attributes:
        sticky_keywords: 精确匹配的粘性词。
        sticky_regex: 正则形式的粘性规则。
        command_prefixes: 视为指令的前缀。
        min_length_for_judgement: 短于此长度的消息不参与话题判定
            （"嗯"/"好的"永远算延续）。
    """

    sticky_keywords: tuple[str, ...] = ()
    sticky_regex: tuple[str, ...] = ()
    command_prefixes: tuple[str, ...] = COMMAND_PREFIXES_DEFAULT
    min_length_for_judgement: int = 6


class Perception:
    """规则感知器。"""

    def __init__(self, cfg: TopicConfig, command_prefixes: Sequence[str] = COMMAND_PREFIXES_DEFAULT) -> None:
        """初始化。

        Args:
            cfg: 话题配置（读取粘性关键词与正则）。
            command_prefixes: 指令前缀，来自插件基础配置或 AstrBot 的指令前缀。

        TODO(v0.1): 预编译正则（`sticky_regex` 配错时跳过并记 WARN，不能启动失败）。
        """
        self._cfg = cfg
        self._prefixes = tuple(command_prefixes)

    def extract(self, event: Any, *, now: float | None = None) -> PerceptionSignal:
        """把事件压成感知信号。

        Args:
            event: AstrBot 消息事件（或测试替身）。
            now: 时间戳；测试注入用，缺省取 `time.time()`。

        Returns:
            PerceptionSignal。

        Raises:
            None: 字段缺失时降级为空串，绝不向调用方抛异常。

        TODO(v0.1):
            1. 取 `unified_msg_origin` / `get_sender_id()` / `message_obj.message_id`，
               任一缺失时降级并记 DEBUG；
            2. 文本取 `message_str`，图片占位与引用标记不参与相似度；
            3. `_looks_like_command()`；
            4. `_check_sticky()`；
            5. `_detect_emotion()`；
            6. `has_question`：中文问号 / 英文问号 / 是否以"吗/呢/吧"结尾；
            7. 返回不可变信号。
        """
        raise NotImplementedError("v0.1: Perception.extract")

    def _detect_emotion(self, text: str) -> tuple[EmotionHint, float]:
        """词表 + 标点 + emoji 粗判情绪。

        Args:
            text: 消息文本。

        Returns:
            (情绪标签, 强度)。强度 -1..1；命中多个词时取绝对值最大的一侧，
            正负抵消时返回 neutral / 0.0。

        TODO(v0.1): 词频 × 基础分 + 感叹号/emoji 加成，结果裁剪到 [-1, 1]。
        """
        raise NotImplementedError("v0.1: Perception._detect_emotion")

    def _check_sticky(self, text: str, keywords: Sequence[str], regexes: Sequence[str]) -> tuple[bool, tuple[str, ...]]:
        """检查粘性关键词与正则。

        Args:
            text: 消息文本。
            keywords: 精确匹配的子串（中文不切词，按包含判断）。
            regexes: 正则表达式列表。

        Returns:
            (是否命中, 命中的规则列表)。

        TODO(v0.1): 关键词用 `in`；正则以 try/except 包裹，单个错误只跳过该条。
        """
        raise NotImplementedError("v0.1: Perception._check_sticky")

    def _looks_like_command(self, text: str) -> bool:
        """判断是否指令消息。

        Args:
            text: 消息文本。

        Returns:
            以配置前缀开头即为指令。

        TODO(v0.1): strip 后按前缀判断；空文本返回 False。
        """
        raise NotImplementedError("v0.1: Perception._looks_like_command")

    def _is_question(self, text: str) -> bool:
        """判断是否为提问。

        Args:
            text: 消息文本。

        Returns:
            含问号或以疑问语气词结尾。

        TODO(v0.1): "？" / "?" / 结尾 "吗/呢/吧/么"。
        """
        raise NotImplementedError("v0.1: Perception._is_question")


def iter_rule_hits(patterns: Iterable[re.Pattern[str]], text: str) -> list[str]:
    """返回所有命中的正则模式（用于日志）。

    Args:
        patterns: 已编译的正则。
        text: 待匹配文本。

    Returns:
        命中模式的 pattern 字符串列表。

    TODO(v0.1): 逐个 `search`，收集 `p.pattern`。
    """
    raise NotImplementedError("v0.1: iter_rule_hits")
