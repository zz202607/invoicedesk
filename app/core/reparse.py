"""Re-run recognition on records imported before OCR support.

Files are deduplicated by content hash, so re-importing the same folder does
not touch an existing "scanned" row. This module lets the workbench retry those
rows with the bundled OCR engine while leaving source PDFs and manual edits
alone.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.core import db, learning
from app.core.importer import CONFLICT_WARNING
from app.core.models import InvoiceType, ParsedInvoice
from app.core.parser import parse_pdf
from app.core.text import normalize

TARGET_TYPES = ("scanned", "unknown", "error")


@dataclass(frozen=True)
class ReparseResult:
    candidates: int
    reparsed: int
    failed: int
    skipped_edited: int
    skipped_missing: int
    conflicts: int


def _usable(invoice: ParsedInvoice) -> bool:
    if invoice.invoice_type in {InvoiceType.SCANNED, InvoiceType.ERROR}:
        return False
    return bool(
        invoice.invoice_number
        or invoice.invoice_date
        or invoice.seller_name
        or invoice.amount_total is not None
    )


def _existing_warnings(raw: str) -> list[str]:
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    # Drop the old "no text layer" notices: the reparse replaces them with its
    # own OCR warning or a fresh failure reason.
    return [
        normalize(str(value))
        for value in values
        if str(value) and not str(value).startswith("无文本层")
    ]


def _merge_warnings(existing: list[str], parsed: list[str]) -> list[str]:
    merged: list[str] = []
    for warning in (*parsed, *existing):
        if warning and warning not in merged:
            merged.append(warning)
    return merged


def reparse_unrecognized(
    connection: sqlite3.Connection,
    *,
    invoice_ids: list[int] | None = None,
) -> ReparseResult:
    """Retry OCR for unrecognized invoices and update them in place.

    Invoices with manual corrections are skipped so a reparse can never
    overwrite a human decision. A reparse that produces a number already in
    the pool keeps both records and flags the conflict instead of deleting.
    """
    where = [f"invoice_type IN ({', '.join('?' for _ in TARGET_TYPES)})"]
    params: list[object] = list(TARGET_TYPES)
    if invoice_ids:
        where.append(f"id IN ({', '.join('?' for _ in invoice_ids)})")
        params.extend(invoice_ids)
    rows = connection.execute(
        "SELECT id, source_path, page_index, warnings FROM invoices"
        f" WHERE {' AND '.join(where)} ORDER BY id",
        params,
    ).fetchall()
    if not rows:
        return ReparseResult(0, 0, 0, 0, 0, 0)

    edited = {
        row["invoice_id"]
        for row in connection.execute("SELECT DISTINCT invoice_id FROM invoice_edits")
    }
    rules = learning.seller_rules(connection)
    parsed_cache: dict[str, list[ParsedInvoice]] = {}
    reparsed = failed = skipped_edited = skipped_missing = conflicts = 0

    with connection:
        for row in rows:
            invoice_id = row["id"]
            if invoice_id in edited:
                skipped_edited += 1
                continue
            source = Path(row["source_path"])
            if not source.is_file():
                skipped_missing += 1
                continue
            key = str(source)
            if key not in parsed_cache:
                try:
                    parsed_cache[key] = parse_pdf(key)
                except Exception:  # noqa: BLE001 - one bad file must not abort the batch
                    parsed_cache[key] = []
            pages = parsed_cache[key]
            index = row["page_index"]
            invoice = pages[index] if 0 <= index < len(pages) else None
            if invoice is None or not _usable(invoice):
                failed += 1
                continue

            rule_key = learning.apply_seller_rules(invoice, rules)
            warnings = _merge_warnings(
                _existing_warnings(row["warnings"]), invoice.warnings
            )
            number = invoice.invoice_number
            conflict = False
            if number:
                others = connection.execute(
                    "SELECT id FROM invoices WHERE invoice_number = ? AND id <> ?",
                    (number, invoice_id),
                ).fetchall()
                if others:
                    conflict = True
                    conflicts += 1
                    if CONFLICT_WARNING not in warnings:
                        warnings.append(CONFLICT_WARNING)
                    for other in others:
                        db.append_warning(connection, other["id"], CONFLICT_WARNING)

            needs_review = int(invoice.needs_review or conflict)
            connection.execute(
                """
                UPDATE invoices SET
                    invoice_type = ?, invoice_number = ?, invoice_date = ?,
                    seller_name = ?, seller_tax_id = ?, buyer_name = ?, buyer_tax_id = ?,
                    amount_total_cents = ?, amount_net_cents = ?, tax_amount_cents = ?,
                    item_summary = ?, category = ?, category_rule = ?,
                    confidence = ?, warnings = ?, needs_review = ?
                WHERE id = ?
                """,
                (
                    invoice.invoice_type.value,
                    number,
                    invoice.invoice_date,
                    normalize(invoice.seller_name),
                    invoice.seller_tax_id,
                    normalize(invoice.buyer_name),
                    invoice.buyer_tax_id,
                    db.money_to_cents(invoice.amount_total),
                    db.money_to_cents(invoice.amount_net),
                    db.money_to_cents(invoice.tax_amount),
                    normalize(invoice.item_summary),
                    invoice.category,
                    invoice.category_rule,
                    json.dumps(invoice.confidence, ensure_ascii=False, sort_keys=True),
                    json.dumps(warnings, ensure_ascii=False),
                    needs_review,
                    invoice_id,
                ),
            )
            if rule_key:
                learning.record_hit(connection, rule_key)
            reparsed += 1

    return ReparseResult(
        candidates=len(rows),
        reparsed=reparsed,
        failed=failed,
        skipped_edited=skipped_edited,
        skipped_missing=skipped_missing,
        conflicts=conflicts,
    )
