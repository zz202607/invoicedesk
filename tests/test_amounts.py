from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.amounts import (
    chinese_upper,
    chinese_upper_cents,
    parse_chinese_amount,
    parse_money,
    text_has_negative_amount,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("壹拾叁元整", "13.00"),
        ("壹万贰仟圆整", "12000.00"),
        ("贰佰叁拾肆圆肆角陆分", "234.46"),
        ("柒拾肆元壹角", "74.10"),
        ("叁佰贰拾捌圆陆角零分", "328.60"),
        ("壹仟零伍拾圆", "1050.00"),
        ("⨂ 壹拾叁元整", "13.00"),
    ],
)
def test_parse_chinese_amount(text: str, expected: str) -> None:
    assert parse_chinese_amount(text) == Decimal(expected)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("¥ 12000.00", "12000.00"),
        ("(小写) ¥ 13", "13.00"),
        ("￥74.10", "74.10"),
        ("¥207.49", "207.49"),
    ],
)
def test_parse_money(text: str, expected: str) -> None:
    assert parse_money(text) == Decimal(expected)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "13%",
        "2024  12  04",
        "2024.12.04",
        "-234.46",
        "- 1,060.00",
        "1,060.00-",
        "(1,060.00)",
        "壹万贰仟圆整",
        "0.00",
        "¥100000000.00",
    ],
)
def test_parse_money_rejects_non_amounts(text: str) -> None:
    assert parse_money(text) is None


def test_negative_notation_detection() -> None:
    assert text_has_negative_amount("- 1,060.00") is True
    assert text_has_negative_amount("1,060.00-") is True
    assert text_has_negative_amount("(1,060.00)") is True
    assert text_has_negative_amount("¥ 1,060.00") is False


def test_parse_money_supports_thousands_separator() -> None:
    assert parse_money("¥1,060.00") == Decimal("1060.00")
    assert parse_money("207.49") == Decimal("207.49")


def test_parse_chinese_amount_rejects_garbage() -> None:
    assert parse_chinese_amount("没有金额") is None
    assert parse_chinese_amount("") is None


def test_chinese_amount_uses_same_upper_bound_as_arabic() -> None:
    assert parse_chinese_amount("玖仟玖佰玖拾玖万圆整") == Decimal("99990000.00")
    assert parse_chinese_amount("壹亿圆整") is None
    assert parse_chinese_amount("壹亿贰仟万圆整") is None
    assert parse_chinese_amount("壹佰亿圆整") is None


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("0", "零元整"),
        ("0.05", "零元零伍分"),
        ("0.50", "零元伍角"),
        ("1.00", "壹元整"),
        ("1.05", "壹元零伍分"),
        ("10.00", "壹拾元整"),
        ("15.48", "壹拾伍元肆角捌分"),
        ("100.01", "壹佰元零壹分"),
        ("1001.00", "壹仟零壹元整"),
        ("1010.00", "壹仟零壹拾元整"),
        ("1100.00", "壹仟壹佰元整"),
        ("100010.00", "壹拾万零壹拾元整"),
        ("100000001.00", "壹亿零壹元整"),
        ("123456789.01", "壹亿贰仟叁佰肆拾伍万陆仟柒佰捌拾玖元零壹分"),
        ("2000846.64", "贰佰万零捌佰肆拾陆元陆角肆分"),
        ("-1234.56", "负壹仟贰佰叁拾肆元伍角陆分"),
    ],
)
def test_chinese_upper(amount: str, expected: str) -> None:
    assert chinese_upper(Decimal(amount)) == expected


def test_chinese_upper_rounds_half_up() -> None:
    assert chinese_upper(Decimal("0.005")) == "零元零壹分"
    assert chinese_upper_cents(1548) == "壹拾伍元肆角捌分"
    assert chinese_upper_cents(None) == ""


def test_chinese_upper_roundtrip_with_parser() -> None:
    for amount in (Decimal("13.00"), Decimal("74.10"), Decimal("328.60"), Decimal("12000.00")):
        assert parse_chinese_amount(chinese_upper(amount)) == amount
