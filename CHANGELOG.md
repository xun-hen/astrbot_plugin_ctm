# CHANGELOG

## v0.4.2

### 新增：自检（失效后**能看见**）

一次历史故障复盘得出：AstrBot 插件可以"加载成功、启动日志正常、配置生效"却一条消息
都不处理，且全程没有 ERROR。这类故障有四道静默门禁，v0.4.2 把它们收拢成一份清单：

1. 全局 `plugin_set` 白名单未包含本插件（框架在 `waking_check` 直接丢弃 handler）；
2. handler 被写成模块级函数（框架 partial 绑实例后 TypeError 被 `call_handler` 吞）；
3. 同优先级其他插件 `stop_event()` 抢占事件；
4. `basic.enabled=false` / `topic.mode=off` / 会话被 `/ctm topic off`。

- `ctm/selfcheck.py`：`run(plugin)` 产出 `{ok, summary, items[]}`，每项带
  `level = ok|info|warn|error`、人话说明与 `fix` 修复动作；单项异常不影响其余检查。
- **`/ctm doctor`**（别名 `/ctm selfcheck` / `/ctm check` / `/ctm 自检`）：
  文本体检，`[XX]` 就是"为什么不工作"，并直接给出下一步。
- **状态页自检卡片**：页面顶部红条 + 逐项清单，5 秒刷新；阻断项变红、警告变黄。
  `/astrbot_plugin_ctm/selfcheck` 与页面、命令共用同一份判定（不会出现"页面绿、
  实际死"的两套逻辑）。
- 新增心跳：`diag.last_entry_at / last_decision_at`，判据是"入口 0 次 + 请求钩子
  有次数 = 派发被拦"，比单纯看"有没有日志"可靠。
- 顶部指标新增"入口消息""跳过/无决策"，直接看 `entries` 与 `skips`。

### 测试

- 新增 `tests/test_selfcheck.py`（14 项，纯标准库替身）：白名单缺失/空、派发被拦、
  handler unbound、DB 失败、聚合异常、会话关闭、坏插件不抛异常、文本渲染。
- 全量 **67 tests OK**（无 AstrBot 环境时 16 项条件跳过）。

## v0.4.1

### 修复（**重要：v0.4.0 在真机上完全静默、一条消息都不处理**）

- **根因**：5 个 `@filter.*` handler 写成了模块级函数（`async def ctm_on_event(event)`）。
  AstrBot 装载插件时会把插件实例 `functools.partial` 绑到 handler 的第一个位置参数上
  （`astrbot/core/star/star_manager.py`，4.26 / 4.28 / 4.29 一致），于是派发变成
  `ctm_on_event(实例, event)` → `TypeError: takes 1 positional argument but 2 were given`；
  该异常被 `pipeline/context_utils.py` 的 `call_handler` 吞掉，只留一行框架 ERROR
  （`Plugin Handler arguments do not match its definition.`），随后照常调用 LLM。
  外部表现：插件加载成功、启动日志正常、配置生效，但 `on_message` / `on_llm_request` /
  `on_llm_response` / 两个命令全部不执行 —— 库里 0 行、插件日志 0 行、回复无延迟。
- 5 个 handler 改为 `CtmPlugin` 的类方法（第一参数 `self`），行为不变。
- 新增启动自检 `ctm/bindings.py` + `CtmPlugin._verify_handler_binding()`：核对每个 handler
  是否已注册、是否绑定到本实例；异常时打 `WARN_HANDLER_BINDING` 并在日志里点明后果。

### 修复（真机现场实测发现的第二个门禁）

- 同一台设备上 `astrbot_plugin_ctm` **不在 AstrBot 全局 `plugin_set` 白名单**里
  （白名单是 `["*"]`=全部 或 显式列表）。不在名单内的插件，handler 会在
  `waking_check` 阶段被直接丢弃（只有 DEBUG 日志），表现与"插件没装"完全一样。
  CTM 是 11-13 新装的，而白名单还停在装 topic_router_pro 那天的旧列表。
- 新增启动自检 `_verify_plugin_set()`：不在白名单时打 `WARN_PLUGIN_SET`，
  并在日志里直接给出修复路径（WebUI → 配置 → 插件 → 加入 plugin_set）。
  `/ctm health` 也会打印 `plugin_set: ok|not_listed|empty|unknown`。

> 两个根因是**串联**的：先过 `plugin_set` 白名单，再过 handler 绑定。只修一个
> 仍然不会工作——真机上先补白名单、再升级到 v0.4.1 后，`entries/decisions/
> sessions/topics/turn_logs` 才从 0 变成 1。排查顺序建议：白名单 → 绑定自检 →
> `INFO_ENTRY` / `DEBUG_SKIP`。

### 新增（让"静默失效"这一类故障以后一定留痕）

- 入口 `INFO_ENTRY`：每条消息记 `session_id / text_len / mode / enabled / aggregate`。
- 4 个静默 return 点各记一条 `DEBUG_SKIP`，带稳定 `reason`
  （`disabled` / `mode_off` / `session_off` / `command` / `empty_text` /
  `aggregating_follower` / `empty_after_aggregate` / `inject_off`）。
- `on_llm_request` 记 `DEBUG_LLM_REQUEST`；`WARN_NO_DECISION` 不再受
  `diag.decisions == 0` 门禁限制（计数为 0 恰恰最需要留痕），并带上原因码。
- `Diag` 新增 `entries / llm_requests / skips / aggregation_anomalies`；
  `/ctm status`、`/ctm health` 展示入口计数与 handler 绑定自检结果。
- `/ctm health` 在"只有请求钩子被调用、消息入口从未进入"时给出明确提示
  （多半是同优先级插件 `stop_event()` 抢占，或本插件被会话级禁用）。

### 修复（兜底死锁）

- `_fallback_decision` 不再要求话题栈非空：空栈时用**不落库**的临时焦点
  （`_ephemeral_state`）照常判定并注入，避免"入口被抢占 → 栈永远为空 →
  兜底永远放弃 → 整类故障完全静默"的闭环。原因记为 `empty_stack_recovered`。

### 加固

- `Aggregator` 的 owner 等待加超时兜底（`max_wait + 5s`）：定时器异常时不再永久挂住
  用户消息；轮次溢出与超时通过 `on_anomaly` 回调记 WARN。
- `configure()` 提前到 `__init__`：即使 `initialize()` 因配置未启用提前返回，
  入口日志也一定能打出来。
- `basic.enabled=false` 由 INFO 改为 WARN，明确提示"插件不工作"。

### 测试

- 新增 `tests/test_bindings.py`（纯标准库）：把"模块级单参 handler 会被误绑"固化成回归用例。
- 新增 `tests/test_entry_and_fallback.py`：入口/跳过日志、空栈兜底、NO_DECISION 留痕。
- `tests/test_import.py` 增加断言：5 个 handler 必须是 `(self, event)` 类方法，
  且模块顶层不允许再出现同名函数。
- 全量 `48 tests ... OK`（无 AstrBot 环境时 11 项条件跳过）。

## v0.4.0

### 新增

- 四种运行模式 `off / mark / hint / auto`（`ctm/modes.py`）：mark 只记录不注入；
  auto 在置信度 ≥ `auto_threshold` 时调用 `conversation_manager.new_conversation`
  自动新建对话（等价 `/new`），可选 `new_conversation_notice` 提示。
- `debug_mode` 空跑：判定照常并记日志，但不注入、不切对话、不落话题状态。
- 指令族：`/ctm topic status|new [标题]|forget|off|on`；新增 `/topicweave`（别名"话题织机"）。
- 评测台 `tests/eval_topic_detection.py`：读 `platform_message_history`，
  输出混淆矩阵、P/R/F1 与模型调用占比（时间间隔代理真值）。
- 页面概览补充运行模式/自动切换阈值/调试状态。

## v0.3.1

### 新增

- 插件可视化页面 `pages/status/`：只读运行状态面板（核心指标、生效配置、聚合进行中、
  最近决策、最近话题、最近长期记忆、健康检查），每 5 秒自动刷新。
- 页面后端 `ctm/web_api.py`：`/astrbot_plugin_ctm/overview|turns|topics|memories|health`
  五个只读路由；老版本无 `astrbot.api.web` 时整体降级，不影响主流程。
- `Store.recent_turn_logs/recent_topics/stats`、`LongTermStore.recent_items/count_total`、
  `Aggregator.active_rounds` 等页面数据接口。

## v0.3.0

### 新增

- 内建消息聚合（`ctm/aggregation.py`）：按 `(会话, 发送者)` 分桶、防抖 2s、
  总等待上限 12s；连发消息合并成一轮，只触发一次判定与一次 LLM 调用。
- 双层话题判定（`ctm/topic_judge.py`）：启发式（相似度/承接词/换话题词/时间间隔/
  短消息保护）+ 可选 LLM 兜底（超时/报错/JSON 非法一律按延续）。
- 请求钩子兜底判定：消息入口被更高优先级者拦截时，仍在 `on_llm_request` 判定
  并注入提示，落 `WARN_PREEMPTED`。
- 配置：`topic.aggregation_*`、`topic.judge_*`、`topic.detect_in_hook_enabled`。

### 修复

- 软边界增加短消息保护，避免"嗯/继续"在低相似度时误切话题。

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
