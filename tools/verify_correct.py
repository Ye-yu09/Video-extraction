"""转写纠错验证：读取已有 transcript.json → LLM 校对 → 打印修改对照 → 保存。

用法：
    .\\.venv\\Scripts\\python.exe tools\\verify_correct.py [transcript.json] [--out corrected.json]
    不带参数时自动使用 data 下最新任务的 transcript.json。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.corrector import correct_texts  # noqa: E402


def _latest_transcript() -> Path:
    dirs = sorted(
        (settings.data_dir).glob("*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for d in dirs:
        if d.is_dir():
            t = d / "transcript.json"
            if t.exists():
                return t
    raise SystemExit("未找到 transcript.json")


def main() -> int:
    ap = argparse.ArgumentParser(description="转写纠错验证")
    ap.add_argument("transcript", nargs="?", default="")
    ap.add_argument("--out", default="", help="输出修正后 JSON 路径")
    args = ap.parse_args()

    src = Path(args.transcript) if args.transcript else _latest_transcript()
    data = json.loads(src.read_text(encoding="utf-8"))
    texts = [s["text"] for s in data["segments"]]
    print(f"[输入] {src} 共 {len(texts)} 段 | llm_configured={settings.llm_configured}")

    result = correct_texts(texts, progress_cb=lambda p: print(f"  纠错进度 {p}%", end="\r"))
    print(f"\n[结果] 修改 {result.changed} 段，批次 {result.batches}")

    shown = 0
    for old, new in zip(texts, result.texts):
        if old != new and shown < 15:
            print(f"  [{shown + 1}] 改: {old}")
            print(f"      →: {new}")
            shown += 1

    if args.out:
        data["segments"] = [{**s, "text": t} for s, t in zip(data["segments"], result.texts)]
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[已保存] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())