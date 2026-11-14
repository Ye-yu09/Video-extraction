"""视频下载模块：yt-dlp 封装。

B 站 bestaudio 通常为 m4a 直链，无需 FFmpeg 即可进入 ASR；
若下载失败且系统存在 FFmpeg，则降级为「下载完整视频 + FFmpeg 提取音频」。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import yt_dlp

from .config import settings
from .errors import (
    ERR_DOWNLOAD_FAILED,
    ERR_URL_INVALID,
    ERR_VIDEO_UNAVAILABLE,
    AppError,
)
from .models import VideoMeta

logger = logging.getLogger(__name__)

# 元信息内存缓存（按规范化 URL）：(时间戳, meta)，TTL 由 settings.meta_cache_ttl 控制
_meta_cache: dict[str, tuple[float, VideoMeta]] = {}
_meta_cache_lock = threading.Lock()

BV_RE = re.compile(r"bilibili\.com/video/(BV[0-9A-Za-z]{10})", re.IGNORECASE)

_YD_ERR_MAP = (
    ("private video", "该视频为私享/仅登录可见，无法公开下载"),
    ("video unavailable", "视频不存在或已被删除"),
    ("geo-restricted", "该视频受地区限制，无法从当前网络访问"),
    ("sign in to confirm", "该视频需要登录后才能观看"),
    ("login required", "该视频需要登录后才能观看"),
    ("http error 412", "触发风控（HTTP 412），请稍后重试或配置 Cookies"),
    ("http error 403", "访问被拒绝（HTTP 403），请稍后重试或配置 Cookies"),
)


def validate_url(url: str) -> str:
    """校验并规范化 B 站视频链接，返回规范化 URL。"""
    url = (url or "").strip()
    if not url:
        raise AppError(ERR_URL_INVALID, "链接不能为空")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    m = BV_RE.search(url)
    if not m:
        raise AppError(
            ERR_URL_INVALID,
            "请提供有效的 B 站视频链接，格式：https://www.bilibili.com/video/BVxxxxxx",
        )
    return f"https://www.bilibili.com/video/{m.group(1)}"


def _map_yd_error(exc: Exception) -> AppError:
    msg = str(exc)
    for key, hint in _YD_ERR_MAP:
        if key in msg.lower():
            return AppError(ERR_VIDEO_UNAVAILABLE, hint, retryable=("412" in msg or "403" in msg))
    return AppError(ERR_DOWNLOAD_FAILED, f"下载失败：{msg[:200]}", retryable=True)


def _base_opts(**kw) -> dict:
    """yt-dlp 公共参数：稳健的网络重试与超时；透传 cookies 配置。"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "retries": 5,
        "fragment_retries": 5,
        "retry_sleep_functions": {"http": lambda n: 2 * n, "fragment": lambda n: 2 * n},
    }
    if settings.ytdlp_cookies:
        opts["cookiefile"] = settings.ytdlp_cookies
    opts.update(kw)
    return opts


def _page_url(url: str, page: int = 0) -> str:
    """拼接分 P 参数：page>0 时在 URL 末尾加 ?p=N（B 站标准格式）。"""
    if page and page > 0:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}p={page}"
    return url


def fetch_meta(url: str, page: int = 0) -> VideoMeta:
    """获取视频元信息（不下载）。带内存缓存（TTL 由 META_CACHE_TTL 控制，0=关闭）。"""
    full_url = _page_url(url, page)
    # 缓存命中检查
    if settings.meta_cache_ttl > 0:
        with _meta_cache_lock:
            entry = _meta_cache.get(full_url)
            if entry and (time.time() - entry[0]) < settings.meta_cache_ttl:
                return entry[1]

    opts = _base_opts(skip_download=True)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(full_url, download=False)
    except Exception as exc:  # noqa: BLE001
        raise _map_yd_error(exc) from exc
    if not info:
        raise AppError(ERR_VIDEO_UNAVAILABLE, "未能获取视频信息")
    meta = VideoMeta(
        url=url,
        title=(info.get("title") or "").strip(),
        uploader=(info.get("uploader") or info.get("artist") or "").strip(),
        duration=int(info.get("duration") or 0),
        webpage_url=info.get("webpage_url") or full_url,
        id=str(info.get("id") or ""),
        page=page,
    )
    if settings.meta_cache_ttl > 0:
        with _meta_cache_lock:
            _meta_cache[full_url] = (time.time(), meta)
    return meta


def fetch_pages(url: str) -> list[dict]:
    """探测多 P 视频的选集列表。

    返回 [{"page": 1, "title": ..., "duration": ...}, ...]；单 P 视频返回空列表。
    """
    norm = validate_url(url)
    opts = _base_opts(skip_download=True)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"{norm}?p=1", download=False)
    except Exception as exc:  # noqa: BLE001
        raise _map_yd_error(exc) from exc
    entries = (info or {}).get("entries")
    if isinstance(entries, (list, tuple)):
        out = []
        for i, e in enumerate(entries):
            if not isinstance(e, dict):
                continue
            out.append({
                "page": i + 1,
                "title": (e.get("title") or f"第 {i + 1} 集").strip(),
                "duration": int(e.get("duration") or 0),
            })
        if len(out) > 1:
            return out
    return []


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def download_audio(url: str, task_dir: Path, page: int = 0) -> Path:
    """下载最佳音频流。策略见模块 docstring；内置 2 次重试（退避 2s/4s）。page>0 时下载指定分 P。"""
    full_url = _page_url(url, page)
    task_dir.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None

    for attempt in range(1, 4):
        try:
            path = _try_direct_audio(full_url, task_dir)
            if path and path.stat().st_size > 1024:
                return path
            raise AppError(ERR_DOWNLOAD_FAILED, "音频文件为空或过小")
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < 3:
                time.sleep(2 * attempt)

    # 兜底：有 FFmpeg 时下载完整视频再提取
    if _ffmpeg_available():
        try:
            return _download_video_then_extract(full_url, task_dir)
        except Exception as exc:  # noqa: BLE001
            last_err = exc

    detail = str(last_err)[:200] if last_err else "未知错误"
    raise AppError(
        ERR_DOWNLOAD_FAILED,
        f"音频下载失败：{detail}（安装 FFmpeg 可提高成功率）",
        retryable=True,
    )


def _try_direct_audio(url: str, task_dir: Path) -> Path | None:
    outtmpl = str(task_dir / "audio.%(ext)s")
    opts = _base_opts(
        format="bestaudio/best",
        outtmpl=outtmpl,
        max_filesize=1024 * 1024 * 1024,  # 1GB 上限
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    hits = sorted(task_dir.glob("audio.*"), key=lambda p: p.stat().st_size, reverse=True)
    return hits[0] if hits else None


def _download_video_then_extract(url: str, task_dir: Path) -> Path:
    outtmpl = str(task_dir / "video.%(ext)s")
    opts = _base_opts(format="best[filesize<1G]/best", outtmpl=outtmpl)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    vids = sorted(task_dir.glob("video.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not vids:
        raise AppError(ERR_DOWNLOAD_FAILED, "视频下载成功但未找到文件")
    src = vids[0]
    out = task_dir / "audio_extracted.wav"
    cmd = ["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(out)]
    # Windows：CREATE_NO_WINDOW 防止 ffmpeg 闪出控制台黑框；POSIX 下不传该参数
    _kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    proc = subprocess.run(cmd, capture_output=True, text=True, **_kw)
    if proc.returncode != 0 or not out.exists():
        raise AppError(ERR_DOWNLOAD_FAILED, f"FFmpeg 音频提取失败:{proc.stderr[-300:]}")
    return out