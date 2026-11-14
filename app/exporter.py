"""结果导出：Markdown → DOCX / PDF。

- DOCX：python-docx 按行解析 Markdown 结构（标题/列表/引用/粗体/分隔线）
- PDF：先生成 DOCX，再用 LibreOffice headless 转换；无 soffice 时抛 RuntimeError 提示
- python-docx 缺失（未装依赖）时自动降级为「带 .md 后缀的纯文本文件」，保证始终有产物可下载
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Windows：CREATE_NO_WINDOW 防止 node / soffice 闪出控制台黑框；POSIX 下不传该参数
_NO_WINDOW_KW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _docx_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("docx") is not None


def _find_soffice() -> str | None:
    """定位 LibreOffice 可执行文件（soffice / soffice.exe）。

    顺序：PATH → 常见安装路径 → SOFFICE_PATH 环境变量 → DSH 捆绑运行时。
    """
    # 用户显式指定优先
    env_p = os.environ.get("SOFFICE_PATH", "").strip()
    if env_p and Path(env_p).exists():
        return env_p
    for name in ("soffice", "soffice.exe", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    # Windows 常见安装路径兜底
    for cand in (
        Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
        Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
    ):
        if cand.exists():
            return str(cand)
    # DSH 等运行时捆绑的 LibreOffice（lorunner.exe / soffice.exe）
    try:
        roots = [Path(os.environ.get("USERPROFILE", "")) / ".dsh" / "dsh-runtimes"]
        d = Path(__file__).resolve().drive + os.sep + "deepseek"
        alt = Path(d) / "resources" / "runtime"
        if alt.exists():
            roots.append(alt)
        for root in roots:
            if not root.exists():
                continue
            hits = sorted([q for q in root.glob("**/lorunner.exe")], key=lambda q: len(str(q)))
            if hits:
                return str(hits[0])
            hits = sorted([q for q in root.glob("**/program/soffice.exe")], key=lambda q: len(str(q)))
            if hits:
                return str(hits[0])
    except Exception:  # noqa: BLE001
        logger.debug("捆绑 LibreOffice 探测失败", exc_info=True)
    return None


def _find_lo_kit_cli() -> tuple[str, str] | None:
    """发现捆绑的 LibreOffice Kit CLI（node + cli.js），PDF 转换备用通道。"""
    candidates = [
        r"D:\deepseek\resources\app.asar.unpacked\dsh\node_modules\@deepseek-ai\libreoffice-kit\lib\cli.js",
    ]
    for cli in candidates:
        cli_path = Path(cli)
        if not cli_path.exists():
            continue
        node_candidates = [
            Path(os.environ.get("USERPROFILE", "")) / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime"
            / "dependencies" / "node" / "bin" / "node.exe",
            Path(r"D:\deepseek\resources\runtime\primary-runtime\dependencies\node\bin\node.exe"),
        ]
        for node in node_candidates:
            if node.exists():
                return str(node), str(cli_path)
    return None


# ---------- Markdown → DOCX ----------

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_UL_RE = re.compile(r"^[-*]\s+(.*)$")
_OL_RE = re.compile(r"^\d+\.\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _add_runs_with_bold(paragraph, text: str) -> None:
    """把含 **bold** 的行拆成 run 写入段落。"""
    pos = 0
    for m in _BOLD_RE.finditer(text):
        if m.start() > pos:
            paragraph.add_run(text[pos : m.start()])
        r = paragraph.add_run(m.group(1))
        r.bold = True
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:])


def markdown_to_docx(md_text: str, out_path: Path) -> Path:
    """渲染 Markdown 为 Word 文档。返回输出路径。

    python-docx 未安装时降级：写 UTF-8 文本文件（扩展名保留，内容即原始 markdown），
    保证导出动作不失败。
    """
    if not _docx_available():
        logger.warning("python-docx 未安装，DOCX 导出降级为纯文本 .md")
        fallback = out_path.with_suffix(".md")
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_text(md_text, encoding="utf-8")
        return fallback

    from docx import Document  # noqa: PLC0415
    from docx.shared import Pt  # noqa: PLC0415

    doc = Document()
    # 中文字体基础设置
    style = doc.styles["Normal"]
    style.font.name = "Microsoft YaHei"
    style.font.size = Pt(10.5)

    lines = md_text.splitlines()
    in_details = False
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("<details"):
            in_details = True
            continue
        if stripped.startswith("</details>"):
            in_details = False
            continue
        if not stripped:
            continue
        if stripped == "---":
            doc.add_paragraph("─" * 40, style=None)
            continue
        m = _HEADING_RE.match(stripped)
        if m:
            level = min(len(m.group(1)), 4)
            doc.add_heading(m.group(2).strip(), level=max(0, level - 1))
            continue
        m = _QUOTE_RE.match(stripped)
        if m:
            p = doc.add_paragraph(style="Intense Quote")
            _add_runs_with_bold(p, m.group(1))
            continue
        m = _UL_RE.match(stripped)
        if m:
            p = doc.add_paragraph(style="List Bullet")
            _add_runs_with_bold(p, m.group(1))
            continue
        m = _OL_RE.match(stripped)
        if m:
            p = doc.add_paragraph(style="List Number")
            _add_runs_with_bold(p, m.group(1))
            continue
        # details 内的完整转写用普通段落即可
        p = doc.add_paragraph()
        _add_runs_with_bold(p, stripped)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    logger.info("DOCX 已生成：%s", out_path)
    return out_path


# ---------- DOCX → PDF（LibreOffice headless）----------

def docx_to_pdf(docx_path: Path, pdf_path: Path) -> Path:
    """DOCX → PDF：优先系统 LibreOffice（soffice headless），其次捆绑 LibreOffice Kit CLI。

    两者都找不到时抛 RuntimeError（API 层转 500 友好提示）。
    """
    soffice = _find_soffice()
    if soffice:
        return _docx_to_pdf_soffice(soffice, docx_path, pdf_path)
    kit = _find_lo_kit_cli()
    if kit:
        node, cli = kit
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [node, cli, "convert", "--input", str(docx_path), "--output", str(pdf_path)],
            capture_output=True, timeout=180, **_NO_WINDOW_KW,  # bytes 模式：kit 输出可能含非 UTF-8 字节
        )
        if not pdf_path.exists():
            err = (proc.stderr or proc.stdout or b"")[-300:].decode("utf-8", errors="replace")
            raise RuntimeError(f"LibreOffice Kit 转换失败：{err}")
        logger.info("PDF 已生成（LibreOffice Kit）：%s", pdf_path)
        return pdf_path
    raise RuntimeError(
        "未找到 LibreOffice（soffice）或捆绑的 LibreOffice Kit，无法导出 PDF。"
        "安装 LibreOffice 后重试，或使用 Docker 镜像（已内置）；也可下载 .md/.docx"
    )


def _docx_to_pdf_soffice(soffice: str, docx_path: Path, pdf_path: Path) -> Path:
    outdir = tempfile.mkdtemp(prefix="lo_pdf_")
    cmd = [
        soffice, "--headless", "--norestore", "--convert-to", "pdf",
        "--outdir", outdir, str(docx_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, **_NO_WINDOW_KW)
        produced = Path(outdir) / (docx_path.stem + ".pdf")
        if proc.returncode != 0 or not produced.exists():
            raise RuntimeError(f"LibreOffice 转换失败：{proc.stderr[-300:] or proc.stdout[-300:]}")
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(pdf_path))
        logger.info("PDF 已生成（soffice）：%s", pdf_path)
        return pdf_path
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def export(task_id: str, markdown: str, fmt: str, data_dir: Path) -> Path:
    """统一导出入口：fmt ∈ {docx, pdf}。返回产物路径（data/{task_id}/export.{ext}）。"""
    fmt = (fmt or "").lower().strip()
    task_dir = Path(data_dir) / task_id
    if fmt == "docx":
        return markdown_to_docx(markdown, task_dir / "export.docx")
    if fmt == "pdf":
        docx = markdown_to_docx(markdown, task_dir / "export.docx")
        try:
            return docx_to_pdf(docx, task_dir / "export.pdf")
        except RuntimeError:
            raise
    raise ValueError(f"不支持的导出格式：{fmt}（可选 docx / pdf）")
