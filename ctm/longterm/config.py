"""长期记忆配置（与 `_conf_schema.json` 的 `longterm` 分组对齐）。

与 CTM 原有的 `memory`（话题摘要/断言/情节）分组不同，本分组描述的是
**跨会话长期记忆**：写入、召回、容量与隐私。两类记忆共用同一个 SQLite，
但表前缀不同（`lt_`），互不影响。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


def _as_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(low, min(high, int(value)))


def _as_float(value: Any, default: float, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(low, min(high, float(value)))


def _as_str(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) else default


@dataclass(frozen=True)
class LongTermConfig:
    """长期记忆的不可变配置快照。

    Attributes:
        enabled: 总开关；关闭后不写入、不注入、不响应记忆工具。
        extract_enabled: 是否在回复后自动抽取记忆。
        extract_every_n_turns: 每多少轮抽取一次（按会话计数）。
        extract_min_chars: 本轮 user+assistant 的最小字符数。
        extract_max_calls_per_hour: 每会话每小时最多抽取次数，0 表示禁用。
        extract_confidence_threshold: 候选入库的最低置信度。
        extract_promote_threshold: 候选直接转正的最低置信度。
        extract_provider_id: 指定抽取用的模型提供商；空串表示沿用当前会话。
        extract_max_candidates: 单次抽取接受的最大候选数。
        recall_enabled: 是否在请求前注入记忆。
        recall_max_items: 单次注入的最大条数。
        recall_max_profile_items: 常驻画像条数上限。
        recall_max_tokens: 注入块 Token 预算（估算）。
        recall_min_score: 低于该分数的记忆不注入（画像除外）。
        recall_timeout_ms: 召回硬超时；超时即放弃注入。
        recall_half_life_days: 新近度半衰期。
        recall_inject_profile: 是否常驻注入高重要度画像。
        recall_include_group_scope: 群聊时是否同时读取群记忆。
        w_lexical: 词面相关性权重。
        w_importance: 重要度权重。
        w_recency: 新近度权重。
        tools_enabled: 是否启用 memory_search / memory_save / memory_forget。
        block_sensitive: 是否拒绝保存密码、密钥、证件号等敏感内容。
        max_items_per_scope: 每个作用域的记忆条数上限。
        retention_days: 保留天数；0 表示永久。
    """

    enabled: bool = True
    extract_enabled: bool = True
    extract_every_n_turns: int = 2
    extract_min_chars: int = 8
    extract_max_calls_per_hour: int = 30
    extract_confidence_threshold: float = 0.55
    extract_promote_threshold: float = 0.72
    extract_provider_id: str = ""
    extract_max_candidates: int = 5
    recall_enabled: bool = True
    recall_max_items: int = 12
    recall_max_profile_items: int = 5
    recall_max_tokens: int = 800
    recall_min_score: float = 0.28
    recall_timeout_ms: int = 800
    recall_half_life_days: float = 30.0
    recall_inject_profile: bool = True
    recall_include_group_scope: bool = True
    w_lexical: float = 0.45
    w_importance: float = 0.35
    w_recency: float = 0.20
    tools_enabled: bool = True
    block_sensitive: bool = True
    max_items_per_scope: int = 300
    retention_days: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "LongTermConfig":
        """从原始配置构建强类型快照；非法值回退默认并夹取区间。

        Args:
            raw: `_conf_schema.json` 中 `longterm` 分组的实际值。

        Returns:
            解析后的配置快照。
        """
        raw = raw if isinstance(raw, dict) else {}
        extract = raw.get("extract") if isinstance(raw.get("extract"), dict) else {}
        recall = raw.get("recall") if isinstance(raw.get("recall"), dict) else {}
        tools = raw.get("tools") if isinstance(raw.get("tools"), dict) else {}
        privacy = raw.get("privacy") if isinstance(raw.get("privacy"), dict) else {}
        base = cls()
        return replace(
            base,
            enabled=_as_bool(raw.get("enabled"), base.enabled),
            extract_enabled=_as_bool(extract.get("enabled"), base.extract_enabled),
            extract_every_n_turns=_as_int(
                extract.get("every_n_turns"),
                base.extract_every_n_turns,
                1,
                50,
            ),
            extract_min_chars=_as_int(
                extract.get("min_chars"), base.extract_min_chars, 0, 2000
            ),
            extract_max_calls_per_hour=_as_int(
                extract.get("max_calls_per_hour"),
                base.extract_max_calls_per_hour,
                0,
                1000,
            ),
            extract_confidence_threshold=_as_float(
                extract.get("confidence_threshold"),
                base.extract_confidence_threshold,
                0.0,
                1.0,
            ),
            extract_promote_threshold=_as_float(
                extract.get("promote_threshold"),
                base.extract_promote_threshold,
                0.0,
                1.0,
            ),
            extract_provider_id=_as_str(
                extract.get("provider_id"), base.extract_provider_id
            ),
            extract_max_candidates=_as_int(
                extract.get("max_candidates"),
                base.extract_max_candidates,
                1,
                20,
            ),
            recall_enabled=_as_bool(recall.get("enabled"), base.recall_enabled),
            recall_max_items=_as_int(
                recall.get("max_items"), base.recall_max_items, 1, 50
            ),
            recall_max_profile_items=_as_int(
                recall.get("max_profile_items"),
                base.recall_max_profile_items,
                0,
                20,
            ),
            recall_max_tokens=_as_int(
                recall.get("max_tokens"), base.recall_max_tokens, 100, 4000
            ),
            recall_min_score=_as_float(
                recall.get("min_score"), base.recall_min_score, 0.0, 1.0
            ),
            recall_timeout_ms=_as_int(
                recall.get("timeout_ms"), base.recall_timeout_ms, 100, 5000
            ),
            recall_half_life_days=_as_float(
                recall.get("half_life_days"),
                base.recall_half_life_days,
                1.0,
                3650.0,
            ),
            recall_inject_profile=_as_bool(
                recall.get("inject_profile"), base.recall_inject_profile
            ),
            recall_include_group_scope=_as_bool(
                recall.get("include_group_scope"), base.recall_include_group_scope
            ),
            w_lexical=_as_float(recall.get("w_lexical"), base.w_lexical, 0.0, 1.0),
            w_importance=_as_float(
                recall.get("w_importance"), base.w_importance, 0.0, 1.0
            ),
            w_recency=_as_float(recall.get("w_recency"), base.w_recency, 0.0, 1.0),
            tools_enabled=_as_bool(tools.get("enabled"), base.tools_enabled),
            block_sensitive=_as_bool(
                privacy.get("block_sensitive"), base.block_sensitive
            ),
            max_items_per_scope=_as_int(
                privacy.get("max_items_per_scope"),
                base.max_items_per_scope,
                10,
                5000,
            ),
            retention_days=_as_int(
                privacy.get("retention_days"), base.retention_days, 0, 3650
            ),
        )
