from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.core.parser import parse_pdf
from tests.pdf_factory import full_digital_lines, make_pdf


def test_uppercase_mismatch_wins_and_is_flagged(tmp_path: Path) -> None:
    lines = full_digital_lines(small="¥ 900.00", uppercase="壹仟圆整")
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.amount_total == Decimal("1000.00")
    assert invoice.confidence["amount_total"] == "medium"
    assert "小写金额与大写金额不一致" in " ".join(invoice.warnings)
    assert invoice.needs_review is True


def test_net_plus_tax_mismatch_is_flagged(tmp_path: Path) -> None:
    lines = full_digital_lines(net="¥1000.00", tax="¥50.00")
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.amount_total == Decimal("1060.00")
    assert "价税合计与「金额+税额」不一致" in invoice.warnings
    assert invoice.needs_review is True


def test_missing_uppercase_downgrades_confidence(tmp_path: Path) -> None:
    lines = full_digital_lines(uppercase=None)
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.amount_total == Decimal("1060.00")
    assert invoice.confidence["amount_total"] == "medium"
    assert invoice.needs_review is False


def test_fallback_to_max_candidate_is_low_confidence(tmp_path: Path) -> None:
    lines = [
        line
        for line in full_digital_lines(uppercase=None)
        if "¥" not in line[2] and "￥" not in line[2] and "小写" not in line[2] and "价税合计" not in line[2] and line[2] not in {"合", "计"}
    ]
    lines += [(400, 268, "¥100.00"), (550, 268, "¥50.00")]
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.amount_total == Decimal("100.00")
    assert invoice.confidence["amount_total"] == "low"
    assert "金额未找到锚点" in " ".join(invoice.warnings)
    assert invoice.needs_review is True


def test_missing_amount_is_flagged(tmp_path: Path) -> None:
    lines = [
        line
        for line in full_digital_lines(uppercase=None)
        if "¥" not in line[2] and "￥" not in line[2] and "小写" not in line[2] and "价税合计" not in line[2] and line[2] not in {"合", "计"}
    ]
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.amount_total is None
    assert "金额缺失" in invoice.warnings
    assert invoice.needs_review is True


def test_one_cent_rounding_is_tolerated(tmp_path: Path) -> None:
    lines = full_digital_lines(small="¥ 1060.01", uppercase="壹仟零陆拾圆整")
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.amount_total == Decimal("1060.01")
    assert not any("不一致" in warning for warning in invoice.warnings)


def test_ten_cent_mismatch_uses_uppercase(tmp_path: Path) -> None:
    lines = full_digital_lines(small="¥ 1060.10", uppercase="壹仟零陆拾圆整")
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.amount_total == Decimal("1060.00")
    assert invoice.confidence["amount_total"] == "medium"
    assert "小写金额与大写金额不一致" in " ".join(invoice.warnings)


def test_red_invoice_is_not_silently_positive(tmp_path: Path) -> None:
    for notation in ("￥-1,060.00", "1,060.00-", "(1,060.00)"):
        lines = full_digital_lines(small=notation, uppercase="壹仟零陆拾圆整")
        invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
        assert invoice.amount_total is None
        assert "红字/负数发票" in " ".join(invoice.warnings)
        assert invoice.needs_review is True


def test_negative_discount_line_does_not_flag_invoice(tmp_path: Path) -> None:
    lines = full_digital_lines() + [(20, 200, "折扣 -100.00")]
    invoice = parse_pdf(make_pdf(tmp_path, lines))[0]
    assert invoice.amount_total == Decimal("1060.00")
    assert not any("红字" in warning for warning in invoice.warnings)
