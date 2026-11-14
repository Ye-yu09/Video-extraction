"""生成转写纠错对照报告（UTF-8）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RAW = Path(r"data\2a9c4eb4aa89\transcript.json")  # 纠错前（旧任务原始转写）
NEW = Path(r"data\corrected_test.json")  # 纠错后

raw = json.loads(RAW.read_text(encoding="utf-8"))
new = json.loads(NEW.read_text(encoding="utf-8"))

lines = []
changed = 0
for a, b in zip(raw["segments"], new["segments"]):
    if a["text"] != b["text"]:
        changed += 1
        lines.append(f"[{a['start']:7.2f}] {a['text']}")
        lines.append(f"        → {b['text']}")

report = "\n".join(lines)
out = Path(r"data\correct_report.txt")
out.write_text(f"修改段数: {changed} / {len(raw['segments'])}\n\n{report}", encoding="utf-8")
print(f"报告已写入 {out}（修改 {changed} 段）")