"""Manual invoice corrections: audit trail, review state, learned seller rules."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.core import invoices, learning
from app.core.importer import CONFLICT_WARNING, INCOMPLETE_WARNING
from app.core.models import InvoiceType
from tests.report_factory import add_invoice, make_db


def _row(connection: sqlite3.Connection, invoice_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
    ).fetchone()
    assert row is not None
    return row


def _edits(connection: sqlite3.Connection, invoice_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM invoice_edits WHERE invoice_id = ? ORDER BY id",
        (invoice_id,),
    ).fetchall()


def test_correction_updates_fields_audit_and_confidence(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection, review=True, category="办公费用")
        assert _row(connection, invoice_id)["needs_review"] == 1

        result = invoices.update_invoice(
            connection,
            invoice_id,
            invoice_number=" 2650 2000 ",
            seller_name="改后销售方",
            amount_total_cents=9999,
        )

        row = _row(connection, invoice_id)
        assert row["invoice_number"] == "26502000"
        assert row["seller_name"] == "改后销售方"
        assert row["amount_total_cents"] == 9999
        # Editing does not resolve a parse warning; confirmation is explicit.
        assert row["needs_review"] == 1
        assert json.loads(row["warnings"]) == ["测试待确认"]
        confidence = json.loads(row["confidence"])
        assert confidence["seller_name"] == "high"
        assert confidence["amount_total"] == "high"
        assert result.rule is None

        confirmed = invoices.confirm_invoice(connection, invoice_id)
        assert (confirmed.field, confirmed.old_value, confirmed.new_value) == (
            "review",
            "待确认",
            "已确认",
        )
        assert _row(connection, invoice_id)["needs_review"] == 0

        audit = {edit["field"]: edit for edit in _edits(connection, invoice_id)}
        assert audit["invoice_number"]["old_value"] == "26502000000000000001"
        assert audit["invoice_number"]["new_value"] == "26502000"
        assert audit["seller_name"]["old_value"] == "示例销售方"
        assert audit["seller_name"]["new_value"] == "改后销售方"
        assert audit["amount_total_cents"]["old_value"] == "120.00"
        assert audit["amount_total_cents"]["new_value"] == "99.99"
        assert audit["review"]["new_value"] == "已确认"
        assert all(edit["edited_at"] for edit in audit.values())
    finally:
        connection.close()


def test_noop_and_missing_invoice(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection)
        same = invoices.update_invoice(
            connection,
            invoice_id,
            seller_name="示例销售方",
            amount_total_cents=12000,
        )
        assert same.changed == ()
        empty = invoices.update_invoice(connection, invoice_id)
        assert empty.changed == ()
        with pytest.raises(invoices.InvoiceError, match="发票不存在"):
            invoices.update_invoice(connection, 999, seller_name="x")
        assert _edits(connection, invoice_id) == []
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("invoice_date", "2026/07/09", "日期格式"),
        ("invoice_date", "1989-12-31", "超出范围"),
        ("category", "   ", "分类不能为空"),
        ("amount_total_cents", 99.5, "整数分"),
        ("amount_total_cents", True, "整数分"),
    ],
)
def test_invalid_values_are_rejected(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection)
        with pytest.raises(invoices.InvoiceError, match=message):
            invoices.update_invoice(connection, invoice_id, **{field: value})
        assert _edits(connection, invoice_id) == []
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1990-01-01", "1990-01-01"),
        ("2100-12-31", "2100-12-31"),
        ("2026-8-9", "2026-08-09"),
    ],
)
def test_invoice_dates_are_normalized(
    tmp_path: Path, value: str, expected: str
) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection)
        invoices.update_invoice(connection, invoice_id, invoice_date=value)
        assert _row(connection, invoice_id)["invoice_date"] == expected
    finally:
        connection.close()


def test_date_error_truncates_long_input(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection)
        value = "2026/07/09 " + "x" * 100
        with pytest.raises(invoices.InvoiceError) as excinfo:
            invoices.update_invoice(connection, invoice_id, invoice_date=value)
        message = str(excinfo.value)
        assert "日期格式" in message
        assert "x" * 100 not in message
        assert "…" in message
    finally:
        connection.close()


def test_negative_red_invoice_amount_is_allowed(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection)
        invoices.update_invoice(connection, invoice_id, amount_total_cents=-5000)
        row = _row(connection, invoice_id)
        assert row["amount_total_cents"] == -5000
        assert json.loads(row["confidence"])["amount_total"] == "high"
        edit = _edits(connection, invoice_id)[0]
        assert (edit["old_value"], edit["new_value"]) == ("120.00", "-50.00")
    finally:
        connection.close()


def test_absurd_amount_is_rejected(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection)
        with pytest.raises(invoices.InvoiceError, match="超出可处理范围"):
            invoices.update_invoice(
                connection,
                invoice_id,
                amount_total_cents=invoices.MAX_ABS_AMOUNT_CENTS + 1,
            )
        assert _edits(connection, invoice_id) == []
    finally:
        connection.close()


def test_damaged_warnings_block_write_without_data_loss(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection)
        connection.execute(
            "UPDATE invoices SET warnings = 'not-json' WHERE id = ?", (invoice_id,)
        )
        connection.commit()

        with pytest.raises(invoices.InvoiceError, match="警告数据损坏"):
            invoices.update_invoice(connection, invoice_id, category="餐饮费")
        assert _row(connection, invoice_id)["warnings"] == "not-json"
        assert _edits(connection, invoice_id) == []
    finally:
        connection.close()


def test_damaged_confidence_blocks_write_without_data_loss(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection)
        connection.execute(
            "UPDATE invoices SET confidence = '[]' WHERE id = ?", (invoice_id,)
        )
        connection.commit()

        with pytest.raises(invoices.InvoiceError, match="置信度数据损坏"):
            invoices.update_invoice(connection, invoice_id, category="餐饮费")
        assert _row(connection, invoice_id)["confidence"] == "[]"
        assert _edits(connection, invoice_id) == []
    finally:
        connection.close()


def test_cleared_amount_returns_invoice_to_review(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection)
        invoices.update_invoice(connection, invoice_id, amount_total_cents=None)
        row = _row(connection, invoice_id)
        assert row["amount_total_cents"] is None
        assert row["needs_review"] == 1
        assert json.loads(row["confidence"])["amount_total"] == "low"
        edit = _edits(connection, invoice_id)[0]
        assert (edit["old_value"], edit["new_value"]) == ("120.00", "")
    finally:
        connection.close()


def test_scanned_record_needs_explicit_confirmation(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(
            connection,
            invoice_type=InvoiceType.SCANNED,
            review=True,
        )
        assert _row(connection, invoice_id)["needs_review"] == 1
        invoices.update_invoice(connection, invoice_id, item_summary="人工核对")
        assert _row(connection, invoice_id)["needs_review"] == 1

        invoices.confirm_invoice(connection, invoice_id)
        row = _row(connection, invoice_id)
        assert row["invoice_type"] == InvoiceType.SCANNED.value
        assert row["needs_review"] == 0

        with pytest.raises(invoices.InvoiceError, match="不需要确认"):
            invoices.confirm_invoice(connection, invoice_id)
        with pytest.raises(invoices.InvoiceError, match="发票不存在"):
            invoices.confirm_invoice(connection, 999)
    finally:
        connection.close()


def test_missing_critical_field_keeps_review_until_confirmed(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection, number="", review=True)
        invoices.update_invoice(connection, invoice_id, category="餐饮费")
        assert _row(connection, invoice_id)["needs_review"] == 1

        invoices.update_invoice(connection, invoice_id, invoice_number="FIXED1")
        row = _row(connection, invoice_id)
        assert row["invoice_number"] == "FIXED1"
        # Filling the gap does not self-certify the record; confirm it.
        assert row["needs_review"] == 1

        invoices.confirm_invoice(connection, invoice_id)
        assert _row(connection, invoice_id)["needs_review"] == 0
    finally:
        connection.close()


def test_confirmed_record_stays_confirmed_until_a_critical_field_is_cleared(
    tmp_path: Path,
) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection, review=True)
        invoices.confirm_invoice(connection, invoice_id)

        invoices.update_invoice(connection, invoice_id, item_summary="改项目")
        assert _row(connection, invoice_id)["needs_review"] == 0

        invoices.update_invoice(connection, invoice_id, amount_total_cents=None)
        assert _row(connection, invoice_id)["needs_review"] == 1
    finally:
        connection.close()


def test_category_correction_learns_and_reinforces_seller_rule(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        first = add_invoice(connection, index=1, seller="示例餐饮公司")
        second = add_invoice(connection, index=2, seller="示例餐饮公司")

        result = invoices.update_invoice(connection, first, category="餐饮费")
        assert result.rule is not None
        assert result.rule.pattern == "示例餐饮公司"
        assert result.rule.category == "餐饮费"
        assert result.rule.confirm_count == 1
        assert _row(connection, first)["category_rule"] == "manual"

        again = invoices.update_invoice(connection, second, category="餐饮费")
        assert again.rule is not None
        assert again.rule.confirm_count == 2
        assert learning.seller_rules(connection) == {"示例餐饮公司": "餐饮费"}

        # A different category replaces the rule and resets its confidence count.
        changed = invoices.update_invoice(connection, first, category="差旅费")
        assert changed.rule is not None
        assert changed.rule.category == "差旅费"
        assert changed.rule.confirm_count == 1
        assert learning.seller_rules(connection) == {"示例餐饮公司": "差旅费"}
    finally:
        connection.close()


def test_seller_and_category_edit_learns_new_seller_only(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection)
        result = invoices.update_invoice(
            connection,
            invoice_id,
            seller_name="改后酒店",
            category="业务招待费",
        )
        assert result.rule is not None
        assert result.rule.pattern == "改后酒店"
        rules = connection.execute(
            "SELECT pattern, category FROM category_rules"
        ).fetchall()
        assert [(row["pattern"], row["category"]) for row in rules] == [
            ("改后酒店", "业务招待费")
        ]
    finally:
        connection.close()


def test_empty_seller_never_learns_a_rule(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection, seller="")
        result = invoices.update_invoice(connection, invoice_id, category="餐饮费")
        assert result.rule is None
        assert (
            connection.execute("SELECT COUNT(*) FROM category_rules").fetchone()[0] == 0
        )
    finally:
        connection.close()


def test_editing_number_to_an_identical_record_flags_both_as_duplicate(
    tmp_path: Path,
) -> None:
    connection = make_db(tmp_path)
    try:
        first = add_invoice(connection, index=1, number="DUP1")
        second = add_invoice(connection, index=2, number="DUP2")

        result = invoices.update_invoice(connection, second, invoice_number="DUP1")

        assert result.conflict is True
        for invoice_id in (first, second):
            assert invoices.DUPLICATE_WARNING in json.loads(
                _row(connection, invoice_id)["warnings"]
            )
            assert _row(connection, invoice_id)["needs_review"] == 1
    finally:
        connection.close()


def test_editing_identity_into_a_mismatch_flags_both_as_conflict(
    tmp_path: Path,
) -> None:
    connection = make_db(tmp_path)
    try:
        first = add_invoice(connection, index=1, number="CONF1")
        second = add_invoice(connection, index=2, number="CONF2")

        result = invoices.update_invoice(
            connection,
            second,
            invoice_number="CONF1",
            amount_total_cents=2000,
        )

        assert result.conflict is True
        for invoice_id in (first, second):
            assert CONFLICT_WARNING in json.loads(
                _row(connection, invoice_id)["warnings"]
            )
            assert _row(connection, invoice_id)["needs_review"] == 1
    finally:
        connection.close()


def test_editing_identity_with_missing_fields_flags_incomplete(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        first = add_invoice(connection, index=1, number="INC1")
        second = add_invoice(connection, index=2, number="INC2")

        result = invoices.update_invoice(
            connection, second, invoice_number="INC1", amount_total_cents=None
        )

        assert result.conflict is True
        for invoice_id in (first, second):
            assert INCOMPLETE_WARNING in json.loads(
                _row(connection, invoice_id)["warnings"]
            )
        assert _row(connection, second)["needs_review"] == 1
    finally:
        connection.close()


def test_editing_to_a_unique_number_or_other_fields_adds_no_warning(
    tmp_path: Path,
) -> None:
    connection = make_db(tmp_path)
    try:
        add_invoice(connection, index=1, number="UNIQ1")
        second = add_invoice(connection, index=2, number="UNIQ2")

        unique = invoices.update_invoice(connection, second, invoice_number="UNIQ3")
        assert unique.conflict is False
        assert json.loads(_row(connection, second)["warnings"]) == []
        assert _row(connection, second)["needs_review"] == 0

        category = invoices.update_invoice(connection, second, category="餐饮费")
        assert category.conflict is False
        assert json.loads(_row(connection, second)["warnings"]) == []
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("invoice_number", "1" * 65, "票号最多"),
        ("seller_name", "销" * 201, "销售方最多"),
        ("buyer_name", "购" * 201, "购买方最多"),
        ("category", "类" * 51, "分类最多"),
        ("item_summary", "项" * 1001, "项目最多"),
    ],
)
def test_field_length_limits_are_enforced(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection)
        with pytest.raises(invoices.InvoiceError, match=message):
            invoices.update_invoice(connection, invoice_id, **{field: value})
        assert _edits(connection, invoice_id) == []
    finally:
        connection.close()


def test_clearing_seller_and_changing_category_learns_nothing(
    tmp_path: Path,
) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection, review=True)
        result = invoices.update_invoice(
            connection, invoice_id, seller_name="", category="餐饮费"
        )
        assert result.rule is None
        row = _row(connection, invoice_id)
        assert row["seller_name"] == ""
        assert row["needs_review"] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM category_rules").fetchone()[0] == 0
        )
    finally:
        connection.close()


def test_edit_history_is_newest_first_and_capped(tmp_path: Path) -> None:
    connection = make_db(tmp_path)
    try:
        invoice_id = add_invoice(connection)
        invoices.update_invoice(connection, invoice_id, item_summary="第一次")
        invoices.update_invoice(connection, invoice_id, item_summary="第二次")
        history = invoices.invoice_edits(connection, invoice_id)
        assert [edit.new_value for edit in history] == ["第二次", "第一次"]
        assert len(invoices.invoice_edits(connection, invoice_id, limit=1)) == 1
    finally:
        connection.close()
