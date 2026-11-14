"""FastAPI 入口：REST API + 静态前端。

启动：uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .errors import AppError
from . import downloader
from .models import DEPTH_LABELS, ExtractRequest, TaskStatus, expand_pages_spec, parse_pages
from .pipeline import (
    cancel_task,
    cleanup_old_data,
    get_task,
    list_tasks,
    run_task,
    submit_or_reuse,
)
from .transcriber import ensure_model
from .bridge_runner import bridge_loop

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("app")


async def _daily_cleanup() -> None:
    """后台定时清理过期任务数据（每 24h 执行一次；启动时先跑一遍）。"""
    await asyncio.sleep(2)  # 启动后稍等，避免与恢复逻辑竞争
    while True:
        try:
            cleanup_old_data(settings.data_retention_days)
        except Exception:  # noqa: BLE001
            logger.exception("定时清理异常")
        await asyncio.sleep(86400)


async def _prewarm_asr() -> None:
    """启动预热 ASR 模型：本地目录秒检；Hub 名后台触发下载，避免首个任务卡几分钟。"""
    if settings.asr_engine == "mock":
        return
    try:
        ok = await asyncio.to_thread(ensure_model)
        logger.info("ASR 模型预检%s：%s", "完成" if ok else "失败（提交任务时将重试）", settings.asr_model)
    except Exception:  # noqa: BLE001
        logger.warning("ASR 模型预检异常（不影响服务，提交任务时将重试）", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.data_retention_days > 0:
        asyncio.create_task(_daily_cleanup())
    if settings.asr_prewarm:
        asyncio.create_task(_prewarm_asr())
    # 内置桥心跳：取代外部计划任务，让「提取器 + 桥」自洽成一个进程、一个启动入口
    if settings.bridge_enabled:
        _tiers = {t.strip() for t in settings.bridge_tiers.split(",") if t.strip()}
        if _tiers:
            asyncio.create_task(bridge_loop(settings.bridge_interval, _tiers))
    yield


app = FastAPI(title="视频内容提取与智能整理", version="1.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
settings.data_dir.mkdir(parents=True, exist_ok=True)


def _http_err(exc: AppError) -> HTTPException:
    if exc.code in ("ERR_NOT_FOUND",):
        return HTTPException(status_code=404, detail=exc.to_dict())
    if exc.code == "ERR_INTERNAL":
        return HTTPException(status_code=500, detail=exc.to_dict())
    return HTTPException(status_code=400, detail=exc.to_dict())


# ---- 鉴权：API_TOKEN 非空时，写操作（POST）需带 X-API-Key 头 ----
_WRITE_METHODS = {"POST", "PUT", "DELETE"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if settings.api_token and request.method in _WRITE_METHODS and request.url.path.startswith("/api/"):
        token = request.headers.get("X-API-Key", "")
        if token != settings.api_token:
            return JSONResponse(
                status_code=401,
                content={"code": "ERR_UNAUTHORIZED", "message": "API Token 无效或缺失（需在 X-API-Key 头传入）"},
            )
    return await call_next(request)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


async def _batch_extract(req: ExtractRequest, page_list: list[int]) -> dict:
    """分 P 批量：逐集走 submit_or_reuse（同链接不同页码互不冲突），返回多任务结果。

    单页请求（解析后仅 1 个页码）退化为普通单任务流程，避免对单P视频误加 ?p=N。
    """
    if len(page_list) == 1:
        single = req.model_copy(update={"page": page_list[0], "pages": ""})
        try:
            rec, reused = submit_or_reuse(single)
        except AppError as exc:
            raise _http_err(exc)
        if not reused:
            asyncio.create_task(run_task(rec.task_id, single))
        return {
            "task_id": rec.task_id,
            "status": rec.status.value,
            "cached": rec.status == TaskStatus.DONE,
            "depth": rec.depth,
            "depth_label": DEPTH_LABELS.get(rec.depth, rec.depth),
            "asr_model": rec.asr_model,
            "output_lang": rec.output_lang,
            "page": rec.page,
        }

    items = []
    for pg in page_list:
        sub = req.model_copy(update={"page": pg, "pages": ""})
        try:
            rec, reused = submit_or_reuse(sub)
        except AppError as exc:
            raise _http_err(exc)
        if not reused:
            asyncio.create_task(run_task(rec.task_id, sub))
        items.append({
            "page": pg,
            "task_id": rec.task_id,
            "status": rec.status.value,
            "cached": rec.status == TaskStatus.DONE,
        })
    return {"batch": True, "count": len(items), "items": items}


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "llm_configured": settings.llm_configured,
        "asr_model": settings.asr_model,
        "asr_engine": settings.asr_engine,
        "transcript_cache": settings.transcript_cache,
        "data_retention_days": settings.data_retention_days,
        "bridge_enabled": settings.bridge_enabled,
        "bridge_interval": settings.bridge_interval,
        "bridge_tiers": settings.bridge_tiers,
        "version": "1.2.0",
    }


@app.post("/api/extract")
async def extract(req: ExtractRequest):
    # 分 P 批量：pages 非空 → 解析范围语法，先探测实际选集数裁剪非法页码，再逐集建任务
    if req.pages.strip():
        try:
            parse_pages(req.pages)  # 语法先行校验（非法 400）
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": "ERR_BAD_PARAM", "message": str(exc)})
        total = None
        try:
            eps = await asyncio.to_thread(downloader.fetch_pages, downloader.validate_url(req.url))
            total = len(eps) if eps else 1
        except AppError:
            pass  # 探测失败不阻断，按原样提交由下载层报错兜底
        if total is not None:
            pages_str, single = expand_pages_spec(req.pages, req.page, total)
            if not pages_str and single:
                req = req.model_copy(update={"page": single[0], "pages": ""})
            else:
                req = req.model_copy(update={"pages": pages_str})
            if not req.pages.strip():
                return await _single_extract(req)
            return await _batch_extract(req, parse_pages(req.pages))

    return await _single_extract(req)


async def _single_extract(req: ExtractRequest) -> dict:
    """单任务路径（含退化后的单页批量）。"""
    try:
        rec, reused = submit_or_reuse(req)
    except AppError as exc:
        raise _http_err(exc)  # 链接格式非法 → 400
    if reused:
        # 命中缓存：进行中任务直接接入，已完成任务直接复用结果（省去重复下载/转写/LLM）
        return {
            "task_id": rec.task_id,
            "status": rec.status.value,
            "cached": rec.status == TaskStatus.DONE,
            "depth": rec.depth,
            "depth_label": DEPTH_LABELS.get(rec.depth, rec.depth),
            "asr_model": rec.asr_model,
            "output_lang": rec.output_lang,
            "page": rec.page,
        }
    asyncio.create_task(run_task(rec.task_id, req))
    return {
        "task_id": rec.task_id,
        "status": rec.status.value,
        "cached": False,
        "depth": rec.depth,
        "depth_label": DEPTH_LABELS.get(rec.depth, rec.depth),
        "asr_model": rec.asr_model,
        "output_lang": rec.output_lang,
        "page": rec.page,
    }


@app.get("/api/tasks")
def tasks(page: int = 1, page_size: int = 20, status: str = ""):
    items, total = list_tasks(page, page_size, status=status.strip() or None)
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "status": status.strip() or None,
        "items": [t.model_dump() for t in items],
    }


@app.get("/api/pages")
async def pages(url: str):
    """探测多 P 视频选集列表（分 P 批量用）。单 P 视频返回空列表。"""
    from . import downloader  # noqa: PLC0415

    try:
        norm = downloader.validate_url(url)
    except AppError as exc:
        raise _http_err(exc)
    try:
        items = await asyncio.to_thread(downloader.fetch_pages, norm)
    except AppError as exc:
        raise _http_err(exc)
    return {"url": norm, "pages": items}


@app.get("/api/tasks/{task_id}/export")
def task_export(task_id: str, fmt: str = "docx"):
    """导出任务结果为 docx / pdf，返回文件下载。"""
    from fastapi.responses import FileResponse  # noqa: PLC0415

    from . import exporter  # noqa: PLC0415

    fmt = (fmt or "").lower().strip()
    if fmt not in ("docx", "pdf"):
        raise HTTPException(status_code=400, detail={"code": "ERR_BAD_PARAM", "message": "fmt 仅支持 docx / pdf"})
    try:
        rec = get_task(task_id)
    except AppError as exc:
        raise _http_err(exc)
    if rec.status != TaskStatus.DONE or not rec.markdown:
        raise HTTPException(
            status_code=409,
            detail={"code": "ERR_NOT_READY", "message": "任务尚未完成，无法导出"},
        )
    try:
        path = exporter.export(task_id, rec.markdown, fmt, settings.data_dir)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail={"code": "ERR_INTERNAL", "message": str(exc)[:300]})
    title = (rec.video.title if rec.video else "") or task_id
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)[:60] or task_id
    filename = f"视频精华_{safe}.{path.suffix.lstrip('.')}"
    media_map = {
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pdf": "application/pdf",
        ".md": "text/markdown; charset=utf-8",
    }
    return FileResponse(str(path), media_type=media_map.get(path.suffix, "application/octet-stream"), filename=filename)


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str):
    try:
        return get_task(task_id).model_dump()
    except AppError as exc:
        raise _http_err(exc)


@app.get("/api/tasks/{task_id}/result")
def task_result(task_id: str):
    try:
        rec = get_task(task_id)
    except AppError as exc:
        raise _http_err(exc)
    if rec.status != TaskStatus.DONE:
        raise HTTPException(
            status_code=409,
            detail={"code": "ERR_NOT_READY", "message": "任务尚未完成", "status": rec.status.value},
        )
    return {
        "task_id": task_id,
        "markdown": rec.markdown,
        "summary": rec.summary.model_dump() if rec.summary else None,
        "video": rec.video.model_dump() if rec.video else None,
        "stats": rec.stats,
    }


@app.get("/api/tasks/{task_id}/transcript")
def task_transcript(task_id: str, as_srt: bool = False):
    try:
        rec = get_task(task_id)
    except AppError as exc:
        raise _http_err(exc)
    if rec.status != TaskStatus.DONE:
        raise HTTPException(
            status_code=409, detail={"code": "ERR_NOT_READY", "message": "任务尚未完成"}
        )
    task_dir = settings.data_dir / task_id
    if as_srt:
        srt = task_dir / "transcript.srt"
        if srt.exists():
            return PlainTextResponse(srt.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")
        raise HTTPException(status_code=404, detail={"code": "ERR_NOT_FOUND", "message": "字幕文件不存在"})
    return FileResponse(task_dir / "transcript.json", media_type="application/json")


@app.post("/api/tasks/{task_id}/cancel")
def task_cancel(task_id: str):
    try:
        return cancel_task(task_id).model_dump()
    except AppError as exc:
        raise _http_err(exc)


# 静态资源（mount 在路由之后，避免覆盖 /api）
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")