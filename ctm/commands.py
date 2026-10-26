"""`/ctm` 管理命令：只做查看与人工修正，不参与每轮热路径。

设计文档 §7 的命令表。两条原则：

* **读命令必须便宜**：`status` / `topics` / `memory list` 只查索引覆盖的列，
  不做全表扫；群聊里被误触发时不至于拖慢回复；
* **写命令必须留痕**：`memory edit` / `pin` / `delete` 都是一次纠正，
  要写 `corrected_count` 或重要度，并记日志，否则"人工修正"会变成静默改库。

权限：建议只给管理员（`event.is_admin()`）。v0.1 在外层用
`@filter.permission_type(PermissionType.ADMIN)` 兜底，本模块不重复判断。
"""

from __future__ import annotations

from typing import Any

USAGE = """CTM 管理命令：
/ctm status              当前会话话题栈、关系状态、预算
/ctm topics              最近话题及摘要
/ctm memory list [type]  列出记忆（type=summary/assertion/episode）
/ctm memory edit <id> <content>  修正记忆（降权 + 记录 corrected_count）
/ctm memory delete <id>  删除记忆
/ctm memory pin <id>     置顶（提高重要度）
/ctm switch hard         显式硬切换（等价 /new，仅非陪伴场景）
/ctm export              导出本会话 JSON
/ctm replay <turn_id>    回放某轮决策与注入内容
/ctm health              适配器 / DB / 队列健康检查
"""


class CtmCommands:
    """命令分发器。"""

    def __init__(self, plugin: Any) -> None:
        """初始化。

        Args:
            plugin: 插件实例；按 duck typing 使用 `cfg / store / memory /
                relationship / topic_sm / diag / adapter / archiver`。
        """
        self._plugin = plugin

    async def dispatch(self, event: Any, raw: str) -> str:
        """解析并执行子命令。

        Args:
            event: 消息事件（用于拿 session_id 与权限）。
            raw: 指令原文（含 `/ctm` 前缀）。

        Returns:
            要回复给用户的文本；未知子命令返回 USAGE。

        TODO(v0.1): 去掉前缀后按空白切分；`memory` 二级子命令单独分派；
            未知参数返回一行提示而不是整段 USAGE（避免刷屏）。
        """
        raise NotImplementedError("v0.1: CtmCommands.dispatch")

    async def status(self, event: Any, args: list[str]) -> str:
        """`/ctm status`：话题栈 + 关系 + 预算使用。

        Args:
            event: 消息事件。
            args: 额外参数（忽略）。

        Returns:
            多行文本。

        TODO(v0.1): 读 session_state + topic_stack + relationship；
            输出当前话题、栈深、turns_since_switch、intimacy/tension、
            最近一次注入 Token 与丢弃条数。
        """
        raise NotImplementedError("v0.1: CtmCommands.status")

    async def topics(self, event: Any, args: list[str]) -> str:
        """`/ctm topics`：最近话题列表。

        Args:
            event: 消息事件。
            args: 可选条数。

        Returns:
            列表文本（标题 + 状态 + 摘要首行）。

        TODO(v0.1): store.list_topics(limit=args or 10)。
        """
        raise NotImplementedError("v0.1: CtmCommands.topics")

    async def memory(self, event: Any, args: list[str]) -> str:
        """`/ctm memory ...`：记忆子命令入口。

        Args:
            event: 消息事件。
            args: 形如 ["list", "assertion"] 或 ["edit", "12", "内容"]。

        Returns:
            文本结果。

        TODO(v0.1): 二级分派到 list/edit/delete/pin；缺参时给单行用法。
        """
        raise NotImplementedError("v0.1: CtmCommands.memory")

    async def switch_hard(self, event: Any, args: list[str]) -> str:
        """`/ctm switch hard`：显式硬切。

        Args:
            event: 消息事件。
            args: 忽略。

        Returns:
            结果文本。

        TODO(v0.1): 调 topic_sm.hard_switch 并保存；记 WARN 级日志（这是破坏性操作）。
        """
        raise NotImplementedError("v0.1: CtmCommands.switch_hard")

    async def export(self, event: Any, args: list[str]) -> str:
        """`/ctm export`：导出会话 JSON。

        Args:
            event: 消息事件。
            args: 忽略。

        Returns:
            导出文件路径或"已发送文件"提示。

        TODO(v0.1): store.export_session → 写插件数据目录 exports/，返回路径；
            群聊场景默认拒绝（隐私），只允许私聊或管理员。
        """
        raise NotImplementedError("v0.1: CtmCommands.export")

    async def replay(self, event: Any, args: list[str]) -> str:
        """`/ctm replay <turn_id>`：回放决策。

        Args:
            event: 消息事件。
            args: turn_id。

        Returns:
            决策摘要（不含记忆正文原文，避免二次泄露）。

        TODO(v0.1): store.get_turn_log；格式化 topic_action / persona / tokens。
        """
        raise NotImplementedError("v0.1: CtmCommands.replay")

    async def health(self, event: Any, args: list[str]) -> str:
        """`/ctm health`：DB / 适配器 / 队列健康。

        Args:
            event: 消息事件。
            args: 忽略。

        Returns:
            检查结果文本。

        TODO(v0.1): store.health() + adapter.health() + archiver 队列长度。
        """
        raise NotImplementedError("v0.1: CtmCommands.health")
