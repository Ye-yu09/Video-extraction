"""管道编排与任务管理：下载 → 转写 → 分块 → LLM 整理 → 渲染输出。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import chunker, corrector, downloader, summarizer, transcriber
from .config import settings
from .errors import (
    ERR_ASR_FAILED,
    ERR_ASR_TIMEOUT,
    ERR_CANCELLED,
    ERR_INTERNAL,
    ERR_NOT_FOUND,
    ERR_TOO_LONG,
    AppError,
)
from .models import DEPTHS, ExtractRequest, Stage, TaskRecord, TaskStatus, TranscriptSegment, VideoMeta
from .store import TaskStore

logger = logging.getLogger(__name__)

_asr_sem = asyncio.Semaphore(settings.max_concurrent_asr)
_llm_sem = asyncio.Semaphore(max(1, settings.max_concurrent_llm_tasks))  # 全局 LLM 并发上限（跨任务）
_store = TaskStore(settings.data_dir / "tasks.db")
_tasks: dict[str, TaskRecord] = _store.load_all()
_lock = threading.RLock()
_submit_lock = threading.Lock()  # 提交去重锁：防并发相同请求重复建任务
_persist_state: dict[str, tuple[float, int]] = {}  # task_id -> (上次落库时间, 当时 progress)，进度写库节流用


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _recover_interrupted() -> None:
    """服务启动时，将上次运行中遗留的 queued/running 任务标记为 failed。"""
    for rec in _tasks.values():
        if rec.status in (TaskStatus.RUNNING, TaskStatus.QUEUED):
            rec.status = TaskStatus.FAILED
            rec.error = AppError(ERR_INTERNAL, "任务因服务重启中断，请重新提交").to_dict()
            rec.updated_at = _now()
            _store.upsert(rec)
            logger.warning("恢复任务 %s：标记为 failed（服务重启中断）", rec.task_id)


_recover_interrupted()


def _prompt_hash(map_prompt: str, reduce_prompt: str) -> str:
    """自定义模板哈希：任一模板非空则参与缓存键，空模板返回空串（不参与区分）。"""
    raw = f"{map_prompt}|{reduce_prompt}".strip("|")
    if not raw:
        return ""
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def create_task(req: ExtractRequest) -> TaskRecord:
    """创建任务并落库请求参数（url/depth/自定义模板哈希等），供缓存去重与历史展示。"""
    task_id = uuid.uuid4().hex[:12]
    norm_url = downloader.validate_url(req.url)
    rec = TaskRecord(
        task_id=task_id,
        status=TaskStatus.QUEUED,
        created_at=_now(),
        updated_at=_now(),
        url=norm_url,
        depth=req.depth if req.depth in DEPTHS else "essence",
        audience=req.audience,
        purpose=req.purpose,
        prompt_hash=_prompt_hash(req.map_prompt, req.reduce_prompt),
        asr_model=req.asr_model.strip(),
        output_lang=req.output_lang.strip() or "zh",
        page=req.page,
        correct_punctuation=req.correct_punctuation,
        summary_self_check=req.summary_self_check,
    )
    _tasks[task_id] = rec
    _store.upsert(rec)
    return rec


def get_task(task_id: str) -> TaskRecord:
    rec = _tasks.get(task_id)
    if not rec:
        raise AppError(ERR_NOT_FOUND, "任务不存在")
    return rec


def _rec_url(rec: TaskRecord) -> str:
    """任务记录链接：优先请求时落库的 url，旧记录回退 video.url。"""
    if rec.url:
        return rec.url
    return rec.video.url if rec.video else ""


def find_cached(req: ExtractRequest | str) -> TaskRecord | None:
    """按「规范化链接 + depth + 自定义模板哈希」查找可复用的进行中/已完成任务。

    - running/queued：返回该任务，新提交者接入同一进度，避免重复下载/转写
    - done：返回该任务，直接复用结果（省去重复下载/转写/LLM 调用）；
      **降级完成的任务不复用**——若当时 LLM 未配置或整理失败（summary.degraded），
      之后配好 LLM 再提交应重跑一次拿到真整理结果，而不是永久复用降级稿
    - failed/cancelled / 深度不同 / 自定义模板不同：不命中，允许重新提交
    - depth 与自定义模板均计入缓存键，避免「detailed 请求」误命中「essence 缓存」
    """
    req = req if isinstance(req, ExtractRequest) else ExtractRequest(url=req)
    norm = downloader.validate_url(req.url)
    depth = req.depth if req.depth in DEPTHS else "essence"
    phash = _prompt_hash(req.map_prompt, req.reduce_prompt)
    page = req.page
    for rec in _tasks.values():
        if rec.status not in (TaskStatus.RUNNING, TaskStatus.QUEUED, TaskStatus.DONE):
            continue
        if _rec_url(rec) != norm:
            continue
        if (rec.depth if rec.depth in DEPTHS else "essence") != depth:
            continue
        if (rec.prompt_hash or "") != phash:
            continue
        if (rec.page or 0) != page:
            continue
        if rec.status == TaskStatus.DONE and rec.summary and rec.summary.degraded:
            continue  # 降级结果不参与缓存复用
        return rec
    return None


def submit_or_reuse(req: ExtractRequest) -> tuple[TaskRecord, bool]:
    """提交入口：加锁去重——命中缓存返回 (已有任务, True)，否则创建新任务返回 (新任务, False)。

    锁保证并发相同请求只创建一个任务（find + create 原子）。
    """
    with _submit_lock:
        cached = find_cached(req)
        if cached:
            return cached, True
        rec = create_task(req)
        return rec, False


def list_tasks(page: int = 1, page_size: int = 20, status: str | None = None) -> tuple[list[TaskRecord], int]:
    """按更新时间倒序分页返回任务列表与总数；status 非空时按状态过滤（非法值忽略）。"""
    items = list(_tasks.values())
    if status:
        valid = {s.value for s in TaskStatus}
        if status in valid:
            items = [r for r in items if r.status.value == status]
    items.sort(key=lambda r: r.updated_at, reverse=True)
    total = len(items)
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    start = (page - 1) * page_size
    return items[start : start + page_size], total


def cancel_task(task_id: str) -> TaskRecord:
    rec = get_task(task_id)
    if rec.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
        return rec
    rec.status = TaskStatus.CANCELLED
    rec.stage = Stage.NONE
    rec.updated_at = _now()
    _store.upsert(rec)
    return rec


def _task_dir(task_id: str) -> Path:
    d = settings.data_dir / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---- 转写级缓存（省钱主力：同一视频换深度/用途不再重复下载+转写+纠错）----

def _transcript_cache_dir() -> Path:
    d = settings.data_dir / "cache" / "transcripts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _transcript_cache_key(req: ExtractRequest) -> str:
    """转写缓存键 = BV + 实际 ASR 模型 + 语言 + VAD + 纠错开关 + 引擎。

    任一配置变化（换模型/改语言）自动失效，保证缓存文本与当前 ASR 配置一致。
    """
    bv = downloader.validate_url(req.url).rstrip("/").rsplit("/", 1)[-1]
    model = (req.asr_model or settings.asr_model).strip()
    lang = settings.asr_language or "auto"
    vad = "1" if settings.asr_vad_filter else "0"
    correct = "1" if (settings.llm_correct and settings.llm_configured) else "0"
    raw = f"{bv}|p{req.page}|{model}|{lang}|{vad}|{correct}|{settings.asr_engine}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _transcript_cache_path(req: ExtractRequest) -> Path:
    return _transcript_cache_dir() / f"{_transcript_cache_key(req)}.json"


def _load_transcript_cache(path: Path) -> transcriber.TranscriptResult | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return transcriber.TranscriptResult(
            segments=[TranscriptSegment(**s) for s in data.get("segments", [])],
            language=data.get("language", ""),
            avg_logprob=float(data.get("avg_logprob", 0.0) or 0.0),
            duration=float(data.get("duration", 0.0) or 0.0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("转写缓存读取失败 %s: %s", path.name, exc)
        return None


def _save_transcript_cache(path: Path, result: transcriber.TranscriptResult) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("转写缓存写入失败 %s: %s", path.name, exc)


def _update(rec: TaskRecord, **kw) -> None:
    """更新任务字段并落库。

    仅 progress 变化时做节流：距上次落库 <1s 且 progress 变化 <2% 则跳过 DB 写（内存已更新），
    降低转写/整理高频进度回调下的 SQLite 写压力；stage/status 等关键变更必落库。
    """
    with _lock:
        for k, v in kw.items():
            setattr(rec, k, v)
        rec.updated_at = _now()
        if set(kw) == {"progress"}:
            now = time.time()
            last = _persist_state.get(rec.task_id)
            if last and (now - last[0] < 1.0) and abs(rec.progress - last[1]) < 2:
                return  # 节流：跳过本次 DB 写
        _store.upsert(rec)
        _persist_state[rec.task_id] = (time.time(), rec.progress)


async def run_task(task_id: str, req: ExtractRequest) -> None:
    """后台任务入口：捕获全部异常并落库，保证任务状态收敛。"""
    rec = get_task(task_id)
    task_dir = _task_dir(task_id)
    try:
        await _run(rec, req, task_dir)
    except AppError as exc:
        _update(rec, status=TaskStatus.FAILED, error=exc.to_dict())
    except Exception as exc:  # noqa: BLE001
        logger.exception("task %s 内部异常", task_id)
        _update(
            rec,
            status=TaskStatus.FAILED,
            error=AppError(ERR_INTERNAL, f"内部错误：{str(exc)[:200]}").to_dict(),
        )
    finally:
        _cleanup(task_dir)


def _check_cancelled(rec: TaskRecord) -> None:
    if rec.status == TaskStatus.CANCELLED:
        raise AppError(ERR_CANCELLED, "任务已取消")


async def _run(rec: TaskRecord, req: ExtractRequest, task_dir: Path) -> None:
    # Stage 1: 校验 + 元信息
    _update(rec, status=TaskStatus.RUNNING, stage=Stage.DOWNLOADING, progress=2)
    url = downloader.validate_url(req.url)

    meta = await asyncio.to_thread(downloader.fetch_meta, url, req.page)
    if settings.max_video_duration > 0 and meta.duration > settings.max_video_duration:
        raise AppError(
            ERR_TOO_LONG,
            f"视频时长 {meta.duration // 60} 分钟超过上限 {settings.max_video_duration // 60} 分钟",
        )
    _update(rec, video=meta, stage_progress={"title": meta.title, "duration": meta.duration})

    # Stage 2+3: 转写（含转写级缓存——命中则跳过下载/转写/纠错）
    _check_cancelled(rec)
    llm_before = summarizer.llm_usage_snapshot()  # LLM 用量起点（含纠错+整理）
    correct_stats: dict = {}
    cached_tx = False

    result: transcriber.TranscriptResult | None = None
    cache_path = _transcript_cache_path(req) if settings.transcript_cache else None
    if cache_path and cache_path.exists():
        cached = _load_transcript_cache(cache_path)
        if cached and cached.segments:
            result = cached
            cached_tx = True
            logger.info("任务 %s 命中转写缓存：%s（跳过下载/转写/纠错）", rec.task_id, cache_path.name)

    if result is None:
        # 未命中：下载音频 → 转写（并发受限，支持模型覆盖）
        audio_path = await asyncio.to_thread(downloader.download_audio, url, task_dir, req.page)

        def _cb(pct: int, phase: str = "transcribe") -> None:
            if phase == "model":
                _update(rec, progress=20 + int(pct * 0.1))  # 20-30%：模型加载/权重下载
            else:
                _update(rec, progress=30 + int(pct * 0.4))  # 30-70%：语音转写

        async with _asr_sem:
            _update(rec, stage=Stage.LOADING_MODEL, progress=20)

            async def _do_transcribe() -> transcriber.TranscriptResult:
                loop = asyncio.get_running_loop()
                fut: asyncio.Future = loop.create_future()

                def _worker() -> None:
                    try:
                        res = transcriber.transcribe(audio_path, _cb, model=req.asr_model or None)
                        loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(res))
                    except BaseException as exc:  # noqa: BLE001
                        loop.call_soon_threadsafe(lambda: fut.done() or fut.set_exception(exc))

                threading.Thread(target=_worker, daemon=True).start()
                return await fut

            coro = _do_transcribe()
            try:
                if settings.asr_timeout > 0:
                    result = await asyncio.wait_for(coro, timeout=settings.asr_timeout)
                else:
                    result = await coro
            except asyncio.TimeoutError as exc:
                raise AppError(
                    ERR_ASR_TIMEOUT,
                    f"转写超时（{settings.asr_timeout // 60} 分钟未完成）。CPU 跑大模型较慢，"
                    "建议：①前端「转写模型」换 tiny/base/small 档重试；②或调大 .env 的 ASR_TIMEOUT",
                ) from exc
            _update(rec, stage=Stage.TRANSCRIBING, progress=70)

        if not result.segments:
            raise AppError(ERR_ASR_FAILED, "转写结果为空")

        # 原始转写留档（未校对版本，供审计对比）
        (task_dir / "transcript.raw.json").write_text(
            json.dumps(result.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Stage 3.5: LLM 转写纠错（失败自动降级；受全局 LLM 并发限制）
        if settings.llm_correct and settings.llm_configured:
            _check_cancelled(rec)
            _update(rec, stage=Stage.CORRECTING, progress=66)

            def _correct():
                def _cb(p: int) -> None:
                    _update(rec, progress=66 + int(p * 0.04))

                return corrector.correct_texts([s.text for s in result.segments], _cb, punctuation=req.correct_punctuation)

            try:
                async with _llm_sem:
                    cr = await asyncio.to_thread(_correct)
                if cr.changed:
                    for seg, new_text in zip(result.segments, cr.texts):
                        seg.text = new_text
                    correct_stats = {
                        "corrected_segments": cr.changed,
                        "correct_batches": cr.batches,
                    }
                    logger.info("任务 %s 转写纠错：%d 段被修改", rec.task_id, cr.changed)
            except Exception as exc:  # noqa: BLE001
                logger.warning("转写纠错阶段异常，继续主流程：%s", exc)

        # 把纠错后的最终转写写入缓存（供同视频换深度/用途时复用）
        if cache_path:
            _save_transcript_cache(cache_path, result)

    transcript_path = task_dir / "transcript.json"
    transcript_path.write_text(
        json.dumps(result.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    transcriber.write_srt(result.segments, task_dir / "transcript.srt")
    _update(
        rec,
        stage=Stage.SUMMARIZING,
        progress=70,
        stats={
            "language": result.language,
            "avg_logprob": round(result.avg_logprob, 3),
            "chars": len(result.full_text),
            "segments": len(result.segments),
            "cached_transcript": cached_tx,
            **correct_stats,
        },
    )

    # Stage 4: 分块
    _check_cancelled(rec)
    chunks = chunker.chunk_segments(result.segments)
    if not chunks:
        raise AppError(ERR_ASR_FAILED, "文本为空，无法整理")
    if len(chunks) > settings.max_chunks:
        raise AppError(
            ERR_TOO_LONG,
            f"文本过长（{len(chunks)} 块，上限 {settings.max_chunks} 块），请考虑人工截取",
        )
    (task_dir / "chunks.json").write_text(
        json.dumps([c.model_dump() for c in chunks], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Stage 5: LLM 整理（受全局 LLM 并发限制；output_lang 透传）
    _check_cancelled(rec)
    _summarize_t0 = time.time()

    def _summarize():
        def _cb(p: int) -> None:
            _update(rec, progress=p)

        return summarizer.summarize_chunks(
            chunks,
            meta,
            req.audience,
            req.purpose,
            _cb,
            map_template=req.map_prompt or None,
            reduce_template=req.reduce_prompt or None,
            segments=result.segments,
            depth=req.depth,
            output_lang=req.output_lang or "zh",
            self_check=req.summary_self_check,
        )

    async with _llm_sem:
        summary = await asyncio.to_thread(_summarize)
    _summarize_s = round(time.time() - _summarize_t0, 1)

    # LLM 用量统计（任务级：纠错 + Map + Reduce 全量消耗）
    llm_after = summarizer.llm_usage_snapshot()
    used = {k: llm_after[k] - llm_before[k] for k in llm_after}
    llm_tokens = {"prompt": used["prompt_tokens"], "completion": used["completion_tokens"]}
    if cached_tx and llm_tokens["prompt"] == 0 and llm_tokens["completion"] == 0:
        # 转写缓存命中且 LLM 零调用：整理阶段也复用了缓存结果（省钱闭环）
        llm_tokens["cached_reuse"] = True

    # Stage 6: 渲染输出
    _check_cancelled(rec)
    _update(rec, stage=Stage.FINALIZING, progress=97)
    markdown = summarizer.render_markdown(meta, summary, result.full_text)
    (task_dir / "result.md").write_text(markdown, encoding="utf-8")
    _update(
        rec,
        status=TaskStatus.DONE,
        progress=100,
        stage=Stage.NONE,
        summary=summary,
        markdown=markdown,
        transcript_path=str(transcript_path),
        stats={
            **rec.stats,
            "chunks": len(chunks),
            "summarize_seconds": _summarize_s,
            "llm_tokens": llm_tokens,
        },
    )


def _cleanup(task_dir: Path) -> None:
    """清理中间产物（音频/视频），保留最终输出。KEEP_MEDIA=true 时跳过。"""
    if settings.keep_media:
        return
    media_exts = {".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus", ".aac", ".webm", ".mp4", ".mkv", ".flv"}
    try:
        for p in task_dir.iterdir():
            if p.suffix.lower() in media_exts:
                p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.warning("清理中间产物失败: %s", task_dir)


def cleanup_old_data(days: int) -> tuple[int, int]:
    """删除超过 days 天的任务目录与 DB 记录；返回 (删目录数, 删记录数)。

    - days <= 0 时直接返回 (0, 0)
    - 按任务 updated_at 判断年龄；同时清理转写缓存目录中过期文件
    - DB 记录删除后从内存 _tasks 同步移除
    """
    if days <= 0:
        return (0, 0)
    cutoff_ts = time.time() - days * 86400
    removed_dirs = removed_recs = 0
    for task_id, rec in list(_tasks.items()):
        try:
            ts = datetime.strptime(rec.updated_at, "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, TypeError):
            continue
        if ts >= cutoff_ts:
            continue
        d = settings.data_dir / task_id
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            removed_dirs += 1
        try:
            _store.delete(task_id)
            removed_recs += 1
        except Exception:  # noqa: BLE001
            logger.warning("清理过期任务 DB 记录失败 %s", task_id)
        _tasks.pop(task_id, None)
    # 清理过期转写缓存文件
    cache_dir = _transcript_cache_dir()
    try:
        for p in cache_dir.glob("*.json"):
            if p.stat().st_mtime < cutoff_ts:
                p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.warning("清理过期转写缓存失败")
    if removed_recs:
        logger.info("数据保留清理：删除 %d 个任务目录 / %d 条 DB 记录", removed_dirs, removed_recs)
    return (removed_dirs, removed_recs)