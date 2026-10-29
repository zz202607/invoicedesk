"""Controlled JSON bridge between the pywebview window and the core services.

Only use-case methods are exposed. Every method returns an envelope
``{"ok": true, "data": ...}`` or ``{"ok": false, "error": "..."}`` so a failed
call becomes a Chinese message in the UI instead of a rejected promise. Paths
and database handles stay on the Python side; the web UI sends ids and filters.
"""

from __future__ import annotations

import contextlib
import functools
import os
import sqlite3
import sys
import threading
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app import __version__
from app.core import db, importer, invoices, learning, reports, workbench
from app.core.categories import CATEGORIES, DEFAULT_CATEGORY
from app.core.excel_export import EXCEL_FILENAME, write_workbook
from app.core.export import (
    ExportError,
    ExportFilters,
    ExportRow,
    collect_rows,
    redact_paths,
)
from app.core.export import summarize as summarize_export
from app.core.paths import output_path_reason
from app.core.print_pdf import PRINT_FILENAME, write_print_pdf
from app.core.report_export import LAYOUTS, report_filename, write_report_workbook
from app.core.reports import ReportError

LAYOUT_LABELS = {
    "project": "项目版式",
    "company": "公司版式",
    "plain": "无项目/公司列",
}
PREVIEW_CACHE_SIZE = 8


def _money(cents: int | None) -> str:
    if cents is None:
        return ""
    value = db.cents_to_money(cents)
    return "" if value is None else f"{value:.2f}"


def _open_folder(path: Path) -> None:
    """Open a folder in the OS file manager (the desktop app targets Windows)."""
    if sys.platform != "win32":
        raise ReportError("当前系统不支持打开文件夹。")
    os.startfile(str(path))  # opens our own output directory


def _amount_cents(value: Any) -> int:
    text = "" if value is None else str(value).strip()
    if not text:
        raise ReportError("金额不能为空")
    cents = db.money_to_cents(reports.parse_amount(text))
    if cents is None:  # pragma: no cover - parse_amount already rejects empties
        raise ReportError("金额不能为空")
    return cents


def _invoice_amount(value: Any) -> int | None:
    """Invoice amount text -> integer cents; blank means "unknown"."""
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    try:
        amount = reports.parse_amount(text)
        return db.money_to_cents(amount)
    except ReportError:
        raise  # carries the readable "format / two decimals" message
    except ArithmeticError as exc:
        # Decimal quantize overflows on absurd exponents before the core check.
        raise invoices.InvoiceError("金额超出可处理范围") from exc


def _int_id(value: Any, label: str) -> int:
    """Accept ints and integer strings only; never truncate a number silently."""
    if isinstance(value, bool):
        raise ReportError(f"{label}必须是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ReportError(f"{label}必须是整数")
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ReportError(f"{label}必须是整数：{value}") from exc
    raise ReportError(f"{label}必须是整数：{value}")


def _invoice_ids(value: Any) -> list[int]:
    if not isinstance(value, (list, tuple)):
        raise ReportError("请先选择发票")
    ids: list[int] = []
    for item in value:
        number = _int_id(item, "发票编号")
        if number < 1:
            raise ReportError("发票编号必须 >= 1")
        if number not in ids:
            ids.append(number)
    if not ids:
        raise ReportError("请先选择发票")
    return ids


def _iso_date(value: Any, label: str) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    try:
        return reports.normalize_line_date(text)
    except ReportError as exc:
        raise ReportError(f"{label}格式应为 YYYY-MM-DD") from exc


def _batch_id(value: Any) -> int | None:
    if isinstance(value, bool):
        raise ReportError("批次编号必须是整数")
    if value is None or value == "":
        return None
    if isinstance(value, int) and value == 0:
        return None
    if isinstance(value, str) and value.strip() == "0":
        return None
    number = _int_id(value, "批次编号")
    if number < 1:
        raise ReportError("批次编号必须 >= 1")
    return number


def _report_dict(report: reports.Report) -> dict[str, Any]:
    return {
        "id": report.id,
        "company": report.company,
        "department": report.department,
        "reporter": report.reporter,
        "month": report.month,
        "title": report.title,
        "note": report.note,
        "created_at": report.created_at,
        "line_count": report.line_count,
        "amount_cents": report.amount_cents,
        "amount": _money(report.amount_cents),
    }


def _invoice_dict(row: workbench.InvoiceRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "batch_id": row.batch_id,
        "batch_label": row.batch_label,
        "source_name": row.source_name,
        "source_exists": row.source_exists,
        "page_index": row.page_index,
        "page_count": row.page_count,
        "type_label": row.type_label,
        "invoice_number": row.invoice_number,
        "invoice_date": row.invoice_date,
        "seller_name": row.seller_name,
        "buyer_name": row.buyer_name,
        "amount_cents": row.amount_total_cents,
        "amount": _money(row.amount_total_cents),
        "item_summary": row.item_summary,
        "category": row.category,
        "category_rule": row.category_rule,
        "confidence_notes": row.confidence_notes,
        "warning_text": row.warning_text,
        "needs_review": row.needs_review,
        "edit_count": row.edit_count,
        "linked": row.is_linked,
        "links": [
            {"report_id": link.report_id, "month": link.report_month}
            for link in row.links
        ],
    }


def _edit_dict(edit: invoices.InvoiceEdit) -> dict[str, Any]:
    return {
        "id": edit.id,
        "field": edit.field,
        "old_value": edit.old_value,
        "new_value": edit.new_value,
        "edited_at": edit.edited_at,
    }


def _invoice_state(
    connection: sqlite3.Connection, invoice_id: int
) -> dict[str, Any]:
    row = workbench.get_invoice_row(connection, invoice_id)
    if row is None:
        raise invoices.InvoiceError(f"发票不存在：#{invoice_id}")
    return {
        "invoice": _invoice_dict(row),
        "edits": [
            _edit_dict(edit)
            for edit in invoices.invoice_edits(connection, invoice_id)
        ],
    }


def _rule_dict(rule: learning.CategoryRule | None) -> dict[str, Any] | None:
    if rule is None:
        return None
    return {
        "id": rule.id,
        "match_type": rule.match_type,
        "pattern": rule.pattern,
        "category": rule.category,
        "confirm_count": rule.confirm_count,
        "hit_count": rule.hit_count,
        "enabled": rule.enabled,
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
    }


def _line_dict(
    line: reports.ReportLine,
    sequence: int,
    briefs: dict[int, workbench.InvoiceBrief],
) -> dict[str, Any]:
    invoices = []
    for invoice_id in line.invoice_ids:
        brief = briefs.get(invoice_id)
        if brief is None:  # pragma: no cover - links are foreign-key enforced
            continue
        invoices.append(
            {
                "id": brief.id,
                "invoice_date": brief.invoice_date,
                "invoice_number": brief.invoice_number,
                "seller_name": brief.seller_name,
                "amount_cents": brief.amount_total_cents,
                "amount": _money(brief.amount_total_cents),
                "category": brief.category,
            }
        )
    return {
        "id": line.id,
        "sequence": sequence,
        "line_date": line.line_date,
        "category": line.category,
        "description": line.description,
        "amount_cents": line.amount_cents,
        "amount": _money(line.amount_cents),
        "has_invoice": line.has_invoice,
        "invoice_note": line.invoice_note,
        "project_name": line.project_name,
        "source": line.source,
        "mileage_km": line.mileage_km,
        "invoice_ids": list(line.invoice_ids),
        "invoice_total_cents": line.invoice_total_cents,
        "invoice_total": _money(line.invoice_total_cents),
        "amount_mismatch": line.amount_mismatch,
        "invoices": invoices,
    }


def _date_range(payload: dict[str, Any]) -> tuple[str, str]:
    date_from = _iso_date(payload.get("date_from"), "开始日期")
    date_to = _iso_date(payload.get("date_to"), "结束日期")
    if date_from and date_to and date_from > date_to:
        raise ReportError("开始日期不能晚于结束日期")
    return date_from, date_to


def _export_filters(payload: dict[str, Any]) -> ExportFilters:
    date_from, date_to = _date_range(payload)
    return ExportFilters(
        batch_id=_batch_id(payload.get("batch_id")),
        buyer=str(payload.get("buyer") or "").strip(),
        date_from=date_from,
        date_to=date_to,
        include_review=bool(payload.get("include_review", True)),
    )


def _invoice_filters(payload: dict[str, Any]) -> dict[str, Any]:
    date_from, date_to = _date_range(payload)
    return {
        "batch_id": _batch_id(payload.get("batch_id")),
        "buyer": str(payload.get("buyer") or "").strip(),
        "date_from": date_from,
        "date_to": date_to,
        "include_review": bool(payload.get("include_review", True)),
        "unlinked_only": bool(payload.get("unlinked_only", False)),
        "search": str(payload.get("search") or "").strip(),
    }


def _endpoint(method):
    """Convert exceptions into a JSON envelope the UI can always read."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            # pywebview delivers bridge calls on worker threads. Serialize use
            # cases so a write cannot interleave with an export or each other.
            with self._lock:
                data = method(self, *args, **kwargs)
            return {"ok": True, "data": data}
        except (
            ReportError,
            workbench.PreviewError,
            invoices.InvoiceError,
            learning.RuleError,
            ExportError,
        ) as exc:
            return {"ok": False, "error": redact_paths(str(exc))}
        except (db.UnsafeDatabasePathError, db.DatabaseVersionError) as exc:
            return {"ok": False, "error": redact_paths(str(exc))}
        except (OSError, sqlite3.Error) as exc:
            print(
                f"[api] {method.__name__} {type(exc).__name__}:"
                f" {redact_paths(str(exc))}",
                file=sys.stderr,
            )
            return {"ok": False, "error": "文件或数据库操作失败。"}
        except Exception as exc:  # noqa: BLE001 - the bridge must not leak tracebacks
            print(
                f"[api] {method.__name__} {type(exc).__name__}:"
                f" {redact_paths(str(exc))}",
                file=sys.stderr,
            )
            return {"ok": False, "error": "操作失败，请查看控制台日志。"}

    return wrapper


class Api:
    """Object registered as ``js_api``; public methods are callable from JS."""

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        out_dir: str | Path | None = None,
        folder_dialog: Callable[[str], str | None] | None = None,
    ) -> None:
        self._db_path = Path(db_path) if db_path is not None else db.DEFAULT_DB_PATH
        self._out_dir = Path(out_dir) if out_dir is not None else Path("out")
        self._folder_dialog = folder_dialog
        self._preview_cache: dict[tuple[int, float], str] = {}
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        return db.connect(self._db_path)

    def _output_dir(self, name: str) -> Path:
        root = self._out_dir.expanduser().resolve()
        target = (root / name).resolve()
        if not target.is_relative_to(root):
            raise ReportError("输出目录不合法。")
        reason = output_path_reason(target)
        if reason is not None:
            raise ReportError(
                f"输出路径{reason}，财务数据只允许保存在本机固定盘或仓库的 out/、data/ 下。"
            )
        target.mkdir(parents=True, exist_ok=True)
        return target

    # ---------------------------------------------------------------- read

    @_endpoint
    def bootstrap(self) -> dict[str, Any]:
        today = date.today()
        with contextlib.closing(self._connect()) as connection:
            batches = db.batch_stats(connection)
            report_rows = reports.list_reports(connection)
        return {
            "version": __version__,
            "today": today.isoformat(),
            "default_month": today.strftime("%Y-%m"),
            "db_name": self._db_path.name,
            "categories": list(CATEGORIES),
            "default_category": DEFAULT_CATEGORY,
            "mileage_rate": str(reports.DEFAULT_MILEAGE_RATE),
            "layouts": [
                {"key": key, "label": LAYOUT_LABELS.get(key, key)} for key in LAYOUTS
            ],
            "batches": [
                {
                    "id": batch.id,
                    "label": batch.label,
                    "created_at": batch.created_at,
                    "invoice_count": batch.invoice_count,
                    "review_count": batch.review_count,
                    "file_count": batch.file_count,
                    "amount": f"{batch.amount_total:.2f}",
                }
                for batch in batches
            ],
            "reports": [_report_dict(report) for report in report_rows],
        }

    @_endpoint
    def list_invoices(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        with contextlib.closing(self._connect()) as connection:
            rows = workbench.list_invoices(
                connection, **_invoice_filters(filters or {})
            )
            summary = workbench.summarize_invoices(rows)
        return {
            "rows": [_invoice_dict(row) for row in rows],
            "summary": {
                "count": summary.count,
                "review_count": summary.review_count,
                "linked_count": summary.linked_count,
                "missing_amount_count": summary.missing_amount_count,
                "total_cents": summary.total_cents,
                "total": _money(summary.total_cents),
            },
        }

    @_endpoint
    def dashboard(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Aggregate the invoice pool for the summary dashboard."""
        with contextlib.closing(self._connect()) as connection:
            rows = workbench.list_invoices(
                connection, **_invoice_filters(filters or {})
            )
            summary = workbench.dashboard_summary(rows)
        return {
            "count": summary.count,
            "total_cents": summary.total_cents,
            "total": _money(summary.total_cents),
            "review_count": summary.review_count,
            "linked_count": summary.linked_count,
            "missing_amount_count": summary.missing_amount_count,
            "missing_source_count": summary.missing_source_count,
            "low_confidence_count": summary.low_confidence_count,
            "edited_count": summary.edited_count,
            "categories": [
                {
                    "category": item.category,
                    "count": item.count,
                    "total_cents": item.total_cents,
                    "total": _money(item.total_cents),
                }
                for item in summary.categories
            ],
            "months": [
                {
                    "month": item.month,
                    "count": item.count,
                    "total_cents": item.total_cents,
                    "total": _money(item.total_cents),
                }
                for item in summary.months
            ],
        }

    @_endpoint
    def list_reports(self) -> list[dict[str, Any]]:
        with contextlib.closing(self._connect()) as connection:
            return [_report_dict(report) for report in reports.list_reports(connection)]

    @_endpoint
    def get_report(self, report_id: int) -> dict[str, Any]:
        with contextlib.closing(self._connect()) as connection:
            report = reports.get_report(connection, int(report_id))
            lines = reports.report_lines(connection, int(report_id))
            all_ids = tuple(
                dict.fromkeys(
                    invoice_id for line in lines for invoice_id in line.invoice_ids
                )
            )
            briefs = workbench.invoice_briefs(connection, all_ids)
        return {
            "report": _report_dict(report),
            "lines": [
                _line_dict(line, sequence, briefs)
                for sequence, line in enumerate(lines, start=1)
            ],
        }

    @_endpoint
    def invoice_preview(self, invoice_id: int, zoom: float = 1.25) -> dict[str, Any]:
        invoice_number = int(invoice_id)
        scale = round(float(zoom), 2)
        cache_key = (invoice_number, scale)
        with contextlib.closing(self._connect()) as connection:
            source_path, page_index, page_count = workbench.invoice_source(
                connection, invoice_number
            )
        data_url = self._preview_cache.get(cache_key)
        if data_url is None:
            data_url = workbench.preview_data_url(
                source_path, page_index, zoom=scale
            )
            if len(self._preview_cache) >= PREVIEW_CACHE_SIZE:
                self._preview_cache.pop(next(iter(self._preview_cache)))
            self._preview_cache[cache_key] = data_url
        return {
            "data_url": data_url,
            "page_index": page_index,
            "page_count": page_count,
            "source_name": Path(source_path).name,
            "page_label": f"{page_index + 1}/{page_count or 1}",
        }

    @_endpoint
    def get_invoice(self, invoice_id: int) -> dict[str, Any]:
        identifier = _int_id(invoice_id, "发票编号")
        with contextlib.closing(self._connect()) as connection:
            return _invoice_state(connection, identifier)

    @_endpoint
    def list_rules(self) -> dict[str, Any]:
        with contextlib.closing(self._connect()) as connection:
            rules = learning.list_rules(connection)
            usage = learning.seller_usage(connection)
        entries = []
        for rule in rules:
            entry = _rule_dict(rule) or {}
            entry["invoice_count"] = usage.get(rule.pattern, 0)
            entries.append(entry)
        return {"rules": entries}

    # --------------------------------------------------------------- write

    @_endpoint
    def update_invoice(
        self, invoice_id: int, fields: dict[str, Any]
    ) -> dict[str, Any]:
        identifier = _int_id(invoice_id, "发票编号")
        if not isinstance(fields, dict):
            raise invoices.InvoiceError("修正内容格式不正确")
        payload = fields
        values: dict[str, Any] = {}
        text_fields = (
            "invoice_number",
            "invoice_date",
            "seller_name",
            "buyer_name",
            "category",
            "item_summary",
        )
        for name in text_fields:
            if name in payload:
                values[name] = str(payload.get(name) or "")
        if "amount" in payload:
            values["amount_total_cents"] = _invoice_amount(payload.get("amount"))
        if not values:
            raise invoices.InvoiceError("没有要修改的内容")
        with contextlib.closing(self._connect()) as connection:
            result = invoices.update_invoice(connection, identifier, **values)
            state = _invoice_state(connection, identifier)
        return {
            **state,
            "changed": len(result.changed),
            "rule": _rule_dict(result.rule),
            "conflict": result.conflict,
        }

    @_endpoint
    def confirm_invoice(self, invoice_id: int) -> dict[str, Any]:
        identifier = _int_id(invoice_id, "发票编号")
        with contextlib.closing(self._connect()) as connection:
            invoices.confirm_invoice(connection, identifier)
            return _invoice_state(connection, identifier)

    @_endpoint
    def set_rule_enabled(self, rule_id: int, enabled: bool) -> dict[str, Any]:
        identifier = _int_id(rule_id, "规则编号")
        if not isinstance(enabled, bool):
            raise learning.RuleError("启用状态必须是布尔值")
        with contextlib.closing(self._connect()) as connection:
            rule = learning.set_rule_enabled(connection, identifier, enabled)
        return {"rule": _rule_dict(rule)}

    @_endpoint
    def delete_rule(self, rule_id: int) -> dict[str, Any]:
        identifier = _int_id(rule_id, "规则编号")
        with contextlib.closing(self._connect()) as connection:
            learning.delete_rule(connection, identifier)
        return {"deleted": True}

    @_endpoint
    def preview_rule_backfill(self, rule_id: int) -> dict[str, Any]:
        identifier = _int_id(rule_id, "规则编号")
        with contextlib.closing(self._connect()) as connection:
            preview = learning.preview_backfill(connection, identifier)
        return {
            "rule": _rule_dict(preview.rule),
            "match_count": preview.match_count,
            "update_count": preview.update_count,
            "unchanged": preview.unchanged,
            "manual_skipped": preview.manual_skipped,
            "token": preview.token,
            "samples": [
                {
                    "id": sample.invoice_id,
                    "invoice_date": sample.invoice_date,
                    "seller_name": sample.seller_name,
                    "category": sample.category,
                    "amount_cents": sample.amount_total_cents,
                    "amount": _money(sample.amount_total_cents),
                }
                for sample in preview.samples
            ],
        }

    @_endpoint
    def apply_rule_backfill(self, rule_id: int, token: str) -> dict[str, Any]:
        identifier = _int_id(rule_id, "规则编号")
        if not isinstance(token, str) or not token.strip():
            raise learning.RuleError("缺少预览凭证，请先预览后再确认。")
        with contextlib.closing(self._connect()) as connection:
            result = learning.apply_backfill(connection, identifier, token)
        return {
            "rule": _rule_dict(result.rule),
            "updated": result.updated,
            "unchanged": result.unchanged,
            "manual_skipped": result.manual_skipped,
            "edited_at": result.edited_at,
        }

    @_endpoint
    def create_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = payload or {}
        with contextlib.closing(self._connect()) as connection:
            report_id = reports.create_report(
                connection,
                company=str(payload.get("company") or ""),
                reporter=str(payload.get("reporter") or ""),
                month=str(payload.get("month") or ""),
                department=str(payload.get("department") or ""),
                title=str(payload.get("title") or ""),
                note=str(payload.get("note") or ""),
            )
            report = reports.get_report(connection, report_id)
        return {"report_id": report_id, "report": _report_dict(report)}

    @_endpoint
    def update_report(self, report_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        fields = fields or {}
        allowed = ("company", "reporter", "month", "department", "title", "note")
        values = {key: str(fields[key]) for key in allowed if key in fields}
        if not values:
            raise ReportError("没有要修改的内容")
        with contextlib.closing(self._connect()) as connection:
            reports.update_report(connection, int(report_id), **values)
            report = reports.get_report(connection, int(report_id))
        return {"report": _report_dict(report)}

    @_endpoint
    def add_line(self, report_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        payload = payload or {}
        line_date = _iso_date(payload.get("line_date"), "日期")
        if not line_date:
            raise ReportError("日期不能为空")
        amount_cents = _amount_cents(payload.get("amount"))
        invoice_ids = payload.get("invoice_ids") or ()
        with contextlib.closing(self._connect()) as connection:
            line_id = reports.add_line(
                connection,
                int(report_id),
                line_date=line_date,
                category=str(payload.get("category") or DEFAULT_CATEGORY),
                description=str(payload.get("description") or ""),
                amount_cents=amount_cents,
                has_invoice=bool(payload.get("has_invoice", False)),
                invoice_note=str(payload.get("invoice_note") or ""),
                project_name=str(payload.get("project_name") or ""),
                invoice_ids=tuple(_invoice_ids(invoice_ids)) if invoice_ids else (),
            )
        return {"line_id": line_id, "amount_cents": amount_cents}

    @_endpoint
    def update_line(
        self, report_id: int, sequence: int, fields: dict[str, Any]
    ) -> dict[str, Any]:
        fields = fields or {}
        values: dict[str, Any] = {}
        if "line_date" in fields:
            values["line_date"] = _iso_date(fields.get("line_date"), "日期")
            if not values["line_date"]:
                raise ReportError("日期不能为空")
        if "category" in fields:
            values["category"] = str(fields.get("category") or "")
        if "description" in fields:
            values["description"] = str(fields.get("description") or "")
        if "amount" in fields:
            values["amount_cents"] = _amount_cents(fields.get("amount"))
        if "has_invoice" in fields:
            values["has_invoice"] = bool(fields.get("has_invoice"))
        if "invoice_note" in fields:
            values["invoice_note"] = str(fields.get("invoice_note") or "")
        if "project_name" in fields:
            values["project_name"] = str(fields.get("project_name") or "")
        if not values:
            raise ReportError("没有要修改的内容")
        with contextlib.closing(self._connect()) as connection:
            reports.update_line(connection, int(report_id), int(sequence), **values)
        return {"updated": True}

    @_endpoint
    def remove_line(self, report_id: int, sequence: int) -> dict[str, Any]:
        with contextlib.closing(self._connect()) as connection:
            reports.delete_line(connection, int(report_id), int(sequence))
        return {"removed": True}

    @_endpoint
    def add_mileage(self, report_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        payload = payload or {}
        line_date = _iso_date(payload.get("line_date"), "日期")
        if not line_date:
            raise ReportError("日期不能为空")
        km = reports.parse_km(str(payload.get("km") or ""))
        rate_text = str(payload.get("rate") or "").strip()
        rate = reports.parse_amount(rate_text) if rate_text else reports.DEFAULT_MILEAGE_RATE
        with contextlib.closing(self._connect()) as connection:
            line_id, amount_cents = reports.add_mileage(
                connection,
                int(report_id),
                line_date=line_date,
                km=km,
                description=str(payload.get("description") or ""),
                category=str(payload.get("category") or "汽车费用"),
                rate=rate,
                project_name=str(payload.get("project_name") or ""),
            )
        return {
            "line_id": line_id,
            "amount_cents": amount_cents,
            "amount": _money(amount_cents),
            "km": str(km),
        }

    @_endpoint
    def link_invoices(
        self, report_id: int, sequence: int, invoice_ids: list[int]
    ) -> dict[str, Any]:
        ids = _invoice_ids(invoice_ids)
        with contextlib.closing(self._connect()) as connection:
            added = reports.link_invoices(
                connection, int(report_id), int(sequence), ids
            )
        return {"added": added}

    @_endpoint
    def unlink_invoices(
        self, report_id: int, sequence: int, invoice_ids: list[int]
    ) -> dict[str, Any]:
        ids = _invoice_ids(invoice_ids)
        with contextlib.closing(self._connect()) as connection:
            removed = reports.unlink_invoices(
                connection, int(report_id), int(sequence), ids
            )
        return {"removed": removed}

    @_endpoint
    def add_lines_from_invoices(
        self,
        report_id: int,
        invoice_ids: list[int],
        project_name: str = "",
    ) -> dict[str, Any]:
        ids = _invoice_ids(invoice_ids)
        with contextlib.closing(self._connect()) as connection:
            result = reports.add_lines_from_invoices(
                connection, int(report_id), ids, project_name=project_name
            )
        return {
            "added": result.added,
            "skipped_linked": result.skipped_linked,
            "skipped_missing_amount": result.skipped_missing_amount,
            "skipped_missing_date": result.skipped_missing_date,
            "skipped": result.skipped,
        }

    # -------------------------------------------------------------- export

    @_endpoint
    def export_report(
        self,
        report_id: int,
        layout: str = "project",
        project_header: str = "",
    ) -> dict[str, Any]:
        out = self._output_dir("report")
        with contextlib.closing(self._connect()) as connection:
            report = reports.get_report(connection, int(report_id))
            lines = reports.report_lines(connection, int(report_id))
        if not lines:
            raise ReportError("报销单没有报销行，无法导出。")
        target = out / report_filename(report)
        temp = out / f".{target.name}.{uuid4().hex[:8]}.tmp"
        try:
            write_report_workbook(
                temp,
                report,
                lines,
                layout_key=layout,
                project_header=project_header,
            )
            if target.is_symlink():
                raise ReportError("导出目标是符号链接，已拒绝写入。")
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)
        return {
            "path": str(target),
            "line_count": len(lines),
            "amount": _money(report.amount_cents),
            "unlinked": sum(1 for line in lines if line.has_invoice and not line.invoice_ids),
            "mismatches": sum(1 for line in lines if line.amount_mismatch),
        }

    @_endpoint
    def export_evidence(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        out = self._output_dir("export")
        export_filters = _export_filters(filters or {})
        with contextlib.closing(self._connect()) as connection:
            rows: list[ExportRow] = collect_rows(connection, export_filters)
            summary = summarize_export(rows)
        if not rows:
            raise ReportError("没有符合条件的发票。")
        generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        excel_path = out / EXCEL_FILENAME
        print_path = out / PRINT_FILENAME
        stamp = uuid4().hex[:8]
        temp_excel = out / f".{EXCEL_FILENAME}.{stamp}.tmp"
        temp_print = out / f".{PRINT_FILENAME}.{stamp}.tmp"
        try:
            print_info = write_print_pdf(temp_print, rows)
            write_workbook(
                temp_excel,
                rows,
                summary,
                filters=export_filters,
                generated_at=generated_at,
                print_info=print_info,
            )
            if print_path.is_symlink() or excel_path.is_symlink():
                raise ExportError("导出目标是符号链接，已拒绝写入。")
            os.replace(temp_print, print_path)
            os.replace(temp_excel, excel_path)
        finally:
            for temp in (temp_excel, temp_print):
                if temp.exists():
                    temp.unlink(missing_ok=True)
        return {
            "excel_path": str(excel_path),
            "pdf_path": str(print_path),
            "count": summary.count,
            "review_count": summary.review_count,
            "total": _money(summary.total_cents),
            "notice_only": print_info.notice_only,
            "pages": print_info.pages,
        }

    @_endpoint
    def open_output_folder(self, kind: str = "export") -> dict[str, Any]:
        """Open ``out/<kind>`` so the user can find exported files afterwards."""
        if kind not in {"export", "report"}:
            raise ReportError("输出目录不合法。")
        target = self._output_dir(kind)
        _open_folder(target)
        return {"path": str(target)}

    # -------------------------------------------------------------- import

    @_endpoint
    def pick_folder(self, current: str = "") -> dict[str, Any]:
        """Let the user choose a folder with the native dialog.

        ``folder_dialog`` is injected by :mod:`app.main`: WinForms dialogs must
        run on the window's GUI thread, while pywebview delivers bridge calls
        on worker threads.
        """
        if self._folder_dialog is None:
            raise ReportError("当前环境不支持文件夹选择，请手动粘贴路径。")
        initial = str(current or "").strip()
        if len(initial) > 4096:
            initial = ""
        chosen = self._folder_dialog(initial)
        path = str(chosen or "").strip()
        return {"path": path or None}

    @_endpoint
    def import_folder(self, path: str, label: str = "") -> dict[str, Any]:
        source = Path(str(path)).expanduser()
        if not source.exists():
            raise ReportError(f"路径不存在：{source}")
        with contextlib.closing(self._connect()) as connection:
            summary = importer.import_folder(
                connection, source, label=(label.strip() or None)
            )
        return {
            "batch_id": summary.batch_id,
            "label": summary.label,
            "files_seen": summary.files_seen,
            "files_imported": summary.files_imported,
            "files_duplicate": summary.files_duplicate,
            "invoices_imported": summary.invoices_imported,
            "invoices_duplicate": summary.invoices_duplicate,
            "invoices_conflict": summary.invoices_conflict,
            "needs_review": summary.needs_review,
            "amount": f"{summary.amount_total:.2f}",
        }

