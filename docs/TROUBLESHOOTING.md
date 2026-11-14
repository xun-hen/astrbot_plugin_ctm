# 排障手册（常见坑与解决办法）

> 本文按"**踩坑概率**"从高到低排列。装完插件第一件事：发一条 `/ctm doctor`，
> 六项全 `ok` 才算真正生效。也可以在插件详情页看顶部**自检红条**。

---

## 0. 先学会读自检

```
/ctm doctor
```

输出每行以标记开头：

| 标记 | 含义 | 要做什么 |
| --- | --- | --- |
| `[OK]` | 通过 | 不用管 |
| `[--]` | 提示/无数据 | 通常正常（如刚启动还没人说话） |
| `[!!]` | 警告 | 能工作但有隐患，建议按 `→` 处理 |
| `[XX]` | **阻断** | 这就是"插件不工作"的原因，按 `→` 处理 |

六项：**消息入口**（心跳）、**全局白名单**、**handler 绑定**、**数据库**、**消息聚合**、**版本**；
在会话里跑还会多一项**本会话**。

---

## 坑 1（最常见）：新装插件不在 `plugin_set` 白名单

**现象**：插件"已启用"、启动日志正常、配置也对，但发消息**没有任何反应**：
用户照常收到回复，插件的 `sessions`/`turn_logs` 全是 0 行，插件日志也是 0 行。
状态页能打开（页面后端在加载时就注册了），但"入口消息"一直是 0。

**原因**：AstrBot 有一个**全局插件白名单** `plugin_set`
（`["*"]` = 全部启用；空列表 = 全不启用；也可以是显式名单）。
不在名单里的插件，handler 会在 `waking_check` 阶段被**直接丢弃**，只有 DEBUG 日志。
很多人的 `plugin_set` 是历史上手工维护过的显式列表，**新插件不会自动加入**。

**判定**：

- `/ctm doctor` → `[XX] 全局白名单未包含本插件`；
- 或启动日志出现 `WARN_PLUGIN_SET`；
- 或自检里 `消息入口` 显示"LLM 请求钩子 N 次、入口 0 次"。

**解决**（任选其一）：

1. WebUI → **配置** → **插件** → 找到 `plugin_set`，把 `astrbot_plugin_ctm` 加进列表并保存；
2. 或把 `plugin_set` 直接设为 `["*"]`（表示启用所有插件）；
3. 保存后无需重启，下一条消息就生效（配置是每次事件读取的）。

**验证**：再发一条消息，`/ctm doctor` 的"消息入口"应变成 `[OK] 已收到 N 条…`。

> 提示：这是 AstrBot 的设计，插件无法自行把自己加进白名单；v0.4.2 起会在启动时
> WARN 并写进自检，至少不会"无声"。

---

## 坑 2：与同优先级插件抢事件

**现象**：装了 `astrbot_plugin_msg_aggregator` 或 `astrbot_plugin_topic_router_pro`，
出现消息被合并两次、判定重复、或 CTM 有时收不到消息；日志里有
`WARN_PREEMPTED`（CTM 的兜底注入）或 `aggregate.preempted`（那是别的插件的）。

**原因**：三者都用 `priority=1000` 抢消息入口；框架分发是"按 priority 降序、
同级按注册顺序"，**前一个插件一旦 `stop_event()`，后面的 handler 直接不执行**。

**解决**：

1. **只留一个**做消息入口（推荐 CTM，或 PRO，不要同装）；
2. 如果一定要共存，把 CTM 的 `topic.mode` 设为 `hint` 并接受"被抢占时靠
   `on_llm_request` 兜底"（v0.4.1 起兜底不再依赖话题栈，被抢占也能注入）；
3. 用 `/ctm doctor` 看"消息入口"是否长期为 0 次——是的话基本就是被抢占了。

**验证**：卸载另一个插件后重载，`/ctm doctor` 全绿。

---

## 坑 3：本会话被关闭（会话级）

**现象**：别的群/私聊正常，只有某个会话里 CTM 完全不工作。

**原因**：该会话执行过 `/ctm topic off`，或面板"会话规则"里禁用了本插件；
`/ctm topic off` 只影响当前会话，重启进程才清空。

**判定**：在该会话发 `/ctm doctor` → 最后一项 `[!!] 本会话已关闭话题功能`。

**解决**：在该会话发 `/ctm topic on`。

---

## 坑 4：总开关 / 模式被关掉

**现象**：完全不动，但日志里有 `WARN_PLUGIN_SET` 之外的一条启动 WARN。

**原因**：`basic.enabled=false`（总开关）或 `topic.mode=off`。

**判定**：

- 启动日志：`配置 basic.enabled=false，插件不工作`；
- `/ctm doctor`：入口为 0 且白名单/绑定都 OK；
- 日志 DEBUG 级有 `DEBUG_SKIP reason=disabled` / `reason=mode_off`。

**解决**：面板 → 插件配置里打开"启用认知中枢"，或把运行模式改成 `hint`。

---

## 坑 5：数据库不可写 / 放在网络盘

**现象**：`/ctm doctor` → `[XX] 数据库不可用`；或时好时坏、报
`database is locked` / `disk I/O error`。

**原因**：数据目录不可写；或 SQLite（WAL 模式）放在 SMB/NFS 等网络盘上。

**解决**：

1. 确认 `data/plugin_data/astrbot_plugin_ctm/` 可写（容器里注意 volume 权限）；
2. **把 `basic.db_path` 指到本地磁盘**（相对路径基于插件数据目录，也可以写绝对路径）；
3. 迁移时停插件 → 拷贝 `ctm.sqlite3`（连同 `-wal`/`-shm`）→ 再启动。

---

## 坑 6：AstrBot 版本过低 / 页面打不开

**现象**：`/ctm doctor` → `[!!] AstrBot 版本偏低`；或插件详情页空白、提示页面后端未注册。

**原因**：要求 `AstrBot >= 4.26`；更老版本没有 `astrbot.api.web`，页面后端会整体降级。

**解决**：升级 AstrBot。页面不可用时，**所有能力仍然可用**（`/ctm doctor`、`/ctm status`
都是命令，不依赖页面）。

---

## 坑 7：看不到细节日志（不是故障）

**现象**：想确认"消息卡在哪一步"，但日志里只有 `INFO_ENTRY`，没有 `DEBUG_SKIP`。

**原因**：`DEBUG_SKIP` 是 DEBUG 级别，默认日志级别看不到。

**解决**：面板 → 设置 → 日志级别调到 `DEBUG`（或给本插件单独设
`PUT /api/v1/plugins/{id}/log-level`），然后按下面关键词搜：

| 关键词 | 说明 |
| --- | --- |
| `INFO_ENTRY` | 每条消息的入口（含 `text_len` / `mode`） |
| `DEBUG_SKIP reason=...` | 静默返回的原因码（见下表） |
| `INFO_DECISION` | 本轮决策（动作/人格/tokens） |
| `WARN_NO_DECISION` | 到请求阶段仍没有决策，带 `reason` |
| `WARN_PREEMPTED` | 入口被抢占，走了兜底注入 |
| `WARN_PLUGIN_SET` | 不在全局白名单（坑 1） |
| `WARN_HANDLER_BINDING` | handler 绑定异常（坑 8） |
| `INFO_DECISION` 之后没有归档 | 看 `ERROR_ARCHIVE` / `WARN_ARCHIVE_DROPPED` |

`DEBUG_SKIP` 的 `reason` 取值：

| reason | 含义 | 处理 |
| --- | --- | --- |
| `disabled` | `basic.enabled=false` | 坑 4 |
| `mode_off` | `topic.mode=off` | 坑 4 |
| `session_off` | 本会话 `/ctm topic off` | 坑 3 |
| `command` | 是指令消息（不参与话题判定） | 正常 |
| `empty_text` | 事件正文为空（纯图片/表情/语音） | 正常；见下 |
| `aggregating_follower` | 消息被合并进上一轮 | 正常（聚合生效） |
| `empty_after_aggregate` | 合并后正文为空 | 正常 |
| `inject_off` | `mark` 模式或 debug 空跑，不注入 | 正常 |
| `empty_injection` | 决策渲染后内容为空（预算/裁剪所致） | 见坑 10 |
| `llm_request_disabled` | 请求阶段发现总开关关闭 | 坑 4 |

> 正文为空的纯媒体消息（图片/表情/语音，且未开图片转述）不参与话题判定，
> 记为 `empty_text`，这是预期行为。

---

## 坑 8：历史版本 v0.4.0 的"完全静默"

**现象**：插件日志 0 行、库 0 行，但框架日志每条消息有一行英文 ERROR：
`Plugin Handler arguments do not match its definition.`

**原因**：v0.4.0 把 handler 写成了模块级函数；AstrBot 会把插件实例 partial 绑到
handler 的第一个位置参数上，于是派发变成 `f(实例, event)` → `TypeError`，
被 `call_handler` 吞掉后照常走 LLM。

**解决**：升级到 **v0.4.2 或更新**。升级后 `/ctm doctor` 的"handler 绑定"应为
`[OK] 5 个 handler 均已注册并绑定本实例`。若仍是 `unbound/missing`，重载插件或重启进程。

---

## 坑 9：热重载残留

**现象**：改完代码/配置后行为还是旧的；或日志出现重复的 handler、重复注入。

**原因**：AstrBot 热重载可能残留旧 handler（历史上有过 handler 重复注册的问题）。

**解决**：**重启 AstrBot 进程**，而不是只点"重载插件"。重启后 `/ctm doctor` 确认
`entries` 从 0 重新计数（说明是新实例）。

> **症状：点"重载"一直转圈，之后连"禁用/更新"也全部超时。**
> 这是卸载（`terminate`）被 worker 拖死，把 AstrBot 的插件管理锁一起占住了，
> 只能重启进程。v0.5.1 已把 `terminate` 每步限时（归档 6s、长期记忆 3s），
> 正常情况下不会再发生；如果从旧版本升级时正好卡住，重启一次即可。
> 排查手段：`curl /api/v1/plugins/{id}/reload` 若 HTTP 000 超时、而
> `/api/v1/plugins` 仍返回 200，就说明锁被占住、进程本身还活着。

---

## 坑 10："判定/注入好像没生效"

先分清是不是**预期行为**：

| 配置 | 效果 |
| --- | --- |
| `topic.mode=mark` | 只记录决策，**不注入** Prompt（这是设计） |
| `topic.debug_mode=true` | 空跑：判定照常但不注入、不切对话、不落话题状态 |
| `memory.injection_budget_tokens=0` 或太小 | 装箱后没有条目可注入 |
| `injection.max_prompt_growth_ratio` 太小 | 注入被裁剪（日志 `WARN_PROMPT_GROWTH_CAPPED`，带裁剪前后字符数） |
| 长期记忆 `items=0` | 还没抽取出内容（需要积累轮次，`longterm.extract.every_n_turns`） |

真正的异常看这几条日志：`ERROR_INJECTION`（渲染失败）、`WARN_NO_DECISION`（到请求阶段
没有决策）、`WARN_BUDGET_OVERFLOW`（预算装不下任何一条，`dropped>0` 且 `kept=0`）、
`WARN_PROMPT_GROWTH_CAPPED`（被增幅上限裁剪）。

---

## 一页速查

| 现象 | 最可能原因 | 一条命令 |
| --- | --- | --- |
| 完全没反应、库 0 行 | 坑 1 白名单 | `/ctm doctor` |
| 框架日志 `arguments do not match` | v0.4.0 旧版 | 升级到 v0.4.2+ |
| 只有一个会话不工作 | 坑 3 会话关闭 | `/ctm topic on` |
| 消息合并两次/判定重复 | 坑 2 同装抢事件 | 卸载另一个 |
| 页面打不开 | 坑 6 版本低/无 api.web | 看命令是否正常 |
| 时好时坏、locked | 坑 5 网络盘/权限 | 换本地盘 |

需要反馈问题时，请附上：`/ctm doctor` 全文 + 插件信息（版本/AstrBot 版本/平台）
+ 相关日志（`grep '[astrbot_plugin_ctm]'`）。
