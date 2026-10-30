"""SQLite storage for the local invoice pool.

Money is stored as integer cents: SQLite REAL would introduce binary float
drift, and TEXT makes SUM() awkward. ``ParsedInvoice`` stays Decimal-based; this
module is the only place that converts.

The database is local-only user data. ``connect`` refuses repository paths
outside the ignored data directories and remote/synced storage; the CLI's
``--force`` is the explicit escape hatch.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from app.core.models import ParsedInvoice
from app.core.paths import is_unsafe_repo_path, remote_storage_reason
from app.core.text import normalize

DB_VERSION = 6
CENT = Decimal("0.01")
DEFAULT_DB_PATH = Path("data") / "invoicedesk.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    source_dir TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    sha256 TEXT PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    source_path TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    record_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    file_sha256 TEXT NOT NULL REFERENCES files(sha256) ON DELETE CASCADE,
    source_path TEXT NOT NULL,
    page_index INTEGER NOT NULL,
    page_count INTEGER NOT NULL,
    invoice_type TEXT NOT NULL,
    invoice_number TEXT NOT NULL DEFAULT '',
    invoice_date TEXT NOT NULL DEFAULT '',
    seller_name TEXT NOT NULL DEFAULT '',
    seller_tax_id TEXT NOT NULL DEFAULT '',
    buyer_name TEXT NOT NULL DEFAULT '',
    buyer_tax_id TEXT NOT NULL DEFAULT '',
    amount_total_cents INTEGER,
    amount_net_cents INTEGER,
    tax_amount_cents INTEGER,
    item_summary TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '其他费用',
    category_rule TEXT NOT NULL DEFAULT 'fallback',
    confidence TEXT NOT NULL DEFAULT '{}',
    warnings TEXT NOT NULL DEFAULT '[]',
    needs_review INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL,
    UNIQUE (file_sha256, page_index)
);

CREATE INDEX IF NOT EXISTS idx_invoices_number ON invoices(invoice_number);
CREATE INDEX IF NOT EXISTS idx_invoices_batch ON invoices(batch_id);
CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(invoice_date);

CREATE TABLE IF NOT EXISTS invoice_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    field TEXT NOT NULL,
    old_value TEXT NOT NULL DEFAULT '',
    new_value TEXT NOT NULL DEFAULT '',
    edited_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_invoice_edits_invoice ON invoice_edits(invoice_id);

CREATE TABLE IF NOT EXISTS category_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_type TEXT NOT NULL,
    pattern TEXT NOT NULL,
    category TEXT NOT NULL,
    confirm_count INTEGER NOT NULL DEFAULT 1,
    hit_count INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (match_type, pattern)
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL DEFAULT '',
    department TEXT NOT NULL DEFAULT '',
    reporter TEXT NOT NULL DEFAULT '',
    month TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    line_date TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '其他费用',
    description TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL,
    has_invoice INTEGER NOT NULL DEFAULT 0,
    invoice_note TEXT NOT NULL DEFAULT '',
    project_name TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    mileage_km REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_report_lines_report ON report_lines(report_id);

CREATE TABLE IF NOT EXISTS report_line_invoices (
    line_id INTEGER NOT NULL REFERENCES report_lines(id) ON DELETE CASCADE,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    PRIMARY KEY (line_id, invoice_id)
);

CREATE INDEX IF NOT EXISTS idx_report_line_invoices_invoice
    ON report_line_invoices(invoice_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


class DatabaseVersionError(RuntimeError):
    """The database was written by a newer application version."""


class UnsafeDatabasePathError(ValueError):
    """The database path would put financial data inside the repository."""


@dataclass(frozen=True)
class BatchStats:
    id: int
    created_at: str
    label: str
    source_dir: str
    file_count: int
    duplicate_count: int
    conflict_count: int
    invoice_count: int
    review_count: int
    amount_total: Decimal


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: str | Path, *, allow_unsafe_path: bool = False) -> sqlite3.Connection:
    """Open the database, create parent directories, and apply migrations.

    Refuses repository paths outside the ignored data directories so a UI or
    script cannot accidentally commit the pool. ``allow_unsafe_path`` is the
    explicit escape hatch behind the CLI's ``--force``.
    """
    db_path = Path(path)
    if not allow_unsafe_path:
        remote_reason = remote_storage_reason(db_path)
        if remote_reason is not None:
            raise UnsafeDatabasePathError(
                f"数据库路径位于{remote_reason}，财务数据只允许保存在本机固定盘；"
                "确需如此请加 --force。"
            )
        if is_unsafe_repo_path(db_path):
            raise UnsafeDatabasePathError(
                "数据库路径在仓库内，财务数据可能被带进 Git；"
                "请写到 data/、out/、tests/local/ 或仓库外（--force 例外）。"
            )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # A background import keeps one write transaction open for its whole run;
    # short reads and status polls should wait for it instead of failing.
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        init_db(connection)
    except Exception:
        connection.close()
        raise
    return connection


def init_db(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > DB_VERSION:
        raise DatabaseVersionError(
            f"数据库版本 {version} 高于当前程序支持的 {DB_VERSION}，请升级程序后再打开。"
        )
    if version < DB_VERSION:
        # Column ALTERs must land before SCHEMA so future indexes/views in
        # SCHEMA may reference the new column without breaking old databases.
        _migrate(connection, version)
        connection.executescript(SCHEMA)
        connection.execute(f"PRAGMA user_version = {DB_VERSION}")
        connection.commit()
    # Imported lazily: recategorize imports this module, and it must be fully
    # initialized before the first refresh runs. The pass is version-gated, so
    # an up-to-date pool pays one settings lookup.
    from app.core.recategorize import refresh_builtin_categories

    refresh_builtin_categories(connection)


def _migrate(connection: sqlite3.Connection, version: int) -> None:
    """Apply column additions that ``CREATE TABLE IF NOT EXISTS`` cannot.

    New tables come from ``SCHEMA``; existing tables need an explicit ALTER.
    A missing table is left to ``SCHEMA`` (brand-new or v2 databases), so the
    ALTER only runs when the table exists without the new column. Every step
    is guarded so a partially migrated database stays usable.
    """
    if version < 4:
        _add_missing_column(
            connection,
            "category_rules",
            "enabled",
            "enabled INTEGER NOT NULL DEFAULT 1",
        )
    if version < 6:
        _add_missing_column(
            connection,
            "invoices",
            "archived_at",
            "archived_at TEXT NOT NULL DEFAULT ''",
        )


def _add_missing_column(
    connection: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    """Add a column to an existing table; a missing table is left to SCHEMA."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if not exists:
        return
    columns = {
        row[1] for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def money_to_cents(value: Decimal | None) -> int | None:
    if value is None:
        return None
    return int(value.quantize(CENT, rounding=ROUND_HALF_UP) * 100)


def cents_to_money(value: int | None) -> Decimal | None:
    if value is None:
        return None
    return (Decimal(value) / 100).quantize(CENT)


def create_batch(
    connection: sqlite3.Connection,
    *,
    label: str,
    source_dir: str,
    created_at: str | None = None,
) -> int:
    cursor = connection.execute(
        "INSERT INTO batches (created_at, label, source_dir) VALUES (?, ?, ?)",
        (created_at or utc_now(), label, source_dir),
    )
    batch_id = cursor.lastrowid
    if batch_id is None:  # pragma: no cover - sqlite always returns a row id here
        raise RuntimeError("无法创建批次")
    return batch_id


def invoice_index(
    connection: sqlite3.Connection,
) -> dict[str, list[tuple[int, tuple[str, int | None, str]]]]:
    """Invoice number -> [(row id, (date, total cents, seller))].

    The identity tuple decides whether a repeated number is the same invoice
    (skip it) or a conflict (keep both and flag them for review).
    """
    index: dict[str, list[tuple[int, tuple[str, int | None, str]]]] = {}
    for row in connection.execute(
        "SELECT id, invoice_number, invoice_date, amount_total_cents, seller_name"
        " FROM invoices WHERE invoice_number <> '' AND archived_at = ''"
    ):
        index.setdefault(row["invoice_number"], []).append(
            (row["id"], (row["invoice_date"], row["amount_total_cents"], row["seller_name"]))
        )
    return index


def append_warning(connection: sqlite3.Connection, invoice_id: int, warning: str) -> None:
    """Add a warning once and force the record into the review queue."""
    warning = normalize(warning)
    row = connection.execute(
        "SELECT warnings FROM invoices WHERE id = ?", (invoice_id,)
    ).fetchone()
    if row is None:  # pragma: no cover - caller passes ids it just saw
        return
    warnings = json.loads(row["warnings"])
    if warning in warnings:
        # A new conflict can arrive after the record was confirmed; the reason
        # did not change, but it must still return to the review queue.
        connection.execute(
            "UPDATE invoices SET needs_review = 1 WHERE id = ?", (invoice_id,)
        )
        return
    warnings.append(warning)
    connection.execute(
        "UPDATE invoices SET warnings = ?, needs_review = 1 WHERE id = ?",
        (json.dumps(warnings, ensure_ascii=False), invoice_id),
    )


def insert_file(
    connection: sqlite3.Connection,
    *,
    sha256: str,
    batch_id: int,
    source_path: str,
    page_count: int,
    imported_at: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO files (sha256, batch_id, source_path, page_count, imported_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (sha256, batch_id, source_path, page_count, imported_at or utc_now()),
    )


def finish_file(
    connection: sqlite3.Connection,
    sha256: str,
    *,
    record_count: int,
    duplicate_count: int,
    conflict_count: int = 0,
) -> None:
    connection.execute(
        "UPDATE files SET record_count = ?, duplicate_count = ?, conflict_count = ?"
        " WHERE sha256 = ?",
        (record_count, duplicate_count, conflict_count, sha256),
    )


def insert_invoice(
    connection: sqlite3.Connection,
    *,
    batch_id: int,
    sha256: str,
    invoice: ParsedInvoice,
    extra_warnings: Sequence[str] = (),
    imported_at: str | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO invoices (
            batch_id, file_sha256, source_path, page_index, page_count,
            invoice_type, invoice_number, invoice_date,
            seller_name, seller_tax_id, buyer_name, buyer_tax_id,
            amount_total_cents, amount_net_cents, tax_amount_cents,
            item_summary, category, category_rule,
            confidence, warnings, needs_review, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            sha256,
            invoice.source_path,
            invoice.page_index,
            invoice.page_count,
            invoice.invoice_type.value,
            invoice.invoice_number,
            invoice.invoice_date,
            normalize(invoice.seller_name),
            invoice.seller_tax_id,
            normalize(invoice.buyer_name),
            invoice.buyer_tax_id,
            money_to_cents(invoice.amount_total),
            money_to_cents(invoice.amount_net),
            money_to_cents(invoice.tax_amount),
            normalize(invoice.item_summary),
            invoice.category,
            invoice.category_rule,
            json.dumps(invoice.confidence, ensure_ascii=False, sort_keys=True),
            json.dumps(
                [normalize(w) for w in (*invoice.warnings, *extra_warnings)],
                ensure_ascii=False,
            ),
            int(invoice.needs_review or bool(extra_warnings)),
            imported_at or utc_now(),
        ),
    )
    invoice_id = cursor.lastrowid
    if invoice_id is None:  # pragma: no cover - sqlite always returns a row id here
        raise RuntimeError("无法写开发票")
    return invoice_id


RECENT_IMPORT_LIMIT = 8
RECENT_IMPORT_KEY = "recent_import_paths"


def get_setting(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    row = connection.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row is not None else default


def set_setting(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def recent_import_paths(connection: sqlite3.Connection) -> list[str]:
    """Folders the user imported from recently, newest first."""
    raw = get_setting(connection, RECENT_IMPORT_KEY, "[]")
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value.strip()]


def remember_import_path(
    connection: sqlite3.Connection, path: str, *, limit: int = RECENT_IMPORT_LIMIT
) -> list[str]:
    """Move ``path`` to the front of the recent-folder list and persist it.

    Windows paths are compared case-insensitively so the same folder typed
    with different casing does not fill the list with duplicates.
    """
    text = str(path).strip()
    if not text:
        return recent_import_paths(connection)
    key = text.casefold().rstrip("\\/")
    history = recent_import_paths(connection)
    result = [text]
    for item in history:
        if item.casefold().rstrip("\\/") != key:
            result.append(item)
    result = result[: max(1, limit)]
    set_setting(connection, RECENT_IMPORT_KEY, json.dumps(result, ensure_ascii=False))
    return result


def report_choices(connection: sqlite3.Connection, *, limit: int = 20) -> dict[str, list[str]]:
    """Distinct company / department / reporter values, most used first."""
    choices: dict[str, list[str]] = {}
    for field in ("company", "department", "reporter"):
        rows = connection.execute(
            f"SELECT {field} AS value, COUNT(*) AS uses FROM reports"
            f" WHERE TRIM({field}) <> '' GROUP BY {field}"
            f" ORDER BY uses DESC, value LIMIT ?",
            (limit,),
        ).fetchall()
        choices[field] = [row["value"] for row in rows]
    return choices


def batch_stats(connection: sqlite3.Connection) -> list[BatchStats]:
    """Batch aggregates.

    ``review_count`` is live: a later import can flag an older invoice. The
    duplicate/conflict counters are import-time counters for the batch that
    introduced them.
    """
    rows = connection.execute(
        """
        SELECT b.id, b.created_at, b.label, b.source_dir,
               (SELECT COUNT(*) FROM files f WHERE f.batch_id = b.id) AS file_count,
               (SELECT COALESCE(SUM(f.duplicate_count), 0) FROM files f
                WHERE f.batch_id = b.id) AS duplicate_count,
               (SELECT COALESCE(SUM(f.conflict_count), 0) FROM files f
                WHERE f.batch_id = b.id) AS conflict_count,
               COUNT(i.id) AS invoice_count,
               COALESCE(SUM(i.needs_review), 0) AS review_count,
               COALESCE(SUM(i.amount_total_cents), 0) AS amount_cents
        FROM batches b
        LEFT JOIN invoices i ON i.batch_id = b.id AND i.archived_at = ''
        GROUP BY b.id
        ORDER BY b.id
        """
    ).fetchall()
    return [
        BatchStats(
            id=row["id"],
            created_at=row["created_at"],
            label=row["label"],
            source_dir=row["source_dir"],
            file_count=row["file_count"],
            duplicate_count=row["duplicate_count"],
            conflict_count=row["conflict_count"],
            invoice_count=row["invoice_count"],
            review_count=row["review_count"],
            amount_total=cents_to_money(row["amount_cents"]) or Decimal("0.00"),
        )
        for row in rows
    ]
