# 对话认知中枢（Cognitive Turn Manager）

> **状态：v0.5.1（可运行）**。长期记忆（跨会话抽取 / 召回 / 管理）、话题软边界、
> 关系状态与异步归档已实现；`on_message → on_llm_request → on_llm_response`
> 全链路可用，默认配置即可工作，核心零第三方依赖。

CTM 把「话题路由 + 长期记忆 + 人格/关系状态」收敛成一个决策点：每轮只产出
一次 `TurnDecision`，只在 `on_llm_request` 注入一次，归档在后台单 worker 执行。
它替代"多个记忆插件各注入一段 Prompt、互相看不见对方"的做法。

## 能力

| 能力 | 说明 |
| --- | --- |
| 运行模式 | `off / mark / hint / auto`：只记录、只提示、或置信度达标时自动新建对话（等价 `/new`） |
| 消息聚合 | 同一会话/同一发送者的连发消息先合并成一轮，再判定、只回一次 |
| 自适应防抖 | 按用户最近的发消息节奏自动调合并窗口（0.8–6s）：爱连发的人少回几次，一句一条的人回得更快 |
| 整段摘要 | 切分话题时把该话题的完整对话交给模型压成四段式摘要（在解决什么/结论与参数/未完成/踩过的坑），写入 `topics.summary` 与 `memories(summary)` |
| 话题判定 | 启发式（相似度/承接词/换话题词/时间间隔）优先，拿不准时问一次便宜模型；失败一律按延续 |
| 长期记忆 | 跨会话记住用户/群聊的稳定事实；回复后异步抽取，请求前按预算召回 |
| 作用域隔离 | `user = platform:sender`、`group = platform:group`，群聊可同时读两者，成员之间互不串号 |
| 软话题边界 | 换话题只压栈、不清空上下文；回溯旧话题时弹栈自然衔接 |
| 短期记忆包 | 话题摘要 / 用户断言 / 情节记忆，按 Token 预算装箱注入 |
| 关系状态 | 亲密度、张力、未完成事项；慢变量，小幅更新 + 自然衰减 |
| 人格模式 | daily / comfort / playful / serious，由情绪、张力与粘性命中决定 |
| 单点注入 | 全插件只在 `on_llm_request` 追加一次，注入量有上限、有日志 |
| 异步归档 | 单 worker 串行写 SQLite，失败重试并落 `archive_failures` |
| 可视化页面 | 插件详情页「运行状态」：核心指标、生效配置、聚合进行中、最近决策/话题/记忆、健康检查（只读） |
| 可管理 | `/ctm` 命令 + `memory_search / memory_save / memory_forget` 工具 |
| 自检 | `/ctm doctor` + 状态页红条：白名单 / handler 绑定 / 入口心跳 / DB / 版本一屏看清 |
| 可回退 | `basic.enabled=false` 完全停用；长期记忆可单独关闭 |

## 安装

1. 将本目录放到 `data/plugins/astrbot_plugin_ctm/`（插件市场安装同名插件亦可）；
2. 重启 AstrBot 或在面板中重载；
3. 首次加载会在 `data/plugin_data/astrbot_plugin_ctm/` 下建 `ctm.sqlite3`。

要求 AstrBot `>= 4.26`，Python 3.10+。

## 配置要点

默认配置即可用。最常调的几项：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `topic.aggregation_enabled` | `true` | 连发消息合并（总等待上限 12s） |
| `topic.aggregation_adaptive_enabled` | `true` | 自适应防抖：按用户节奏在 `adaptive_min`–`adaptive_max` 间浮动 |
| `topic.aggregation_adaptive_min_seconds` | `0.8` | 自适应窗口下限（习惯单发的用户走这里） |
| `topic.aggregation_adaptive_max_seconds` | `6.0` | 自适应窗口上限 |
| `topic.aggregation_debounce_seconds` | `2.0` | 基础/冷启动窗口：样本不足时用它 |
| `topic.judge_enabled` | `true` | 话题判定；`judge_provider_id` 留空沿用当前模型 |
| `topic.judge_max_checks_per_hour` | `30` | 每会话每小时模型判定上限 |
| `topic.judge_min_message_length` | `2` | 短于此按延续处理；中文短句信息密度高，默认 2 |
| `topic.min_turns_before_switch` | `4` | 新话题至少持续几轮才允许再切（防抖抖动） |
| `topic.mode` | `hint` | 运行模式：`off` 不判定 / `mark` 只记录 / `hint` 判定并注入 / `auto` 自动新建对话 |
| `topic.auto_threshold` | `0.8` | `auto` 模式切换对话所需置信度 |
| `topic.debug_mode` | `false` | 空跑：判定照常但不注入、不切对话、不落状态 |
| `topic.new_conversation_notice` | `false` | 自动切换时是否回一句提示 |
| `longterm.enabled` | `true` | 长期记忆总开关 |
| `longterm.extract.every_n_turns` | `2` | 每多少轮抽取一次；成本主要旋钮 |
| `longterm.extract.max_calls_per_hour` | `30` | 每会话每小时抽取上限 |
| `longterm.recall.max_tokens` | `800` | 长期记忆注入预算 |
| `longterm.recall.timeout_ms` | `800` | 召回硬超时，超时即跳过注入 |
| `memory.injection_budget_tokens` | `1000` | CTM 短期记忆包注入预算 |
| `injection.max_prompt_growth_ratio` | `0.25` | 注入不超过原 system_prompt 的 25% |
| `topic.auto_switch_strategy` | `soft` | `off` 只判定不动作；`soft` 压栈（推荐） |
| `topic.detect_in_hook_enabled` | `true` | 消息入口被抢占时，在请求钩子兜底判定并注入 |
| `basic.enabled` | `true` | 插件总开关 |

敏感内容（密码、密钥、证件号、银行卡号）默认拒绝写入长期记忆。

## 命令（仅管理员）

```
/ctm status              当前会话话题栈、关系状态、预算、长期记忆统计
/ctm topic status        当前模式、本会话开关、话题与判定状态
/ctm topic new [标题]    手动新建对话并开新话题（等价 /new）
/ctm topic forget        忘记当前话题，下一条重新建档
/ctm topic off|on        关闭/恢复本会话的自动话题判定
/topicweave ...          与 /ctm ... 等价（别名，含"话题织机"）
/ctm topics              最近话题及摘要
/ctm memory list [type]  短期记忆列表（summary/assertion/episode）
/ctm memory edit <id> <content>  修正记忆（降权 + corrected_count）
/ctm memory delete <id>  删除短期记忆
/ctm memory pin <id>     提高重要度
/ctm longterm list       长期记忆列表
/ctm longterm delete <id前缀>  删除长期记忆
/ctm switch hard         显式硬切换（等价 /new）
/ctm export              导出本会话数据到插件数据目录
/ctm replay <turn_id>    回放某轮决策
/ctm health              DB / 归档队列 / 长期记忆健康检查
```

## 数据与隐私

- 数据默认落在 `data/plugin_data/astrbot_plugin_ctm/ctm.sqlite3`，WAL 模式；
- 长期记忆只存抽取后的陈述，不存原始对话全文（会话摘要除外）；
- 每条记忆可定位、可删除，写入与删除都记 `lt_audit`；
- 群聊作用域与用户作用域分离；删除按 id 时会校验当前事件可读作用域。

## 架构

```
main.py                 AstrBot 接线（hook / 生命周期 / 工具）
config.py               强类型配置（与 _conf_schema.json 对齐）
pages/status/           插件详情页（只读运行状态，bridge + 5 个页面 API）
ctm/
├── aggregation.py      连发消息防抖聚合（合并成一轮）
├── adaptive.py         自适应防抖（按用户发消息节奏调窗口）
├── topic_judge.py      话题判定：启发式 + 可选 LLM 兜底
├── perception.py       规则感知（无 IO，P99 < 5ms）
├── textsim.py          词法相似度（Jaccard + 字符 2-gram）
├── topic_state.py      话题栈 / 软边界 / 弹栈 / 硬切
├── memory_store.py     短期记忆候选召回与打分
├── relationship.py     关系状态
├── cognition.py        联合决策 TurnDecision
├── injection.py        Token 预算装箱 + 模板渲染
├── archiver.py         异步归档（单 worker + 重试）
├── store.py            唯一 SQLite 入口
├── commands.py         /ctm 子命令
├── observability.py    日志码与诊断计数
├── bindings.py         handler 绑定自检（防"静默不派发"）
├── web_api.py          页面只读 API
└── longterm/           长期记忆（config/store/retriever/extractor/manager）
```

## 开发与测试

交接与维护说明仅本地保留、不随仓库分发；架构设计见 [docs/DESIGN.md](docs/DESIGN.md)。

```bash
python -m compileall .
python -m json.tool _conf_schema.json > /dev/null
python -m unittest discover -s tests -p "test_*.py"
python tests/eval_topic_detection.py --db ~/.astrbot/data/data_v4.db
```

`tests/eval_topic_detection.py` 用真实聊天记录跑启发式判定，输出混淆矩阵、
精确率/召回率/F1 与模型调用占比（时间间隔代理真值）。`tests/` 默认不加载 AstrBot：
`ctm/*` 与 `ctm/longterm/*` 均为纯标准库，事件对象用最小替身；需要 AstrBot 的
接线测试（`test_import.py` / `test_entry_and_fallback.py`）在本机装了 AstrBot 时
自动运行，否则条件跳过。

## 兼容性

| 项 | 说明 |
| --- | --- |
| AstrBot 版本 | `>= 4.26`。已在 4.26.5、4.28.1、4.29.1 上验证（handler 绑定/派发语义三版一致） |
| 第三方依赖 | 无，纯标准库（SQLite 用内置） |
| 页面 | 需要支持 `astrbot.api.web` 的版本；不支持时页面后端自动降级，`/ctm doctor` 仍可用 |
| 同装冲突 | **不要**与 `astrbot_plugin_msg_aggregator` / `astrbot_plugin_topic_router_pro` 同装：三者都抢 `priority=1000`，会互相抢占事件、重复判定 |
| 装完第一步 | 发 `/ctm doctor`，六项全 `ok` 才算真正生效（白名单是最容易漏的一项） |

## 常见坑（上架前必读）

按踩坑概率排序，完整判定步骤与解决办法见 **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)**。

| 坑 | 现象 | 解决办法 |
| --- | --- | --- |
| 不在全局 `plugin_set` 白名单 | 已启用、能回复，但插件零日志零数据 | WebUI → 配置 → 插件 → 把 `astrbot_plugin_ctm` 加入 `plugin_set`（或设 `["*"]`） |
| 与同 `priority=1000` 插件同装 | 消息被合并两次/判定重复/偶尔收不到 | 只留一个消息入口插件 |
| 会话级关闭 | 只有某个会话不工作 | 该会话发 `/ctm topic on` |
| 总开关或模式关着 | 完全不动 | 开 `basic.enabled`，`topic.mode` 改 `hint` |
| 数据库在网络盘/无写权限 | 时好时坏、`database is locked` | `basic.db_path` 指到本地可写目录 |
| AstrBot < 4.26 | 页面打不开 | 升级；命令能力不受影响 |

装完第一件事：`/ctm doctor`，六项全 `ok` 才算生效。

## 排障

插件"看起来正常但完全没反应"时，**先跑 `/ctm doctor`**（或看插件页顶部的
自检红条），它会把下面每一道门禁逐条判定并给出修复动作。也可以按顺序人工看：

0. **全局白名单**：`/ctm health` 的 `plugin_set` 一行，或搜 `WARN_PLUGIN_SET`。
   AstrBot 的 `plugin_set` 是全局白名单（`["*"]` = 全部），**不在名单里的插件
   handler 会被静默丢弃**，症状与"插件没装"完全一样。新装插件最容易踩。
1. **启动自检**：搜日志 `handler 绑定自检`。若出现
   `WARN_HANDLER_BINDING ... handler 必须是本插件的类方法`，说明 handler
   被写成了模块级函数——框架会把实例 partial 到第一个位置参数，派发时
   `TypeError` 并被吞掉（v0.4.0 的故障）。
2. **入口有没有进来**：默认每条消息有一条 `INFO_ENTRY`（含 `session_id`、`text_len`）。
   有它但没有 `INFO_DECISION` → 搜 `DEBUG_SKIP`，那里的 `reason` 就是卡住的步骤
   （`disabled` / `mode_off` / `session_off` / `empty_text` / `aggregating_follower` /
   `inject_off` …）。把面板日志级别调到 DEBUG 才能看到 `DEBUG_SKIP`。
3. **请求钩子有没有兜底**：搜 `WARN_NO_DECISION`（带 reason）与
   `WARN_PREEMPTED`。只有请求钩子被调用、`entries=0` 时，多半是同优先级的其他
   插件 `stop_event()` 抢占了事件，或本插件被会话级禁用；`/ctm health` 会直接提示。

`/ctm health` 会打印 `handlers: ok（N 个已绑定本实例）` 与入口计数
（`entries / decisions / skips / no_decision / llm_requests`）。

## 许可

AGPL-3.0-or-later，与 AstrBot 保持一致。
