"""Category rules learned from explicit user corrections.

A correction is the strongest offline signal available: the user looked at the
invoice and said what it belongs to. Correcting an invoice's category therefore
stores a seller -> category rule immediately ("修正一次即记住"). The importer
applies these rules before inserting, overriding the built-in keyword rules.

Rules are local-only user data. They live in the pool database, never in the
repository. Repeated corrections of the same seller to the same category
increase ``confirm_count``; correcting to a different category replaces the
rule and resets the count. ``hit_count`` counts imports that used the rule.

A rule can be disabled (kept but not applied to new imports) or deleted. A
disabled rule stays disabled when the seller is corrected again: stopping
automatic classification is an explicit choice the next correction should not
silently undo. Existing invoices keep their category either way; only an
explicit backfill rewrites history, and that writes ``invoice_edits`` rows per
changed invoice so every rewrite is traceable. Apply requires the token issued
by the preview the user confirmed, so the written set cannot grow silently
between confirmation and execution.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from app.core import db
from app.core.models import ParsedInvoice
from app.core.text import normalize

MATCH_SELLER = "seller"
LEARNED_SELLER_RULE = "learned.seller"
MANUAL_CATEGORY_RULE = "manual"
BACKFILL_SAMPLE_LIMIT = 20


class RuleError(ValueError):
    """A rule action was rejected for a reason the UI can show."""


@dataclass(frozen=True)
class CategoryRule:
    id: int
    match_type: str
    pattern: str
    category: str
    confirm_count: int
    hit_count: int
    enabled: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class BackfillSample:
    invoice_id: int
    invoice_date: str
    seller_name: str
    category: str
    amount_total_cents: int | None


@dataclass(frozen=True)
class BackfillPreview:
    rule: CategoryRule
    match_count: int
    update_count: int
    unchanged: int
    manual_skipped: int
    samples: tuple[BackfillSample, ...]
    token: str


@dataclass(frozen=True)
class BackfillResult:
    rule: CategoryRule
    updated: int
    unchanged: int
    manual_skipped: int
    edited_at: str


def seller_key(seller_name: str) -> str:
    """Normalize a seller name to the key stored in ``category_rules``.

    NFKC folds full-width Latin letters, ``casefold`` makes Latin names
    case-insensitive, and surrounding whitespace is dropped. Internal spaces
    are collapsed to one but kept: "AB 公司" and "AB公司" stay distinct.
    """
    return normalize(seller_name).strip().casefold()


def _rule_from_row(row: sqlite3.Row) -> CategoryRule:
    return CategoryRule(
        id=row["id"],
        match_type=row["match_type"],
        pattern=row["pattern"],
        category=row["category"],
        confirm_count=row["confirm_count"],
        hit_count=row["hit_count"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def remember_seller_category(
    connection: sqlite3.Connection,
    seller_name: str,
    category: str,
    *,
    now: str | None = None,
) -> CategoryRule | None:
    """Store or refresh the seller -> category rule; no commit of its own.

    Returns ``None`` when the seller name is empty: a rule needs a stable key,
    and learning from "unknown seller" would misfile unrelated invoices.
    """
    key = seller_key(seller_name)
    target = normalize(category).strip()
    if not key or not target:
        return None
    stamp = now or db.utc_now()
    row = connection.execute(
        "SELECT id, category, confirm_count FROM category_rules"
        " WHERE match_type = ? AND pattern = ?",
        (MATCH_SELLER, key),
    ).fetchone()
    if row is None:
        cursor = connection.execute(
            "INSERT INTO category_rules"
            " (match_type, pattern, category, confirm_count, hit_count,"
            "  created_at, updated_at)"
            " VALUES (?, ?, ?, 1, 0, ?, ?)",
            (MATCH_SELLER, key, target, stamp, stamp),
        )
        rule_id = cursor.lastrowid
        if rule_id is None:  # pragma: no cover - sqlite always returns a row id
            raise RuntimeError("无法保存分类规则")
    else:
        confirm = row["confirm_count"] + 1 if row["category"] == target else 1
        connection.execute(
            "UPDATE category_rules SET category = ?, confirm_count = ?, updated_at = ?"
            " WHERE id = ?",
            (target, confirm, stamp, row["id"]),
        )
        rule_id = row["id"]
    refreshed = connection.execute(
        "SELECT * FROM category_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    if refreshed is None:  # pragma: no cover - the row was just written
        raise RuntimeError("无法读取分类规则")
    return _rule_from_row(refreshed)


def seller_rules(connection: sqlite3.Connection) -> dict[str, str]:
    """Enabled seller -> category rules, keyed by normalized seller name."""
    return {
        row["pattern"]: row["category"]
        for row in connection.execute(
            "SELECT pattern, category FROM category_rules"
            " WHERE match_type = ? AND enabled = 1",
            (MATCH_SELLER,),
        )
    }


def list_rules(connection: sqlite3.Connection) -> list[CategoryRule]:
    """All rules, enabled first; newest edit breaks ties."""
    rows = connection.execute(
        "SELECT * FROM category_rules"
        " ORDER BY enabled DESC, updated_at DESC, id DESC"
    ).fetchall()
    return [_rule_from_row(row) for row in rows]


def get_rule(connection: sqlite3.Connection, rule_id: int) -> CategoryRule:
    row = connection.execute(
        "SELECT * FROM category_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    if row is None:
        raise RuleError(f"规则不存在：#{rule_id}")
    return _rule_from_row(row)


def seller_usage(connection: sqlite3.Connection) -> dict[str, int]:
    """Invoice counts per normalized seller key, for the rules screen."""
    counts: dict[str, int] = {}
    for row in connection.execute(
        "SELECT seller_name FROM invoices"
        " WHERE seller_name <> '' AND archived_at = ''"
    ):
        key = seller_key(row["seller_name"])
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def set_rule_enabled(
    connection: sqlite3.Connection,
    rule_id: int,
    enabled: bool,
    *,
    now: str | None = None,
) -> CategoryRule:
    stamp = now or db.utc_now()
    with connection:
        cursor = connection.execute(
            "UPDATE category_rules SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, stamp, rule_id),
        )
        if cursor.rowcount == 0:
            raise RuleError(f"规则不存在：#{rule_id}")
    return get_rule(connection, rule_id)


def delete_rule(connection: sqlite3.Connection, rule_id: int) -> None:
    """Remove a rule. Already-classified invoices keep their category."""
    with connection:
        cursor = connection.execute(
            "DELETE FROM category_rules WHERE id = ?", (rule_id,)
        )
        if cursor.rowcount == 0:
            raise RuleError(f"规则不存在：#{rule_id}")


def _backfill_candidates(
    connection: sqlite3.Connection, rule: CategoryRule
) -> tuple[list[sqlite3.Row], int, int]:
    """Return (rows to update, already-correct count, manual-skip count).

    A manual category correction is the user's explicit decision for that
    invoice, so a later rule backfill never overwrites it.
    """
    rows = connection.execute(
        "SELECT id, invoice_date, seller_name, category, category_rule,"
        " amount_total_cents, confidence FROM invoices WHERE archived_at = ''"
        " ORDER BY CASE WHEN invoice_date = '' THEN 1 ELSE 0 END,"
        " invoice_date DESC, id DESC"
    ).fetchall()
    candidates: list[sqlite3.Row] = []
    unchanged = 0
    manual_skipped = 0
    for row in rows:
        if seller_key(row["seller_name"]) != rule.pattern:
            continue
        if row["category"] == rule.category:
            unchanged += 1
        elif row["category_rule"] == MANUAL_CATEGORY_RULE:
            manual_skipped += 1
        else:
            candidates.append(row)
    return candidates, unchanged, manual_skipped


def _require_backfillable(rule: CategoryRule) -> None:
    if rule.match_type != MATCH_SELLER:
        raise RuleError("暂不支持这类规则的回溯。")
    if not rule.enabled:
        raise RuleError("规则已停用，请先启用再回溯。")


def _backfill_token(rule: CategoryRule, candidates: list[sqlite3.Row]) -> str:
    """Fingerprint the exact candidate set a preview showed the user.

    Apply requires this token so the write cannot silently grow beyond the
    invoices the confirmation text described. It is a consistency check, not
    an authentication secret: the app is local and single-user.
    """
    payload = json.dumps(
        {
            "rule_id": rule.id,
            "updated_at": rule.updated_at,
            "category": rule.category,
            "invoices": [
                (row["id"], row["category"], row["category_rule"])
                for row in candidates
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def preview_backfill(
    connection: sqlite3.Connection, rule_id: int
) -> BackfillPreview:
    """Count and sample the invoices a backfill would change; writes nothing."""
    rule = get_rule(connection, rule_id)
    _require_backfillable(rule)
    candidates, unchanged, manual_skipped = _backfill_candidates(connection, rule)
    samples = tuple(
        BackfillSample(
            invoice_id=row["id"],
            invoice_date=row["invoice_date"],
            seller_name=normalize(row["seller_name"]),
            category=normalize(row["category"]),
            amount_total_cents=row["amount_total_cents"],
        )
        for row in candidates[:BACKFILL_SAMPLE_LIMIT]
    )
    return BackfillPreview(
        rule=rule,
        match_count=len(candidates) + unchanged + manual_skipped,
        update_count=len(candidates),
        unchanged=unchanged,
        manual_skipped=manual_skipped,
        samples=samples,
        token=_backfill_token(rule, candidates),
    )


def _category_confidence_high(raw: str) -> str:
    """Raise the category confidence to high without touching other fields."""
    try:
        confidence = json.loads(raw) if raw else {}
    except (TypeError, ValueError) as exc:
        raise RuleError("发票置信度数据损坏，无法回溯分类。") from exc
    if not isinstance(confidence, dict):
        raise RuleError("发票置信度数据损坏，无法回溯分类。")
    confidence["category"] = "high"
    return json.dumps(confidence, ensure_ascii=False, sort_keys=True)


def apply_backfill(
    connection: sqlite3.Connection,
    rule_id: int,
    token: str,
    *,
    now: str | None = None,
) -> BackfillResult:
    """Rewrite matching invoices to the rule's category, with audit rows.

    ``token`` must come from a preview of the same candidate set; a changed
    pool or rule is rejected rather than widened past what the user confirmed.
    Each invoice gets a ``category`` audit row, plus a ``category_rule`` row
    when its classification source changes. Review flags and warnings are
    untouched: a category rewrite does not resolve whatever put an invoice in
    the review queue. All rows commit as one transaction, so damaged confidence
    data aborts the whole batch instead of leaving a half-applied rule.
    """
    if not isinstance(token, str) or not token.strip():
        raise RuleError("缺少预览凭证，请先预览后再确认。")
    stamp = now or db.utc_now()
    # Read, verify, and write inside one immediate transaction. Another
    # connection (CLI or a second window) could otherwise edit a candidate
    # between the token check and the rewrite, letting stale old values land
    # in the audit trail and silently overwriting a manual category.
    with connection:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        rule = get_rule(connection, rule_id)
        _require_backfillable(rule)
        candidates, unchanged, manual_skipped = _backfill_candidates(connection, rule)
        if token != _backfill_token(rule, candidates):
            raise RuleError("发票数据或规则已变化，请重新预览后再确认。")
        for row in candidates:
            connection.execute(
                "INSERT INTO invoice_edits"
                " (invoice_id, field, old_value, new_value, edited_at)"
                " VALUES (?, 'category', ?, ?, ?)",
                (row["id"], row["category"], rule.category, stamp),
            )
            if row["category_rule"] != LEARNED_SELLER_RULE:
                connection.execute(
                    "INSERT INTO invoice_edits"
                    " (invoice_id, field, old_value, new_value, edited_at)"
                    " VALUES (?, 'category_rule', ?, ?, ?)",
                    (row["id"], row["category_rule"], LEARNED_SELLER_RULE, stamp),
                )
            connection.execute(
                "UPDATE invoices SET category = ?, category_rule = ?,"
                " confidence = ? WHERE id = ?",
                (
                    rule.category,
                    LEARNED_SELLER_RULE,
                    _category_confidence_high(row["confidence"]),
                    row["id"],
                ),
            )
    return BackfillResult(
        rule=rule,
        updated=len(candidates),
        unchanged=unchanged,
        manual_skipped=manual_skipped,
        edited_at=stamp,
    )


def apply_seller_rules(invoice: ParsedInvoice, rules: Mapping[str, str]) -> str | None:
    """Override the parsed category when the seller has a learned rule.

    Returns the matched seller key so the caller can count the hit, or
    ``None`` when no rule applies.
    """
    key = seller_key(invoice.seller_name)
    category = rules.get(key)
    if not category:
        return None
    invoice.category = category
    invoice.category_rule = LEARNED_SELLER_RULE
    invoice.confidence["category"] = "high"
    return key


def record_hit(connection: sqlite3.Connection, pattern: str) -> None:
    """Count one import that used the rule; no commit of its own."""
    connection.execute(
        "UPDATE category_rules SET hit_count = hit_count + 1"
        " WHERE match_type = ? AND pattern = ?",
        (MATCH_SELLER, pattern),
    )
