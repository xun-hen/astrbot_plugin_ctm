"""配置模型：与 `_conf_schema.json` 逐项对应的强类型配置。

约定（见 docs/DESIGN.md §6）：

* **默认值只写两处**：本文件与 `_conf_schema.json`；二者必须逐项一致，
  由测试断言（参考 msg_aggregator 的漂移教训：代码里是 True、面板里是 false，
  用户看到的值和实际生效的值不同，排障时极其误导）。
* 配置解析是**纯函数**：`load_config()` 不读写磁盘、不依赖插件实例，
  便于单测与热更新（后续 `/ctm` 面板改完配置可直接重建快照）。
* 非法值**回退到默认并记 WARN**，而不是让插件启动失败。

当前 v0.1 只落"形状"：字段、类型、默认值与分组；解析/校验逻辑留空，
见 `load_config()` 的 TODO。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .ctm.longterm.config import LongTermConfig

PLUGIN_NAME = "astrbot_plugin_ctm"

# 话题动作策略（与 ctm.enums.SwitchStrategy 保持一致；此处不 import，避免配置层反向依赖核心层）
STRATEGY_OFF = "off"
STRATEGY_SOFT = "soft"
STRATEGY_HARD = "hard"

# 记忆后端名（与 ctm.adapters 的注册名一致）
BACKEND_SQLITE = "sqlite"
BACKEND_LIVING_MEMORY = "living_memory"
BACKEND_MEMORY_COMPANION = "memory_companion"


@dataclass(frozen=True)
class TopicConfig:
    """话题状态机配置（对应 schema.topic）。

    Attributes:
        auto_switch_enabled: 是否自动判定话题变化。
        auto_switch_strategy: off / soft / hard。
        topic_threshold: 相似度阈值，低于该值才可能切话题。
        min_turns_before_switch: 新话题至少持续轮数。
        switch_cooldown_seconds: 两次切换的最小间隔。
        preserve_recent_topics: 注入候选里保留的最近话题数。
        sticky_keywords: 精确匹配的粘性关键词。
        sticky_regex: 正则形式的粘性规则。
        new_intent_markers: 新意图标记词。
    """

    auto_switch_enabled: bool = True
    auto_switch_strategy: str = STRATEGY_SOFT
    topic_threshold: float = 0.55
    min_turns_before_switch: int = 3
    switch_cooldown_seconds: int = 120
    preserve_recent_topics: int = 2
    sticky_keywords: tuple[str, ...] = (
        "承诺",
        "答应",
        "生日",
        "纪念日",
        "难过",
        "生病",
        "吵架",
        "对不起",
    )
    sticky_regex: tuple[str, ...] = ()
    new_intent_markers: tuple[str, ...] = ("另外", "话说", "换个话题", "对了", "突然想到")


@dataclass(frozen=True)
class Weights:
    """记忆评分权重（对应 schema.memory.w_*）。

    Attributes:
        relevance: 相关性权重。
        salience: 情感显著性权重。
        recency: 新近度权重。
        usage: 使用反馈权重。
    """

    relevance: float = 0.45
    salience: float = 0.35
    recency: float = 0.15
    usage: float = 0.05

    def total(self) -> float:
        """返回权重和。

        Returns:
            四项权重之和；用于归一化与配置校验。
        """
        return self.relevance + self.salience + self.recency + self.usage


@dataclass(frozen=True)
class MemoryConfig:
    """记忆与预算配置（对应 schema.memory）。

    Attributes:
        injection_budget_tokens: 每轮注入 Token 预算。
        summary_limit: 话题摘要条数上限。
        assertion_limit: 断言条数上限。
        episode_limit: 情节条数上限。
        recency_half_life_days: 新近度半衰期（天）。
        weights: 评分权重。
        assertion_confidence_threshold: 断言入库阈值。
        episode_salience_threshold: 情节入库阈值。
    """

    injection_budget_tokens: int = 1000
    summary_limit: int = 2
    assertion_limit: int = 3
    episode_limit: int = 2
    recency_half_life_days: float = 14.0
    weights: Weights = field(default_factory=Weights)
    assertion_confidence_threshold: float = 0.7
    episode_salience_threshold: float = 0.6


@dataclass(frozen=True)
class RelationshipConfig:
    """关系状态配置（对应 schema.relationship）。

    Attributes:
        enabled: 是否启用关系状态。
        intimacy_decay_per_day: 亲密度每日衰减量。
        tension_decay_per_turn: 张力每轮衰减系数。
        promise_detection_enabled: 是否检测承诺。
    """

    enabled: bool = True
    intimacy_decay_per_day: float = 0.01
    tension_decay_per_turn: float = 0.98
    promise_detection_enabled: bool = True


@dataclass(frozen=True)
class AdapterConfig:
    """适配器配置（对应 schema.adapter）。

    Attributes:
        backend: sqlite / living_memory / memory_companion。
        fallback_sqlite: 外部适配器失败时是否回退 SQLite。
        fallback_markdown: 是否同步落人类可读 Markdown 备份。
    """

    backend: str = BACKEND_SQLITE
    fallback_sqlite: bool = True
    fallback_markdown: bool = True


@dataclass(frozen=True)
class InjectionConfig:
    """注入配置（对应 schema.injection）。

    Attributes:
        include_meta: 是否把注入元信息写进 Prompt。
        soft_boundary_once: 软边界提示是否只注入一次。
        max_prompt_growth_ratio: 注入量相对原 system_prompt 的上限比例。
    """

    include_meta: bool = False
    soft_boundary_once: bool = True
    max_prompt_growth_ratio: float = 0.25


@dataclass(frozen=True)
class ObservabilityConfig:
    """可观测性配置（对应 schema.observability）。

    Attributes:
        log_decisions: 是否记录每轮决策。
        log_dropped_items: 是否记录被预算丢弃的条目。
        log_level: 插件日志级别。
        log_redact: 日志中的记忆内容是否脱敏。
    """

    log_decisions: bool = True
    log_dropped_items: bool = True
    log_level: str = "INFO"
    log_redact: bool = True


@dataclass(frozen=True)
class Config:
    """插件配置总根（对应 `_conf_schema.json` 的根对象）。

    注意：AstrBot 面板里看到的就是本对象，**没有**再套一层
    `cognitive_turn_manager:`；设计文档里的 YAML 根节点只是示意。

    Attributes:
        enabled: 插件总开关。
        db_path: SQLite 路径；相对路径相对于插件数据目录。
        timezone: 计算用时的时区名。
        topic: 话题状态机配置。
        memory: 记忆与预算配置。
        relationship: 关系状态配置。
        adapter: 适配器配置。
        injection: 注入配置。
        observability: 可观测性配置。
    """

    enabled: bool = True
    db_path: str = "ctm.sqlite3"
    timezone: str = "Asia/Shanghai"
    topic: TopicConfig = field(default_factory=TopicConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    relationship: RelationshipConfig = field(default_factory=RelationshipConfig)
    adapter: AdapterConfig = field(default_factory=AdapterConfig)
    injection: InjectionConfig = field(default_factory=InjectionConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    longterm: LongTermConfig = field(default_factory=LongTermConfig)


def _group(raw: dict[str, Any], name: str) -> dict[str, Any]:
    """取一个配置分组，缺失或类型错误时返回空 dict。"""
    value = raw.get(name)
    return value if isinstance(value, dict) else {}


def _bool(group: dict[str, Any], key: str, default: bool) -> bool:
    value = group.get(key)
    return value if isinstance(value, bool) else default


def _int(group: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = group.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(low, min(high, int(value)))


def _float(
    group: dict[str, Any], key: str, default: float, low: float, high: float
) -> float:
    value = group.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(low, min(high, float(value)))


def _str(group: dict[str, Any], key: str, default: str) -> str:
    value = group.get(key)
    return value.strip() if isinstance(value, str) else default


def _str_list(
    group: dict[str, Any], key: str, default: tuple[str, ...]
) -> tuple[str, ...]:
    value = group.get(key)
    if not isinstance(value, list):
        return default
    return tuple(item for item in value if isinstance(item, str))


def _normalize_weights(weights: Weights) -> Weights:
    """把权重归一化到和为 1；全 0 时回退默认。"""
    total = weights.total()
    if abs(total - 1.0) < 1e-6 or total <= 0:
        return weights
    return replace(
        weights,
        relevance=weights.relevance / total,
        salience=weights.salience / total,
        recency=weights.recency / total,
        usage=weights.usage / total,
    )


def load_config(raw: dict[str, Any] | None) -> Config:
    """把 AstrBot 传入的原始配置解析成 `Config`。

    Args:
        raw: 插件配置对象（通常是 `AstrBotConfig`，dict 子类）；None 视为空配置。

    Returns:
        解析后的配置快照。非法字段回退默认值，不抛异常。

    Raises:
        None: 按约定，配置错误只记日志。

    TODO(v0.1):
        1. 读取嵌套分组（basic / topic / memory / ...），未知分组忽略并记 WARN；
        2. 逐项类型强制转换（float/int/bool/list），list 转 tuple 以保持不可变，越界夹到合法区间；
        3. 权重和不为 1 时归一化并记 WARN；
        4. `auto_switch_strategy` 不在 {off, soft, hard} 时回退 soft；
        5. 返回不可变 `Config` 快照。
    """
    raw = raw if isinstance(raw, dict) else {}
    basic = _group(raw, "basic")
    topic_raw = _group(raw, "topic")
    memory_raw = _group(raw, "memory")
    relationship_raw = _group(raw, "relationship")
    adapter_raw = _group(raw, "adapter")
    injection_raw = _group(raw, "injection")
    observability_raw = _group(raw, "observability")

    defaults = Config()
    strategy = _str(topic_raw, "auto_switch_strategy", STRATEGY_SOFT)
    if strategy not in (STRATEGY_OFF, STRATEGY_SOFT, STRATEGY_HARD):
        strategy = STRATEGY_SOFT

    weights = _normalize_weights(
        Weights(
            relevance=_float(memory_raw, "w_relevance", 0.45, 0.0, 1.0),
            salience=_float(memory_raw, "w_salience", 0.35, 0.0, 1.0),
            recency=_float(memory_raw, "w_recency", 0.15, 0.0, 1.0),
            usage=_float(memory_raw, "w_usage", 0.05, 0.0, 1.0),
        )
    )

    backend = _str(adapter_raw, "backend", BACKEND_SQLITE)
    if backend not in (
        BACKEND_SQLITE,
        BACKEND_LIVING_MEMORY,
        BACKEND_MEMORY_COMPANION,
    ):
        backend = BACKEND_SQLITE

    return Config(
        enabled=_bool(basic, "enabled", defaults.enabled),
        db_path=_str(basic, "db_path", defaults.db_path) or defaults.db_path,
        timezone=_str(basic, "timezone", defaults.timezone) or defaults.timezone,
        topic=TopicConfig(
            auto_switch_enabled=_bool(
                topic_raw, "auto_switch_enabled", defaults.topic.auto_switch_enabled
            ),
            auto_switch_strategy=strategy,
            topic_threshold=_float(
                topic_raw, "topic_threshold", defaults.topic.topic_threshold, 0.0, 1.0
            ),
            min_turns_before_switch=_int(
                topic_raw,
                "min_turns_before_switch",
                defaults.topic.min_turns_before_switch,
                1,
                100,
            ),
            switch_cooldown_seconds=_int(
                topic_raw,
                "switch_cooldown_seconds",
                defaults.topic.switch_cooldown_seconds,
                0,
                86400,
            ),
            preserve_recent_topics=_int(
                topic_raw,
                "preserve_recent_topics",
                defaults.topic.preserve_recent_topics,
                0,
                20,
            ),
            sticky_keywords=_str_list(
                topic_raw, "sticky_keywords", defaults.topic.sticky_keywords
            ),
            sticky_regex=_str_list(
                topic_raw, "sticky_regex", defaults.topic.sticky_regex
            ),
            new_intent_markers=_str_list(
                topic_raw, "new_intent_markers", defaults.topic.new_intent_markers
            ),
        ),
        memory=MemoryConfig(
            injection_budget_tokens=_int(
                memory_raw,
                "injection_budget_tokens",
                defaults.memory.injection_budget_tokens,
                100,
                8000,
            ),
            summary_limit=_int(
                memory_raw, "summary_limit", defaults.memory.summary_limit, 0, 20
            ),
            assertion_limit=_int(
                memory_raw, "assertion_limit", defaults.memory.assertion_limit, 0, 20
            ),
            episode_limit=_int(
                memory_raw, "episode_limit", defaults.memory.episode_limit, 0, 20
            ),
            recency_half_life_days=_float(
                memory_raw,
                "recency_half_life_days",
                defaults.memory.recency_half_life_days,
                1.0,
                3650.0,
            ),
            weights=weights,
            assertion_confidence_threshold=_float(
                memory_raw,
                "assertion_confidence_threshold",
                defaults.memory.assertion_confidence_threshold,
                0.0,
                1.0,
            ),
            episode_salience_threshold=_float(
                memory_raw,
                "episode_salience_threshold",
                defaults.memory.episode_salience_threshold,
                0.0,
                1.0,
            ),
        ),
        relationship=RelationshipConfig(
            enabled=_bool(
                relationship_raw, "enabled", defaults.relationship.enabled
            ),
            intimacy_decay_per_day=_float(
                relationship_raw,
                "intimacy_decay_per_day",
                defaults.relationship.intimacy_decay_per_day,
                0.0,
                1.0,
            ),
            tension_decay_per_turn=_float(
                relationship_raw,
                "tension_decay_per_turn",
                defaults.relationship.tension_decay_per_turn,
                0.0,
                1.0,
            ),
            promise_detection_enabled=_bool(
                relationship_raw,
                "promise_detection_enabled",
                defaults.relationship.promise_detection_enabled,
            ),
        ),
        adapter=AdapterConfig(
            backend=backend,
            fallback_sqlite=_bool(
                adapter_raw, "fallback_sqlite", defaults.adapter.fallback_sqlite
            ),
            fallback_markdown=_bool(
                adapter_raw, "fallback_markdown", defaults.adapter.fallback_markdown
            ),
        ),
        injection=InjectionConfig(
            include_meta=_bool(
                injection_raw, "include_meta", defaults.injection.include_meta
            ),
            soft_boundary_once=_bool(
                injection_raw,
                "soft_boundary_once",
                defaults.injection.soft_boundary_once,
            ),
            max_prompt_growth_ratio=_float(
                injection_raw,
                "max_prompt_growth_ratio",
                defaults.injection.max_prompt_growth_ratio,
                0.0,
                1.0,
            ),
        ),
        observability=ObservabilityConfig(
            log_decisions=_bool(
                observability_raw, "log_decisions", defaults.observability.log_decisions
            ),
            log_dropped_items=_bool(
                observability_raw,
                "log_dropped_items",
                defaults.observability.log_dropped_items,
            ),
            log_level=_str(
                observability_raw, "log_level", defaults.observability.log_level
            ),
            log_redact=_bool(
                observability_raw, "log_redact", defaults.observability.log_redact
            ),
        ),
        longterm=LongTermConfig.from_dict(_group(raw, "longterm")),
    )
