"""Synthetic invoices and reports for the reimbursement tests."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

from app.core import db, reports
from app.core.models import InvoiceType, ParsedInvoice


def make_db(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(tmp_path / "reports.db")


def sample_invoice(
    *,
    source_path: str = r"D:\invoices\a.pdf",
    number: str = "26502000000000000001",
    date: str = "2026-07-09",
    seller: str = "示例销售方",
    item: str = "办公用品",
    amount: Decimal | None = Decimal("120.00"),
    category: str = "办公费用",
    category_rule: str = "test",
    buyer: str = "示例公司",
    invoice_type: InvoiceType = InvoiceType.FULL_DIGITAL_NORMAL,
    confidence: dict[str, str] | None = None,
) -> ParsedInvoice:
    return ParsedInvoice(
        source_path=source_path,
        invoice_type=invoice_type,
        invoice_number=number,
        invoice_date=date,
        seller_name=seller,
        seller_tax_id="92500000TEST00002X",
        buyer_name=buyer,
        buyer_tax_id="91500000TEST00001X",
        amount_total=amount,
        amount_net=None,
        tax_amount=None,
        item_summary=item,
        category=category,
        category_rule=category_rule,
        confidence=confidence or {"amount_total": "high"},
        warnings=[],
    )


def add_invoice(
    connection: sqlite3.Connection,
    *,
    batch_id: int | None = None,
    index: int = 1,
    review: bool = False,
    **overrides,
) -> int:
    invoice = sample_invoice(**overrides)
    if review:
        invoice.warnings.append("测试待确认")
    if batch_id is None:
        batch_id = db.create_batch(
            connection, label=f"批次 {index}", source_dir=r"D:\invoices"
        )
    sha = f"test-sha-{index}-{invoice.invoice_number}"
    db.insert_file(
        connection,
        sha256=sha,
        batch_id=batch_id,
        source_path=invoice.source_path,
        page_count=1,
    )
    invoice_id = db.insert_invoice(
        connection, batch_id=batch_id, sha256=sha, invoice=invoice
    )
    connection.commit()
    return invoice_id


def make_report(
    connection: sqlite3.Connection,
    *,
    company: str = "示例公司",
    reporter: str = "张三",
    month: str = "2026-07",
    department: str = "示例部门",
    title: str = "",
) -> int:
    return reports.create_report(
        connection,
        company=company,
        reporter=reporter,
        month=month,
        department=department,
        title=title,
    )


def add_line(
    connection: sqlite3.Connection,
    report_id: int,
    *,
    line_date: str = "2026-07-09",
    category: str = "办公费用",
    description: str = "测试费用",
    amount_cents: int = 12000,
    **kwargs,
) -> int:
    return reports.add_line(
        connection,
        report_id,
        line_date=line_date,
        category=category,
        description=description,
        amount_cents=amount_cents,
        **kwargs,
    )
