"""语音转文字模块。

优先 faster-whisper（离线、中文效果好、带时间戳）；
faster-whisper 未安装且 ASR_ENGINE=auto 时自动降级为 mock（仅供离线联调）；
ASR_ENGINE=mock 可强制模拟转写，便于跑通整条管道。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings
from .errors import ERR_ASR_FAILED, AppError
from .models import TranscriptSegment

logger = logging.getLogger(__name__)

AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus", ".aac", ".webm"}


def _is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTS


@dataclass
class TranscriptResult:
    segments: list[TranscriptSegment] = field(default_factory=list)
    language: str = ""
    avg_logprob: float = 0.0
    duration: float = 0.0

    @property
    def full_text(self) -> str:
        return "\n".join(s.text for s in self.segments if s.text.strip())

    def to_json(self) -> dict:
        return {
            "language": self.language,
            "avg_logprob": self.avg_logprob,
            "duration": self.duration,
            "segments": [s.model_dump() for s in self.segments],
        }


# 常见可下载模型名（用于提示）
KNOWN_MODELS = {"tiny", "base", "small", "medium", "large-v3", "large"}

# Hub 仓库映射（预热/进度钩子用）
REPO_MAP = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large": "Systran/faster-whisper-large-v3",
}

# 当前权重下载进度回调（_get_model 加载阶段临时设置；加载在 _model_lock 内串行，无并发竞态）
_model_dl_cb = None

# Whisper 模型实例缓存：按 (model_ref, device, compute_type, cpu_threads) 复用，避免每次转写重新加载
_model_cache: dict[tuple, object] = {}
_model_lock = threading.Lock()


def _safe_cb(cb, pct: int, phase: str) -> None:
    """兼容两种回调签名：新 (pct, phase) / 旧 (pct)。"""
    if not cb:
        return
    try:
        cb(pct, phase)
    except TypeError:
        cb(pct)


def _progress_tqdm(*args, **kwargs):
    """hf_hub 进度条代理：禁用原生 tqdm，转成 on_progress(percent) 回调。"""
    total = kwargs.get("total") or 0

    class _Cb:
        def __init__(self) -> None:
            self.n = 0

        def update(self, n: int = 1) -> None:
            self.n += n
            cb = _model_dl_cb
            if cb and total:
                _safe_cb(cb, min(99, int(self.n / total * 100)), "model")

        def close(self) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Cb()


def ensure_model(model_ref: str | None = None) -> bool:
    """预检/预热 ASR 模型：本地目录直接可用；Hub 名则触发下载缓存（启动后台调用）。

    返回 True 表示模型已就绪或名称无法解析（交给 faster-whisper），False 表示预热失败。
    """
    ref = (model_ref or settings.asr_model).strip()
    if Path(ref).is_dir():
        return True
    repo = REPO_MAP.get(ref)
    if not repo:
        return True
    try:
        from huggingface_hub import snapshot_download  # noqa: PLC0415

        logger.info("ASR 模型预热开始：%s（若未缓存将自动下载）", ref)
        snapshot_download(repo_id=repo)
        logger.info("ASR 模型预热完成：%s", ref)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("ASR 模型预热失败 %s：%s（任务提交时将重试下载）", ref, exc)
        return False


def transcribe(audio_path: Path, progress_cb=None, model: str | None = None) -> TranscriptResult:
    """转写音频为带时间戳的文本。progress_cb: (percent: int) -> None。

    model 非空时覆盖 settings.asr_model（如前端「快速预览」档传 tiny/base）。
    """
    audio_path = Path(audio_path)
    if not audio_path.exists() or not _is_audio(audio_path):
        raise AppError(ERR_ASR_FAILED, f"音频文件无效：{audio_path.name}")

    engine = settings.asr_engine
    if engine == "mock":
        return _mock_transcribe(audio_path, progress_cb)

    try:
        from faster_whisper import WhisperModel  # 延迟导入，降低启动成本
    except ImportError:
        if engine == "whisper":
            raise AppError(
                ERR_ASR_FAILED, "faster-whisper 未安装，请执行：pip install faster-whisper"
            )
        logger.warning("faster-whisper 未安装，切换为 mock 转写（仅供联调，生产请安装）。")
        return _mock_transcribe(audio_path, progress_cb)

    return _whisper_transcribe(audio_path, progress_cb, WhisperModel, model)


def _get_model(model_ref: str, device: str, WhisperModel, progress_cb=None):
    """按 (模型,设备,计算类型,CPU线程) 缓存已加载的 WhisperModel，跨任务复用，避免每次转写重新加载。

    加载在 _model_lock 内完成：同一模型并发任务只加载一次（第二个等第一个加载完直接复用），
    顺带消除原 _model_dl_cb 全局变量在并发下的竞态。返回 (model, hit_cache)。
    """
    key = (str(model_ref), device, settings.asr_compute_type, settings.cpu_threads if device == "cpu" else 0)
    with _model_lock:
        cached = _model_cache.get(key)
        if cached is not None:
            _safe_cb(progress_cb, 100, "model")
            return cached, True

        global _model_dl_cb
        _model_dl_cb = progress_cb
        try:
            try:
                from huggingface_hub import progress as _hf_progress  # noqa: PLC0415

                _hf_progress.tqdm = _progress_tqdm
            except Exception:  # noqa: BLE001
                logger.debug("hf 进度钩子安装失败，忽略")
            _safe_cb(progress_cb, 1, "model")  # 通知进入模型加载阶段
            load_kwargs = dict(device=device, compute_type=settings.asr_compute_type)
            if settings.cpu_threads > 0 and device == "cpu":
                load_kwargs["cpu_threads"] = settings.cpu_threads
            model = WhisperModel(model_ref, **load_kwargs)
            _model_cache[key] = model
            return model, False
        finally:
            _model_dl_cb = None


def _whisper_transcribe(audio_path: Path, progress_cb, WhisperModel, model_override: str | None = None) -> TranscriptResult:
    device = settings.asr_device
    if device == "auto":
        try:
            import torch  # noqa: F401, PLC0415

            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"

    model_ref = model_override or settings.asr_model
    # 支持本地模型目录：ASR_MODEL=data/models/whisper-small（已用 snapshot_download 预下载）
    if Path(model_ref).is_dir():
        model_ref = str(Path(model_ref).resolve())
        logger.info("使用本地模型目录: %s", model_ref)
    t0 = time.time()
    try:
        model, hit = _get_model(model_ref, device, WhisperModel, progress_cb)
    except Exception as exc:  # noqa: BLE001
        hint = ""
        msg_l = str(exc).lower()
        if any(k in msg_l for k in ("timeout", "connection", "hub", "network")):
            hint = "。若无法访问 HuggingFace，请设置环境变量 HF_ENDPOINT=https://hf-mirror.com 后重试"
        raise AppError(ERR_ASR_FAILED, f"Whisper 模型加载失败:{exc}{hint}", retryable=True) from exc
    if hit:
        logger.info("复用已缓存 Whisper 模型（跳过加载）")
    else:
        logger.info("模型加载耗时 %.1fs", time.time() - t0)

    try:
        seg_iter, info = model.transcribe(
            str(audio_path),
            language=settings.asr_language,
            vad_filter=settings.asr_vad_filter,
            beam_size=5,
            condition_on_previous_text=settings.asr_context_text,
        )
    except Exception as exc:  # noqa: BLE001
        raise AppError(ERR_ASR_FAILED, f"转写失败：{exc}", retryable=True) from exc

    segments: list[TranscriptSegment] = []
    total = float(getattr(info, "duration", 0) or 0)
    try:
        for idx, seg in enumerate(seg_iter):
            text = (seg.text or "").strip()
            if text:
                segments.append(
                    TranscriptSegment(start=round(seg.start, 2), end=round(seg.end, 2), text=text)
                )
            if progress_cb and total > 0 and idx % 10 == 0:
                _safe_cb(progress_cb, min(100, int(seg.end / total * 100)), "transcribe")
    except Exception as exc:  # noqa: BLE001
        raise AppError(ERR_ASR_FAILED, f"转写中断：{exc}", retryable=True) from exc

    # VAD 空结果自动回退：部分模型版本 vad_filter=true 会返回空段（实测 large-v3 bug）
    if not segments and settings.asr_vad_filter and settings.asr_vad_fallback:
        logger.warning("VAD 转写返回空，自动回退无 VAD 重跑：%s", audio_path.name)
        return _transcribe_no_vad(model, audio_path, progress_cb)

    if not segments:
        raise AppError(ERR_ASR_FAILED, "未检测到人声内容（视频可能为纯音乐/无人声），请更换视频后重试")

    return TranscriptResult(
        segments=segments,
        language=getattr(info, "language", "") or "",
        avg_logprob=float(getattr(info, "avg_logprob", 0.0) or 0.0),
        duration=total,
    )


def _transcribe_no_vad(model, audio_path: Path, progress_cb) -> TranscriptResult:
    """无 VAD 兜底转写（VAD 空结果回退路径）。"""
    seg_iter, info = model.transcribe(
        str(audio_path),
        language=settings.asr_language,
        beam_size=5,
        condition_on_previous_text=settings.asr_context_text,
    )
    segments: list[TranscriptSegment] = []
    total = float(getattr(info, "duration", 0) or 0)
    for idx, seg in enumerate(seg_iter):
        text = (seg.text or "").strip()
        if text:
            segments.append(TranscriptSegment(start=round(seg.start, 2), end=round(seg.end, 2), text=text))
        if progress_cb and total > 0 and idx % 10 == 0:
            _safe_cb(progress_cb, min(100, int(seg.end / total * 100)), "transcribe")
    if not segments:
        raise AppError(ERR_ASR_FAILED, "未检测到人声内容（视频可能为纯音乐/无人声），请更换视频后重试")
    return TranscriptResult(
        segments=segments,
        language=getattr(info, "language", "") or "",
        avg_logprob=float(getattr(info, "avg_logprob", 0.0) or 0.0),
        duration=total,
    )


def _mock_transcribe(audio_path: Path, progress_cb=None) -> TranscriptResult:
    """离线联调用：按音频时长生成占位文本，便于无模型环境跑通管道。"""
    import av  # noqa: PLC0415

    duration = 60.0
    try:
        container = av.open(str(audio_path))
        if container.duration:
            duration = float(container.duration) / av.time_base
        container.close()
    except Exception:  # noqa: BLE001
        logger.warning("mock 转写无法读取音频时长，使用默认 60s")
    if duration <= 0:
        duration = 60.0

    sentences = [
        "大家好，欢迎收看本期视频。",
        "今天我们主要讨论一个核心问题。",
        "首先，需要明确的是，这个问题的背景非常重要。",
        "其次，我们来看几个关键的数据和案例。",
        "总结来说，核心结论有三点。",
        "最后，给出一些可落地的行动建议。",
    ]
    seg_len = max(2.0, duration / max(1, len(sentences)))
    segments = []
    for i, s in enumerate(sentences):
        segments.append(
            TranscriptSegment(start=round(i * seg_len, 2), end=round((i + 1) * seg_len, 2), text=s)
        )
        _safe_cb(progress_cb, min(100, int((i + 1) / len(sentences) * 100)), "transcribe")
    return TranscriptResult(segments=segments, language="zh", avg_logprob=-0.5, duration=duration)


def write_srt(segments: list[TranscriptSegment], path: Path) -> None:
    """写出标准 SRT 字幕文件。"""

    def _fmt(sec: float) -> str:
        ms = int(round(sec * 1000))
        h, rem = divmod(ms, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines: list[str] = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{_fmt(seg.start)} --> {_fmt(seg.end)}")
        lines.append(seg.text)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("SRT 已写入 %s", path)