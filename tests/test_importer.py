from __future__ import annotations

import json
import shutil
import threading
from decimal import Decimal
from pathlib import Path

import fitz
import pytest

from app.core import db, importer, learning
from app.core.importer import CONFLICT_WARNING, import_folder, sha256_file
from tests.pdf_factory import full_digital_lines, make_pdf, make_pdf_pages


def write_invoice(
    folder: Path,
    name: str,
    *,
    number: str = "26502000000000000001",
    seller: str = "示例乙酒店",
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return make_pdf(
        folder,
        full_digital_lines(number=number, seller=seller),
        name=name,
    )


def different_bytes(path: Path) -> None:
    """Change PDF metadata only, so parsing is identical but the hash differs."""
    with fitz.open(path) as document:
        document.set_metadata({"title": "rescan copy"})
        document.saveIncr()


def blank_pdf(path: Path) -> Path:
    document = fitz.open()
    document.new_page(width=300, height=200)
    document.save(path)
    document.close()
    return path


def test_import_stores_invoice_and_leaves_source_untouched(tmp_path: Path) -> None:
    folder = tmp_path / "2026年7月"
    pdf = write_invoice(folder, "a.pdf")
    original = pdf.read_bytes()
    original_mtime = pdf.stat().st_mtime_ns

    connection = db.connect(tmp_path / "invoices.db")
    try:
        summary = import_folder(connection, folder)
        assert summary.batch_id == 1
        assert summary.label == "2026年7月"
        assert summary.files_seen == 1
        assert summary.files_imported == 1
        assert summary.files_duplicate == 0
        assert summary.invoices_imported == 1
        assert summary.invoices_duplicate == 0
        assert summary.amount_total == Decimal("1060.00")

        row = connection.execute("SELECT * FROM invoices").fetchone()
        assert row["invoice_number"] == "26502000000000000001"
        assert row["invoice_date"] == "2026-07-30"
        assert row["seller_name"] == "示例乙酒店"
        assert row["amount_total_cents"] == 106000
        assert row["source_path"] == str(pdf.resolve())
        assert row["batch_id"] == 1
    finally:
        connection.close()

    assert pdf.read_bytes() == original
    assert pdf.stat().st_mtime_ns == original_mtime


def test_learned_seller_rule_overrides_builtin_category_on_import(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "learned"
    write_invoice(folder, "a.pdf", number="LEARN1", seller="示例乙酒店")
    write_invoice(folder, "b.pdf", number="LEARN2", seller="示例丙酒店")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        with connection:
            learning.remember_seller_category(connection, "示例乙酒店", "餐饮费")
        summary = import_folder(connection, folder)
        assert summary.invoices_imported == 2

        rows = {
            row["seller_name"]: row
            for row in connection.execute("SELECT * FROM invoices")
        }
        learned = rows["示例乙酒店"]
        assert learned["category"] == "餐饮费"
        assert learned["category_rule"] == "learned.seller"
        assert json.loads(learned["confidence"])["category"] == "high"
        assert rows["示例丙酒店"]["category"] == "差旅费"

        rule = connection.execute("SELECT * FROM category_rules").fetchone()
        assert rule["category"] == "餐饮费"
        assert rule["hit_count"] == 1
    finally:
        connection.close()


def test_rule_hits_count_only_new_records(tmp_path: Path) -> None:
    first = tmp_path / "first"
    write_invoice(first, "a.pdf", number="10000001", seller="示例乙酒店")
    write_invoice(first, "b.pdf", number="10000002", seller="示例乙酒店")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        with connection:
            learning.remember_seller_category(connection, "示例乙酒店", "餐饮费")
        assert import_folder(connection, first).invoices_imported == 2
        assert (
            connection.execute("SELECT hit_count FROM category_rules").fetchone()[0] == 2
        )

        second = tmp_path / "second"
        second.mkdir()
        shutil.copy2(first / "a.pdf", second / "a.pdf")
        write_invoice(second, "c.pdf", number="10000003", seller="示例乙酒店")
        summary = import_folder(connection, second)

        assert summary.files_imported == 1
        assert summary.files_duplicate == 1
        assert summary.invoices_imported == 1
        assert (
            connection.execute("SELECT hit_count FROM category_rules").fetchone()[0] == 3
        )
        categories = {
            row[0]
            for row in connection.execute("SELECT category FROM invoices")
        }
        assert categories == {"餐饮费"}
    finally:
        connection.close()


def test_rule_hits_do_not_count_identity_duplicates(tmp_path: Path) -> None:
    first = tmp_path / "first"
    original = write_invoice(first, "a.pdf", number="10000021", seller="示例乙酒店")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        with connection:
            learning.remember_seller_category(connection, "示例乙酒店", "餐饮费")
        assert import_folder(connection, first).invoices_imported == 1
        assert (
            connection.execute("SELECT hit_count FROM category_rules").fetchone()[0] == 1
        )

        second = tmp_path / "second"
        second.mkdir()
        copy = second / "b.pdf"
        shutil.copy2(original, copy)
        different_bytes(copy)
        write_invoice(second, "c.pdf", number="10000022", seller="示例乙酒店")

        summary = import_folder(connection, second)
        assert summary.files_imported == 2
        assert summary.invoices_imported == 1
        assert summary.invoices_duplicate == 1
        assert (
            connection.execute("SELECT hit_count FROM category_rules").fetchone()[0] == 2
        )
    finally:
        connection.close()


def test_rule_change_applies_to_the_next_import(tmp_path: Path) -> None:
    first = tmp_path / "first"
    write_invoice(first, "a.pdf", number="10000001", seller="示例乙酒店")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        with connection:
            learning.remember_seller_category(connection, "示例乙酒店", "餐饮费")
        import_folder(connection, first)
        assert (
            connection.execute(
                "SELECT category FROM invoices WHERE invoice_number = '10000001'"
            ).fetchone()[0]
            == "餐饮费"
        )

        with connection:
            changed = learning.remember_seller_category(
                connection, "示例乙酒店", "办公费用"
            )
        assert changed is not None
        assert changed.confirm_count == 1

        second = tmp_path / "second"
        write_invoice(second, "b.pdf", number="10000002", seller="示例乙酒店")
        import_folder(connection, second)

        row = connection.execute(
            "SELECT category, category_rule FROM invoices WHERE invoice_number = '10000002'"
        ).fetchone()
        assert (row["category"], row["category_rule"]) == ("办公费用", "learned.seller")
        rule = connection.execute(
            "SELECT hit_count, confirm_count FROM category_rules"
        ).fetchone()
        assert (rule["hit_count"], rule["confirm_count"]) == (2, 1)
    finally:
        connection.close()


def test_reimport_same_folder_creates_no_batch(tmp_path: Path) -> None:
    folder = tmp_path / "july"
    write_invoice(folder, "a.pdf")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        import_folder(connection, folder)
        summary = import_folder(connection, folder)
        assert summary.batch_id is None
        assert summary.has_new_data is False
        assert summary.files_seen == 1
        assert summary.files_imported == 0
        assert summary.files_duplicate == 1
        assert summary.invoices_imported == 0
        assert connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1
    finally:
        connection.close()


def test_reimport_retries_old_scanned_records(tmp_path: Path, monkeypatch) -> None:
    """A hash-skipped file must still gain OCR fields when the row is old."""
    monkeypatch.setattr("app.core.ocr.is_available", lambda: True)
    folder = tmp_path / "october"
    write_invoice(folder, "a.pdf")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        import_folder(connection, folder)
        connection.execute(
            "UPDATE invoices SET invoice_type = 'scanned', invoice_number = '',"
            " invoice_date = '', seller_name = '', buyer_name = '',"
            " amount_total_cents = NULL"
        )
        connection.commit()

        summary = import_folder(connection, folder)

        assert summary.batch_id is None
        assert summary.files_imported == 0
        assert summary.files_duplicate == 1
        assert summary.reparse_candidates == 1
        assert summary.reparsed == 1
        assert summary.reparse_failed == 0
        row = connection.execute("SELECT * FROM invoices").fetchone()
        assert row["invoice_type"] == "full_digital_normal"
        assert row["invoice_number"] == "26502000000000000001"
        assert row["seller_name"] == "示例乙酒店"
        assert row["amount_total_cents"] == 106000
    finally:
        connection.close()


def test_reimport_skips_reparse_when_ocr_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("app.core.ocr.is_available", lambda: False)
    folder = tmp_path / "october"
    write_invoice(folder, "a.pdf")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        import_folder(connection, folder)
        connection.execute(
            "UPDATE invoices SET invoice_type = 'scanned', invoice_number = ''"
        )
        connection.commit()

        summary = import_folder(connection, folder)

        assert summary.reparse_candidates == 0
        assert summary.reparsed == 0
        row = connection.execute("SELECT invoice_type FROM invoices").fetchone()
        assert row["invoice_type"] == "scanned"
    finally:
        connection.close()


def test_reimport_does_not_touch_recognized_duplicates(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("app.core.ocr.is_available", lambda: True)
    called: list[object] = []
    monkeypatch.setattr(
        "app.core.reparse.reparse_unrecognized",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )
    folder = tmp_path / "october"
    write_invoice(folder, "a.pdf")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        import_folder(connection, folder)
        summary = import_folder(connection, folder)
        assert summary.reparse_candidates == 0
        assert called == []
    finally:
        connection.close()


def test_same_invoice_number_in_another_file_is_skipped(tmp_path: Path) -> None:
    july = tmp_path / "july"
    august = tmp_path / "august"
    first = write_invoice(july, "a.pdf")
    second = write_invoice(august, "rescan.pdf")
    different_bytes(second)
    assert sha256_file(first) != sha256_file(second)

    connection = db.connect(tmp_path / "invoices.db")
    try:
        import_folder(connection, july)
        summary = import_folder(connection, august)
        assert summary.batch_id == 2
        assert summary.files_imported == 1
        assert summary.invoices_imported == 0
        assert summary.invoices_duplicate == 1
        assert summary.invoices_conflict == 0
        assert summary.amount_total == Decimal("0.00")

        file_row = connection.execute(
            "SELECT * FROM files WHERE batch_id = 2"
        ).fetchone()
        assert file_row["record_count"] == 0
        assert file_row["duplicate_count"] == 1
        assert connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1
    finally:
        connection.close()


def test_same_number_with_different_identity_is_kept_and_flagged(tmp_path: Path) -> None:
    july = tmp_path / "july"
    august = tmp_path / "august"
    write_invoice(july, "a.pdf", seller="示例乙酒店")
    write_invoice(august, "other.pdf", seller="示例丙酒店")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        import_folder(connection, july)
        summary = import_folder(connection, august)
        assert summary.invoices_imported == 1
        assert summary.invoices_duplicate == 0
        assert summary.invoices_conflict == 1
        assert summary.needs_review == 1
        assert connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 2
        rows = connection.execute("SELECT * FROM invoices ORDER BY id").fetchall()
        assert all(row["needs_review"] == 1 for row in rows)
        assert all(CONFLICT_WARNING in row["warnings"] for row in rows)
        file_row = connection.execute(
            "SELECT * FROM files WHERE batch_id = 2"
        ).fetchone()
        assert file_row["conflict_count"] == 1

        stats = db.batch_stats(connection)
        assert stats[0].review_count == 1  # live: the old invoice is re-flagged
        assert stats[0].conflict_count == 0  # introduced by batch 2
        assert stats[1].review_count == 1
        assert stats[1].conflict_count == 1
    finally:
        connection.close()


def test_same_number_without_identity_fields_is_not_silently_deduplicated(
    tmp_path: Path,
) -> None:
    july = tmp_path / "july"
    august = tmp_path / "august"
    july.mkdir()
    august.mkdir()
    lines = [
        (200, 20, "电子发票(普通发票)"),
        (430, 40, "发票号码:"),
        (480, 40, "26502000000000000009"),
    ]
    first = make_pdf(july, lines, name="a.pdf")
    second = make_pdf(august, lines, name="b.pdf")
    different_bytes(second)
    assert sha256_file(first) != sha256_file(second)

    connection = db.connect(tmp_path / "invoices.db")
    try:
        import_folder(connection, july)
        summary = import_folder(connection, august)
        assert summary.invoices_imported == 1
        assert summary.invoices_duplicate == 0
        assert summary.invoices_conflict == 1
        assert summary.needs_review == 1
        assert connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 2
        rows = connection.execute("SELECT * FROM invoices ORDER BY id").fetchall()
        assert all(row["needs_review"] == 1 for row in rows)
        assert all(CONFLICT_WARNING in row["warnings"] for row in rows)
    finally:
        connection.close()


def test_import_rolls_back_when_parsing_fails(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "july"
    write_invoice(folder, "a.pdf", number="26502000000000000001")
    write_invoice(folder, "b.pdf", number="26502000000000000002")

    real_parse = importer.parse_pdf
    calls = {"count": 0}

    def flaky(path):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("parse exploded")
        return real_parse(path)

    monkeypatch.setattr("app.core.importer.parse_pdf", flaky)

    connection = db.connect(tmp_path / "invoices.db")
    try:
        with pytest.raises(RuntimeError):
            import_folder(connection, folder)
        for table in ("batches", "files", "invoices"):
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, f"{table} kept partial rows"
    finally:
        connection.close()


def test_duplicate_file_within_one_scan_is_imported_once(tmp_path: Path) -> None:
    folder = tmp_path / "july"
    original = write_invoice(folder, "a.pdf")
    shutil.copyfile(original, folder / "copy.pdf")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        summary = import_folder(connection, folder)
        assert summary.files_seen == 2
        assert summary.files_imported == 1
        assert summary.files_duplicate == 1
        assert summary.invoices_imported == 1
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    finally:
        connection.close()


def test_multi_page_file_with_repeated_number_keeps_one_record(tmp_path: Path) -> None:
    folder = tmp_path / "july"
    folder.mkdir()
    lines = full_digital_lines()
    path = make_pdf_pages(folder, [lines, lines], name="two-pages.pdf")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        summary = import_folder(connection, folder)
        assert summary.files_imported == 1
        assert summary.invoices_imported == 1
        assert summary.invoices_duplicate == 1
        file_row = connection.execute("SELECT * FROM files").fetchone()
        assert file_row["page_count"] == 2
        assert file_row["record_count"] == 1
        assert file_row["duplicate_count"] == 1
        row = connection.execute("SELECT * FROM invoices").fetchone()
        assert row["source_path"] == str(path.resolve())
        assert row["page_count"] == 2
    finally:
        connection.close()


def test_limit_applies_before_deduplication(tmp_path: Path) -> None:
    folder = tmp_path / "july"
    write_invoice(folder, "a.pdf", number="26502000000000000001")
    write_invoice(folder, "b.pdf", number="26502000000000000002")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        summary = import_folder(connection, folder, limit=1)
        assert summary.files_seen == 1
        assert summary.invoices_imported == 1
        assert connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1
    finally:
        connection.close()


def test_empty_folder_imports_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    connection = db.connect(tmp_path / "invoices.db")
    try:
        summary = import_folder(connection, folder)
        assert summary.batch_id is None
        assert summary.files_seen == 0
        assert connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    finally:
        connection.close()


def test_blank_pdf_is_stored_for_review(tmp_path: Path) -> None:
    folder = tmp_path / "july"
    folder.mkdir()
    blank_pdf(folder / "blank.pdf")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        summary = import_folder(connection, folder)
        assert summary.invoices_imported == 1
        assert summary.needs_review == 1
        row = connection.execute("SELECT * FROM invoices").fetchone()
        assert row["invoice_type"] == "scanned"
        assert row["invoice_number"] == ""
        assert row["needs_review"] == 1
        assert row["amount_total_cents"] is None
    finally:
        connection.close()


def test_label_override_is_used(tmp_path: Path) -> None:
    folder = tmp_path / "july"
    write_invoice(folder, "a.pdf")
    connection = db.connect(tmp_path / "invoices.db")
    try:
        summary = import_folder(connection, folder, label="7月示例甲")
        assert summary.label == "7月示例甲"
        assert connection.execute("SELECT label FROM batches").fetchone()[0] == "7月示例甲"
    finally:
        connection.close()


def test_preflight_reports_counts_without_writing(tmp_path: Path) -> None:
    folder = tmp_path / "2026-07"
    write_invoice(folder, "a.pdf", number="26502000000000000041")
    write_invoice(folder, "b.pdf", number="26502000000000000042")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        plan = importer.plan_import(connection, folder)

        assert plan.label == "2026-07"
        assert plan.files_seen == 2
        assert plan.new_files == 2
        assert plan.duplicate_files == 0
        assert plan.ocr_pages == 0
        assert plan.reparse_candidates == 0
        assert connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0

        # The plan carries digests, so executing it imports the same files.
        summary = importer.execute_plan(connection, plan)
        assert summary.files_imported == 2
        assert summary.invoices_imported == 2
        assert summary.cancelled is False
    finally:
        connection.close()


def test_preflight_counts_pages_without_a_text_layer(tmp_path: Path) -> None:
    folder = tmp_path / "mixed"
    write_invoice(folder, "digital.pdf")
    blank_pdf(folder / "scan.pdf")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        assert importer.plan_import(connection, folder).ocr_pages == 1
        # The import path skips the estimate; only preflight pays for it.
        assert importer.plan_import(
            connection, folder, estimate_ocr=False
        ).ocr_pages == 0
    finally:
        connection.close()


def test_preflight_finds_duplicates_and_old_scanned_rows(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("app.core.ocr.is_available", lambda: True)
    folder = tmp_path / "october"
    write_invoice(folder, "a.pdf")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        import_folder(connection, folder)
        connection.execute(
            "UPDATE invoices SET invoice_type = 'scanned', invoice_number = ''"
        )
        connection.commit()

        plan = importer.plan_import(connection, folder)
        assert plan.new_files == 0
        assert plan.duplicate_files == 1
        assert plan.reparse_candidates == 1
    finally:
        connection.close()


def test_cancel_mid_import_rolls_back_everything(tmp_path: Path) -> None:
    folder = tmp_path / "batch"
    write_invoice(folder, "a.pdf", number="26502000000000000051")
    write_invoice(folder, "b.pdf", number="26502000000000000052")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        cancel = threading.Event()

        def progress(stage: str, done: int, total: int, name: str) -> None:
            if stage == "parse" and done >= 1:
                cancel.set()

        with pytest.raises(importer.ImportCancelled):
            import_folder(connection, folder, progress=progress, cancel=cancel)

        for table in ("batches", "files", "invoices"):
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, table
    finally:
        connection.close()


def test_cancel_stops_the_automatic_reparse(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.core.ocr.is_available", lambda: True)
    folder = tmp_path / "october"
    write_invoice(folder, "a.pdf")

    connection = db.connect(tmp_path / "invoices.db")
    try:
        import_folder(connection, folder)
        connection.execute(
            "UPDATE invoices SET invoice_type = 'scanned', invoice_number = ''"
        )
        connection.commit()

        cancel = threading.Event()
        cancel.set()
        summary = import_folder(connection, folder, cancel=cancel)

        assert summary.batch_id is None
        assert summary.cancelled is True
        assert summary.reparsed == 0
        row = connection.execute("SELECT invoice_type FROM invoices").fetchone()
        assert row["invoice_type"] == "scanned"
    finally:
        connection.close()
