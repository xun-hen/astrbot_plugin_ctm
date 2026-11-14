"""自检：把"插件静默失效"的每一道门禁收拢成一张可读清单。

为什么需要它：一个 AstrBot 插件可以"加载成功、启动日志正常、配置生效"却**一条
消息都不处理**，而且全程没有 ERROR。已知的静默门禁至少有四道：

1. 全局 `plugin_set` 白名单没包含本插件（框架在 `waking_check` 阶段直接丢弃 handler）；
2. handler 被写成模块级函数 / 第一参数不是 `self`（框架 partial 绑实例后 TypeError 被吞）；
3. 同优先级的其他插件 `stop_event()` 抢占了事件（后续 handler 不再执行）；
4. 配置里 `basic.enabled=false` 或 `topic.mode=off`。

本模块只做**判定**，不做修复；输出是给人看（`/ctm doctor`）和给页面看
（`/astrbot_plugin_ctm/selfcheck`）的结构化清单。纯标准库 + 可选框架探测，
任何单项失败都不会中断其余检查。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

LEVEL_OK = "ok"
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"

#: 启动后多久仍无入口消息才提示"可疑"（给人留出没消息的空闲期）。
IDLE_SUSPICIOUS_SECONDS = 600.0

_VERSION_RE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)


def read_plugin_version() -> str:
    """从 `metadata.yaml` 读插件版本；失败返回空串（不抛异常）。"""
    try:
        text = (Path(__file__).resolve().parents[1] / "metadata.yaml").read_text(
            encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        return ""
    match = _VERSION_RE.search(text)
    return match.group(1).strip() if match else ""


def _astrbot_version() -> str:
    try:
        from astrbot import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        return ""


def _check(
    check_id: str,
    level: str,
    title: str,
    detail: str,
    fix: str = "",
) -> dict[str, str]:
    return {
        "id": check_id,
        "level": level,
        "title": title,
        "detail": detail,
        "fix": fix,
    }


def _ago(seconds: float) -> str:
    """把秒数压成人话。"""
    seconds = max(0.0, float(seconds or 0.0))
    if seconds < 90:
        return f"{int(seconds)} 秒"
    if seconds < 5400:
        return f"{int(seconds // 60)} 分钟"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


def _check_dispatch(plugin: Any, now: float, uptime: float) -> dict[str, str]:
    """核心检查：消息入口到底有没有进来过。"""
    diag = plugin.diag
    entries = int(getattr(diag, "entries", 0) or 0)
    requests = int(getattr(diag, "llm_requests", 0) or 0)
    last_entry = float(getattr(diag, "last_entry_at", 0.0) or 0.0)
    if entries > 0:
        when = f"，最近一次 {_ago(now - last_entry)}前" if last_entry else ""
        return _check(
            "dispatch",
            LEVEL_OK,
            "消息入口",
            f"已收到 {entries} 条入口消息{when}；LLM 请求钩子 {requests} 次。",
        )
    if requests > 0:
        return _check(
            "dispatch",
            LEVEL_ERROR,
            "消息入口被拦截",
            f"LLM 请求钩子跑了 {requests} 次，但消息入口 **0** 次："
            "消息根本没派发到本插件（不是插件内部逻辑问题）。",
            "依次查：① 全局 plugin_set 白名单是否包含本插件；"
            "② 是否有同 priority 的其他插件 stop_event()；③ 本会话是否被禁用本插件。",
        )
    if uptime < IDLE_SUSPICIOUS_SECONDS:
        return _check(
            "dispatch",
            LEVEL_INFO,
            "消息入口",
            f"插件启动 {_ago(uptime)}，还没有任何消息进来（可能只是没人说话）。",
        )
    return _check(
        "dispatch",
        LEVEL_WARN,
        "消息入口长时间无消息",
        f"已启动 {_ago(uptime)}，入口消息 0 条、请求钩子 0 次。"
        "如果你刚发过消息，说明派发在框架侧就被拦住了。",
        "查全局 plugin_set 白名单（WebUI → 配置 → 插件）与同级插件的 stop_event()。",
    )


def _check_plugin_set(plugin: Any) -> dict[str, str]:
    state = str(getattr(plugin, "_plugin_set_state", "unknown") or "unknown")
    if state == "ok":
        return _check("plugin_set", LEVEL_OK, "全局白名单", "plugin_set 包含本插件。")
    if state == "not_listed":
        return _check(
            "plugin_set",
            LEVEL_ERROR,
            "全局白名单未包含本插件",
            "AstrBot 的 `plugin_set` 是全局白名单（[\"*\"] 表示全部），"
            "不在名单内的插件 handler 会在消息派发阶段被静默丢弃。",
            "WebUI → 配置 → 插件 → 把 astrbot_plugin_ctm 加入 plugin_set"
            "（或把 plugin_set 设为 [\"*\"]）。",
        )
    if state == "empty":
        return _check(
            "plugin_set",
            LEVEL_ERROR,
            "全局白名单为空",
            "`plugin_set=[]` 表示禁用所有第三方插件，本插件不会收到消息。",
            "WebUI → 配置 → 插件 → 设置 plugin_set（至少加入本插件或填 [\"*\"]）。",
        )
    return _check(
        "plugin_set",
        LEVEL_INFO,
        "全局白名单",
        "当前框架未暴露 plugin_set（或读不到），无法核对。",
    )


def _check_handlers(plugin: Any) -> dict[str, str]:
    binding = getattr(plugin, "_handler_binding", None) or {}
    if not binding:
        return _check(
            "handlers",
            LEVEL_INFO,
            "handler 绑定",
            "尚未自检（插件未完成 initialize）。",
        )
    bad = {name: status for name, status in binding.items() if status != "ok"}
    if not bad:
        return _check(
            "handlers",
            LEVEL_OK,
            "handler 绑定",
            f"{len(binding)} 个 handler 均已注册并绑定本实例。",
        )
    detail = "、".join(f"{name}={status}" for name, status in bad.items())
    fix = (
        "handler 必须定义为 CtmPlugin 的类方法（第一参数 self）；"
        "模块级函数会被框架 partial 误绑成 f(实例, event) → TypeError 且被吞掉。"
    )
    missing = [name for name, status in bad.items() if status == "missing"]
    if missing:
        fix += " 状态为 missing 说明 handler 根本不在注册表里（注册失败/被清理后未重载）。"
    return _check("handlers", LEVEL_ERROR, "handler 绑定异常", detail, fix)


def _check_store(plugin: Any) -> dict[str, str]:
    try:
        health = plugin.store.health()
    except Exception as exc:  # noqa: BLE001
        return _check("store", LEVEL_ERROR, "数据库", f"检查失败：{exc}", "重载插件；仍失败请查看数据目录权限。")
    if health.get("ok"):
        size = float(health.get("size_bytes") or 0) / 1024.0
        return _check(
            "store",
            LEVEL_OK,
            "数据库",
            f"{health.get('path')}（{size:.1f} KB，{health.get('tables')} 张表）",
        )
    return _check(
        "store",
        LEVEL_ERROR,
        "数据库不可用",
        f"{health.get('error')}（{health.get('path')}）",
        "检查数据目录是否存在、可写；SQLite 请放本地盘而不是网络盘。",
    )


def _check_aggregation(plugin: Any) -> dict[str, str]:
    cfg = plugin.cfg.topic
    if not cfg.aggregation_enabled:
        return _check(
            "aggregation",
            LEVEL_INFO,
            "消息聚合",
            "已关闭（每条消息各自成一轮）。",
        )
    anomalies = int(getattr(plugin.diag, "aggregation_anomalies", 0) or 0)
    detail = (
        f"防抖 {cfg.aggregation_debounce_seconds}s，总等待上限 "
        f"{cfg.aggregation_max_wait_seconds}s"
    )
    if anomalies:
        return _check(
            "aggregation",
            LEVEL_WARN,
            "消息聚合有异常",
            f"{detail}；已发生 {anomalies} 次异常（owner 等待超时/轮次溢出），已强制放行。",
            "若频繁出现，调大 max_wait 或检查事件循环是否被阻塞。",
        )
    return _check("aggregation", LEVEL_OK, "消息聚合", detail)


def _check_version() -> dict[str, str]:
    plugin_version = read_plugin_version() or "unknown"
    astrbot_version = _astrbot_version() or "unknown"
    if astrbot_version == "unknown":
        return _check(
            "version",
            LEVEL_INFO,
            "版本",
            f"插件 {plugin_version}；AstrBot 版本未知。",
        )
    try:
        major, minor = (int(part) for part in astrbot_version.split(".")[:2])
    except (TypeError, ValueError):
        return _check(
            "version", LEVEL_INFO, "版本", f"插件 {plugin_version}；AstrBot {astrbot_version}"
        )
    if (major, minor) < (4, 26):
        return _check(
            "version",
            LEVEL_WARN,
            "AstrBot 版本偏低",
            f"插件 {plugin_version} 要求 AstrBot >= 4.26，当前 {astrbot_version}。",
            "升级 AstrBot；旧版本可能没有页面 API / 会话管理接口。",
        )
    return _check(
        "version",
        LEVEL_OK,
        "版本",
        f"插件 {plugin_version}；AstrBot {astrbot_version}（要求 >= 4.26）。",
    )


def _check_session(plugin: Any, session_id: str) -> dict[str, str] | None:
    if not session_id:
        return None
    if session_id in getattr(plugin, "_topic_disabled", set()):
        return _check(
            "session",
            LEVEL_WARN,
            "本会话已关闭话题功能",
            "这个会话执行过 `/ctm topic off`，话题判定与注入不会生效。",
            "在本会话发送 `/ctm topic on` 恢复。",
        )
    return _check("session", LEVEL_OK, "本会话", "话题功能在本会话启用。")


def run(plugin: Any, session_id: str = "") -> dict[str, Any]:
    """跑一遍全部检查。

    Args:
        plugin: CTM 插件实例（duck typing 只读其 cfg / diag / store / 自检快照）。
        session_id: 可选，当前会话标识（用于检查会话级关闭）。

    Returns:
        `{"ok": bool, "summary": {...}, "items": [ {id,level,title,detail,fix} ]}`。
        单项检查失败不会中断其余项。
    """
    now = time.time()
    started_at = float(getattr(plugin, "_started_at", 0.0) or 0.0)
    uptime = now - started_at if started_at else 0.0

    items: list[dict[str, str]] = []
    checks = (
        lambda: _check_dispatch(plugin, now, uptime),
        lambda: _check_plugin_set(plugin),
        lambda: _check_handlers(plugin),
        lambda: _check_store(plugin),
        lambda: _check_aggregation(plugin),
        lambda: _check_version(),
    )
    for factory in checks:
        try:
            items.append(factory())
        except Exception as exc:  # noqa: BLE001 - 自检本身不能崩
            items.append(
                _check("internal", LEVEL_WARN, "检查项异常", f"{factory}: {exc}")
            )
    try:
        session_item = _check_session(plugin, session_id)
    except Exception:  # noqa: BLE001
        session_item = None
    if session_item is not None:
        items.append(session_item)

    counts = {level: 0 for level in (LEVEL_OK, LEVEL_INFO, LEVEL_WARN, LEVEL_ERROR)}
    for item in items:
        counts[item["level"]] = counts.get(item["level"], 0) + 1
    ok = counts[LEVEL_ERROR] == 0
    return {
        "ok": ok,
        "summary": counts,
        "uptime_seconds": round(uptime, 1),
        "items": items,
    }


def format_text(result: dict[str, Any]) -> str:
    """把自检结果渲染成命令输出（纯文本，无 ANSI）。"""
    icons = {
        LEVEL_OK: "[OK]",
        LEVEL_INFO: "[--]",
        LEVEL_WARN: "[!!]",
        LEVEL_ERROR: "[XX]",
    }
    lines = [
        f"CTM 自检：{'通过' if result.get('ok') else '发现阻断项'}"
        f"（OK {result['summary'].get(LEVEL_OK, 0)} / "
        f"提示 {result['summary'].get(LEVEL_INFO, 0)} / "
        f"警告 {result['summary'].get(LEVEL_WARN, 0)} / "
        f"阻断 {result['summary'].get(LEVEL_ERROR, 0)}；"
        f"已运行 {_ago(float(result.get('uptime_seconds') or 0.0))}）"
    ]
    for item in result.get("items", []):
        icon = icons.get(item.get("level", ""), "[??]")
        lines.append(f"{icon} {item.get('title')}：{item.get('detail')}")
        if item.get("fix") and item.get("level") in (LEVEL_WARN, LEVEL_ERROR):
            lines.append(f"     → {item.get('fix')}")
    if result.get("ok") and result["summary"].get(LEVEL_WARN, 0) == 0:
        lines.append("结论：链路正常，无需处理。")
    elif result.get("ok"):
        lines.append("结论：没有阻断项，但有警告，建议按上面的 → 处理。")
    else:
        lines.append("结论：上面 [XX] 的项就是「为什么一条消息都不处理」的原因。")
    return "\n".join(lines)
