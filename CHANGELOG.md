# CHANGELOG

## v0.2.0

### 新增

- 长期记忆子系统 `ctm/longterm/`：跨会话抽取、召回、作用域隔离、容量淘汰、
  敏感信息过滤与审计（`lt_memories` / `lt_audit`）。
- `memory_search` / `memory_save` / `memory_forget` 三个 LLM 工具。
- `/ctm longterm list|delete`、`/ctm export`、`/ctm replay` 等管理命令。

### 实现

- 话题状态机：软边界压栈、回溯弹栈、显式硬切。
- 规则感知（情绪/粘性/指令/提问）、词法相似度。
- 短期记忆：话题摘要、用户断言、情节记忆的召回与打分。
- Token 预算装箱、单点注入、注入比例截断。
- 关系状态（亲密度/张力/未完成事项）与自然衰减。
- 异步归档：单 worker、指数退避重试、失败落 `archive_failures`。
- 配置解析（`config.py`）与 `_conf_schema.json` 双写一致性。

### 测试

- `tests/test_ctm.py`：11 项单测（配置、存储、话题、记忆、装箱、归档、长期记忆）。
- `tests/test_import.py`：AstrBot 接线导入 smoke。
