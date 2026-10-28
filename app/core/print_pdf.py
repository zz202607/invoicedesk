"""Build a 2-up A4 print PDF from the invoice source files.

Each unique source page becomes one half of an A4 page, with a short label so
the stack can be checked against the Excel detail. Source PDFs are opened
read-only; nothing is written back to the invoice folders.

A source page can back several invoice records (multi-invoice pages, or a
conflict kept twice). It is printed once, and the label says how many records
share it.
"""

from __future__ import annotations

import contextlib
import math
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import fitz

from app.core.export import ExportError, ExportRow

# PyMuPDF raises RuntimeError/ValueError/OSError subclasses for corrupt files,
# missing pages, and write failures. Type errors from our own calls should
# still surface as bugs.
PDF_ERRORS = (RuntimeError, ValueError, OSError)

PRINT_FILENAME = "发票二合一.pdf"

A4_WIDTH = 595.276
A4_HEIGHT = 841.89
MARGIN = 16.0
GAP = 6.0
LABEL_HEIGHT = 20.0
LABEL_FONT_SIZE = 7.5
LABEL_FONT_NAME = "china-s"
LABEL_WIDTH = A4_WIDTH - 2 * MARGIN - 4
TEXT_COLOR = (0.25, 0.25, 0.25)
REVIEW_COLOR = (0.65, 0.0, 0.0)
SEPARATOR_COLOR = (0.75, 0.75, 0.75)


@dataclass(frozen=True)
class PrintInfo:
    pages: int
    unique_pages: int
    records: int
    duplicate_records: int
    skipped_missing: tuple[str, ...]
    skipped_invalid: tuple[str, ...]
    notice_only: bool = False


@dataclass(frozen=True)
class _Fonts:
    latin: fitz.Font
    cjk: fitz.Font | None


def _fonts() -> _Fonts:
    try:
        cjk = fitz.Font(LABEL_FONT_NAME)
    except (RuntimeError, ValueError):  # pragma: no cover - environment-specific
        cjk = None
    return _Fonts(latin=fitz.Font("helv"), cjk=cjk)


def _is_cjk(char: str) -> bool:
    return unicodedata.east_asian_width(char) in {"W", "F"}


def _split_runs(text: str) -> list[tuple[str, bool]]:
    """Split into (chunk, is_cjk) runs so Latin text keeps normal spacing.

    The built-in CJK font draws ASCII as full-width glyphs, which looks spaced
    out; Latin runs are drawn with Helvetica instead.
    """
    runs: list[tuple[str, bool]] = []
    for char in text:
        cjk = _is_cjk(char)
        if runs and runs[-1][1] == cjk:
            runs[-1] = (runs[-1][0] + char, cjk)
        else:
            runs.append((char, cjk))
    return runs


def _font_for(fonts: _Fonts, cjk: bool) -> fitz.Font:
    if cjk and fonts.cjk is not None:
        return fonts.cjk
    return fonts.latin


def _line_width(text: str, fonts: _Fonts, size: float = LABEL_FONT_SIZE) -> float:
    return sum(
        _font_for(fonts, cjk).text_length(run, fontsize=size)
        for run, cjk in _split_runs(text)
    )


def _fit(text: str, fonts: _Fonts, size: float = LABEL_FONT_SIZE) -> str:
    if _line_width(text, fonts, size) <= LABEL_WIDTH:
        return text
    trimmed = text
    while trimmed and _line_width(trimmed + "…", fonts, size) > LABEL_WIDTH:
        trimmed = trimmed[:-1]
    return trimmed + "…"


def _draw_line(
    page: fitz.Page,
    *,
    text: str,
    x: float,
    y: float,
    fonts: _Fonts,
    color: tuple[float, float, float],
    size: float = LABEL_FONT_SIZE,
) -> None:
    for run, cjk in _split_runs(text):
        if cjk and fonts.cjk is None:
            # Keep the ASCII parts of the label readable if this PyMuPDF build
            # lacks the built-in CJK font; helv cannot encode CJK glyphs.
            run = "?" * len(run)
        fontname = LABEL_FONT_NAME if cjk and fonts.cjk is not None else "helv"
        page.insert_text(
            (x, y),
            run,
            fontname=fontname,
            fontsize=size,
            color=color,
        )
        x += _font_for(fonts, cjk).text_length(run, fontsize=size)


def _money(cents: int | None) -> str:
    if cents is None:
        return "金额未识别"
    return f"¥{cents / 100:,.2f}"


def _reference(row: ExportRow) -> str:
    return f"{row.source_name} p{row.page_index + 1}"


def _draw_notice(
    page: fitz.Page,
    *,
    fonts: _Fonts,
    records: int,
    missing: int,
    invalid: int,
) -> None:
    """Replace a stale print PDF when this export has no printable page."""
    lines = (
        "本次没有可打印的票面",
        f"筛选结果共 {records} 条发票记录，但没有可读取的源文件。",
        f"缺少源文件 {missing} 条；无法打开或页号无效 {invalid} 条。",
        "请检查源发票是否被移动或删除，然后重新导出。",
        "本文件由 InvoiceDesk 覆盖生成，避免与旧导出的发票混用。",
    )
    x = MARGIN + 12
    _draw_line(
        page,
        text=lines[0],
        x=x,
        y=96,
        fonts=fonts,
        color=REVIEW_COLOR,
        size=16,
    )
    for offset, line in enumerate(lines[1:], start=1):
        _draw_line(
            page,
            text=line,
            x=x,
            y=96 + 28 * offset,
            fonts=fonts,
            color=TEXT_COLOR,
            size=10,
        )


def _slot_rects(slot: int) -> tuple[fitz.Rect, float]:
    slot_height = (A4_HEIGHT - 2 * MARGIN - GAP) / 2
    slot_y0 = MARGIN + slot * (slot_height + GAP)
    content = fitz.Rect(
        MARGIN,
        slot_y0 + LABEL_HEIGHT,
        A4_WIDTH - MARGIN,
        slot_y0 + slot_height,
    )
    return content, slot_y0


def _draw_label(
    page: fitz.Page,
    *,
    slot_y0: float,
    sequence: int,
    group: Sequence[ExportRow],
    fonts: _Fonts,
) -> None:
    row = group[0]
    review = any(item.needs_review for item in group)
    line1 = (
        f"{sequence}. {row.invoice_date or '日期未识别'} · "
        f"{row.invoice_number or '无票号'} · {_reference(row)}"
        f"/{row.page_count or '?'}"
    )
    line2 = f"{_money(row.amount_total_cents)} · {row.category or '未分类'} · "
    line2 += row.seller_name or "销售方未识别"
    if len(group) > 1:
        line2 += f" · 本页 {len(group)} 张"
    if review:
        line2 += " · 待确认"

    color = REVIEW_COLOR if review else TEXT_COLOR
    label_rect = fitz.Rect(MARGIN, slot_y0, A4_WIDTH - MARGIN, slot_y0 + LABEL_HEIGHT)
    # A separate label page keeps CJK text intact: PyMuPDF corrupts text
    # inserted directly after show_pdf_page on the same page. The label is
    # shown onto the output page only after the invoice rendered successfully.
    overlay = fitz.open()
    try:
        label_page = overlay.new_page(width=label_rect.width, height=label_rect.height)
        _draw_line(
            label_page, text=_fit(line1, fonts), x=2, y=9, fonts=fonts, color=color
        )
        _draw_line(
            label_page, text=_fit(line2, fonts), x=2, y=18, fonts=fonts, color=color
        )
        page.show_pdf_page(label_rect, overlay, 0, keep_proportion=False)
    finally:
        overlay.close()


def write_print_pdf(path: str | Path, rows: Sequence[ExportRow]) -> PrintInfo:
    """Write the 2-up PDF and report what could not be printed.

    Missing or unreadable sources are skipped and reported; the rest of the
    package still exports. When nothing can be printed, a notice page replaces
    the target file so a previous export cannot be mistaken for this one.
    Raises ``ExportError`` only when the output cannot be written.
    """
    groups: dict[tuple[str, int], list[ExportRow]] = {}
    for row in rows:
        groups.setdefault((row.source_path, row.page_index), []).append(row)

    output = fitz.open()
    fonts = _fonts()
    placed = 0
    records = 0
    duplicate_records = 0
    skipped_missing: list[str] = []
    skipped_invalid: list[str] = []

    try:
        for (source_path, page_index), group in groups.items():
            reference = _reference(group[0])
            if not Path(source_path).is_file():
                skipped_missing.append(reference)
                continue
            try:
                # Embedding keeps MuPDF's source document alive until the
                # output is written, so opening by path leaks one OS file
                # handle per source and fails past the process limit (~512 on
                # Windows). A memory stream has no OS handle to leak.
                source_bytes = Path(source_path).read_bytes()
                source = fitz.open(stream=source_bytes, filetype="pdf")
            except PDF_ERRORS:
                skipped_invalid.append(reference)
                continue
            try:
                if source.needs_pass or not 0 <= page_index < source.page_count:
                    skipped_invalid.append(reference)
                    continue

                created = False
                try:
                    if placed % 2 == 0:
                        page = output.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                        created = True
                    else:
                        page = output[-1]
                    slot = placed % 2
                    content, slot_y0 = _slot_rects(slot)
                    page.show_pdf_page(content, source, page_index, keep_proportion=True)
                    _draw_label(
                        page,
                        slot_y0=slot_y0,
                        sequence=placed + 1,
                        group=group,
                        fonts=fonts,
                    )
                    if slot == 0:
                        slot_height = (A4_HEIGHT - 2 * MARGIN - GAP) / 2
                        y = slot_y0 + slot_height + GAP / 2
                        page.draw_line(
                            (MARGIN, y),
                            (A4_WIDTH - MARGIN, y),
                            color=SEPARATOR_COLOR,
                            width=0.4,
                        )
                except PDF_ERRORS:
                    if created:
                        with contextlib.suppress(*PDF_ERRORS):
                            output.delete_page(output.page_count - 1)
                    skipped_invalid.append(reference)
                    continue
                placed += 1
                records += len(group)
                duplicate_records += len(group) - 1
            finally:
                source.close()

        notice_only = placed == 0
        target = Path(path)
        if target.is_symlink():
            raise ExportError("导出目标是符号链接，已拒绝写入。")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if notice_only:
                notice_page = output.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                _draw_notice(
                    notice_page,
                    fonts=fonts,
                    records=len(rows),
                    missing=len(skipped_missing),
                    invalid=len(skipped_invalid),
                )
            output.save(str(target), garbage=3, deflate=True)
        except PDF_ERRORS as exc:
            raise ExportError("无法写入打印 PDF，请确认文件未被占用且有写入权限。") from exc
    finally:
        output.close()

    return PrintInfo(
        pages=math.ceil(placed / 2),
        unique_pages=placed,
        records=records,
        duplicate_records=duplicate_records,
        skipped_missing=tuple(skipped_missing),
        skipped_invalid=tuple(skipped_invalid),
        notice_only=notice_only,
    )
