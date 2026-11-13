# 对话认知中枢（Cognitive Turn Manager）

> **状态：v0.2（可运行）**。长期记忆（跨会话抽取 / 召回 / 管理）、话题软边界、
> 关系状态与异步归档已实现；`on_message → on_llm_request → on_llm_response`
> 全链路可用，默认配置即可工作，核心零第三方依赖。

CTM 把「话题路由 + 长期记忆 + 人格/关系状态」收敛成一个决策点：每轮只产出
一次 `TurnDecision`，只在 `on_llm_request` 注入一次，归档在后台单 worker 执行。
它替代"多个记忆插件各注入一段 Prompt、互相看不见对方"的做法。

## 能力

| 能力 | 说明 |
| --- | --- |
| 消息聚合 | 同一会话/同一发送者的连发消息先合并成一轮，再判定、只回一次 |
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
| `topic.aggregation_enabled` | `true` | 连发消息合并（默认 2s 防抖，总等待上限 12s） |
| `topic.judge_enabled` | `true` | 话题判定；`judge_provider_id` 留空沿用当前模型 |
| `topic.judge_max_checks_per_hour` | `30` | 每会话每小时模型判定上限 |
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
├── web_api.py          页面只读 API
└── longterm/           长期记忆（config/store/retriever/extractor/manager）
```

## 开发与测试

交接与维护说明见 [docs/HANDOVER.md](docs/HANDOVER.md)；架构设计见 [docs/DESIGN.md](docs/DESIGN.md)。

```bash
python -m compileall .
python -m json.tool _conf_schema.json > /dev/null
python -m unittest discover -s tests -p "test_*.py"
```

`tests/` 不加载 AstrBot：`ctm/*` 与 `ctm/longterm/*` 均为纯标准库，事件对象用
最小替身；`main.py` 的 AstrBot 接线只做导入级 smoke。

## 许可

AGPL-3.0-or-later，与 AstrBot 保持一致。
