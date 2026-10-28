from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.amounts import parse_chinese_amount, parse_money, text_has_negative_amount


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
