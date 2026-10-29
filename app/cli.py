"""Command-line entry point for local parsing and evaluation."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.cli_report import add_report_parser, run_report
from app.core import db
from app.core.excel_export import EXCEL_FILENAME, write_workbook
from app.core.export import ExportError, ExportFilters, collect_rows
from app.core.export import summarize as summarize_export
from app.core.group_pdf import GROUP_DIRNAME, write_group_pdfs
from app.core.importer import import_folder, scan_pdfs
from app.core.models import ParsedInvoice
from app.core.parser import parse_pdf, summarize
from app.core.paths import is_unsafe_repo_path, remote_storage_reason
from app.core.print_pdf import PRINT_FILENAME, write_print_pdf
from app.core.text import normalize

DEFAULT_DB = db.DEFAULT_DB_PATH
DEFAULT_EXPORT_DIR = Path("out") / "export"


def _safe(value: object) -> str:
    return normalize(str(value))


def _is_unsafe_repo_output(output: Path) -> bool:
    """Refuse to write invoices into the repository unless it is an ignored dir."""
    return is_unsafe_repo_path(output)


def _unsafe_output_reason(output: Path) -> str | None:
    """Why an export directory is not local/ignored storage, or None."""
    remote = remote_storage_reason(output)
    if remote is not None:
        return f"位于{remote}"
    if is_unsafe_repo_path(output):
        return "在仓库内非忽略目录"
    return None


def _print_skipped(label: str, references: Sequence[str]) -> None:
    if not references:
        return
    shown = "、".join(references[:5])
    more = "…" if len(references) > 5 else ""
    print(f"警告: {label} {len(references)} 张：{shown}{more}", file=sys.stderr)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def parse_folder(path: Path, limit: int | None = None) -> list[ParsedInvoice]:
    invoices: list[ParsedInvoice] = []
    files = scan_pdfs(path)
    if limit:
        files = files[:limit]
    for item in files:
        invoices.extend(parse_pdf(item))
    return invoices


def _format_money(value) -> str:
    return "-" if value is None else f"{value:,.2f}"


def print_report(
    invoices: Sequence[ParsedInvoice], *, verbose: bool = False, show_detail: bool = False
) -> None:
    if show_detail:
        print(f"{'文件':<34} {'类型':<20} {'日期':<10} {'金额':>12}  {'类别':<8} {'票号'}")
        print("-" * 118)
    for invoice in invoices if show_detail else []:
        name = _safe(Path(invoice.source_path).name)
        if invoice.page_count > 1:
            name = f"{name}#p{invoice.page_index + 1}"
        name = name[:32]
        print(
            f"{name:<34} {invoice.invoice_type.value:<20} "
            f"{invoice.invoice_date or '-':<10} {_format_money(invoice.amount_total):>12}  "
            f"{invoice.category:<8} {invoice.invoice_number or '-'}"
        )
        if verbose or invoice.needs_review:
            if invoice.seller_name:
                print(f"{'':34} 销售方: {_safe(invoice.seller_name)}")
            if invoice.item_summary:
                print(f"{'':34} 项目: {_safe(invoice.item_summary)}")
            if invoice.warnings:
                print(f"{'':34} 待确认: {'; '.join(_safe(w) for w in invoice.warnings)}")

    summary = summarize(invoices)
    types: dict[str, int] = {}
    for invoice in invoices:
        types[invoice.invoice_type.value] = types.get(invoice.invoice_type.value, 0) + 1
    with_number = sum(1 for invoice in invoices if invoice.invoice_number)
    with_date = sum(1 for invoice in invoices if invoice.invoice_date)
    with_amount = sum(1 for invoice in invoices if invoice.amount_total is not None)
    with_seller = sum(1 for invoice in invoices if invoice.seller_name)

    if show_detail:
        print("-" * 118)
    print(
        f"共 {summary.total} 条，待确认 {summary.review} 条，合计 {summary.amount:,.2f} 元；"
        f"票号 {with_number}/{summary.total}，日期 {with_date}/{summary.total}，"
        f"金额 {with_amount}/{summary.total}，销售方 {with_seller}/{summary.total}"
    )
    print("票型: " + ", ".join(f"{name}={count}" for name, count in sorted(types.items())))


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("编号必须 >= 1")
    return number


def _iso_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期格式应为 YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invoice-desk", description="InvoiceDesk local tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse_cmd = subparsers.add_parser("parse", help="parse a PDF or a folder of PDFs")
    parse_cmd.add_argument("path", type=Path)
    parse_cmd.add_argument("--json", type=Path, help="write parsed records as JSON")
    parse_cmd.add_argument("--limit", type=_positive_int, help="only parse the first N files")
    parse_cmd.add_argument("--verbose", action="store_true")
    parse_cmd.add_argument(
        "--absolute-paths",
        action="store_true",
        help="include absolute source paths in JSON (debug only; never commit)",
    )
    parse_cmd.add_argument(
        "--include-tax-ids",
        action="store_true",
        help="include full buyer/seller tax IDs in JSON (sensitive)",
    )
    parse_cmd.add_argument(
        "--force",
        action="store_true",
        help="allow writing invoice JSON inside the repository (unsafe)",
    )

    import_cmd = subparsers.add_parser(
        "import", help="parse a folder and import it into the local database"
    )
    import_cmd.add_argument("path", type=Path)
    import_cmd.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    import_cmd.add_argument("--label", help="batch label (defaults to the folder name)")
    import_cmd.add_argument("--limit", type=_positive_int, help="only import the first N files")
    import_cmd.add_argument(
        "--force",
        action="store_true",
        help="allow a database inside the repository outside data/ (unsafe)",
    )
    import_cmd.add_argument(
        "--debug",
        action="store_true",
        help="show the traceback for unexpected import errors",
    )

    stats_cmd = subparsers.add_parser("stats", help="show stored batches and totals")
    stats_cmd.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")

    export_cmd = subparsers.add_parser(
        "export", help="export invoice details to Excel and a 2-up A4 print PDF"
    )
    export_cmd.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    export_cmd.add_argument(
        "--out", type=Path, default=DEFAULT_EXPORT_DIR, help="output directory"
    )
    export_cmd.add_argument("--batch", type=_positive_int, help="only this batch id")
    export_cmd.add_argument("--buyer", help="only this exact buyer name")
    export_cmd.add_argument(
        "--from", dest="date_from", type=_iso_date, help="first invoice date (YYYY-MM-DD)"
    )
    export_cmd.add_argument(
        "--to", dest="date_to", type=_iso_date, help="last invoice date (YYYY-MM-DD)"
    )
    export_cmd.add_argument(
        "--exclude-review",
        action="store_true",
        help="skip invoices flagged for review",
    )
    export_cmd.add_argument(
        "--force",
        action="store_true",
        help="allow an unsafe output path (repo outside out/data, network, OneDrive)",
    )
    export_cmd.add_argument(
        "--debug",
        action="store_true",
        help="show the traceback for unexpected export errors",
    )

    add_report_parser(subparsers)
    return parser


def _run_parse(args: argparse.Namespace) -> int:
    if not args.path.exists():
        print(f"路径不存在: {args.path}", file=sys.stderr)
        return 2

    invoices = parse_folder(args.path, args.limit)
    if not invoices:
        print("没有找到 PDF 文件。")
        return 1

    if args.verbose and not sys.stdout.isatty():
        print(
            "警告: 明细输出可能被重定向并记录，含发票敏感信息。",
            file=sys.stderr,
        )
    print_report(invoices, verbose=args.verbose, show_detail=args.verbose)
    if args.json:
        if _is_unsafe_repo_output(args.json) and not args.force:
            print(
                "错误: JSON 输出在仓库内，可能把真实发票数据带进 Git；"
                "请写到 out/、data/ 或仓库外，确需如此请加 --force。",
                file=sys.stderr,
            )
            return 2
        if args.absolute_paths:
            print(
                "警告: --absolute-paths 会输出完整本机路径，仅用于本地调试，切勿提交。",
                file=sys.stderr,
            )
        try:
            args.json.write_text(
                json.dumps(
                    [
                        invoice.to_dict(
                            include_absolute_path=args.absolute_paths,
                            include_tax_ids=args.include_tax_ids,
                        )
                        for invoice in invoices
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"错误: 无法写入 JSON: {exc}", file=sys.stderr)
            return 2
        print(f"\nJSON 已写入: {args.json}")
    return 0


def _run_import(args: argparse.Namespace) -> int:
    if not args.path.exists():
        print(f"路径不存在: {args.path}", file=sys.stderr)
        return 2

    try:
        connection = db.connect(args.db, allow_unsafe_path=args.force)
    except (db.UnsafeDatabasePathError, db.DatabaseVersionError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        if args.debug:
            raise
        print("错误: 无法打开数据库（加 --debug 查看详情）。", file=sys.stderr)
        return 2
    try:
        summary = import_folder(connection, args.path, label=args.label, limit=args.limit)
    except (OSError, sqlite3.Error):
        if args.debug:
            raise
        print(
            "错误: 导入失败：无法读取输入文件或写入数据库（加 --debug 查看详情）。",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:  # transaction already rolled back by import_folder
        if args.debug:
            raise
        print(f"错误: 导入失败（{type(exc).__name__}）。加 --debug 查看详情。", file=sys.stderr)
        return 2
    finally:
        connection.close()

    if summary.files_seen == 0:
        print("没有找到 PDF 文件。")
        return 1
    if not summary.has_new_data:
        print(
            f"没有新文件：{summary.files_seen} 个 PDF 均已导入"
            f"（重复文件 {summary.files_duplicate}）。"
        )
        return 0

    print(f"批次 #{summary.batch_id}「{_safe(summary.label)}」已导入")
    print(
        f"新增文件 {summary.files_imported}，发票 {summary.invoices_imported}，"
        f"重复文件 {summary.files_duplicate}，重复票号 {summary.invoices_duplicate}，"
        f"冲突 {summary.invoices_conflict}，待确认 {summary.needs_review}，"
        f"合计 {summary.amount_total:,.2f} 元"
    )
    print(f"数据库: {args.db}")
    return 0


def _run_stats(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"数据库不存在: {args.db}", file=sys.stderr)
        return 1
    try:
        connection = db.connect(args.db)
    except (db.UnsafeDatabasePathError, db.DatabaseVersionError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        print("错误: 无法打开数据库。", file=sys.stderr)
        return 2
    try:
        batches = db.batch_stats(connection)
    finally:
        connection.close()

    if not batches:
        print("数据库为空。")
        return 0
    for batch in batches:
        print(
            f"批次 #{batch.id}「{_safe(batch.label) or '-'}」: 文件 {batch.file_count}，"
            f"发票 {batch.invoice_count}，重复票号 {batch.duplicate_count}，"
            f"冲突 {batch.conflict_count}，待确认 {batch.review_count}，"
            f"合计 {batch.amount_total:,.2f} 元"
        )
    total_amount = sum((batch.amount_total for batch in batches), Decimal("0.00"))
    print(
        f"合计: 批次 {len(batches)}，文件 {sum(b.file_count for b in batches)}，"
        f"发票 {sum(b.invoice_count for b in batches)}，"
        f"重复票号 {sum(b.duplicate_count for b in batches)}，"
        f"冲突 {sum(b.conflict_count for b in batches)}，"
        f"待确认 {sum(b.review_count for b in batches)}，合计 {total_amount:,.2f} 元"
    )
    return 0


def _run_export(args: argparse.Namespace) -> int:
    # Resolve once so the guard inspects exactly the path that receives files
    # (`..`, symlinks, and junctions must not be able to dodge the check).
    out = args.out.expanduser().resolve()
    if not args.db.exists():
        print(f"数据库不存在: {args.db}", file=sys.stderr)
        return 1
    if out.exists() and not out.is_dir():
        print(f"错误: 输出路径不是目录: {out}", file=sys.stderr)
        return 2
    if args.date_from and args.date_to and args.date_from > args.date_to:
        print("错误: --from 不能晚于 --to。", file=sys.stderr)
        return 2
    reason = _unsafe_output_reason(out)
    if reason is not None and not args.force:
        print(
            f"错误: 输出路径{reason}，财务数据只允许保存在本机固定盘"
            "或仓库的 out/、data/ 下；确需如此请加 --force。",
            file=sys.stderr,
        )
        return 2

    try:
        connection = db.connect(args.db)
    except (db.UnsafeDatabasePathError, db.DatabaseVersionError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        if args.debug:
            raise
        print("错误: 无法打开数据库（加 --debug 查看详情）。", file=sys.stderr)
        return 2
    try:
        if (
            args.batch is not None
            and connection.execute(
                "SELECT 1 FROM batches WHERE id = ?", (args.batch,)
            ).fetchone()
            is None
        ):
            print(f"批次不存在: #{args.batch}", file=sys.stderr)
            return 1
        filters = ExportFilters(
            batch_id=args.batch,
            buyer=args.buyer or "",
            date_from=args.date_from or "",
            date_to=args.date_to or "",
            include_review=not args.exclude_review,
        )
        rows = collect_rows(connection, filters)
        summary = summarize_export(rows)
    except sqlite3.Error:
        if args.debug:
            raise
        print("错误: 查询发票失败（加 --debug 查看详情）。", file=sys.stderr)
        return 2
    finally:
        connection.close()

    if not rows:
        print("没有符合条件的发票。")
        return 1

    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    excel_path = out / EXCEL_FILENAME
    print_path = out / PRINT_FILENAME
    stamp = uuid4().hex[:8]
    temp_excel = out / f".{EXCEL_FILENAME}.{stamp}.tmp"
    temp_print = out / f".{PRINT_FILENAME}.{stamp}.tmp"
    try:
        # Stage both files first, then replace the visible pair. A failure
        # while writing leaves the previous export untouched.
        print_info = write_print_pdf(temp_print, rows)
        write_workbook(
            temp_excel,
            rows,
            summary,
            filters=filters,
            generated_at=generated_at,
            print_info=print_info,
        )
        if print_path.is_symlink() or excel_path.is_symlink():
            raise ExportError("导出目标是符号链接，已拒绝写入。")
        group_info = write_group_pdfs(out / GROUP_DIRNAME, rows)
        os.replace(temp_print, print_path)
        os.replace(temp_excel, excel_path)
    except ExportError as exc:
        if args.debug:
            raise
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        if args.debug:
            raise
        print("错误: 无法写入导出文件（加 --debug 查看详情）。", file=sys.stderr)
        return 2
    finally:
        for temp in (temp_excel, temp_print):
            if temp.exists():
                temp.unlink(missing_ok=True)

    print(
        f"已导出 {summary.count} 张发票（待确认 {summary.review_count} 张），"
        f"合计 {summary.total_cents / 100:,.2f} 元"
    )
    print(f"Excel: {excel_path}")
    if print_info.notice_only:
        print(
            f"打印 PDF: {print_path}（本次无可打印票面，已生成说明页）",
            file=sys.stderr,
        )
    else:
        print(
            f"打印 PDF: {print_path}"
            f"（{print_info.pages} 页，{print_info.unique_pages} 张票面，"
            f"{print_info.records} 条记录）"
        )
    if group_info.groups:
        print(
            f"分类 PDF: {group_info.directory}"
            f"（{len(group_info.groups)} 个文件，按公司和分类拆分，A4 二合一，无标注）"
        )
        for group in group_info.groups:
            print(
                f"  {group.company} / {group.category}"
                f"：{group.count} 张、{group.source_pages} 张票面 / "
                f"{group.pages} 张 A4 → {group.path}"
            )
    _print_skipped("缺少源文件", print_info.skipped_missing)
    _print_skipped("无法打开票面", print_info.skipped_invalid)
    _print_skipped("分类 PDF 缺少源文件", group_info.skipped_missing)
    _print_skipped("分类 PDF 无法打开票面", group_info.skipped_invalid)
    if print_info.records != summary.count:
        print(
            f"提示: 打印包缺少 {summary.count - print_info.records} 条发票记录，"
            "Excel 明细仍完整。",
            file=sys.stderr,
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    args = build_parser().parse_args(argv)
    if args.command == "parse":
        return _run_parse(args)
    if args.command == "import":
        return _run_import(args)
    if args.command == "stats":
        return _run_stats(args)
    if args.command == "export":
        return _run_export(args)
    if args.command == "report":
        return run_report(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
