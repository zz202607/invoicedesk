"""Retry OCR on records imported before image-only PDFs were supported."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import db, ocr, reparse
from app.core.importer import CONFLICT_WARNING, sha256_file
from app.core.models import InvoiceType, ParsedInvoice
from tests.pdf_factory import full_digital_lines, make_pdf
from tests.test_parser_ocr import rasterize_pdf


def _insert(
    connection,
    source: Path,
    *,
    number: str = "",
    warnings: list[str] | None = None,
) -> int:
    batch_id = db.create_batch(
        connection, label="旧批次", source_dir=str(source.parent)
    )
    sha = sha256_file(source)
    db.insert_file(
        connection,
        sha256=sha,
        batch_id=batch_id,
        source_path=str(source.resolve()),
        page_count=1,
    )
    invoice = ParsedInvoice(
        source_path=str(source.resolve()),
        invoice_type=InvoiceType.SCANNED,
        invoice_number=number,
        warnings=warnings or ["无文本层，可能是扫描件/图片发票"],
    )
    invoice_id = db.insert_invoice(
        connection, batch_id=batch_id, sha256=sha, invoice=invoice
    )
    connection.commit()
    return invoice_id


@pytest.fixture
def scanned_pdf(tmp_path: Path) -> Path:
    text_pdf = make_pdf(tmp_path, full_digital_lines(), name="text.pdf")
    return rasterize_pdf(text_pdf, tmp_path / "scan.pdf")


@pytest.mark.skipif(not ocr.is_available(), reason="RapidOCR is not installed")
def test_reparse_fills_fields_from_ocr(tmp_path: Path, scanned_pdf: Path) -> None:
    connection = db.connect(tmp_path / "pool.db")
    try:
        invoice_id = _insert(connection, scanned_pdf)
        result = reparse.reparse_unrecognized(connection)
        assert result.reparsed == 1
        row = connection.execute(
            "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
        ).fetchone()
        assert row["invoice_type"] == "full_digital_normal"
        assert row["invoice_number"] == "26502000000000000001"
        assert row["seller_name"] == "示例乙酒店"
        assert row["amount_total_cents"] == 106000
        assert row["needs_review"] == 1
        warnings = json.loads(row["warnings"])
        assert any("OCR" in warning for warning in warnings)
        assert not any(warning.startswith("无文本层") for warning in warnings)
    finally:
        connection.close()


@pytest.mark.skipif(not ocr.is_available(), reason="RapidOCR is not installed")
def test_reparse_skips_manual_edits(tmp_path: Path, scanned_pdf: Path) -> None:
    connection = db.connect(tmp_path / "pool.db")
    try:
        invoice_id = _insert(connection, scanned_pdf)
        connection.execute(
            "INSERT INTO invoice_edits (invoice_id, field, old_value, new_value,"
            " edited_at) VALUES (?, 'seller_name', '', '人工销售方', 'now')",
            (invoice_id,),
        )
        connection.commit()
        result = reparse.reparse_unrecognized(connection)
        assert result.skipped_edited == 1
        row = connection.execute(
            "SELECT invoice_type FROM invoices WHERE id = ?", (invoice_id,)
        ).fetchone()
        assert row["invoice_type"] == "scanned"
    finally:
        connection.close()


@pytest.mark.skipif(not ocr.is_available(), reason="RapidOCR is not installed")
def test_review_only_edits_do_not_block_reparse(
    tmp_path: Path, scanned_pdf: Path
) -> None:
    """“确认无误” is an audit row, not a field correction."""
    connection = db.connect(tmp_path / "pool.db")
    try:
        invoice_id = _insert(connection, scanned_pdf)
        connection.execute(
            "INSERT INTO invoice_edits (invoice_id, field, old_value, new_value,"
            " edited_at) VALUES (?, 'review', '待确认', '已确认', 'now')",
            (invoice_id,),
        )
        connection.commit()
        result = reparse.reparse_unrecognized(connection)
        assert result.skipped_edited == 0
        assert result.reparsed == 1
        row = connection.execute(
            "SELECT invoice_type, invoice_number FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        assert row["invoice_type"] == "full_digital_normal"
        assert row["invoice_number"] == "26502000000000000001"
    finally:
        connection.close()


def test_reparse_skips_missing_sources(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "pool.db")
    try:
        missing = tmp_path / "gone.pdf"
        missing.write_bytes(b"%PDF-1.4 fake")
        _insert(connection, missing)
        missing.unlink()
        result = reparse.reparse_unrecognized(connection)
        assert result.skipped_missing == 1
    finally:
        connection.close()


@pytest.mark.skipif(not ocr.is_available(), reason="RapidOCR is not installed")
def test_reparse_marks_a_number_conflict_without_deleting(
    tmp_path: Path, scanned_pdf: Path
) -> None:
    other_pdf = make_pdf(
        tmp_path,
        full_digital_lines(seller="示例丙酒店"),
        name="other.pdf",
    )
    connection = db.connect(tmp_path / "pool.db")
    try:
        invoice_id = _insert(connection, scanned_pdf)
        other_id = _insert(connection, other_pdf, number="26502000000000000001")
        result = reparse.reparse_unrecognized(connection, invoice_ids=[invoice_id])
        assert result.reparsed == 1
        assert result.conflicts == 1

        for target in (invoice_id, other_id):
            row = connection.execute(
                "SELECT warnings FROM invoices WHERE id = ?", (target,)
            ).fetchone()
            assert CONFLICT_WARNING in json.loads(row["warnings"])
    finally:
        connection.close()
