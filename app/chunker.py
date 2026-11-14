"""文本分块：按 token 预算切分，块间保留重叠，避免切断语义。"""
from __future__ import annotations

import re

from .config import settings
from .models import Chunk, TranscriptSegment

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def estimate_tokens(text: str) -> int:
    """中文按约 0.75 token/字、其余字符按 0.3 token/字符 保守估算。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return int(cjk * 0.75 + other * 0.3) or 1


def _char_target(tokens: int) -> int:
    """token 预算折算为字符数（用于 overlap 截取），按中文占比粗略换算。"""
    return max(1, int(tokens / 0.75))


def _fmt_ts(sec: float) -> str:
    sec = int(sec)
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"


def _timed_text(segs: list[TranscriptSegment]) -> str:
    """生成带 [mm:ss] 时间戳的逐句文本，供 LLM 定位原文时间。"""
    return "\n".join(f"[{_fmt_ts(s.start)}] {s.text}" for s in segs)


def chunk_segments(
    segments: list[TranscriptSegment],
    max_tokens: int | None = None,
    overlap_tokens: int | None = None,
) -> list[Chunk]:
    """将带时间戳的段列表切成 Chunk 列表。

    - 每块累计 token 不超过 max_tokens，单段超长则按字符硬切；
    - 每块携带上一块尾部 overlap 文本，防止切在语义中间。
    """
    if max_tokens is None:
        max_tokens = settings.chunk_tokens
    if overlap_tokens is None:
        overlap_tokens = settings.overlap_tokens
    if not segments:
        return []

    chunks: list[Chunk] = []
    buf: list[TranscriptSegment] = []
    buf_tokens = 0

    def flush() -> None:
        nonlocal buf, buf_tokens
        if not buf:
            return
        text = "".join(s.text for s in buf)
        chunks.append(
            Chunk(
                index=len(chunks),
                start=round(buf[0].start, 2),
                end=round(buf[-1].end, 2),
                text=text,
                timed_text=_timed_text(buf),
            )
        )
        # 保留 overlap：下一块开头带上本块末尾若干字符
        keep_chars = _char_target(overlap_tokens)
        if keep_chars > 0 and len(text) > keep_chars * 2:
            tail = text[-keep_chars:]
            buf = [TranscriptSegment(start=buf[-1].start, end=buf[-1].end, text=tail)]
            buf_tokens = estimate_tokens(tail)
        else:
            buf = []
            buf_tokens = 0

    for seg in segments:
        seg_tokens = estimate_tokens(seg.text)
        if buf and buf_tokens + seg_tokens > max_tokens:
            flush()
        buf.append(seg)
        buf_tokens += seg_tokens

    flush()
    return chunks