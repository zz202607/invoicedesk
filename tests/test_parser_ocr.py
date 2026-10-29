"""Image-only invoice PDFs: OCR fallback, explicit review state, and mapping."""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from app.core import ocr, parser
from tests.pdf_factory import full_digital_lines, make_pdf


def rasterize_pdf(source: Path, target: Path, *, dpi: int = 200) -> Path:
    """Turn a text PDF into the same page as a single raster image."""
    with fitz.open(source) as document:
        page = document[0]
        pixmap = page.get_pixmap(dpi=dpi)
        image = fitz.open()
        image_page = image.new_page(width=page.rect.width, height=page.rect.height)
        image_page.insert_image(image_page.rect, pixmap=pixmap)
        image.save(target)
        image.close()
    return target


@pytest.fixture
def scanned_pdf(tmp_path: Path) -> Path:
    text_pdf = make_pdf(tmp_path, full_digital_lines(), name="text.pdf")
    scanned = rasterize_pdf(text_pdf, tmp_path / "scan.pdf")
    with fitz.open(scanned) as document:
        assert document[0].get_text().strip() == ""
    return scanned


def test_scanned_pdf_is_marked_for_review_when_ocr_is_missing(
    scanned_pdf: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        parser, "_ocr_lines", lambda page: (None, "OCR 组件加载失败")
    )
    invoices = parser.parse_pdf(scanned_pdf)
    assert len(invoices) == 1
    invoice = invoices[0]
    assert invoice.invoice_type.value == "scanned"
    assert invoice.needs_review
    assert any("OCR" in warning for warning in invoice.warnings)


@pytest.mark.skipif(not ocr.is_available(), reason="RapidOCR is not installed")
def test_scanned_pdf_is_recognized_by_ocr(scanned_pdf: Path) -> None:
    invoices = parser.parse_pdf(scanned_pdf)
    assert len(invoices) == 1
    invoice = invoices[0]
    assert invoice.invoice_type.value == "full_digital_normal"
    assert invoice.invoice_number == "26502000000000000001"
    assert invoice.invoice_date == "2026-07-30"
    assert invoice.seller_name == "示例乙酒店"
    assert invoice.buyer_name == "示例甲公司"
    assert invoice.amount_total is not None
    assert str(invoice.amount_total) == "1060.00"
    assert invoice.needs_review
    assert parser.OCR_WARNING in invoice.warnings
    # OCR is a recovery path, so no field is allowed to claim highest confidence.
    assert "high" not in invoice.confidence.values()


def _line(text: str, x0: float, y0: float) -> object:
    from app.core.text import TextLine

    return TextLine(text, x0, y0, x0 + 140, y0 + 10)


def test_side_by_side_parties_follow_columns_when_seller_row_is_higher() -> None:
    lines = [
        _line("名称:乙公司", 310, 95),
        _line("名称:甲公司", 30, 99),
        _line("统一社会信用代码/纳税人识别号:91300000000000001X", 440, 120),
        _line("统一社会信用代码/纳税人识别号:91500000000000002X", 30, 124),
    ]
    values, _, _ = parser._extract_parties(
        lines, parser.InvoiceType.FULL_DIGITAL_NORMAL, 800
    )
    assert values["buyer_name"] == "甲公司"
    assert values["seller_name"] == "乙公司"


def test_a_single_seller_label_identifies_the_seller_side() -> None:
    lines = [
        _line("销售方信息", 33, 175),
        _line("名称:乙公司", 30, 96),
        _line("名称:甲公司", 30, 300),
        _line("统一社会信用代码/纳税人识别号:91300000000000001X", 30, 126),
        _line("统一社会信用代码/纳税人识别号:91500000000000002X", 30, 330),
    ]
    values, _, _ = parser._extract_parties(
        lines, parser.InvoiceType.FULL_DIGITAL_NORMAL, 800
    )
    # Upstream layout is vertical: the label marks the first block as seller.
    assert values["seller_name"] == "乙公司"
    assert values["buyer_name"] == "甲公司"


def test_name_candidates_ignore_amounts_currency_and_remark_numbers() -> None:
    lines = [
        _line("名称:甲公司", 30, 100),
        _line("壹拾柒圆玖角整", 250, 385),
        _line("(小写)¥17.90", 300, 385),
        _line("*日用杂品*卷尺(2)", 18, 220),
        _line("统一社会信用代码/纳税人识别号:010192218471428", 30, 412),
    ]
    names = [line.text for line in parser._name_candidates(lines)]
    assert names == ["甲公司"]


def test_single_tax_pair_fills_the_missing_party_by_position() -> None:
    lines = [
        _line("名称:甲公司", 30, 100),
        _line("名称:乙公司", 310, 100),
        _line("统一社会信用代码/纳税人识别号:91500000000000001X", 30, 130),
        _line("壹拾柒圆玖角整", 250, 385),
        # A digits-only remark can look like a tax ID; it must not become a
        # second party just because an amount line sits nearby.
        _line("统一社会信用代码/纳税人识别号:010192218471428", 30, 412),
    ]
    values, confidence, warnings = parser._extract_parties(
        lines, parser.InvoiceType.FULL_DIGITAL_NORMAL, 800
    )
    assert values["buyer_name"] == "甲公司"
    assert values["seller_name"] == "乙公司"
    assert confidence["seller_name"] == "medium"
    assert any("按位置推断" in warning for warning in warnings)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("发票) 电子发 普通 发票号码:265", "full_digital_normal"),
        ("电子发票 专用发票 发票号码:265", "full_digital_special"),
        ("发票号码:265", None),
    ],
)
def test_ocr_type_fallback_uses_title_fragments(text: str, expected: str | None) -> None:
    inferred = parser._infer_ocr_type(text)
    assert (inferred.value if inferred is not None else None) == expected
