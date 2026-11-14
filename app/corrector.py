"""LLM 转写纠错：二次校对 ASR 错别字/同音误识别。

- 逐批（默认 20 段）把转写文本交给 LLM 校对，只修错字不改语义；
- 任一批失败时保留原文继续（不阻断主流程）；
- LLM 未配置或开关关闭时直接返回原文。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import summarizer  # 复用其带重试的 LLM JSON 调用（模块属性方式，便于测试替换）
from .config import settings

logger = logging.getLogger(__name__)

_SYSTEM = "你是一名语音识别（ASR）文本校对员。只输出合法 JSON，不要输出任何其他内容。"

DEFAULT_CORRECT_TEMPLATE = """以下是语音识别（ASR）产出的文本片段，每行格式为 [编号] 文本：
{Blocks}

请逐条校对，只修正明确的错别字、同音字误识别、明显的标点/断句错误。严格要求：
1. 不得改写语义、不得增删内容、不得补全或润色句子
2. 无法确定是否有误的片段保持原文不变
3. 输出必须覆盖所有编号片段，编号与输入一一对应
4. 若某片段无需修改，text 原样输出该片段文本

只输出 JSON：{"segments": [{"index": 0, "text": "校对后文本"}]}"""

# 断句+标点增强档（CORRECT_PUNCTUATION=true 时使用）：
# 在修错字基础上优化断句与标点（加逗号/句号/问号等），但不得增删词语、不得改变语义，
# 以尽量保住引用对齐（_align_quote_time 做去空白子串匹配，标点不影响命中）。
PUNCTUATION_CORRECT_TEMPLATE = """以下是语音识别（ASR）产出的文本片段，每行格式为 [编号] 文本：
{Blocks}

请逐条校对，在修正错别字/同音误识别的基础上，**同时优化断句与标点**（补充缺失的逗号、句号、问号、顿号等，拆分过长无标点句）。严格要求：
1. 不得改写语义、不得增删内容词语、不得补全或润色句子
2. 只调整标点与断句位置，使句子更易读
3. 无法确定是否有误的片段保持原文不变（仅可加标点）
4. 输出必须覆盖所有编号片段，编号与输入一一对应
5. 若某片段无需修改，text 原样输出该片段文本

只输出 JSON：{"segments": [{"index": 0, "text": "校对后文本"}]}"""


@dataclass
class CorrectResult:
    texts: list[str] = field(default_factory=list)
    changed: int = 0  # 被修改的片段数
    batches: int = 0  # 实际完成的批次


def _correct_batch(texts: list[str], start_index: int, punctuation: bool | None = None) -> list[str]:
    """校对一批片段（编号从 start_index 开始），返回与入参等长的修正文本。

    punctuation 非None 时覆盖 settings.correct_punctuation；None 时用服务端配置。
    """
    blocks = "\n".join(f"[{i}] {t}" for i, t in enumerate(texts, start=start_index))
    use_punct = settings.correct_punctuation if punctuation is None else punctuation
    template = PUNCTUATION_CORRECT_TEMPLATE if use_punct else DEFAULT_CORRECT_TEMPLATE
    user = template.replace("{Blocks}", blocks)
    raw = summarizer._call_llm(_SYSTEM, user)
    segments = raw.get("segments") or []
    corrected = list(texts)
    for item in segments:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        text = str(item.get("text") or "").strip()
        if isinstance(idx, int) and 0 <= idx - start_index < len(corrected) and text:
            corrected[idx - start_index] = text
    return corrected


def correct_texts(texts: list[str], progress_cb=None, punctuation: bool | None = None) -> CorrectResult:
    """对转写文本列表做 LLM 校对。progress_cb: (percent: int) -> None。

    返回 CorrectResult；LLM 不可用或全部批次失败时 texts 与原列表一致。
    punctuation 非 None 时覆盖 settings.correct_punctuation。
    """
    if not settings.llm_configured or not texts:
        return CorrectResult(texts=list(texts), changed=0, batches=0)

    capped = texts[: settings.correct_max_segments]
    if len(capped) < len(texts):
        logger.warning("校对段数超过上限 %d，按前 %d 段处理", settings.correct_max_segments, len(capped))

    corrected = list(capped)
    total_changed = 0
    done_batches = 0
    batch_size = max(1, settings.correct_batch_size)

    for start in range(0, len(capped), batch_size):
        batch = capped[start : start + batch_size]
        try:
            result = _correct_batch(batch, start, punctuation)
            for offset, new_text in enumerate(result):
                if new_text.strip() and new_text.strip() != batch[offset].strip():
                    total_changed += 1
                    corrected[start + offset] = new_text.strip()
            done_batches += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("校对批次 %d 失败，保留原文：%s", start, exc)
        if progress_cb:
            progress_cb(int((start + len(batch)) / len(capped) * 100))

    logger.info("转写校对完成：%d/%d 段被修改，%d 批", total_changed, len(capped), done_batches)
    return CorrectResult(texts=corrected, changed=total_changed, batches=done_batches)