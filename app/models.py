"""Pydantic 数据模型。"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

# 提取深度三档（与 summarizer 模板一一对应）
DEPTHS: tuple[str, ...] = ("essence", "detailed", "full")
DEPTH_LABELS: dict[str, str] = {
    "essence": "精华摘要",
    "detailed": "详细要点",
    "full": "全文整理",
}


class ExtractRequest(BaseModel):
    """用户提交参数。"""

    url: str = Field(..., description="B 站视频链接")
    depth: str = Field("essence", description="essence=精华摘要 | detailed=详细要点 | full=全文整理")
    audience: str = Field("通用受众", description="目标受众")
    purpose: str = Field("快速了解视频核心内容", description="使用用途")
    map_prompt: str = Field("", description="可选：自定义逐块提炼模板（占位符见 summarizer 文档）")
    reduce_prompt: str = Field("", description="可选：自定义全局归纳模板（占位符见 summarizer 文档）")
    asr_model: str = Field("", description="可选：覆盖本次转写模型（tiny/base/small/medium/大模型名或本地目录），空=用服务端 ASR_MODEL 配置")
    output_lang: str = Field("zh", description="整理结果输出语言（zh/en/ja 等，透传给 LLM 提示词）")
    page: int = Field(0, ge=0, description="分 P 页码（0=默认/无分P；>0=指定第 N 个分P）")
    pages: str = Field("", description="分 P 批量：范围语法字符串，如 1-3 / 1,4,7 / 2-5,8（与 page 二选一，pages 优先）")
    correct_punctuation: bool = Field(False, description="本次纠错启用断句+标点增强档（覆盖 CORRECT_PUNCTUATION 配置）")
    summary_self_check: str = Field("", description="摘要自检模式（空=用服务端配置；off/full/always）")

    @field_validator("depth")
    @classmethod
    def _check_depth(cls, v: str) -> str:
        """边界校验：非法深度直接报错，不再静默回退 essence。"""
        if v not in DEPTHS:
            raise ValueError(f"depth 非法：{v!r}，可选值：{' / '.join(DEPTHS)}")
        return v


def parse_pages(spec: str, max_page: int = 200) -> list[int]:
    """解析分 P 范围语法："1-3"→[1,2,3]；"1,4,7"→[1,4,7]；"2-5,8"→[2,3,4,5,8]。

    - 支持 单个页码、a-b 闭区间、逗号混合；自动去重升序
    - 非法片段抛 ValueError；上限 max_page 防误输入超大范围
    """
    out: set[int] = set()
    for part in (spec or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= 0 or b < a:
                raise ValueError(f"分P范围非法：{part!r}（应为 起始-结束 且 ≥1）")
            if b - a + 1 > max_page:
                raise ValueError(f"分P范围过大：{part!r}（单次最多 {max_page} 集）")
            out.update(range(a, b + 1))
            continue
        if re.fullmatch(r"\d+", part):
            n = int(part)
            if n <= 0:
                raise ValueError(f"分P页码非法：{part!r}（应 ≥1）")
            out.add(n)
            continue
        raise ValueError(f"无法识别的分P写法：{part!r}（支持 1-3 / 1,4,7 / 2-5,8）")
    result = sorted(out)
    if len(result) > max_page:
        raise ValueError(f"分P数量超过上限 {max_page}")
    return result


def expand_pages_spec(pages: str, page: int, total_pages: int) -> tuple[str, list[int]]:
    """把范围语法展开为具体页码列表，并按实际选集数裁剪。

    - total_pages<=1（单P视频/探测失败）：所有请求页码退化为主视频 page=0，
      避免对不存在的分 P 加 ?p=N 导致下载报错
    - 超出实际集数的页码自动剔除；结果仅剩 1 页时退化为单任务路径（pages 置空）
    返回 (pages_str, single_page_or_none)。single_page_or_none=[n] 表示应走单任务 n 页（含 0）。
    """
    req_pages = parse_pages(pages)
    if total_pages <= 1:
        return "", [0]
    valid = [p for p in req_pages if p <= total_pages]
    if not valid:
        return "", [0]
    if len(valid) == 1:
        return "", valid
    return ",".join(map(str, valid)), []


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Stage(str, Enum):
    DOWNLOADING = "downloading"
    LOADING_MODEL = "loading_model"
    TRANSCRIBING = "transcribing"
    CORRECTING = "correcting"
    SUMMARIZING = "summarizing"
    FINALIZING = "finalizing"
    NONE = ""


class VideoMeta(BaseModel):
    url: str
    title: str = ""
    uploader: str = ""
    duration: int = 0
    webpage_url: str = ""
    id: str = ""
    page: int = 0  # 分 P 页码（0=无分P/默认）


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str


class Chunk(BaseModel):
    index: int
    start: float
    end: float
    text: str
    timed_text: str = ""  # 带 [mm:ss] 时间戳的逐句文本（供 LLM 定位原文）


class KeyPoint(BaseModel):
    point: str
    time: str = ""
    evidence: str = ""  # 支撑该要点的原文短句（用于后端对齐真实时间戳）


class Topic(BaseModel):
    topic: str
    details: list[str] = Field(default_factory=list)
    time_range: str = ""


class Quote(BaseModel):
    quote: str
    time: str = ""


class ChunkDetail(BaseModel):
    """full 深度档：逐段小结。"""

    time: str = ""  # mm:ss-mm:ss（后端按真实分块回填）
    gist: str = ""


class SummaryResult(BaseModel):
    summary: str = ""
    overview: str = ""  # 视频概要：一段 150-300 字，讲清全片主题/核心论点/结论
    key_points: list[KeyPoint] = Field(default_factory=list)
    topics: list[Topic] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    chunk_details: list[ChunkDetail] = Field(default_factory=list)  # full 档逐段小结
    degraded: bool = False
    degraded_reason: str = ""


class TaskRecord(BaseModel):
    task_id: str
    status: TaskStatus
    stage: Stage = Stage.NONE
    progress: int = 0
    stage_progress: dict[str, Any] = Field(default_factory=dict)
    error: Optional[dict] = None
    video: Optional[VideoMeta] = None
    summary: Optional[SummaryResult] = None
    markdown: str = ""
    transcript_path: str = ""
    stats: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    # 请求参数落库（缓存去重 / 历史展示用；旧记录缺失时取默认值）
    url: str = ""  # 规范化后的链接（任务创建时写入，queued 阶段即可参与去重）
    depth: str = "essence"  # 提取深度
    audience: str = "通用受众"
    purpose: str = "快速了解视频核心内容"
    prompt_hash: str = ""  # 自定义模板哈希（map_prompt+reduce_prompt），空=未用自定义模板
    asr_model: str = ""  # 本次转写模型覆盖（空=服务端 ASR_MODEL 配置）
    output_lang: str = "zh"  # 整理输出语言
    page: int = 0  # 分 P 页码（0=无分P/默认）