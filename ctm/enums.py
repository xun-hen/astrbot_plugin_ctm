"""跨模块共享的枚举：字符串值与数据库列、配置、注入模板一一对应。

约定：所有枚举继承 `str`，这样写进 JSON / SQLite 时不需要再转换，
读回来也能直接与字符串比较（避免"存成 `TopicAction.SOFT_BOUNDARY`"这类事故）。
"""

from __future__ import annotations

from enum import Enum


class TopicAction(str, Enum):
    """本轮的话题动作。"""

    CONTINUE = "continue"
    SOFT_BOUNDARY = "soft_boundary"
    HARD_SWITCH = "hard_switch"


class PersonaMode(str, Enum):
    """人格模式，决定注入模板里的行为约束。"""

    DAILY = "daily"
    COMFORT = "comfort"
    PLAYFUL = "playful"
    SERIOUS = "serious"


class EmotionHint(str, Enum):
    """粗粒度情绪标签；只用于路由，不做情绪分析展示。"""

    NEGATIVE = "negative"
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class MemoryType(str, Enum):
    """记忆类型，对应 `memories.type` 列。"""

    SUMMARY = "summary"
    ASSERTION = "assertion"
    EPISODE = "episode"


class TopicStatus(str, Enum):
    """话题生命周期状态，对应 `topics.status` 列。"""

    ACTIVE = "active"
    ARCHIVED = "archived"
    POPPED = "popped"


class SwitchStrategy(str, Enum):
    """自动判定的执行策略，对应 `topic.auto_switch_strategy`。"""

    OFF = "off"
    SOFT = "soft"
    HARD = "hard"
