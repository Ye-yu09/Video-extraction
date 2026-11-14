"""应用配置：从 .env 与默认值加载。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    # 用项目内绝对路径加载 .env，避免依赖 cwd / 调用栈导致读不到（原 load_dotenv() 在本机从未生效）
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # dotenv 未安装时静默跳过
    pass

BASE_DIR = Path(__file__).resolve().parent.parent


def _ensure_hf_endpoint() -> None:
    """国内环境自动兜底：用户未显式配置 HF_ENDPOINT 时默认走 hf-mirror.com。

    避免首次用 large-v3 等 Hub 模型时直连 huggingface.co 超时（WinError 10060）。
    已设置则尊重用户值；Docker/他人机器同样受益，无需手动 setx。
    """
    if not os.getenv("HF_ENDPOINT", "").strip():
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"


_ensure_hf_endpoint()


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _asr_language() -> str | None:
    """ASR 语言：zh/en/... 或 none/auto/空 → 自动检测。

    注意：Windows 下 PowerShell 可能传不进空字符串，因此显式支持 'none'/'auto'。
    """
    v = os.getenv("ASR_LANGUAGE", "zh").strip().lower()
    if v in ("", "none", "auto", "null"):
        return None
    return v


@dataclass
class Settings:
    # --- LLM（OpenAI 兼容协议）---
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", "").strip())
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").strip())
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "deepseek-chat").strip())
    llm_timeout: float = float(os.getenv("LLM_TIMEOUT", "120"))
    llm_max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "2"))
    llm_concurrency: int = int(os.getenv("LLM_CONCURRENCY", "5"))
    # 自定义提示词模板（可选，空则用内置默认模板），占位符见 app/summarizer.py 文档
    llm_map_prompt: str = field(default_factory=lambda: os.getenv("LLM_MAP_PROMPT", "").strip())
    llm_reduce_prompt: str = field(default_factory=lambda: os.getenv("LLM_REDUCE_PROMPT", "").strip())
    # 转写纠错（LLM 二次校对 ASR 错别字/同音误识别）
    llm_correct: bool = _env_bool("LLM_CORRECT", True)
    correct_batch_size: int = int(os.getenv("CORRECT_BATCH_SIZE", "20"))
    correct_max_segments: int = int(os.getenv("CORRECT_MAX_SEGMENTS", "500"))
    # 纠错增强档：开启后在纠错时一并优化断句/标点（opt-in，默认只修错字以保引用对齐）
    correct_punctuation: bool = _env_bool("CORRECT_PUNCTUATION", False)

    # --- ASR ---
    asr_model: str = field(default_factory=lambda: os.getenv("ASR_MODEL", "large-v3").strip())
    asr_device: str = field(default_factory=lambda: os.getenv("ASR_DEVICE", "auto").strip())
    asr_compute_type: str = field(default_factory=lambda: os.getenv("ASR_COMPUTE_TYPE", "int8").strip())
    asr_language: str | None = field(default_factory=_asr_language)
    # VAD 过滤静音/纯音乐段；歌曲/纯音乐视频识别不出内容时请设为 false
    asr_vad_filter: bool = _env_bool("ASR_VAD_FILTER", True)
    # VAD 返回空结果时自动回退无 VAD 重跑一次（修 large-v3+vad 空结果 bug）
    asr_vad_fallback: bool = _env_bool("ASR_VAD_FALLBACK", True)
    # faster-whisper CPU 推理线程数：0=自动（全部核）。实测本版本并行 transcribe 会互抢算力，
    # 故不做分段多线程；提速靠调大此值让单段吃满 CPU
    cpu_threads: int = int(os.getenv("ASR_CPU_THREADS", "0"))
    # 上下文条件转写：true=每段带前文（逐字精度略高、慢一倍）；false=实测同音频快 2 倍
    # （341s→172s），长句偶发少字但要点/时间戳校准不受影响。默认 false 吃提速
    asr_context_text: bool = _env_bool("ASR_CONTEXT_TEXT", False)
    # auto=优先 faster-whisper，缺失自动切 mock；whisper=强制；mock=强制模拟（离线联调）
    asr_engine: str = field(default_factory=lambda: os.getenv("ASR_ENGINE", "auto").strip())
    # 单任务转写超时（秒）：CPU 跑大模型长视频防无限等待，0=不限；超时给出换档建议
    asr_timeout: int = int(os.getenv("ASR_TIMEOUT", "1800"))
    # 启动时后台预热 ASR 模型（加载/下载权重），避免首个任务卡在「语音转文字」几分钟
    asr_prewarm: bool = _env_bool("ASR_PREWARM", True)

    # --- 任务限制 ---
    max_video_duration: int = int(os.getenv("MAX_VIDEO_DURATION", "10800"))  # 秒，0=不限
    max_chunks: int = int(os.getenv("MAX_CHUNKS", "50"))
    chunk_tokens: int = int(os.getenv("CHUNK_TOKENS", "3000"))
    overlap_tokens: int = int(os.getenv("OVERLAP_TOKENS", "200"))
    max_concurrent_asr: int = int(os.getenv("MAX_CONCURRENT_ASR", "2"))
    # 全局 LLM 并发上限：限制同时进入整理/纠错阶段的任务数（防多任务并发打爆 API 与费用）
    max_concurrent_llm_tasks: int = int(os.getenv("MAX_CONCURRENT_LLM_TASKS", "2"))

    # --- 转写级缓存（省钱主力：换深度/用途不再重复下载+转写）---
    # 缓存键 = BV + 实际 ASR 模型 + 语言 + VAD + 纠错开关，改任一配置自动失效
    transcript_cache: bool = _env_bool("TRANSCRIPT_CACHE", True)
    # 视频元信息缓存 TTL（秒，0=关闭；避免换深度重提时代替一次 yt-dlp 网络请求）
    meta_cache_ttl: int = int(os.getenv("META_CACHE_TTL", "86400"))
    # 数据保留天数：0=不清理；>0 时启动及每日清理超过该天数的任务目录与 DB 记录
    data_retention_days: int = int(os.getenv("DATA_RETENTION_DAYS", "0"))

    # --- LLM 输出 ---
    # 单次 LLM 响应 max_tokens 上限（0=不限，防 full 档长输出被截断时可调大/限制成本）
    llm_max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "0"))
    # 摘要自检：Reduce 输出后 LLM 再评审一遍，发现遗漏关键数据则补充（+1 次调用/任务）
    # off=关闭 / full=仅 full 档启用 / always=所有档启用
    summary_self_check: str = field(default_factory=lambda: os.getenv("SUMMARY_SELF_CHECK", "off").strip().lower())

    # --- 鉴权 ---
    # API Token：非空时 /api/extract 等写操作需带 X-API-Key 头匹配；空=不鉴权（本地开发）
    api_token: str = field(default_factory=lambda: os.getenv("API_TOKEN", "").strip())

    # --- 下载 ---
    # yt-dlp cookies 文件路径（大会员/需登录视频，如 yt-dlp --cookies-from-browser 导出的 cookies.txt）
    ytdlp_cookies: str = field(default_factory=lambda: os.getenv("YTDLP_COOKIES", "").strip())

    # --- 内置桥（bili-scroll → 提取器 进程内心跳；档位②整合，取代外部 Windows 计划任务）---
    # 桥脚本本体在 bridge/bili_bridge.py；这里只配置「服务进程内自动跑心跳」的开关与节奏。
    # 仅在 Windows 且 ~/.dsh/bili-duty 有日报时才有实际转发；Linux/Docker 下扫不到东西但不报错，可按需关。
    bridge_enabled: bool = _env_bool("BRIDGE_ENABLED", False)  # 默认关（开源友好）；本机 .env 显式设 true 开启
    bridge_interval: int = int(os.getenv("BRIDGE_INTERVAL", "600"))  # 心跳间隔秒（默认 10 分钟）
    bridge_tiers: str = field(default_factory=lambda: os.getenv("BRIDGE_TIERS", "值得看").strip())

    # --- 存储/清理 ---
    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("DATA_DIR", "")).resolve()
        if os.getenv("DATA_DIR", "").strip()
        else BASE_DIR / "data"
    )
    keep_media: bool = _env_bool("KEEP_MEDIA", False)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)


settings = Settings()