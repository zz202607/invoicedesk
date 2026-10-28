"""Learned seller rules: key normalization, confirmation counts, application."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.core import learning
from app.core.models import InvoiceType, ParsedInvoice
from tests.report_factory import make_db


def _invoice(seller: str) -> ParsedInvoice:
    return ParsedInvoice(
        source_path=r"D:\invoices\a.pdf",
        invoice_type=InvoiceType.FULL_DIGITAL_NORMAL,
        seller_name=seller,
        confidence={},
    )


def _rule_count(connection: sqlite3.Connection) -> int:
    return connection.execute("SELECT COUNT(*) FROM category_rules").fetchone()[0]


@pytest.mark.parametrize("seller", ["", "   ", "　"])
def test_blank_sellers_are_never_learned(tmp_path: Path, seller: str) -> None:
    connection = make_db(tmp_path)
    try:
        with connection:
            assert (
                learning.remember_seller_category(connection, seller, "餐饮费") is None
            )
        assert _rule_count(connection) == 0
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("ABC 公司", "ａｂｃ 公司"),
        ("示例乙酒店", " 示例乙酒店 "),
    ],
)
def test_equivalent_seller_spellings_share_one_rule(
    tmp_path: Path, first: str, second: str
) -> None:
    connection = make_db(tmp_path)
    try:
        with connection:
            created = learning.remember_seller_category(connection, first, "餐饮费")
            updated = learning.remember_seller_category(connection, second, "餐饮费")
        assert created is not None and updated is not None
        assert updated.id == created.id
        assert updated.confirm_count == 2
        assert _rule_count(connection) == 1
    finally:
        connection.close()


def test_internal_spaces_are_not_conflated(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        with connection:
            learning.remember_seller_category(connection, "AB 公司", "餐饮费")
            learning.remember_seller_category(connection, "AB公司", "餐饮费")
        assert _rule_count(connection) == 2
    finally:
        connection.close()


def test_changing_the_category_resets_confirmation_count(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        with connection:
            learning.remember_seller_category(connection, "示例乙酒店", "餐饮费")
            learning.remember_seller_category(connection, "示例乙酒店", "餐饮费")
            changed = learning.remember_seller_category(
                connection, "示例乙酒店", "办公费用"
            )
        assert changed is not None
        assert changed.category == "办公费用"
        assert changed.confirm_count == 1
    finally:
        connection.close()


def test_apply_seller_rules_overrides_category_and_marks_confidence() -> None:
    invoice = _invoice("示例乙酒店")
    matched = learning.apply_seller_rules(invoice, {"示例乙酒店": "餐饮费"})
    assert matched == "示例乙酒店"
    assert invoice.category == "餐饮费"
    assert invoice.category_rule == "learned.seller"
    assert invoice.confidence["category"] == "high"

    untouched = _invoice("另一家酒店")
    assert learning.apply_seller_rules(untouched, {"示例乙酒店": "餐饮费"}) is None
    assert untouched.category_rule == "fallback"


def test_record_hit_increments_only_the_matching_rule(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        with connection:
            learning.remember_seller_category(connection, "示例乙酒店", "餐饮费")
            learning.remember_seller_category(connection, "示例丙酒店", "办公费用")
            learning.record_hit(connection, "示例乙酒店")
            learning.record_hit(connection, "示例乙酒店")
            learning.record_hit(connection, "不存在的销售方")
        rows = {
            row["pattern"]: row["hit_count"]
            for row in connection.execute(
                "SELECT pattern, hit_count FROM category_rules"
            )
        }
        assert rows == {"示例乙酒店": 2, "示例丙酒店": 0}
    finally:
        connection.close()
