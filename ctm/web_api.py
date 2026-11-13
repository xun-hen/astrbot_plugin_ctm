"""插件可视化页面的后端（只读）。

页面通过 AstrBot 的插件页面 bridge 调这些接口：`/{plugin}/overview` 等。
所有接口**只读**，不接受任何写操作；查询参数做 clamp，非法类型返回 400。

页面不可用时（老版本没有 `astrbot.api.web`）整个模块降级，不影响插件主流程。
"""

from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger

from ..config import PLUGIN_NAME

try:  # 老版本没有页面 API 时整体降级
    from astrbot.api.web import error_response, json_response, request

    WEB_API_AVAILABLE = True
except Exception:  # noqa: BLE001
    error_response = None  # type: ignore[assignment]
    json_response = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]
    WEB_API_AVAILABLE = False

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def parse_limit(raw: Any) -> int:
    """解析并夹紧 limit；非整数抛 ValueError（前端 bug 不静默）。"""
    if raw is None or raw == "":
        return DEFAULT_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    return max(1, min(MAX_LIMIT, value))


def _decode_decision(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class PageApi:
    """页面只读视图。"""

    def __init__(self, plugin: Any) -> None:
        """初始化。

        Args:
            plugin: CTM 插件实例（只读其 cfg / diag / store / lt / aggregation）。
        """
        self.plugin = plugin

    async def api_overview(self) -> Any:
        """概览：生效配置、诊断计数、表计数、聚合/长期记忆状态。"""
        plugin = self.plugin
        cfg = plugin.cfg
        diag = plugin.diag.as_dict()
        lt_enabled = plugin.lt is not None
        lt_items = plugin.lt.stats()["items"] if lt_enabled else 0
        return json_response(
            {
                "enabled": cfg.enabled,
                "topic": {
                    "strategy": cfg.topic.auto_switch_strategy,
                    "threshold": cfg.topic.topic_threshold,
                    "min_turns": cfg.topic.min_turns_before_switch,
                    "cooldown_seconds": cfg.topic.switch_cooldown_seconds,
                    "judge_enabled": cfg.topic.judge_enabled,
                    "judge_provider_id": cfg.topic.judge_provider_id or "（当前会话模型）",
                    "judge_min_confidence": cfg.topic.judge_min_confidence,
                    "judge_max_checks_per_hour": cfg.topic.judge_max_checks_per_hour,
                    "detect_in_hook": cfg.topic.detect_in_hook_enabled,
                },
                "aggregation": {
                    "enabled": cfg.topic.aggregation_enabled,
                    "debounce_seconds": cfg.topic.aggregation_debounce_seconds,
                    "max_wait_seconds": cfg.topic.aggregation_max_wait_seconds,
                    "active": plugin.aggregation.active_rounds(),
                },
                "budget": {
                    "injection_tokens": cfg.memory.injection_budget_tokens,
                    "summary_limit": cfg.memory.summary_limit,
                    "assertion_limit": cfg.memory.assertion_limit,
                    "episode_limit": cfg.memory.episode_limit,
                    "max_prompt_growth_ratio": cfg.injection.max_prompt_growth_ratio,
                },
                "longterm": {
                    "enabled": lt_enabled,
                    "items": lt_items,
                },
                "diag": diag,
                "tables": plugin.store.stats(),
                "db": plugin.store.health(),
            }
        )

    async def api_turns(self) -> Any:
        """最近决策日志（不含记忆正文）。"""
        try:
            limit = parse_limit(request.query.get("limit"))
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        rows = []
        for row in self.plugin.store.recent_turn_logs(limit):
            decision = _decode_decision(row.get("decision_json"))
            rows.append(
                {
                    "turn_id": str(row.get("turn_id", ""))[:12],
                    "session": str(row.get("session_id", "")),
                    "action": decision.get("topic_action", ""),
                    "persona": decision.get("persona_mode", ""),
                    "tokens": int(row.get("injected_tokens") or 0),
                    "dropped": len(decision.get("dropped_items") or []),
                    "at": float(row.get("created_at") or 0),
                }
            )
        return json_response({"items": rows})

    async def api_topics(self) -> Any:
        """最近话题。"""
        try:
            limit = parse_limit(request.query.get("limit"))
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        rows = [
            {
                "id": row.get("id"),
                "session": str(row.get("session_id", "")),
                "title": str(row.get("title", "")),
                "status": str(row.get("status", "")),
                "summary": str(row.get("summary") or "")[:120],
                "at": float(row.get("last_active_at") or 0),
            }
            for row in self.plugin.store.recent_topics(limit)
        ]
        return json_response({"items": rows})

    async def api_memories(self) -> Any:
        """最近长期记忆。"""
        if self.plugin.lt is None:
            return json_response({"items": [], "enabled": False})
        try:
            limit = parse_limit(request.query.get("limit"))
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        rows = [
            {
                "id": str(row.get("id", ""))[:8],
                "scope": f"{row.get('scope_type')}:{row.get('scope_id')}",
                "kind": str(row.get("kind", "")),
                "slot_key": str(row.get("slot_key") or ""),
                "content": str(row.get("content") or "")[:200],
                "importance": round(float(row.get("importance") or 0), 2),
                "confidence": round(float(row.get("confidence") or 0), 2),
                "at": float(row.get("updated_at") or 0),
            }
            for row in self.plugin.lt.store.recent_items(limit)
        ]
        return json_response({"items": rows, "enabled": True})

    async def api_health(self) -> Any:
        """健康检查。"""
        plugin = self.plugin
        return json_response(
            {
                "db": plugin.store.health(),
                "archive_queue": plugin.archiver._queue.qsize(),  # noqa: SLF001
                "aggregation_active": len(plugin.aggregation.active_rounds()),
                "longterm": {
                    "enabled": plugin.lt is not None,
                    "items": plugin.lt.stats()["items"] if plugin.lt else 0,
                },
            }
        )


ROUTES = (
    ("/{plugin}/overview", "api_overview", ("GET",), "CTM overview"),
    ("/{plugin}/turns", "api_turns", ("GET",), "Recent turn decisions"),
    ("/{plugin}/topics", "api_topics", ("GET",), "Recent topics"),
    ("/{plugin}/memories", "api_memories", ("GET",), "Recent long-term memories"),
    ("/{plugin}/health", "api_health", ("GET",), "Health"),
)


def register_page_api(context: Any, plugin: Any) -> bool:
    """注册页面只读路由；失败只告警，不影响插件主流程。

    Args:
        context: AstrBot 插件上下文。
        plugin: CTM 插件实例。

    Returns:
        是否注册成功。
    """
    if not WEB_API_AVAILABLE:
        logger.info(f"[{PLUGIN_NAME}] 页面后端未注册：当前 AstrBot 不支持 api.web")
        return False
    api = PageApi(plugin)
    try:
        for route, handler_name, methods, desc in ROUTES:
            context.register_web_api(
                route.format(plugin=PLUGIN_NAME),
                getattr(api, handler_name),
                list(methods),
                desc,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[{PLUGIN_NAME}] 注册页面后端失败：{exc}")
        return False
    logger.info(f"[{PLUGIN_NAME}] 页面后端已注册 {len(ROUTES)} 个只读路由")
    return True
