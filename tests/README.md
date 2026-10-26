# CTM 测试计划

> 当前处于**骨架阶段**：本目录只有计划，没有用例。所有 `raise
> NotImplementedError` 的方法都应"实现一条、补一条用例"，不允许实现完再补。

## 怎么跑

```bash
# 方案 A：pytest（推荐；开发环境）
uv run --with pytest pytest tests/ -q

# 方案 B：零依赖（只跑标准库断言，供 CI 空环境用）
python -m unittest discover -s tests -p "test_*.py"
```

测试**不加载 AstrBot**：`ctm/*` 不 import astrbot，事件对象用最小替身即可。
唯一需要 AstrBot 的是 `main.py` 的 hook 注册，这部分用 1 个 smoke 用例
（导入模块、断言 4 个 handler 存在）覆盖，不启动真实实例。

## 用例矩阵

| 编号 | 文件 | 覆盖 | 关键断言 |
| --- | --- | --- | --- |
| T01 | `test_t01_t03_switch.py` | 粘性命中 | 不触发软切；`reason` 含 `sticky` |
| T02 | 同上 | `min_turns_before_switch` | 第 2 轮新话题不切 |
| T03 | 同上 | `switch_cooldown_seconds` | 冷却期内不切 |
| T04 | `test_t04_backtrack.py` | 软切后回溯 | 弹栈；边界提示只注入一次 |
| T05 | `test_t05_budget.py` | 预算不足 | 按 PRIORITY_ORDER 截断；dropped 完整 |
| T06 | `test_t06_adapter_fallback.py` | 外部适配器缺失 | 回退 SQLite；不抛异常 |
| T07 | `test_t07_injection_fallback.py` | 渲染异常 | 最小关系块兜底；日志码正确 |
| T08 | `test_t08_archive.py` | 归档失败 | 重试 2 次后落 `archive_failures` |
| T09 | `test_t09_hard_switch.py` | 显式硬切 | 清栈；旧话题 archived |
| T10 | `test_t10_duplicate.py` | 重复注入检测 | 只告警一次；不产生第二段注入 |
| T11 | `test_t11_correction.py` | 手动修正记忆 | `corrected_count+1`；重要度下降 |
| T12 | `test_t12_export.py` | 导出/导入 | 字段完整；ID 映射可逆 |
| — | `test_config_schema_parity.py` | 配置双写 | `config.py` 默认值 == schema 默认值 |
| — | `test_injection_template.py` | 模板一致性 | `TEMPLATE` 占位符 ↔ `build()` 字段 |

## 测试替身约定

| 替身 | 代替 | 说明 |
| --- | --- | --- |
| `FakeStore` | `Store` | 内存 dict 表；只实现被测方法；记录调用顺序 |
| `FakeEvent` | `AstrMessageEvent` | 提供 `unified_msg_origin / message_str / message_obj / get_sender_id / set_extra / get_extra` |
| `FakeProviderRequest` | `ProviderRequest` | 只有 `system_prompt` 字段 |
| `FakeClock` | 时间 | 注入 `now` 参数，禁止测试里 `sleep` |

## 回归基线

M2 起维护 `tests/fixtures/dialogue_50turns.jsonl`：同一段 50 轮多话题对话，
分别用 `soft` / `hard` 跑一遍，人工打 1-5 分连贯性，并记录每轮注入 Token。
基线不达标（软切不低于硬切）就不进 M4。

## 不做什么

- 不测 NotImplementedError 本身（骨架阶段跑测试必然红，属预期）；
- 不在单测里连真实平台、真实模型或网络；
- 不为了覆盖率去测 getter/setter。
