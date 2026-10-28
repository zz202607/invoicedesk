"""Reimbursement reports: header rows, expense lines, and invoice links.

A report is what gets submitted; invoices are evidence. A line may link zero or
more invoices, so mileage, meals without receipts, and transfers are
first-class rows (ADR-003). Amounts stay in integer cents; this module is the
only place that parses user-entered money and distances.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.core import db
from app.core.categories import DEFAULT_CATEGORY
from app.core.text import normalize

CENT = Decimal("0.01")
KM_STEP = Decimal("0.01")
DEFAULT_MILEAGE_RATE = Decimal("1.5")
LINE_SOURCES = {"manual", "invoice", "mileage"}
_MONTH_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


class ReportError(ValueError):
    """A report operation failed for a reason the CLI can print."""


@dataclass(frozen=True)
class Report:
    id: int
    company: str
    department: str
    reporter: str
    month: str
    title: str
    note: str
    created_at: str
    line_count: int = 0
    amount_cents: int = 0

    @property
    def amount(self) -> Decimal:
        return db.cents_to_money(self.amount_cents) or Decimal("0.00")


@dataclass(frozen=True)
class ReportLine:
    id: int
    report_id: int
    line_date: str
    category: str
    description: str
    amount_cents: int
    has_invoice: bool
    invoice_note: str
    project_name: str
    source: str
    mileage_km: float | None
    invoice_ids: tuple[int, ...] = ()
    invoice_total_cents: int | None = None

    @property
    def amount(self) -> Decimal:
        return db.cents_to_money(self.amount_cents) or Decimal("0.00")

    @property
    def invoice_sum_known(self) -> bool:
        """True when every linked invoice has an amount."""
        return bool(self.invoice_ids) and self.invoice_total_cents is not None

    @property
    def amount_mismatch(self) -> bool:
        return self.invoice_sum_known and self.invoice_total_cents != self.amount_cents


@dataclass(frozen=True)
class FromInvoicesResult:
    added: int
    skipped_linked: int
    skipped_missing_amount: int
    skipped_missing_date: int

    @property
    def skipped(self) -> int:
        return self.skipped_linked + self.skipped_missing_amount + self.skipped_missing_date


def _clean_text(value: str) -> str:
    """Clean user-authored text without NFKC-folding its punctuation.

    Report lines are typed or copied by the user; full-width parentheses and
    spacing are part of what gets submitted. Control/format characters are
    dropped so terminal output cannot be repainted.
    """
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = "".join(
        char for char in text if unicodedata.category(char) not in {"Cc", "Cf", "Zl", "Zp"}
    )
    return text.strip()


def parse_amount(text: str) -> Decimal:
    """Parse a user-entered amount with at most two decimal places."""
    cleaned = normalize(str(text)).strip().replace(",", "").replace("¥", "").replace("￥", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ReportError(f"金额格式不正确：{text}") from exc
    if not value.is_finite():
        raise ReportError(f"金额格式不正确：{text}")
    if value.quantize(CENT, rounding=ROUND_HALF_UP) != value:
        raise ReportError(f"金额最多两位小数：{text}")
    return value


def parse_km(text: str) -> Decimal:
    """Parse a distance in kilometers (up to two decimals, must be positive)."""
    cleaned = normalize(str(text)).replace("km", "").replace("公里", "").strip()
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ReportError(f"里程格式不正确：{text}") from exc
    if not value.is_finite() or value <= 0:
        raise ReportError(f"里程必须是大于 0 的数字：{text}")
    if value.quantize(KM_STEP, rounding=ROUND_HALF_UP) != value:
        raise ReportError(f"里程最多两位小数：{text}")
    return value


def mileage_cents(km: Decimal, rate: Decimal = DEFAULT_MILEAGE_RATE) -> int:
    if rate <= 0:
        raise ReportError("里程单价必须大于 0")
    cents = (km * rate).quantize(CENT, rounding=ROUND_HALF_UP) * 100
    return int(cents)


def normalize_month(value: str) -> str:
    text = normalize(value).strip()
    if not _MONTH_RE.fullmatch(text):
        raise ReportError(f"月份格式应为 YYYY-MM：{value}")
    return text


def normalize_line_date(value: str) -> str:
    try:
        return datetime.strptime(normalize(value).strip(), "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ReportError(f"日期格式应为 YYYY-MM-DD：{value}") from exc


def default_title(company: str, month: str) -> str:
    return f"{company}{int(month.rsplit('-', 1)[-1])}月报销费用支出明细表"


def _ensure_report(connection: sqlite3.Connection, report_id: int) -> None:
    row = connection.execute("SELECT 1 FROM reports WHERE id = ?", (report_id,)).fetchone()
    if row is None:
        raise ReportError(f"报销单不存在：#{report_id}")


def create_report(
    connection: sqlite3.Connection,
    *,
    company: str,
    reporter: str,
    month: str,
    department: str = "",
    title: str = "",
    note: str = "",
    created_at: str | None = None,
) -> int:
    company = _clean_text(company)
    reporter = _clean_text(reporter)
    if not company:
        raise ReportError("公司不能为空")
    if not reporter:
        raise ReportError("报销人不能为空")
    month = normalize_month(month)
    title = _clean_text(title) or default_title(company, month)
    with connection:
        cursor = connection.execute(
            "INSERT INTO reports"
            " (company, department, reporter, month, title, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                company,
                _clean_text(department),
                reporter,
                month,
                title,
                _clean_text(note),
                created_at or db.utc_now(),
            ),
        )
    report_id = cursor.lastrowid
    if report_id is None:  # pragma: no cover - sqlite always returns a row id here
        raise RuntimeError("无法创建报销单")
    return report_id


_REPORT_SELECT = """
SELECT r.id, r.company, r.department, r.reporter, r.month,
       r.title, r.note, r.created_at,
       (SELECT COUNT(*) FROM report_lines l WHERE l.report_id = r.id) AS line_count,
       (SELECT COALESCE(SUM(l.amount_cents), 0) FROM report_lines l
        WHERE l.report_id = r.id) AS amount_cents
FROM reports r
"""


def _report_from_row(row: sqlite3.Row) -> Report:
    return Report(
        id=row["id"],
        company=row["company"],
        department=row["department"],
        reporter=row["reporter"],
        month=row["month"],
        title=row["title"],
        note=row["note"],
        created_at=row["created_at"],
        line_count=row["line_count"],
        amount_cents=row["amount_cents"],
    )


def get_report(connection: sqlite3.Connection, report_id: int) -> Report:
    row = connection.execute(_REPORT_SELECT + " WHERE r.id = ?", (report_id,)).fetchone()
    if row is None:
        raise ReportError(f"报销单不存在：#{report_id}")
    return _report_from_row(row)


def list_reports(connection: sqlite3.Connection) -> list[Report]:
    rows = connection.execute(_REPORT_SELECT + " ORDER BY r.id DESC").fetchall()
    return [_report_from_row(row) for row in rows]


def update_report(
    connection: sqlite3.Connection,
    report_id: int,
    *,
    company: str | None = None,
    reporter: str | None = None,
    month: str | None = None,
    department: str | None = None,
    title: str | None = None,
    note: str | None = None,
) -> None:
    """Edit report header fields; omitted fields stay unchanged."""
    current = get_report(connection, report_id)
    fields: dict[str, str] = {}
    if company is not None:
        cleaned = _clean_text(company)
        if not cleaned:
            raise ReportError("公司不能为空")
        fields["company"] = cleaned
    if reporter is not None:
        cleaned = _clean_text(reporter)
        if not cleaned:
            raise ReportError("报销人不能为空")
        fields["reporter"] = cleaned
    if month is not None:
        fields["month"] = normalize_month(month)
    if department is not None:
        fields["department"] = _clean_text(department)
    if note is not None:
        fields["note"] = _clean_text(note)
    effective_company = fields.get("company", current.company)
    effective_month = fields.get("month", current.month)
    header_changed = (effective_company, effective_month) != (
        current.company,
        current.month,
    )
    auto_title = current.title == default_title(current.company, current.month)
    if title is not None:
        cleaned = _clean_text(title)
        if not cleaned or (cleaned == current.title and header_changed and auto_title):
            # A cleared title, or the UI echoing the old auto title while the
            # company/month changed, both mean "regenerate from the header".
            fields["title"] = default_title(effective_company, effective_month)
        else:
            fields["title"] = cleaned
    elif header_changed and auto_title:
        fields["title"] = default_title(effective_company, effective_month)
    if not fields:
        raise ReportError("没有要修改的内容")
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with connection:
        connection.execute(
            f"UPDATE reports SET {assignments} WHERE id = ?",
            (*fields.values(), report_id),
        )


def _line_id(connection: sqlite3.Connection, report_id: int, sequence: int) -> int:
    _ensure_report(connection, report_id)
    if sequence < 1:
        raise ReportError("行号必须 >= 1")
    row = connection.execute(
        "SELECT id FROM report_lines WHERE report_id = ? ORDER BY id LIMIT 1 OFFSET ?",
        (report_id, sequence - 1),
    ).fetchone()
    if row is None:
        raise ReportError(f"报销单 #{report_id} 没有第 {sequence} 行")
    return row["id"]


def _validated_line_fields(
    *,
    line_date: str | None = None,
    category: str | None = None,
    description: str | None = None,
    amount_cents: int | None = None,
    has_invoice: bool | None = None,
    invoice_note: str | None = None,
    project_name: str | None = None,
    source: str | None = None,
) -> dict[str, object]:
    fields: dict[str, object] = {}
    if line_date is not None:
        fields["line_date"] = normalize_line_date(line_date)
    if category is not None:
        category = _clean_text(category)
        if not category:
            raise ReportError("费用名称不能为空")
        fields["category"] = category
    if description is not None:
        fields["description"] = _clean_text(description)
    if amount_cents is not None:
        if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
            raise ReportError("金额必须是整数分")
        fields["amount_cents"] = amount_cents
    if has_invoice is not None:
        fields["has_invoice"] = int(has_invoice)
    if invoice_note is not None:
        fields["invoice_note"] = _clean_text(invoice_note)
    if project_name is not None:
        fields["project_name"] = _clean_text(project_name)
    if source is not None:
        if source not in LINE_SOURCES:
            raise ReportError(f"未知的行来源：{source}")
        fields["source"] = source
    return fields


def add_line(
    connection: sqlite3.Connection,
    report_id: int,
    *,
    line_date: str,
    category: str,
    description: str,
    amount_cents: int,
    has_invoice: bool = False,
    invoice_note: str = "",
    project_name: str = "",
    source: str = "manual",
    mileage_km: float | None = None,
    invoice_ids: Sequence[int] = (),
    created_at: str | None = None,
) -> int:
    """Insert one line, optionally linking invoices in the same transaction."""
    _ensure_report(connection, report_id)
    ids = _dedupe_ids(invoice_ids) if invoice_ids else []
    fields = _validated_line_fields(
        line_date=line_date,
        category=category or DEFAULT_CATEGORY,
        description=description,
        amount_cents=amount_cents,
        has_invoice=has_invoice or bool(ids),
        invoice_note=invoice_note,
        project_name=project_name,
        source=source,
    )
    with connection:
        if ids:
            _ensure_invoices_exist(connection, ids)
        cursor = connection.execute(
            "INSERT INTO report_lines"
            " (report_id, line_date, category, description, amount_cents,"
            "  has_invoice, invoice_note, project_name, source, mileage_km, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report_id,
                fields["line_date"],
                fields["category"],
                fields["description"],
                fields["amount_cents"],
                fields["has_invoice"],
                fields["invoice_note"],
                fields["project_name"],
                fields["source"],
                mileage_km,
                created_at or db.utc_now(),
            ),
        )
        line_id = cursor.lastrowid
        if line_id is None:  # pragma: no cover - sqlite always returns a row id here
            raise RuntimeError("无法添加报销行")
        for invoice_id in ids:
            connection.execute(
                "INSERT INTO report_line_invoices (line_id, invoice_id) VALUES (?, ?)",
                (line_id, invoice_id),
            )
    return line_id


def _ensure_invoices_exist(connection: sqlite3.Connection, ids: Sequence[int]) -> None:
    placeholders = ",".join("?" for _ in ids)
    existing = {
        row["id"]
        for row in connection.execute(
            f"SELECT id FROM invoices WHERE id IN ({placeholders})", tuple(ids)
        )
    }
    missing = [invoice_id for invoice_id in ids if invoice_id not in existing]
    if missing:
        shown = "、".join(f"#{invoice_id}" for invoice_id in missing[:5])
        raise ReportError(f"发票不存在：{shown}")


def add_mileage(
    connection: sqlite3.Connection,
    report_id: int,
    *,
    line_date: str,
    km: Decimal,
    description: str,
    category: str = "汽车费用",
    rate: Decimal = DEFAULT_MILEAGE_RATE,
    project_name: str = "",
) -> tuple[int, int]:
    """Add a mileage line; returns ``(line_id, amount_cents)``."""
    amount_cents = mileage_cents(km, rate)
    line_id = add_line(
        connection,
        report_id,
        line_date=line_date,
        category=category,
        description=description,
        amount_cents=amount_cents,
        has_invoice=False,
        project_name=project_name,
        source="mileage",
        mileage_km=float(km),
    )
    return line_id, amount_cents


_UNSET = object()


def update_line(
    connection: sqlite3.Connection,
    report_id: int,
    sequence: int,
    *,
    line_date: Any = _UNSET,
    category: Any = _UNSET,
    description: Any = _UNSET,
    amount_cents: Any = _UNSET,
    has_invoice: Any = _UNSET,
    invoice_note: Any = _UNSET,
    project_name: Any = _UNSET,
) -> None:
    line_id = _line_id(connection, report_id, sequence)
    fields = _validated_line_fields(
        line_date=None if line_date is _UNSET else line_date,
        category=None if category is _UNSET else category,
        description=None if description is _UNSET else description,
        amount_cents=None if amount_cents is _UNSET else amount_cents,
        has_invoice=None if has_invoice is _UNSET else has_invoice,
        invoice_note=None if invoice_note is _UNSET else invoice_note,
        project_name=None if project_name is _UNSET else project_name,
    )
    if not fields:
        raise ReportError("没有要修改的内容")
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with connection:
        connection.execute(
            f"UPDATE report_lines SET {assignments} WHERE id = ?",
            (*fields.values(), line_id),
        )


def delete_line(connection: sqlite3.Connection, report_id: int, sequence: int) -> None:
    line_id = _line_id(connection, report_id, sequence)
    with connection:
        connection.execute("DELETE FROM report_lines WHERE id = ?", (line_id,))


def _dedupe_ids(invoice_ids: Sequence[int]) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for invoice_id in invoice_ids:
        if isinstance(invoice_id, bool):
            raise ReportError("发票编号必须是整数")
        try:
            value = int(invoice_id)
        except (TypeError, ValueError) as exc:
            raise ReportError(f"发票编号必须是整数：{invoice_id}") from exc
        if value < 1:
            raise ReportError("发票编号必须 >= 1")
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    if not ordered:
        raise ReportError("至少需要一张发票")
    return ordered


def link_invoices(
    connection: sqlite3.Connection,
    report_id: int,
    sequence: int,
    invoice_ids: Sequence[int],
) -> int:
    """Link invoices to a line; returns how many links were newly created."""
    line_id = _line_id(connection, report_id, sequence)
    ids = _dedupe_ids(invoice_ids)
    added = 0
    with connection:
        _ensure_invoices_exist(connection, ids)
        for invoice_id in ids:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO report_line_invoices (line_id, invoice_id)"
                " VALUES (?, ?)",
                (line_id, invoice_id),
            )
            added += cursor.rowcount
        connection.execute(
            "UPDATE report_lines SET has_invoice = 1 WHERE id = ?", (line_id,)
        )
    return added


def unlink_invoices(
    connection: sqlite3.Connection,
    report_id: int,
    sequence: int,
    invoice_ids: Sequence[int],
) -> int:
    line_id = _line_id(connection, report_id, sequence)
    ids = _dedupe_ids(invoice_ids)
    placeholders = ",".join("?" for _ in ids)
    with connection:
        cursor = connection.execute(
            "DELETE FROM report_line_invoices"
            f" WHERE line_id = ? AND invoice_id IN ({placeholders})",
            (line_id, *ids),
        )
    return cursor.rowcount


def report_lines(connection: sqlite3.Connection, report_id: int) -> list[ReportLine]:
    _ensure_report(connection, report_id)
    rows = connection.execute(
        "SELECT id, report_id, line_date, category, description, amount_cents,"
        " has_invoice, invoice_note, project_name, source, mileage_km"
        " FROM report_lines WHERE report_id = ? ORDER BY id",
        (report_id,),
    ).fetchall()
    links: dict[int, list[tuple[int, int | None]]] = {}
    for row in connection.execute(
        "SELECT li.line_id, li.invoice_id, i.amount_total_cents"
        " FROM report_line_invoices li"
        " JOIN invoices i ON i.id = li.invoice_id"
        " JOIN report_lines l ON l.id = li.line_id"
        " WHERE l.report_id = ?"
        " ORDER BY li.invoice_id",
        (report_id,),
    ):
        links.setdefault(row["line_id"], []).append(
            (row["invoice_id"], row["amount_total_cents"])
        )
    lines: list[ReportLine] = []
    for row in rows:
        linked = links.get(row["id"], [])
        known = bool(linked) and all(amount is not None for _, amount in linked)
        lines.append(
            ReportLine(
                id=row["id"],
                report_id=row["report_id"],
                line_date=row["line_date"],
                category=row["category"],
                description=row["description"],
                amount_cents=row["amount_cents"],
                has_invoice=bool(row["has_invoice"]),
                invoice_note=row["invoice_note"],
                project_name=row["project_name"],
                source=row["source"],
                mileage_km=row["mileage_km"],
                invoice_ids=tuple(invoice_id for invoice_id, _ in linked),
                invoice_total_cents=(
                    sum(amount or 0 for _, amount in linked) if known else None
                ),
            )
        )
    return lines


def _invoice_line_description(
    item_summary: str, seller_name: str, invoice_number: str
) -> str:
    """Human-readable line name, e.g. ``办公用品（示例销售方）``."""
    item = normalize(item_summary).strip()
    seller = normalize(seller_name).strip()
    if item and seller:
        return normalize(f"{item}（{seller}）")
    return item or seller or f"发票 {invoice_number}".strip()


def add_lines_from_invoices(
    connection: sqlite3.Connection,
    report_id: int,
    invoice_ids: Sequence[int],
    *,
    project_name: str = "",
) -> FromInvoicesResult:
    """Turn explicitly selected invoices into report lines (one per invoice).

    The workbench selects rows instead of filtering the whole pool, so this is
    the id-addressed counterpart of :func:`from_invoices`. Already linked,
    missing-amount, and missing-date invoices are skipped and counted.
    """
    _ensure_report(connection, report_id)
    ids = _dedupe_ids(invoice_ids)
    _ensure_invoices_exist(connection, ids)
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        "SELECT i.id, i.invoice_date, i.invoice_number, i.seller_name,"
        " i.item_summary, i.amount_total_cents, i.category,"
        " EXISTS (SELECT 1 FROM report_line_invoices li"
        "         WHERE li.invoice_id = i.id) AS linked"
        f" FROM invoices i WHERE i.id IN ({placeholders})",
        tuple(ids),
    ).fetchall()
    by_id = {row["id"]: row for row in rows}

    added = 0
    skipped_linked = 0
    missing_amount = 0
    missing_date = 0
    created_at = db.utc_now()
    project = normalize(project_name).strip()
    with connection:
        for invoice_id in ids:
            row = by_id[invoice_id]
            if row["linked"]:
                skipped_linked += 1
                continue
            if row["amount_total_cents"] is None:
                missing_amount += 1
                continue
            if not row["invoice_date"]:
                missing_date += 1
                continue
            cursor = connection.execute(
                "INSERT INTO report_lines"
                " (report_id, line_date, category, description, amount_cents,"
                "  has_invoice, project_name, source, mileage_km, created_at)"
                " VALUES (?, ?, ?, ?, ?, 1, ?, 'invoice', NULL, ?)",
                (
                    report_id,
                    row["invoice_date"],
                    normalize(row["category"]) or DEFAULT_CATEGORY,
                    _invoice_line_description(
                        row["item_summary"], row["seller_name"], row["invoice_number"]
                    ),
                    row["amount_total_cents"],
                    project,
                    created_at,
                ),
            )
            line_id = cursor.lastrowid
            if line_id is None:  # pragma: no cover - sqlite always returns a row id
                raise RuntimeError("无法添加报销行")
            connection.execute(
                "INSERT INTO report_line_invoices (line_id, invoice_id) VALUES (?, ?)",
                (line_id, invoice_id),
            )
            added += 1
    return FromInvoicesResult(
        added=added,
        skipped_linked=skipped_linked,
        skipped_missing_amount=missing_amount,
        skipped_missing_date=missing_date,
    )


def from_invoices(
    connection: sqlite3.Connection,
    report_id: int,
    *,
    batch_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
    buyer: str = "",
    include_review: bool = True,
    project_name: str = "",
) -> FromInvoicesResult:
    """Turn unlinked invoices into report lines (one line per invoice)."""
    _ensure_report(connection, report_id)
    filters: list[str] = []
    params: list[object] = []
    if batch_id is not None:
        filters.append("i.batch_id = ?")
        params.append(batch_id)
    if buyer:
        filters.append("i.buyer_name = ?")
        params.append(normalize(buyer))
    if date_from:
        filters.append("i.invoice_date <> '' AND i.invoice_date >= ?")
        params.append(date_from)
    if date_to:
        filters.append("i.invoice_date <> '' AND i.invoice_date <= ?")
        params.append(date_to)
    if not include_review:
        filters.append("i.needs_review = 0")
    unlinked_sql = (
        " WHERE NOT EXISTS (SELECT 1 FROM report_line_invoices li"
        " WHERE li.invoice_id = i.id)"
        + (" AND " + " AND ".join(filters) if filters else "")
    )

    rows = connection.execute(
        "SELECT i.id, i.invoice_date, i.invoice_number, i.seller_name,"
        " i.item_summary, i.amount_total_cents, i.category"
        " FROM invoices i" + unlinked_sql +
        " ORDER BY CASE WHEN i.invoice_date = '' THEN 1 ELSE 0 END,"
        " i.invoice_date, i.invoice_number, i.id",
        params,
    ).fetchall()
    linked_count = connection.execute(
        "SELECT COUNT(*) FROM invoices i"
        " WHERE EXISTS (SELECT 1 FROM report_line_invoices li"
        " WHERE li.invoice_id = i.id)" + (" AND " + " AND ".join(filters) if filters else ""),
        params,
    ).fetchone()[0]

    added = 0
    missing_amount = 0
    missing_date = 0
    created_at = db.utc_now()
    with connection:
        for row in rows:
            if row["amount_total_cents"] is None:
                missing_amount += 1
                continue
            if not row["invoice_date"]:
                missing_date += 1
                continue
            description = _invoice_line_description(
                row["item_summary"], row["seller_name"], row["invoice_number"]
            )
            cursor = connection.execute(
                "INSERT INTO report_lines"
                " (report_id, line_date, category, description, amount_cents,"
                "  has_invoice, project_name, source, mileage_km, created_at)"
                " VALUES (?, ?, ?, ?, ?, 1, ?, 'invoice', NULL, ?)",
                (
                    report_id,
                    row["invoice_date"],
                    normalize(row["category"]) or DEFAULT_CATEGORY,
                    description,
                    row["amount_total_cents"],
                    normalize(project_name).strip(),
                    created_at,
                ),
            )
            line_id = cursor.lastrowid
            if line_id is None:  # pragma: no cover - sqlite always returns a row id
                raise RuntimeError("无法添加报销行")
            connection.execute(
                "INSERT INTO report_line_invoices (line_id, invoice_id) VALUES (?, ?)",
                (line_id, row["id"]),
            )
            added += 1
    return FromInvoicesResult(
        added=added,
        skipped_linked=linked_count,
        skipped_missing_amount=missing_amount,
        skipped_missing_date=missing_date,
    )
