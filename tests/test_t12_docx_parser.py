# -*- coding: utf-8 -*-
"""T12：DOCX（.docx）简历解析测试——全部用 python-docx 在 tmp_path 现场合成，
不依赖真实简历目录、不联网。

覆盖：
- .docx 后缀被分派到 python-docx 解析；
- 段落文本（过滤空行）可被抽出，关键词命中；
- extraction_method 标识 "python-docx"；
- 空文档 → 正文为空 → needs_review（与 pdf/image 空文档行为一致）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docx import Document

from src.parser import extract_pages, parse_document
from src.schemas import ParseStatus


def _write_docx(path: Path, paragraphs, tables=None):
    """最小合成器：段落 + 可选表格（rows 为二维字符串列表）。"""
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    for row in (tables or []):
        for cell_text in row:
            doc.add_paragraph(cell_text)
    doc.save(str(path))
    return path


def test_docx_paragraphs_extracted_with_keywords(tmp_path):
    """.docx 解析：非空 pages、关键词在文本中、method=python-docx。"""
    p = _write_docx(
        tmp_path / "candidate.docx",
        paragraphs=[
            "张三  海外项目负责人（钢结构方向）",
            "",  # 空行应被过滤，不影响正文
            "2016-2024 主导钢结构模块化项目交付，接受出差与驻场。",
        ],
    )
    pages, flags = extract_pages(p)
    assert flags == []
    assert len(pages) == 1
    text = pages[0].text
    assert "海外项目负责人" in text
    assert "钢结构" in text
    assert "接受出差" in text
    assert pages[0].extraction_method == "python-docx"
    # 空行被过滤：正文不应以空段开头
    assert not text.startswith("\n")


def test_docx_via_parse_document_status_parsed(tmp_path):
    """parse_document 入口：.docx → PARSED，document_id 被回填。"""
    p = _write_docx(
        tmp_path / "resume.docx",
        paragraphs=["李四  钢结构工程师", "英语流利，负责海外 MiC 项目安装。"],
    )
    pages, status, reason = parse_document("doc-001", str(p))
    assert status == ParseStatus.PARSED
    assert reason == ""
    assert len(pages) == 1
    assert "MiC" in pages[0].text
    assert pages[0].document_id == "doc-001"
    assert pages[0].extraction_method == "python-docx"


def test_docx_empty_document_routes_to_review(tmp_path):
    """空 .docx（无任何段落）→ 正文为空 → needs_review，不参与评分。"""
    p = _write_docx(tmp_path / "empty.docx", paragraphs=[])
    pages, status, reason = parse_document("doc-empty", str(p))
    assert status == ParseStatus.NEEDS_REVIEW
    assert "空" in reason or "empty" in reason.lower()
    assert pages == [] or not any(pg.text.strip() for pg in pages)
