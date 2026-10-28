"""CLI commands for reimbursement reports: ``invoice-desk report ...``."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.core import db, reports
from app.core.paths import is_unsafe_repo_path, remote_storage_reason
from app.core.report_export import (
    LAYOUTS,
    display_date,
    invoice_label,
    report_filename,
    write_report_workbook,
)
from app.core.reports import ReportError

DEFAULT_REPORT_DIR = Path("out") / "report"


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("编号必须是整数") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("编号必须 >= 1")
    return number


def _money(value: str) -> Decimal:
    try:
        return reports.parse_amount(value)
    except ReportError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _distance(value: str) -> Decimal:
    try:
        return reports.parse_km(value)
    except ReportError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _month(value: str) -> str:
    try:
        return reports.normalize_month(value)
    except ReportError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _iso_date(value: str) -> str:
    try:
        return reports.normalize_line_date(value)
    except ReportError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _invoice_ids(value: str) -> list[int]:
    ids: list[int] = []
    for part in value.replace("，", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            number = int(part)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"发票编号必须是整数：{part}") from exc
        if number < 1:
            raise argparse.ArgumentTypeError("发票编号必须 >= 1")
        if number not in ids:
            ids.append(number)
    if not ids:
        raise argparse.ArgumentTypeError("至少需要一张发票")
    return ids


def _output_reason(path: Path) -> str | None:
    """Why ``path`` is not local/ignored storage, or None.

    Same policy as the invoice exports; kept here so the ``report`` subcommands
    stay importable without loading the parent CLI module.
    """
    remote = remote_storage_reason(path)
    if remote is not None:
        return f"位于{remote}"
    if is_unsafe_repo_path(path):
        return "在仓库内非忽略目录"
    return None


def _open_database(args: argparse.Namespace) -> sqlite3.Connection | None:
    try:
        return db.connect(args.db)
    except (db.UnsafeDatabasePathError, db.DatabaseVersionError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
    except (OSError, sqlite3.Error):
        print("错误: 无法打开数据库。", file=sys.stderr)
    return None


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _add_db_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH, help="SQLite database path")


def add_report_parser(subparsers) -> None:
    report = subparsers.add_parser("report", help="create and export reimbursement reports")
    report_sub = report.add_subparsers(dest="report_command", required=True)

    create = report_sub.add_parser("create", help="create a reimbursement report")
    create.add_argument("--company", required=True, help="company short name, e.g. 示例甲")
    create.add_argument("--reporter", required=True, help="who submits the report")
    create.add_argument("--month", required=True, type=_month, help="report month (YYYY-MM)")
    create.add_argument("--department", default="", help="department line")
    create.add_argument("--title", default="", help="override the generated title")
    create.add_argument("--note", default="", help="free-form note")
    _add_db_argument(create)

    listing = report_sub.add_parser("list", help="list reports")
    _add_db_argument(listing)

    show = report_sub.add_parser("show", help="show a report and its lines")
    show.add_argument("--report", required=True, type=_positive_int)
    _add_db_argument(show)

    add_line = report_sub.add_parser("add-line", help="add a manual expense line")
    add_line.add_argument("--report", required=True, type=_positive_int)
    add_line.add_argument("--date", required=True, type=_iso_date, help="expense date (YYYY-MM-DD)")
    add_line.add_argument("--category", default="其他费用", help="expense category")
    add_line.add_argument("--amount", required=True, type=_money, help="amount in yuan")
    add_line.add_argument("--description", required=True, help="expense description")
    add_line.add_argument("--project", default="", help="project name")
    add_line.add_argument("--invoice", type=_invoice_ids, help="invoice ids, e.g. 12,13")
    add_line.add_argument(
        "--has-invoice", action="store_true", help="mark as receipt-backed without linking ids"
    )
    add_line.add_argument(
        "--invoice-note", default="", help="extra text inside 有票（…）, e.g. 加油、高速"
    )
    _add_db_argument(add_line)

    mileage = report_sub.add_parser("add-mileage", help="add a mileage line at 1.5 yuan/km")
    mileage.add_argument("--report", required=True, type=_positive_int)
    mileage.add_argument("--date", required=True, type=_iso_date)
    mileage.add_argument("--km", required=True, type=_distance)
    mileage.add_argument("--description", required=True)
    mileage.add_argument("--category", default="汽车费用")
    mileage.add_argument("--rate", type=_money, default=reports.DEFAULT_MILEAGE_RATE)
    mileage.add_argument("--project", default="")
    _add_db_argument(mileage)

    update = report_sub.add_parser("update-line", help="change fields on an existing line")
    update.add_argument("--report", required=True, type=_positive_int)
    update.add_argument("--line", required=True, type=_positive_int, help="1-based line number")
    update.add_argument("--date", type=_iso_date)
    update.add_argument("--category")
    update.add_argument("--amount", type=_money)
    update.add_argument("--description")
    update.add_argument("--invoice-note")
    update.add_argument("--project")
    flags = update.add_mutually_exclusive_group()
    flags.add_argument("--has-invoice", dest="has_invoice", action="store_true", default=None)
    flags.add_argument("--no-invoice", dest="has_invoice", action="store_false")
    _add_db_argument(update)

    remove = report_sub.add_parser("remove-line", help="delete a line and its invoice links")
    remove.add_argument("--report", required=True, type=_positive_int)
    remove.add_argument("--line", required=True, type=_positive_int)
    _add_db_argument(remove)

    link = report_sub.add_parser("link", help="link invoices to a line")
    link.add_argument("--report", required=True, type=_positive_int)
    link.add_argument("--line", required=True, type=_positive_int)
    link.add_argument("--invoice", required=True, type=_invoice_ids)
    _add_db_argument(link)

    unlink = report_sub.add_parser("unlink", help="remove invoice links from a line")
    unlink.add_argument("--report", required=True, type=_positive_int)
    unlink.add_argument("--line", required=True, type=_positive_int)
    unlink.add_argument("--invoice", required=True, type=_invoice_ids)
    _add_db_argument(unlink)

    from_invoices = report_sub.add_parser(
        "from-invoices", help="add one line per unlinked invoice in the pool"
    )
    from_invoices.add_argument("--report", required=True, type=_positive_int)
    from_invoices.add_argument("--batch", type=_positive_int, help="only this batch id")
    from_invoices.add_argument("--buyer", help="only this exact buyer name")
    from_invoices.add_argument("--from", dest="date_from", type=_iso_date)
    from_invoices.add_argument("--to", dest="date_to", type=_iso_date)
    from_invoices.add_argument(
        "--exclude-review", action="store_true", help="skip invoices flagged for review"
    )
    from_invoices.add_argument("--project", default="", help="project name for generated lines")
    _add_db_argument(from_invoices)

    export = report_sub.add_parser("export", help="export the report to the company Excel sheet")
    export.add_argument("--report", required=True, type=_positive_int)
    export.add_argument("--out", type=Path, default=DEFAULT_REPORT_DIR, help="output directory")
    export.add_argument(
        "--layout", choices=sorted(LAYOUTS), default="project", help="column layout preset"
    )
    export.add_argument(
        "--project-header",
        default="",
        help="override the project column header (e.g. 费用发生项目)",
    )
    export.add_argument(
        "--force",
        action="store_true",
        help="allow an unsafe output path (repo outside out/data, network, OneDrive)",
    )
    export.add_argument("--debug", action="store_true", help="show tracebacks")
    _add_db_argument(export)


def _run_create(args: argparse.Namespace) -> int:
    connection = _open_database(args)
    if connection is None:
        return 2
    try:
        report_id = reports.create_report(
            connection,
            company=args.company,
            reporter=args.reporter,
            month=args.month,
            department=args.department,
            title=args.title,
            note=args.note,
        )
        report = reports.get_report(connection, report_id)
    except ReportError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error:
        print("错误: 无法写入数据库。", file=sys.stderr)
        return 2
    finally:
        connection.close()
    print(f"已创建报销单 #{report.id}：{report.title}")
    print(f"公司 {report.company}｜报销人 {report.reporter}｜月份 {report.month}")
    return 0


def _run_list(args: argparse.Namespace) -> int:
    connection = _open_database(args)
    if connection is None:
        return 2
    try:
        found = reports.list_reports(connection)
    except sqlite3.Error:
        print("错误: 无法读取数据库。", file=sys.stderr)
        return 2
    finally:
        connection.close()
    if not found:
        print("还没有报销单。")
        return 0
    for report in found:
        print(
            f"#{report.id} {report.month} {report.company}｜{report.reporter}｜"
            f"{report.line_count} 行｜{report.amount:,.2f} 元｜{report.title}"
        )
    return 0


def _run_show(args: argparse.Namespace) -> int:
    connection = _open_database(args)
    if connection is None:
        return 2
    try:
        report = reports.get_report(connection, args.report)
        lines = reports.report_lines(connection, args.report)
    except ReportError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error:
        print("错误: 无法读取数据库。", file=sys.stderr)
        return 2
    finally:
        connection.close()

    print(f"报销单 #{report.id}：{report.title}")
    print(
        f"公司 {report.company}｜部门 {report.department or '-'}｜"
        f"报销人 {report.reporter}｜月份 {report.month}"
    )
    if not lines:
        print("还没有报销行。")
        return 0
    print(f"共 {len(lines)} 行，合计 {report.amount:,.2f} 元")
    print(f"{'序号':<4} {'日期':<11} {'费用名称':<8} {'金额':>11}  {'有票':<8} {'项目':<10} 备注")
    for sequence, line in enumerate(lines, start=1):
        note = line.description
        if line.mileage_km is not None:
            note = f"[{line.mileage_km:g}km] {note}"
        if line.invoice_ids:
            ids = "、".join(f"#{invoice_id}" for invoice_id in line.invoice_ids)
            note = f"{note}（发票 {ids}）"
        print(
            f"{sequence:<4} {display_date(line.line_date):<11} "
            f"{_truncate(line.category, 8):<8} {line.amount:>11,.2f}  "
            f"{invoice_label(line):<8} "
            f"{_truncate(line.project_name, 10):<10} {_truncate(note, 60)}"
        )
    mismatches = [
        sequence for sequence, line in enumerate(lines, start=1) if line.amount_mismatch
    ]
    if mismatches:
        shown = "、".join(str(number) for number in mismatches[:10])
        print(f"警告: 第 {shown} 行关联发票合计与行金额不一致", file=sys.stderr)
    unlinked = [
        sequence
        for sequence, line in enumerate(lines, start=1)
        if line.has_invoice and not line.invoice_ids
    ]
    if unlinked:
        print(
            f"提示: {len(unlinked)} 行标记有票但未关联发票（仅导出“有票”，无证据链）",
            file=sys.stderr,
        )
    return 0


def _run_add_line(args: argparse.Namespace) -> int:
    connection = _open_database(args)
    if connection is None:
        return 2
    try:
        report = reports.get_report(connection, args.report)
        amount_cents = db.money_to_cents(args.amount)
        if amount_cents is None:  # pragma: no cover - argparse requires a value
            raise ReportError("金额不能为空")
        reports.add_line(
            connection,
            args.report,
            line_date=args.date,
            category=args.category,
            description=args.description,
            amount_cents=amount_cents,
            has_invoice=args.has_invoice,
            invoice_note=args.invoice_note,
            project_name=args.project,
            invoice_ids=args.invoice or (),
        )
    except ReportError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error:
        print("错误: 无法写入数据库。", file=sys.stderr)
        return 2
    finally:
        connection.close()
    sequence = report.line_count + 1
    print(
        f"已添加第 {sequence} 行：{display_date(args.date)} {args.category} "
        f"{args.amount:,.2f} 元"
    )
    if args.invoice:
        print(f"关联发票 {len(args.invoice)} 张")
    return 0


def _run_add_mileage(args: argparse.Namespace) -> int:
    connection = _open_database(args)
    if connection is None:
        return 2
    try:
        report = reports.get_report(connection, args.report)
        _, amount_cents = reports.add_mileage(
            connection,
            args.report,
            line_date=args.date,
            km=args.km,
            description=args.description,
            category=args.category,
            rate=args.rate,
            project_name=args.project,
        )
    except ReportError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error:
        print("错误: 无法写入数据库。", file=sys.stderr)
        return 2
    finally:
        connection.close()
    sequence = report.line_count + 1
    print(
        f"已添加第 {sequence} 行（里程 {args.km:g} km × {args.rate:g} = "
        f"{amount_cents / 100:,.2f} 元）"
    )
    return 0


def _run_update_line(args: argparse.Namespace) -> int:
    values: dict[str, object] = {}
    if args.date is not None:
        values["line_date"] = args.date
    if args.category is not None:
        values["category"] = args.category
    if args.description is not None:
        values["description"] = args.description
    if args.invoice_note is not None:
        values["invoice_note"] = args.invoice_note
    if args.project is not None:
        values["project_name"] = args.project
    if args.amount is not None:
        cents = db.money_to_cents(args.amount)
        if cents is None:  # pragma: no cover - argparse requires a value
            print("错误: 金额不能为空", file=sys.stderr)
            return 2
        values["amount_cents"] = cents
    if args.has_invoice is not None:
        values["has_invoice"] = args.has_invoice
    if not values:
        print("错误: 没有要修改的内容。", file=sys.stderr)
        return 2
    connection = _open_database(args)
    if connection is None:
        return 2
    try:
        reports.update_line(connection, args.report, args.line, **values)
    except ReportError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error:
        print("错误: 无法写入数据库。", file=sys.stderr)
        return 2
    finally:
        connection.close()
    print(f"已更新第 {args.line} 行。")
    return 0


def _run_remove_line(args: argparse.Namespace) -> int:
    connection = _open_database(args)
    if connection is None:
        return 2
    try:
        reports.delete_line(connection, args.report, args.line)
    except ReportError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error:
        print("错误: 无法写入数据库。", file=sys.stderr)
        return 2
    finally:
        connection.close()
    print(f"已删除第 {args.line} 行。")
    return 0


def _run_link(args: argparse.Namespace) -> int:
    connection = _open_database(args)
    if connection is None:
        return 2
    try:
        added = reports.link_invoices(connection, args.report, args.line, args.invoice)
    except ReportError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error:
        print("错误: 无法写入数据库。", file=sys.stderr)
        return 2
    finally:
        connection.close()
    if added:
        print(f"已关联 {added} 张发票到第 {args.line} 行。")
    else:
        print("没有新增关联（发票均已关联）。")
    return 0


def _run_unlink(args: argparse.Namespace) -> int:
    connection = _open_database(args)
    if connection is None:
        return 2
    try:
        removed = reports.unlink_invoices(connection, args.report, args.line, args.invoice)
    except ReportError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error:
        print("错误: 无法写入数据库。", file=sys.stderr)
        return 2
    finally:
        connection.close()
    if removed:
        print(f"已取消 {removed} 张发票关联。")
    else:
        print("没有匹配的关联。")
    return 0


def _run_from_invoices(args: argparse.Namespace) -> int:
    connection = _open_database(args)
    if connection is None:
        return 2
    try:
        result = reports.from_invoices(
            connection,
            args.report,
            batch_id=args.batch,
            date_from=args.date_from or "",
            date_to=args.date_to or "",
            buyer=args.buyer or "",
            include_review=not args.exclude_review,
            project_name=args.project,
        )
    except ReportError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error:
        print("错误: 无法写入数据库。", file=sys.stderr)
        return 2
    finally:
        connection.close()
    if result.added:
        print(f"已从发票池添加 {result.added} 行到报销单 #{args.report}。")
    else:
        print("没有可添加的发票。")
    if result.skipped:
        print(
            f"跳过 {result.skipped} 张：已关联 {result.skipped_linked}、"
            f"缺金额 {result.skipped_missing_amount}、缺日期 {result.skipped_missing_date}",
            file=sys.stderr,
        )
    return 0 if result.added else 1


def _run_export(args: argparse.Namespace) -> int:
    out = args.out.expanduser().resolve()
    if not args.db.exists():
        print(f"数据库不存在: {args.db}", file=sys.stderr)
        return 1
    if out.exists() and not out.is_dir():
        print(f"错误: 输出路径不是目录: {out}", file=sys.stderr)
        return 2
    reason = _output_reason(out)
    if reason is not None and not args.force:
        print(
            f"错误: 输出路径{reason}，财务数据只允许保存在本机固定盘"
            "或仓库的 out/、data/ 下；确需如此请加 --force。",
            file=sys.stderr,
        )
        return 2

    connection = _open_database(args)
    if connection is None:
        return 2
    try:
        report = reports.get_report(connection, args.report)
        lines = reports.report_lines(connection, args.report)
    except ReportError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error:
        print("错误: 无法读取数据库。", file=sys.stderr)
        return 2
    finally:
        connection.close()

    if not lines:
        print("报销单没有报销行，无法导出。", file=sys.stderr)
        return 1

    target = out / report_filename(report)
    stamp = uuid4().hex[:8]
    temp = out / f".{target.name}.{stamp}.tmp"
    try:
        write_report_workbook(
            temp,
            report,
            lines,
            layout_key=args.layout,
            project_header=args.project_header,
        )
        if target.is_symlink():
            raise ReportError("导出目标是符号链接，已拒绝写入。")
        os.replace(temp, target)
    except ReportError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        if args.debug:
            raise
        print("错误: 无法写入导出文件（加 --debug 查看详情）。", file=sys.stderr)
        return 2
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)

    print(f"已导出报销单 #{report.id}：{len(lines)} 行，合计 {report.amount:,.2f} 元")
    print(f"Excel: {target}")
    unlinked = sum(1 for line in lines if line.has_invoice and not line.invoice_ids)
    if unlinked:
        print(f"提示: {unlinked} 行标记有票但未关联发票。", file=sys.stderr)
    mismatches = sum(1 for line in lines if line.amount_mismatch)
    if mismatches:
        print(
            f"警告: {mismatches} 行关联发票合计与行金额不一致，提交前请核对。",
            file=sys.stderr,
        )
    return 0


_HANDLERS = {
    "create": _run_create,
    "list": _run_list,
    "show": _run_show,
    "add-line": _run_add_line,
    "add-mileage": _run_add_mileage,
    "update-line": _run_update_line,
    "remove-line": _run_remove_line,
    "link": _run_link,
    "unlink": _run_unlink,
    "from-invoices": _run_from_invoices,
    "export": _run_export,
}


def run_report(args: argparse.Namespace) -> int:
    handler = _HANDLERS.get(args.report_command)
    if handler is None:  # pragma: no cover - argparse enforces the choices
        return 2
    return handler(args)
