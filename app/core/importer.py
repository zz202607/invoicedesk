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
import threading
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import fitz

from app.core import db, learning
from app.core.appconfig import EXPORT_SUBDIR, LEGACY_DATA_SUBDIR
from app.core.parser import parse_pdf
from app.core.text import normalize

SKIP_DIRS = {
    "分类合并结果",
    "__pycache__",
    ".git",
    "out",
    EXPORT_SUBDIR,
    LEGACY_DATA_SUBDIR,
}
HASH_CHUNK_SIZE = 1024 * 1024
CONFLICT_WARNING = "票号相同但开票日期/金额/销售方不一致,需人工核对"
INCOMPLETE_WARNING = "关键字段缺失,无法确认是否重复,需人工核对"
# A page with less extractable text than this is likely a scan or screenshot;
# the parser applies the same threshold before trying OCR.
OCR_ESTIMATE_TEXT = 20

# progress(stage, done, total, current_name); "parse" or "reparse".
ProgressFn = Callable[[str, int, int, str], None]


class ImportCancelled(RuntimeError):
    """The user cancelled an import before its batch was committed."""


@dataclass(frozen=True)
class ImportPlan:
    """What an import would do, computed before any row is written.

    The plan carries the file digests so the import itself does not rehash,
    and the counts feed the pre-import confirmation in the UI.
    """

    source: Path
    label: str
    source_dir: str
    files_seen: int
    candidates: tuple[tuple[Path, str], ...]
    duplicate_files: int
    duplicate_hashes: frozenset[str]
    ocr_pages: int
    reparse_candidates: int

    @property
    def new_files(self) -> int:
        return len(self.candidates)


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
    # Duplicate files are skipped by hash, but an old record may predate OCR.
    # These counters report the automatic retry on this import.
    reparse_candidates: int = 0
    reparsed: int = 0
    reparse_failed: int = 0
    # The import itself committed, but the follow-up OCR retry was stopped
    # early. A cancel before the batch commits raises ImportCancelled instead.
    cancelled: bool = False

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


def count_ocr_pages(candidates: tuple[tuple[Path, str], ...]) -> int:
    """Estimate how many pages will need OCR, without running OCR.

    Opening each PDF and reading its text layer is cheap compared with
    recognition; it lets the preflight tell the user what the import costs
    before any row is written.
    """
    pages = 0
    for item, _digest in candidates:
        try:
            with fitz.open(str(item)) as document:
                if document.needs_pass:
                    continue
                for page in document:
                    try:
                        text = page.get_text("text")
                    except Exception:  # noqa: BLE001 - one bad page is not fatal
                        text = ""
                    if len(text.strip()) < OCR_ESTIMATE_TEXT:
                        pages += 1
        # Unreadable files become error rows at import time; the estimate
        # simply skips them instead of failing the preflight.
        except Exception:  # noqa: BLE001, S110
            pass
    return pages


def _unrecognized_duplicate_ids(
    connection: sqlite3.Connection, hashes: set[str] | frozenset[str]
) -> list[int]:
    """Old unrecognized rows whose file is seen again in this import."""
    if not hashes:
        return []
    values = tuple(hashes)
    return [
        row[0]
        for row in connection.execute(
            "SELECT id FROM invoices"
            f" WHERE file_sha256 IN ({', '.join('?' for _ in values)})"
            " AND invoice_type IN ('scanned', 'unknown', 'error')"
            " AND archived_at = ''",
            values,
        )
    ]


def _retry_unrecognized_duplicates(
    connection: sqlite3.Connection,
    duplicate_hashes: set[str] | frozenset[str],
    *,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
):
    """OCR old ``scanned`` records for files seen again in this import.

    Files are deduplicated by content hash, so re-importing a folder cannot
    upgrade rows created by a build that had no OCR. Retrying here makes the
    recovery automatic instead of hiding it behind a separate button.
    """
    if not duplicate_hashes:
        return None
    from app.core import ocr  # local import: OCR is an optional, heavy dependency

    if not ocr.is_available():
        return None
    ids = _unrecognized_duplicate_ids(connection, duplicate_hashes)
    if not ids:
        return None
    from app.core import reparse  # local import avoids a module cycle

    return reparse.reparse_unrecognized(
        connection, invoice_ids=ids, progress=progress, cancel=cancel
    )


def _ocr_available() -> bool:
    try:
        from app.core import ocr
    except Exception:  # noqa: BLE001 - optional dependency
        return False
    return ocr.is_available()


def plan_import(
    connection: sqlite3.Connection,
    path: str | Path,
    *,
    label: str | None = None,
    limit: int | None = None,
    estimate_ocr: bool = True,
) -> ImportPlan:
    """Scan a folder and hash every PDF without writing anything.

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
    duplicate_hashes: set[str] = set()
    duplicates = 0
    for item in files:
        digest = sha256_file(item)
        if digest in known_hashes or digest in seen_hashes:
            duplicates += 1
            if digest in known_hashes:
                duplicate_hashes.add(digest)
            continue
        seen_hashes.add(digest)
        candidates.append((item, digest))

    batch_label = label or (source.stem if source.is_file() else source.name)
    source_dir = str(source.parent.resolve() if source.is_file() else source.resolve())
    ocr_pages = count_ocr_pages(tuple(candidates)) if estimate_ocr else 0
    # Loading the OCR module is slow, so only ask when a retry is possible.
    reparse_candidates = (
        len(_unrecognized_duplicate_ids(connection, duplicate_hashes))
        if duplicate_hashes and _ocr_available()
        else 0
    )
    return ImportPlan(
        source=source,
        label=batch_label,
        source_dir=source_dir,
        files_seen=len(files),
        candidates=tuple(candidates),
        duplicate_files=duplicates,
        duplicate_hashes=frozenset(duplicate_hashes),
        ocr_pages=ocr_pages,
        reparse_candidates=reparse_candidates,
    )


def execute_plan(
    connection: sqlite3.Connection,
    plan: ImportPlan,
    *,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> ImportSummary:
    """Run a plan in one transaction; cancelling before the commit rolls back.

    ``cancel`` is checked before every file, so cancelling during the parse
    phase leaves the database exactly as it was. The OCR retry for old
    duplicates runs after the commit; cancelling there stops the retry and
    reports ``cancelled`` instead of undoing a finished import.
    """

    def notify(stage: str, done: int, total: int, name: str = "") -> None:
        if progress is not None:
            progress(stage, done, total, name)

    def check_cancel() -> None:
        if cancel is not None and cancel.is_set():
            raise ImportCancelled("导入已取消")

    candidates = plan.candidates
    if not candidates:
        retry = _retry_unrecognized_duplicates(
            connection,
            plan.duplicate_hashes,
            progress=progress,
            cancel=cancel,
        )
        return ImportSummary(
            batch_id=None,
            label=plan.label,
            source_dir=plan.source_dir,
            files_seen=plan.files_seen,
            files_imported=0,
            files_duplicate=plan.duplicate_files,
            invoices_imported=0,
            invoices_duplicate=0,
            invoices_conflict=0,
            needs_review=0,
            amount_total=Decimal("0.00"),
            reparse_candidates=retry.candidates if retry else 0,
            reparsed=retry.reparsed if retry else 0,
            reparse_failed=retry.failed if retry else 0,
            cancelled=bool(retry and retry.cancelled),
        )

    check_cancel()
    invoice_index = db.invoice_index(connection)
    learned_rules = learning.seller_rules(connection)
    invoices_imported = 0
    invoices_duplicate = 0
    invoices_conflict = 0
    needs_review = 0
    amount_total = Decimal("0.00")
    total_files = len(candidates)

    with connection:
        inserted_at = db.utc_now()
        batch_id = db.create_batch(
            connection,
            label=plan.label,
            source_dir=plan.source_dir,
            created_at=inserted_at,
        )
        for index, (item, digest) in enumerate(candidates):
            # Report before parsing so the progress bar shows the file the
            # user is waiting on; a cancel set by that reporting aborts here.
            notify("parse", index, total_files, item.name)
            check_cancel()
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
        # A cancel arriving during the final file must still roll the batch back.
        check_cancel()

    notify("parse", total_files, total_files)
    cancelled = cancel is not None and cancel.is_set()
    retry = None
    if not cancelled:
        retry = _retry_unrecognized_duplicates(
            connection,
            plan.duplicate_hashes,
            progress=progress,
            cancel=cancel,
        )
    return ImportSummary(
        batch_id=batch_id,
        label=plan.label,
        source_dir=plan.source_dir,
        files_seen=plan.files_seen,
        files_imported=len(candidates),
        files_duplicate=plan.duplicate_files,
        invoices_imported=invoices_imported,
        invoices_duplicate=invoices_duplicate,
        invoices_conflict=invoices_conflict,
        needs_review=needs_review,
        amount_total=amount_total,
        reparse_candidates=retry.candidates if retry else 0,
        reparsed=retry.reparsed if retry else 0,
        reparse_failed=retry.failed if retry else 0,
        cancelled=cancelled or bool(retry and retry.cancelled),
    )


def import_folder(
    connection: sqlite3.Connection,
    path: str | Path,
    *,
    label: str | None = None,
    limit: int | None = None,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> ImportSummary:
    """Plan and import in one call; kept for the CLI and existing callers."""
    plan = plan_import(connection, path, label=label, limit=limit, estimate_ocr=False)
    return execute_plan(connection, plan, progress=progress, cancel=cancel)
