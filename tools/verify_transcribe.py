"""转写链路独立验证脚本。

用法（在项目根目录）：
    $env:ASR_ENGINE="whisper"; $env:ASR_MODEL="data\\models\\whisper-small"; $env:ASR_LANGUAGE=""
    .\\.venv\\Scripts\\python.exe tools\\verify_transcribe.py <音频路径>
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import transcriber  # noqa: E402
from app.config import settings  # noqa: E402


def main() -> int:
    audio = sys.argv[1] if len(sys.argv) > 1 else r"data\dl_test\audio.m4a"
    if not Path(audio).exists():
        print(f"音频不存在: {audio}")
        return 1

    print(f"[配置] engine={settings.asr_engine} model={settings.asr_model} language={settings.asr_language or 'auto'}")

    def _cb(p: int) -> None:
        print(f"  转写进度 {p}%", end="\r")

    result = transcriber.transcribe(audio, _cb)
    print(f"\n[结果] 语言={result.language} 置信度={result.avg_logprob:.3f} 时长={result.duration:.1f}s")
    print(f"[结果] 段数={len(result.segments)} 总字数={len(result.full_text)}")
    print("--- 前 8 段 ---")
    for seg in result.segments[:8]:
        print(f"[{seg.start:7.2f} -> {seg.end:7.2f}] {seg.text}")
    if len(result.segments) > 8:
        print(f"  ... 共 {len(result.segments)} 段")

    # 写 SRT 验证
    srt = Path(audio).with_suffix(".srt")
    transcriber.write_srt(result.segments, srt)
    print(f"[SRT] 已写入 {srt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())