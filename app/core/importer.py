"""Import parsed invoices into the local SQLite pool.

Dedup happens at two levels:

1. File hash: a PDF already imported is skipped without being parsed.
2. Invoice number: the same invoice arriving in a differently named PDF is
   skipped. A number is only treated as the same invoice when date, total, and
   seller all exist and match; otherwise both records are kept and flagged for
   review, because a mismatch can mean a misparse or a multi-invoice page.

Records without an invoice number (scanned/unknown/error pages) cannot be
deduplicated by number; they are imported and marked for review instead of
being silently dropped.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from app.core import db, learning
from app.core.parser import parse_pdf
from app.core.text import normalize

SKIP_DIRS = {"分类合并结果", "__pycache__", ".git", "out"}
HASH_CHUNK_SIZE = 1024 * 1024
CONFLICT_WARNING = "票号相同但开票日期/金额/销售方不一致,需人工核对"
INCOMPLETE_WARNING = "关键字段缺失,无法确认是否重复,需人工核对"


@dataclass(frozen=True)
class ImportSummary:
    batch_id: int | None
    label: str
    source_dir: str
    files_seen: int
    files_imported: int
    files_duplicate: int
    invoices_imported: int
    invoices_duplicate: int
    invoices_conflict: int
    needs_review: int
    amount_total: Decimal

    @property
    def has_new_data(self) -> bool:
        return self.batch_id is not None


def scan_pdfs(path: Path) -> list[Path]:
    """List PDFs under a file or folder, skipping known output directories."""
    if path.is_file():
        return [path] if path.suffix.lower() == ".pdf" else []
    files: list[Path] = []
    for item in path.rglob("*"):
        if not item.is_file() or item.suffix.lower() != ".pdf":
            continue
        if any(part in SKIP_DIRS for part in item.relative_to(path).parts):
            continue
        files.append(item)
    return sorted(files, key=lambda item: str(item).lower())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def import_folder(
    connection: sqlite3.Connection,
    path: str | Path,
    *,
    label: str | None = None,
    limit: int | None = None,
) -> ImportSummary:
    """Parse new PDFs under ``path`` and store them as one batch.

    ``limit`` applies to the first N PDFs found, before dedup, matching the
    ``parse`` command's ``--limit``. A duplicate-heavy prefix can therefore
    import nothing; that is reported instead of scanning further files.
    """
    source = Path(path)
    files = scan_pdfs(source)
    if limit:
        files = files[:limit]

    known_hashes = {row[0] for row in connection.execute("SELECT sha256 FROM files")}
    candidates: list[tuple[Path, str]] = []
    seen_hashes: set[str] = set()
    duplicates = 0
    for item in files:
        digest = sha256_file(item)
        if digest in known_hashes or digest in seen_hashes:
            duplicates += 1
            continue
        seen_hashes.add(digest)
        candidates.append((item, digest))

    batch_label = label or (source.stem if source.is_file() else source.name)
    source_dir = str(source.parent.resolve() if source.is_file() else source.resolve())

    if not candidates:
        return ImportSummary(
            batch_id=None,
            label=batch_label,
            source_dir=source_dir,
            files_seen=len(files),
            files_imported=0,
            files_duplicate=duplicates,
            invoices_imported=0,
            invoices_duplicate=0,
            invoices_conflict=0,
            needs_review=0,
            amount_total=Decimal("0.00"),
        )

    invoice_index = db.invoice_index(connection)
    learned_rules = learning.seller_rules(connection)
    invoices_imported = 0
    invoices_duplicate = 0
    invoices_conflict = 0
    needs_review = 0
    amount_total = Decimal("0.00")

    with connection:
        inserted_at = db.utc_now()
        batch_id = db.create_batch(
            connection,
            label=batch_label,
            source_dir=source_dir,
            created_at=inserted_at,
        )
        for item, digest in candidates:
            resolved = str(item.resolve())
            invoices = parse_pdf(resolved)
            page_count = invoices[0].page_count if invoices else 0
            db.insert_file(
                connection,
                sha256=digest,
                batch_id=batch_id,
                source_path=resolved,
                page_count=page_count,
                imported_at=inserted_at,
            )
            record_count = 0
            duplicate_count = 0
            conflict_count = 0
            for invoice in invoices:
                # A user correction outranks the built-in keyword rules; the
                # rule only counts as a hit when a new record is stored.
                rule_key = learning.apply_seller_rules(invoice, learned_rules)
                number = invoice.invoice_number
                identity = (
                    invoice.invoice_date,
                    db.money_to_cents(invoice.amount_total),
                    normalize(invoice.seller_name),
                )
                # A partial identity (missing date/total/seller) cannot prove
                # duplication; matching on it could silently drop a real invoice.
                identity_complete = (
                    bool(invoice.invoice_date)
                    and invoice.amount_total is not None
                    and bool(normalize(invoice.seller_name))
                )
                extra_warnings: tuple[str, ...] = ()
                if number:
                    existing = invoice_index.get(number, [])
                    if identity_complete and any(
                        existing_identity == identity for _, existing_identity in existing
                    ):
                        duplicate_count += 1
                        continue
                    if existing:
                        # Same number but different date/total/seller: keep both
                        # and make the conflict explicit instead of guessing.
                        extra_warnings = (CONFLICT_WARNING,)
                        for existing_id, _ in existing:
                            db.append_warning(connection, existing_id, CONFLICT_WARNING)
                        conflict_count += 1
                    elif not identity_complete:
                        extra_warnings = (INCOMPLETE_WARNING,)
                new_id = db.insert_invoice(
                    connection,
                    batch_id=batch_id,
                    sha256=digest,
                    invoice=invoice,
                    extra_warnings=extra_warnings,
                    imported_at=inserted_at,
                )
                if rule_key:
                    learning.record_hit(connection, rule_key)
                if number:
                    invoice_index.setdefault(number, []).append((new_id, identity))
                record_count += 1
                invoices_imported += 1
                if invoice.needs_review or extra_warnings:
                    needs_review += 1
                if invoice.amount_total is not None:
                    amount_total += invoice.amount_total
            invoices_duplicate += duplicate_count
            invoices_conflict += conflict_count
            db.finish_file(
                connection,
                digest,
                record_count=record_count,
                duplicate_count=duplicate_count,
                conflict_count=conflict_count,
            )

    return ImportSummary(
        batch_id=batch_id,
        label=batch_label,
        source_dir=source_dir,
        files_seen=len(files),
        files_imported=len(candidates),
        files_duplicate=duplicates,
        invoices_imported=invoices_imported,
        invoices_duplicate=invoices_duplicate,
        invoices_conflict=invoices_conflict,
        needs_review=needs_review,
        amount_total=amount_total,
    )
