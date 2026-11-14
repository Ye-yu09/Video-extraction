"""命令行版：单次运行，跑通「下载 → 转写 → 整理」并输出 Markdown。

用法：
    python -m app.cli <B站链接> [--out result.md]
    python -m app.cli https://www.bilibili.com/video/BVxxxxxx --depth detailed --out 精华.md
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import settings
from .models import ExtractRequest, TaskStatus
from .pipeline import create_task, get_task, run_task


def _force_utf8() -> None:
    """Windows 控制台默认 GBK，强制 UTF-8 输出避免 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(description="B 站视频内容提取与智能整理（命令行版）")
    parser.add_argument("url", help="B 站视频链接")
    parser.add_argument("--depth", default="essence", choices=["essence", "detailed", "full"])
    parser.add_argument("--audience", default="通用受众", help="目标受众")
    parser.add_argument("--purpose", default="快速了解视频核心内容", help="使用用途")
    parser.add_argument("--out", default="", help="输出 Markdown 路径（默认 data/{task_id}/result.md）")
    args = parser.parse_args()

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    req = ExtractRequest(url=args.url, depth=args.depth, audience=args.audience, purpose=args.purpose)
    rec = create_task(req)
    print(f"[任务] {rec.task_id}  链接: {args.url}")

    asyncio.run(run_task(rec.task_id, req))
    rec = get_task(rec.task_id)

    if rec.status == TaskStatus.DONE:
        out = Path(args.out) if args.out else settings.data_dir / rec.task_id / "result.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rec.markdown, encoding="utf-8")
        print(f"\n[已保存] {out}")
        print()
        print(rec.markdown)
        return 0

    print(f"\n[失败] {rec.error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())