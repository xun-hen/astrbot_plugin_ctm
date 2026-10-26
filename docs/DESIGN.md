# 对话认知中枢（CTM）技术设计 · 骨架版

| 项目 | 内容 |
| --- | --- |
| 文档编号 | TDD-CTM-001 |
| 版本 | **v0.2-framework**（骨架定稿：模块边界 / 接口 / DDL / 失败码 / 里程碑） |
| 状态 | 骨架待评审；通过后按 §10 的 M1 填实现 |
| 最后更新 | 2026-10-26 |
| 目标平台 | AstrBot `>= 4.26`，Python 3.10+ |
| 代码基线 | 本地 `astrbot-src/`（hook 与 `ProviderRequest` 已核对） |
| 关联 | `astrbot_plugin_topic_router_pro`（迁移来源，源码不在本仓库）、`astrbot_plugin_msg_aggregator`（聚合层教训来源） |

### 变更记录

| 版本 | 变更 |
| --- | --- |
| v0.1 | 首版：话题栈 + 软边界 + 统一注入 + 决策日志 |
| v0.2 | 详细设计：断言/情节、关系状态、预算装箱、适配器、DDL、测试矩阵、迁移点 |
| **v0.2-framework** | **本次**：落成可填实现的骨架。新增 §2 文件地图、§5 十条工程不变量、§8 失败码矩阵、§12 已知缺口（D1、GAP-1~7、Q1~Q6）；修正 v0.2 中三处"设计成立但落地缺表/缺触发器"的问题 |

> **骨架版的判断**：v0.2 的设计在"做什么"上已经完整，但有三处只在纸上成立：
> ① 话题状态机需要**逐轮持久化三个会话级计数**，DDL 里没有表（D1）；
> ② 文档给了 `pop_to_previous()` 却没定义**何时**弹栈（GAP-2）；
> ③ `turns_since_switch` 的语义没写死，直接实现会与 `min_turns_before_switch`
> 互相抵消（GAP-3）。这三处不先钉死，M1 写出来的东西会"能跑但判定永远不触发"。

---

## 1. 定位与边界

CTM 是**边界传感器 + 上下文编排器**，不是"更大的记忆插件"：

* 它决定"话题变没变"（边界），决定"这一轮该带哪些记忆"（编排），
  并用一个人格模式解释"为什么这样接话"（解释）；
* 它不负责平台适配、不做消息发送、不做命令解析（交给 AstrBot 与其他插件）；
* 它不重新实现向量检索；向量只是 §3.2 相似度栈的一个可选实现。

### 1.1 做什么 / 不做什么

| 做 | 不做 |
| --- | --- |
| 每轮一次联合决策（`TurnDecision`） | 每个模块各注入一段 Prompt |
| 软边界压栈、回溯弹栈 | 换话题就清空上下文 |
| 按 Token 预算装箱注入 | 无上限地把所有记忆塞进 Prompt |
| 归档异步、单 worker 串行写 | 在消息链路上做同步数据库写 |
| 适配外部记忆后端，失败回退 | 依赖外部插件才能启动 |
| 决策可回放（`turn_logs`） | 只写"已注入"日志，无法解释为什么 |

### 1.2 与三个既有件的关系

| 既有件 | 关系 | 落地方式 |
| --- | --- | --- |
| 话题路由器Pro | 升级来源：它的感知与话题判定并入 CTM | §13 的 patch 点；旧插件在 CTM 启用后应停用 |
| msg_aggregator | **不得同装**：CTM 自带聚合层（priority=1000） | 聚合实现见 GAP-4（v0.1 先逐条处理） |
| LivingMemory / memory_companion | 可选后端 | `ctm/adapters/`，失败回退 SQLite，永不阻塞启动 |

---

## 2. 文件地图与依赖方向

```
astrbot_plugin_ctm/
├── metadata.yaml          插件元数据
├── _conf_schema.json      面板 schema（默认值的唯一来源之一）
├── config.py              强类型配置（另一个默认值来源）+ load_config()
├── main.py                接线：4 个 hook、生命周期、单例、DB 路径解析
├── ctm/
│   ├── enums.py           全部字符串枚举（与 DB 列/JSON 一一对应）
│   ├── observability.py   LogCode + log_event + Diag 计数
│   ├── perception.py      PerceptionSignal / Perception（纯规则，<5ms）
│   ├── textsim.py         SimilarityScorer / LexicalScorer / topic_similarity
│   ├── topic_state.py     TopicNode / TopicState / TopicStateMachine
│   ├── memory_store.py    RecallItem / MemoryStore（召回 + 打分 + 写入）
│   ├── relationship.py    RelationshipState / RelationshipManager
│   ├── cognition.py       TurnDecision / Cognition（决策顺序的唯一实现）
│   ├── injection.py       InjectionBuilder（plan/pack/build/cap）+ 模板
│   ├── archiver.py        ArchiveJob / Archiver（asyncio 单 worker）
│   ├── store.py           Store：唯一 SQL 出入口
│   ├── commands.py        CtmCommands（/ctm 子命令）
│   ├── adapters/          MemoryAdapter + sqlite / living_memory / memory_companion
│   └── sql/001_init.sql   DDL
├── docs/DESIGN.md         本文件
└── tests/                 测试计划与后续用例
```

### 2.1 依赖规则

| 规则 | 理由 |
| --- | --- |
| `ctm/*` **不得 import `astrbot`** | 核心逻辑可脱离 AstrBot 单测；唯一依赖点在 `main.py`，两个外部适配器只接收 `Context` 参数 |
| `ctm/store.py` 不 import 上层模块，只收发 `dict` | 避免"实体类 ↔ 持久层"循环；行结构由 DDL 定义 |
| `ctm/injection.py` 只在 `TYPE_CHECKING` 下 import `cognition` | 打破 cognition ↔ injection 的运行时循环 |
| `config.py` 是叶子模块 | 被 `main.py` 与 `ctm/*` 单向消费；它不 import 任何业务模块 |

`main.py` 是唯一的装配点：它构造 Store → 四个门面 → Archiver，并把
`event.set_extra("ctm_decision", ...)` 作为唯一跨 hook 的传递通道。

---

## 3. 每轮时序

```
消息事件（priority=1000）
  ├─ on_message
  │    ├─ [聚合]（GAP-4：v0.1 逐条）
  │    ├─ Perception.extract → PerceptionSignal
  │    ├─ is_command ? → 放行（不决策）
  │    ├─ TopicStateMachine.load
  │    ├─ MemoryStore.recall_candidates
  │    ├─ RelationshipManager.load
  │    ├─ Cognition.decide → TurnDecision
  │    ├─ _apply_topic_action（状态落地 + 保存）
  │    └─ event.set_extra("ctm_decision", decision)
  │
  ├─ on_llm_request
  │    ├─ 取决策；缺失 → WARN_NO_DECISION，不注入
  │    ├─ InjectionBuilder.build（预算装箱结果已在决策里）
  │    ├─ cap_by_ratio（注入 ≤ 原 prompt 25%）
  │    ├─ req.system_prompt += payload（只追加）
  │    ├─ 记录 turn_logs（可晚一拍）
  │    └─ soft_boundary_once → 清除 pending 标记
  │
  ├─ LLM
  │
  └─ on_llm_response
       ├─ 组装 ArchiveJob（只带文本与快照）
       └─ Archiver.enqueue（满则丢弃并记 WARN_ARCHIVE_DROPPED）
```

### 3.1 入口点速查

| 阶段 | 函数 | 位置 | 关键约束 |
| --- | --- | --- | --- |
| 消息入口 | `ctm_on_event` → `CtmPlugin.on_message` | `main.py` | 吞异常；不阻断；指令放行 |
| 请求前 | `ctm_on_llm_request` → `CtmPlugin.on_llm_request` | `main.py` | 只追加；不改会话；失败降级 |
| 响应后 | `ctm_on_llm_response` | `main.py` | 只入队；不 await 归档结果 |
| 命令 | `ctm_command` | `main.py` | 仅管理员；群聊拒绝 export |
| 卸载 | `CtmPlugin.terminate` | `main.py` | flush ≤5s；checkpoint；关连接 |

### 3.2 异常路径

| 场景 | 行为 | 日志码 |
| --- | --- | --- |
| 请求前拿不到决策 | 不注入，正常放行 | `WARN_NO_DECISION` |
| 注入构建抛异常 | 追加"最小关系块"；关系也没有则完全不注入 | `ERROR_INJECTION` |
| 注入超预算 | 按优先级截断，记录 dropped；固定块超限先砍低优先级固定块 | `WARN_BUDGET_OVERFLOW` |
| 注入超过原 prompt 比例 | 按行截断（保留模板骨架） | `WARN_PROMPT_GROWTH_CAPPED` |
| 归档单阶段失败 | 重试 2 次（0.5s/1s 退避），最终落 `archive_failures` | `ERROR_ARCHIVE` |
| 归档队列满 | 丢弃该轮并计数；**不反压消息链路** | `WARN_ARCHIVE_DROPPED` |
| 外部适配器不可用 | 回退 SQLite（无论 `fallback_sqlite` 开关） | `WARN_ADAPTER_FALLBACK` |
| 数据库读失败 | 返回空状态/空候选，判定退化为"延续" | `ERROR_STORE` |

---

## 4. 接口契约

### 4.1 数据结构

| 类型 | 关键字段 | 不变量 |
| --- | --- | --- |
| `PerceptionSignal` | session_id / user_id / message_id / text / sticky_hit / emotion_hint / emotion_score | 不可变；字段缺失降级为空串，不抛异常 |
| `TopicNode` | topic_id / title / summary / status / last_active_at | `status` 只取 active/archived/popped |
| `TopicState` | stack / current_topic / previous_topic / turns_since_switch / last_switch_at / soft_boundary_pending | `current_topic == stack[-1].title`；读取一律走 `focus()` |
| `RecallItem` | memory_id / memory_type / content / score / 四个分量 / reason | 分数已加权；`reason` 必填 |
| `TurnDecision` | turn_id / topic_action / persona_mode / recall_items / estimated_tokens / dropped_items / reason / relationship 快照 | 不可变；注入与归档只读它 |
| `RelationshipState` | intimacy / tension / pending_promises / updated_at | 两个数值恒在 [0,1] |
| `ArchiveJob` | turn_id / user_text / response_text / topic_action / decision_json / injected_tokens | **不含 event 对象**；决策以 JSON 快照随任务携带 |

### 4.2 类与方法

| 类 | 文件 | 公开方法（骨架已给签名） |
| --- | --- | --- |
| `Perception` | perception.py | `extract` / `_detect_emotion` / `_check_sticky` / `_looks_like_command` / `_is_question` |
| `TopicStateMachine` | topic_state.py | `load` / `save` / `should_soft_boundary` / `should_pop_back` / `soft_boundary` / `pop_to_previous` / `hard_switch` / `title_for` / `is_sticky` |
| `MemoryStore` | memory_store.py | `recall_candidates` / `mark_recalled` / `update_importance` / `save_summary` / `save_assertion` / `save_episode` / `_score` / `_recency` / `_relevance` |
| `RelationshipManager` | relationship.py | `load` / `save` / `update_after_turn` / `decay` / `level` / `detect_promises` / `pending_summary` |
| `Cognition` | cognition.py | `decide` / `_topic_action` / `_pick_persona` / `_new_turn_id` |
| `InjectionBuilder` | injection.py | `plan` / `pack` / `build` / `estimate_tokens` / `cap_by_ratio` |
| `Archiver` | archiver.py | `start` / `stop` / `enqueue` / `archive_async` / `_archive_one` / `_summarize_topic` / `_extract_assertions` / `_detect_episode` |
| `Store` | store.py | 会话/话题/栈/记忆/关系/日志/失败/导出，共 20+ 个方法 |
| `CtmCommands` | commands.py | `dispatch` + 9 个子命令 |

`Cognition` 的构造签名比 v0.2 多一个 `topic_sm` 参数：决策需要"要不要切"的判定，
但**不复制**该逻辑，也不在决策里改状态——状态变更由 `on_message` 在决策后
统一落地（`_apply_topic_action`）。这样"决策"保持纯函数，"状态"只有一个写入者。

---

## 5. 工程不变量

| 编号 | 不变量 | 违反后果 | 检查点 |
| --- | --- | --- | --- |
| I1 | 全系统只有 `on_llm_request` 改 `system_prompt` | 重复注入、互相覆盖 | code review + T10 |
| I2 | 两个 LLM hook 只追加，不 `return`/不阻断 | 本轮请求被吞 | code review |
| I3 | 硬切不在 `on_llm_request` 内执行 | 本轮召回被清空 | `hard_switch` 调用点仅消息入口/命令 |
| I4 | 每个 `TurnDecision` 必有 `reason`，并写 `turn_logs` | 无法解释"为什么没切/为什么注入这条" | T05/T12 + `/ctm replay` |
| I5 | 注入量有硬上限（固定块 + 记忆块） | 小模型上下文挤爆 | T05 + `cap_by_ratio` |
| I6 | 归档队列不持有 event/消息链对象 | 内存泄漏、平台连接被拖住 | code review |
| I7 | 任何失败路径都不得阻断消息 | 用户发消息没人回 | 异常兜底 + §3.2 |
| I8 | 一切召回按 `session_id` 隔离 | 跨会话泄露 | T06/T12 + SQL 检查 |
| I9 | `config.py` 与 `_conf_schema.json` 默认值逐项一致 | 用户看到的 ≠ 生效的 | `test_config_schema_parity.py` |
| I10 | 卸载时 flush ≤ 5s，超时可取消 | AstrBot 卸载卡住 | T08 + `terminate` |

---

## 6. 数据库

DDL 在 `ctm/sql/001_init.sql`，与 v0.2 §4 的差异只有 **D1**：

* 新增 `session_state(session_id, turns_since_switch, last_switch_at,
  soft_boundary_pending, updated_at)`。v0.2 的状态机字段需要逐轮持久化，
  §4 的 DDL 没有落这三项；不落地则重启后 `min_turns` / `cooldown` 全部失真。

另加两个索引：`idx_memories_session_type_recalled`（§9 的候选过滤）与
`idx_turn_logs_turn_id`（`/ctm replay` 按 turn_id 查）。

| 表 | 写入者 | 读频率 | 保留策略 |
| --- | --- | --- | --- |
| sessions / session_state | on_message（每轮） | 每轮 | 与会话同生命周期 |
| topics / topic_stack | TopicStateMachine | 每轮 | 永久（摘要可归档） |
| memories | Archiver | 每轮 | 单会话 1 万条以内建议 |
| relationship_state | Archiver | 每轮 | 永久 |
| turn_logs | Archiver（晚一拍） | 命令/排障 | 30 天（`purge_turn_logs`） |
| archive_failures | Archiver | 排障 | 永久（人工清理） |

写入模型：**单连接 + 单写 worker**。WAL 允许多读一写；多写者会互相
`database is locked`，所以不做连接池。运维见 §14。

---

## 7. 配置

分组见 `_conf_schema.json`：`basic / topic / memory / relationship / adapter /
injection / observability`。三条规则：

1. **默认值双写**：`config.py` 与 `_conf_schema.json` 各写一次，测试断言一致（I9）；
2. **非法值回退默认并记 WARN**，不让插件启动失败；
3. **热更新白名单**：`topic.threshold / min_turns / cooldown / limits / weights /
   include_meta / log_*` 可热改；`enabled / db_path / adapter.backend`
   改变资源生命周期，必须重载才生效（与 msg_aggregator 的结论一致）。

`db_path` 默认 `ctm.sqlite3`，相对路径相对于**插件数据目录**
（`data/plugin_data/astrbot_plugin_ctm/`），而不是 AstrBot 的 `data/` 根。
理由：备份、卸载、迁移都按插件边界走，避免多个插件往同一个目录塞库。

---

## 8. 失败码矩阵（`ctm/observability.py`）

| 码 | 触发 | 行为 | 用户可见 |
| --- | --- | --- | --- |
| `INFO_DECISION` | 每轮决策完成 | 记 topic_action / persona / tokens | 日志（`log_decisions`） |
| `INFO_DROPPED` | 候选被预算/上限丢弃 | 记 memory_id 与原因 | 日志（`log_dropped_items`） |
| `WARN_NO_DECISION` | 请求前无决策 | 跳过注入 | 日志 |
| `ERROR_INJECTION` | 渲染/追加异常 | 最小关系块兜底 | 日志 |
| `WARN_BUDGET_OVERFLOW` | 固定块超预算 | 砍低优先级固定块 | 日志 |
| `WARN_PROMPT_GROWTH_CAPPED` | 超过 25% 比例 | 按行截断 | 日志 |
| `WARN_DUPLICATE_INJECTION` | 检测到同类插件自动注入 | 只告警，不改对方配置 | 日志 + `/ctm health` |
| `WARN_ADAPTER_FALLBACK` | 外部后端不可用 | 回退 SQLite | 日志 + `/ctm health` |
| `ERROR_STORE` | SQL 失败 | 空结果降级 | 日志 + `/ctm health` |
| `ERROR_ARCHIVE` | 归档最终失败 | 落 `archive_failures` | `/ctm health` 计数 |
| `WARN_ARCHIVE_DROPPED` | 队列满 | 丢弃该轮归档 | 日志 |

---

## 9. 性能预算与测量点

| 项 | 目标 | 测量位置 | 约束 |
| --- | --- | --- | --- |
| Perception P99 | < 5ms | `on_message` 前后 | 只做词表/正则，禁止 IO 与 LLM |
| 决策 P99 | < 20ms | `Cognition.decide` 前后 | 读库只走索引；候选有上限 |
| 注入构建 P99 | < 10ms | `build` 前后 | 纯字符串拼接 |
| 归档延迟 | < 2s | 入队到落库 | 单 worker，批内串行 |
| 单会话记忆量 | ≤ 1 万条 | `/ctm health` | 超限提示清理 |
| 单会话库大小 | < 50MB | `/ctm health` | 定期 VACUUM |

测量方式：`Diag` 同步计数 + 每次决策耗时写 DEBUG 日志；不引入 metrics 依赖。

---

## 10. 里程碑与验收

| 里程碑 | 内容 | 交付物 | 验收 |
| --- | --- | --- | --- |
| **M0（本次）** | 骨架：文件、签名、DDL、schema、设计文档 | 本目录全部文件可编译；schema 合法 | `compileall` 通过；文档评审 |
| M1（v0.1） | SQLite 话题栈 + 软边界 + 统一注入 + 决策日志 | `store.init/migrate`、Perception、TopicStateMachine、Cognition、InjectionBuilder 基础版、`/ctm status|topics|health` | T02、T03、T09 通过 |
| M2（v0.2） | 断言/情节抽取 + 关系状态 + 预算装箱 | MemoryStore 写入路径、RelationshipManager、pack/plan | T01、T05、T07、T10 通过 |
| M3（v0.3） | 适配器 + 管理命令全量 | adapters 工厂、memory 子命令、export | T06、T11 通过 |
| M4（v0.4） | 人格动态 + 承诺追踪 + 手动修正降权 | persona 映射、pending_promises、corrected_count | 人工对比 50 轮连贯性 |
| M5（v0.5+） | 面板 / 回放 / 阈值调优 / 向量后端 | `/ctm replay`、`ctm_diag`、可选 sqlite-vec | T04、T08、T12 通过 |

每个里程碑完成后跑一遍全量用例 + 一次 50 轮多话题回归（软切 vs 硬切对比）。

---

## 11. 测试矩阵映射

| 编号 | 场景 | 用例文件（待实现） | 断言要点 |
| --- | --- | --- | --- |
| T01 | 粘性关键词命中 | `tests/test_t01_t03_switch.py` | 不切；`reason` 含 sticky |
| T02 | 连续 2 轮内新话题 | 同上 | `min_turns` 生效 |
| T03 | 冷却期内新话题 | 同上 | `cooldown` 生效 |
| T04 | 软切后回溯旧话题 | `tests/test_t04_backtrack.py` | 弹栈 + 提示注入一次 |
| T05 | 预算不足 | `tests/test_t05_budget.py` | 按优先级截断；dropped 记录 |
| T06 | 记忆插件不可用 | `tests/test_t06_adapter_fallback.py` | 回退 SQLite；不抛 |
| T07 | 注入异常 | `tests/test_t07_injection_fallback.py` | 最小关系块兜底 |
| T08 | 归档失败 | `tests/test_t08_archive.py` | 重试后落 failures；下一轮正常 |
| T09 | 硬切命令 | `tests/test_t09_hard_switch.py` | 清栈；不进 on_llm_request |
| T10 | 重复注入检测 | `tests/test_t10_duplicate.py` | 告警一次；不重复注入 |
| T11 | 记忆手动修正 | `tests/test_t11_correction.py` | `corrected_count+1`；降权 |
| T12 | 导出/导入 | `tests/test_t12_export.py` | 数据完整；ID 映射正确 |
| — | 配置双写一致 | `tests/test_config_schema_parity.py` | 逐项相等 |
| — | 模板占位一致 | `tests/test_injection_template.py` | 占位符 ↔ 字段一一对应 |

---

## 12. 已知缺口与待定问题

### 12.1 已决策

| 编号 | 问题 | 决策 |
| --- | --- | --- |
| D1 | 会话级计数无表 | 新增 `session_state`（§6） |
| GAP-3 | `turns_since_switch` 语义 | **切换当轮归零，之后每轮 +1**；`should_soft_boundary` 要求 `>= min_turns_before_switch`，即切换后至少再过 3 轮才允许再次切换 |
| GAP-6 | 人格提示与原人设冲突 | 注入文本只能是"建议式约束"，不得覆盖原始 persona；模板已按此写 |

### 12.2 待定（实现前必须落一项）

| 编号 | 问题 | 选项 | 建议 |
| --- | --- | --- | --- |
| GAP-2 | 回溯旧话题（弹栈）的触发器未定义 | A. 相似度指向更早话题即弹栈；B. 显式"我们接着说 X"规则；C. A+B | 先 A（B 作为 A 的加权），`should_pop_back` 已留签名 |
| GAP-4 | 聚合层是内建还是复用 msg_aggregator | A. 内建（文档原意）；B. 依赖外部插件 | A，但 M1 前先确认群聊可达性；v0.1 逐条处理 |
| GAP-5 | 关系粒度：群聊 umo 是"群"不是"人" | A. 按 (umo, sender)；B. 按 umo | A；否则群友共享一个 intimacy，表现会失真 |
| GAP-7 | 摘要/断言是否调模型 | A. 纯规则；B. 轻量模型；C. 规则优先 + 模型兜底 | C；`Archiver(summarizer=...)` 已留注入点 |
| Q1 | Embedding Provider 配置放哪 | 独立分组 / 复用 AstrBot provider | v1.0 再定，`SimilarityScorer` 已隔离 |
| Q2 | `max_prompt_growth_ratio` 按字符还是 Token | 字符近似 / tokenizer | v0.1 用字符近似，接 tokenizer 后切换 |

---

## 13. 从话题路由器Pro迁移的 patch 点

| # | v0.2 的迁移点 | 落到本骨架 |
| --- | --- | --- |
| 1 | 新增 `soft` 分支，`auto` 不再直接 `/new` | `TopicStateMachine.soft_boundary` + `auto_switch_strategy` |
| 2 | 注入权收口：原 hint 逻辑迁移到 `InjectionBuilder` | `injection.build()` 是唯一渲染点；原插件只产出决策 |
| 3 | 消息聚合保留 priority=1000 | `main.MESSAGE_PRIORITY`；与 msg_aggregator 不同装 |
| 4 | 硬切保留为配置项，默认关闭 | `switch_hard()` + `auto_switch_strategy=hard`（默认 soft） |
| 5 | 关闭重复压缩 | 若接外部记忆后端，README 注明关闭内置压缩 |
| 6 | 观测一周再调阈值 | `turn_logs` + `/ctm replay` + 每日 `ctm_diag` |

回退开关：`auto_switch_strategy=hard` 恢复旧行为；`enabled=false` 完全停用；
数据可 `export`，不锁定格式。

---

## 14. 运维

| 操作 | 做法 |
| --- | --- |
| 备份 | 停写后 `PRAGMA wal_checkpoint(TRUNCATE)`，拷 `ctm.sqlite3`（含 `-wal` 更稳） |
| 清理 | `turn_logs` 保留 30 天；`archive_failures` 人工清理 |
| 体检 | `/ctm health`：DB 大小、表数、适配器、队列长度 |
| 诊断 | `ctm_diag` 输出：触发率、误判率、平均注入 Token、召回命中率 |
| 迁移 | `/ctm export` 导出 JSON；导入工具 v1.0 提供 |

隐私（§10 文档）：默认本地库；`log_redact=true`；导出/删除必须可达；
多用户严格按 `session_id` 隔离（GAP-5 的按人粒度是**收紧**，不是放宽）。

---

## 15. 结论

骨架版把"可落地"拆成三件可检查的事：**接口是否闭合**（§4 的每个方法都有归属）、
**失败是否可见**（§8 的每个码都有行为）、**状态是否只有一个写入者**
（§5 的 I1/I3/I6）。这三件事成立之后，实现阶段剩下的只是把 `NotImplementedError`
逐条替换成算法，而不是边写边改结构。
