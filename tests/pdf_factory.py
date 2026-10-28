"""Synthetic invoice PDFs for tests. All companies, tax IDs, and numbers are fake."""

from __future__ import annotations

from pathlib import Path

import fitz


def make_pdf_pages(
    tmp_path: Path,
    pages: list[list[tuple[float, float, str]]],
    name: str = "invoice.pdf",
) -> Path:
    document = fitz.open()
    for lines in pages:
        page = document.new_page(width=800, height=450)
        for x, y, text in lines:
            page.insert_text((x, y), text, fontname="china-s", fontsize=9)
    path = tmp_path / name
    document.save(path)
    document.close()
    return path


def make_pdf(tmp_path: Path, lines: list[tuple[float, float, str]], name: str = "invoice.pdf") -> Path:
    return make_pdf_pages(tmp_path, [lines], name)


def full_digital_lines(
    *,
    buyer_tax: bool = True,
    small: str = "¥ 1060.00",
    uppercase: str | None = "壹仟零陆拾圆整",
    header: str = "电子发票(普通发票)",
    item: str = "*生产生活服务*住宿费",
    number: str = "26502000000000000001",
    seller: str = "示例乙酒店",
    net: str = "¥1000.00",
    tax: str = "¥60.00",
) -> list[tuple[float, float, str]]:
    lines = [
        (430, 40, "发票号码:"),
        (480, 40, number),
        (200, 20, header),
        (430, 56, "开票日期:"),
        (480, 56, "2026年07月30日"),
        (30, 104, "名称:"),
        (56, 104, "示例甲公司"),
        (310, 104, "名称:"),
        (336, 104, seller),
        (310, 132, "统一社会信用代码/纳税人识别号:"),
        (440, 132, "92500000TEST00002X"),
        (20, 166, item),
        (400, 268, net),
        (550, 268, tax),
        (60, 267, "合"),
        (100, 267, "计"),
        (410, 287, "(小写)"),
        (450, 287, small),
    ]
    if uppercase is not None:
        lines.extend([(50, 287, "价税合计(大写)"), (180, 287, uppercase)])
    if buyer_tax:
        lines.insert(8, (30, 132, "统一社会信用代码/纳税人识别号:"))
        lines.insert(9, (160, 132, "91500000TEST00001X"))
    return lines
