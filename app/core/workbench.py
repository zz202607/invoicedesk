"""Read models for the desktop workbench.

The CLI prints text and writes files; the workbench needs rows to render and a
page image to preview. This module keeps those queries out of the bridge so the
API layer stays a thin JSON adapter. Nothing here writes to the database and
nothing touches the source PDFs except to render a page.
"""

from __future__ import annotations

import base64
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import fitz

from app.core.export import (
    TYPE_LABELS,
    confidence_notes,
    parse_confidence,
    parse_warnings,
    redact_paths,
)
from app.core.text import normalize

MAX_PREVIEW_EDGE = 4000  # pixels per side; A4 at zoom 3 is ~2526px tall


class PreviewError(RuntimeError):
    """The requested invoice page could not be rendered."""


@dataclass(frozen=True)
class InvoiceLink:
    report_id: int
    report_month: str


@dataclass(frozen=True)
class InvoiceBrief:
    id: int
    invoice_date: str
    invoice_number: str
    seller_name: str
    amount_total_cents: int | None
    category: str


@dataclass(frozen=True)
class InvoiceRow:
    id: int
    batch_id: int
    batch_label: str
    source_path: str
    source_name: str
    source_exists: bool
    page_index: int
    page_count: int
    invoice_type: str
    type_label: str
    invoice_number: str
    invoice_date: str
    seller_name: str
    buyer_name: str
    amount_total_cents: int | None
    item_summary: str
    category: str
    category_rule: str
    confidence_notes: str
    warning_text: str
    needs_review: bool
    edit_count: int
    links: tuple[InvoiceLink, ...]
    archived_at: str = ""

    @property
    def is_linked(self) -> bool:
        return bool(self.links)


@dataclass(frozen=True)
class InvoiceSummary:
    count: int
    review_count: int
    linked_count: int
    missing_amount_count: int
    total_cents: int


@dataclass(frozen=True)
class CategorySummary:
    category: str
    count: int
    total_cents: int


@dataclass(frozen=True)
class MonthSummary:
    month: str
    count: int
    total_cents: int


@dataclass(frozen=True)
class DashboardSummary:
    count: int
    total_cents: int
    review_count: int
    linked_count: int
    missing_amount_count: int
    missing_source_count: int
    low_confidence_count: int
    edited_count: int
    categories: tuple[CategorySummary, ...]
    months: tuple[MonthSummary, ...]


def _like_pattern(term: str) -> str:
    """Escape LIKE metacharacters so a ``%`` in a search is literal."""
    cleaned = normalize(term).strip()
    escaped = (
        cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def _link_map(
    connection: sqlite3.Connection, invoice_ids: list[int]
) -> dict[int, list[InvoiceLink]]:
    if not invoice_ids:
        return {}
    placeholders = ",".join("?" for _ in invoice_ids)
    rows = connection.execute(
        "SELECT li.invoice_id, r.id AS report_id, r.month"
        " FROM report_line_invoices li"
        " JOIN report_lines l ON l.id = li.line_id"
        " JOIN reports r ON r.id = l.report_id"
        f" WHERE li.invoice_id IN ({placeholders})"
        " ORDER BY r.id",
        tuple(invoice_ids),
    ).fetchall()
    links: dict[int, list[InvoiceLink]] = {}
    seen: set[tuple[int, int]] = set()
    for row in rows:
        key = (row["invoice_id"], row["report_id"])
        if key in seen:
            continue
        seen.add(key)
        links.setdefault(row["invoice_id"], []).append(
            InvoiceLink(report_id=row["report_id"], report_month=row["month"])
        )
    return links


def list_invoices(
    connection: sqlite3.Connection,
    *,
    batch_id: int | None = None,
    buyer: str = "",
    date_from: str = "",
    date_to: str = "",
    include_review: bool = True,
    unlinked_only: bool = False,
    unrecognized_only: bool = False,
    review_only: bool = False,
    search: str = "",
    invoice_id: int | None = None,
    invoice_numbers: Sequence[str] | None = None,
    status: str = "active",
) -> list[InvoiceRow]:
    """Return invoice-pool rows for the workbench, newest first.

    ``status`` selects active rows (default), archived rows, or both. Archived
    records are duplicates the user set aside; they keep their audit trail and
    can be restored, so every default read path must skip them.
    """
    if status not in {"active", "archived", "all"}:
        raise ValueError(f"unknown invoice status: {status}")
    where: list[str] = []
    params: list[object] = []
    if status == "active":
        where.append("i.archived_at = ''")
    elif status == "archived":
        where.append("i.archived_at <> ''")
    if invoice_id is not None:
        where.append("i.id = ?")
        params.append(invoice_id)
    if invoice_numbers is not None:
        numbers = [number for number in invoice_numbers if number]
        if not numbers:
            return []
        where.append(
            f"i.invoice_number IN ({', '.join('?' for _ in numbers)})"
        )
        params.extend(numbers)
    if batch_id is not None:
        where.append("i.batch_id = ?")
        params.append(batch_id)
    if buyer:
        where.append("i.buyer_name = ?")
        params.append(normalize(buyer))
    if date_from:
        where.append("i.invoice_date <> '' AND i.invoice_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("i.invoice_date <> '' AND i.invoice_date <= ?")
        params.append(date_to)
    if not include_review:
        where.append("i.needs_review = 0")
    if unlinked_only:
        where.append(
            "NOT EXISTS (SELECT 1 FROM report_line_invoices li"
            " WHERE li.invoice_id = i.id)"
        )
    if unrecognized_only:
        where.append("i.invoice_type IN ('scanned', 'unknown', 'error')")
    if review_only:
        # The review queue walks these rows one by one; combine with the batch
        # or date filters to scope a single month's cleanup.
        where.append("i.needs_review = 1")
    if search:
        pattern = _like_pattern(search)
        where.append(
            "(i.invoice_number LIKE ? ESCAPE '\\'"
            " OR i.seller_name LIKE ? ESCAPE '\\'"
            " OR i.buyer_name LIKE ? ESCAPE '\\'"
            " OR i.item_summary LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern] * 4)

    sql = (
        "SELECT i.id, i.batch_id, b.label AS batch_label, i.source_path,"
        " i.page_index, i.page_count, i.invoice_type, i.invoice_number,"
        " i.invoice_date, i.seller_name, i.buyer_name, i.amount_total_cents,"
        " i.item_summary, i.category, i.category_rule, i.confidence, i.warnings,"
        " i.needs_review, i.archived_at,"
        " (SELECT COUNT(*) FROM invoice_edits e"
        "  WHERE e.invoice_id = i.id"
        "  AND e.field NOT IN ('review', 'warning', 'category_refresh'))"
        " AS edit_count"
        " FROM invoices i JOIN batches b ON b.id = i.batch_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (
        " ORDER BY CASE WHEN i.invoice_date = '' THEN 1 ELSE 0 END,"
        " i.invoice_date DESC, i.id DESC"
    )
    rows = connection.execute(sql, params).fetchall()
    links = _link_map(connection, [row["id"] for row in rows])
    invoices: list[InvoiceRow] = []
    for row in rows:
        source_path = row["source_path"]
        confidence = parse_confidence(row["confidence"])
        invoices.append(
            InvoiceRow(
                id=row["id"],
                batch_id=row["batch_id"],
                batch_label=normalize(row["batch_label"]),
                source_path=source_path,
                source_name=Path(source_path).name,
                source_exists=Path(source_path).is_file(),
                page_index=row["page_index"],
                page_count=row["page_count"],
                invoice_type=row["invoice_type"],
                type_label=TYPE_LABELS.get(
                    row["invoice_type"], normalize(row["invoice_type"])
                ),
                invoice_number=normalize(row["invoice_number"]),
                invoice_date=row["invoice_date"],
                seller_name=normalize(row["seller_name"]),
                buyer_name=normalize(row["buyer_name"]),
                amount_total_cents=row["amount_total_cents"],
                item_summary=normalize(row["item_summary"]),
                category=normalize(row["category"]),
                category_rule=normalize(row["category_rule"]),
                confidence_notes=confidence_notes(confidence),
                warning_text=redact_paths("；".join(parse_warnings(row["warnings"]))),
                needs_review=bool(row["needs_review"]),
                edit_count=row["edit_count"],
                links=tuple(links.get(row["id"], ())),
                archived_at=row["archived_at"],
            )
        )
    return invoices


def get_invoice_row(
    connection: sqlite3.Connection, invoice_id: int
) -> InvoiceRow | None:
    """Look up one row for detail views, archived records included."""
    rows = list_invoices(connection, invoice_id=invoice_id, status="all")
    return rows[0] if rows else None


@dataclass(frozen=True)
class ConflictGroup:
    """Active invoices that share one non-empty number.

    ``kind`` is ``duplicate`` when two records have the exact same identity
    (date, total, seller) and ``conflict`` otherwise; both need a human pick.
    """

    invoice_number: str
    kind: str
    rows: tuple[InvoiceRow, ...]


def _identity(row: InvoiceRow) -> tuple[str, int | None, str]:
    return (row.invoice_date, row.amount_total_cents, row.seller_name)


def list_conflict_groups(connection: sqlite3.Connection) -> list[ConflictGroup]:
    """Group active records by invoice number, newest groups first."""
    numbers = [
        row["invoice_number"]
        for row in connection.execute(
            "SELECT invoice_number, MAX(invoice_date) AS newest"
            " FROM invoices WHERE invoice_number <> '' AND archived_at = ''"
            " GROUP BY invoice_number HAVING COUNT(*) > 1"
            " ORDER BY CASE WHEN MAX(invoice_date) = '' THEN 1 ELSE 0 END,"
            " newest DESC, invoice_number"
        )
    ]
    if not numbers:
        return []
    grouped: dict[str, list[InvoiceRow]] = {}
    for row in list_invoices(connection, invoice_numbers=numbers):
        grouped.setdefault(row.invoice_number, []).append(row)
    groups: list[ConflictGroup] = []
    for number in numbers:
        rows = grouped.get(number, [])
        if len(rows) < 2:  # pragma: no cover - count and rows agree
            continue
        identities = [_identity(row) for row in rows]
        kind = "duplicate" if len(set(identities)) < len(identities) else "conflict"
        groups.append(
            ConflictGroup(
                invoice_number=number, kind=kind, rows=tuple(rows)
            )
        )
    return groups


def summarize_invoices(rows: list[InvoiceRow]) -> InvoiceSummary:
    return InvoiceSummary(
        count=len(rows),
        review_count=sum(1 for row in rows if row.needs_review),
        linked_count=sum(1 for row in rows if row.is_linked),
        missing_amount_count=sum(1 for row in rows if row.amount_total_cents is None),
        total_cents=sum(row.amount_total_cents or 0 for row in rows),
    )


UNKNOWN_CATEGORY = "未分类"
UNKNOWN_MONTH = "未知日期"


def _invoice_month(value: str) -> str:
    """Return ``YYYY-MM`` for a stored ISO date, else the unknown bucket."""
    if (
        len(value) >= 7
        and value[4] == "-"
        and value[:4].isdigit()
        and value[5:7].isdigit()
    ):
        return value[:7]
    return UNKNOWN_MONTH


def dashboard_summary(rows: list[InvoiceRow]) -> DashboardSummary:
    """Aggregate the current invoice-pool rows for the summary dashboard.

    Missing amounts are counted but excluded from money totals, matching the
    invoice pool summary. Categories and months keep zero-amount rows so a
    count is never hidden by a missing amount.
    """
    summary = summarize_invoices(rows)
    category_groups: dict[str, list[int]] = {}
    month_groups: dict[str, list[int]] = {}
    for row in rows:
        category = row.category or UNKNOWN_CATEGORY
        bucket = category_groups.setdefault(category, [0, 0])
        bucket[0] += 1
        bucket[1] += row.amount_total_cents or 0
        month = _invoice_month(row.invoice_date)
        bucket = month_groups.setdefault(month, [0, 0])
        bucket[0] += 1
        bucket[1] += row.amount_total_cents or 0

    categories = tuple(
        sorted(
            (
                CategorySummary(category=name, count=count, total_cents=total)
                for name, (count, total) in category_groups.items()
            ),
            key=lambda item: (-item.total_cents, -item.count, item.category),
        )
    )
    months = tuple(
        MonthSummary(month=name, count=count, total_cents=total)
        for name, (count, total) in sorted(
            month_groups.items(),
            key=lambda item: (item[0] == UNKNOWN_MONTH, item[0]),
        )
    )
    return DashboardSummary(
        count=summary.count,
        total_cents=summary.total_cents,
        review_count=summary.review_count,
        linked_count=summary.linked_count,
        missing_amount_count=summary.missing_amount_count,
        missing_source_count=sum(1 for row in rows if not row.source_exists),
        low_confidence_count=sum(1 for row in rows if row.confidence_notes),
        edited_count=sum(1 for row in rows if row.edit_count),
        categories=categories,
        months=months,
    )


def invoice_briefs(
    connection: sqlite3.Connection, invoice_ids: tuple[int, ...]
) -> dict[int, InvoiceBrief]:
    """Look up linked-invoice summaries for the report line table."""
    if not invoice_ids:
        return {}
    placeholders = ",".join("?" for _ in invoice_ids)
    rows = connection.execute(
        "SELECT id, invoice_date, invoice_number, seller_name,"
        f" amount_total_cents, category FROM invoices WHERE id IN ({placeholders})",
        tuple(invoice_ids),
    ).fetchall()
    return {
        row["id"]: InvoiceBrief(
            id=row["id"],
            invoice_date=row["invoice_date"],
            invoice_number=normalize(row["invoice_number"]),
            seller_name=normalize(row["seller_name"]),
            amount_total_cents=row["amount_total_cents"],
            category=normalize(row["category"]),
        )
        for row in rows
    }


def invoice_source(
    connection: sqlite3.Connection, invoice_id: int
) -> tuple[str, int, int]:
    row = connection.execute(
        "SELECT source_path, page_index, page_count FROM invoices WHERE id = ?",
        (invoice_id,),
    ).fetchone()
    if row is None:
        raise PreviewError(f"发票不存在：#{invoice_id}")
    return row["source_path"], row["page_index"], row["page_count"]


def preview_data_url(source_path: str, page_index: int, *, zoom: float = 1.25) -> str:
    """Render one invoice page to a data URL for the local preview pane."""
    path = Path(source_path)
    if path.suffix.lower() != ".pdf":
        raise PreviewError("源文件不可预览")
    if not path.is_file():
        raise PreviewError(f"源文件不存在：{path.name}")
    if not 0.5 <= zoom <= 3:
        raise PreviewError("缩放比例超出范围")
    try:
        with fitz.open(path) as document:
            if page_index < 0 or page_index >= document.page_count:
                raise PreviewError(f"页码超出范围：{page_index + 1}")
            page = document[page_index]
            rect = page.rect
            if (
                rect.width * zoom > MAX_PREVIEW_EDGE
                or rect.height * zoom > MAX_PREVIEW_EDGE
            ):
                raise PreviewError("页面过大，无法预览")
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            encoded = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
    except PreviewError:
        raise
    except Exception as exc:  # read-only render; never leak internals to the UI
        raise PreviewError(f"无法读取源文件：{path.name}") from exc
    return f"data:image/png;base64,{encoded}"
