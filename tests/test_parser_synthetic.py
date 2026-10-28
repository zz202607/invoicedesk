from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import fitz

from app.core.models import InvoiceType
from app.core.parser import parse_pdf
from tests.pdf_factory import full_digital_lines, make_pdf


def test_full_digital_invoice(tmp_path: Path) -> None:
    path = make_pdf(tmp_path, full_digital_lines())
    invoice = parse_pdf(path)[0]

    assert invoice.invoice_type is InvoiceType.FULL_DIGITAL_NORMAL
    assert invoice.invoice_number == "26502000000000000001"
    assert invoice.invoice_date == "2026-07-30"
    assert invoice.buyer_name == "示例甲公司"
    assert invoice.seller_name == "示例乙酒店"
    assert invoice.amount_total == Decimal("1060.00")
    assert invoice.amount_net == Decimal("1000.00")
    assert invoice.tax_amount == Decimal("60.00")
    assert invoice.item_summary == "住宿费"
    assert invoice.category == "差旅费"
    assert invoice.needs_review is False
    assert invoice.confidence["amount_total"] == "high"
    assert invoice.confidence["seller_name"] == "high"
    assert invoice.confidence["invoice_number"] == "high"
    assert invoice.confidence["invoice_date"] == "high"


def test_old_vat_layout_uses_geometry_not_text_order(tmp_path: Path) -> None:
    lines = [
        (200, 30, "重庆增值税电子普通发票"),
        (430, 20, "发票代码:"),
        (470, 20, "050009900001"),
        (430, 36, "发票号码:"),
        (470, 36, "29000001"),
        (430, 52, "开票日期:"),
        (470, 52, "2026 07 15"),
        (50, 90, "名称:"),
        (108, 90, "示例甲公司"),
        (108, 104, "91500000TEST00001X"),
        (50, 300, "名称:"),
        (108, 300, "示例能源公司"),
        (108, 314, "92500000TEST00002X"),
        (20, 164, "*汽油*92号 车用汽油"),
        (400, 260, "¥100.00"),
        (550, 260, "¥13.00"),
        (60, 260, "合 计"),
        (430, 280, "(小写)"),
        (480, 280, "¥113.00"),
        (180, 280, "壹佰壹拾叁圆整"),
    ]
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]

    assert invoice.invoice_type is InvoiceType.VAT_ELECTRONIC
    assert invoice.invoice_number == "29000001"
    assert invoice.invoice_date == "2026-07-15"
    assert invoice.seller_name == "示例能源公司"
    assert invoice.amount_total == Decimal("113.00")
    assert invoice.amount_net == Decimal("100.00")
    assert invoice.tax_amount == Decimal("13.00")
    assert invoice.item_summary == "92号 车用汽油"
    assert invoice.category == "汽车费用"


def test_train_uses_invoice_date_not_travel_date(tmp_path: Path) -> None:
    lines = [
        (200, 20, "电子发票(铁路电子客票)"),
        (20, 50, "发票号码:24500000000000000001"),
        (20, 135, "2024年11月25日"),
        (430, 50, "开票日期:2024年12月02日"),
        (20, 165, "票价:￥72.00"),
        (90, 85, "示例甲站"),
        (412, 85, "示例乙站"),
        (20, 305, "购买方名称:"),
        (90, 305, "示例甲公司"),
        (360, 305, "统一社会信用代码:91500000TEST00001X"),
    ]
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]

    assert invoice.invoice_type is InvoiceType.TRAIN
    assert invoice.invoice_date == "2024-12-02"
    assert invoice.amount_total == Decimal("72.00")
    assert invoice.seller_name == "中国铁路"
    assert invoice.buyer_name == "示例甲公司"
    assert invoice.item_summary == "铁路客票（示例甲站→示例乙站）"
    assert invoice.category == "差旅费"


def test_scanned_pdf_needs_review(tmp_path: Path) -> None:
    invoice = parse_pdf(make_pdf(tmp_path, []))[0]
    assert invoice.invoice_type is InvoiceType.SCANNED
    assert invoice.needs_review is True


def test_broken_pdf_returns_error_record(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a real pdf")
    invoice = parse_pdf(broken)[0]
    assert invoice.invoice_type is InvoiceType.ERROR
    assert invoice.needs_review is True


def test_multi_invoice_page_is_flagged(tmp_path: Path) -> None:
    lines = full_digital_lines() + [
        (430, 220, "发票号码:"),
        (480, 220, "26502000000000000002"),
        (410, 330, "(小写)"),
        (450, 330, "¥ 1.00"),
    ]
    records = parse_pdf(make_pdf(tmp_path, lines))
    assert len(records) == 1
    assert records[0].page_index == 0
    assert records[0].page_count == 1
    assert "页面可能包含多张发票，需人工拆分" in records[0].warnings


def test_single_right_side_party_is_seller(tmp_path: Path) -> None:
    lines = [
        (200, 20, "电子发票(普通发票)"),
        (430, 40, "发票号码:"),
        (480, 40, "26502000000000000003"),
        (430, 56, "开票日期:"),
        (480, 56, "2026年07月30日"),
        (310, 104, "名称:"),
        (336, 104, "示例乙酒店"),
        (310, 132, "统一社会信用代码/纳税人识别号:"),
        (440, 132, "92500000TEST00002X"),
        (20, 166, "*生产生活服务*住宿费"),
        (400, 268, "¥1000.00"),
        (550, 268, "¥60.00"),
        (60, 267, "合"),
        (100, 267, "计"),
        (50, 287, "价税合计(大写)"),
        (180, 287, "壹仟零陆拾圆整"),
        (410, 287, "(小写)"),
        (450, 287, "¥ 1060.00"),
    ]
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.seller_name == "示例乙酒店"
    assert "仅识别到销售方" in " ".join(invoice.warnings)


def test_special_invoice_type(tmp_path: Path) -> None:
    lines = full_digital_lines(header="电子发票(专用发票)")
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.invoice_type is InvoiceType.FULL_DIGITAL_SPECIAL


def test_toll_invoice_type(tmp_path: Path) -> None:
    lines = full_digital_lines(
        header="重庆增值税电子普通发票",
        item="*经营租赁*通行费",
        seller="示例路桥公司",
    )
    lines.append((180, 150, "车牌号"))
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.invoice_type is InvoiceType.TOLL
    assert invoice.amount_total == Decimal("1060.00")
    assert invoice.category == "汽车费用"


def test_taxi_invoice_type(tmp_path: Path) -> None:
    lines = full_digital_lines(item="*运输服务*客运服务费")
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.invoice_type is InvoiceType.TAXI
    assert invoice.category == "差旅费"


def test_air_itinerary_type(tmp_path: Path) -> None:
    lines = [
        (200, 20, "航空运输电子客票行程单"),
        (430, 40, "发票号码:"),
        (480, 40, "26502000000000000010"),
        (430, 56, "开票日期:"),
        (480, 56, "2026年07月30日"),
        (50, 287, "价税合计(大写)"),
        (180, 287, "伍佰圆整"),
        (410, 287, "(小写)"),
        (450, 287, "¥ 500.00"),
    ]
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.invoice_type is InvoiceType.AIR_ITINERARY
    assert invoice.amount_total == Decimal("500.00")
    assert invoice.needs_review is True


def test_unknown_document_is_flagged(tmp_path: Path) -> None:
    lines = [
        (20, 30, "这是一份普通说明文档，不是发票。"),
        (20, 50, "请不要把它计入报销金额。"),
    ]
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.invoice_type is InvoiceType.UNKNOWN
    assert invoice.needs_review is True


def test_number_without_anchor_is_medium_confidence(tmp_path: Path) -> None:
    lines = [line for line in full_digital_lines() if line[2] != "发票号码:"]
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.invoice_number == "26502000000000000001"
    assert invoice.confidence["invoice_number"] == "medium"
    assert "发票号码按候选推断" in invoice.warnings


def test_scanned_image_page_is_flagged(tmp_path: Path) -> None:
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), 0)
    pixmap.clear_with(200)

    document = fitz.open()
    page = document.new_page(width=400, height=300)
    page.insert_image(fitz.Rect(10, 10, 200, 200), pixmap=pixmap)
    path = tmp_path / "scan.pdf"
    document.save(path)
    document.close()

    invoice = parse_pdf(path)[0]
    assert invoice.invoice_type is InvoiceType.SCANNED
    assert invoice.needs_review is True


def test_single_left_side_party_is_buyer(tmp_path: Path) -> None:
    lines = [
        line
        for line in full_digital_lines()
        if line[2] not in {"名称:", "示例乙酒店", "92500000TEST00002X"}
    ]
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.buyer_name == "示例甲公司"
    assert invoice.seller_name == ""
    assert invoice.confidence["seller_name"] == "low"
    assert "仅识别到一个交易方" in " ".join(invoice.warnings)
    assert invoice.needs_review is True


def test_source_pdf_is_not_modified(tmp_path: Path) -> None:
    path = make_pdf(tmp_path, full_digital_lines())
    before = path.read_bytes()
    parse_pdf(path)
    assert path.read_bytes() == before


def test_multi_page_returns_one_record_per_page(tmp_path: Path) -> None:
    path = make_pdf(tmp_path, full_digital_lines())
    document = fitz.open(path)
    document.new_page(width=800, height=450)
    document.saveIncr()
    document.close()

    records = parse_pdf(path)
    assert [record.page_index for record in records] == [0, 1]
    assert {record.page_count for record in records} == {2}
    assert [record.to_dict()["page"] for record in records] == [1, 2]
