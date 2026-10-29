"""Merge invoice source pages into one clean PDF per company and category.

Finance asks for one PDF per buyer company and, inside it, one PDF per
category. Each category file is named ``<category>-<amount>元.pdf`` so the
amount is visible without opening it, and each company also gets one
``总合并-<amount>元.pdf`` that concatenates the category files in taxonomy
order. Each output sheet is A4 with two invoice pages stacked top and bottom,
scaled to fit their half. No labels, no sequence numbers, no invented cover
page — the 2-up audit PDF in :mod:`app.core.print_pdf` keeps its labels; this
module is for submission bundles. The thin separator line between the two
halves is a cutting guide, not a label.

Source PDFs are opened read-only. A page shared by several rows in the same
group is written once; the same page used by rows in different groups is
written once per group, because each group is a stand-alone submission file.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import fitz

from app.core.categories import CATEGORIES
from app.core.export import ExportError, ExportRow
from app.core.text import normalize

GROUP_DIRNAME = "发票分组"
MANIFEST_NAME = ".manifest.json"
UNKNOWN_COMPANY = "未识别购买方"
UNKNOWN_CATEGORY = "未分类"

PDF_ERRORS = (RuntimeError, ValueError, OSError)
MAX_NAME_CHARS = 60
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_CATEGORY_ORDER = {name: index for index, name in enumerate(CATEGORIES)}

# A4 in points. Two source pages share one sheet, top half and bottom half.
A4_WIDTH = 595.276
A4_HEIGHT = 841.89
MARGIN = 16.0
GAP = 6.0
SEPARATOR_COLOR = (0.75, 0.75, 0.75)


@dataclass(frozen=True)
class GroupPdfFile:
    company: str
    category: str
    count: int
    total_cents: int
    pages: int
    source_pages: int
    path: str


@dataclass(frozen=True)
class CompanyPdfFile:
    """One company's merged PDF: every category file, in taxonomy order."""

    company: str
    count: int
    total_cents: int
    pages: int
    source_pages: int
    path: str


@dataclass(frozen=True)
class GroupPdfResult:
    directory: str
    groups: tuple[GroupPdfFile, ...]
    merged: tuple[CompanyPdfFile, ...]
    skipped_missing: tuple[str, ...]
    skipped_invalid: tuple[str, ...]


def _safe_name(value: str) -> str:
    """Return a Windows-safe file or folder name with its meaning kept."""
    name = unicodedata.normalize("NFKC", value)
    name = _INVALID_FILENAME.sub("_", name)
    name = " ".join(name.split()).strip(" .")
    if not name:
        name = "_"
    if len(name) > MAX_NAME_CHARS:
        name = name[:MAX_NAME_CHARS].rstrip(" .")
    if name.split(".")[0].casefold() in _RESERVED_NAMES:
        name = f"_{name}"
    return name or "_"


def _resolve_fallback_company(rows: Sequence[ExportRow], hint: str) -> str:
    """Match the report's short company name to a full buyer name when unique.

    A report may store a short name (“甲公司”) while the invoice says the full
    name (“示例甲有限公司”). An unassigned invoice (a train ticket has no buyer)
    should still land in that company's folder, but only when the match is
    unambiguous.
    """
    hint = normalize(hint)
    buyers = {
        normalize(row.buyer_name)
        for row in rows
        if normalize(row.buyer_name)
    }
    if not hint:
        # A single-company export has an obvious home for buyer-less tickets.
        return next(iter(buyers)) if len(buyers) == 1 else ""
    if hint in buyers:
        return hint
    matches = {buyer for buyer in buyers if hint in buyer or buyer in hint}
    # Two matches mean the short report name (“示例”) cannot identify a company;
    # keeping the unassigned invoices in a separate folder is safer than
    # guessing.
    return matches.pop() if len(matches) == 1 else ""


def _company_of(row: ExportRow, fallback: str) -> str:
    return normalize(row.buyer_name) or fallback or UNKNOWN_COMPANY


def _category_key(category: str) -> tuple[int, int | str]:
    if category in _CATEGORY_ORDER:
        return (0, _CATEGORY_ORDER[category])
    if category == UNKNOWN_CATEGORY:
        return (2, 0)
    return (1, category.casefold())


def _reference(row: ExportRow) -> str:
    return f"{row.source_name} p{row.page_index + 1}"


def _slot_rect(slot: int) -> fitz.Rect:
    slot_height = (A4_HEIGHT - 2 * MARGIN - GAP) / 2
    y0 = MARGIN + slot * (slot_height + GAP)
    return fitz.Rect(MARGIN, y0, A4_WIDTH - MARGIN, y0 + slot_height)


def _draw_separator(page: fitz.Page) -> None:
    slot_height = (A4_HEIGHT - 2 * MARGIN - GAP) / 2
    y = MARGIN + slot_height + GAP / 2
    page.draw_line(
        (MARGIN, y),
        (A4_WIDTH - MARGIN, y),
        color=SEPARATOR_COLOR,
        width=0.4,
    )


def _manifest_paths(directory: Path) -> list[str]:
    try:
        payload = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    files = payload.get("files") if isinstance(payload, dict) else None
    return [item for item in files if isinstance(item, str)] if isinstance(files, list) else []


def _inside(directory: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(directory.resolve())
    except (OSError, ValueError):
        return False
    return True


def _prune_previous(directory: Path) -> None:
    """Delete only PDFs this module wrote last time, then empty folders.

    The output folder can contain files the user dropped there; the manifest
    keeps the cleanup from touching them.
    """
    for relative in _manifest_paths(directory):
        candidate = directory / relative
        if not relative.lower().endswith(".pdf"):
            continue
        if not _inside(directory, candidate):
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        with contextlib.suppress(OSError):
            candidate.unlink()
    for folder in sorted(
        (item for item in directory.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        with contextlib.suppress(OSError):
            folder.rmdir()


def _write_manifest(directory: Path, files: Iterable[str]) -> None:
    temp = directory / f".{MANIFEST_NAME}.{uuid4().hex[:8]}.tmp"
    payload = {"version": 1, "files": sorted(files)}
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(temp, directory / MANIFEST_NAME)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


def _yuan(cents: int) -> str:
    return f"{cents / 100:.2f}"


def _category_stem(category: str, total_cents: int) -> str:
    return f"{category}-{_yuan(total_cents)}元"


def _merge_stem(total_cents: int) -> str:
    return f"总合并-{_yuan(total_cents)}元"


def _unique_file(
    company_dir: Path, stem: str, used: set[str]
) -> tuple[Path, str]:
    base = _safe_name(stem)
    suffix = 2
    while True:
        name = f"{base}.pdf" if suffix == 2 else f"{base} ({suffix}).pdf"
        relative = f"{company_dir.name}/{name}"
        key = relative.casefold()
        if key not in used:
            used.add(key)
            return company_dir / name, relative
        suffix += 1


def _merge_group(
    target: Path,
    records: Sequence[ExportRow],
    *,
    skipped_missing: list[str],
    skipped_invalid: list[str],
) -> tuple[int, int]:
    """Write one group PDF; return (source pages, A4 sheets) written."""
    pages: dict[tuple[str, int], ExportRow] = {}
    for row in records:
        pages.setdefault((row.source_path, row.page_index), row)

    output = fitz.open()
    documents: dict[str, fitz.Document | None] = {}
    sheet: fitz.Page | None = None
    placed = 0
    try:
        for (source_path, page_index), row in pages.items():
            reference = _reference(row)
            if not Path(source_path).is_file():
                skipped_missing.append(reference)
                continue
            if source_path not in documents:
                try:
                    source_bytes = Path(source_path).read_bytes()
                    documents[source_path] = fitz.open(
                        stream=source_bytes, filetype="pdf"
                    )
                except PDF_ERRORS:
                    documents[source_path] = None
            source = documents[source_path]
            if source is None or source.needs_pass or not 0 <= page_index < source.page_count:
                skipped_invalid.append(reference)
                continue
            created = False
            try:
                slot = placed % 2
                if slot == 0:
                    sheet = output.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                    created = True
                if sheet is None:
                    skipped_invalid.append(reference)
                    continue
                sheet.show_pdf_page(
                    _slot_rect(slot), source, page_index, keep_proportion=True
                )
                if slot == 0:
                    _draw_separator(sheet)
            except PDF_ERRORS:
                if created and output.page_count:
                    with contextlib.suppress(*PDF_ERRORS):
                        output.delete_page(output.page_count - 1)
                    sheet = None
                skipped_invalid.append(reference)
                continue
            placed += 1

        if not placed:
            # An empty group must not leave a zero-page PDF behind.
            return 0, 0
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.parent / f".{target.name}.{uuid4().hex[:8]}.tmp"
        try:
            output.save(str(temp), garbage=3, deflate=True)
            os.replace(temp, target)
        except PDF_ERRORS as exc:
            raise ExportError(
                "无法写入分类 PDF，请确认输出目录可写且文件未被占用。"
            ) from exc
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)
    finally:
        for document in documents.values():
            if document is not None:
                with contextlib.suppress(*PDF_ERRORS):
                    document.close()
        output.close()
    return placed, (placed + 1) // 2


def _merge_pdfs(target: Path, sources: Sequence[Path]) -> int:
    """Concatenate already-written category PDFs in the given order."""
    output = fitz.open()
    pages = 0
    try:
        for source_path in sources:
            try:
                with fitz.open(str(source_path)) as source:
                    output.insert_pdf(source)
            except PDF_ERRORS as exc:
                raise ExportError(
                    "无法合并分类 PDF，请确认输出目录可读。"
                ) from exc
        pages = output.page_count
        if not pages:
            return 0
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.parent / f".{target.name}.{uuid4().hex[:8]}.tmp"
        try:
            output.save(str(temp), garbage=3, deflate=True)
            os.replace(temp, target)
        except PDF_ERRORS as exc:
            raise ExportError(
                "无法写入总合并 PDF，请确认输出目录可写且文件未被占用。"
            ) from exc
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)
    finally:
        output.close()
    return pages


def write_group_pdfs(
    directory: str | Path,
    rows: Sequence[ExportRow],
    *,
    fallback_company: str = "",
) -> GroupPdfResult:
    """Write one label-free PDF per (company, category) plus one merged PDF.

    Category files are named ``<category>-<amount>元.pdf``. Each company's
    ``总合并-<amount>元.pdf`` concatenates every written category file in
    taxonomy order, so cutting apart the 2-up sheets is not needed to hand the
    bundle over as one document.

    Previous outputs listed in ``.manifest.json`` are removed first, so a
    re-export never leaves a stale company or category PDF behind. Groups that
    end up with no readable source page are not written at all.
    """
    target_dir = Path(directory)
    if target_dir.exists() and target_dir.is_symlink():
        raise ExportError("分类 PDF 目录是符号链接，已拒绝写入。")
    target_dir.mkdir(parents=True, exist_ok=True)
    _prune_previous(target_dir)

    fallback = _resolve_fallback_company(rows, fallback_company)
    grouped: dict[tuple[str, str], list[ExportRow]] = {}
    for row in rows:
        company = _company_of(row, fallback)
        category = normalize(row.category) or UNKNOWN_CATEGORY
        grouped.setdefault((company, category), []).append(row)

    ordered = sorted(
        grouped.items(),
        key=lambda item: (item[0][0].casefold(), _category_key(item[0][1])),
    )
    company_dirs: dict[str, Path] = {}
    used_dirs: set[str] = set()
    # Names only need to be unique inside one company folder: two companies can
    # both have ``差旅费-100.00元.pdf`` without one getting a spurious suffix.
    used_names: dict[str, set[str]] = {}
    written: list[GroupPdfFile] = []
    merged: list[CompanyPdfFile] = []
    company_files: dict[str, list[Path]] = {}
    company_records: dict[str, list[ExportRow]] = {}
    manifest: list[str] = []
    skipped_missing: list[str] = []
    skipped_invalid: list[str] = []

    for (company, category), records in ordered:
        company_dir = company_dirs.get(company)
        if company_dir is None:
            base = _safe_name(company)
            folder = base
            suffix = 2
            while folder.casefold() in used_dirs:
                folder = f"{base} ({suffix})"
                suffix += 1
            used_dirs.add(folder.casefold())
            company_dir = target_dir / folder
            company_dirs[company] = company_dir
            company_files[company] = []
            company_records[company] = []
        total_cents = sum(row.amount_total_cents or 0 for row in records)
        path, relative = _unique_file(
            company_dir,
            _category_stem(category, total_cents),
            used_names.setdefault(company, set()),
        )
        source_pages, sheets = _merge_group(
            path,
            records,
            skipped_missing=skipped_missing,
            skipped_invalid=skipped_invalid,
        )
        if not source_pages:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            continue
        manifest.append(relative)
        company_files[company].append(path)
        company_records[company].extend(records)
        written.append(
            GroupPdfFile(
                company=company,
                category=category,
                count=len(records),
                total_cents=total_cents,
                pages=sheets,
                source_pages=source_pages,
                path=str(path),
            )
        )

    for company, sources in company_files.items():
        if not sources:
            continue
        records = company_records[company]
        total_cents = sum(row.amount_total_cents or 0 for row in records)
        path, relative = _unique_file(
            company_dirs[company],
            _merge_stem(total_cents),
            used_names[company],
        )
        pages = _merge_pdfs(path, sources)
        if not pages:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            continue
        manifest.append(relative)
        merged.append(
            CompanyPdfFile(
                company=company,
                count=len(records),
                total_cents=total_cents,
                pages=pages,
                source_pages=sum(
                    group.source_pages
                    for group in written
                    if group.company == company
                ),
                path=str(path),
            )
        )

    _write_manifest(target_dir, manifest)
    return GroupPdfResult(
        directory=str(target_dir),
        groups=tuple(written),
        merged=tuple(merged),
        skipped_missing=tuple(skipped_missing),
        skipped_invalid=tuple(skipped_invalid),
    )
