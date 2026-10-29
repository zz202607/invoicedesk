"""Render a reimbursement report as the company's Excel detail sheet.

The layout follows the real 《费用报销支出明细表》 files: a merged title, a
department/reporter row, a red-ink-style total, then one row per expense line.
Column sets have drifted between companies and months, so three presets are
kept: ``project`` (newest 示例乙/示例甲 sheets), ``company`` (July 示例甲, which shows
the company column instead of a project), and ``plain`` (sheets without either).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.core.amounts import chinese_upper_cents
from app.core.reports import Report, ReportError, ReportLine

MONEY_FORMAT = "#,##0.00"
MAX_CELL_CHARS = 500
_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class ReportLayout:
    key: str
    headers: tuple[str, ...]
    widths: tuple[float, ...]
    project_column: str | None  # "project", "company", or None
    merge_columns: int | None = None  # None: span every column


LAYOUTS: dict[str, ReportLayout] = {
    "project": ReportLayout(
        key="project",
        headers=(
            "序号",
            "日期",
            "费用名称",
            "金额",
            "报销人",
            "是否持有发票",
            "项目名称",
            "备注（报销费用说明）",
        ),
        widths=(10.09, 12.36, 14.0, 10.45, 11.27, 16.45, 18.0, 42.0),
        project_column="project",
    ),
    "company": ReportLayout(
        key="company",
        headers=(
            "序号",
            "日期",
            "费用名称",
            "金额",
            "报销人",
            "是否持有发票",
            "备注（报销费用说明）",
            "公司",
        ),
        widths=(10.09, 12.36, 14.0, 10.45, 9.18, 18.63, 38.45, 13.0),
        project_column="company",
        # The July source sheet keeps the 公司 column outside the title block.
        merge_columns=7,
    ),
    "plain": ReportLayout(
        key="plain",
        headers=(
            "序号",
            "日期",
            "费用名称",
            "金额",
            "报销人",
            "是否持有发票",
            "备注（报销费用说明）",
        ),
        widths=(10.09, 12.36, 14.0, 10.45, 11.27, 16.45, 42.0),
        project_column=None,
    ),
}

_THIN = Side(style="thin", color="000000")
BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
TITLE_FONT = Font(name="微软雅黑", size=16, bold=True)
SUBTITLE_FONT = Font(name="微软雅黑", size=11, bold=True)
HEADER_FONT = Font(name="微软雅黑", size=11, bold=True)
BODY_FONT = Font(name="微软雅黑", size=10)
MISMATCH_FONT = Font(name="微软雅黑", size=10, bold=True, color="C00000")
TOTAL_FONT = Font(name="微软雅黑", size=10, bold=True)
CENTER = Alignment(horizontal="center", vertical="center")
LEFT = Alignment(horizontal="left", vertical="center")
WRAP_LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)


def get_layout(key: str) -> ReportLayout:
    try:
        return LAYOUTS[key]
    except KeyError as exc:
        raise ReportError(f"未知的报销单版式：{key}") from exc


def sanitize_filename(name: str, *, limit: int = 120) -> str:
    cleaned = _INVALID_FILENAME_RE.sub("_", name).strip().rstrip(". ")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip(". ")
    return cleaned or "报销单"


def report_filename(report: Report) -> str:
    year, month = report.month.split("-")
    stamp = f"{year[2:]}{month}"
    base = f"{report.company}费用报销支出明细表"
    if report.reporter:
        base += f"（{report.reporter}）"
    return sanitize_filename(f"{base}{stamp}") + ".xlsx"


def _set_literal(cell, value: str, *, limit: int = MAX_CELL_CHARS) -> None:
    text = value.replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    cell.value = text
    # User-authored text must never become a formula when the sheet is opened,
    # so every string cell is written as explicit text.
    cell.data_type = "s"


def display_date(value: str) -> str:
    """Render an ISO date the way the paper sheets do: ``2026.9.19``."""
    parts = value.split("-")
    if len(parts) != 3:
        return value
    try:
        year, month, day = (int(part) for part in parts)
    except ValueError:
        return value
    return f"{year}.{month}.{day}"


def invoice_label(line: ReportLine) -> str:
    """``有票`` / ``无票``, preserving the paper sheets' parenthetical notes."""
    if not line.has_invoice:
        return "无票"
    return f"有票（{line.invoice_note}）" if line.invoice_note else "有票"


def _write_title_rows(
    worksheet: Worksheet, report: Report, columns: int, merge_columns: int
) -> None:
    last = get_column_letter(merge_columns)
    worksheet.merge_cells(f"A1:{last}1")
    title = worksheet.cell(row=1, column=1)
    _set_literal(title, report.title, limit=200)
    title.font = TITLE_FONT
    title.alignment = CENTER
    worksheet.row_dimensions[1].height = 37

    worksheet.merge_cells(f"A2:{last}2")
    subtitle = worksheet.cell(row=2, column=1)
    _set_literal(
        subtitle,
        f"部门：{report.department}                                       "
        f"报销人：{report.reporter}",
        limit=200,
    )
    subtitle.font = SUBTITLE_FONT
    subtitle.alignment = LEFT
    worksheet.row_dimensions[2].height = 29

    label = worksheet.cell(row=3, column=1, value="报销金额：")
    label.font = SUBTITLE_FONT
    label.alignment = LEFT
    total = worksheet.cell(row=3, column=2, value=Decimal(report.amount_cents) / 100)
    total.number_format = MONEY_FORMAT
    total.font = SUBTITLE_FONT
    total.alignment = LEFT
    if columns > 2:
        upper_cell = worksheet.cell(row=3, column=3)
        _set_literal(
            upper_cell,
            f"大写：{chinese_upper_cents(report.amount_cents)}",
            limit=200,
        )
        upper_cell.font = SUBTITLE_FONT
        upper_cell.alignment = LEFT
        if merge_columns > 3:
            worksheet.merge_cells(
                start_row=3,
                start_column=3,
                end_row=3,
                end_column=merge_columns,
            )
    worksheet.row_dimensions[3].height = 29

    for row in (1, 2, 3):
        for column in range(1, columns + 1):
            worksheet.cell(row=row, column=column).border = BORDER


def _write_header_row(worksheet: Worksheet, layout: ReportLayout, columns: int) -> None:
    for column, header in enumerate(layout.headers, start=1):
        cell = worksheet.cell(row=4, column=column)
        _set_literal(cell, header, limit=100)
        cell.font = HEADER_FONT
        cell.alignment = CENTER
        cell.border = BORDER
    for column in range(1, columns + 1):
        worksheet.cell(row=4, column=column).border = BORDER
    worksheet.row_dimensions[4].height = 27


def _write_total_row(
    worksheet: Worksheet, report: Report, columns: int, excel_row: int
) -> None:
    """Grand-total row with the Chinese uppercase amount."""
    if columns >= 3:
        worksheet.merge_cells(
            start_row=excel_row, start_column=1, end_row=excel_row, end_column=3
        )
    label = worksheet.cell(row=excel_row, column=1)
    _set_literal(label, "合计", limit=20)
    label.font = TOTAL_FONT
    label.alignment = CENTER

    amount = worksheet.cell(row=excel_row, column=4)
    amount.value = Decimal(report.amount_cents) / 100
    amount.number_format = MONEY_FORMAT
    amount.font = TOTAL_FONT
    amount.alignment = CENTER

    if columns > 4:
        upper = worksheet.cell(row=excel_row, column=5)
        _set_literal(
            upper,
            f"大写：{chinese_upper_cents(report.amount_cents)}",
            limit=200,
        )
        upper.font = TOTAL_FONT
        upper.alignment = LEFT
        if columns > 5:
            worksheet.merge_cells(
                start_row=excel_row,
                start_column=5,
                end_row=excel_row,
                end_column=columns,
            )
    for column in range(1, columns + 1):
        worksheet.cell(row=excel_row, column=column).border = BORDER
    worksheet.row_dimensions[excel_row].height = 24


def _write_category_sheet(
    worksheet: Worksheet, report: Report, lines: list[ReportLine]
) -> None:
    """Per-category subtotals, grand total, and their uppercase amount."""
    headers = ("费用类别", "行数", "金额", "占比")
    worksheet.merge_cells("A1:D1")
    title = worksheet.cell(row=1, column=1)
    _set_literal(title, f"{report.title} · 分类汇总", limit=200)
    title.font = TITLE_FONT
    title.alignment = CENTER
    worksheet.row_dimensions[1].height = 32

    groups: dict[str, tuple[int, int]] = {}
    for line in lines:
        name = line.category or "未分类"
        count, cents = groups.get(name, (0, 0))
        groups[name] = (count + 1, cents + line.amount_cents)

    for column, header in enumerate(headers, start=1):
        cell = worksheet.cell(row=2, column=column)
        _set_literal(cell, header, limit=100)
        cell.font = HEADER_FONT
        cell.alignment = CENTER
        cell.border = BORDER
    worksheet.row_dimensions[2].height = 24

    total_cents = report.amount_cents
    ordered = sorted(groups.items(), key=lambda item: (-item[1][1], item[0]))
    row = 3
    for name, (count, cents) in ordered:
        category = worksheet.cell(row=row, column=1)
        _set_literal(category, name, limit=100)
        category.font = BODY_FONT
        category.alignment = WRAP_LEFT
        count_cell = worksheet.cell(row=row, column=2)
        count_cell.value = count
        count_cell.number_format = "0"
        count_cell.font = BODY_FONT
        count_cell.alignment = CENTER
        amount = worksheet.cell(row=row, column=3)
        amount.value = Decimal(cents) / 100
        amount.number_format = MONEY_FORMAT
        amount.font = BODY_FONT
        amount.alignment = CENTER
        share = worksheet.cell(row=row, column=4)
        share.value = cents / total_cents if total_cents else 0.0
        share.number_format = "0.0%"
        share.font = BODY_FONT
        share.alignment = CENTER
        for column in range(1, 5):
            worksheet.cell(row=row, column=column).border = BORDER
        row += 1

    label = worksheet.cell(row=row, column=1)
    _set_literal(label, "合计", limit=20)
    label.font = TOTAL_FONT
    label.alignment = CENTER
    count_cell = worksheet.cell(row=row, column=2)
    count_cell.value = len(lines)
    count_cell.number_format = "0"
    count_cell.font = TOTAL_FONT
    count_cell.alignment = CENTER
    amount = worksheet.cell(row=row, column=3)
    amount.value = Decimal(total_cents) / 100
    amount.number_format = MONEY_FORMAT
    amount.font = TOTAL_FONT
    amount.alignment = CENTER
    share = worksheet.cell(row=row, column=4)
    share.value = 1.0 if total_cents else 0.0
    share.number_format = "0.0%"
    share.font = TOTAL_FONT
    share.alignment = CENTER
    for column in range(1, 5):
        worksheet.cell(row=row, column=column).border = BORDER
    row += 1

    upper_label = worksheet.cell(row=row, column=1)
    _set_literal(upper_label, "金额大写", limit=20)
    upper_label.font = TOTAL_FONT
    upper_label.alignment = CENTER
    worksheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
    upper = worksheet.cell(row=row, column=2)
    _set_literal(upper, chinese_upper_cents(total_cents), limit=200)
    upper.font = TOTAL_FONT
    upper.alignment = LEFT
    for column in range(1, 5):
        worksheet.cell(row=row, column=column).border = BORDER
    worksheet.row_dimensions[row].height = 24

    for column, width in enumerate((26, 10, 16, 10), start=1):
        worksheet.column_dimensions[get_column_letter(column)].width = width
    worksheet.sheet_view.showGridLines = False
    worksheet.page_setup.orientation = "portrait"
    worksheet.page_setup.paperSize = Worksheet.PAPERSIZE_A4
    worksheet.page_setup.fitToWidth = 1
    worksheet.page_setup.fitToHeight = 0
    worksheet.sheet_properties.pageSetUpPr.fitToPage = True


def _write_line(
    worksheet: Worksheet,
    excel_row: int,
    sequence: int,
    line: ReportLine,
    report: Report,
    layout: ReportLayout,
) -> None:
    count = len(layout.headers)
    description_column = 7 if layout.project_column == "company" else count
    values: dict[int, object] = {
        1: sequence,
        2: display_date(line.line_date),
        3: line.category,
        5: report.reporter,
        6: invoice_label(line),
        description_column: line.description,
    }
    if layout.project_column == "project":
        values[7] = line.project_name
    elif layout.project_column == "company":
        values[8] = report.company

    for column in range(1, count + 1):
        cell = worksheet.cell(row=excel_row, column=column)
        value = values.get(column)
        if column == 4:
            cell.value = Decimal(line.amount_cents) / 100
            cell.number_format = MONEY_FORMAT
            cell.alignment = CENTER
        elif column == 1:
            cell.value = sequence
            cell.number_format = "0"
            cell.alignment = CENTER
        else:
            _set_literal(cell, "" if value is None else str(value))
            cell.alignment = WRAP_LEFT if column == description_column else CENTER
        cell.font = (
            MISMATCH_FONT
            if column == 4 and line.amount_mismatch
            else BODY_FONT
        )
        cell.border = BORDER


def write_report_workbook(
    path: str | Path,
    report: Report,
    lines: list[ReportLine],
    *,
    layout_key: str = "project",
    project_header: str = "",
) -> None:
    """Write the report workbook. Raises ``ReportError`` on refuse/write errors."""
    if not lines:
        raise ReportError("报销单没有报销行，无法导出。")
    layout = get_layout(layout_key)
    headers = layout.headers
    if project_header and layout.project_column == "project":
        headers = (*headers[:6], project_header.strip(), headers[7])
        layout = ReportLayout(
            key=layout.key,
            headers=headers,
            widths=layout.widths,
            project_column=layout.project_column,
            merge_columns=layout.merge_columns,
        )

    workbook = Workbook()
    workbook.properties.creator = "InvoiceDesk"
    workbook.properties.lastModifiedBy = "InvoiceDesk"
    workbook.properties.title = report.title
    workbook.properties.description = "本机离线生成的报销单，含财务信息。"

    worksheet = workbook.active
    worksheet.title = "明细"
    columns = len(headers)
    _write_title_rows(worksheet, report, columns, layout.merge_columns or columns)
    _write_header_row(worksheet, layout, columns)
    for index, line in enumerate(lines, start=1):
        _write_line(worksheet, index + 4, index, line, report, layout)
    _write_total_row(worksheet, report, columns, len(lines) + 5)
    _write_category_sheet(workbook.create_sheet("分类汇总"), report, lines)

    for column, width in enumerate(layout.widths, start=1):
        worksheet.column_dimensions[get_column_letter(column)].width = width
    worksheet.sheet_view.showGridLines = False
    worksheet.page_setup.orientation = "portrait"
    worksheet.page_setup.paperSize = Worksheet.PAPERSIZE_A4
    worksheet.page_setup.fitToWidth = 1
    worksheet.page_setup.fitToHeight = 0
    worksheet.sheet_properties.pageSetUpPr.fitToPage = True
    worksheet.print_title_rows = "1:4"

    target = Path(path)
    if target.is_symlink():
        raise ReportError("导出目标是符号链接，已拒绝写入。")
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(target)
