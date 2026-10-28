"""Query the invoice pool for export.

This phase exports at invoice level. Reimbursement rows (one row may link to
zero or more invoices) arrive in Phase 2, so the exports here are the evidence
package: invoice detail with traceability, category/company summaries, and a
2-up A4 print PDF.

Amounts are summed as integer cents; only the final presentation converts to
Decimal or float.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from app.core.models import InvoiceType
from app.core.text import normalize

# Warnings are parser/import free text. Redact path-shaped fragments so an
# exported workbook that may be submitted never carries local directories.
# Paths can contain spaces ("C:\\Users\\Zhang San\\a.pdf"), so redact to the
# end of the warning segment rather than stopping at the first space. Warnings
# are joined with "；" after redaction, so this cannot swallow a second warning.
_PATH_PATTERNS = (
    re.compile(r"[A-Za-z]:[\\/][^\n；;]*"),
    re.compile(r"\\\\[^\n；;]*"),
    re.compile(r"/(?:Users|home|root)/[^\n；;]*"),
)
_PATH_PLACEHOLDER = "<路径已隐藏>"


def redact_paths(text: str) -> str:
    for pattern in _PATH_PATTERNS:
        text = pattern.sub(_PATH_PLACEHOLDER, text)
    return text

TYPE_LABELS: dict[str, str] = {
    InvoiceType.FULL_DIGITAL_NORMAL.value: "全电普票",
    InvoiceType.FULL_DIGITAL_SPECIAL.value: "全电专票",
    InvoiceType.VAT_ELECTRONIC.value: "增值税电子普票",
    InvoiceType.TOLL.value: "通行费",
    InvoiceType.TRAIN.value: "铁路客票",
    InvoiceType.AIR_ITINERARY.value: "航空行程单",
    InvoiceType.TAXI.value: "出租车/网约车",
    InvoiceType.UNKNOWN.value: "未识别",
    InvoiceType.SCANNED.value: "扫描件",
    InvoiceType.ERROR.value: "解析失败",
}

CONFIDENCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("invoice_type", "票型"),
    ("invoice_number", "票号"),
    ("invoice_date", "日期"),
    ("seller_name", "销售方"),
    ("buyer_name", "购买方"),
    ("amount_total", "金额"),
    ("item", "项目"),
    ("category", "分类"),
)
CONFIDENCE_LEVELS = {"high": "高", "medium": "中", "low": "低"}

class ExportError(RuntimeError):
    """An export failed for a reason the CLI can report without a traceback."""


@dataclass(frozen=True)
class ExportFilters:
    batch_id: int | None = None
    buyer: str = ""
    date_from: str = ""
    date_to: str = ""
    include_review: bool = True

    def describe(self) -> str:
        parts: list[str] = []
        if self.batch_id is not None:
            parts.append(f"批次 #{self.batch_id}")
        if self.buyer:
            parts.append(f"购买方「{normalize(self.buyer)}」")
        if self.date_from and self.date_to:
            parts.append(f"开票日期 {self.date_from} 至 {self.date_to}")
        elif self.date_from:
            parts.append(f"开票日期 ≥ {self.date_from}")
        elif self.date_to:
            parts.append(f"开票日期 ≤ {self.date_to}")
        if not self.include_review:
            parts.append("排除待确认")
        return "；".join(parts) if parts else "全部发票"

    def review_mode(self) -> str:
        return "包含，并在明细中标记" if self.include_review else "已排除"


@dataclass(frozen=True)
class ExportRow:
    id: int
    batch_id: int
    source_path: str
    page_index: int
    page_count: int
    invoice_type: str
    invoice_number: str
    invoice_date: str
    seller_name: str
    buyer_name: str
    amount_net_cents: int | None
    tax_amount_cents: int | None
    amount_total_cents: int | None
    item_summary: str
    category: str
    confidence: dict[str, str]
    warnings: tuple[str, ...]
    needs_review: bool

    @property
    def confidence_notes(self) -> str:
        """Fields the parser was unsure about, e.g. ``销售方(中)；分类(中)``."""
        return confidence_notes(self.confidence)

    @property
    def source_name(self) -> str:
        """File name only: exported sheets may be submitted outside this machine."""
        normalized = self.source_path.replace("\\", "/")
        return normalized.rsplit("/", 1)[-1]

    @property
    def page_label(self) -> str:
        return f"{self.page_index + 1}/{self.page_count or 1}"

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.invoice_type, self.invoice_type)

    @property
    def warning_text(self) -> str:
        return "；".join(redact_paths(warning) for warning in self.warnings)


@dataclass(frozen=True)
class ExportSummary:
    count: int
    review_count: int
    missing_amount_count: int
    net_cents: int
    tax_cents: int
    total_cents: int
    unique_pages: int


def confidence_notes(confidence: dict[str, str]) -> str:
    """Fields the parser was unsure about, e.g. ``销售方(中)；分类(中)``."""
    notes: list[str] = []
    for field, label in CONFIDENCE_FIELDS:
        level = confidence.get(field, "")
        if level in {"medium", "low"}:
            notes.append(f"{label}({CONFIDENCE_LEVELS.get(level, level)})")
    return "；".join(notes)


def parse_warnings(raw: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(normalize(str(item)) for item in parsed)


def parse_confidence(raw: str) -> dict[str, str]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value) for key, value in parsed.items()}


def collect_rows(connection: sqlite3.Connection, filters: ExportFilters) -> list[ExportRow]:
    """Return matching invoices in a stable, print-friendly order."""
    where: list[str] = []
    params: list[object] = []
    if filters.batch_id is not None:
        where.append("batch_id = ?")
        params.append(filters.batch_id)
    if filters.buyer:
        where.append("buyer_name = ?")
        params.append(normalize(filters.buyer))
    if filters.date_from:
        # An invoice without a date cannot prove it falls inside the range;
        # including it would silently widen the filter.
        where.append("invoice_date <> '' AND invoice_date >= ?")
        params.append(filters.date_from)
    if filters.date_to:
        where.append("invoice_date <> '' AND invoice_date <= ?")
        params.append(filters.date_to)
    if not filters.include_review:
        where.append("needs_review = 0")

    sql = (
        "SELECT id, batch_id, source_path, page_index, page_count, invoice_type,"
        " invoice_number, invoice_date, seller_name, buyer_name,"
        " amount_net_cents, tax_amount_cents, amount_total_cents,"
        " item_summary, category, confidence, warnings, needs_review"
        " FROM invoices"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (
        " ORDER BY CASE WHEN invoice_date = '' THEN 1 ELSE 0 END,"
        " invoice_date, invoice_number, source_path, page_index, id"
    )
    rows = connection.execute(sql, params).fetchall()
    return [
        ExportRow(
            id=row["id"],
            batch_id=row["batch_id"],
            source_path=row["source_path"],
            page_index=row["page_index"],
            page_count=row["page_count"],
            invoice_type=row["invoice_type"],
            invoice_number=normalize(row["invoice_number"]),
            invoice_date=row["invoice_date"],
            seller_name=normalize(row["seller_name"]),
            buyer_name=normalize(row["buyer_name"]),
            amount_net_cents=row["amount_net_cents"],
            tax_amount_cents=row["tax_amount_cents"],
            amount_total_cents=row["amount_total_cents"],
            item_summary=normalize(row["item_summary"]),
            category=normalize(row["category"]),
            confidence=parse_confidence(row["confidence"]),
            warnings=parse_warnings(row["warnings"]),
            needs_review=bool(row["needs_review"]),
        )
        for row in rows
    ]


def summarize(rows: list[ExportRow]) -> ExportSummary:
    return ExportSummary(
        count=len(rows),
        review_count=sum(1 for row in rows if row.needs_review),
        missing_amount_count=sum(1 for row in rows if row.amount_total_cents is None),
        net_cents=sum(row.amount_net_cents or 0 for row in rows),
        tax_cents=sum(row.tax_amount_cents or 0 for row in rows),
        total_cents=sum(row.amount_total_cents or 0 for row in rows),
        unique_pages=len({(row.source_path, row.page_index) for row in rows}),
    )
