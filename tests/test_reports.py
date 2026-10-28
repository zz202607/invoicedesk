"""Report model: schema upgrade, lines, links, and invoice import."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core import db, reports
from app.core.reports import ReportError
from tests.report_factory import add_invoice, add_line, make_db, make_report


@pytest.fixture
def connection(tmp_path):
    conn = make_db(tmp_path)
    yield conn
    conn.close()


def test_v1_database_upgrades_and_keeps_invoices(tmp_path) -> None:
    path = tmp_path / "pool.db"
    connection = db.connect(path)
    add_invoice(connection)
    connection.executescript(
        "DROP TABLE report_line_invoices;"
        "DROP TABLE report_lines;"
        "DROP TABLE reports;"
        "PRAGMA user_version = 1;"
    )
    connection.commit()
    connection.close()

    connection = db.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db.DB_VERSION
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"reports", "report_lines", "report_line_invoices"} <= tables
        assert connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1
    finally:
        connection.close()


def test_create_report_generates_title_and_lists_newest_first(connection) -> None:
    first = make_report(connection, month="2026-07")
    second = reports.create_report(
        connection,
        company="示例甲",
        reporter="李四",
        month="2026-09",
        department="示例部门乙",
        title="示例甲9月报销费用支出明细表",
    )

    listing = reports.list_reports(connection)
    assert [report.id for report in listing] == [second, first]
    assert reports.get_report(connection, first).title == "示例公司7月报销费用支出明细表"
    assert reports.get_report(connection, second).title == "示例甲9月报销费用支出明细表"
    assert reports.get_report(connection, first).amount_cents == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"company": ""},
        {"company": "   "},
        {"reporter": ""},
        {"month": "2026-13"},
        {"month": "202607"},
    ],
)
def test_create_report_rejects_invalid_header(connection, overrides) -> None:
    values = {"company": "示例公司", "reporter": "张三", "month": "2026-07"}
    values.update(overrides)
    with pytest.raises(ReportError):
        reports.create_report(connection, **values)


def test_get_report_rejects_unknown_id(connection) -> None:
    with pytest.raises(ReportError, match="不存在"):
        reports.get_report(connection, 99)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1234.5", "1234.50"),
        ("1,234.56", "1234.56"),
        ("¥ 12", "12.00"),
        ("-1000.00", "-1000.00"),
        ("０.５", "0.50"),
    ],
)
def test_parse_amount_accepts_reimbursement_formats(text: str, expected: str) -> None:
    assert reports.parse_amount(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["1.234", "abc", "", "NaN", "Infinity"])
def test_parse_amount_rejects_invalid_values(text: str) -> None:
    with pytest.raises(ReportError):
        reports.parse_amount(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("152", "152"), ("12.5km", "12.5"), ("12.50 公里", "12.50")],
)
def test_parse_km_accepts_units(text: str, expected: str) -> None:
    assert reports.parse_km(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["0", "-1", "1.234", "abc"])
def test_parse_km_rejects_invalid_values(text: str) -> None:
    with pytest.raises(ReportError):
        reports.parse_km(text)


def test_mileage_cents_rounds_half_up() -> None:
    assert reports.mileage_cents(Decimal(150)) == 22500
    assert reports.mileage_cents(Decimal("12.5")) == 1875
    assert reports.mileage_cents(Decimal(150), Decimal(2)) == 30000
    with pytest.raises(ReportError):
        reports.mileage_cents(Decimal(1), Decimal(0))


def test_add_line_keeps_order_and_negative_transfers(connection) -> None:
    report_id = make_report(connection)
    first = add_line(connection, report_id)
    second = add_line(
        connection,
        report_id,
        line_date="2026-07-10",
        category="转账",
        description="示例转账",
        amount_cents=-100000,
        project_name="项目A",
        has_invoice=True,
        invoice_note="示例附注",
    )

    lines = reports.report_lines(connection, report_id)
    assert [line.id for line in lines] == [first, second]
    assert lines[1].amount == Decimal("-1000.00")
    assert lines[1].has_invoice is True
    assert lines[1].invoice_note == "示例附注"
    assert lines[1].project_name == "项目A"

    report = reports.get_report(connection, report_id)
    assert report.line_count == 2
    assert report.amount_cents == 12000 - 100000
    assert report.amount == Decimal("-880.00")


def test_add_line_defaults_category_and_validates_fields(connection) -> None:
    report_id = make_report(connection)
    add_line(connection, report_id, category="")
    assert reports.report_lines(connection, report_id)[0].category == "其他费用"

    with pytest.raises(ReportError):
        add_line(connection, report_id, line_date="2026/07/09")
    with pytest.raises(ReportError):
        add_line(connection, report_id, category="   ")
    with pytest.raises(ReportError):
        add_line(connection, report_id, amount_cents=True)
    with pytest.raises(ReportError):
        add_line(connection, report_id, source="guess")
    with pytest.raises(ReportError):
        reports.add_line(
            connection,
            99,
            line_date="2026-07-09",
            category="办公费用",
            description="x",
            amount_cents=100,
        )


def test_add_mileage_stores_distance_and_amount(connection) -> None:
    report_id = make_report(connection)
    line_id, cents = reports.add_mileage(
        connection,
        report_id,
        line_date="2026-07-10",
        km=Decimal(150),
        description="示例行程",
    )
    assert cents == 22500

    line = reports.report_lines(connection, report_id)[0]
    assert line.id == line_id
    assert line.source == "mileage"
    assert line.mileage_km == 150.0
    assert line.amount_cents == 22500
    assert line.category == "汽车费用"
    assert line.has_invoice is False

    with pytest.raises(ReportError):
        reports.add_mileage(
            connection,
            report_id,
            line_date="2026-07-10",
            km=Decimal(10),
            description="x",
            rate=Decimal(0),
        )


def test_update_line_changes_only_given_fields(connection) -> None:
    report_id = make_report(connection)
    add_line(connection, report_id)

    reports.update_line(
        connection,
        report_id,
        1,
        line_date="2026-07-11",
        category="差旅费",
        amount_cents=12345,
        description="",
        project_name="项目B",
        has_invoice=True,
    )
    line = reports.report_lines(connection, report_id)[0]
    assert line.line_date == "2026-07-11"
    assert line.category == "差旅费"
    assert line.amount_cents == 12345
    assert line.description == ""
    assert line.project_name == "项目B"
    assert line.has_invoice is True

    reports.update_line(connection, report_id, 1, description="改回说明")
    assert reports.report_lines(connection, report_id)[0].description == "改回说明"

    with pytest.raises(ReportError):
        reports.update_line(connection, report_id, 1)
    with pytest.raises(ReportError):
        reports.update_line(connection, report_id, 2, description="x")


def test_delete_line_shifts_sequence_and_cascades_links(connection) -> None:
    invoice_id = add_invoice(connection)
    report_id = make_report(connection)
    add_line(connection, report_id, description="第一行", invoice_ids=[invoice_id])
    add_line(connection, report_id, description="第二行")

    reports.delete_line(connection, report_id, 1)
    lines = reports.report_lines(connection, report_id)
    assert [line.description for line in lines] == ["第二行"]
    assert connection.execute("SELECT COUNT(*) FROM report_line_invoices").fetchone()[0] == 0

    with pytest.raises(ReportError):
        reports.delete_line(connection, report_id, 2)


def test_link_and_unlink_invoices(connection) -> None:
    invoice_id = add_invoice(connection)
    report_id = make_report(connection)
    add_line(connection, report_id)

    assert reports.link_invoices(connection, report_id, 1, [invoice_id, invoice_id]) == 1
    line = reports.report_lines(connection, report_id)[0]
    assert line.invoice_ids == (invoice_id,)
    assert line.has_invoice is True
    assert line.invoice_total_cents == 12000
    assert not line.amount_mismatch

    assert reports.link_invoices(connection, report_id, 1, [invoice_id]) == 0

    with pytest.raises(ReportError):
        reports.link_invoices(connection, report_id, 1, [999])
    with pytest.raises(ReportError):
        reports.link_invoices(connection, report_id, 1, [])

    assert reports.unlink_invoices(connection, report_id, 1, [invoice_id]) == 1
    line = reports.report_lines(connection, report_id)[0]
    assert line.invoice_ids == ()
    assert reports.unlink_invoices(connection, report_id, 1, [invoice_id]) == 0


def test_add_line_with_missing_invoice_is_atomic(connection) -> None:
    report_id = make_report(connection)
    with pytest.raises(ReportError, match="发票不存在"):
        add_line(connection, report_id, invoice_ids=[999])
    assert reports.get_report(connection, report_id).line_count == 0


def test_line_detects_invoice_total_mismatch(connection) -> None:
    invoice_id = add_invoice(connection)
    report_id = make_report(connection)
    add_line(connection, report_id, amount_cents=10000, invoice_ids=[invoice_id])

    line = reports.report_lines(connection, report_id)[0]
    assert line.invoice_sum_known
    assert line.invoice_total_cents == 12000
    assert line.amount_mismatch


def test_line_with_invoice_missing_amount_is_unknown(connection) -> None:
    invoice_id = add_invoice(connection, amount=None)
    report_id = make_report(connection)
    add_line(connection, report_id, invoice_ids=[invoice_id])

    line = reports.report_lines(connection, report_id)[0]
    assert line.invoice_ids == (invoice_id,)
    assert line.invoice_sum_known is False
    assert line.invoice_total_cents is None
    assert line.amount_mismatch is False


def test_from_invoices_creates_linked_lines(connection) -> None:
    first = add_invoice(connection)
    second = add_invoice(
        connection,
        index=2,
        number="26502000000000000002",
        date="2026-07-10",
        seller="示例乙酒店",
        item="住宿费",
        amount=Decimal("800.00"),
        category="差旅费",
    )
    report_id = make_report(connection)

    result = reports.from_invoices(connection, report_id)
    assert result.added == 2
    assert result.skipped == 0

    lines = reports.report_lines(connection, report_id)
    assert [line.line_date for line in lines] == ["2026-07-09", "2026-07-10"]
    assert [line.amount_cents for line in lines] == [12000, 80000]
    assert [line.category for line in lines] == ["办公费用", "差旅费"]
    assert lines[0].description == "办公用品(示例销售方)"
    assert lines[1].description == "住宿费(示例乙酒店)"
    assert [line.invoice_ids for line in lines] == [(first,), (second,)]
    assert all(line.has_invoice and line.source == "invoice" for line in lines)

    again = reports.from_invoices(connection, report_id)
    assert again.added == 0
    assert again.skipped_linked == 2


def test_from_invoices_skips_missing_amount_and_date(connection) -> None:
    add_invoice(connection, index=1, amount=None)
    add_invoice(connection, index=2, number="26502000000000000002", date="")
    report_id = make_report(connection)

    result = reports.from_invoices(connection, report_id)
    assert result.added == 0
    assert result.skipped_missing_amount == 1
    assert result.skipped_missing_date == 1


def test_from_invoices_skips_invoices_linked_to_another_report(connection) -> None:
    invoice_id = add_invoice(connection)
    other_report = make_report(connection, company="示例甲", reporter="李四", month="2026-09")
    add_line(connection, other_report, invoice_ids=[invoice_id])

    report_id = make_report(connection)
    result = reports.from_invoices(connection, report_id)
    assert result.added == 0
    assert result.skipped_linked == 1
    assert reports.get_report(connection, report_id).line_count == 0


def test_from_invoices_filters_batch_buyer_and_date(connection) -> None:
    batch_one = db.create_batch(connection, label="七月", source_dir=r"D:\invoices")
    batch_two = db.create_batch(connection, label="八月", source_dir=r"D:\invoices")
    add_invoice(
        connection,
        batch_id=batch_one,
        index=1,
        number="26502000000000000001",
        date="2026-07-09",
        buyer="示例甲公司",
    )
    add_invoice(
        connection,
        batch_id=batch_one,
        index=2,
        number="26502000000000000002",
        date="2026-07-20",
        buyer="示例乙公司",
    )
    add_invoice(
        connection,
        batch_id=batch_one,
        index=3,
        number="26502000000000000003",
        date="2026-07-25",
        buyer="示例丙公司",
    )
    add_invoice(
        connection,
        batch_id=batch_two,
        index=4,
        number="26502000000000000004",
        date="2026-08-01",
        buyer="示例甲公司",
    )

    by_batch = make_report(connection)
    result = reports.from_invoices(connection, by_batch, batch_id=batch_two)
    assert result.added == 1
    assert reports.report_lines(connection, by_batch)[0].line_date == "2026-08-01"

    by_buyer = make_report(connection, month="2026-09")
    result = reports.from_invoices(connection, by_buyer, buyer="示例乙公司")
    assert result.added == 1
    assert reports.report_lines(connection, by_buyer)[0].line_date == "2026-07-20"

    by_date = make_report(connection, month="2026-10")
    result = reports.from_invoices(
        connection, by_date, date_from="2026-07-15", date_to="2026-07-31"
    )
    assert result.added == 1
    assert result.skipped_linked == 1
    assert reports.report_lines(connection, by_date)[0].line_date == "2026-07-25"


def test_from_invoices_can_exclude_review(connection) -> None:
    add_invoice(connection, index=1, review=True)
    add_invoice(connection, index=2, number="26502000000000000002")

    report_id = make_report(connection)
    result = reports.from_invoices(connection, report_id, include_review=False)
    assert result.added == 1
    assert reports.report_lines(connection, report_id)[0].amount_cents == 12000

    with_invoice = make_report(connection, month="2026-09")
    result = reports.from_invoices(connection, with_invoice)
    assert result.added == 1
    assert reports.report_lines(connection, with_invoice)[0].amount_cents == 12000


def test_from_invoices_applies_project_name(connection) -> None:
    add_invoice(connection)
    report_id = make_report(connection)
    reports.from_invoices(connection, report_id, project_name="示例项目")
    assert reports.report_lines(connection, report_id)[0].project_name == "示例项目"
