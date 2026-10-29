"""Re-apply built-in category rules to rows classified by an older ruleset.

Built-in rules live in the code, but the category is stored with each invoice
at import time, so a rule fix (for example the hotel-meal override) only
reaches newly imported invoices. This module closes that gap once per
``categories.RULES_VERSION``: every invoice whose ``category_rule`` names a
built-in rule is reclassified from its stored seller, item, and file name.

Two kinds of rows are never touched:

* ``manual`` — the user corrected the category on that invoice;
* ``learned.seller`` — a learned seller rule decided it.

Both are explicit decisions, not rule output. A classification change is
written to ``invoice_edits`` so the rewrite stays traceable, and the version
marker in ``settings`` makes the pass a no-op on every later open.
"""

from __future__ import annotations

import sqlite3

from app.core import db
from app.core.categories import RULES_VERSION, categorize
from app.core.learning import LEARNED_SELLER_RULE, MANUAL_CATEGORY_RULE

RULES_VERSION_KEY = "category_rules_version"
# A rules refresh is not a human correction: the audit row uses its own field
# so the workbench's "已修正" badge still means the user changed something.
CATEGORY_REFRESH_FIELD = "category_refresh"
_BUILTIN_RULE_PREFIXES = ("seller.", "item.")


def _is_builtin_rule(rule: str) -> bool:
    return rule == "fallback" or rule.startswith(_BUILTIN_RULE_PREFIXES)


def _file_name(source_path: str) -> str:
    return str(source_path).replace("\\", "/").rsplit("/", 1)[-1]


def _stored_version(connection: sqlite3.Connection) -> int:
    try:
        return int(db.get_setting(connection, RULES_VERSION_KEY, "0"))
    except (TypeError, ValueError):
        return 0


def refresh_builtin_categories(connection: sqlite3.Connection) -> int:
    """Reclassify built-in-rule rows once per rules version; return changes.

    The caller owns the connection. A version marker is written even when no
    row changes, so an unchanged pool is not rescanned on every open.
    """
    if _stored_version(connection) >= RULES_VERSION:
        return 0
    rows = connection.execute(
        "SELECT id, seller_name, item_summary, source_path, category,"
        " category_rule FROM invoices WHERE archived_at = ''"
    ).fetchall()
    stamp = db.utc_now()
    updated = 0
    with connection:
        for row in rows:
            rule = row["category_rule"] or ""
            if rule in {MANUAL_CATEGORY_RULE, LEARNED_SELLER_RULE}:
                continue
            if not _is_builtin_rule(rule):
                continue
            result = categorize(
                item_summary=row["item_summary"],
                seller_name=row["seller_name"],
                file_name=_file_name(row["source_path"]),
            )
            if result.category == row["category"]:
                continue
            connection.execute(
                "INSERT INTO invoice_edits"
                " (invoice_id, field, old_value, new_value, edited_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    row["id"],
                    CATEGORY_REFRESH_FIELD,
                    row["category"],
                    result.category,
                    stamp,
                ),
            )
            connection.execute(
                "UPDATE invoices SET category = ?, category_rule = ? WHERE id = ?",
                (result.category, result.rule, row["id"]),
            )
            updated += 1
        db.set_setting(connection, RULES_VERSION_KEY, str(RULES_VERSION))
    return updated
