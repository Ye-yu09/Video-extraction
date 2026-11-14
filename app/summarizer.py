"""大模型整理模块：Map（逐块提炼）→ Reduce（全局归纳）。

LLM 未配置或调用失败时进入降级模式（输出转写原文/逐段提炼），保证始终有产出。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import settings
from .errors import ERR_LLM_FAILED, AppError
from .models import DEPTHS, Chunk, ChunkDetail, KeyPoint, Quote, SummaryResult, Topic, VideoMeta

logger = logging.getLogger(__name__)

# ---- LLM 用量统计（进程级累计，供任务 stats 展示消耗）----
_usage_lock = threading.Lock()
_usage = {"prompt_tokens": 0, "completion_tokens": 0}


def llm_usage_snapshot() -> dict:
    """当前进程累计的 LLM token 用量快照（prompt/completion）。"""
    with _usage_lock:
        return dict(_usage)


def _record_usage(resp) -> None:
    """从 OpenAI 兼容响应中提取 usage 并累计（缺失时静默跳过）。"""
    usage = getattr(resp, "usage", None)
    if not usage:
        return
    try:
        with _usage_lock:
            _usage["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
            _usage["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)
    except (TypeError, ValueError):
        pass

_MAP_SYSTEM = "你是一名专业的视频内容分析师。只输出合法 JSON，不要输出任何其他内容。"
_REDUCE_SYSTEM = "你是一名资深内容编辑。只输出合法 JSON，不要输出任何其他内容。"

# ---- 默认提示词模板 ----
# 可用占位符（{Var} 形式，render_template 精确替换，与 JSON 花括号不冲突）：
#   Map:    {Index} {StartTime} {EndTime} {Audience} {Purpose} {ChunkText} {TimedSegments}
#   Reduce: {Title} {Uploader} {Duration} {Audience} {Purpose} {ChunkResults}
DEFAULT_MAP_TEMPLATE = """以下是某个视频转写文本的第 {Index} 段（时间范围 {StartTime}s–{EndTime}s）。
目标受众：{Audience}；用途：{Purpose}。

请从该段中提取：
1. core_ideas：核心观点列表（每条 ≤ 40 字，必须结合原文的具体表述/细节，避免空泛概括，最多 5 条）
2. facts：提到的事实、数据、案例（保持原数值与原本表述，不做总结）
3. conclusions：作者给出的结论或建议（如有，保留原话要点）
4. quotes：值得原文引用的句子（1–3 条，必须逐字来自原文，禁止改写，不要翻译）

只输出 JSON：{"core_ideas": [], "facts": [], "conclusions": [], "quotes": [{"quote": "原句"}]}

原文（每行带 [mm:ss] 时间戳，引用时以此为据）：
{TimedSegments}"""

DEFAULT_REDUCE_TEMPLATE = """以下是某视频各片段的分析结果（已按时间顺序排列）。
视频标题：{Title}，UP主：{Uploader}，总时长：{Duration}s。
目标受众：{Audience}；用途：{Purpose}。

请生成最终精华稿，严格按以下 JSON 结构输出：
{
  "summary": "一句话摘要，≤ 60 字",
  "overview": "视频概要，150-300 字一段话，讲清全片主题、核心论点、关键数据与结论，让没看过的人快速知道视频讲了什么",
  "key_points": [{"point": "要点，≤ 45 字，必须具体可感（含数据/细节/原句关键词，避免‘表达情感’这类空泛概括）, 共 6-10 条", "evidence": "支撑该要点的原文短句，逐字来自输入，≤ 40 字"}],
  "topics": [{"topic": "主题名", "details": ["细节，须具体"], "time_range": "mm:ss-mm:ss"}],
  "quotes": [{"quote": "原句，逐字来自输入分块结果的 quotes，禁止改写", "time": "mm:ss"}],
  "actions": ["可落地的行动建议"]
}

要求：key_points 的 point 必须具体可感；每条的 evidence 逐字来自『分块分析结果』；quotes 必须逐字来自分块结果的 quotes 字段；time/time_range 参考填即可（后端会校准为真实时间戳），禁止编造。
只输出 JSON。

分块分析结果：
{ChunkResults}"""

DEPTH_TEMPLATES = {
    "essence": """以下是某视频各片段的分析结果（已按时间顺序排列）。
视频标题：{Title}，UP主：{Uploader}，总时长：{Duration}s。
目标受众：{Audience}；用途：{Purpose}。

请生成精华摘要，严格按以下 JSON 结构输出：
{
  "summary": "一句话摘要，≤ 50 字",
  "overview": "视频概要，150-300 字一段话，讲清全片主题、核心论点、关键数据与结论，让没看过的人快速知道视频讲了什么",
  "key_points": [{"point": "要点，≤ 45 字，必须具体可感（含数据/细节/原句关键词，避免空泛概括）, 精选 4-7 条最核心", "evidence": "支撑该要点的原文短句，逐字来自输入，≤ 40 字"}],
  "quotes": [{"quote": "原句，逐字来自输入分块结果的 quotes，禁止改写", "time": "mm:ss"}],
  "actions": ["可落地的行动建议"]
}

要求：只保留最核心信息，不输出主题分类；key_points 的 point 必须具体可感；evidence 逐字来自『分块分析结果』；time 参考填即可（后端会校准），禁止编造。
只输出 JSON。

分块分析结果：
{ChunkResults}""",
    "detailed": """以下是某视频各片段的分析结果（已按时间顺序排列）。
视频标题：{Title}，UP主：{Uploader}，总时长：{Duration}s。
目标受众：{Audience}；用途：{Purpose}。

请生成详细要点整理，严格按以下 JSON 结构输出：
{
  "summary": "一句话摘要，≤ 60 字",
  "overview": "视频概要，150-300 字一段话，讲清全片主题、核心论点、关键数据与结论，让没看过的人快速知道视频讲了什么",
  "key_points": [{"point": "要点，≤ 45 字，必须具体可感（含数据/细节/原句关键词）, 6-10 条", "evidence": "支撑该要点的原文短句，逐字来自输入，≤ 40 字"}],
  "topics": [{"topic": "主题名", "details": ["细节，须具体"], "time_range": "mm:ss-mm:ss"}],
  "quotes": [{"quote": "原句，逐字来自输入分块结果的 quotes，禁止改写", "time": "mm:ss"}],
  "actions": ["可落地的行动建议"]
}

要求：信息按主题归类；key_points 的 point 必须具体可感；evidence 逐字来自『分块分析结果』；quotes 必须逐字来自分块结果的 quotes 字段；time/time_range 参考填即可（后端会校准为真实时间戳），禁止编造。
只输出 JSON。

分块分析结果：
{ChunkResults}""",
    "full": """以下是某视频各片段的分析结果（已按时间顺序排列）。
视频标题：{Title}，UP主：{Uploader}，总时长：{Duration}s。
目标受众：{Audience}；用途：{Purpose}。

请生成全文整理，严格按以下 JSON 结构输出：
{
  "summary": "一句话摘要，≤ 60 字",
  "overview": "视频概要，150-300 字一段话，讲清全片主题、核心论点、关键数据与结论，让没看过的人快速知道视频讲了什么",
  "key_points": [{"point": "要点，≤ 45 字，必须具体可感（含数据/细节/原句关键词）, 8-12 条", "evidence": "支撑该要点的原文短句，逐字来自输入，≤ 40 字"}],
  "topics": [{"topic": "主题名", "details": ["细节，须具体"], "time_range": "mm:ss-mm:ss"}],
  "quotes": [{"quote": "原句，逐字来自输入分块结果的 quotes，禁止改写", "time": "mm:ss"}],
  "actions": ["可落地的行动建议"],
  "segments_summary": [{"time": "mm:ss-mm:ss（参考填，后端校准）", "gist": "该分块的核心内容概括，≤ 60 字"}]
}

要求：segments_summary 必须按输入分块顺序逐一覆盖全部分块；key_points 具体可感；evidence 逐字来自『分块分析结果』；time/time_range 参考填即可（后端会校准为真实时间戳），禁止编造。
只输出 JSON。

分块分析结果：
{ChunkResults}""",
}
DEFAULT_REDUCE_BY_DEPTH = DEPTH_TEMPLATES  # 别名：按 depth 选择


def render_template(template: str, **kwargs: str) -> str:
    """按 {Var} 占位符精确替换（非 str.format，避免与 JSON 花括号冲突）。"""
    out = template
    for key, value in kwargs.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def llm_configured() -> bool:
    return settings.llm_configured


def _extract_json(text: str) -> dict:
    """容错解析：优先 json.loads，失败则截取首个 { 到末个 } 之间的内容。"""
    text = (text or "").strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    raise AppError(ERR_LLM_FAILED, "LLM 返回内容无法解析为 JSON")


def _make_client():
    from openai import OpenAI  # noqa: PLC0415

    return OpenAI(
        api_key=settings.llm_api_key or "unset",
        base_url=settings.llm_base_url or None,
        timeout=settings.llm_timeout,
    )


def _call_llm(system: str, user: str) -> dict:
    """带指数退避重试的 LLM JSON 调用；成功响应累计 token 用量。"""
    client = _make_client()
    last_exc: Exception | None = None
    for attempt in range(settings.llm_max_retries + 1):
        try:
            kwargs = dict(
                model=settings.llm_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            if settings.llm_max_tokens > 0:
                kwargs["max_tokens"] = settings.llm_max_tokens
            resp = client.chat.completions.create(**kwargs)
            _record_usage(resp)
            content = resp.choices[0].message.content or ""
            return _extract_json(content)
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < settings.llm_max_retries:
                time.sleep(1.5 ** (attempt + 1))
    raise AppError(ERR_LLM_FAILED, f"LLM 调用失败：{str(last_exc)[:200]}", retryable=True)


_LANG_LABEL = {"zh": "中文", "en": "English", "ja": "日本語", "ko": "한국어"}


def _lang_line(output_lang: str) -> str:
    """输出语言前缀：拼到 user 消息顶部，要求 LLM 用指定语言撰写全部内容。"""
    lang = (output_lang or "zh").strip()
    label = _LANG_LABEL.get(lang, lang)
    return f"重要：请用{label}撰写全部输出内容（摘要、要点、主题、引用说明、行动项等）。"


def _norm_key_points(raw: list) -> list[KeyPoint]:
    out: list[KeyPoint] = []
    for item in raw or []:
        if isinstance(item, str):
            out.append(KeyPoint(point=item.strip()))
        elif isinstance(item, dict):
            out.append(
                KeyPoint(
                    point=str(item.get("point") or item.get("content") or "").strip(),
                    time=str(item.get("time") or ""),
                    evidence=str(item.get("evidence") or "").strip(),
                )
            )
    return [k for k in out if k.point]


def _norm_topics(raw: list) -> list[Topic]:
    out: list[Topic] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        details = item.get("details") or item.get("points") or []
        details = [str(d) for d in details if str(d).strip()]
        out.append(
            Topic(
                topic=str(item.get("topic") or item.get("title") or "未命名主题").strip(),
                details=details,
                time_range=str(item.get("time_range") or item.get("time") or ""),
            )
        )
    return [t for t in out if t.topic and t.topic != "未命名主题"]


def _norm_quotes(raw: list) -> list[Quote]:
    out: list[Quote] = []
    for item in raw or []:
        if isinstance(item, str):
            out.append(Quote(quote=item.strip()))
        elif isinstance(item, dict):
            out.append(
                Quote(
                    quote=str(item.get("quote") or "").strip(),
                    time=str(item.get("time") or ""),
                )
            )
    return [q for q in out if q.quote]


def _norm_chunk_details(raw) -> list[ChunkDetail]:
    """full 深度档：逐段小结（time 随后由后端按真实分块回填）。"""
    out: list[ChunkDetail] = []
    for item in raw or []:
        if isinstance(item, str):
            out.append(ChunkDetail(gist=item.strip()))
        elif isinstance(item, dict):
            out.append(
                ChunkDetail(
                    time=str(item.get("time") or ""),
                    gist=str(item.get("gist") or item.get("content") or "").strip(),
                )
            )
    return [d for d in out if d.gist]


def normalize_depth(depth: str | None) -> str:
    """规范化 depth 参数（内部安全网），非法值回退 essence。API 边界已由 models 校验。"""
    return depth if depth in DEPTHS else "essence"


def map_chunk(
    chunk: Chunk,
    audience: str,
    purpose: str,
    template: str | None = None,
    output_lang: str = "zh",
) -> dict:
    """Map 阶段：单块提炼。template 为空时依次回退 .env LLM_MAP_PROMPT → 内置默认。"""
    tpl = template or settings.llm_map_prompt or DEFAULT_MAP_TEMPLATE
    timed_segs = chunk.timed_text or chunk.text
    user = render_template(
        tpl,
        Index=str(chunk.index + 1),
        StartTime=f"{chunk.start:.0f}",
        EndTime=f"{chunk.end:.0f}",
        Audience=audience,
        Purpose=purpose,
        ChunkText=chunk.text,
        TimedSegments=timed_segs,
    )
    user = _lang_line(output_lang) + "\n" + user
    return _call_llm(_MAP_SYSTEM, user)


def reduce_summary(
    chunk_results: list[dict],
    meta: VideoMeta,
    audience: str,
    purpose: str,
    template: str | None = None,
    depth: str = "essence",
    output_lang: str = "zh",
) -> SummaryResult:
    """Reduce 阶段：全局归纳。

    template 为空时依次回退 .env LLM_REDUCE_PROMPT → 按 depth 选择内置模板。
    """
    payload = json.dumps(chunk_results, ensure_ascii=False)
    depth = normalize_depth(depth)
    tpl = (
        template
        or settings.llm_reduce_prompt
        or DEPTH_TEMPLATES.get(depth, DEPTH_TEMPLATES["essence"])
    )
    user = render_template(
        tpl,
        Title=meta.title,
        Uploader=meta.uploader,
        Duration=str(meta.duration),
        Audience=audience,
        Purpose=purpose,
        ChunkResults=payload,
    )
    user = _lang_line(output_lang) + "\n" + user
    raw = _call_llm(_REDUCE_SYSTEM, user)
    return SummaryResult(
        summary=str(raw.get("summary") or "").strip(),
        overview=str(raw.get("overview") or "").strip(),
        key_points=_norm_key_points(raw.get("key_points")),
        topics=_norm_topics(raw.get("topics")),
        quotes=_norm_quotes(raw.get("quotes")),
        actions=[str(a).strip() for a in (raw.get("actions") or []) if str(a).strip()],
        chunk_details=_norm_chunk_details(
            raw.get("segments_summary") or raw.get("chunk_details")
        ),
    )


def summarize_chunks(
    chunks: list[Chunk],
    meta: VideoMeta,
    audience: str,
    purpose: str,
    progress_cb=None,
    map_template: str | None = None,
    reduce_template: str | None = None,
    segments=None,
    depth: str = "essence",
    output_lang: str = "zh",
    self_check: str = "",
) -> SummaryResult:
    """Map-Reduce 汇总。

    - LLM 未配置 → 降级（转写原文要点）
    - 个别块提炼失败 → 标记后继续
    - 全部块失败 / Reduce 失败 → 降级（返回已有转写要点）
    - map_template / reduce_template 为空时回退 .env 配置与内置模板
    - segments 传入原始转写段时，输出时间戳会被校准为真实时间
    - depth 控制输出深度：essence=精华 / detailed=+主题详解 / full=+逐段细览
    - output_lang 控制整理结果输出语言（zh/en/ja…），透传给 Map/Reduce 提示词
    - self_check 非空时覆盖 settings.summary_self_check（off/full/always）
    """
    if not llm_configured():
        return _degraded(chunks, meta, "未配置 LLM_API_KEY，跳过智能整理，输出转写原文要点")

    results: list[dict | None] = [None] * len(chunks)
    failed_indices: list[int] = []
    with ThreadPoolExecutor(max_workers=min(settings.llm_concurrency, max(1, len(chunks)))) as pool:
        futures = {
            pool.submit(map_chunk, c, audience, purpose, map_template, output_lang): i
            for i, c in enumerate(chunks)
        }
        done = 0
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except AppError as exc:
                logger.warning("chunk %d 提炼失败: %s", idx, exc.message)
                failed_indices.append(idx)
            except Exception as exc:  # noqa: BLE001
                logger.warning("chunk %d 提炼异常: %s", idx, exc)
                failed_indices.append(idx)
            done += 1
            if progress_cb:
                progress_cb(int(32 + done / len(chunks) * 58))

    valid = [r for r in results if r]
    if not valid:
        return _degraded(chunks, meta, "所有分块提炼失败（LLM 不可用），输出转写原文要点")

    for idx in failed_indices:
        results[idx] = {
            "core_ideas": ["（本段未提炼成功）"],
            "facts": [],
            "conclusions": [],
            "quotes": [],
        }

    try:
        summary = reduce_summary(
            results, meta, audience, purpose, reduce_template, depth, output_lang  # type: ignore[arg-type]
        )
    except AppError as exc:
        logger.warning("Reduce 失败，降级输出: %s", exc.message)
        return _degraded(chunks, meta, f"全局归纳失败（{exc.message}），输出逐段提炼结果")

    # full 深度档：逐段小结按真实分块回填时间范围（覆盖 LLM 猜测值）
    if summary.chunk_details and chunks:
        for detail, c in zip(summary.chunk_details, chunks):
            detail.time = f"{_fmt_ts(c.start)}-{_fmt_ts(c.end)}"

    # 用真实转写段校准全部时间戳（确定性对齐，覆盖 LLM 猜测值）
    try:
        summary = align_summary_times(summary, segments)
    except Exception:  # noqa: BLE001
        logger.warning("时间戳校准失败，保留 LLM 输出", exc_info=True)

    # 摘要自检：Reduce 后 LLM 再评审一遍，发现遗漏关键数据则补充（+1 次调用）
    self_check_mode = self_check or settings.summary_self_check
    if self_check_mode in ("full", "always") and llm_configured():
        if self_check_mode == "full" and depth != "full":
            pass  # full 模式仅 full 档启用
        else:
            try:
                summary = _self_check(summary, results, meta, audience, purpose, output_lang)
            except Exception:  # noqa: BLE001
                logger.warning("摘要自检失败，保留原始 Reduce 结果", exc_info=True)

    if progress_cb:
        progress_cb(96)
    if failed_indices:
        summary.degraded = True
        summary.degraded_reason = f"部分分块提炼失败：{failed_indices}"
    return summary


_SELF_CHECK_SYSTEM = "你是一名内容审稿编辑。只输出合法 JSON，不要输出任何其他内容。"

_SELF_CHECK_TEMPLATE = """以下是某视频整理后的摘要结果与各片段提炼数据。
视频标题：{Title}，UP主：{Uploader}，总时长：{Duration}s。

已生成的摘要：
{Summary}

已有要点：
{KeyPoints}

各片段提炼数据（含 core_ideas / facts / conclusions）：
{ChunkData}

请评审上述摘要是否遗漏了「各片段提炼数据」中的重要事实、数据或结论。
- 如果已覆盖，返回 {"missed": false, "additions": []}
- 如果有遗漏，返回 {"missed": true, "additions": [{"point": "遗漏的要点（≤45字，须含具体数据/细节）", "evidence": "原文短句"}]}
- additions 最多 3 条，只补充确属遗漏的关键内容，不做润色
只输出 JSON。"""


def _self_check(
    summary: SummaryResult,
    chunk_results: list[dict],
    meta: VideoMeta,
    audience: str,
    purpose: str,
    output_lang: str = "zh",
) -> SummaryResult:
    """摘要自检：Reduce 后让 LLM 评审是否遗漏关键数据，遗漏则补充 key_points。"""
    kp_text = "\n".join(f"- {k.point}" for k in summary.key_points) or "（无）"
    user = render_template(
        _SELF_CHECK_TEMPLATE,
        Title=meta.title,
        Uploader=meta.uploader,
        Duration=str(meta.duration),
        Summary=summary.summary or "（无）",
        KeyPoints=kp_text,
        ChunkData=json.dumps(chunk_results, ensure_ascii=False)[:8000],  # 截断防超长
    )
    user = _lang_line(output_lang) + "\n" + user
    raw = _call_llm(_SELF_CHECK_SYSTEM, user)
    if not raw.get("missed"):
        return summary
    additions = raw.get("additions") or []
    if not additions:
        return summary
    new_points = _norm_key_points(additions)
    if new_points:
        summary.key_points.extend(new_points)
        logger.info("摘要自检：补充 %d 条遗漏要点", len(new_points))
    return summary


def _degraded(chunks: list[Chunk], meta: VideoMeta, reason: str) -> SummaryResult:
    points = []
    for c in chunks:
        head = c.text[:80].strip()
        if head:
            points.append(KeyPoint(point=head, time=_fmt_ts(c.start)))
    return SummaryResult(
        summary=f"（降级输出）{reason}",
        key_points=points,
        degraded=True,
        degraded_reason=reason,
    )


def _fmt_ts(sec: float) -> str:
    sec = int(sec)
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"


def _align_quote_time(text: str, segments) -> str:
    """在转写段列表中定位引文的真实时间戳（确定性对齐，不依赖 LLM 编造）。

    匹配策略：① 整句包含匹配 → ② 去空白前缀匹配 → ③ difflib 相似度兜底。
    返回 "mm:ss"；找不到返回 ""。
    """
    import difflib

    t = (text or "").strip()
    if not t:
        return ""

    def _norm(s: str) -> str:
        return re.sub(r"\s+", "", s)

    # ① 整句包含：引文是某段文本的子串，或段文本是引文的子串
    for seg in segments:
        st = seg.text
        if t in st or st in t:
            return _fmt_ts(seg.start)
    # ② 去空白后的前缀（前 15 字符）匹配
    head = _norm(t)[:15]
    if head:
        for seg in segments:
            if head in _norm(seg.text):
                return _fmt_ts(seg.start)
    # ③ difflib 相似度兜底
    best, best_ratio = "", 0.0
    for seg in segments:
        ratio = difflib.SequenceMatcher(None, _norm(t), _norm(seg.text)).ratio()
        if ratio > best_ratio:
            best, best_ratio = _fmt_ts(seg.start), ratio
    return best if best_ratio >= 0.5 else ""


def align_summary_times(summary: SummaryResult, segments) -> SummaryResult:
    """用真实转写段回填/校准 summary 中所有时间戳（evidence/引文优先）。"""
    if not segments:
        return summary
    for kp in summary.key_points:
        src = kp.evidence or kp.point
        kp.time = _align_quote_time(src, segments)
    for q in summary.quotes:
        q.time = _align_quote_time(q.quote, segments)
    return summary


def render_markdown(meta: VideoMeta, summary: SummaryResult, transcript_full: str = "") -> str:
    """按文档第 5 节模板渲染 Markdown。"""
    lines = [f"# 【视频精华】{meta.title or '未命名视频'}", ""]
    lines.append(
        f"> 元信息：UP主：{meta.uploader or '未知'} ｜ 时长：{_fmt_ts(meta.duration)}"
        f" ｜ 链接：{meta.webpage_url or meta.url}"
    )
    if summary.degraded:
        lines.append(f"> ⚠️ {summary.degraded_reason}")
    lines.append("")
    lines.append("## 一句话摘要")
    lines.append(summary.summary or "（无）")
    lines.append("")
    if summary.overview:
        lines.append("## 视频概要")
        lines.append(summary.overview)
        lines.append("")
    if summary.key_points:
        lines.append("## 精华要点")
        for i, kp in enumerate(summary.key_points, 1):
            ts = f" —— 出处 {kp.time}" if kp.time else ""
            lines.append(f"- **要点 {i}**：{kp.point}{ts}")
        lines.append("")
    if summary.topics:
        lines.append("## 分主题详解")
        for t in summary.topics:
            lines.append(f"### {t.topic}" + (f"（{t.time_range}）" if t.time_range else ""))
            for d in t.details:
                lines.append(f"- {d}")
            lines.append("")
    if summary.quotes:
        lines.append("## 关键引用")
        for q in summary.quotes:
            ts = f" —— {q.time}" if q.time else ""
            lines.append(f"> {q.quote}{ts}")
        lines.append("")
    if summary.actions:
        lines.append("## 行动项 / 可落地建议")
        for i, a in enumerate(summary.actions, 1):
            lines.append(f"{i}. {a}")
        lines.append("")
    if summary.chunk_details:
        lines.append("## 逐段细览")
        for cd in summary.chunk_details:
            ts = f"（{cd.time}）" if cd.time else ""
            lines.append(f"- {cd.gist}{ts}")
        lines.append("")
    lines.append("---")
    lines.append("*由 视频提取器 自动生成，仅供参考。*")
    if transcript_full:
        lines.append("")
        lines.append("<details><summary>查看完整转写</summary>")
        lines.append("")
        lines.append(transcript_full)
        lines.append("")
        lines.append("</details>")
    return "\n".join(lines)