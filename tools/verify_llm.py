"""LLM 智能整理链路验证：读取已转写 transcript.json → 分块 → 真实 Map-Reduce → 输出 Markdown。

用法：
    .\\.venv\\Scripts\\python.exe tools\\verify_llm.py [transcript.json] [--out result.md]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import chunker, summarizer  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import TranscriptSegment, VideoMeta  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 智能整理链路验证")
    ap.add_argument("transcript", nargs="?", default=r"data\024add1de5a3\transcript.json")
    ap.add_argument("--out", default="", help="输出 Markdown 路径（默认仅打印）")
    ap.add_argument("--title", default="【官方 MV】Never Gonna Give You Up - Rick Astley")
    ap.add_argument("--uploader", default="索尼音乐中国")
    ap.add_argument("--duration", type=int, default=212)
    ap.add_argument("--depth", choices=["essence", "detailed", "full"], default="essence")
    args = ap.parse_args()

    print(f"[配置] llm_configured={settings.llm_configured} base_url={settings.llm_base_url} model={settings.llm_model}")

    data = json.loads(Path(args.transcript).read_text(encoding="utf-8"))
    segments = [TranscriptSegment(**s) for s in data.get("segments", [])]
    if not segments:
        print("转写文件无 segments")
        return 1
    full_text = "\n".join(s.text for s in segments)

    chunks = chunker.chunk_segments(segments)
    print(f"[分块] {len(chunks)} 块（共 {len(full_text)} 字）")

    meta = VideoMeta(
        url="https://www.bilibili.com/video/BV1GJ411x7h7",
        title=args.title,
        uploader=args.uploader,
        duration=args.duration,
        webpage_url="https://www.bilibili.com/video/BV1GJ411x7h7",
        id="BV1GJ411x7h7",
    )

    print("[整理] 调用 LLM Map-Reduce 中…")
    summary = summarizer.summarize_chunks(
        chunks,
        meta,
        "音乐爱好者",
        "快速了解歌曲内容",
        progress_cb=lambda p: None,
        segments=segments,  # 触发真实时间戳校准
        depth=args.depth,
    )
    md = summarizer.render_markdown(meta, summary, full_text)

    print()
    print(md)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"\n[已保存] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())