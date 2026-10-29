"""Manual corrections to parsed invoice fields.

Recognition output is editable, and every edit is auditable: each changed field
gets an ``invoice_edits`` row with the old and new value, and the field's
confidence becomes ``high`` (or ``low`` when cleared).

Corrections never clear the review flag on their own: a "待确认" record can
carry a conflict or multi-invoice warning that a category edit does not resolve.
Clearing it requires the explicit ``confirm_invoice`` action, which writes a
``review`` audit entry. Editing a critical field to empty re-enters review.
Warnings and low-confidence notes stay visible either way.

A category correction additionally teaches a seller rule; see
``app.core.learning``. Source PDFs are never touched: corrections only update
the local database.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core import db, learning
from app.core.importer import CONFLICT_WARNING, INCOMPLETE_WARNING
from app.core.text import normalize

DUPLICATE_WARNING = "修正后票号与既有发票完全相同,需人工核对是否重复"
# Red-letter invoices are legitimately negative, so only an absurd magnitude
# (a typo such as one extra group of digits) is rejected. SQLite binds 64-bit
# integers; this bound stays far below that while catching paste accidents.
MAX_ABS_AMOUNT_CENTS = 10**15
# A correction to any of these can change whether two records are the same
# invoice, so it re-runs the duplicate/conflict check the importer performs.
IDENTITY_FIELDS = (
    "invoice_number",
    "invoice_date",
    "seller_name",
    "amount_total_cents",
)

# Database column -> confidence key used by the parser/export notes.
EDITABLE_FIELDS: dict[str, str] = {
    "invoice_number": "invoice_number",
    "invoice_date": "invoice_date",
    "seller_name": "seller_name",
    "buyer_name": "buyer_name",
    "amount_total_cents": "amount_total",
    "category": "category",
    "item_summary": "item",
}

FIELD_LIMITS: dict[str, int] = {
    "invoice_number": 64,
    "seller_name": 200,
    "buyer_name": 200,
    "category": 50,
    "item_summary": 1000,
}
FIELD_LABELS: dict[str, str] = {
    "invoice_number": "票号",
    "seller_name": "销售方",
    "buyer_name": "购买方",
    "category": "分类",
    "item_summary": "项目",
}

_UNSET: Any = object()


class InvoiceError(ValueError):
    """An invoice correction was rejected for a reason the UI can show."""


@dataclass(frozen=True)
class InvoiceEdit:
    id: int
    field: str
    old_value: str
    new_value: str
    edited_at: str


@dataclass(frozen=True)
class InvoiceUpdateResult:
    changed: tuple[InvoiceEdit, ...]
    rule: learning.CategoryRule | None
    conflict: bool = False


def _clean_text(value: str) -> str:
    return normalize(str(value))


def _snippet(value: Any, limit: int = 32) -> str:
    """Echo only a short prefix of rejected input back to the UI."""
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clean_number(value: str) -> str:
    # A pasted invoice number often carries spaces; identity dedup compares the
    # stored digits, so drop whitespace instead of storing a lookalike.
    return re.sub(r"\s+", "", normalize(str(value)))


def _clean_date(value: str) -> str:
    text = normalize(str(value)).strip()
    if not text:
        return ""
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise InvoiceError(f"日期格式应为 YYYY-MM-DD：{_snippet(value)}") from exc
    if not 1990 <= parsed.year <= 2100:
        raise InvoiceError(f"日期超出范围：{_snippet(value)}")
    return parsed.isoformat()


def _clean_amount(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvoiceError("金额必须是整数分")
    if abs(value) > MAX_ABS_AMOUNT_CENTS:
        raise InvoiceError("金额超出可处理范围")
    return value


def _display(column: str, value: Any) -> str:
    if value is None:
        return ""
    if column == "amount_total_cents":
        money = db.cents_to_money(value)
        return "" if money is None else f"{money:.2f}"
    return str(value)


def _bounded(column: str, value: str) -> str:
    limit = FIELD_LIMITS[column]
    if len(value) > limit:
        raise InvoiceError(f"{FIELD_LABELS[column]}最多 {limit} 个字符")
    return value


def _normalized_fields(
    *,
    invoice_number: str | None,
    invoice_date: str | None,
    seller_name: str | None,
    buyer_name: str | None,
    amount_total_cents: Any,
    category: str | None,
    item_summary: str | None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if invoice_number is not None:
        fields["invoice_number"] = _bounded(
            "invoice_number", _clean_number(invoice_number)
        )
    if invoice_date is not None:
        fields["invoice_date"] = _clean_date(invoice_date)
    if seller_name is not None:
        fields["seller_name"] = _bounded(
            "seller_name", _clean_text(seller_name)
        )
    if buyer_name is not None:
        fields["buyer_name"] = _bounded("buyer_name", _clean_text(buyer_name))
    if amount_total_cents is not _UNSET:
        fields["amount_total_cents"] = _clean_amount(amount_total_cents)
    if category is not None:
        cleaned = _bounded("category", _clean_text(category))
        if not cleaned:
            raise InvoiceError("分类不能为空")
        fields["category"] = cleaned
    if item_summary is not None:
        fields["item_summary"] = _bounded(
            "item_summary", _clean_text(item_summary)
        )
    return fields


def _warnings_list(raw: Any) -> list[str]:
    """Parse warnings without ever discarding damaged data on write-back."""
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError) as exc:
        raise InvoiceError("发票警告数据损坏，无法保存修正") from exc
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) for item in parsed
    ):
        raise InvoiceError("发票警告数据损坏，无法保存修正")
    return parsed


def _audit_warning(
    connection: sqlite3.Connection,
    invoice_id: int,
    field: str,
    old_value: str,
    new_value: str,
    stamp: str,
) -> None:
    connection.execute(
        "INSERT INTO invoice_edits"
        " (invoice_id, field, old_value, new_value, edited_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (invoice_id, field, old_value, new_value, stamp),
    )


def _flag_identity_conflict(
    connection: sqlite3.Connection,
    invoice_id: int,
    row: sqlite3.Row,
    fields: dict[str, Any],
    stamp: str,
) -> str | None:
    """Re-check same-number records after an identity edit.

    An exact identity match is a duplicate; same number with different or
    incomplete identity is a conflict. Both sides get a warning and re-enter
    review, matching the importer's rule. Nothing is merged or deleted. The
    affected peer records get their own audit row so the cross-invoice
    side effect is traceable from either side.
    """
    number = fields.get("invoice_number", row["invoice_number"])
    if not number:
        return None
    others = connection.execute(
        "SELECT id, invoice_date, amount_total_cents, seller_name, warnings,"
        " needs_review FROM invoices WHERE invoice_number = ? AND id <> ?",
        (number, invoice_id),
    ).fetchall()
    if not others:
        return None
    date = fields.get("invoice_date", row["invoice_date"])
    amount = fields.get("amount_total_cents", row["amount_total_cents"])
    seller = fields.get("seller_name", row["seller_name"])
    identity_complete = bool(date) and amount is not None and bool(seller)
    if not identity_complete:
        warning = INCOMPLETE_WARNING
    elif any(
        (other["invoice_date"], other["amount_total_cents"], other["seller_name"])
        == (date, amount, seller)
        for other in others
    ):
        warning = DUPLICATE_WARNING
    else:
        warning = CONFLICT_WARNING
    for other in others:
        if warning not in _warnings_list(other["warnings"]):
            _audit_warning(connection, other["id"], "warning", "", warning, stamp)
        elif not other["needs_review"]:
            _audit_warning(
                connection, other["id"], "review", "已确认", "待确认", stamp
            )
        db.append_warning(connection, other["id"], warning)
    return warning


def _refresh_confidence(row: sqlite3.Row, fields: dict[str, Any]) -> str:
    raw = row["confidence"]
    try:
        confidence = json.loads(raw) if raw else {}
    except (TypeError, ValueError) as exc:
        raise InvoiceError("发票置信度数据损坏，无法保存修正") from exc
    if not isinstance(confidence, dict):
        raise InvoiceError("发票置信度数据损坏，无法保存修正")
    for column, value in fields.items():
        present = value is not None and (not isinstance(value, str) or bool(value))
        confidence[EDITABLE_FIELDS[column]] = "high" if present else "low"
    return json.dumps(confidence, ensure_ascii=False, sort_keys=True)


def update_invoice(
    connection: sqlite3.Connection,
    invoice_id: int,
    *,
    invoice_number: str | None = None,
    invoice_date: str | None = None,
    seller_name: str | None = None,
    buyer_name: str | None = None,
    amount_total_cents: Any = _UNSET,
    category: str | None = None,
    item_summary: str | None = None,
    edited_at: str | None = None,
) -> InvoiceUpdateResult:
    """Apply a manual correction, record it, and learn from its category.

    Omitted fields stay unchanged. Passing ``None`` as ``amount_total_cents``
    clears the amount; passing ``""`` clears a text field.
    """
    row = connection.execute(
        "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
    ).fetchone()
    if row is None:
        raise InvoiceError(f"发票不存在：#{invoice_id}")
    fields = _normalized_fields(
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        seller_name=seller_name,
        buyer_name=buyer_name,
        amount_total_cents=amount_total_cents,
        category=category,
        item_summary=item_summary,
    )
    changed = {
        column: value
        for column, value in fields.items()
        if row[column] != value
    }
    if not changed:
        # Same-value submissions ("120" vs "120.00") are idempotent no-ops;
        # the UI normalizes amounts, and other clients may not.
        return InvoiceUpdateResult(changed=(), rule=None)
    if not set(changed) <= set(EDITABLE_FIELDS):
        # Column names are interpolated into SQL below; keep the whitelist
        # explicit rather than relying on the caller's construction.
        raise InvoiceError("包含不支持的修正字段")

    stamp = edited_at or db.utc_now()
    number = changed.get("invoice_number", row["invoice_number"])
    date = changed.get("invoice_date", row["invoice_date"])
    amount = changed.get("amount_total_cents", row["amount_total_cents"])
    # Edits can add a review flag, never remove one: unresolved warnings or
    # conflicts require the explicit confirmation below.
    needs_review = 1 if not number or not date or amount is None else row["needs_review"]
    warnings = _warnings_list(row["warnings"])
    conflict = False
    extra_audits = 0

    with connection:
        if any(column in changed for column in IDENTITY_FIELDS):
            warning = _flag_identity_conflict(
                connection, invoice_id, row, changed, stamp
            )
            if warning is not None:
                conflict = True
                needs_review = 1
                if warning not in warnings:
                    warnings.append(warning)
                    _audit_warning(
                        connection, invoice_id, "warning", "", warning, stamp
                    )
                    extra_audits += 1
                elif not row["needs_review"]:
                    _audit_warning(
                        connection,
                        invoice_id,
                        "review",
                        "已确认",
                        "待确认",
                        stamp,
                    )
                    extra_audits += 1
        assignments = ", ".join(f"{column} = ?" for column in changed)
        params: list[Any] = [*changed.values()]
        rule_assignment = ""
        if "category" in changed:
            rule_assignment = ", category_rule = ?"
            params.append(learning.MANUAL_CATEGORY_RULE)
        connection.execute(
            f"UPDATE invoices SET {assignments}{rule_assignment}, warnings = ?,"
            " confidence = ?, needs_review = ? WHERE id = ?",
            (
                *params,
                json.dumps(warnings, ensure_ascii=False),
                _refresh_confidence(row, changed),
                needs_review,
                invoice_id,
            ),
        )
        for column, value in changed.items():
            connection.execute(
                "INSERT INTO invoice_edits"
                " (invoice_id, field, old_value, new_value, edited_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    invoice_id,
                    column,
                    _display(column, row[column]),
                    _display(column, value),
                    stamp,
                ),
            )
        rule = None
        if "category" in changed:
            effective_seller = changed.get("seller_name", row["seller_name"])
            rule = learning.remember_seller_category(
                connection, effective_seller, changed["category"], now=stamp
            )
    return InvoiceUpdateResult(
        changed=tuple(
            invoice_edits(connection, invoice_id, limit=len(changed) + extra_audits)
        ),
        rule=rule,
        conflict=conflict,
    )


def confirm_invoice(
    connection: sqlite3.Connection,
    invoice_id: int,
    *,
    confirmed_at: str | None = None,
) -> InvoiceEdit:
    """Explicitly mark a record as reviewed and audit that decision."""
    row = connection.execute(
        "SELECT needs_review FROM invoices WHERE id = ?", (invoice_id,)
    ).fetchone()
    if row is None:
        raise InvoiceError(f"发票不存在：#{invoice_id}")
    if not row["needs_review"]:
        raise InvoiceError("这张发票不需要确认")
    stamp = confirmed_at or db.utc_now()
    with connection:
        connection.execute(
            "UPDATE invoices SET needs_review = 0 WHERE id = ?", (invoice_id,)
        )
        cursor = connection.execute(
            "INSERT INTO invoice_edits"
            " (invoice_id, field, old_value, new_value, edited_at)"
            " VALUES (?, 'review', '待确认', '已确认', ?)",
            (invoice_id, stamp),
        )
    edit_id = cursor.lastrowid
    if edit_id is None:  # pragma: no cover - sqlite always returns a row id
        raise RuntimeError("无法记录复核")
    return InvoiceEdit(
        id=edit_id,
        field="review",
        old_value="待确认",
        new_value="已确认",
        edited_at=stamp,
    )


def reopen_invoice(
    connection: sqlite3.Connection,
    invoice_id: int,
    *,
    reopened_at: str | None = None,
) -> InvoiceEdit:
    """Undo an accidental confirmation by putting the record back in review.

    Only the flag flips: confirmation never removed warnings or low-confidence
    notes, so re-entering review restores exactly what the user had confirmed.
    """
    row = connection.execute(
        "SELECT needs_review FROM invoices WHERE id = ?", (invoice_id,)
    ).fetchone()
    if row is None:
        raise InvoiceError(f"发票不存在：#{invoice_id}")
    if row["needs_review"]:
        raise InvoiceError("这张发票已经在待确认队列中")
    stamp = reopened_at or db.utc_now()
    with connection:
        connection.execute(
            "UPDATE invoices SET needs_review = 1 WHERE id = ?", (invoice_id,)
        )
        cursor = connection.execute(
            "INSERT INTO invoice_edits"
            " (invoice_id, field, old_value, new_value, edited_at)"
            " VALUES (?, 'review', '已确认', '待确认', ?)",
            (invoice_id, stamp),
        )
    edit_id = cursor.lastrowid
    if edit_id is None:  # pragma: no cover - sqlite always returns a row id
        raise RuntimeError("无法记录复核")
    return InvoiceEdit(
        id=edit_id,
        field="review",
        old_value="已确认",
        new_value="待确认",
        edited_at=stamp,
    )


def invoice_edits(
    connection: sqlite3.Connection, invoice_id: int, *, limit: int = 20
) -> list[InvoiceEdit]:
    """Newest corrections first, capped so a long history cannot flood the UI."""
    rows = connection.execute(
        "SELECT id, field, old_value, new_value, edited_at FROM invoice_edits"
        " WHERE invoice_id = ? ORDER BY id DESC LIMIT ?",
        (invoice_id, limit),
    ).fetchall()
    return [
        InvoiceEdit(
            id=row["id"],
            field=row["field"],
            old_value=row["old_value"],
            new_value=row["new_value"],
            edited_at=row["edited_at"],
        )
        for row in rows
    ]
