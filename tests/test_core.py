"""chunker / downloader / summarizer / pipeline / corrector 单元测试。运行：python -m pytest tests -q"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest


@pytest.fixture
def tmp_path(request):
    """覆盖内置 tmp_path：用工作区内目录（沙箱仅保证 workspace 可写，系统 TMP 可能被拒）。"""
    root = Path(__file__).resolve().parent / "_tmp" / request.node.name
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


from app.chunker import chunk_segments, estimate_tokens
from app.config import settings
from app.corrector import correct_texts
from app.downloader import validate_url
from app.errors import AppError, ERR_URL_INVALID
from app.models import ExtractRequest, KeyPoint, Quote, SummaryResult, TranscriptSegment
from app.summarizer import (
    DEFAULT_MAP_TEMPLATE,
    _align_quote_time,
    align_summary_times,
    render_template,
)


def _segs(texts: list[str], step: float = 5.0) -> list[TranscriptSegment]:
    out = []
    t = 0.0
    for text in texts:
        out.append(TranscriptSegment(start=round(t, 2), end=round(t + step, 2), text=text))
        t += step
    return out


class TestEstimateTokens:
    def test_chinese(self):
        assert estimate_tokens("今天天气很好我们出去散步") > 0
        assert estimate_tokens("") == 0

    def test_mixed(self):
        assert estimate_tokens("hello world 你好世界") > 0


class TestChunkSegments:
    def test_empty(self):
        assert chunk_segments([], max_tokens=100) == []

    def test_single_chunk(self):
        segs = _segs(["第一句话。", "第二句话。"])
        chunks = chunk_segments(segs, max_tokens=100000, overlap_tokens=0)
        assert len(chunks) == 1
        assert chunks[0].text == "第一句话。第二句话。"
        assert chunks[0].start == 0.0

    def test_split_and_budget(self):
        # 每段 10 字 ≈ 7.5 token，max_tokens=20 → 每个 chunk 不超过约 3 段
        segs = _segs([f"第{i}句内容：" + "很" * 8 for i in range(10)])
        chunks = chunk_segments(segs, max_tokens=20, overlap_tokens=0)
        assert len(chunks) >= 2
        for c in chunks:
            assert estimate_tokens(c.text) <= 40  # 留有单段溢出余量

    def test_overlap_present(self):
        segs = _segs([f"段{i}：" + "内容" * 20 for i in range(6)])  # 每段 ~42 字 ≈ 32 token
        chunks = chunk_segments(segs, max_tokens=80, overlap_tokens=50)
        assert len(chunks) >= 2
        # 后一块开头应包含前一块结尾文本（重叠）
        prev_tail = chunks[0].text[-30:]
        assert prev_tail in chunks[1].text

    def test_time_monotonic(self):
        segs = _segs([f"内容内容内容{i}" for i in range(10)])
        chunks = chunk_segments(segs, max_tokens=12, overlap_tokens=0)
        for i in range(1, len(chunks)):
            assert chunks[i].start >= chunks[i - 1].start
            assert chunks[i].index == i


class TestValidateUrl:
    def test_valid(self):
        assert validate_url("https://www.bilibili.com/video/BV1GJ411x7h7") == \
            "https://www.bilibili.com/video/BV1GJ411x7h7"

    def test_no_scheme(self):
        assert validate_url("www.bilibili.com/video/BV1GJ411x7h7") == \
            "https://www.bilibili.com/video/BV1GJ411x7h7"

    def test_invalid(self):
        with pytest.raises(AppError) as ei:
            validate_url("https://example.com/watch?v=1")
        assert ei.value.code == ERR_URL_INVALID

    def test_empty(self):
        with pytest.raises(AppError):
            validate_url("  ")


class TestRenderTemplate:
    def test_placeholder_replace(self):
        out = render_template("你好{Name}，{Index}号", Name="世界", Index="3")
        assert out == "你好世界，3号"

    def test_json_braces_not_touched(self):
        tpl = '只输出 JSON：{"a": 1, "b": "{ChunkText}"}'
        out = render_template(tpl, ChunkText="原文")
        assert '"a": 1' in out
        assert '"b": "{ChunkText}"'.replace("{ChunkText}", "原文") in out

    def test_unknown_placeholder_kept(self):
        out = render_template("{A}{Missing}", A="x")
        assert out == "x{Missing}"

    def test_default_map_template_render(self):
        out = render_template(
            DEFAULT_MAP_TEMPLATE,
            Index="1", StartTime="0", EndTime="10",
            Audience="学生", Purpose="学习", ChunkText="今天讲 Python",
            TimedSegments="[00:00] 今天讲 Python",
        )
        assert "第 1 段" in out
        assert "[00:00] 今天讲 Python" in out
        assert '"core_ideas"' in out
        assert '"quote"' in out


class TestAlignQuoteTime:
    SEGS = [
        TranscriptSegment(start=5.0, end=9.0, text="大家好，欢迎收看本期视频。"),
        TranscriptSegment(start=9.0, end=15.0, text="今天我们讨论人工智能的发展。"),
        TranscriptSegment(start=15.0, end=21.0, text="明年市场规模预计达到三千亿元。"),
    ]

    def test_exact_substring(self):
        assert _align_quote_time("人工智能的发展", self.SEGS) == "00:09"
        assert _align_quote_time("市场规模预计", self.SEGS) == "00:15"

    def test_segment_is_substring_of_quote(self):
        # 引文比段文本更长（段被并入引文）
        assert _align_quote_time("今天我们讨论人工智能的发展。更多内容", self.SEGS) == "00:09"

    def test_prefix_fuzzy(self):
        assert _align_quote_time("大家好，欢迎收看！", self.SEGS) == "00:05"

    def test_unknown_returns_empty(self):
        assert _align_quote_time("完全无关的内容xyz", self.SEGS) == ""

    def test_empty(self):
        assert _align_quote_time("", self.SEGS) == ""

    def test_repeated_lyrics_first_match(self):
        segs = [
            TranscriptSegment(start=10.0, end=15.0, text="Never gonna give you up"),
            TranscriptSegment(start=40.0, end=45.0, text="Never gonna give you up"),
        ]
        assert _align_quote_time("Never gonna give you up", segs) == "00:10"


class TestAlignSummaryTimes:
    def test_fills_kp_and_quotes(self):
        segs = [
            TranscriptSegment(start=0.0, end=5.0, text="第一段内容。"),
            TranscriptSegment(start=30.0, end=35.0, text="这是关键数据段的原话。"),
        ]
        summary = SummaryResult(
            key_points=[KeyPoint(point="概括", evidence="这是关键数据段的原话")],
            quotes=[Quote(quote="第一段内容")],
        )
        align_summary_times(summary, segs)
        assert summary.key_points[0].time == "00:30"
        assert summary.quotes[0].time == "00:00"

    def test_no_segments_noop(self):
        summary = SummaryResult(key_points=[KeyPoint(point="概括", time="01:00")])
        align_summary_times(summary, [])
        assert summary.key_points[0].time == "01:00"


def _fake_correct_llm(system: str, user: str) -> dict:
    """模拟 LLM：把行内明确错字替换后原样返回全部片段。"""
    segments = []
    for line in user.splitlines():
        m = re.match(r"^\[(\d+)\]\s*(.*)$", line)
        if m:
            idx, text = int(m.group(1)), m.group(2)
            fixed = text.replace("油产阶级", "资产阶级").replace("振卫", "捍卫")
            segments.append({"index": idx, "text": fixed})
    assert segments, "fake LLM 未解析到任何片段"
    return {"segments": segments}


class TestCorrectTexts:
    def test_batch_replace(self, monkeypatch):
        from app import summarizer as _sum

        monkeypatch.setattr(settings, "correct_batch_size", 4)
        monkeypatch.setattr(_sum, "_call_llm", _fake_correct_llm)
        texts = [f"这是第{i}句，油产阶级战士振卫人类文明" for i in range(9)]
        result = correct_texts(texts)
        assert result.changed == 9
        assert result.batches == 3  # 9 段 / 4 段每批 → 3 批
        assert all("资产阶级" in t and "捍卫" in t for t in result.texts)
        assert len(result.texts) == len(texts)

    def test_unchanged_texts_not_counted(self, monkeypatch):
        from app import summarizer as _sum

        monkeypatch.setattr(settings, "correct_batch_size", 10)
        monkeypatch.setattr(_sum, "_call_llm", _fake_correct_llm)
        texts = ["句子正常没有错字。", "这也是一句正常的。"]
        result = correct_texts(texts)
        assert result.changed == 0
        assert result.texts == texts

    def test_failure_keeps_original(self, monkeypatch):
        from app import summarizer as _sum

        monkeypatch.setattr(settings, "correct_batch_size", 10)

        def boom(system, user):
            raise RuntimeError("LLM 挂了")

        monkeypatch.setattr(_sum, "_call_llm", boom)
        texts = ["原句一。", "原句二。"]
        result = correct_texts(texts)
        assert result.changed == 0
        assert result.texts == texts  # 降级保原文

    def test_no_llm_key_skips(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_api_key", "")
        texts = ["内容"]
        result = correct_texts(texts)
        assert result.changed == 0
        assert result.texts == texts

    def test_empty_input(self, monkeypatch):
        from app import summarizer as _sum

        monkeypatch.setattr(_sum, "_call_llm", _fake_correct_llm)
        assert correct_texts([]).changed == 0

    def test_max_segments_cap(self, monkeypatch):
        from app import summarizer as _sum

        monkeypatch.setattr(settings, "correct_max_segments", 3)
        monkeypatch.setattr(settings, "correct_batch_size", 10)
        monkeypatch.setattr(_sum, "_call_llm", _fake_correct_llm)
        texts = [f"第{i}句，油产阶级" for i in range(8)]
        result = correct_texts(texts)
        assert len(result.texts) == 3  # 截断到上限
        assert result.changed == 3


class TestListTasksPagination:
    def test_pagination(self, monkeypatch):
        from app import pipeline

        # 不污染真实 DB
        monkeypatch.setattr(pipeline._store, "upsert", lambda rec: None)
        monkeypatch.setattr(pipeline, "_tasks", {})

        reqs = [
            ExtractRequest(url=f"https://www.bilibili.com/video/BV{str(i).zfill(10)}00")
            for i in range(5)
        ]
        for req in reqs:
            pipeline.create_task(req)

        page1, total = pipeline.list_tasks(page=1, page_size=2)
        assert total == 5
        assert len(page1) == 2
        page3, _ = pipeline.list_tasks(page=3, page_size=2)
        assert len(page3) == 1
        # 边界：越界页返回空
        page9, _ = pipeline.list_tasks(page=9, page_size=2)
        assert page9 == []
        # page_size 上限
        big, _ = pipeline.list_tasks(page=1, page_size=999)
        assert len(big) == 5

    def test_status_filter(self, monkeypatch):
        from app import pipeline
        from app.models import TaskRecord, TaskStatus

        monkeypatch.setattr(pipeline._store, "upsert", lambda rec: None)
        monkeypatch.setattr(pipeline, "_tasks", {})
        for i, st in enumerate(["done", "done", "failed", "running", "queued"]):
            rec = TaskRecord(task_id=f"t{i}", status=TaskStatus(st), created_at="t", updated_at=f"t{i}")
            pipeline._tasks[rec.task_id] = rec

        done_items, done_total = pipeline.list_tasks(page=1, page_size=20, status="done")
        assert done_total == 2 and all(r.status.value == "done" for r in done_items)
        _, run_total = pipeline.list_tasks(page=1, page_size=20, status="running")
        assert run_total == 1
        _, all_total = pipeline.list_tasks(page=1, page_size=20, status="")
        assert all_total == 5
        # 非法状态值：忽略筛选，返回全部
        _, bad_total = pipeline.list_tasks(page=1, page_size=20, status="nonsense")
        assert bad_total == 5


class TestNormalizeDepth:
    def test_valid_values(self):
        from app.summarizer import normalize_depth

        assert normalize_depth("essence") == "essence"
        assert normalize_depth("detailed") == "detailed"
        assert normalize_depth("full") == "full"

    def test_invalid_falls_back(self):
        from app.summarizer import normalize_depth

        assert normalize_depth("超长") == "essence"
        assert normalize_depth(None) == "essence"
        assert normalize_depth("") == "essence"


class TestDepthTemplates:
    def test_all_depths_present(self):
        from app.summarizer import DEPTH_TEMPLATES, DEPTHS

        assert set(DEPTH_TEMPLATES) == set(DEPTHS) == {"essence", "detailed", "full"}

    def test_essence_has_no_topics(self):
        from app.summarizer import DEPTH_TEMPLATES

        tpl = DEPTH_TEMPLATES["essence"]
        assert "topics" not in tpl
        assert "key_points" in tpl

    def test_full_has_segments_summary(self):
        from app.summarizer import DEPTH_TEMPLATES

        assert "segments_summary" in DEPTH_TEMPLATES["full"]
        assert "topics" in DEPTH_TEMPLATES["full"]


class TestFindCached:
    def _task(self, task_id, status, url=None, depth="essence", prompt_hash=""):
        from app.models import TaskRecord, TaskStatus, VideoMeta

        rec = TaskRecord(
            task_id=task_id,
            status=TaskStatus(status),
            created_at="t",
            updated_at="t",
            depth=depth,
            prompt_hash=prompt_hash,
        )
        if url:
            rec.video = VideoMeta(url=url, title="t")
        return rec

    def test_done_matched(self, monkeypatch):
        from app import pipeline

        monkeypatch.setattr(pipeline, "_tasks", {
            "a": self._task("a", "done", url="https://www.bilibili.com/video/BV1GJ411x7h7"),
        })
        hit = pipeline.find_cached("https://www.bilibili.com/video/BV1GJ411x7h7")
        assert hit is not None and hit.task_id == "a"

    def test_running_matched(self, monkeypatch):
        from app import pipeline

        monkeypatch.setattr(pipeline, "_tasks", {
            "b": self._task("b", "running", url="https://www.bilibili.com/video/BV1GJ411x7h7"),
        })
        assert pipeline.find_cached("https://www.bilibili.com/video/BV1GJ411x7h7") is not None

    def test_failed_not_matched(self, monkeypatch):
        from app import pipeline

        monkeypatch.setattr(pipeline, "_tasks", {
            "c": self._task("c", "failed", url="https://www.bilibili.com/video/BV1GJ411x7h7"),
        })
        assert pipeline.find_cached("https://www.bilibili.com/video/BV1GJ411x7h7") is None

    def test_url_normalized(self, monkeypatch):
        from app import pipeline

        monkeypatch.setattr(pipeline, "_tasks", {
            "d": self._task("d", "done", url="https://www.bilibili.com/video/BV1GJ411x7h7"),
        })
        # 无协议前缀同样命中
        assert pipeline.find_cached("www.bilibili.com/video/BV1GJ411x7h7") is not None

    def test_invalid_url_raises(self, monkeypatch):
        from app import pipeline
        from app.errors import AppError

        monkeypatch.setattr(pipeline, "_tasks", {})
        with pytest.raises(AppError):
            pipeline.find_cached("https://example.com/x")

    def test_depth_mismatch_not_matched(self, monkeypatch):
        """detailed 请求不得命中 essence 缓存（depth 计入缓存键）。"""
        from app import pipeline
        from app.models import ExtractRequest

        monkeypatch.setattr(pipeline, "_tasks", {
            "a": self._task("a", "done", url="https://www.bilibili.com/video/BV1GJ411x7h7", depth="essence"),
        })
        # essence 请求命中
        assert pipeline.find_cached("https://www.bilibili.com/video/BV1GJ411x7h7") is not None
        # detailed 请求不命中
        detail_req = ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", depth="detailed")
        assert pipeline.find_cached(detail_req) is None

    def test_depth_match_hits(self, monkeypatch):
        from app import pipeline
        from app.models import ExtractRequest

        monkeypatch.setattr(pipeline, "_tasks", {
            "a": self._task("a", "done", url="https://www.bilibili.com/video/BV1GJ411x7h7", depth="full"),
        })
        hit = pipeline.find_cached(
            ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", depth="full")
        )
        assert hit is not None and hit.task_id == "a"

    def test_queued_matches_by_persisted_url(self, monkeypatch):
        """queued 任务 video 未获取时，凭创建时落库的 rec.url 也能命中（防并发重复建任务）。"""
        from app import pipeline
        from app.models import TaskRecord, TaskStatus

        rec = TaskRecord(
            task_id="q1", status=TaskStatus.QUEUED, created_at="t", updated_at="t",
            url="https://www.bilibili.com/video/BV1GJ411x7h7",
        )
        monkeypatch.setattr(pipeline, "_tasks", {"q1": rec})
        assert pipeline.find_cached("https://www.bilibili.com/video/BV1GJ411x7h7") is not None

    def test_custom_template_breaks_cache(self, monkeypatch):
        """自定义提示词模板不同 → 缓存键不同，不命中。"""
        from app import pipeline
        from app.models import ExtractRequest

        monkeypatch.setattr(pipeline, "_tasks", {
            "a": self._task("a", "done", url="https://www.bilibili.com/video/BV1GJ411x7h7", prompt_hash=""),
        })
        custom_req = ExtractRequest(
            url="https://www.bilibili.com/video/BV1GJ411x7h7", map_prompt="自定义 Map 模板"
        )
        assert pipeline.find_cached(custom_req) is None

    def test_custom_template_same_hash_matches(self, monkeypatch):
        from app import pipeline
        from app.models import ExtractRequest

        req = ExtractRequest(
            url="https://www.bilibili.com/video/BV1GJ411x7h7",
            map_prompt="模板甲", reduce_prompt="模板乙",
        )
        phash = pipeline._prompt_hash("模板甲", "模板乙")
        assert phash  # 非空
        monkeypatch.setattr(pipeline, "_tasks", {
            "a": self._task("a", "done", url="https://www.bilibili.com/video/BV1GJ411x7h7", prompt_hash=phash),
        })
        assert pipeline.find_cached(req) is not None

    def test_degraded_done_not_reused(self, monkeypatch):
        """降级完成的 DONE 任务不参与缓存复用（配好 LLM 后应重跑拿真结果）。"""
        from app import pipeline
        from app.models import SummaryResult, TaskRecord, TaskStatus

        rec = TaskRecord(
            task_id="dg", status=TaskStatus.DONE, created_at="t", updated_at="t",
            url="https://www.bilibili.com/video/BV1GJ411x7h7",
            summary=SummaryResult(degraded=True, degraded_reason="未配置 LLM_API_KEY"),
        )
        monkeypatch.setattr(pipeline, "_tasks", {"dg": rec})
        assert pipeline.find_cached("https://www.bilibili.com/video/BV1GJ411x7h7") is None

    def test_normal_done_still_reused(self, monkeypatch):
        """非降级的 DONE 任务正常复用。"""
        from app import pipeline
        from app.models import SummaryResult, TaskRecord, TaskStatus

        rec = TaskRecord(
            task_id="ok", status=TaskStatus.DONE, created_at="t", updated_at="t",
            url="https://www.bilibili.com/video/BV1GJ411x7h7",
            summary=SummaryResult(summary="真整理结果", degraded=False),
        )
        monkeypatch.setattr(pipeline, "_tasks", {"ok": rec})
        hit = pipeline.find_cached("https://www.bilibili.com/video/BV1GJ411x7h7")
        assert hit is not None and hit.task_id == "ok"

    def test_running_degraded_still_joins(self, monkeypatch):
        """RUNNING 的降级中任务仍可接入（进度共享，不因中间态 degraded 拒绝）。"""
        from app import pipeline
        from app.models import SummaryResult, TaskRecord, TaskStatus

        rec = TaskRecord(
            task_id="rn", status=TaskStatus.RUNNING, created_at="t", updated_at="t",
            url="https://www.bilibili.com/video/BV1GJ411x7h7",
            summary=SummaryResult(degraded=True),
        )
        monkeypatch.setattr(pipeline, "_tasks", {"rn": rec})
        assert pipeline.find_cached("https://www.bilibili.com/video/BV1GJ411x7h7") is not None


class TestSubmitOrReuse:
    def test_identical_request_reuses(self, monkeypatch):
        from app import pipeline
        from app.models import ExtractRequest

        monkeypatch.setattr(pipeline._store, "upsert", lambda rec: None)
        monkeypatch.setattr(pipeline, "_tasks", {})

        req = ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", depth="essence")
        rec1, reused1 = pipeline.submit_or_reuse(req)
        assert not reused1
        rec2, reused2 = pipeline.submit_or_reuse(
            ExtractRequest(url="www.bilibili.com/video/BV1GJ411x7h7", depth="essence")
        )
        assert reused2 and rec2.task_id == rec1.task_id

    def test_different_depth_creates_new(self, monkeypatch):
        from app import pipeline
        from app.models import ExtractRequest

        monkeypatch.setattr(pipeline._store, "upsert", lambda rec: None)
        monkeypatch.setattr(pipeline, "_tasks", {})

        rec1, _ = pipeline.submit_or_reuse(
            ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", depth="essence")
        )
        rec2, reused2 = pipeline.submit_or_reuse(
            ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", depth="full")
        )
        assert not reused2 and rec2.task_id != rec1.task_id


class TestCreateTaskPersists:
    def test_request_params_saved(self, monkeypatch):
        from app import pipeline

        monkeypatch.setattr(pipeline._store, "upsert", lambda rec: None)
        monkeypatch.setattr(pipeline, "_tasks", {})
        from app.models import ExtractRequest

        req = ExtractRequest(
            url="www.bilibili.com/video/BV1GJ411x7h7", depth="detailed",
            audience="学生", purpose="学习笔记", map_prompt="自定义A", reduce_prompt="自定义B",
        )
        rec = pipeline.create_task(req)
        assert rec.url == "https://www.bilibili.com/video/BV1GJ411x7h7"
        assert rec.depth == "detailed"
        assert rec.audience == "学生"
        assert rec.purpose == "学习笔记"
        assert rec.prompt_hash  # 非空
        # 默认请求不产生模板哈希
        rec2 = pipeline.create_task(ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7"))
        assert rec2.prompt_hash == ""
        assert rec2.url == "https://www.bilibili.com/video/BV1GJ411x7h7"


class TestDepthValidation:
    def test_valid_depths(self):
        from app.models import ExtractRequest

        for d in ("essence", "detailed", "full"):
            req = ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", depth=d)
            assert req.depth == d

    def test_invalid_depth_raises(self):
        from pydantic import ValidationError

        from app.models import ExtractRequest

        with pytest.raises(ValidationError):
            ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", depth="超详细")
        with pytest.raises(ValidationError):
            ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", depth="")


class TestTranscriptCacheKey:
    """转写缓存键：BV + ASR 模型 + 语言 + VAD + 纠错 + 引擎 → 配置变更自动失效。"""

    def test_same_request_same_key(self):
        from app.pipeline import _transcript_cache_key

        req1 = ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7")
        req2 = ExtractRequest(url="www.bilibili.com/video/BV1GJ411x7h7")
        assert _transcript_cache_key(req1) == _transcript_cache_key(req2)

    def test_different_model_different_key(self):
        from app.pipeline import _transcript_cache_key

        req1 = ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", asr_model="tiny")
        req2 = ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", asr_model="small")
        assert _transcript_cache_key(req1) != _transcript_cache_key(req2)

    def test_default_vs_override_different(self):
        from app.pipeline import _transcript_cache_key

        req_default = ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7")
        req_tiny = ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", asr_model="tiny")
        assert _transcript_cache_key(req_default) != _transcript_cache_key(req_tiny)

    def test_different_url_different_key(self):
        from app.pipeline import _transcript_cache_key

        req1 = ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7")
        req2 = ExtractRequest(url="https://www.bilibili.com/video/BV1C34q6yEv5")
        assert _transcript_cache_key(req1) != _transcript_cache_key(req2)


class TestLLMUsageTracking:
    """LLM token 用量统计：_record_usage 累计 + snapshot 快照。"""

    def test_snapshot_initial(self):
        from app.summarizer import llm_usage_snapshot

        snap = llm_usage_snapshot()
        assert "prompt_tokens" in snap and "completion_tokens" in snap

    def test_record_usage_accumulates(self, monkeypatch):
        from app import summarizer as _sum

        # 用 fake resp 对象模拟 OpenAI 响应
        class FakeUsage:
            def __init__(self, p, c):
                self.prompt_tokens = p
                self.completion_tokens = c

        class FakeResp:
            def __init__(self, p, c):
                self.usage = FakeUsage(p, c)

        before = _sum.llm_usage_snapshot()
        _sum._record_usage(FakeResp(100, 50))
        _sum._record_usage(FakeResp(200, 80))
        after = _sum.llm_usage_snapshot()
        assert after["prompt_tokens"] - before["prompt_tokens"] == 300
        assert after["completion_tokens"] - before["completion_tokens"] == 130

    def test_record_usage_no_usage_attr(self):
        from app.summarizer import _record_usage, llm_usage_snapshot

        before = llm_usage_snapshot()
        _record_usage(object())  # 无 usage 属性 → 静默跳过
        after = llm_usage_snapshot()
        assert after == before


class TestCreateTaskNewFields:
    """create_task 落库 asr_model / output_lang。"""

    def test_asr_model_and_output_lang_saved(self, monkeypatch):
        from app import pipeline
        from app.models import ExtractRequest

        monkeypatch.setattr(pipeline._store, "upsert", lambda rec: None)
        monkeypatch.setattr(pipeline, "_tasks", {})
        req = ExtractRequest(
            url="https://www.bilibili.com/video/BV1GJ411x7h7",
            depth="full", asr_model="tiny", output_lang="en",
        )
        rec = pipeline.create_task(req)
        assert rec.asr_model == "tiny"
        assert rec.output_lang == "en"

    def test_defaults_when_not_specified(self, monkeypatch):
        from app import pipeline
        from app.models import ExtractRequest

        monkeypatch.setattr(pipeline._store, "upsert", lambda rec: None)
        monkeypatch.setattr(pipeline, "_tasks", {})
        rec = pipeline.create_task(ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7"))
        assert rec.asr_model == ""
        assert rec.output_lang == "zh"


class TestCleanupOldData:
    """数据保留清理：过期任务目录 + DB 记录删除。"""

    def test_cleanup_removes_old(self, monkeypatch, tmp_path):
        from app import pipeline
        from app.models import TaskRecord, TaskStatus

        # 用 tmp_path 作为 data_dir
        monkeypatch.setattr(pipeline.settings, "data_dir", tmp_path)
        monkeypatch.setattr(pipeline._store, "delete", lambda tid: None)
        monkeypatch.setattr(pipeline, "_tasks", {})

        # 创建一个「过期」任务（updated_at 为 40 天前）+ 一个新任务
        old_ts = "2020-01-01 00:00:00"
        old_dir = tmp_path / "oldtask"
        old_dir.mkdir(parents=True)
        (old_dir / "result.md").write_text("old", encoding="utf-8")
        old_rec = TaskRecord(task_id="oldtask", status=TaskStatus.DONE, created_at=old_ts, updated_at=old_ts)
        pipeline._tasks["oldtask"] = old_rec

        new_rec = TaskRecord(task_id="newtask", status=TaskStatus.DONE, created_at="2099-01-01 00:00:00", updated_at="2099-01-01 00:00:00")
        pipeline._tasks["newtask"] = new_rec

        dirs, recs = pipeline.cleanup_old_data(30)
        assert dirs == 1 and recs == 1
        assert "oldtask" not in pipeline._tasks
        assert "newtask" in pipeline._tasks
        assert not old_dir.exists()

    def test_cleanup_zero_days_noop(self, monkeypatch):
        from app import pipeline

        dirs, recs = pipeline.cleanup_old_data(0)
        assert dirs == 0 and recs == 0


class TestStoreDelete:
    """store.delete 删除 DB 记录。"""

    def test_delete_existing(self, tmp_path):
        from app.store import TaskStore
        from app.models import TaskRecord, TaskStatus

        store = TaskStore(tmp_path / "test.db")
        rec = TaskRecord(task_id="del1", status=TaskStatus.DONE, created_at="t", updated_at="t")
        store.upsert(rec)
        assert store.get("del1") is not None
        store.delete("del1")
        assert store.get("del1") is None

    def test_delete_nonexistent_noop(self, tmp_path):
        from app.store import TaskStore

        store = TaskStore(tmp_path / "test.db")
        store.delete("nonexistent")  # 不报错


class TestAuthMiddleware:
    """API_TOKEN 鉴权：写操作需 X-API-Key，读操作放行。"""

    def test_no_token_allows_writes(self, monkeypatch):
        import app.main as main
        from fastapi.testclient import TestClient

        monkeypatch.setattr(main.settings, "api_token", "")
        client = TestClient(main.app)
        r = client.post("/api/extract", json={"url": "https://example.com/x"})
        # 链接非法 → 400（鉴权放行后才到链接校验）
        assert r.status_code == 400

    def test_token_blocks_write_without_header(self, monkeypatch):
        import app.main as main
        from fastapi.testclient import TestClient

        monkeypatch.setattr(main.settings, "api_token", "secret123")
        client = TestClient(main.app)
        r = client.post("/api/extract", json={"url": "https://www.bilibili.com/video/BV1GJ411x7h7"})
        assert r.status_code == 401

    def test_token_allows_write_with_correct_header(self, monkeypatch):
        import app.main as main
        from fastapi.testclient import TestClient

        monkeypatch.setattr(main.settings, "api_token", "secret123")
        monkeypatch.setattr(main.settings, "data_dir", __import__("pathlib").Path(__import__("tempfile").mkdtemp()))
        # run_task 替换为空实现，不发真实下载
        async def _fake_run(*a, **kw):
            await __import__("asyncio").sleep(0)
        monkeypatch.setattr(main, "run_task", _fake_run)
        client = TestClient(main.app)
        r = client.post("/api/extract", json={"url": "https://www.bilibili.com/video/BV1GJ411x7h7"},
                        headers={"X-API-Key": "secret123"})
        assert r.status_code == 200

    def test_token_allows_reads_without_header(self, monkeypatch):
        import app.main as main
        from fastapi.testclient import TestClient

        monkeypatch.setattr(main.settings, "api_token", "secret123")
        client = TestClient(main.app)
        r = client.get("/api/health")
        assert r.status_code == 200
        r = client.get("/api/tasks")
        assert r.status_code == 200


class TestPageParameter:
    """分 P 参数：ExtractRequest / create_task / find_cached / 转写缓存键。"""

    def test_page_in_request(self):
        from app.models import ExtractRequest

        req = ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", page=3)
        assert req.page == 3

    def test_page_default_zero(self):
        from app.models import ExtractRequest

        req = ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7")
        assert req.page == 0

    def test_page_invalid_negative(self):
        from pydantic import ValidationError

        from app.models import ExtractRequest

        with pytest.raises(ValidationError):
            ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", page=-1)

    def test_find_cached_page_mismatch(self, monkeypatch):
        """不同分 P 不命中缓存。"""
        from app import pipeline
        from app.models import ExtractRequest, TaskRecord, TaskStatus

        rec = TaskRecord(task_id="p1", status=TaskStatus.DONE, created_at="t", updated_at="t",
                         url="https://www.bilibili.com/video/BV1GJ411x7h7", page=1)
        monkeypatch.setattr(pipeline, "_tasks", {"p1": rec})
        # page=2 请求不命中 page=1 缓存
        assert pipeline.find_cached(ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", page=2)) is None
        # page=1 请求命中
        assert pipeline.find_cached(ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", page=1)) is not None

    def test_transcript_cache_key_includes_page(self):
        from app.pipeline import _transcript_cache_key

        from app.models import ExtractRequest

        req1 = ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", page=1)
        req2 = ExtractRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7", page=2)
        assert _transcript_cache_key(req1) != _transcript_cache_key(req2)

    def test_page_url_helper(self):
        from app.downloader import _page_url

        assert _page_url("https://www.bilibili.com/video/BV1xx", 0) == "https://www.bilibili.com/video/BV1xx"
        assert _page_url("https://www.bilibili.com/video/BV1xx", 3) == "https://www.bilibili.com/video/BV1xx?p=3"
        assert _page_url("https://www.bilibili.com/video/BV1xx?t=1", 2) == "https://www.bilibili.com/video/BV1xx?t=1&p=2"


class TestCorrectorPunctuationOverride:
    """纠错断句+标点档：punctuation 参数覆盖 settings。"""

    def test_punctuation_template_used(self, monkeypatch):
        from app import corrector, summarizer as _sum

        captured = {}
        def _fake_llm(system, user):
            captured["user"] = user
            return {"segments": [{"index": 0, "text": "测试。"}]}

        monkeypatch.setattr(_sum, "_call_llm", _fake_llm)
        monkeypatch.setattr(corrector.settings, "correct_punctuation", False)
        corrector._correct_batch(["测试"], 0, punctuation=True)
        assert "优化断句与标点" in captured["user"]

    def test_default_template_when_no_override(self, monkeypatch):
        from app import corrector, summarizer as _sum

        captured = {}
        def _fake_llm(system, user):
            captured["user"] = user
            return {"segments": [{"index": 0, "text": "测试。"}]}

        monkeypatch.setattr(_sum, "_call_llm", _fake_llm)
        monkeypatch.setattr(corrector.settings, "correct_punctuation", False)
        corrector._correct_batch(["测试"], 0)
        assert "优化断句与标点" not in captured["user"]


class TestParsePages:
    """分 P 范围语法解析。"""

    def test_range(self):
        from app.models import parse_pages

        assert parse_pages("1-3") == [1, 2, 3]

    def test_list(self):
        from app.models import parse_pages

        assert parse_pages("1,4,7") == [1, 4, 7]

    def test_mixed_and_dedup_sorted(self):
        from app.models import parse_pages

        assert parse_pages("2-5,8,3-4") == [2, 3, 4, 5, 8]

    def test_fullwidth_comma_and_spaces(self):
        from app.models import parse_pages

        assert parse_pages("1， 2 ， 3-4") == [1, 2, 3, 4]

    def test_single(self):
        from app.models import parse_pages

        assert parse_pages("5") == [5]

    def test_invalid_syntax(self):
        from app.models import parse_pages

        for bad in ("abc", "1-", "-2", "0", "3-1"):
            with pytest.raises(ValueError):
                parse_pages(bad)

    def test_over_limit(self):
        from app.models import parse_pages

        with pytest.raises(ValueError):
            parse_pages("1-999", max_page=200)


class TestBatchExtractAPI:
    """pages 参数走批量分支（TestClient，选集探测被 mock，不触真实网络）。"""

    def _client(self, monkeypatch, eps):
        import asyncio

        import app.main as main
        from fastapi.testclient import TestClient

        async def _fake_run(*a, **kw):
            await asyncio.sleep(0)

        monkeypatch.setattr(main, "run_task", _fake_run)
        # 工作区内临时 data_dir（沙箱下系统 TMP 不可靠）
        _tmp = Path(__file__).resolve().parent / "_tmp" / "batch_client"
        if _tmp.exists():
            shutil.rmtree(_tmp, ignore_errors=True)
        _tmp.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(main.settings, "data_dir", _tmp)
        monkeypatch.setattr(main.downloader, "fetch_pages", lambda url: eps)
        return TestClient(main.app)

    @staticmethod
    def _eps(n):
        return [{"page": i + 1, "title": f"第{i+1}集", "duration": 60} for i in range(n)]

    def test_batch_returns_items(self, monkeypatch):
        client = self._client(monkeypatch, self._eps(8))
        r = client.post("/api/extract", json={
            "url": "https://www.bilibili.com/video/BV1GJ411x7h7", "depth": "essence", "pages": "1-3",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["batch"] is True and data["count"] == 3
        assert [i["page"] for i in data["items"]] == [1, 2, 3]
        assert len({i["task_id"] for i in data["items"]}) == 3

    def test_batch_out_of_range_trimmed(self, monkeypatch):
        # 视频只有 2 集：1-5 应裁剪为 1,2
        client = self._client(monkeypatch, self._eps(2))
        r = client.post("/api/extract", json={
            "url": "https://www.bilibili.com/video/BV1GJ411x7h7", "pages": "1-5",
        })
        data = r.json()
        assert data["batch"] is True and [i["page"] for i in data["items"]] == [1, 2]

    def test_single_p_video_degrades_to_main(self, monkeypatch):
        # 单P视频填 pages=5 → 退化为主视频 page=0，不产生 ?p=N 下载
        client = self._client(monkeypatch, [])
        r = client.post("/api/extract", json={
            "url": "https://www.bilibili.com/video/BV1GJ411x7h7", "pages": "5",
        })
        data = r.json()
        assert "batch" not in data and data.get("page") == 0

    def test_batch_invalid_pages_400(self, monkeypatch):
        client = self._client(monkeypatch, self._eps(8))
        r = client.post("/api/extract", json={
            "url": "https://www.bilibili.com/video/BV1GJ411x7h7", "pages": "abc",
        })
        assert r.status_code == 400

    def test_single_page_still_normal(self, monkeypatch):
        client = self._client(monkeypatch, self._eps(8))
        r = client.post("/api/extract", json={
            "url": "https://www.bilibili.com/video/BV1GJ411x7h7", "page": 2,
        })
        assert r.status_code == 200 and "batch" not in r.json() and r.json()["status"] == "queued"


class TestExporterFallback:
    """导出降级：python-docx 缺失时 DOCX 导出落为 .md 纯文本，不报错。"""

    def test_docx_fallback_to_md(self, tmp_path, monkeypatch):
        from app import exporter

        monkeypatch.setattr(exporter, "_docx_available", lambda: False)
        out = exporter.markdown_to_docx("# 标题\n正文内容", tmp_path / "export.docx")
        assert out.suffix == ".md"
        assert "标题" in out.read_text(encoding="utf-8")

    def test_export_dispatch(self, tmp_path, monkeypatch):
        from app import exporter

        monkeypatch.setattr(exporter, "_docx_available", lambda: False)
        p = exporter.export("t1", "# x", "docx", tmp_path)
        assert p.exists()
        with pytest.raises(ValueError):
            exporter.export("t1", "# x", "exe", tmp_path)

    def test_soffice_not_found_raises(self, tmp_path, monkeypatch):
        from app import exporter

        monkeypatch.setattr(exporter, "_find_soffice", lambda: None)
        with pytest.raises(RuntimeError, match="LibreOffice"):
            exporter.docx_to_pdf(tmp_path / "a.docx", tmp_path / "a.pdf")


class TestAsrStageAndTimeout:
    """①模型加载阶段 + ③超时配置。"""

    def test_loading_model_stage_exists(self):
        from app.models import Stage

        assert Stage.LOADING_MODEL.value == "loading_model"

    def test_asr_timeout_default(self):
        from app.config import settings

        assert settings.asr_timeout >= 0
        assert isinstance(settings.asr_prewarm, bool)

    def test_safe_cb_compat_old_signature(self):
        """旧单参回调（corrector/summarizer）与新双参回调共存。"""
        from app.transcriber import _safe_cb

        calls_new = []
        _safe_cb(lambda pct, phase: calls_new.append((pct, phase)), 50, "model")
        assert calls_new == [(50, "model")]
        calls_old = []
        _safe_cb(lambda pct: calls_old.append(pct), 30, "transcribe")  # TypeError 回退
        assert calls_old == [30]

    def test_ensure_model_local_dir_ok(self, tmp_path):
        from app.transcriber import ensure_model

        assert ensure_model(str(tmp_path)) is True  # 本地目录直接可用

    def test_fetch_pages_single_returns_empty(self):
        from app.downloader import fetch_pages

        # mock 引擎下 yt-dlp 仍会真实请求；这里只验证函数存在且非法链接抛错
        from app.errors import AppError

        with pytest.raises(AppError):
            fetch_pages("https://example.com/x")


class TestOverview:
    """视频概要（overview）：比一句话摘要长的整体概述。"""

    def test_reduce_parses_overview(self, monkeypatch):
        from app import summarizer as _sum
        from app.models import VideoMeta

        fake = {
            "summary": "一句话",
            "overview": "这是一段约一百五十字的视频概要，讲清了全片主题、核心论点与结论。",
            "key_points": [{"point": "要点一", "evidence": "原文一"}],
            "quotes": [],
            "actions": [],
        }
        monkeypatch.setattr(_sum, "_call_llm", lambda s, u: fake)
        res = _sum.reduce_summary([], VideoMeta(url="u", title="T"), "通用受众", "了解", depth="essence")
        assert res.overview.startswith("这是一段")
        assert res.summary == "一句话"

    def test_render_includes_overview_section(self):
        from app.summarizer import render_markdown
        from app.models import SummaryResult, VideoMeta

        s = SummaryResult(summary="一句话", overview="概要内容一段话。", key_points=[])
        md = render_markdown(VideoMeta(url="u", title="T"), s)
        assert "## 视频概要" in md and "概要内容一段话。" in md

    def test_render_skips_empty_overview(self):
        """旧任务无 overview 字段时不渲染该段（向后兼容）。"""
        from app.summarizer import render_markdown
        from app.models import SummaryResult, VideoMeta

        s = SummaryResult(summary="一句话", overview="")
        md = render_markdown(VideoMeta(url="u", title="T"), s)
        assert "## 视频概要" not in md

    def test_all_depth_templates_request_overview(self):
        from app.summarizer import DEPTH_TEMPLATES

        for d in ("essence", "detailed", "full"):
            assert '"overview"' in DEPTH_TEMPLATES[d]