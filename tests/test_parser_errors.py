from __future__ import annotations

from pathlib import Path

import fitz

from app.cli import parse_folder
from app.core.models import InvoiceType
from app.core.parser import parse_pdf
from tests.pdf_factory import full_digital_lines, make_pdf


def test_broken_pdf_returns_error_record(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a real pdf")
    invoice = parse_pdf(broken)[0]
    assert invoice.invoice_type is InvoiceType.ERROR
    assert invoice.needs_review is True


def test_zero_byte_pdf_returns_error_record(tmp_path: Path) -> None:
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    invoice = parse_pdf(empty)[0]
    assert invoice.invoice_type is InvoiceType.ERROR
    assert invoice.needs_review is True


def test_encrypted_pdf_returns_error_record(tmp_path: Path) -> None:
    document = fitz.open()
    document.new_page(width=300, height=200)
    encrypted = tmp_path / "encrypted.pdf"
    document.save(
        encrypted,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner",
        user_pw="user",
    )
    document.close()

    invoice = parse_pdf(encrypted)[0]
    assert invoice.invoice_type is InvoiceType.ERROR
    assert invoice.needs_review is True


def test_parse_folder_continues_after_bad_pdf(tmp_path: Path) -> None:
    good = make_pdf(tmp_path, full_digital_lines(), name="good.pdf")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"broken")

    records = parse_folder(tmp_path)
    assert len(records) == 2
    assert {record.invoice_type for record in records} == {
        InvoiceType.ERROR,
        InvoiceType.FULL_DIGITAL_NORMAL,
    }
    assert good.exists()
