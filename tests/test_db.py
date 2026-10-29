from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app.core import db
from app.core.models import InvoiceType, ParsedInvoice
from app.core.paths import remote_storage_reason

ROOT = Path(__file__).resolve().parents[1]


def sample_invoice(**overrides) -> ParsedInvoice:
    values = {
        "source_path": r"D:\invoices\a.pdf",
        "page_index": 0,
        "page_count": 1,
        "invoice_type": InvoiceType.FULL_DIGITAL_NORMAL,
        "invoice_number": "26502000000000000001",
        "invoice_date": "2026-07-30",
        "seller_name": "示例乙酒店",
        "seller_tax_id": "92500000TEST00002X",
        "buyer_name": "示例甲公司",
        "buyer_tax_id": "91500000TEST00001X",
        "amount_total": Decimal("1060.00"),
        "amount_net": Decimal("1000.00"),
        "tax_amount": Decimal("60.00"),
        "item_summary": "住宿费",
        "category": "差旅费",
        "category_rule": "item.travel",
        "confidence": {"amount": "high", "invoice_number": "high"},
        "warnings": [],
    }
    values.update(overrides)
    return ParsedInvoice(**values)


def insert_sample(connection: sqlite3.Connection, invoice: ParsedInvoice | None = None) -> int:
    batch_id = db.create_batch(
        connection,
        label="2026年7月",
        source_dir=r"D:\invoices",
        created_at="2026-10-28T00:00:00+00:00",
    )
    sha = f"hash-{invoice.page_index if invoice else 0}"
    db.insert_file(
        connection,
        sha256=sha,
        batch_id=batch_id,
        source_path=invoice.source_path if invoice else r"D:\invoices\a.pdf",
        page_count=1,
        imported_at="2026-10-28T00:00:00+00:00",
    )
    db.insert_invoice(
        connection,
        batch_id=batch_id,
        sha256=sha,
        invoice=invoice or sample_invoice(),
        imported_at="2026-10-28T00:00:00+00:00",
    )
    return batch_id


def test_connect_creates_schema_and_parent_directories(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "nested" / "invoices.db")
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db.DB_VERSION
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "batches",
            "files",
            "invoices",
            "invoice_edits",
            "category_rules",
        } <= tables
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_connect_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "invoices.db"
    db.connect(path).close()
    connection = db.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db.DB_VERSION
    finally:
        connection.close()


def test_v2_database_gains_correction_tables_without_data_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old.db"
    connection = db.connect(path)
    try:
        insert_sample(connection)
        connection.executescript(
            "DROP TABLE invoice_edits;"
            " DROP TABLE category_rules;"
            " PRAGMA user_version = 2;"
        )
        connection.commit()
    finally:
        connection.close()

    connection = db.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db.DB_VERSION
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"invoice_edits", "category_rules"} <= tables
        assert connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1

        edit_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(invoice_edits)")
        }
        assert edit_columns == {
            "id",
            "invoice_id",
            "field",
            "old_value",
            "new_value",
            "edited_at",
        }
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(invoice_edits)")
        }
        assert "idx_invoice_edits_invoice" in indexes
        assert (
            connection.execute("SELECT category_rule FROM invoices").fetchone()[0]
            == "item.travel"
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO category_rules"
                " (match_type, pattern, category, confirm_count, hit_count,"
                "  created_at, updated_at)"
                " VALUES ('seller', 'X', 'Y', 1, 0, 'now', 'now')"
            )
            connection.execute(
                "INSERT INTO category_rules"
                " (match_type, pattern, category, confirm_count, hit_count,"
                "  created_at, updated_at)"
                " VALUES ('seller', 'X', '其他费用', 1, 0, 'now', 'now')"
            )
        connection.rollback()
    finally:
        connection.close()


def test_v3_database_gains_enabled_flag_and_keeps_rules(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    connection = db.connect(path)
    try:
        connection.executescript(
            "DROP TABLE category_rules;"
            " CREATE TABLE category_rules ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  match_type TEXT NOT NULL,"
            "  pattern TEXT NOT NULL,"
            "  category TEXT NOT NULL,"
            "  confirm_count INTEGER NOT NULL DEFAULT 1,"
            "  hit_count INTEGER NOT NULL DEFAULT 0,"
            "  created_at TEXT NOT NULL,"
            "  updated_at TEXT NOT NULL,"
            "  UNIQUE (match_type, pattern)"
            " );"
            " INSERT INTO category_rules"
            "  (match_type, pattern, category, confirm_count, hit_count,"
            "   created_at, updated_at)"
            "  VALUES ('seller', '旧销售方', '餐饮费', 3, 7, 'old', 'old');"
            " PRAGMA user_version = 3;"
        )
        connection.commit()
    finally:
        connection.close()

    connection = db.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db.DB_VERSION
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(category_rules)")
        }
        assert "enabled" in columns
        rule = connection.execute(
            "SELECT pattern, category, confirm_count, hit_count, enabled"
            " FROM category_rules"
        ).fetchone()
        assert tuple(rule) == ("旧销售方", "餐饮费", 3, 7, 1)
    finally:
        connection.close()


def test_v3_migration_alters_before_schema_ddl(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "old.db"
    connection = db.connect(path)
    try:
        connection.executescript(
            "DROP TABLE category_rules;"
            " CREATE TABLE category_rules ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  match_type TEXT NOT NULL,"
            "  pattern TEXT NOT NULL,"
            "  category TEXT NOT NULL,"
            "  confirm_count INTEGER NOT NULL DEFAULT 1,"
            "  hit_count INTEGER NOT NULL DEFAULT 0,"
            "  created_at TEXT NOT NULL,"
            "  updated_at TEXT NOT NULL,"
            "  UNIQUE (match_type, pattern)"
            " );"
            " PRAGMA user_version = 3;"
        )
        connection.commit()
    finally:
        connection.close()

    # A future SCHEMA may reference the new column in an index or view. The
    # ALTER must therefore run before SCHEMA, or an old database would fail
    # with "no such column: enabled" before reaching the migration.
    monkeypatch.setattr(
        db,
        "SCHEMA",
        db.SCHEMA
        + "\nCREATE INDEX IF NOT EXISTS idx_category_rules_enabled"
        + " ON category_rules(enabled);",
    )
    connection = db.connect(path)
    try:
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(category_rules)")
        }
        assert "idx_category_rules_enabled" in indexes
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db.DB_VERSION
    finally:
        connection.close()


def test_connect_refuses_repository_paths(tmp_path: Path) -> None:
    target = ROOT / "_test_stray.db"
    target.unlink(missing_ok=True)
    try:
        with pytest.raises(db.UnsafeDatabasePathError):
            db.connect(target)
        assert not target.exists()
    finally:
        target.unlink(missing_ok=True)


def test_connect_allows_force_inside_repository() -> None:
    target = ROOT / "data" / "_test_guard.db"
    target.unlink(missing_ok=True)
    try:
        connection = db.connect(target, allow_unsafe_path=True)
        connection.close()
        assert target.exists()
    finally:
        target.unlink(missing_ok=True)


def test_remote_storage_reason_detects_unc_and_sync_dirs(
    tmp_path: Path, monkeypatch
) -> None:
    assert remote_storage_reason(Path(r"\\server\share\db")) == "网络共享路径"
    synced = tmp_path / "OneDrive"
    synced.mkdir()
    monkeypatch.setenv("OneDrive", str(synced))
    assert remote_storage_reason(synced / "invoicedesk.db") == "云同步目录"


def test_connect_refuses_unc_database_paths() -> None:
    with pytest.raises(db.UnsafeDatabasePathError):
        db.connect(Path(r"\\server\share\invoicedesk.db"))


def test_connect_refuses_onedrive_by_default(tmp_path: Path, monkeypatch) -> None:
    synced = tmp_path / "OneDrive"
    synced.mkdir()
    monkeypatch.setenv("OneDrive", str(synced))
    target = synced / "invoicedesk.db"

    with pytest.raises(db.UnsafeDatabasePathError):
        db.connect(target)
    assert not target.exists()

    connection = db.connect(target, allow_unsafe_path=True)
    connection.close()
    target.unlink(missing_ok=True)


def test_newer_database_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 99")
    raw.commit()
    raw.close()

    with pytest.raises(db.DatabaseVersionError):
        db.connect(path)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1060.00"), 106000),
        (Decimal("0.01"), 1),
        (Decimal("12.345"), 1235),
        (Decimal("-12.34"), -1234),
        (None, None),
    ],
)
def test_money_to_cents(value: Decimal | None, expected: int | None) -> None:
    assert db.money_to_cents(value) == expected


def test_cents_to_money_round_trip() -> None:
    assert db.cents_to_money(106000) == Decimal("1060.00")
    assert db.cents_to_money(-1234) == Decimal("-12.34")
    assert db.cents_to_money(None) is None


def test_insert_invoice_round_trip(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "invoices.db")
    try:
        insert_sample(connection)
        row = connection.execute("SELECT * FROM invoices").fetchone()
        assert row["invoice_type"] == "full_digital_normal"
        assert row["invoice_number"] == "26502000000000000001"
        assert row["amount_total_cents"] == 106000
        assert row["amount_net_cents"] == 100000
        assert row["tax_amount_cents"] == 6000
        assert row["needs_review"] == 0
        assert row["confidence"] == '{"amount": "high", "invoice_number": "high"}'
        assert row["warnings"] == "[]"
        assert row["source_path"] == r"D:\invoices\a.pdf"
    finally:
        connection.close()


def test_same_invoice_number_may_be_stored_twice(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "invoices.db")
    try:
        insert_sample(connection)
        sha = "hash-second"
        batch_id = connection.execute("SELECT id FROM batches").fetchone()[0]
        db.insert_file(
            connection,
            sha256=sha,
            batch_id=batch_id,
            source_path=r"D:\invoices\b.pdf",
            page_count=1,
        )
        db.insert_invoice(
            connection,
            batch_id=batch_id,
            sha256=sha,
            invoice=sample_invoice(
                source_path=r"D:\invoices\b.pdf",
                amount_total=Decimal("200.00"),
            ),
        )
        assert connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 2
    finally:
        connection.close()


def test_append_warning_is_idempotent_and_marks_review(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "invoices.db")
    try:
        insert_sample(connection)
        invoice_id = connection.execute("SELECT id FROM invoices").fetchone()[0]
        db.append_warning(connection, invoice_id, "票号冲突")
        db.append_warning(connection, invoice_id, "票号冲突")
        row = connection.execute(
            "SELECT warnings, needs_review FROM invoices WHERE id = ?", (invoice_id,)
        ).fetchone()
        assert json.loads(row["warnings"]) == ["票号冲突"]
        assert row["needs_review"] == 1
    finally:
        connection.close()


def test_empty_invoice_numbers_are_not_deduplicated(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "invoices.db")
    try:
        batch_id = db.create_batch(connection, label="扫描件", source_dir=r"D:\scans")
        db.insert_file(
            connection,
            sha256="scan-hash",
            batch_id=batch_id,
            source_path=r"D:\scans\scan.pdf",
            page_count=2,
        )
        for page in (0, 1):
            db.insert_invoice(
                connection,
                batch_id=batch_id,
                sha256="scan-hash",
                invoice=sample_invoice(
                    source_path=r"D:\scans\scan.pdf",
                    page_index=page,
                    page_count=2,
                    invoice_type=InvoiceType.SCANNED,
                    invoice_number="",
                    amount_total=None,
                ),
            )
        count = connection.execute(
            "SELECT COUNT(*) FROM invoices WHERE invoice_number = ''"
        ).fetchone()[0]
        assert count == 2
    finally:
        connection.close()


def test_batch_stats_aggregate(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "invoices.db")
    try:
        first = insert_sample(connection)
        second = db.create_batch(connection, label="2026年8月", source_dir=r"D:\august")
        connection.execute(
            "INSERT INTO files (sha256, batch_id, source_path, page_count, duplicate_count,"
            " conflict_count, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("aug-hash", second, r"D:\august\b.pdf", 1, 2, 1, db.utc_now()),
        )
        db.insert_invoice(
            connection,
            batch_id=second,
            sha256="aug-hash",
            invoice=sample_invoice(
                invoice_number="26502000000000000002",
                amount_total=Decimal("200.00"),
                invoice_type=InvoiceType.SCANNED,
                invoice_date="",
            ),
        )
        stats = db.batch_stats(connection)
        assert [item.id for item in stats] == [first, second]
        assert stats[0].invoice_count == 1
        assert stats[0].amount_total == Decimal("1060.00")
        assert stats[0].review_count == 0
        assert stats[1].duplicate_count == 2
        assert stats[1].conflict_count == 1
        assert stats[1].invoice_count == 1
        assert stats[1].review_count == 1
        assert stats[1].amount_total == Decimal("200.00")
    finally:
        connection.close()


def test_invoice_index_excludes_empty_and_keeps_identity(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "invoices.db")
    try:
        insert_sample(connection)
        batch_id = connection.execute("SELECT id FROM batches").fetchone()[0]
        db.insert_file(
            connection,
            sha256="scan-hash",
            batch_id=batch_id,
            source_path=r"D:\invoices\scan.pdf",
            page_count=1,
        )
        db.insert_invoice(
            connection,
            batch_id=batch_id,
            sha256="scan-hash",
            invoice=sample_invoice(invoice_number="", source_path=r"D:\invoices\scan.pdf"),
        )
        index = db.invoice_index(connection)
        assert set(index) == {"26502000000000000001"}
        invoice_id, identity = index["26502000000000000001"][0]
        assert invoice_id > 0
        assert identity == ("2026-07-30", 106000, "示例乙酒店")
    finally:
        connection.close()


def test_recent_import_paths_are_deduped_and_newest_first(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "pool.db")
    try:
        db.remember_import_path(connection, r"D:\发票\2026-07")
        db.remember_import_path(connection, r"D:\发票\2026-08")
        db.remember_import_path(connection, r"d:\发票\2026-07")
        connection.commit()
        assert db.recent_import_paths(connection) == [
            r"d:\发票\2026-07",
            r"D:\发票\2026-08",
        ]
    finally:
        connection.close()


def test_recent_import_paths_are_bounded_and_corrupt_state_is_ignored(
    tmp_path: Path,
) -> None:
    connection = db.connect(tmp_path / "pool.db")
    try:
        for index in range(12):
            db.remember_import_path(connection, f"C:/invoices/{index}")
        connection.commit()
        paths = db.recent_import_paths(connection)
        assert len(paths) == db.RECENT_IMPORT_LIMIT
        assert paths[0] == "C:/invoices/11"

        db.set_setting(connection, db.RECENT_IMPORT_KEY, "{not json")
        connection.commit()
        assert db.recent_import_paths(connection) == []
    finally:
        connection.close()


def test_report_choices_are_ordered_by_use(tmp_path: Path) -> None:
    from app.core import reports

    connection = db.connect(tmp_path / "pool.db")
    try:
        for company, reporter in (
            ("甲公司", "张三"),
            ("甲公司", "张三"),
            ("乙公司", "李四"),
        ):
            reports.create_report(
                connection,
                company=company,
                reporter=reporter,
                month="2026-07",
                department="研发部",
            )
        connection.commit()
        choices = db.report_choices(connection)
        assert choices["company"] == ["甲公司", "乙公司"]
        assert choices["reporter"] == ["张三", "李四"]
        assert choices["department"] == ["研发部"]
    finally:
        connection.close()


def test_reopening_refreshes_stale_builtin_category_with_audit(tmp_path: Path) -> None:
    path = tmp_path / "pool.db"
    connection = db.connect(path)
    try:
        invoice_id = insert_sample(
            connection,
            sample_invoice(
                source_path=r"D:\invoices\餐费.pdf",
                seller_name="示例江景酒店",
                item_summary="餐饮服务",
                category="差旅费",
                category_rule="seller.hotel",
            ),
        )
        # Simulate a pool last written by an older build: no rules version yet.
        connection.execute(
            "DELETE FROM settings WHERE key = 'category_rules_version'"
        )
        connection.commit()
    finally:
        connection.close()

    connection = db.connect(path)
    try:
        row = connection.execute(
            "SELECT category, category_rule FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        assert tuple(row) == ("餐饮费", "item.meal")
        edits = [
            tuple(edit)
            for edit in connection.execute(
                "SELECT field, old_value, new_value FROM invoice_edits"
                " WHERE invoice_id = ? ORDER BY id",
                (invoice_id,),
            )
        ]
        assert edits == [("category_refresh", "差旅费", "餐饮费")]
        assert db.get_setting(connection, "category_rules_version")

        # A rules refresh is not a human correction: the workbench badge and
        # the dashboard "已修正" count must stay at zero.
        from app.core import workbench

        listed = workbench.list_invoices(connection)
        assert [(row.id, row.edit_count) for row in listed] == [(invoice_id, 0)]
        assert workbench.dashboard_summary(listed).edited_count == 0
    finally:
        connection.close()


def test_refresh_keeps_manual_and_learned_categories(tmp_path: Path) -> None:
    path = tmp_path / "pool.db"
    connection = db.connect(path)
    try:
        manual_id = insert_sample(
            connection,
            sample_invoice(
                page_index=0,
                seller_name="示例江景酒店",
                item_summary="餐饮服务",
                category="其他费用",
                category_rule="manual",
            ),
        )
        learned_id = insert_sample(
            connection,
            sample_invoice(
                page_index=1,
                seller_name="示例江景酒店",
                item_summary="餐饮服务",
                category="差旅费",
                category_rule="learned.seller",
            ),
        )
        connection.execute(
            "DELETE FROM settings WHERE key = 'category_rules_version'"
        )
        connection.commit()
    finally:
        connection.close()

    connection = db.connect(path)
    try:
        rows = {
            row[0]: (row[1], row[2])
            for row in connection.execute(
                "SELECT id, category, category_rule FROM invoices"
            )
        }
        assert rows[manual_id] == ("其他费用", "manual")
        assert rows[learned_id] == ("差旅费", "learned.seller")
        assert connection.execute("SELECT COUNT(*) FROM invoice_edits").fetchone()[0] == 0
    finally:
        connection.close()


def test_current_rules_version_skips_the_refresh_pass(tmp_path: Path) -> None:
    from app.core.categories import RULES_VERSION

    path = tmp_path / "pool.db"
    connection = db.connect(path)
    try:
        invoice_id = insert_sample(
            connection,
            sample_invoice(
                seller_name="示例江景酒店",
                item_summary="餐饮服务",
                category="差旅费",
                category_rule="seller.hotel",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    # First reopen applies the refresh; afterwards the version marker is
    # current, so a hand-written stale value must survive the next open.
    connection = db.connect(path)
    try:
        connection.execute(
            "UPDATE invoices SET category = '差旅费', category_rule = 'seller.hotel'"
            " WHERE id = ?",
            (invoice_id,),
        )
        connection.commit()
        assert db.get_setting(connection, "category_rules_version") == str(RULES_VERSION)
    finally:
        connection.close()

    connection = db.connect(path)
    try:
        row = connection.execute(
            "SELECT category, category_rule FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        assert tuple(row) == ("差旅费", "seller.hotel")
    finally:
        connection.close()


def test_v5_database_gains_archived_at_column(tmp_path: Path) -> None:
    """Archiving needs a v6 column; old pools must migrate without data loss."""
    path = tmp_path / "v5.db"
    connection = db.connect(path)
    try:
        batch_id = db.create_batch(
            connection, label="旧批次", source_dir=r"D:\invoices"
        )
        db.insert_file(
            connection,
            sha256="old-sha",
            batch_id=batch_id,
            source_path=r"D:\invoices\a.pdf",
            page_count=1,
        )
        invoice_id = db.insert_invoice(
            connection,
            batch_id=batch_id,
            sha256="old-sha",
            invoice=sample_invoice(),
        )
        connection.commit()
        # Rewind the schema to v5 and drop the column added in v6.
        connection.execute("ALTER TABLE invoices DROP COLUMN archived_at")
        connection.execute("PRAGMA user_version = 5")
        connection.commit()
    finally:
        connection.close()

    connection = db.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db.DB_VERSION
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(invoices)")
        }
        assert "archived_at" in columns
        row = connection.execute(
            "SELECT invoice_number, archived_at FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        assert tuple(row) == ("26502000000000000001", "")
    finally:
        connection.close()
