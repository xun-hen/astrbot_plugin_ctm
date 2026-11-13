# 交接文档 · astrbot_plugin_ctm

> 面向接手开发/维护者的一页式说明。读完本文应能：跑起来、改得动、知道哪里不能碰、知道下一步做什么。

| 项目 | 内容 |
| --- | --- |
| 插件 | `astrbot_plugin_ctm`（对话认知中枢 · 长期记忆） |
| 版本 | `v0.4.0`（可运行；骨架期设计见 `docs/DESIGN.md`） |
| 仓库 | https://github.com/xun-hen/astrbot_plugin_ctm |
| 分支 | `main`，交接基线 commit `beb87f2` |
| 运行要求 | AstrBot `>= 4.26`，Python 3.10+，**核心零第三方依赖** |
| 数据 | `data/plugin_data/astrbot_plugin_ctm/ctm.sqlite3`（WAL） |
| 状态 | 四种模式 / 自动 /new / 消息聚合 / 双层判定 / 长期记忆 / 软边界 / 关系 / 归档 / 只读状态页均已实现并通过单测；未上架插件市场 |

---

## 1. 一页速览

插件做三件事，且每轮只做一次：

1. **消息到达时**（`on_message`）：规则感知 → 读取话题栈/关系 → 召回候选记忆 → 产出一次 `TurnDecision` → 落到事件 `ctm_decision`。
2. **LLM 请求前**（`on_llm_request`，唯一注入点）：把决策渲染成一段带预算上限的 Prompt 块，追加到 `system_prompt`；同时召回长期记忆，作为**临时** `TextPart`（`mark_as_temp()`）注入，不落历史。
3. **LLM 响应后**（`on_llm_response`）：把本轮数据打包成 `ArchiveJob` 入队，后台单 worker 串行归档（摘要/断言/情节/关系/日志）；同时调度长期记忆抽取。

> 三条不可违反的约定：
> - 所有 hook 吞掉异常（`CancelledError` 除外），插件出错不能变成"消息没人回"；
> - 只在 `on_llm_request` 注入一次，注入量有上限（`injection.max_prompt_growth_ratio`）；
> - 归档只在后台 worker 写库，消息入口/请求前不做重 IO。

---

## 2. 安装与运行

### 2.1 作为插件安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/xun-hen/astrbot_plugin_ctm.git
# 重启 AstrBot 或在 WebUI「插件」页重载
```

首次加载会在 `data/plugin_data/astrbot_plugin_ctm/` 建库并写默认配置。
`basic.enabled=false` 时插件完全停用（不判定、不注入、不归档）。

### 2.2 本地开发

```bash
cd astrbot_plugin_ctm
python -m compileall .
python -m json.tool _conf_schema.json > /dev/null
python -m unittest discover -s tests -p "test_*.py"
```

测试不加载 AstrBot、不连网、不连真实模型；SQLite 用临时目录。
`tests/test_import.py` 在未安装 AstrBot 的环境中自动跳过。

### 2.3 数据与备份

- 主库：`data/plugin_data/astrbot_plugin_ctm/ctm.sqlite3`；
- 备份前可调 `store.checkpoint()`（或 `/ctm export` 导出单会话 JSON 到插件数据目录 `exports/`）；
- 卸载插件不会自动删库，确认不需要后再手动清理。

---

## 3. 目录地图

```
astrbot_plugin_ctm/
├── main.py              AstrBot 接线：4 个 handler + 3 个 LLM 工具 + 生命周期
├── config.py            强类型配置解析（含夹取/归一化），与 _conf_schema.json 双写
├── _conf_schema.json    WebUI 配置 schema（8 个分组）
├── metadata.yaml        插件市场元数据（name/version/repo/astrbot_version）
├── pages/status/        插件详情页（只读运行状态：指标/配置/最近决策/记忆/健康）
├── README.md            用户向说明
├── CHANGELOG.md         版本记录
├── docs/
│   ├── DESIGN.md        架构/不变量/失败码/里程碑（设计基线，部分为路线图）
│   └── HANDOVER.md      本文件
├── ctm/                 核心包（纯标准库，不 import astrbot，可单测）
│   ├── perception.py    规则感知（情绪/粘性/指令/提问），无 IO
│   ├── textsim.py       词法相似度（Jaccard + 字符 2-gram）
│   ├── topic_state.py   话题栈：软边界压栈 / 回溯弹栈 / 硬切
│   ├── memory_store.py  短期记忆召回与打分（摘要/断言/情节）
│   ├── relationship.py  关系状态（亲密度/张力/未完成事项）
│   ├── cognition.py     联合决策 TurnDecision
│   ├── injection.py     Token 预算装箱 + Prompt 渲染
│   ├── archiver.py      异步归档（单 worker + 指数退避重试）
│   ├── store.py         唯一 SQLite 入口（CTM 8 张表）
│   ├── commands.py      /ctm 子命令
│   ├── observability.py 稳定日志码 + 诊断计数
│   ├── longterm/        长期记忆（独立表前缀 lt_）
│   │   ├── config.py    长期记忆配置
│   │   ├── store.py     lt_memories / lt_audit
│   │   ├── extractor.py Prompt + JSON 解析 + 敏感过滤
│   │   ├── retriever.py 打分 + Token 预算 + 渲染
│   │   └── manager.py   召回/入队/抽取/管理
│   ├── enums.py         字符串枚举
│   └── adapters/        外部记忆后端适配器（**未实现，见 §9**）
└── tests/
    ├── test_ctm.py      10 项单测
    ├── test_import.py   AstrBot 导入 smoke（条件跳过）
    └── README.md        测试说明
```

---

## 4. 每轮数据流

```
平台消息
  └─ on_message
       ├─ Aggregator.accept            → 连发合并；后续消息 stop_event
       ├─ Perception.extract           → PerceptionSignal（合并后的文本）
       ├─ TopicStateMachine.load       → TopicState（空栈时建初始话题）
       ├─ TopicJudge.judge             → 启发式/LLM 话题判定
       ├─ MemoryStore.recall_candidates→ RecallItem[]
       ├─ Cognition.decide             → TurnDecision
       ├─ 落地话题动作（soft/hard/continue）、保存状态
       └─ event.set_extra("ctm_decision", decision)

LLM 请求前
  └─ on_llm_request
       ├─ 无决策时 TopicJudge 兜底判定  → WARN_PREEMPTED（不改话题状态）
       ├─ LongTermMemory.recall        → 临时 TextPart（mark_as_temp）
       ├─ InjectionBuilder.build       → CTM Prompt 块
       ├─ InjectionBuilder.cap_by_ratio→ 按比例截断
       └─ req.system_prompt += 块

LLM 响应后
  └─ on_llm_response
       ├─ Archiver.enqueue(ArchiveJob) → 后台单 worker：
       │     摘要 → 断言 → 情节 → 关系 → turn_logs
       └─ LongTermMemory.observe_turn  → 入队（每 N 轮抽一次）
```

### 关键不变量

| # | 不变量 | 落点 |
| --- | --- | --- |
| I1 | 长期记忆注入不落历史 | `TextPart.mark_as_temp()` |
| I2 | 用户/群作用域隔离 | `LongTermMemory._scopes`：`platform:sender` / `platform:group` |
| I3 | 候选态不参与召回 | 召回只查 `status='active'` |
| I4 | 注入有上限 | `max_prompt_growth_ratio` + `injection_budget_tokens` |
| I5 | 归档不阻塞下一轮 | 有界队列（256）+ 单 worker |
| I6 | hook 异常不外泄 | 模块级 handler 的 try/except |
| I7 | 配置非法值回退 | `config.py` 逐项夹取 |
| I8 | 删除需校验可读作用域 | `LongTermMemory.forget(memory_id=...)` |

---

## 5. 数据模型（10 张表）

`ctm/sql/001_init.sql`（`PRAGMA user_version=1`）：

| 表 | 用途 |
| --- | --- |
| `sessions` | 会话主档（`session_id = unified_msg_origin`） |
| `session_state` | 话题计数/切换时间/边界待注入（DDL 补充表，见 DESIGN D1） |
| `topics` | 话题标题、摘要、状态（active/archived/popped） |
| `topic_stack` | 会话话题栈（position 升序，栈顶在最后） |
| `memories` | 短期记忆：`summary` / `assertion` / `episode` |
| `relationship_state` | 亲密度/张力/未完成事项（JSON） |
| `turn_logs` | 每轮决策 JSON + 注入量 + 丢弃项 |
| `archive_failures` | 归档阶段最终失败（`stage/error/payload`） |

`ctm/longterm/store.py`（同库，独立表前缀）：

| 表 | 用途 |
| --- | --- |
| `lt_memories` | 长期记忆（画/偏好/事件/任务/事实/摘要，含 `dedupe_hash` 唯一约束、置信度、证据数、使用次数） |
| `lt_audit` | 长期记忆审计：create/update/promote/forget/prune |

时间统一 Unix 秒（REAL）；枚举存字符串（见 `ctm/enums.py`）。

---

## 6. 配置速查

WebUI 分组（`_conf_schema.json`）：`basic` / `longterm` / `topic` / `memory` / `relationship` / `adapter` / `injection` / `observability`。

### 最常调

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `basic.enabled` | `true` | 插件总开关 |
| `longterm.enabled` | `true` | 长期记忆总开关 |
| `longterm.extract.every_n_turns` | `2` | 每 N 轮抽取一次（成本主旋钮） |
| `longterm.extract.max_calls_per_hour` | `30` | 每会话每小时抽取上限 |
| `longterm.recall.max_tokens` | `800` | 长期记忆注入预算 |
| `longterm.recall.timeout_ms` | `800` | 召回硬超时（超时跳过注入） |
| `longterm.privacy.block_sensitive` | `true` | 密码/密钥/证件号拒写 |
| `memory.injection_budget_tokens` | `1000` | CTM 短期记忆包预算 |
| `injection.max_prompt_growth_ratio` | `0.25` | 注入不超过原 system_prompt 的 25% |
| `topic.auto_switch_strategy` | `soft` | `off` 只判定；`soft` 压栈（推荐）；`hard` 直接清栈 |
| `topic.min_turns_before_switch` | `3` | 新话题最少轮数 |
| `topic.switch_cooldown_seconds` | `120` | 两次切换最小间隔 |
| `adapter.backend` | `sqlite` | 目前**只有 sqlite 生效**（见 §9） |

> 配置双写：`config.py` 默认值与 `_conf_schema.json` 必须一致，`tests/test_ctm.py::test_defaults_match_schema` 会锁死关键项。改默认值必须两处同改。

---

## 7. 命令与工具

### `/ctm` 命令（仅管理员）

```
/ctm status                      话题栈、关系、预算、长期记忆统计
/ctm topic status                模式、本会话开关、话题与判定状态
/ctm topic new [标题]            手动新建对话并开新话题
/ctm topic forget                忘记当前话题
/ctm topic off|on                关闭/恢复本会话自动判定
/topicweave ...                  与 /ctm ... 等价（别名）
/ctm topics                      最近话题
/ctm memory list [type]          短期记忆（summary/assertion/episode）
/ctm memory edit <id> <content>  修正 + 降权
/ctm memory delete <id>          删除短期记忆
/ctm memory pin <id>             提高重要度
/ctm longterm list               长期记忆列表
/ctm longterm delete <id前缀>    删除长期记忆
/ctm switch hard                 硬切（只重置 CTM 话题栈，不调用 AstrBot /new）
/ctm export                      导出会话 JSON（群聊需管理员）
/ctm replay <turn_id>            回放决策
/ctm health                      DB / 归档队列 / 长期记忆健康
```

### LLM 工具（自动挂载，`longterm.tools.enabled` 控制）

| 工具 | 作用 |
| --- | --- |
| `memory_search` | 按 query 检索长期记忆（user/group/auto） |
| `memory_save` | 显式保存（用户明确要求记住时） |
| `memory_forget` | 按 id / query 删除（遗忘、纠错） |

---

## 8. 日志与排障

稳定日志码见 `ctm/observability.py`：`INFO_DECISION`、`WARN_NO_DECISION`、`WARN_BUDGET_OVERFLOW`、`WARN_PROMPT_GROWTH_CAPPED`、`WARN_ARCHIVE_DROPPED`、`ERROR_INJECTION`、`ERROR_STORE`、`ERROR_ARCHIVE`、`WARN_ADAPTER_FALLBACK` 等。

常见问题：

| 现象 | 先看 | 处理 |
| --- | --- | --- |
| 完全不工作 | `basic.enabled`、插件是否加载失败 | 看 WebUI 插件页错误；`python -m compileall .` |
| 记不住东西 | `longterm.extract.enabled`、`max_calls_per_hour`、日志有无抽取失败 | 调大频率/换便宜模型；确认 provider 可用 |
| 没有注入 | `longterm.recall.enabled`、`timeout_ms`、候选是否为空 | `/ctm longterm list` 看库里有没有 |
| 注入太长/太短 | `max_tokens`、`injection_budget_tokens`、`max_prompt_growth_ratio` | 按预算调 |
| 归档丢任务 | `WARN_ARCHIVE_DROPPED`、`archive_failures` 表 | 队列 256，满说明写库慢或模型慢 |
| 群聊串号 | 作用域是否 `platform:sender` / `platform:group` | 检查 `longterm.recall.include_group_scope` |

`/ctm health` 返回 store 状态、归档队列长度、长期记忆是否启用。

---

## 9. 已知缺口与 TODO（按优先级）

### P0：接手前必须知道

1. **适配器未实现**：`ctm/adapters/`（sqlite/living_memory/memory_companion 与 `create_adapter`）仍是 `NotImplementedError` 骨架，**运行时未接入**；`adapter.backend` 配置目前不生效，实际始终用内置 SQLite。要么实现，要么在 schema 里标注"预留"。
2. **硬切语义有限**：`/ctm switch hard` 只清空 CTM 话题栈并把旧话题置 archived，**不会**清空 AstrBot 的会话历史（没有调用 `/new`）。文案已在 README 说明，但产品上需确认是否符合预期。
3. **长期记忆召回是同步 SQL**：`on_llm_request` 中直接读 SQLite（短查询 + 索引 + 800ms 超时兜底）。数据量大时需改为 `asyncio.to_thread` 或缓存。

### P1：下一迭代建议

4. **无 Embedding/rerank**：目前纯词法召回（`w_lexical/w_importance/w_recency`）。接口已留在 `textsim.SimilarityScorer`，接 Embedding 时只替换打分器，不改判定代码。
5. **页面只读**：`pages/status/` 只展示指标/配置/最近决策/话题/记忆/健康，没有编辑与删除；
   写操作仍走 `/ctm` 命令与 LLM 工具。
6. **`/ctm memory` 只覆盖短期记忆**：长期记忆走 `longterm list/delete`，命令层级可合并（`/ctm memory --longterm`）。
7. **进程内字典无上限**：`main._pending_old_topic/_pending_emotion/_pending_sticky` 按会话累积，长期运行建议加清理（参考 `longterm` 的 5000 上限做法）。
8. **集成测试缺失**：目前只有纯标准库单测 + 导入 smoke；建议补"真实事件替身 → 三个 hook → 落库"的端到端用例（fake provider）。

### P2：可选增强

9. 话题标题目前是"截断 16 字"规则，可换轻量模型；
10. `archive_failures` 无自动重放工具，可加 `/ctm replay-failures`；
11. `turn_logs` 无自动清理（`purge_turn_logs` 已实现但未调度）；
12. 插件市场尚未提交（见 §12）；
13. 聚合是精简版：未实现输入状态感知、撤回过滤、图片转存、聚合统计页（完整版可参考
    `astrbot_plugin_msg_aggregator` / 话题路由器 PRO 的 `aggregation/`）；
14. 判定模型未配置时只用启发式；`judge_always_llm` 成本较高，默认关闭。

---

## 10. 变更与回滚

| 变更 | 方式 |
| --- | --- |
| 回滚 v0.2 代码 | `git revert beb87f2 && git push origin main` |
| 临时停用 | WebUI 关 `basic.enabled` 或卸载插件（保留数据库） |
| 回滚数据库 schema | v0.2 未改 `001_init.sql`，`user_version=1`；删除 `ctm.sqlite3` 即重建（会丢数据，先备份） |
| 长期记忆单独关闭 | `longterm.enabled=false`（CTM 其余能力不受影响） |

---

## 11. 接手第一天清单

- [ ] 按 §2 装插件，发一条消息，确认日志出现 `INFO_DECISION`；
- [ ] `/ctm status` 有话题/关系/预算输出；
- [ ] 连发两条消息（2 秒内），确认只回一次且日志只有一条 `INFO_DECISION`；
- [ ] 发一条"我叫 X / 我住在 Y"，等 2~4 轮后 `/ctm longterm list` 能看到；
- [ ] 新开会话问"我叫什么"，确认长期记忆被注入（回复里能体现）；
- [ ] `/ctm memory edit <id> 新内容` 后 `corrected_count` 增加、重要度下降；
- [ ] `/ctm health` 三项正常；
- [ ] 跑一遍 §2.2 的测试命令。

---

## 12. 外部依赖与账号

| 项 | 说明 |
| --- | --- |
| GitHub | `xun-hen/astrbot_plugin_ctm`，SSH push 已验证可用（`git@github.com`） |
| 插件市场 | 通过 [cloud.astrbot.app/publish](https://cloud.astrbot.app/publish) 提交，需要 AstrBot Cloud 账号；`metadata.yaml` 已就绪，压缩包 < 16MB |
| 模型 | 不内置 key；抽取复用当前会话模型，可配 `longterm.extract.provider_id` 指定便宜模型 |
| 密钥 | 仓库内无任何密钥/令牌；`requirements.txt` 仅注释（零第三方依赖） |

---

## 13. 术语表

| 词 | 含义 |
| --- | --- |
| CTM | Cognitive Turn Manager，本插件 |
| 软边界 `soft_boundary` | 换话题只压栈、不清历史；注入一条边界提示 |
| 硬切 `hard_switch` | 清空 CTM 话题栈（不等于 AstrBot `/new`） |
| 短期记忆 | `memories` 表：话题摘要 / 断言 / 情节 |
| 长期记忆 | `lt_memories` 表：跨会话的用户/群画像、事件、任务 |
| 断言 | 用户陈述的事实/偏好，如"用户喜欢猫" |
| 情节 | 有情感显著性的具体事件 |
| 候选态 `candidate` | 低置信度记忆，不参与注入，再次出现才转正 |

---

*本文随版本维护：改动 hook 顺序、配置键、表结构或已知缺口时，请同步更新本文件与 `README.md`。*
