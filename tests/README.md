# CTM 测试

```bash
python -m unittest discover -s tests -p "test_*.py"
```

- `test_ctm.py`：纯标准库单测（配置一致性、相似度、SQLite 存储、话题软边界、
  记忆召回与装箱、归档重试、长期记忆抽取/召回/删除）。
- `test_import.py`：AstrBot 接线层导入 smoke；未安装 AstrBot 时自动跳过。

测试不连接真实平台、真实模型或网络；SQLite 一律使用临时目录。
