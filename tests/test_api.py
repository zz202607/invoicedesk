"""The pywebview JSON bridge: envelopes, report workflow, exports, preview."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import app.api as api_module
from app import __version__
from app.api import Api, _batch_id, _int_id
from app.core import appconfig, db
from app.core.models import InvoiceType
from app.core.paths import output_path_reason
from app.core.reports import ReportError
from tests.pdf_factory import full_digital_lines, make_pdf
from tests.report_factory import add_invoice

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def api(tmp_path):
    return Api(db_path=tmp_path / "workbench.db", out_dir=tmp_path / "out")


def _create_report(api: Api) -> int:
    response = api.create_report(
        {
            "company": "示例公司",
            "reporter": "张三",
            "month": "2026-07",
            "department": "研发部",
        }
    )
    assert response["ok"] is True, response
    return response["data"]["report_id"]


def _add_invoice(tmp_path: Path) -> int:
    connection = db.connect(tmp_path / "workbench.db")
    try:
        pdf = make_pdf(tmp_path, [(50, 50, "发票")], name="source.pdf")
        invoice_id = add_invoice(
            connection,
            index=1,
            number="API1",
            date="2026-07-09",
            seller="示例销售方",
            amount=Decimal("120.00"),
            source_path=str(pdf),
        )
    finally:
        connection.close()
    return invoice_id


def _add_review_invoice(tmp_path: Path) -> int:
    connection = db.connect(tmp_path / "workbench.db")
    try:
        pdf = make_pdf(tmp_path, [(50, 50, "发票")], name="review.pdf")
        invoice_id = add_invoice(
            connection,
            index=3,
            number="REVIEW1",
            date="2026-07-10",
            seller="待确认销售方",
            amount=Decimal("30.00"),
            source_path=str(pdf),
            review=True,
        )
    finally:
        connection.close()
    return invoice_id


def test_bootstrap_envelope(api: Api) -> None:
    response = api.bootstrap()
    assert response["ok"] is True
    data = response["data"]
    assert data["default_month"] == date.today().strftime("%Y-%m")
    assert len(data["categories"]) == 9
    assert data["default_category"] == "其他费用"
    assert data["mileage_rate"] == "1.5"
    assert [layout["key"] for layout in data["layouts"]] == ["project", "company", "plain"]
    assert data["reports"] == []
    assert data["db_name"] == "workbench.db"
    assert data["unrecognized_count"] == 0
    assert data["recent_import_paths"] == []
    assert data["report_choices"] == {
        "company": [],
        "department": [],
        "reporter": [],
    }
    assert "db_path" not in data


def test_bootstrap_reports_about_information(api: Api) -> None:
    """The About dialog shows the release, schema, and how the app is running."""
    data = api.bootstrap()["data"]
    assert data["version"] == __version__
    assert data["database_version"] == db.DB_VERSION
    assert data["frozen"] is False
    assert data["data_dir"] == str(api._db_path.resolve().parent)


def test_bootstrap_counts_unrecognized_invoices(api: Api, tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "workbench.db")
    try:
        add_invoice(
            connection,
            index=9,
            number="SCAN1",
            invoice_type=InvoiceType.SCANNED,
            amount=None,
        )
        add_invoice(connection, index=10, number="OK1")
    finally:
        connection.close()

    response = api.bootstrap()

    assert response["ok"] is True
    assert response["data"]["unrecognized_count"] == 1


def test_dashboard_envelope_and_filters(api: Api, tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "workbench.db")
    try:
        add_invoice(
            connection,
            index=1,
            number="DASH1",
            date="2026-07-01",
            seller="甲商家",
            category="餐饮费",
            amount=Decimal("10.00"),
        )
        add_invoice(
            connection,
            index=2,
            number="DASH2",
            date="2026-08-02",
            seller="乙商家",
            category="交通费",
            amount=None,
            review=True,
        )
        add_invoice(
            connection,
            index=3,
            number="DASH3",
            date="",
            seller="丙商家",
            category="餐饮费",
            amount=Decimal("5.00"),
        )
    finally:
        connection.close()

    response = api.dashboard()
    assert response["ok"] is True, response
    data = response["data"]
    assert data["count"] == 3
    assert data["total_cents"] == 1500
    assert data["total"] == "15.00"
    assert data["review_count"] == 2
    assert data["missing_amount_count"] == 1
    assert data["missing_source_count"] == 3
    assert data["linked_count"] == 0
    assert [
        (item["category"], item["count"], item["total"]) for item in data["categories"]
    ] == [("餐饮费", 2, "15.00"), ("交通费", 1, "0.00")]
    assert [item["month"] for item in data["months"]] == [
        "2026-07",
        "2026-08",
        "未知日期",
    ]

    filtered = api.dashboard({"include_review": False})
    assert filtered["ok"] is True
    assert filtered["data"]["count"] == 1
    assert filtered["data"]["review_count"] == 0

    searched = api.dashboard({"search": "甲商家"})
    assert searched["data"]["count"] == 1
    assert searched["data"]["total"] == "10.00"

    bad_range = api.dashboard({"date_from": "2026-09-01", "date_to": "2026-08-01"})
    assert bad_range["ok"] is False
    assert "不能晚于" in bad_range["error"]
    bad_date = api.dashboard({"date_from": "07/01/2026"})
    assert bad_date["ok"] is False
    assert "YYYY-MM-DD" in bad_date["error"]


def test_report_line_workflow(api: Api) -> None:
    report_id = _create_report(api)

    added = api.add_line(
        report_id,
        {
            "line_date": "2026-07-09",
            "category": "办公费用",
            "description": "测试费用",
            "amount": "120.00",
        },
    )
    assert added["ok"] is True, added
    assert added["data"]["amount_cents"] == 12000

    updated = api.update_line(
        report_id, 1, {"description": "改后的费用", "amount": "88.50"}
    )
    assert updated["ok"] is True, updated

    report = api.get_report(report_id)
    assert report["ok"] is True
    line = report["data"]["lines"][0]
    assert line["description"] == "改后的费用"
    assert line["amount"] == "88.50"
    assert report["data"]["report"]["amount"] == "88.50"

    removed = api.remove_line(report_id, 1)
    assert removed["ok"] is True
    assert api.get_report(report_id)["data"]["lines"] == []


def test_line_validation_errors(api: Api) -> None:
    report_id = _create_report(api)
    bad_amount = api.add_line(
        report_id,
        {"line_date": "2026-07-09", "amount": "12.345", "description": "x"},
    )
    assert bad_amount["ok"] is False
    assert "两位小数" in bad_amount["error"]

    bad_date = api.add_line(
        report_id, {"line_date": "07/09/2026", "amount": "1.00", "description": "x"}
    )
    assert bad_date["ok"] is False
    assert "YYYY-MM-DD" in bad_date["error"]

    missing = api.get_report(9999)
    assert missing["ok"] is False
    assert "不存在" in missing["error"]


def test_from_selected_invoices_and_links(api: Api, tmp_path) -> None:
    invoice_id = _add_invoice(tmp_path)
    report_id = _create_report(api)

    first = api.add_lines_from_invoices(report_id, [invoice_id], "测试项目")
    assert first["ok"] is True, first
    assert first["data"]["added"] == 1

    second = api.add_lines_from_invoices(report_id, [invoice_id])
    assert second["ok"] is True
    assert second["data"]["added"] == 0
    assert second["data"]["skipped"] == 1
    assert second["data"]["skipped_linked"] == 1
    assert second["data"]["skipped_missing_amount"] == 0
    assert second["data"]["skipped_missing_date"] == 0

    detail = api.get_report(report_id)["data"]
    line = detail["lines"][0]
    assert line["invoice_ids"] == [invoice_id]
    assert line["invoices"][0]["invoice_number"] == "API1"
    assert line["project_name"] == "测试项目"
    assert line["amount_mismatch"] is False

    empty = api.add_lines_from_invoices(report_id, [])
    assert empty["ok"] is False


def test_amount_formats_are_normalized(api: Api) -> None:
    report_id = _create_report(api)
    cases = [
        ("１５０００", 1500000),
        ("15,000.00", 1500000),
        ("-1000.00", -100000),
        ("￥100", 10000),
    ]
    for text, expected in cases:
        response = api.add_line(
            report_id,
            {"line_date": "2026-07-09", "amount": text, "description": text},
        )
        assert response["ok"] is True, response
        assert response["data"]["amount_cents"] == expected

    lines = api.get_report(report_id)["data"]["lines"]
    assert [line["amount_cents"] for line in lines] == [case[1] for case in cases]

    for bad in ("", "   ", "0.005"):
        response = api.add_line(
            report_id, {"line_date": "2026-07-09", "amount": bad, "description": "x"}
        )
        assert response["ok"] is False


@pytest.mark.parametrize("value", [True, False, 1.5, "1.0", None, Decimal("1.5"), ""])
def test_int_id_rejects_non_integer_values(value) -> None:
    with pytest.raises(ReportError):
        _int_id(value, "编号")


@pytest.mark.parametrize("value, expected", [(2, 2), (2.0, 2), (" 2 ", 2), ("-3", -3)])
def test_int_id_accepts_integer_values(value, expected) -> None:
    assert _int_id(value, "编号") == expected


@pytest.mark.parametrize("value", [None, "", 0, "0", " 0 "])
def test_batch_id_empty_values_mean_all(value) -> None:
    assert _batch_id(value) is None


@pytest.mark.parametrize("value", [False, True, -1, 0.0, 1.5, "abc"])
def test_batch_id_rejects_invalid_values(value) -> None:
    with pytest.raises(ReportError):
        _batch_id(value)


def test_output_path_reason(tmp_path) -> None:
    assert output_path_reason(tmp_path) is None
    assert output_path_reason(ROOT / "app") == "在仓库内非忽略目录"
    if sys.platform == "win32":
        assert output_path_reason(Path(r"\\server\share\out")) == "位于网络共享路径"


def test_concurrent_writes_are_serialized(api: Api, capsys) -> None:
    report_id = _create_report(api)
    barrier = threading.Barrier(10)

    def add_line(index: int):
        barrier.wait(timeout=5)
        return api.add_line(
            report_id,
            {
                "line_date": "2026-07-09",
                "amount": "1.00",
                "description": f"并发行 {index}",
            },
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(add_line, range(10)))

    assert all(result["ok"] for result in results), results
    lines = api.get_report(report_id)["data"]["lines"]
    assert len(lines) == 10
    assert sum(line["amount_cents"] for line in lines) == 1000
    assert "database is locked" not in capsys.readouterr().err


def test_empty_database_contract(tmp_path) -> None:
    api = Api(db_path=tmp_path / "empty.db", out_dir=tmp_path / "out")
    boot = api.bootstrap()
    assert boot["ok"] is True
    assert boot["data"]["batches"] == []
    assert boot["data"]["reports"] == []

    listing = api.list_invoices({})
    assert listing["ok"] is True
    assert listing["data"]["rows"] == []
    assert listing["data"]["summary"] == {
        "count": 0,
        "review_count": 0,
        "linked_count": 0,
        "missing_amount_count": 0,
        "total_cents": 0,
        "total": "0.00",
    }

    missing = api.get_report(123)
    assert missing["ok"] is False
    assert "不存在" in missing["error"]
    assert str(tmp_path) not in missing["error"]


def test_sqlite_errors_use_fixed_message(api: Api, monkeypatch, capsys) -> None:
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError(r"C:\Users\secret\pool.db")

    monkeypatch.setattr(api_module.workbench, "list_invoices", fail)
    response = api.list_invoices({})
    assert response["ok"] is False
    assert response["error"] == "文件或数据库操作失败。"
    logged = capsys.readouterr().err
    assert "secret" not in logged
    assert "<路径已隐藏>" in logged


def test_bootstrap_reports_database_version_error(tmp_path) -> None:
    path = tmp_path / "future.db"
    connection = db.connect(path)
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()
    response = Api(db_path=path, out_dir=tmp_path / "out").bootstrap()
    assert response["ok"] is False
    assert "版本" in response["error"]


def test_id_inputs_reject_booleans_and_fractions(api: Api, tmp_path) -> None:
    invoice_id = _add_invoice(tmp_path)
    report_id = _create_report(api)

    for bad in ([True], [2.7], [invoice_id, 3.9]):
        response = api.add_lines_from_invoices(report_id, bad)
        assert response["ok"] is False
        assert "整数" in response["error"]

    batch = api.list_invoices({"batch_id": True})
    assert batch["ok"] is False
    assert "整数" in batch["error"]

    fractional_batch = api.list_invoices({"batch_id": 1.5})
    assert fractional_batch["ok"] is False


def test_output_root_is_created_and_refuses_unsafe_locations(
    api: Api, monkeypatch
) -> None:
    root = api._out_dir.expanduser().resolve()
    assert api._output_root() == root
    assert root.is_dir()
    monkeypatch.setattr(
        api_module, "output_path_reason", lambda path: "在仓库内非忽略目录"
    )
    with pytest.raises(ReportError, match="输出路径"):
        api._output_root()


def test_error_envelope_hides_local_paths(api: Api, monkeypatch, capsys) -> None:
    def fail(*args, **kwargs):
        raise OSError(r"C:\Users\secret\invoices\a.pdf")

    monkeypatch.setattr(api_module.workbench, "list_invoices", fail)
    response = api.list_invoices({})
    assert response["ok"] is False
    assert response["error"] == "文件或数据库操作失败。"
    assert "secret" not in response["error"]

    logged = capsys.readouterr().err
    assert "secret" not in logged
    assert "[api] list_invoices" in logged
    assert "<路径已隐藏>" in logged

    def explode(*args, **kwargs):
        raise RuntimeError(r"C:\Users\secret\boom.db")

    monkeypatch.setattr(api_module.workbench, "list_invoices", explode)
    response = api.list_invoices({})
    assert response["ok"] is False
    assert response["error"] == "操作失败，请查看控制台日志。"
    assert "secret" not in capsys.readouterr().err


def test_mileage_line(api: Api) -> None:
    report_id = _create_report(api)
    response = api.add_mileage(
        report_id,
        {
            "line_date": "2026-07-10",
            "km": "12.4",
            "description": "拜访客户",
            "project_name": "项目A",
        },
    )
    assert response["ok"] is True, response
    assert response["data"]["amount_cents"] == 1860

    invalid = api.add_mileage(
        report_id, {"line_date": "2026-07-10", "km": "-1", "description": "x"}
    )
    assert invalid["ok"] is False


def test_export_report(api: Api, tmp_path) -> None:
    report_id = _create_report(api)
    api.add_line(
        report_id,
        {"line_date": "2026-07-09", "amount": "120.00", "description": "办公用品"},
    )
    response = api.export_report(report_id, "project")
    assert response["ok"] is True, response
    target = Path(response["data"]["path"])
    assert target.is_file()
    assert target.name == "示例公司费用报销支出明细表（张三）2607.xlsx"
    assert response["data"]["line_count"] == 1
    assert response["data"]["unlinked"] == 0

    empty_id = _create_report(api)
    empty = api.export_report(empty_id)
    assert empty["ok"] is False
    assert "没有报销行" in empty["error"]


def test_export_report_refuses_repository_output(tmp_path) -> None:
    unsafe = Api(db_path=tmp_path / "workbench.db", out_dir=ROOT / "app")
    report_id = _create_report(unsafe)
    unsafe.add_line(
        report_id,
        {"line_date": "2026-07-09", "amount": "1.00", "description": "x"},
    )
    response = unsafe.export_report(report_id)
    assert response["ok"] is False
    assert "仓库内非忽略目录" in response["error"]


def test_export_evidence(api: Api, tmp_path) -> None:
    _add_invoice(tmp_path)
    response = api.export_evidence({})
    assert response["ok"] is True, response
    data = response["data"]
    assert Path(data["excel_path"]).is_file()
    assert Path(data["pdf_path"]).is_file()
    assert data["count"] == 1
    assert data["total"] == "120.00"
    assert data["group_count"] == 1
    group = data["groups"][0]
    assert (group["company"], group["category"]) == ("示例公司", "办公费用")
    assert group["count"] == 1
    assert group["total"] == "120.00"
    assert group["source_pages"] == 1
    assert group["pages"] == 1
    assert Path(group["path"]).is_file()
    assert Path(group["path"]).name == "办公费用-120.00元.pdf"
    assert Path(group["path"]).parent.parent == Path(data["group_dir"])
    merged = data["merged"][0]
    assert merged["company"] == "示例公司"
    assert merged["count"] == 1
    assert merged["total"] == "120.00"
    assert Path(merged["path"]).is_file()
    assert Path(merged["path"]).name == "总合并-120.00元.pdf"

    grouped = api.export_evidence({"buyer": "示例公司"}, company_hint="示例")
    assert grouped["ok"] is True, grouped
    assert grouped["data"]["groups"][0]["path"] == group["path"]

    none = api.export_evidence({"date_from": "2027-01-01"})
    assert none["ok"] is False
    assert "没有符合条件" in none["error"]


def test_open_output_folder(api: Api, monkeypatch) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(api_module, "_open_folder", opened.append)

    response = api.open_output_folder()
    assert response["ok"] is True, response
    root = api._out_dir.expanduser().resolve()
    assert Path(response["data"]["path"]) == root
    assert opened == [root]

    report = api.open_output_folder("report")
    assert report["ok"] is True, report
    assert opened[-1] == root

    invalid = api.open_output_folder("../../escape")
    assert invalid["ok"] is False
    assert "不合法" in invalid["error"]
    assert len(opened) == 2


def test_open_output_folder_reports_failure(api: Api, monkeypatch) -> None:
    def fail(path: Path) -> None:
        raise OSError("no file manager")

    monkeypatch.setattr(api_module, "_open_folder", fail)
    response = api.open_output_folder("export")
    assert response["ok"] is False
    assert response["error"] == "文件或数据库操作失败。"


def test_invoice_listing_and_preview(api: Api, tmp_path) -> None:
    invoice_id = _add_invoice(tmp_path)
    listing = api.list_invoices({"unlinked_only": True, "search": "示例"})
    assert listing["ok"] is True, listing
    assert listing["data"]["summary"]["count"] == 1
    assert listing["data"]["rows"][0]["invoice_number"] == "API1"

    preview = api.invoice_preview(invoice_id)
    assert preview["ok"] is True, preview
    assert preview["data"]["data_url"].startswith("data:image/png;base64,")
    assert preview["data"]["page_label"] == "1/1"

    cached = api.invoice_preview(invoice_id)
    assert cached["data"]["data_url"] == preview["data"]["data_url"]


def test_invoice_correction_workflow(api: Api, tmp_path) -> None:
    invoice_id = _add_invoice(tmp_path)
    source = tmp_path / "source.pdf"
    before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    before_mtime = source.stat().st_mtime_ns

    detail = api.get_invoice(invoice_id)
    assert detail["ok"] is True, detail
    assert detail["data"]["edits"] == []
    assert detail["data"]["invoice"]["edit_count"] == 0

    updated = api.update_invoice(
        invoice_id,
        {"seller_name": "改后销售方", "amount": "88.50", "category": "餐饮费"},
    )
    assert updated["ok"] is True, updated
    data = updated["data"]
    assert data["changed"] == 3
    assert data["invoice"]["seller_name"] == "改后销售方"
    assert data["invoice"]["amount"] == "88.50"
    assert data["invoice"]["category"] == "餐饮费"
    assert data["invoice"]["category_rule"] == "manual"
    assert data["invoice"]["edit_count"] == 3
    assert data["invoice"]["needs_review"] is False
    assert data["conflict"] is False
    assert data["rule"]["pattern"] == "改后销售方"
    assert data["rule"]["category"] == "餐饮费"
    assert {edit["field"] for edit in data["edits"]} == {
        "seller_name",
        "amount_total_cents",
        "category",
    }

    listing = api.list_invoices({"search": "改后销售方"})
    row = listing["data"]["rows"][0]
    assert row["edit_count"] == 3
    assert row["category"] == "餐饮费"

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before_hash
    assert source.stat().st_mtime_ns == before_mtime


def test_invoice_edit_reports_number_conflict(api: Api, tmp_path) -> None:
    first = _add_invoice(tmp_path)
    connection = db.connect(tmp_path / "workbench.db")
    try:
        second = add_invoice(
            connection,
            index=2,
            number="API2",
            date="2026-07-09",
            seller="示例销售方",
            amount=Decimal("120.00"),
            source_path=str(tmp_path / "source.pdf"),
        )
    finally:
        connection.close()

    response = api.update_invoice(second, {"invoice_number": "API1"})
    assert response["ok"] is True, response
    data = response["data"]
    assert data["conflict"] is True
    assert data["invoice"]["needs_review"] is True
    assert "重复" in data["invoice"]["warning_text"]
    assert data["invoice"]["edit_count"] == 1

    first_response = api.get_invoice(first)
    assert first_response["ok"] is True
    assert "重复" in first_response["data"]["invoice"]["warning_text"]
    assert first_response["data"]["invoice"]["needs_review"] is True
    assert "warning" in {edit["field"] for edit in first_response["data"]["edits"]}


def test_confirmed_peer_returns_to_review_when_a_new_conflict_arrives(
    api: Api, tmp_path
) -> None:
    first = _add_invoice(tmp_path)
    connection = db.connect(tmp_path / "workbench.db")
    try:
        second = add_invoice(
            connection,
            index=2,
            number="API2",
            date="2026-07-09",
            seller="示例销售方",
            amount=Decimal("120.00"),
            source_path=str(tmp_path / "source.pdf"),
        )
    finally:
        connection.close()

    duplicate = api.update_invoice(second, {"invoice_number": "API1"})
    assert duplicate["data"]["invoice"]["needs_review"] is True
    assert api.confirm_invoice(first)["data"]["invoice"]["needs_review"] is False

    conflict = api.update_invoice(second, {"amount": "130.00"})
    assert conflict["data"]["conflict"] is True
    assert api.get_invoice(first)["data"]["invoice"]["needs_review"] is True

    assert api.confirm_invoice(first)["data"]["invoice"]["needs_review"] is False
    again = api.update_invoice(second, {"amount": "140.00"})
    assert again["data"]["conflict"] is True
    first_final = api.get_invoice(first)
    assert first_final["data"]["invoice"]["needs_review"] is True
    review_edits = [
        edit for edit in first_final["data"]["edits"] if edit["field"] == "review"
    ]
    assert any(
        edit["old_value"] == "已确认" and edit["new_value"] == "待确认"
        for edit in review_edits
    )


def test_invoice_same_amount_in_another_format_is_a_noop(api: Api, tmp_path) -> None:
    invoice_id = _add_invoice(tmp_path)
    response = api.update_invoice(invoice_id, {"amount": "120"})
    assert response["ok"] is True, response
    assert response["data"]["changed"] == 0
    assert response["data"]["invoice"]["amount"] == "120.00"
    assert response["data"]["edits"] == []


def test_invoice_amount_parsing_errors_use_a_readable_message(
    api: Api, tmp_path
) -> None:
    invoice_id = _add_invoice(tmp_path)
    for value in ("NaN", "Infinity", "1e100000", "9" * 5000):
        response = api.update_invoice(invoice_id, {"amount": value})
        assert response["ok"] is False, (value, response)
        assert "金额" in response["error"]
    assert api.get_invoice(invoice_id)["data"]["edits"] == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2026-08-09", "2026-08-09"), ("2026-8-9", "2026-08-09")],
)
def test_invoice_dates_normalize_through_the_api(
    api: Api, tmp_path, value: str, expected: str
) -> None:
    invoice_id = _add_invoice(tmp_path)
    response = api.update_invoice(invoice_id, {"invoice_date": value})
    assert response["ok"] is True, response
    assert response["data"]["invoice"]["invoice_date"] == expected


@pytest.mark.parametrize(
    "value", ["2026-02-30", "2026-13-01", "1989-12-31", "2101-01-01"]
)
def test_invalid_invoice_dates_are_rejected_through_the_api(
    api: Api, tmp_path, value: str
) -> None:
    invoice_id = _add_invoice(tmp_path)
    response = api.update_invoice(invoice_id, {"invoice_date": value})
    assert response["ok"] is False, (value, response)
    assert "日期" in response["error"]


def test_invoice_amount_can_be_cleared(api: Api, tmp_path) -> None:
    invoice_id = _add_invoice(tmp_path)

    cleared = api.update_invoice(invoice_id, {"amount": ""})
    assert cleared["ok"] is True, cleared
    assert cleared["data"]["invoice"]["amount_cents"] is None
    assert cleared["data"]["invoice"]["amount"] == ""
    assert cleared["data"]["invoice"]["needs_review"] is True
    assert cleared["data"]["edits"][0]["new_value"] == ""
    assert cleared["data"]["edits"][0]["old_value"] == "120.00"

    assert api.update_invoice(invoice_id, {"amount": "100.00"})["ok"] is True
    null_amount = api.update_invoice(invoice_id, {"amount": None})
    assert null_amount["ok"] is True, null_amount
    assert null_amount["data"]["invoice"]["amount_cents"] is None

    assert api.update_invoice(invoice_id, {"amount": "100.00"})["ok"] is True
    blank = api.update_invoice(invoice_id, {"amount": "   "})
    assert blank["ok"] is True, blank
    assert blank["data"]["invoice"]["amount_cents"] is None


def test_invoice_amount_boundaries(api: Api, tmp_path) -> None:
    invoice_id = _add_invoice(tmp_path)

    boundary = api.update_invoice(invoice_id, {"amount": "10000000000000.00"})
    assert boundary["ok"] is True, boundary
    assert boundary["data"]["invoice"]["amount_cents"] == 10**15

    too_big = api.update_invoice(invoice_id, {"amount": "10000000000000.01"})
    assert too_big["ok"] is False
    assert "超出可处理范围" in too_big["error"]

    negative = api.update_invoice(invoice_id, {"amount": "-100.00"})
    assert negative["ok"] is True, negative
    assert negative["data"]["invoice"]["amount_cents"] == -10000

    too_small = api.update_invoice(invoice_id, {"amount": "-10000000000000.01"})
    assert too_small["ok"] is False
    assert "超出可处理范围" in too_small["error"]


def test_invoice_negative_amount_through_the_api(api: Api, tmp_path) -> None:
    invoice_id = _add_invoice(tmp_path)
    response = api.update_invoice(invoice_id, {"amount": "-88.50"})
    assert response["ok"] is True, response
    invoice = response["data"]["invoice"]
    assert invoice["amount_cents"] == -8850
    assert invoice["amount"] == "-88.50"
    edit = response["data"]["edits"][0]
    assert (edit["old_value"], edit["new_value"]) == ("120.00", "-88.50")

    connection = db.connect(tmp_path / "workbench.db")
    try:
        confidence = json.loads(
            connection.execute(
                "SELECT confidence FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()[0]
        )
    finally:
        connection.close()
    assert confidence["amount_total"] == "high"


def test_invoice_edit_audits_only_changed_fields(api: Api, tmp_path) -> None:
    invoice_id = _add_invoice(tmp_path)
    response = api.update_invoice(
        invoice_id,
        {
            "seller_name": "示例销售方",
            "amount": "88.50",
            "category": "办公费用",
        },
    )
    assert response["ok"] is True, response
    data = response["data"]
    assert data["changed"] == 1
    assert data["invoice"]["edit_count"] == 1
    assert [edit["field"] for edit in data["edits"]] == ["amount_total_cents"]


def test_invoice_edit_without_seller_learns_no_rule(api: Api, tmp_path) -> None:
    connection = db.connect(tmp_path / "workbench.db")
    try:
        pdf = make_pdf(tmp_path, [(50, 50, "发票")], name="no-seller.pdf")
        invoice_id = add_invoice(
            connection,
            index=5,
            number="NOS1",
            seller="",
            source_path=str(pdf),
            review=True,
        )
    finally:
        connection.close()

    response = api.update_invoice(invoice_id, {"category": "餐饮费"})
    assert response["ok"] is True, response
    assert response["data"]["rule"] is None

    connection = db.connect(tmp_path / "workbench.db")
    try:
        assert (
            connection.execute("SELECT COUNT(*) FROM category_rules").fetchone()[0] == 0
        )
    finally:
        connection.close()

    confirmed = api.confirm_invoice(invoice_id)
    assert confirmed["ok"] is True, confirmed
    assert confirmed["data"]["invoice"]["needs_review"] is False


def test_invoice_edit_reports_conflict_and_incomplete_branches(
    api: Api, tmp_path
) -> None:
    _add_invoice(tmp_path)
    connection = db.connect(tmp_path / "workbench.db")
    try:
        second = add_invoice(
            connection,
            index=2,
            number="API2",
            date="2026-08-08",
            seller="示例销售方",
            amount=Decimal("120.00"),
            source_path=str(tmp_path / "source.pdf"),
        )
    finally:
        connection.close()

    conflict = api.update_invoice(second, {"invoice_number": "API1"})
    assert conflict["ok"] is True, conflict
    assert conflict["data"]["conflict"] is True
    assert conflict["data"]["invoice"]["needs_review"] is True
    assert "不一致" in conflict["data"]["invoice"]["warning_text"]

    incomplete = api.update_invoice(second, {"invoice_date": ""})
    assert incomplete["ok"] is True, incomplete
    assert incomplete["data"]["conflict"] is True
    assert incomplete["data"]["invoice"]["needs_review"] is True
    assert "无法确认" in incomplete["data"]["invoice"]["warning_text"]


def test_invoice_confirmation_clears_review_and_audits(api: Api, tmp_path) -> None:
    invoice_id = _add_review_invoice(tmp_path)

    updated = api.update_invoice(invoice_id, {"category": "餐饮费"})
    assert updated["ok"] is True, updated
    assert updated["data"]["invoice"]["needs_review"] is True

    confirmed = api.confirm_invoice(invoice_id)
    assert confirmed["ok"] is True, confirmed
    assert confirmed["data"]["invoice"]["needs_review"] is False
    assert confirmed["data"]["invoice"]["edit_count"] == 1
    assert confirmed["data"]["edits"][0]["field"] == "review"
    assert confirmed["data"]["edits"][0]["new_value"] == "已确认"
    assert len(confirmed["data"]["edits"]) == 2

    again = api.confirm_invoice(invoice_id)
    assert again["ok"] is False
    assert "不需要确认" in again["error"]

    missing = api.confirm_invoice(999)
    assert missing["ok"] is False
    assert "发票不存在" in missing["error"]


def test_review_queue_filter_and_reopen(api: Api, tmp_path) -> None:
    invoice_id = _add_review_invoice(tmp_path)
    _add_invoice(tmp_path)  # clean record; must stay out of the queue

    queue = api.list_invoices({"review_only": True})
    assert queue["ok"] is True, queue
    assert [row["id"] for row in queue["data"]["rows"]] == [invoice_id]
    assert queue["data"]["summary"]["review_count"] == 1

    assert api.confirm_invoice(invoice_id)["ok"] is True
    assert api.list_invoices({"review_only": True})["data"]["rows"] == []

    reopened = api.reopen_invoice(invoice_id)
    assert reopened["ok"] is True, reopened
    assert reopened["data"]["invoice"]["needs_review"] is True
    assert reopened["data"]["edits"][0]["field"] == "review"
    assert reopened["data"]["edits"][0]["old_value"] == "已确认"
    assert reopened["data"]["edits"][0]["new_value"] == "待确认"

    again = api.reopen_invoice(invoice_id)
    assert again["ok"] is False
    assert "已经在待确认队列" in again["error"]

    missing = api.reopen_invoice(999)
    assert missing["ok"] is False
    assert "发票不存在" in missing["error"]


def test_invoice_correction_validation(api: Api, tmp_path) -> None:
    invoice_id = _add_invoice(tmp_path)

    bad_date = api.update_invoice(invoice_id, {"invoice_date": "2026/07/09"})
    assert bad_date["ok"] is False
    assert "日期格式" in bad_date["error"]

    bad_amount = api.update_invoice(invoice_id, {"amount": "12.345"})
    assert bad_amount["ok"] is False
    assert "两位小数" in bad_amount["error"]

    empty_category = api.update_invoice(invoice_id, {"category": "  "})
    assert empty_category["ok"] is False
    assert "分类不能为空" in empty_category["error"]

    no_fields = api.update_invoice(invoice_id, {})
    assert no_fields["ok"] is False
    assert "没有要修改的内容" in no_fields["error"]

    unchanged = api.update_invoice(invoice_id, {"seller_name": "示例销售方"})
    assert unchanged["ok"] is True, unchanged
    assert unchanged["data"]["changed"] == 0
    assert unchanged["data"]["edits"] == []

    missing = api.update_invoice(999, {"seller_name": "x"})
    assert missing["ok"] is False
    assert "发票不存在" in missing["error"]

    bad_payload = api.update_invoice(invoice_id, ["seller_name"])
    assert bad_payload["ok"] is False
    assert "格式不正确" in bad_payload["error"]

    bad_id = api.get_invoice(True)
    assert bad_id["ok"] is False
    assert "整数" in bad_id["error"]


def test_preview_reports_missing_source_without_path(api: Api, tmp_path) -> None:
    connection = db.connect(tmp_path / "workbench.db")
    try:
        invoice_id = add_invoice(
            connection,
            index=2,
            number="MISSING1",
            source_path=str(tmp_path / "gone.pdf"),
        )
    finally:
        connection.close()
    response = api.invoice_preview(invoice_id)
    assert response["ok"] is False
    assert "源文件不存在" in response["error"]
    assert str(tmp_path) not in response["error"]


def test_preview_and_export_leave_source_untouched(api: Api, tmp_path) -> None:
    invoice_id = _add_invoice(tmp_path)
    source = tmp_path / "source.pdf"
    before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    before_mtime = source.stat().st_mtime_ns

    assert api.invoice_preview(invoice_id)["ok"] is True
    assert api.export_evidence({})["ok"] is True

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before_hash
    assert source.stat().st_mtime_ns == before_mtime


def test_business_error_messages_are_redacted(api: Api, tmp_path, monkeypatch) -> None:
    _add_invoice(tmp_path)

    def fail(*args, **kwargs):
        raise api_module.ExportError(r"无法写入 C:\Users\secret\out.pdf")

    monkeypatch.setattr(api_module, "write_print_pdf", fail)
    response = api.export_evidence({})
    assert response["ok"] is False
    assert "secret" not in response["error"]
    assert "<路径已隐藏>" in response["error"]


def test_import_errors(api: Api, tmp_path) -> None:
    missing = api.import_folder(str(tmp_path / "nope"))
    assert missing["ok"] is False
    assert "不存在" in missing["error"]


def test_import_folder_reports_auto_reparse_fields(api: Api, tmp_path) -> None:
    folder = tmp_path / "october"
    folder.mkdir()
    make_pdf(folder, full_digital_lines(), name="a.pdf")

    response = api.import_folder(str(folder))

    assert response["ok"] is True, response
    data = response["data"]
    assert data["invoices_imported"] == 1
    assert data["reparse_candidates"] == 0
    assert data["reparsed"] == 0
    assert data["reparse_failed"] == 0


def _wait_import(api: Api, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = api.import_status(job_id)["data"]
        if state["status"] != "running":
            return state
        time.sleep(0.02)
    raise AssertionError("import job did not finish in time")


def test_import_preflight_and_background_job(api: Api, tmp_path: Path) -> None:
    folder = tmp_path / "october"
    folder.mkdir()
    make_pdf(folder, full_digital_lines(), name="a.pdf")

    planned = api.import_preflight(str(folder))
    assert planned["ok"] is True, planned
    plan = planned["data"]
    assert plan["label"] == "october"
    assert plan["new_files"] == 1
    assert plan["duplicate_files"] == 0
    assert plan["ocr_pages"] == 0
    assert plan["reparse_candidates"] == 0
    # Preflight reads files but writes nothing.
    connection = db.connect(tmp_path / "workbench.db")
    try:
        assert connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    finally:
        connection.close()

    started = api.import_start("", "", plan["token"])
    assert started["ok"] is True, started
    state = _wait_import(api, started["data"]["job_id"])
    assert state["status"] == "done", state
    assert state["summary"]["invoices_imported"] == 1
    assert state["summary"]["cancelled"] is False
    assert api.import_status("stale-job")["data"]["status"] == "idle"

    # The plan token is single use; without a path the start must ask for a
    # fresh preflight instead of silently importing the cached folder again.
    again = api.import_start("", "", plan["token"])
    assert again["ok"] is False
    assert "预检" in again["error"]


def test_import_cancel_rolls_back_and_is_reported(
    api: Api, tmp_path: Path, monkeypatch
) -> None:
    folder = tmp_path / "october"
    folder.mkdir()
    make_pdf(folder, full_digital_lines(), name="a.pdf")

    def slow_execute(connection, plan, *, progress=None, cancel=None):
        while not cancel.is_set():
            time.sleep(0.01)
        raise api_module.importer.ImportCancelled("cancelled")

    monkeypatch.setattr(api_module.importer, "execute_plan", slow_execute)

    started = api.import_start(str(folder))
    assert started["ok"] is True, started
    job_id = started["data"]["job_id"]

    second = api.import_start(str(folder))
    assert second["ok"] is False
    assert "正在进行" in second["error"]

    cancelled = api.cancel_import(job_id)
    assert cancelled["data"]["cancelling"] is True
    state = _wait_import(api, job_id, timeout=5)
    assert state["status"] == "cancelled"

    connection = db.connect(tmp_path / "workbench.db")
    try:
        assert connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0
    finally:
        connection.close()


def _learn_rule(api: Api, invoice_id: int, category: str = "餐饮费") -> dict:
    response = api.update_invoice(invoice_id, {"category": category})
    assert response["ok"] is True, response
    rule = response["data"]["rule"]
    assert rule is not None
    return rule


def test_rule_management_endpoints(api: Api, tmp_path) -> None:
    first = _add_invoice(tmp_path)
    rule = _learn_rule(api, first)
    assert rule["enabled"] is True

    # A second invoice for the same seller starts in the old category, so the
    # backfill has a real candidate.
    connection = db.connect(tmp_path / "workbench.db")
    try:
        second = add_invoice(
            connection,
            index=2,
            number="API2",
            date="2026-07-11",
            seller="示例销售方",
            amount=Decimal("80.00"),
            source_path=str(tmp_path / "source.pdf"),
            category="办公费用",
            category_rule="item.office",
        )
    finally:
        connection.close()

    listed = api.list_rules()
    assert listed["ok"] is True, listed
    entry = next(item for item in listed["data"]["rules"] if item["id"] == rule["id"])
    assert entry["invoice_count"] == 2

    disabled = api.set_rule_enabled(rule["id"], False)
    assert disabled["ok"] is True, disabled
    assert disabled["data"]["rule"]["enabled"] is False
    assert api.set_rule_enabled(rule["id"], "false")["ok"] is False
    assert api.preview_rule_backfill(rule["id"])["ok"] is False

    assert api.set_rule_enabled(rule["id"], True)["ok"] is True
    preview = api.preview_rule_backfill(rule["id"])
    assert preview["ok"] is True, preview
    assert preview["data"]["update_count"] == 1
    assert [sample["id"] for sample in preview["data"]["samples"]] == [second]
    assert preview["data"]["samples"][0]["category"] == "办公费用"
    assert preview["data"]["samples"][0]["amount"] == "80.00"
    assert api.apply_rule_backfill(rule["id"], "")["ok"] is False
    assert api.apply_rule_backfill(rule["id"], "stale-token")["ok"] is False

    applied = api.apply_rule_backfill(rule["id"], preview["data"]["token"])
    assert applied["ok"] is True, applied
    assert applied["data"]["updated"] == 1

    state = api.get_invoice(second)["data"]
    assert state["invoice"]["category"] == "餐饮费"
    assert state["invoice"]["category_rule"] == "learned.seller"
    assert [edit["field"] for edit in state["edits"]] == [
        "category_rule",
        "category",
    ]
    assert state["edits"][1]["old_value"] == "办公费用"
    assert state["edits"][1]["new_value"] == "餐饮费"
    assert state["edits"][0]["old_value"] == "item.office"

    assert api.delete_rule(rule["id"])["ok"] is True
    assert api.delete_rule(rule["id"])["ok"] is False
    assert api.list_rules()["data"]["rules"] == []


def test_rule_endpoints_reject_invalid_ids(api: Api) -> None:
    assert api.set_rule_enabled("abc", True)["ok"] is False
    assert api.delete_rule(0)["ok"] is False
    assert api.preview_rule_backfill(None)["ok"] is False
    assert api.apply_rule_backfill(999, "token")["ok"] is False


def _folder_dialog_api(tmp_path, dialog):
    return Api(
        db_path=tmp_path / "workbench.db",
        out_dir=tmp_path / "out",
        folder_dialog=dialog,
    )


def test_pick_folder_returns_selection_and_trims_the_initial_value(tmp_path) -> None:
    seen: list[str] = []

    def dialog(current: str) -> str | None:
        seen.append(current)
        return r"D:\发票\2026-07"

    api = _folder_dialog_api(tmp_path, dialog)
    response = api.pick_folder(r"  D:\发票  ")
    assert response == {"ok": True, "data": {"path": r"D:\发票\2026-07"}}
    assert seen == [r"D:\发票"]


def test_pick_folder_is_remembered_for_the_next_import(tmp_path) -> None:
    folder = tmp_path / "发票"
    folder.mkdir()
    api = _folder_dialog_api(tmp_path, lambda current: str(folder))
    assert api.pick_folder()["ok"] is True
    boot = api.bootstrap()
    assert boot["ok"] is True
    assert boot["data"]["recent_import_paths"] == [str(folder)]


def test_pick_folder_cancel_blank_and_oversized_initial(tmp_path) -> None:
    seen: list[str] = []

    def cancel(current: str) -> str | None:
        seen.append(current)
        return None

    api = _folder_dialog_api(tmp_path, cancel)
    assert api.pick_folder() == {"ok": True, "data": {"path": None}}
    assert api.pick_folder("x" * 4097) == {"ok": True, "data": {"path": None}}
    assert seen == ["", ""]

    blank = _folder_dialog_api(tmp_path, lambda current: "   ")
    assert blank.pick_folder(r"D:\发票") == {"ok": True, "data": {"path": None}}


def test_pick_folder_without_a_dialog_is_an_error(tmp_path) -> None:
    api = Api(db_path=tmp_path / "workbench.db", out_dir=tmp_path / "out")
    response = api.pick_folder()
    assert response["ok"] is False
    assert "粘贴路径" in response["error"]


def test_pick_folder_errors_are_redacted(tmp_path) -> None:
    def broken(current: str) -> str | None:
        raise ReportError(r"无法打开 D:\Customers\张 三\发票")

    api = _folder_dialog_api(tmp_path, broken)
    response = api.pick_folder()
    assert response["ok"] is False
    assert r"D:\Customers" not in response["error"]
    assert "<路径已隐藏>" in response["error"]


def test_reparse_endpoint_is_empty_when_everything_is_recognized(api: Api) -> None:
    response = api.reparse_unrecognized()
    assert response["ok"] is True
    assert response["data"] == {
        "candidates": 0,
        "reparsed": 0,
        "failed": 0,
        "skipped_edited": 0,
        "skipped_missing": 0,
        "conflicts": 0,
    }


def test_bootstrap_falls_back_to_existing_batch_folders(api: Api, tmp_path) -> None:
    folder = tmp_path / "发票"
    folder.mkdir()
    connection = db.connect(tmp_path / "workbench.db")
    try:
        with connection:
            db.create_batch(connection, label="旧批次", source_dir=str(folder))
    finally:
        connection.close()
    boot = api.bootstrap()
    assert boot["ok"] is True
    assert boot["data"]["recent_import_paths"] == [str(folder)]


# ---------------------------------------------------------------- first run


@pytest.fixture
def app_config(monkeypatch, tmp_path):
    """Keep the settings file inside the test's tmp folder."""
    path = tmp_path / "app-home" / "config.json"
    monkeypatch.setattr(appconfig, "config_path", lambda: path)
    return path


def test_bootstrap_reports_first_run_and_the_data_dir(api: Api, app_config) -> None:
    data = api.bootstrap()["data"]
    assert data["first_run"] is True
    assert data["data_dir"] == str(Path(api._db_path).resolve().parent)
    assert data["suggested_data_dir"]
    assert api.complete_welcome() == {"ok": True, "data": {"welcome_done": True}}
    assert api.bootstrap()["data"]["first_run"] is False


def test_first_run_is_false_when_the_pool_already_has_data(api: Api, app_config) -> None:
    _create_report(api)
    assert api.bootstrap()["data"]["first_run"] is False


def test_inspect_data_dir_handles_new_and_existing_folders(
    api: Api, tmp_path, app_config
) -> None:
    fresh = tmp_path / "新目录"
    response = api.inspect_data_dir(str(fresh))
    assert response["ok"] is True, response
    data = response["data"]
    assert data["exists"] is False
    assert data["has_database"] is False
    assert data["path"] == str(fresh.resolve())

    existing = tmp_path / "旧数据"
    existing.mkdir()
    connection = db.connect(existing / "invoicedesk.db")
    try:
        add_invoice(connection)
    finally:
        connection.close()
    response = api.inspect_data_dir(str(existing))
    assert response["ok"] is True, response
    data = response["data"]
    assert data["exists"] is True
    assert data["has_database"] is True
    assert data["invoice_count"] == 1
    assert data["batch_count"] == 1
    assert data["database_version"] == db.DB_VERSION


def test_inspect_data_dir_rejects_an_unreadable_database(
    api: Api, tmp_path, app_config
) -> None:
    target = tmp_path / "坏数据"
    target.mkdir()
    (target / "invoicedesk.db").write_text("not a database", encoding="utf-8")
    response = api.inspect_data_dir(str(target))
    assert response["ok"] is False
    assert "invoicedesk.db" in response["error"]


def test_select_data_dir_moves_an_empty_pool(api: Api, tmp_path, app_config) -> None:
    target = tmp_path / "发票数据"
    response = api.select_data_dir(str(target))
    assert response["ok"] is True, response
    data = response["data"]
    assert data["data_dir"] == str(target.resolve())
    assert data["has_database"] is False
    assert api._db_path == target.resolve() / "invoicedesk.db"
    # Exports are a separate choice: switching the pool never moves them.
    assert api._out_dir == tmp_path / "out"
    assert api.bootstrap()["data"]["data_dir"] == str(target.resolve())
    assert appconfig.data_dir() == target.resolve()
    # Writes after the switch land in the new database, not the old one.
    _create_report(api)
    connection = db.connect(target / "invoicedesk.db")
    try:
        count = connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_select_data_dir_refuses_when_the_pool_has_data(
    api: Api, tmp_path, app_config
) -> None:
    _create_report(api)
    before = api._db_path
    response = api.select_data_dir(str(tmp_path / "其他目录"))
    assert response["ok"] is False
    assert "已有数据" in response["error"]
    assert api._db_path == before


def test_select_data_dir_adopts_an_existing_database(
    api: Api, tmp_path, app_config
) -> None:
    existing = tmp_path / "已有数据"
    existing.mkdir()
    connection = db.connect(existing / "invoicedesk.db")
    try:
        add_invoice(connection)
    finally:
        connection.close()
    response = api.select_data_dir(str(existing))
    assert response["ok"] is True, response
    data = response["data"]
    assert data["has_database"] is True
    assert data["invoice_count"] == 1
    assert data["batch_count"] == 1
    assert len(api.list_invoices()["data"]["rows"]) == 1
    assert api.bootstrap()["data"]["first_run"] is False


def test_select_data_dir_rejects_relative_and_repo_paths(api: Api, app_config) -> None:
    relative = api.select_data_dir("发票数据")
    assert relative["ok"] is False
    assert "完整路径" in relative["error"]
    inside_repo = api.select_data_dir(str(ROOT / "app" / "data"))
    assert inside_repo["ok"] is False
    assert "仓库" in inside_repo["error"]


def test_pick_folder_can_skip_the_recent_import_list(tmp_path) -> None:
    folder = tmp_path / "数据目录"
    folder.mkdir()
    api = _folder_dialog_api(tmp_path, lambda current: str(folder))
    assert api.pick_folder("", False)["ok"] is True
    assert api.bootstrap()["data"]["recent_import_paths"] == []


def test_inspect_data_dir_treats_an_empty_database_as_new(
    api: Api, tmp_path, app_config
) -> None:
    target = tmp_path / "空库"
    connection = db.connect(target / "invoicedesk.db")
    connection.close()
    data = api.inspect_data_dir(str(target))["data"]
    assert data["has_database"] is False
    assert data["invoice_count"] == 0
    assert data["batch_count"] == 0


def test_inspect_data_dir_rejects_a_newer_database(api: Api, tmp_path, app_config) -> None:
    target = tmp_path / "新版数据"
    target.mkdir()
    connection = db.connect(target / "invoicedesk.db")
    try:
        with connection:
            connection.execute(f"PRAGMA user_version = {db.DB_VERSION + 1}")
    finally:
        connection.close()
    response = api.inspect_data_dir(str(target))
    assert response["ok"] is False
    assert "版本" in response["error"]


def test_inspect_data_dir_checks_the_schema_before_the_version(
    api: Api, tmp_path, app_config
) -> None:
    target = tmp_path / "外来库"
    target.mkdir()
    connection = sqlite3.connect(target / "invoicedesk.db")
    try:
        with connection:
            connection.execute("CREATE TABLE unrelated (id INTEGER)")
            connection.execute(f"PRAGMA user_version = {db.DB_VERSION + 1}")
    finally:
        connection.close()
    response = api.inspect_data_dir(str(target))
    assert response["ok"] is False
    assert "不是发票管家的数据库" in response["error"]


def test_select_data_dir_leaves_settings_alone_when_the_database_is_broken(
    api: Api, tmp_path, app_config
) -> None:
    target = tmp_path / "坏库"
    target.mkdir()
    (target / "invoicedesk.db").write_text("not a database", encoding="utf-8")
    before = api._db_path
    response = api.select_data_dir(str(target))
    assert response["ok"] is False
    assert "invoicedesk.db" in response["error"]
    assert api._db_path == before
    assert appconfig.data_dir() is None
    assert not app_config.exists()


def test_select_data_dir_rejects_a_newer_database(api: Api, tmp_path, app_config) -> None:
    target = tmp_path / "新版数据"
    target.mkdir()
    connection = db.connect(target / "invoicedesk.db")
    try:
        with connection:
            connection.execute(f"PRAGMA user_version = {db.DB_VERSION + 1}")
    finally:
        connection.close()
    before = api._db_path
    response = api.select_data_dir(str(target))
    assert response["ok"] is False
    assert "版本" in response["error"]
    assert api._db_path == before
    assert not app_config.exists()


def test_select_data_dir_rejects_a_foreign_sqlite_database(
    api: Api, tmp_path, app_config
) -> None:
    target = tmp_path / "外来库"
    target.mkdir()
    connection = sqlite3.connect(target / "invoicedesk.db")
    try:
        with connection:
            connection.execute("CREATE TABLE unrelated (id INTEGER)")
    finally:
        connection.close()
    before = api._db_path
    response = api.select_data_dir(str(target))
    assert response["ok"] is False
    assert "不是发票管家的数据库" in response["error"]
    assert api._db_path == before
    assert appconfig.data_dir() is None
    assert not app_config.exists()


def _invoice_count(db_path: Path) -> int:
    connection = db.connect(db_path)
    try:
        return connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
    finally:
        connection.close()


def _packaged_api(tmp_path, monkeypatch) -> Api:
    """A fresh Api shaped like the packaged app: no fixed output folder."""
    monkeypatch.setattr(api_module.runtime, "uses_app_home", lambda: True)
    home_out = tmp_path / "home-out"
    monkeypatch.setattr(api_module.runtime, "default_out_dir", lambda: home_out)
    return Api(db_path=tmp_path / "workbench.db")


def test_first_import_pins_exports_beside_the_invoices(
    tmp_path, app_config, monkeypatch
) -> None:
    api = _packaged_api(tmp_path, monkeypatch)
    folder = tmp_path / "发票"
    folder.mkdir()
    source_pdf = make_pdf(folder, full_digital_lines(), name="a.pdf")
    source_bytes = source_pdf.read_bytes()
    target = folder / appconfig.EXPORT_SUBDIR

    planned = api.import_preflight(str(folder))
    assert planned["ok"] is True, planned
    assert planned["data"]["export_dir_preview"] == str(target.resolve())

    started = api.import_start(str(folder))
    assert started["ok"] is True, started
    assert started["data"]["adopted_export_dir"] == str(target.resolve())
    state = _wait_import(api, started["data"]["job_id"])
    assert state["status"] == "done", state

    assert appconfig.export_dir() == target.resolve()
    assert api._out_dir == target.resolve()
    # The pool stays in the data folder; the invoice folder receives exports
    # only, never the database.
    assert _invoice_count(tmp_path / "workbench.db") == 1
    assert not (target / "invoicedesk.db").exists()
    assert source_pdf.read_bytes() == source_bytes
    assert {entry.name for entry in folder.iterdir()} == {
        "a.pdf",
        appconfig.EXPORT_SUBDIR,
    }
    boot = api.bootstrap()["data"]
    assert boot["data_dir"] == str(tmp_path.resolve())
    assert boot["export_dir"] == str(target.resolve())


def test_a_later_import_moves_the_export_folder(
    tmp_path, app_config, monkeypatch
) -> None:
    api = _packaged_api(tmp_path, monkeypatch)
    july = tmp_path / "七月"
    july.mkdir()
    make_pdf(july, full_digital_lines(), name="a.pdf")
    august = tmp_path / "八月"
    august.mkdir()
    make_pdf(august, full_digital_lines(), name="b.pdf")

    for folder in (july, august):
        started = api.import_start(str(folder))
        assert started["ok"] is True, started
        state = _wait_import(api, started["data"]["job_id"])
        assert state["status"] == "done", state

    target = (august / appconfig.EXPORT_SUBDIR).resolve()
    assert appconfig.export_dir() == target
    assert api.bootstrap()["data"]["export_dir"] == str(target)


def test_export_adoption_ignores_a_pinned_data_dir(
    tmp_path, app_config, monkeypatch
) -> None:
    pinned = tmp_path / "固定目录"
    pinned.mkdir()
    appconfig.remember_data_dir(pinned)
    api = _packaged_api(tmp_path, monkeypatch)
    folder = tmp_path / "发票"
    folder.mkdir()
    make_pdf(folder, full_digital_lines(), name="a.pdf")
    target = folder / appconfig.EXPORT_SUBDIR

    planned = api.import_preflight(str(folder))
    assert planned["data"]["export_dir_preview"] == str(target.resolve())
    started = api.import_start(str(folder))
    assert started["data"]["adopted_export_dir"] == str(target.resolve())
    _wait_import(api, started["data"]["job_id"])

    assert appconfig.data_dir() == pinned
    assert appconfig.export_dir() == target.resolve()


def test_explicit_out_dir_is_never_moved(
    api: Api, tmp_path, app_config, monkeypatch
) -> None:
    monkeypatch.setattr(api_module.runtime, "uses_app_home", lambda: True)
    folder = tmp_path / "发票"
    folder.mkdir()
    make_pdf(folder, full_digital_lines(), name="a.pdf")

    planned = api.import_preflight(str(folder))
    assert planned["data"]["export_dir_preview"] is None
    started = api.import_start(str(folder))
    assert started["data"]["adopted_export_dir"] is None
    _wait_import(api, started["data"]["job_id"])

    assert api._out_dir == tmp_path / "out"
    assert appconfig.export_dir() is None
    assert not (folder / appconfig.EXPORT_SUBDIR).exists()


def test_source_checkout_keeps_the_default_export_dir(
    tmp_path, app_config, monkeypatch
) -> None:
    monkeypatch.setattr(api_module.runtime, "default_out_dir", lambda: tmp_path / "out")
    api = Api(db_path=tmp_path / "workbench.db")
    folder = tmp_path / "发票"
    folder.mkdir()
    make_pdf(folder, full_digital_lines(), name="a.pdf")

    planned = api.import_preflight(str(folder))
    assert planned["data"]["export_dir_preview"] is None
    started = api.import_start(str(folder))
    assert started["data"]["adopted_export_dir"] is None
    _wait_import(api, started["data"]["job_id"])

    assert api._out_dir == tmp_path / "out"
    assert _invoice_count(tmp_path / "workbench.db") == 1
    assert not (folder / appconfig.EXPORT_SUBDIR).exists()


def test_export_adoption_failure_stops_the_import(
    tmp_path, app_config, monkeypatch
) -> None:
    api = _packaged_api(tmp_path, monkeypatch)
    folder = tmp_path / "发票"
    folder.mkdir()
    make_pdf(folder, full_digital_lines(), name="a.pdf")
    target = folder / appconfig.EXPORT_SUBDIR

    def deny(path):
        raise OSError("denied")

    monkeypatch.setattr(appconfig, "remember_export_dir", deny)

    planned = api.import_preflight(str(folder))
    assert planned["data"]["export_dir_preview"] == str(target.resolve())
    started = api.import_start(str(folder))
    assert started["ok"] is False
    assert "导出目录" in started["error"]
    # Nothing was imported and the pointer never changed.
    assert api.import_status("")["data"]["status"] == "idle"
    assert _invoice_count(tmp_path / "workbench.db") == 0
    assert not (target / "invoicedesk.db").exists()
    assert appconfig.export_dir() is None


def test_export_adoption_skips_an_unsafe_target(
    tmp_path, app_config, monkeypatch
) -> None:
    api = _packaged_api(tmp_path, monkeypatch)
    folder = tmp_path / "发票"
    folder.mkdir()
    make_pdf(folder, full_digital_lines(), name="a.pdf")

    def unsafe(path):
        raise appconfig.ConfigError("数据目录位于云同步目录")

    monkeypatch.setattr(appconfig, "resolve_data_dir", unsafe)

    planned = api.import_preflight(str(folder))
    assert planned["data"]["export_dir_preview"] is None
    started = api.import_start(str(folder))
    assert started["data"]["adopted_export_dir"] is None
    _wait_import(api, started["data"]["job_id"])
    assert _invoice_count(tmp_path / "workbench.db") == 1
    assert not (folder / appconfig.EXPORT_SUBDIR).exists()


def test_export_adoption_skips_known_sync_folders(
    tmp_path, app_config, monkeypatch
) -> None:
    api = _packaged_api(tmp_path, monkeypatch)
    folder = tmp_path / "Dropbox" / "发票"
    folder.mkdir(parents=True)
    make_pdf(folder, full_digital_lines(), name="a.pdf")

    planned = api.import_preflight(str(folder))
    assert planned["data"]["export_dir_preview"] is None
    assert "Dropbox" in planned["data"]["export_dir_note"]
    started = api.import_start(str(folder))
    assert started["data"]["adopted_export_dir"] is None
    _wait_import(api, started["data"]["job_id"])
    assert _invoice_count(tmp_path / "workbench.db") == 1
    assert not (folder / appconfig.EXPORT_SUBDIR).exists()
