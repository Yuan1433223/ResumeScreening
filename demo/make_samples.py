# -*- coding: utf-8 -*-
"""生成合成样例简历（离线、不依赖外部库）：最小 PDF + 坏文件。

- PDF 用 Helvetica 标准字体（无需嵌入），pdfplumber 可提取文本；
- 文本含英文关键词（steel structure / overseas / fluent english 等），
  让 Mock 抽取器命中部分事实，其余维度显示为"缺失项"（演示证据与缺失项）；
- bad_resume.pdf 为损坏文件，演示解析失败 → needs_review 容错路径。
"""
from __future__ import annotations

from pathlib import Path


def make_pdf(path: Path, text: str) -> None:
    """生成一页英文文本的最小 PDF。"""
    esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    lines = esc.splitlines()[:14]
    content = "\n".join(
        f"BT /F1 11 Tf 56 {700 - i * 16} Td ({l}) Tj ET" for i, l in enumerate(lines)
    )
    pdf = f"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length {len(content) + 10}>>stream
{content}
endstream
endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
trailer<</Root 1 0 R>>
"""
    path.write_bytes(pdf.encode("latin-1", errors="replace"))


# 4 份样例：3 份可解析（含证据与缺失项）+ 1 份损坏
SAMPLES = [
    ("zhang_wei.pdf",
     "Zhang Wei\nSenior Project Manager\n10+ years in steel structure and modular building\n"
     "Led overseas delivery of prefab modules in Southeast Asia\n"
     "Fluent English, business English daily\n5000 t steel fabricated per year\n"
     "2015.03 - 2021.06 steel structure project manager\n2021.07 - present overseas project lead"),
    ("li_na.pdf",
     "Li Na\nProject Director\n15 years heavy steel structure engineering\n"
     "Managed international projects, international clients\n"
     "IELTS 7.5, English as working language\n"
     "Delivered 80000 m2 modular building\n2012.01 - 2018.08 steel structure\n2018.09 - now overseas"),
    ("wang_qiang.pdf",
     "Wang Qiang\nConstruction Manager\n8 years prefab and steel structure\n"
     "Participated in overseas steel structure projects\n"
     "CET-6, business english\n"
     "2018.04 - 2022.12 steel structure site\n2023.01 - present overseas project"),
]


def generate(sample_dir: Path) -> None:
    sample_dir.mkdir(parents=True, exist_ok=True)
    for name, text in SAMPLES:
        make_pdf(sample_dir / name, text)
    # 坏文件：损坏的 PDF
    (sample_dir / "bad_resume.pdf").write_bytes(b"%PDF-1.4 garbage\x00\xff not a real pdf")


if __name__ == "__main__":
    import sys

    d = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "samples"
    generate(d)
    print(f"合成样例已生成: {d}")
