"""简历解析器 - 基于规则的解析，无 LLM

P0 变更（对应 DEVELOPMENT_SPEC.md §2）：
- 文件名/目录名不再进入评分文本，仅作展示元数据
- 正文为空或过少时 parse_status=needs_review，绝不参与评分
- 工作年限不再优先取文件名；由正文工作区间合并计算（见 scorer.merge_employments）
- 年份估算不再固定到 2026，日期不足标记未知

P1 变更（对应 DEVELOPMENT_SPEC.md §6.1/§9）：
- 分页解析：parse_document -> list[Page]（含页文本哈希与页级质量标记）
- parse_resume 保留兼容入口（文本聚合视图）
"""
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import pdfplumber

# 可选 OCR：优先 tesseract；不可用时回退 rapidocr（纯 pip、模型随包、无需外部下载）
import shutil
import traceback

from .config import YEAR_PATTERNS  # 保留：仅作“待核实元数据”展示
from .schemas import Page, ParseStatus

OCR_AVAILABLE = False
OCR_ERROR: Optional[str] = None
_TESSERACT_CMD: Optional[str] = None
_OCR_BACKEND: Optional[str] = None
_RAPID_ENGINE: Optional[Any] = None
_RAPID_INIT_ERROR: Optional[str] = None

try:
    from PIL import Image
    import pytesseract
    _TESSERACT_CMD = shutil.which("tesseract")
    if _TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD
        OCR_AVAILABLE = True
        _OCR_BACKEND = "tesseract"
    else:
        pytesseract = None  # type: ignore[assignment]
except Exception as e:
    pytesseract = None  # type: ignore[assignment]
    _RAPID_INIT_ERROR = f"tesseract 初始化失败: {e}"

# rapidocr 惰性初始化（导入较慢，仅在图片解析时触发）
try:
    from rapidocr_onnxruntime import RapidOCR  # noqa: F401
    _RAPID_AVAILABLE = True
except Exception as e:
    _RAPID_AVAILABLE = False
    _RAPID_INIT_ERROR = (_RAPID_INIT_ERROR or "") + f"; rapidocr 导入失败: {e}"

if not OCR_AVAILABLE and _RAPID_AVAILABLE:
    OCR_AVAILABLE = True
    _OCR_BACKEND = "rapidocr"
    OCR_ERROR = "tesseract 不可用，已回退 rapidocr（模型随包内置）"
elif not OCR_AVAILABLE:
    OCR_ERROR = (_RAPID_INIT_ERROR or "未检测到 tesseract 可执行文件，且 rapidocr 不可用") + "，图片简历无法 OCR"


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _page_flags(text: str) -> List[str]:
    """页级质量标记：文本过少/异常比例。"""
    flags: List[str] = []
    stripped = text.strip()
    if not stripped:
        flags.append("text_empty")
    elif len(stripped) < 50:
        flags.append("text_too_short")
    # 异常字符比例：非中文、非 ASCII 字母数字的可打印字符占比较高时标记
    if stripped:
        unusual = sum(1 for ch in stripped if ord(ch) > 0x2FFF and not (0x4E00 <= ord(ch) <= 0x9FFF))
        if unusual / max(len(stripped), 1) > 0.3:
            flags.append("unusual_char_ratio")
    return flags


def extract_pages_from_pdf(path: Path) -> Tuple[List[Page], List[str]]:
    """按页解析 PDF，返回 (pages, 文件级质量标记)。"""
    pages: List[Page] = []
    flags: List[str] = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            for idx, page in enumerate(pdf.pages, start=1):
                t = page.extract_text() or ""
                pages.append(Page(
                    document_id="",
                    page_number=idx,
                    text=t,
                    text_hash=_text_hash(t),
                    extraction_method="pdfplumber",
                    quality_flags=_page_flags(t),
                ))
    except Exception:
        flags.append("pdf_parse_failed")
        return [], flags
    if not pages:
        flags.append("no_pages")
    return pages, flags


def _get_rapid_engine() -> Optional[Any]:
    """惰性初始化 rapidocr 引擎（模型随包内置，无外部下载）。"""
    global _RAPID_ENGINE
    if _RAPID_ENGINE is None and _RAPID_AVAILABLE:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _RAPID_ENGINE = RapidOCR()
        except Exception as e:
            _RAPID_INIT_ERROR = f"rapidocr 初始化失败: {e}"
            return None
    return _RAPID_ENGINE


def _ocr_with_rapid(image: Image.Image) -> Tuple[str, List[str]]:
    """rapidocr 切块 OCR：超长简历图（如 720x20000）切 2000px 高、150px 重叠逐块识别。

    返回 (拼接文本, 质量标记)。
    """
    engine = _get_rapid_engine()
    if engine is None:
        return "", ["ocr_unavailable", _RAPID_INIT_ERROR or "rapidocr 不可用"]
    try:
        w, h = image.size
        chunk_h, overlap = 2000, 150
        tmp_dir = Path(tempfile.gettempdir())
        lines: List[str] = []
        y = 0
        while y < h:
            bottom = min(y + chunk_h, h)
            crop = image.crop((0, y, w, bottom))
            tmp = tmp_dir / f"resume_ocr_slice_{os.getpid()}_{y}.png"
            try:
                crop.save(tmp)
                result, _ = engine(str(tmp))
                if result:
                    for _, text, _ in result:
                        if text and text.strip():
                            lines.append(text.strip())
            finally:
                tmp.unlink(missing_ok=True)
            if bottom >= h:
                break
            y += chunk_h - overlap
        text = "\n".join(lines)
        if not text.strip():
            return "", ["ocr_empty"]
        return text, []
    except Exception as e:
        traceback.print_exc()
        return "", ["ocr_failed", str(e)]


def extract_pages_from_image(path: Path) -> Tuple[List[Page], List[str]]:
    """对图片 OCR 为一页；tesseract 优先，rapidocr 回退。"""
    if not OCR_AVAILABLE:
        return [], ["ocr_unavailable", OCR_ERROR or "OCR 不可用"]
    try:
        image = Image.open(path)
        text = ""
        method = ""
        if pytesseract is not None and _TESSERACT_CMD:
            try:
                text = pytesseract.image_to_string(image, lang="chi_sim+eng")
                method = "tesseract"
            except Exception as e:
                text = ""
                _tess_err = f"tesseract OCR 失败: {e}"
        if not text.strip() and _RAPID_AVAILABLE:
            text, rapid_flags = _ocr_with_rapid(image)
            method = "rapidocr"
            if rapid_flags:
                return [], rapid_flags
        text = text.strip()
        flags: List[str] = []
        if not text:
            flags.append("ocr_empty")
        page = Page(
            document_id="",
            page_number=1,
            text=text,
            text_hash=_text_hash(text),
            extraction_method=method or "ocr",
            quality_flags=_page_flags(text) + flags,
        )
        return [page], []
    except Exception as e:
        traceback.print_exc()
        return [], ["ocr_failed", str(e)]


def extract_pages_from_docx(path: Path) -> Tuple[List[Page], List[str]]:
    """用 python-docx 解析 Word 简历为单页文本（正文段落 + 表格单元格文本）。

    - 过滤空段落；表格单元格文本一并抽取（简历常用表格排版）。
    - DOCX 无真实分页概念，合并为第 1 页；extraction_method 标识 "python-docx"。
    """
    try:
        from docx import Document
    except Exception as e:  # 依赖缺失（理论上 requirements 已锁定 python-docx）
        return [], [f"docx_import_failed:{e}"]
    try:
        doc = Document(str(path))
    except Exception as e:
        return [], [f"docx_parse_failed:{e}"]

    lines: List[str] = []
    for p in doc.paragraphs:
        t = (p.text or "").strip()
        if t:
            lines.append(t)
    # 表格排版简历：单元格文本也作为正文
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                t = (cell.text or "").strip()
                if t:
                    lines.append(t)

    text = "\n".join(lines)
    page = Page(
        document_id="",
        page_number=1,
        text=text,
        text_hash=_text_hash(text),
        extraction_method="python-docx",
        quality_flags=_page_flags(text),
    )
    return [page], []


def extract_pages(path: Path) -> Tuple[List[Page], List[str]]:
    """根据扩展名解析为分页文本，返回 (pages, 文件级质量标记)。"""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pages_from_pdf(path)
    if suffix in (".jpg", ".jpeg", ".png", ".bmp"):
        return extract_pages_from_image(path)
    if suffix == ".docx":
        return extract_pages_from_docx(path)
    return [], [f"unsupported_suffix:{suffix}"]


def parse_document(document_id: str, file_path: str) -> Tuple[List[Page], ParseStatus, str]:
    """P1 分页解析入口：返回 (pages, parse_status, 失败原因)。"""
    path = Path(file_path)
    pages, flags = extract_pages(path)
    if flags and not pages:
        status = ParseStatus.NEEDS_REVIEW
        reason = "; ".join(flags) or "解析失败"
        return [], status, reason
    if not any(p.text.strip() for p in pages):
        status = ParseStatus.NEEDS_REVIEW
        reason = "正文为空" if not flags else "; ".join(flags)
        return pages, status, reason
    for p in pages:
        p.document_id = document_id
    return pages, ParseStatus.PARSED, ""


def extract_text(path: Path) -> Tuple[str, List[str]]:
    """兼容入口：聚合全部页文本。"""
    pages, flags = extract_pages(path)
    text = "\n".join(p.text for p in pages)
    if not text.strip():
        flags.append("text_empty")
    elif len(text.strip()) < 50:
        flags.append("text_too_short")
    return text, flags


def extract_text_from_pdf(path: Path) -> Tuple[str, List[str]]:
    """兼容入口：PDF 聚合文本。"""
    text, flags = extract_text(path)
    return text, flags


def extract_text_from_image(path: Path) -> Tuple[str, List[str]]:
    """兼容入口：图片 OCR 聚合文本。"""
    text, flags = extract_text(path)
    return text, flags


def parse_years_from_filename(filename: str) -> Optional[int]:
    """从文件名解析工作年限（仅作待核实元数据展示，不参与评分）"""
    for pat in YEAR_PATTERNS:
        m = re.search(pat, filename)
        if m:
            return int(m.group(1))
    return None


def parse_name_from_filename(filename: str) -> str:
    """从文件名解析候选人姓名（仅展示）"""
    base = Path(filename).stem
    base = re.sub(r"^[【\[].*?[】\]]", "", base)
    base = re.sub(r"^作品集", "", base)
    base = re.sub(r"\d+年以上|\d+年|10年|6年|7年|8年|9年", "", base)
    base = re.sub(r"\(\d+\)$", "", base).strip()
    base = base.replace(" ", "").replace("_", "").strip()
    return base or "未知姓名"


def parse_resume(file_path: str) -> Dict[str, Any]:
    """解析单份简历，返回结构化字段（兼容入口，聚合页文本）。

    返回字段含 parse_status：
    - "parsed": 正文可用
    - "needs_review": 正文为空/过少/解析失败 → 人工复核，不参与评分
    - "failed": 文件级错误
    """
    path = Path(file_path)
    name = parse_name_from_filename(path.name)
    filename_years = parse_years_from_filename(path.name)

    try:
        raw_text, flags = extract_text(path)
    except Exception as e:
        return {
            "name": name,
            "file_name": path.name,
            "file_path": str(path),
            "parse_status": "failed",
            "parse_error": f"解析异常: {e}",
            "raw_text": "",
            "quality_flags": ["extract_exception"],
        }

    has_text = bool(raw_text.strip())
    status = "parsed" if has_text else "needs_review"

    # 区分“正文为空”与“图片 OCR 不可用”，不静默降级
    if not has_text and path.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp"):
        if not OCR_AVAILABLE:
            reason = OCR_ERROR or "OCR 不可用"
        elif "ocr_empty" in flags:
            reason = "OCR 无识别结果"
        else:
            reason = flags[-1] if flags else "图片解析无文本"
    elif not raw_text.strip():
        reason = "正文为空"
    else:
        reason = "正文过少"

    return {
        "name": name,
        "file_name": path.name,
        "file_path": str(path),
        "parse_status": status,
        "parse_error": None if has_text else reason,
        "raw_text": raw_text,
        "quality_flags": flags,
        "filename_years": filename_years,  # 仅展示
        "text_length": len(raw_text),
        "has_text": has_text,
        "ocr_used": has_text and path.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp"),
    }
