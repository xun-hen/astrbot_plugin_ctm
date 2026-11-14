"""`/ctm` 管理命令：只做查看与人工修正，不参与每轮热路径。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import selfcheck
from .bindings import describe as describe_binding
from .injection import InjectionBuilder
from .observability import LogCode, log_event

USAGE = """CTM 管理命令：
/ctm status              当前会话话题栈、关系状态、预算
/ctm topics              最近话题及摘要
/ctm memory list [type]  列出记忆（type=summary/assertion/episode）
/ctm memory edit <id> <content>  修正记忆（降权 + 记录 corrected_count）
/ctm memory delete <id>  删除记忆
/ctm memory pin <id>     置顶（提高重要度）
/ctm longterm list       列出长期记忆
/ctm longterm delete <id> 删除长期记忆
/ctm switch hard         显式硬切换（等价 /new，仅非紧急场景）
/ctm export              导出本会话 JSON
/ctm replay <turn_id>    回放某轮决策与注入内容
/ctm health              适配器 / DB / 队列健康检查
/ctm doctor              自检：白名单 / handler 绑定 / 入口心跳 / DB / 版本
/ctm 自检                同上（中文别名）
"""


class CtmCommands:
    """命令分发器。"""

    def __init__(self, plugin: Any) -> None:
        """初始化。

        Args:
            plugin: 插件实例（duck typing 使用其公开属性）。
        """
        self._plugin = plugin

    async def dispatch(self, event: Any, raw: str) -> str:
        """解析并执行子命令。"""
        parts = (raw or "").strip().split()
        if parts and parts[0].startswith("/"):
            parts = parts[1:]
        if parts and parts[0] in {"ctm", "topicweave", "话题织机"}:
            parts = parts[1:]
        if not parts:
            return USAGE
        command, args = parts[0], parts[1:]
        if command == "status":
            return await self.status(event, args)
        if command == "topic":
            return await self.topic(event, args)
        if command == "topics":
            return await self.topics(event, args)
        if command == "memory":
            return await self.memory(event, args)
        if command == "longterm":
            return await self.longterm(event, args)
        if command == "switch":
            return await self.switch_hard(event, args)
        if command == "export":
            return await self.export(event, args)
        if command == "replay":
            return await self.replay(event, args)
        if command == "health":
            return await self.health(event, args)
        if command in ("doctor", "selfcheck", "check", "自检"):
            return await self.doctor(event, args)
        return USAGE

    async def status(self, event: Any, args: list[str]) -> str:
        """`/ctm status`：话题栈 + 关系 + 预算使用。"""
        plugin = self._plugin
        session_id = str(getattr(event, "unified_msg_origin", ""))
        state = plugin.topic_sm.load(session_id)
        relationship = plugin.relationship.load(session_id)
        diag = plugin.diag.as_dict()
        lines = [
            f"模式：{plugin.cfg.topic.mode}"
            + ("（调试空跑）" if plugin.cfg.topic.debug_mode else ""),
            f"话题：{state.current_topic or '（无）'}",
            f"栈深：{len(state.stack)}  turns_since_switch={state.turns_since_switch}",
            f"亲密度：{relationship.intimacy:.2f}  张力：{relationship.tension:.2f}",
            f"预算：{plugin.cfg.memory.injection_budget_tokens} tokens",
            f"当前注入：{diag['injected_tokens']} tokens / 丢弃 {diag['dropped_items']} 条",
            f"入口：entries={diag['entries']} decisions={diag['decisions']} "
            f"skips={diag['skips']}",
            f"长期记忆：{plugin.lt.stats()['items']} 条" if plugin.lt else "长期记忆：未启用",
            f"诊断：{json.dumps(diag, ensure_ascii=False)}",
        ]
        return "\n".join(lines)

    async def topic(self, event: Any, args: list[str]) -> str:
        """`/ctm topic ...`：模式与话题操作。"""
        plugin = self._plugin
        session_id = str(getattr(event, "unified_msg_origin", ""))
        action = args[0] if args else "status"
        if action == "status":
            state = plugin.topic_sm.load(session_id)
            disabled = session_id in plugin._topic_disabled  # noqa: SLF001
            return "\n".join(
                [
                    f"模式：{plugin.cfg.topic.mode}"
                    + ("（调试空跑）" if plugin.cfg.topic.debug_mode else ""),
                    f"本会话：{'已关闭自动判定' if disabled else '启用'}",
                    f"当前话题：{state.current_topic or '（无）'}  栈深：{len(state.stack)}",
                    f"判定：{'开' if plugin.cfg.topic.judge_enabled else '关'}"
                    f"，自动切换阈值 {plugin.cfg.topic.auto_threshold}",
                    f"聚合：{'开' if plugin.cfg.topic.aggregation_enabled else '关'}"
                    f"（防抖 {plugin.cfg.topic.aggregation_debounce_seconds}s）",
                ]
            )
        if action == "new":
            title = " ".join(args[1:]).strip()
            return await plugin.manual_new_topic(event, title)
        if action == "forget":
            signal = plugin.perception.extract(event)
            state = plugin.topic_sm.load(session_id)
            plugin.topic_sm.hard_switch(signal, state)
            return "已忘记当前话题，下一条消息将重新建档。"
        if action in {"off", "on"}:
            if action == "off":
                plugin._topic_disabled.add(session_id)  # noqa: SLF001
                return "本会话已关闭自动话题判定（长期记忆不受影响）。"
            plugin._topic_disabled.discard(session_id)  # noqa: SLF001
            return "本会话已恢复自动话题判定。"
        return "用法：/ctm topic status|new [标题]|forget|off|on"

    async def topics(self, event: Any, args: list[str]) -> str:
        """`/ctm topics`：最近话题列表。"""
        limit = _int_arg(args, 10)
        session_id = str(getattr(event, "unified_msg_origin", ""))
        rows = self._plugin.store.list_topics(session_id, limit=limit)
        if not rows:
            return "（无话题记录）"
        lines = []
        for row in rows:
            summary = str(row.get("summary") or "").splitlines()
            first = summary[0][:40] if summary else ""
            lines.append(
                f"#{row['id']} [{row['status']}] {row['title']}"
                + (f" — {first}" if first else "")
            )
        return "\n".join(lines)

    async def memory(self, event: Any, args: list[str]) -> str:
        """`/ctm memory ...`：CTM 短期记忆子命令。"""
        if not args:
            return "用法：/ctm memory list|edit|delete|pin ..."
        action, rest = args[0], args[1:]
        session_id = str(getattr(event, "unified_msg_origin", ""))
        if action == "list":
            memory_type = rest[0] if rest else ""
            types = [memory_type] if memory_type else [
                "summary",
                "assertion",
                "episode",
            ]
            rows = self._plugin.store.fetch_memories(session_id, types, limit=10)
            if not rows:
                return "（无记忆）"
            return "\n".join(
                f"#{row['id']} [{row['type']}] {str(row['content'])[:80]}"
                for row in rows
            )
        if action in {"edit", "delete", "pin"} and len(rest) >= 1:
            try:
                memory_id = int(rest[0])
            except ValueError:
                return "记忆 ID 必须是整数。"
            if action == "delete":
                deleted = self._plugin.store.delete_memory(memory_id)
                return "已删除。" if deleted else "未找到该记忆。"
            if action == "pin":
                self._plugin.store.adjust_importance(memory_id, 0.2)
                return "已提高重要度。"
            content = " ".join(rest[1:]).strip()
            if not content:
                return "用法：/ctm memory edit <id> <content>"
            self._plugin.store.mark_corrected(memory_id)
            self._plugin.store.update_memory_content(memory_id, content)
            log_event(LogCode.DECISION, session_id=session_id, action="memory_edit")
            return "已修正并降权。"
        return "用法：/ctm memory list|edit|delete|pin ..."

    async def longterm(self, event: Any, args: list[str]) -> str:
        """`/ctm longterm ...`：长期记忆子命令。"""
        plugin = self._plugin
        if plugin.lt is None:
            return "长期记忆未启用。"
        platform_id = _platform_id(event)
        sender_id = _sender_id(event)
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        is_group = bool(group_id)
        session_id = str(getattr(event, "unified_msg_origin", ""))
        action = args[0] if args else "list"
        if action == "list":
            rows = plugin.lt.store.list_scope(
                "user", f"{platform_id}:{sender_id}", statuses=("active",), limit=20
            )
            if not rows:
                return "（无长期记忆）"
            return "\n".join(
                f"{row['id'][:8]} [{row['kind']}] {str(row['content'])[:80]}"
                for row in rows
            )
        if action == "delete" and len(args) >= 2:
            prefix = args[1]
            rows = plugin.lt.store.list_scope(
                "user", f"{platform_id}:{sender_id}", statuses=("active",), limit=100
            )
            for row in rows:
                if str(row["id"]).startswith(prefix):
                    deleted = plugin.lt.forget(
                        session_id=session_id,
                        platform_id=platform_id,
                        sender_id=sender_id,
                        group_id=group_id,
                        is_group=is_group,
                        memory_id=str(row["id"]),
                    )
                    return "已删除。" if deleted else "未找到该记忆。"
            return "未找到该记忆。"
        return "用法：/ctm longterm list|delete <id前缀>"

    async def switch_hard(self, event: Any, args: list[str]) -> str:
        """`/ctm switch hard`：显式硬切。"""
        if not args or args[0] != "hard":
            return "用法：/ctm switch hard"
        session_id = str(getattr(event, "unified_msg_origin", ""))
        signal = self._plugin.perception.extract(event)
        state = self._plugin.topic_sm.load(session_id)
        self._plugin.topic_sm.hard_switch(signal, state)
        log_event(LogCode.DECISION, session_id=session_id, action="hard_switch")
        return "已硬切换（旧话题已归档）。"

    async def export(self, event: Any, args: list[str]) -> str:
        """`/ctm export`：导出会话 JSON 到插件数据目录。"""
        session_id = str(getattr(event, "unified_msg_origin", ""))
        is_admin = bool(getattr(event, "is_admin", lambda: False)())
        is_private = not bool(getattr(event, "get_group_id", lambda: "")() or "")
        if not is_private and not is_admin:
            return "群聊中导出数据需管理员权限。"
        data = self._plugin.store.export_session(session_id)
        export_dir = Path(self._plugin.data_dir) / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        safe_name = session_id.replace(":", "_").replace("/", "_")[-60:]
        path = export_dir / f"{safe_name}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"已导出到：{path}"

    async def replay(self, event: Any, args: list[str]) -> str:
        """`/ctm replay <turn_id>`：回放决策。"""
        if not args:
            return "用法：/ctm replay <turn_id>"
        log = self._plugin.store.get_turn_log(args[0])
        if not log:
            return "未找到该轮次。"
        decision = log.get("decision_json") or "{}"
        try:
            parsed = json.loads(decision)
        except ValueError:
            parsed = {}
        return (
            f"turn={log.get('turn_id')}\n"
            f"action={parsed.get('topic_action')} persona={parsed.get('persona_mode')}\n"
            f"tokens={log.get('injected_tokens')} dropped={log.get('dropped_items')}"
        )

    async def health(self, event: Any, args: list[str]) -> str:
        """`/ctm health`：DB / 队列 / handler 绑定 / 入口计数。"""
        plugin = self._plugin
        store_health = plugin.store.health()
        diag = plugin.diag.as_dict()
        binding = getattr(plugin, "_handler_binding", None) or {}
        lines = [
            f"store: {'ok' if store_health.get('ok') else 'error'} "
            f"{store_health.get('path', '')}",
            f"archive queue: {plugin.archiver._queue.qsize()}",  # noqa: SLF001
            f"longterm: {'ok' if plugin.lt is not None else 'off'}",
            f"handlers: {describe_binding(binding)}",
            f"plugin_set: {getattr(plugin, '_plugin_set_state', 'unknown')}",
            f"入口: entries={diag['entries']} decisions={diag['decisions']} "
            f"skips={diag['skips']} no_decision={diag['no_decision']} "
            f"llm_requests={diag['llm_requests']}",
        ]
        if diag["entries"] == 0 and diag["llm_requests"] > 0:
            lines.append(
                "⚠ 只有请求钩子被调用、消息入口从未进入：典型原因是同优先级的"
                "其他插件 stop_event() 抢占了事件，或本插件被全局白名单/会话级禁用。"
            )
        return "\n".join(lines)

    async def doctor(self, event: Any, args: list[str]) -> str:
        """`/ctm doctor`：一次性体检"插件到底有没有在工作"。"""
        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        result = selfcheck.run(self._plugin, session_id)
        return selfcheck.format_text(result)


def _int_arg(args: list[str], default: int) -> int:
    if not args:
        return default
    try:
        return max(1, min(100, int(args[0])))
    except ValueError:
        return default


def _platform_id(event: Any) -> str:
    getter = getattr(event, "get_platform_id", None)
    return str(getter() or "") if callable(getter) else ""


def _sender_id(event: Any) -> str:
    getter = getattr(event, "get_sender_id", None)
    return str(getter() or "") if callable(getter) else ""
