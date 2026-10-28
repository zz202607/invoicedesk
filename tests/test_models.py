from __future__ import annotations

import json
from decimal import Decimal

from app.core.models import InvoiceType, ParsedInvoice
from app.core.parser import summarize


def test_to_dict_is_json_safe_and_complete() -> None:
    invoice = ParsedInvoice(
        source_path="invoice.pdf",
        invoice_type=InvoiceType.FULL_DIGITAL_NORMAL,
        invoice_number="26502000000000000001",
        invoice_date="2026-07-30",
        seller_name="示例乙酒店",
        seller_tax_id="92500000TEST00002X",
        buyer_tax_id="91500000TEST00001X",
        amount_total=Decimal("1060.00"),
        confidence={"amount_total": "high"},
    )
    data = invoice.to_dict()

    assert data["amount_total"] == "1060.00"
    assert data["needs_review"] is False
    assert data["page"] == 1
    assert data["source_name"] == "invoice.pdf"
    assert data["seller_tax_id"] != "92500000TEST00002X"
    assert "*" in data["seller_tax_id"]
    assert "source_path" not in data
    assert data["confidence"]["amount_total"] == "high"
    assert json.loads(json.dumps(data))["invoice_number"] == "26502000000000000001"
    full = invoice.to_dict(include_tax_ids=True)
    assert full["seller_tax_id"] == "92500000TEST00002X"
    assert full["buyer_tax_id"] == "91500000TEST00001X"


def test_to_dict_can_include_sanitized_absolute_path() -> None:
    invoice = ParsedInvoice(
        source_path="C:\\tmp\\bad\x1b[2J.pdf",
        invoice_type=InvoiceType.SCANNED,
    )
    assert invoice.to_dict()["source_name"] == "bad[2J.pdf"
    with_path = invoice.to_dict(include_absolute_path=True)
    assert "source_path" in with_path
    assert "\x1b" not in with_path["source_path"]


def test_needs_review_on_warning() -> None:
    invoice = ParsedInvoice(
        source_path="invoice.pdf",
        invoice_type=InvoiceType.FULL_DIGITAL_NORMAL,
        invoice_number="1",
        invoice_date="2026-07-30",
        amount_total=Decimal("1.00"),
        warnings=["测试警告"],
    )
    assert invoice.needs_review is True


def test_summarize_counts_and_sums_only_present_amounts() -> None:
    valid = ParsedInvoice(
        source_path="a.pdf",
        invoice_type=InvoiceType.TAXI,
        invoice_number="1",
        invoice_date="2026-07-30",
        amount_total=Decimal("100.00"),
    )
    warned = ParsedInvoice(
        source_path="b.pdf",
        invoice_type=InvoiceType.TAXI,
        invoice_number="2",
        invoice_date="2026-07-30",
        amount_total=Decimal("50.00"),
        warnings=["需确认"],
    )
    missing = ParsedInvoice(source_path="c.pdf", invoice_type=InvoiceType.SCANNED)

    summary = summarize([valid, warned, missing])
    assert summary.total == 3
    assert summary.review == 2
    assert summary.amount == Decimal("150.00")
