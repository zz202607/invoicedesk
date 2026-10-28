"""Read models for the desktop workbench.

The CLI prints text and writes files; the workbench needs rows to render and a
page image to preview. This module keeps those queries out of the bridge so the
API layer stays a thin JSON adapter. Nothing here writes to the database and
nothing touches the source PDFs except to render a page.
"""

from __future__ import annotations

import base64
import sqlite3
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
    search: str = "",
    invoice_id: int | None = None,
) -> list[InvoiceRow]:
    """Return invoice-pool rows for the workbench, newest first."""
    where: list[str] = []
    params: list[object] = []
    if invoice_id is not None:
        where.append("i.id = ?")
        params.append(invoice_id)
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
        " i.needs_review,"
        " (SELECT COUNT(*) FROM invoice_edits e"
        "  WHERE e.invoice_id = i.id"
        "  AND e.field NOT IN ('review', 'warning')) AS edit_count"
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
            )
        )
    return invoices


def get_invoice_row(
    connection: sqlite3.Connection, invoice_id: int
) -> InvoiceRow | None:
    rows = list_invoices(connection, invoice_id=invoice_id)
    return rows[0] if rows else None


def summarize_invoices(rows: list[InvoiceRow]) -> InvoiceSummary:
    return InvoiceSummary(
        count=len(rows),
        review_count=sum(1 for row in rows if row.needs_review),
        linked_count=sum(1 for row in rows if row.is_linked),
        missing_amount_count=sum(1 for row in rows if row.amount_total_cents is None),
        total_cents=sum(row.amount_total_cents or 0 for row in rows),
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
