"""离线评测：用真实聊天记录跑启发式话题判定（零网络、零 AstrBot 依赖）。

目标不是"绝对准确率"，而是**同一口径下的前后对比**：改阈值/改规则后，
看混淆矩阵与模型调用占比怎么变。

```bash
python tests/eval_topic_detection.py --db ~/.astrbot/data/data_v4.db
python tests/eval_topic_detection.py --gap-minutes 30 --json-out before.json
```

真值口径（与话题路由器 PRO 一致）：真实数据没有人工标注的话题边界，默认用
**时间间隔代理** —— 相邻两条用户消息间隔超过 `--gap-minutes` 记为新话题。
代理本身有噪声，绝对值只作参考。
"""

from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_ctm.ctm.topic_judge import (  # noqa: E402
    VERDICT_NEW_TOPIC,
    heuristic,
)


class _Node:
    def __init__(self, title: str, last_active_at: float) -> None:
        self.title = title
        self.summary = ""
        self.last_active_at = last_active_at


class _State:
    def __init__(self, title: str, last_active_at: float) -> None:
        self.current_topic = title
        self._node = _Node(title, last_active_at)

    def focus(self) -> _Node:
        return self._node


def extract_text(content: Any) -> str:
    """从消息链 JSON 中提取纯文本。"""
    if isinstance(content, str):
        text = content.strip()
        if text.startswith("["):
            try:
                return extract_text(json.loads(text))
            except ValueError:
                pass
        return text
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for component in content:
        if not isinstance(component, dict):
            continue
        if component.get("type") in ("plain", "text"):
            data = component.get("data") or {}
            text = data.get("text") if isinstance(data, dict) else None
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts).strip()


def load_turns(db_path: Path, limit: int) -> list[tuple[str, float, str]]:
    """读取按 (平台, 会话, 发送者) 分组的时间有序消息。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='platform_message_history'"
        ).fetchone()
        if not exists:
            raise SystemExit("数据库里没有 platform_message_history 表，无法评测。")
        rows = conn.execute(
            """
            SELECT platform_id, user_id, sender_id, content, created_at
            FROM platform_message_history
            ORDER BY platform_id, user_id, sender_id, id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    turns: list[tuple[str, float, str]] = []
    for platform_id, user_id, sender_id, content, created_at in rows:
        text = extract_text(content)
        if not text:
            continue
        key = f"{platform_id}:{user_id}:{sender_id or ''}"
        timestamp = _to_ts(created_at)
        turns.append((key, timestamp, text))
    return turns


def _to_ts(value: Any) -> float:
    """把 SQLite 里的时间值转成 Unix 秒。"""
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def evaluate(
    turns: list[tuple[str, float, str]],
    *,
    gap_minutes: float,
    sim_continue: float,
    sim_new_topic: float,
    min_message_length: int,
    short_message_length: int,
) -> dict[str, Any]:
    """按会话逐条跑启发式，返回混淆矩阵与指标。"""
    tp = fp = tn = fn = 0
    llm = 0
    total = 0
    previous: dict[str, tuple[str, float]] = {}
    samples: list[dict[str, Any]] = []

    for key, timestamp, text in turns:
        prev = previous.get(key)
        previous[key] = (text, timestamp)
        if prev is None:
            continue
        prev_text, prev_ts = prev
        if timestamp <= 0 or prev_ts <= 0:
            continue
        label_new = (timestamp - prev_ts) / 60.0 >= gap_minutes
        state = _State(prev_text[:16], prev_ts)
        result = heuristic(
            text,
            state,
            now=timestamp,
            sim_continue=sim_continue,
            sim_new_topic=sim_new_topic,
            time_gap_minutes=int(gap_minutes),
            min_message_length=min_message_length,
            short_message_length=short_message_length,
        )
        predicted_new = result.verdict == VERDICT_NEW_TOPIC
        total += 1
        llm += int(result.needs_llm)
        if predicted_new and label_new:
            tp += 1
        elif predicted_new and not label_new:
            fp += 1
        elif not predicted_new and not label_new:
            tn += 1
        else:
            fn += 1
            if len(samples) < 20:
                samples.append(
                    {
                        "key": key,
                        "text": text[:120],
                        "similarity": round(result.similarity, 3),
                        "gap_minutes": round((timestamp - prev_ts) / 60, 1),
                        "reason": result.reason,
                    }
                )

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "gap_minutes": gap_minutes,
        "turns": total,
        "matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "llm_ratio": round(llm / total, 4) if total else 0.0,
        "false_negatives_sample": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="CTM 启发式话题判定评测")
    parser.add_argument("--db", default=str(Path.home() / ".astrbot" / "data" / "data_v4.db"))
    parser.add_argument("--gap-minutes", type=float, default=45.0)
    parser.add_argument("--sim-continue", type=float, default=0.5)
    parser.add_argument("--sim-new-topic", type=float, default=0.12)
    parser.add_argument("--min-message-length", type=int, default=6)
    parser.add_argument("--short-message-length", type=int, default=25)
    parser.add_argument("--limit", type=int, default=20000)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        raise SystemExit(f"数据库不存在：{db_path}")
    turns = load_turns(db_path, args.limit)
    if not turns:
        raise SystemExit("没有可评测的消息（platform_message_history 为空）。")

    report = evaluate(
        turns,
        gap_minutes=args.gap_minutes,
        sim_continue=args.sim_continue,
        sim_new_topic=args.sim_new_topic,
        min_message_length=args.min_message_length,
        short_message_length=args.short_message_length,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已写入 {args.json_out}")


if __name__ == "__main__":
    main()
