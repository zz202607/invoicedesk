from __future__ import annotations

from pathlib import Path

from app.core.models import InvoiceType
from app.core.parser import parse_pdf
from tests.pdf_factory import full_digital_lines, make_pdf, make_pdf_pages


def test_multi_invoice_page_is_flagged(tmp_path: Path) -> None:
    lines = full_digital_lines() + [
        (430, 220, "发票号码:"),
        (480, 220, "26502000000000000002"),
        (410, 330, "(小写)"),
        (450, 330, "¥ 1.00"),
    ]
    records = parse_pdf(make_pdf(tmp_path, lines))
    assert len(records) == 1
    assert records[0].page_index == 0
    assert records[0].page_count == 1
    assert "页面可能包含多张发票，需人工拆分" in records[0].warnings


def test_multi_page_pdf_yields_one_record_per_page(tmp_path: Path) -> None:
    path = make_pdf_pages(tmp_path, [full_digital_lines(), full_digital_lines()])
    records = parse_pdf(path)
    assert len(records) == 2
    assert [record.page_index for record in records] == [0, 1]
    assert {record.page_count for record in records} == {2}
    assert [record.to_dict()["page"] for record in records] == [1, 2]


def test_non_invoice_document_is_flagged(tmp_path: Path) -> None:
    lines = [
        (20, 30, "这是一份费用报销明细说明文档。"),
        (20, 50, "它不是发票，请不要计入发票金额。"),
    ]
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.invoice_type is InvoiceType.UNKNOWN
    assert invoice.needs_review is True
    assert "未识别票型" in invoice.warnings
