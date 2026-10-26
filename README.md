# 对话认知中枢（Cognitive Turn Manager）

> **状态：v0.1 骨架**。模块边界、接口签名、DDL、配置与失败码已定稿；
> 判定/召回/注入/归档的**逻辑体全部留空**（`raise NotImplementedError`），
> 按 `docs/DESIGN.md` 的里程碑填实现即可。

CTM 把「话题路由 + 长期记忆 + 人格/关系状态」收敛成一个决策点：每轮只产出
一次 `TurnDecision`，只在 `on_llm_request` 注入一次，归档异步执行。
它替代"多个记忆插件各注入一段 Prompt、互相看不见对方"的做法。

## 能力（目标态）

| 能力 | 说明 |
| --- | --- |
| 软话题边界 | 换话题只压栈、不清空上下文；回溯旧话题时弹栈自然衔接 |
| 记忆包 | 话题摘要 / 用户断言 / 情节记忆，按 Token 预算装箱注入 |
| 关系状态 | 亲密度、张力、未完成事项；慢变量，小幅更新 + 自然衰减 |
| 人格模式 | daily / comfort / playful / serious，由情绪、张力与粘性命中决定 |
| 单点注入 | 全插件只有 `on_llm_request` 改 `system_prompt`，注入量有上限 |
| 异步归档 | 单 worker 串行写 SQLite，失败重试并落 `archive_failures` |
| 可回退 | `auto_switch_strategy=hard` 回到旧行为；`enabled=false` 完全停用 |

## 安装

1. 将本目录放到 `data/plugins/astrbot_plugin_ctm/`；
2. 重启 AstrBot 或在面板中重载；
3. 首次加载会在 `data/plugin_data/astrbot_plugin_ctm/` 下建 `ctm.sqlite3`。

要求 AstrBot `>= 4.26`，Python 3.10+，核心零第三方依赖。

## 配置要点

默认配置即可用。三个最常调的项：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `topic.auto_switch_strategy` | `soft` | `off` 只判定不动作；`soft` 压栈；`hard` 等价 `/new`（默认不推荐） |
| `memory.injection_budget_tokens` | `1000` | 每轮注入预算；超了按优先级丢，并记录 dropped |
| `injection.max_prompt_growth_ratio` | `0.25` | 注入不超过原 system_prompt 的 25% |

**不要**与 `astrbot_plugin_msg_aggregator` 同装（CTM 自带聚合层，优先级同为
1000，两者会互相抢消息）。若同时启用其他记忆插件的"自动注入"，请关掉它，
否则会重复注入（CTM 会记 `WARN_DUPLICATE_INJECTION`）。

## 命令

```
/ctm status              当前会话话题栈、关系状态、预算
/ctm topics              最近话题及摘要
/ctm memory list [type]  记忆列表
/ctm memory edit <id> <content>  修正记忆（降权 + corrected_count）
/ctm memory delete <id>  删除记忆
/ctm memory pin <id>     提高重要度
/ctm switch hard         显式硬切换
/ctm export              导出本会话数据
/ctm replay <turn_id>    回放某轮决策
/ctm health              适配器 / DB / 队列健康检查
```

命令仅管理员可用；`export` 在群聊默认拒绝。

## 开发

- 架构、不变量、失败码、里程碑与测试矩阵：[docs/DESIGN.md](docs/DESIGN.md)
- 测试计划：[tests/README.md](tests/README.md)
- 配置默认值与 `_conf_schema.json` **必须逐项一致**（有测试锁死）。

```bash
# 仅静态检查（骨架阶段）
python -m compileall astrbot_plugin_ctm
python -m json.tool astrbot_plugin_ctm/_conf_schema.json > /dev/null
```

## 目录

```
astrbot_plugin_ctm/
├── main.py            AstrBot 接线（hook / 生命周期 / 单例）
├── config.py          强类型配置（与 _conf_schema.json 对齐）
├── _conf_schema.json  面板配置 schema
├── ctm/               核心包（不 import astrbot，可单测）
│   ├── perception.py  规则感知（<5ms）
│   ├── topic_state.py 话题栈 / 软边界 / 弹栈 / 硬切
│   ├── memory_store.py 候选召回与打分
│   ├── relationship.py 关系状态
│   ├── cognition.py   联合决策
│   ├── injection.py   预算装箱 + 模板渲染
│   ├── archiver.py    异步归档（单 worker 串行写）
│   ├── store.py       唯一 SQLite 入口
│   ├── adapters/      sqlite / living_memory / memory_companion
│   ├── commands.py    /ctm 子命令
│   ├── observability.py 日志码 + 诊断计数
│   └── sql/001_init.sql DDL
├── docs/DESIGN.md     技术与落地设计
└── tests/             测试计划（待实现）
```
