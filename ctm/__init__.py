"""CTM 核心包：所有认知逻辑都在这里，`main.py` 只做接线。

分层与依赖方向（只允许向下依赖，禁止循环）：

```
main.py (接线 / 生命周期 / AstrBot hook)
  └── ctm.cognition       联合决策
        ├── ctm.topic_state   话题状态机（软边界 / 弹栈 / 硬切）
        ├── ctm.memory_store  记忆召回与打分
        ├── ctm.relationship  关系状态
        ├── ctm.injection     预算装箱与 Prompt 渲染
        └── ctm.perception    消息感知（不调 LLM）
  └── ctm.archiver        异步归档
        ├── ctm.store         唯一 SQLite 访问点
        └── ctm.adapters      外部记忆后端适配
```

* `ctm.store` 不 import 上层模块，只认 SQL 与 dict 行，避免"实体类 ↔ 持久层"循环；
* `ctm` 包不 import `astrbot`，可脱离 AstrBot 单测（事件对象按 duck typing 处理）；
* 唯一的 AstrBot 依赖点在 `main.py`；`ctm.adapters.living_memory / memory_companion`
  只接收 `Context` 参数、不 import astrbot（v0.3 实现时也要守住）。

详细边界、时序与不变量见 `docs/DESIGN.md`。
"""
