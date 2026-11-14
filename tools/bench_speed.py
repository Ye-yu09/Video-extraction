"""CPU 转写提速 A/B 实测：condition_on_previous_text True vs False + VAD 回退验证。

用真实音频 data/dl_test/audio.m4a（212s），large-v3 CPU int8，beam_size=5 不变。
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from app.config import settings  # noqa: E402

settings.asr_language = "en"  # 该测试音频为英文

from faster_whisper import WhisperModel  # noqa: E402

AUDIO = Path("data/dl_test/audio.m4a")

print("加载模型 large-v3 ...")
t_load = time.time()
m = WhisperModel("large-v3", device="cpu", compute_type="int8")
print(f"模型加载 {time.time() - t_load:.1f}s\n")


def run(vad: bool, ctx: bool):
    t0 = time.time()
    it, info = m.transcribe(
        str(AUDIO), language="en", vad_filter=vad, beam_size=5,
        condition_on_previous_text=ctx,
    )
    segs = [(s.start, s.end, s.text) for s in it]
    dt = time.time() - t0
    chars = sum(len(t) for _, _, t in segs)
    return dt, segs, chars


# A) 旧配置：condition=True（带上下文，慢）
dA, sA, cA = run(False, True)
print(f"[A 旧 ctx=True ] {dA:6.1f}s | {len(sA):3d} 段 | {cA:5d} 字符")

# B) 新默认：condition=False（快）
dB, sB, cB = run(False, False)
print(f"[B 新 ctx=False] {dB:6.1f}s | {len(sB):3d} 段 | {cB:5d} 字符")

speedup = dA / dB if dB else 0
print(f"\n提速倍数: {speedup:.2f}x  (省 {dA - dB:.1f}s)")
print(f"输出量差异: A {cA} 字 vs B {cB} 字  ({(cB - cA) / cA * 100:+.1f}%)")

# C) VAD 空结果 bug 复现 + 回退验证
print("\n--- VAD 行为验证 ---")
dV, sV, _ = run(True, True)
print(f"vad_filter=True 主路径: {dV:.1f}s | {len(sV)} 段  (0 段=触发回退)")

# D) 端到端产品路径：transcriber.transcribe 走 vad→fallback 逻辑
print("\n--- 产品端到端 (vad=True + fallback=True + ctx=False) ---")
from app import transcriber  # noqa: E402

settings.asr_vad_filter = True
settings.asr_vad_fallback = True
settings.asr_context_text = False
t0 = time.time()
res = transcriber.transcribe(AUDIO)
print(f"产品转写: {time.time() - t0:.1f}s | {len(res.segments)} 段 | {len(res.full_text)} 字符 | lang={res.language}")
print(f"首段: {res.segments[0].text[:60] if res.segments else '(空)'}")
