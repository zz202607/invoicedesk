"""Workbench read models: invoice listing, summaries, and PDF preview."""

from __future__ import annotations

import base64
import hashlib
import json
from decimal import Decimal

import fitz
import pytest

from app.core import db, invoices, reports, workbench
from app.core.models import InvoiceType
from app.core.workbench import PreviewError
from tests.pdf_factory import make_pdf, make_pdf_pages
from tests.report_factory import add_invoice, add_line, make_db, make_report


@pytest.fixture
def connection(tmp_path):
    conn = make_db(tmp_path)
    yield conn
    conn.close()


def test_invoice_row_exposes_learned_category_rule(connection) -> None:
    invoice_id = add_invoice(
        connection, category="餐饮费", category_rule="learned.seller"
    )
    row = workbench.get_invoice_row(connection, invoice_id)
    assert row is not None
    assert row.category_rule == "learned.seller"


def test_list_invoices_filters_and_order(connection) -> None:
    first = add_invoice(
        connection,
        index=1,
        number="A1",
        date="2026-07-01",
        seller="甲酒店",
        buyer="甲公司",
    )
    second = add_invoice(
        connection,
        index=2,
        number="A2",
        date="2026-08-01",
        seller="乙加油站",
        buyer="乙公司",
        review=True,
    )

    newest_first = workbench.list_invoices(connection)
    assert [row.id for row in newest_first] == [second, first]
    assert newest_first[0].needs_review is True
    assert newest_first[0].source_exists is False

    assert [row.id for row in workbench.list_invoices(connection, search="甲")] == [first]
    assert [row.id for row in workbench.list_invoices(connection, search="乙")] == [second]
    assert workbench.list_invoices(connection, search="%") == []
    assert [
        row.id for row in workbench.list_invoices(connection, include_review=False)
    ] == [first]
    assert [
        row.id
        for row in workbench.list_invoices(
            connection, date_from="2026-07-15", date_to="2026-08-31"
        )
    ] == [second]
    assert [
        row.id for row in workbench.list_invoices(connection, buyer="乙公司")
    ] == [second]


def test_list_invoices_or_branches_and_date_edges(connection) -> None:
    seller_hit = add_invoice(
        connection, index=1, number="X1", seller="只在销售方命中", item="普通项目"
    )
    item_hit = add_invoice(
        connection, index=2, number="X2", seller="普通商家", item="只在项目命中"
    )
    buyer_hit = add_invoice(
        connection, index=3, number="X3", seller="普通商家", item="普通项目", buyer="只在购买方命中"
    )
    no_date = add_invoice(connection, index=4, number="X4", date="", seller="无日期")

    def ids(rows):
        return [row.id for row in rows]

    assert ids(workbench.list_invoices(connection, search="只在销售方")) == [seller_hit]
    assert ids(workbench.list_invoices(connection, search="只在项目")) == [item_hit]
    assert ids(workbench.list_invoices(connection, search="只在购买方")) == [buyer_hit]
    assert no_date not in ids(
        workbench.list_invoices(connection, date_from="2000-01-01")
    )
    assert no_date not in ids(
        workbench.list_invoices(connection, date_to="2999-12-31")
    )


def test_unrecognized_only_filter(connection) -> None:
    recognized = add_invoice(connection, index=1, number="U1")
    scanned = add_invoice(
        connection, index=2, number="U2", invoice_type=InvoiceType.SCANNED
    )
    unknown = add_invoice(
        connection, index=3, number="U3", invoice_type=InvoiceType.UNKNOWN
    )

    rows = workbench.list_invoices(connection, unrecognized_only=True)

    assert {row.id for row in rows} == {scanned, unknown}
    assert recognized not in {row.id for row in rows}


def test_links_and_unlinked_filter(connection) -> None:
    linked_id = add_invoice(connection, index=1, number="L1")
    other_id = add_invoice(connection, index=2, number="L2")
    report_id = make_report(connection)
    add_line(connection, report_id, invoice_ids=[linked_id])

    rows = workbench.list_invoices(connection)
    linked = next(row for row in rows if row.id == linked_id)
    assert linked.is_linked is True
    assert linked.links[0].report_id == report_id
    assert linked.links[0].report_month == "2026-07"

    assert [row.id for row in workbench.list_invoices(connection, unlinked_only=True)] == [
        other_id
    ]

    summary = workbench.summarize_invoices(rows)
    assert summary.count == 2
    assert summary.linked_count == 1
    assert summary.total_cents == 24000


def test_add_lines_from_selected_invoices(connection) -> None:
    normal = add_invoice(connection, index=1, number="S1")
    no_amount = add_invoice(connection, index=2, number="S2", amount=None)
    no_date = add_invoice(connection, index=3, number="S3", date="")
    already_linked = add_invoice(connection, index=4, number="S4")
    zero_amount = add_invoice(connection, index=5, number="S5", amount=Decimal("0.00"))
    report_id = make_report(connection)
    add_line(connection, report_id, invoice_ids=[already_linked])

    result = reports.add_lines_from_invoices(
        connection,
        report_id,
        [normal, no_amount, no_date, already_linked, zero_amount, normal],
        project_name="测试项目",
    )

    assert result.added == 2
    assert result.skipped_linked == 1
    assert result.skipped_missing_amount == 1
    assert result.skipped_missing_date == 1
    assert result.skipped == 3
    lines = reports.report_lines(connection, report_id)
    added = next(line for line in lines if normal in line.invoice_ids)
    assert added.description == "办公用品(示例销售方)"
    assert added.project_name == "测试项目"
    assert added.amount_cents == 12000
    zero_line = next(line for line in lines if zero_amount in line.invoice_ids)
    assert zero_line.amount_cents == 0

    second = reports.add_lines_from_invoices(
        connection,
        report_id,
        [normal, no_amount, no_date, already_linked, zero_amount, normal],
    )
    assert second.added == 0
    assert second.skipped_linked == 3
    assert second.skipped_missing_amount == 1
    assert second.skipped_missing_date == 1
    assert len(reports.report_lines(connection, report_id)) == len(lines)


def test_add_lines_from_invoices_rejects_bad_input_atomically(connection) -> None:
    report_id = make_report(connection)
    normal = add_invoice(connection, index=1, number="S1")

    with pytest.raises(reports.ReportError):
        reports.add_lines_from_invoices(connection, report_id, [normal, 9999])
    assert reports.report_lines(connection, report_id) == []

    with pytest.raises(reports.ReportError):
        reports.add_lines_from_invoices(connection, report_id, [])
    with pytest.raises(reports.ReportError):
        reports.add_lines_from_invoices(connection, report_id, [True])
    with pytest.raises(reports.ReportError):
        reports.add_lines_from_invoices(connection, 9999, [normal])


def test_add_lines_defaults_empty_category(connection) -> None:
    invoice_id = add_invoice(connection, index=1, number="C1", category="")
    report_id = make_report(connection)
    result = reports.add_lines_from_invoices(connection, report_id, [invoice_id])
    assert result.added == 1
    line = reports.report_lines(connection, report_id)[0]
    assert line.category == "其他费用"


def test_search_escapes_metacharacters_and_normalizes(connection) -> None:
    percent = add_invoice(
        connection, index=1, number="12345%6", seller="普通商家", buyer="甲公司"
    )
    underscore = add_invoice(
        connection, index=2, number="A_1", seller="A_B 测试", buyer="乙公司"
    )
    backslash = add_invoice(
        connection, index=3, number="B1", seller="测试\\供应商", buyer="丙公司"
    )
    wide = add_invoice(
        connection, index=4, number="C1", seller="示例", buyer="ＡＢＣ公司"
    )

    def ids(rows):
        return [row.id for row in rows]

    assert ids(workbench.list_invoices(connection, search="%")) == [percent]
    assert ids(workbench.list_invoices(connection, search="12345%6")) == [percent]
    assert ids(workbench.list_invoices(connection, search="A_B")) == [underscore]
    assert workbench.list_invoices(connection, search="AXB") == []
    assert ids(workbench.list_invoices(connection, search="\\")) == [backslash]
    assert ids(workbench.list_invoices(connection, search="abc")) == [wide]
    assert ids(workbench.list_invoices(connection, search="   ")) == [
        wide,
        backslash,
        underscore,
        percent,
    ]


def test_summary_missing_amounts_and_duplicate_links(connection) -> None:
    first = add_invoice(connection, index=1, number="M1", amount=Decimal("10.00"))
    add_invoice(connection, index=2, number="M2", amount=Decimal("20.00"))
    add_invoice(connection, index=3, number="M3", amount=None)
    summary = workbench.summarize_invoices(workbench.list_invoices(connection))
    assert summary.count == 3
    assert summary.missing_amount_count == 1
    assert summary.total_cents == 3000

    first_report = make_report(connection)
    second_report = make_report(connection)
    add_line(connection, first_report, invoice_ids=[first])
    add_line(connection, first_report, invoice_ids=[first])
    add_line(connection, second_report, invoice_ids=[first])
    row = next(
        item for item in workbench.list_invoices(connection) if item.id == first
    )
    assert [link.report_id for link in row.links] == [first_report, second_report]


def test_update_report_fields(connection) -> None:
    report_id = make_report(connection)
    reports.update_report(
        connection, report_id, company="新公司", month="2026-08", title=""
    )
    report = reports.get_report(connection, report_id)
    assert report.company == "新公司"
    assert report.month == "2026-08"
    assert report.title == "新公司8月报销费用支出明细表"

    reports.update_report(connection, report_id, title="")
    assert (
        reports.get_report(connection, report_id).title
        == "新公司8月报销费用支出明细表"
    )

    reports.update_report(connection, report_id, note="只改备注")
    partial = reports.get_report(connection, report_id)
    assert partial.note == "只改备注"
    assert (partial.company, partial.reporter, partial.month) == (
        "新公司",
        "张三",
        "2026-08",
    )

    before = reports.get_report(connection, report_id)
    with pytest.raises(reports.ReportError):
        reports.update_report(connection, report_id, company="  ")
    with pytest.raises(reports.ReportError):
        reports.update_report(connection, report_id, reporter="  ")
    with pytest.raises(reports.ReportError):
        reports.update_report(connection, report_id, month="2026-13")
    with pytest.raises(reports.ReportError):
        reports.update_report(connection, report_id)
    with pytest.raises(reports.ReportError):
        reports.update_report(connection, 9999, note="不存在")
    assert reports.get_report(connection, report_id) == before


def test_preview_renders_a_page(tmp_path) -> None:
    pdf = make_pdf(tmp_path, [(50, 50, "发票测试")], name="preview.pdf")
    data_url = workbench.preview_data_url(str(pdf), 0)
    assert data_url.startswith("data:image/png;base64,")
    payload = base64.b64decode(data_url.split(",", 1)[1])
    assert payload.startswith(b"\x89PNG")

    with pytest.raises(PreviewError):
        workbench.preview_data_url(str(pdf), 5)
    with pytest.raises(PreviewError):
        workbench.preview_data_url(str(pdf), -1)
    with pytest.raises(PreviewError):
        workbench.preview_data_url(str(pdf), 0, zoom=0.49)
    with pytest.raises(PreviewError):
        workbench.preview_data_url(str(pdf), 0, zoom=3.01)
    with pytest.raises(PreviewError) as missing:
        workbench.preview_data_url(str(tmp_path / "missing.pdf"), 0)
    assert str(missing.value) == "源文件不存在：missing.pdf"

    broken = tmp_path / "broken.pdf"
    broken.write_text("not a pdf", encoding="utf-8")
    with pytest.raises(PreviewError):
        workbench.preview_data_url(str(broken), 0)

    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(PreviewError) as error:
        workbench.preview_data_url(str(empty), 0)
    assert str(tmp_path) not in str(error.value)

    before_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    before_mtime = pdf.stat().st_mtime_ns
    with pytest.raises(PreviewError):
        workbench.preview_data_url(str(pdf), 5)
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == before_hash
    assert pdf.stat().st_mtime_ns == before_mtime


def test_preview_rejects_oversized_pages(tmp_path) -> None:
    document = fitz.open()
    document.new_page(width=5000, height=1000)
    path = tmp_path / "huge.pdf"
    document.save(path)
    document.close()
    with pytest.raises(PreviewError) as error:
        workbench.preview_data_url(str(path), 0)
    assert str(error.value) == "页面过大，无法预览"


def test_preview_boundaries_pages_and_extension(tmp_path) -> None:
    pdf = make_pdf_pages(
        tmp_path,
        [[(50, 50, "第一页")], [(50, 50, "第二页")]],
        name="two-pages.pdf",
    )
    for zoom in (0.5, 3.0):
        assert workbench.preview_data_url(str(pdf), 0, zoom=zoom).startswith(
            "data:image/png;base64,"
        )
    assert workbench.preview_data_url(str(pdf), 1).startswith("data:image/png;base64,")
    with pytest.raises(PreviewError):
        workbench.preview_data_url(str(pdf), 2)

    not_pdf = tmp_path / "note.txt"
    not_pdf.write_text("not a pdf", encoding="utf-8")
    with pytest.raises(PreviewError) as error:
        workbench.preview_data_url(str(not_pdf), 0)
    assert str(error.value) == "源文件不可预览"


def test_warning_paths_are_redacted(connection) -> None:
    invoice_id = add_invoice(connection, index=1, number="W1")
    connection.execute(
        "UPDATE invoices SET warnings = ? WHERE id = ?",
        (
            json.dumps(
                [r"无法读取 C:\Users\secret\invoices\a.pdf"], ensure_ascii=False
            ),
            invoice_id,
        ),
    )
    connection.commit()
    row = workbench.list_invoices(connection)[0]
    assert "secret" not in row.warning_text
    assert "<路径已隐藏>" in row.warning_text


def test_update_report_refreshes_auto_title_only(connection) -> None:
    auto_id = make_report(connection)
    reports.update_report(connection, auto_id, company="新公司")
    assert (
        reports.get_report(connection, auto_id).title
        == "新公司7月报销费用支出明细表"
    )

    # The UI echoes the old auto title; a header change still refreshes it.
    reports.update_report(
        connection,
        auto_id,
        company="另一公司",
        title="新公司7月报销费用支出明细表",
    )
    assert (
        reports.get_report(connection, auto_id).title
        == "另一公司7月报销费用支出明细表"
    )

    custom_id = reports.create_report(
        connection,
        company="示例公司",
        reporter="张三",
        month="2026-07",
        title="自定义标题",
    )
    reports.update_report(connection, custom_id, company="新公司")
    reports.update_report(
        connection, custom_id, company="另一公司", title="自定义标题"
    )
    assert reports.get_report(connection, custom_id).title == "自定义标题"


def test_invoice_source_lookup(connection, tmp_path) -> None:
    pdf = make_pdf(tmp_path, [(50, 50, "发票")], name="source.pdf")
    invoice_id = add_invoice(connection, index=1, number="P1", source_path=str(pdf))
    source, page_index, page_count = workbench.invoice_source(connection, invoice_id)
    assert source == str(pdf)
    assert page_index == 0
    assert page_count == 1
    with pytest.raises(PreviewError):
        workbench.invoice_source(connection, 9999)


def test_mileage_rounding_matches_whole_cents() -> None:
    assert reports.mileage_cents(Decimal("1.005")) == 151
    assert reports.mileage_cents(Decimal(150)) == 22500
    # The JS preview uses integer hundredths; 3.33 × 1.5 is 5.00, not 4.99.
    assert reports.mileage_cents(Decimal("3.33")) == 500


def test_dashboard_summary_groups_counts_and_orders(connection, tmp_path) -> None:
    existing = tmp_path / "exists.pdf"
    existing.write_bytes(b"%PDF-1.4")
    july = add_invoice(
        connection,
        index=1,
        number="D1",
        date="2026-07-01",
        category="餐饮费",
        amount=Decimal("10.00"),
        source_path=str(existing),
    )
    add_invoice(
        connection,
        index=2,
        number="D2",
        date="2026-07-02",
        category="餐饮费",
        amount=Decimal("20.00"),
        confidence={"amount_total": "low"},
    )
    add_invoice(
        connection,
        index=3,
        number="D3",
        date="2026-08-03",
        category="交通费",
        amount=None,
    )
    add_invoice(
        connection,
        index=4,
        number="D4",
        date="",
        category="",
        amount=Decimal("5.00"),
    )
    report_id = make_report(connection)
    add_line(connection, report_id, invoice_ids=[july])
    invoices.update_invoice(connection, july, seller_name="改过的销售方")

    summary = workbench.dashboard_summary(workbench.list_invoices(connection))
    assert summary.count == 4
    assert summary.total_cents == 3500
    assert summary.review_count == 2
    assert summary.linked_count == 1
    assert summary.missing_amount_count == 1
    assert summary.missing_source_count == 3
    assert summary.low_confidence_count == 1
    assert summary.edited_count == 1
    assert [
        (item.category, item.count, item.total_cents) for item in summary.categories
    ] == [
        ("餐饮费", 2, 3000),
        ("未分类", 1, 500),
        ("交通费", 1, 0),
    ]
    assert [(item.month, item.count, item.total_cents) for item in summary.months] == [
        ("2026-07", 2, 3000),
        ("2026-08", 1, 0),
        ("未知日期", 1, 500),
    ]

    empty = workbench.dashboard_summary([])
    assert empty.count == 0
    assert empty.total_cents == 0
    assert empty.categories == ()
    assert empty.months == ()


def test_invoice_briefs(connection) -> None:
    invoice_id = add_invoice(connection, index=1, number="B1", amount=Decimal("88.00"))
    briefs = workbench.invoice_briefs(connection, (invoice_id,))
    assert briefs[invoice_id].invoice_number == "B1"
    assert briefs[invoice_id].amount_total_cents == 8800
    assert workbench.invoice_briefs(connection, ()) == {}
    assert db.cents_to_money(briefs[invoice_id].amount_total_cents) == Decimal("88.00")
