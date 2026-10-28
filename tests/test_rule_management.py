"""Rule management: disable/delete, usage counts, and audited backfill."""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.core import learning
from tests.report_factory import add_invoice, make_db


def _seed_backfill_rows(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        "update": add_invoice(
            connection,
            index=1,
            number="BF1",
            seller="示例乙酒店",
            category="差旅费",
            category_rule="item.travel",
            review=True,
        ),
        "manual": add_invoice(
            connection,
            index=2,
            number="BF2",
            seller="示例乙酒店",
            category="办公费用",
            category_rule=learning.MANUAL_CATEGORY_RULE,
        ),
        "same": add_invoice(
            connection,
            index=3,
            number="BF3",
            seller="示例乙酒店",
            category="餐饮费",
            category_rule="item.meal",
        ),
        "other": add_invoice(
            connection,
            index=4,
            number="BF4",
            seller="其他酒店",
            category="差旅费",
            category_rule="item.travel",
        ),
    }


def test_seller_rules_exclude_disabled_rules(tmp_path) -> None:
    connection = make_db(tmp_path)
    try:
        with connection:
            created = learning.remember_seller_category(
                connection, "示例乙酒店", "餐饮费"
            )
        assert created is not None
        assert learning.seller_rules(connection) == {"示例乙酒店": "餐饮费"}

        with connection:
            disabled = learning.set_rule_enabled(
                connection, created.id, False, now="2026-10-28T10:00:00+00:00"
            )
        assert disabled.enabled is False
        assert disabled.updated_at == "2026-10-28T10:00:00+00:00"
        assert learning.seller_rules(connection) == {}
    finally:
        connection.close()


def test_remember_keeps_an_existing_rule_disabled(tmp_path) -> None:
    connection = make_db(tmp_path)
    try:
        with connection:
            created = learning.remember_seller_category(
                connection, "示例乙酒店", "餐饮费"
            )
        assert created is not None
        with connection:
            learning.set_rule_enabled(connection, created.id, False)
            updated = learning.remember_seller_category(
                connection, "示例乙酒店", "办公费用"
            )
        assert updated is not None
        assert updated.category == "办公费用"
        assert updated.enabled is False
        assert learning.seller_rules(connection) == {}
    finally:
        connection.close()


def test_set_rule_enabled_and_delete_reject_unknown_rules(tmp_path) -> None:
    connection = make_db(tmp_path)
    try:
        with pytest.raises(learning.RuleError):
            learning.set_rule_enabled(connection, 999, True)
        with pytest.raises(learning.RuleError):
            learning.delete_rule(connection, 999)
        with pytest.raises(learning.RuleError):
            learning.get_rule(connection, 999)

        with connection:
            created = learning.remember_seller_category(
                connection, "示例乙酒店", "餐饮费"
            )
        assert created is not None
        learning.delete_rule(connection, created.id)
        assert learning.list_rules(connection) == []
        with pytest.raises(learning.RuleError):
            learning.delete_rule(connection, created.id)
    finally:
        connection.close()


def test_seller_usage_counts_normalized_names_and_ignores_blanks(tmp_path) -> None:
    connection = make_db(tmp_path)
    try:
        add_invoice(connection, index=1, number="U1", seller="ABC 公司")
        add_invoice(connection, index=2, number="U2", seller="ａｂｃ 公司")
        add_invoice(connection, index=3, number="U3", seller="")
        assert learning.seller_usage(connection) == {"abc 公司": 2}
    finally:
        connection.close()


def test_backfill_preview_skips_manual_and_unchanged_rows(tmp_path) -> None:
    connection = make_db(tmp_path)
    try:
        rows = _seed_backfill_rows(connection)
        with connection:
            rule = learning.remember_seller_category(
                connection, "示例乙酒店", "餐饮费"
            )
        assert rule is not None
        preview = learning.preview_backfill(connection, rule.id)
        assert preview.match_count == 3
        assert preview.update_count == 1
        assert preview.unchanged == 1
        assert preview.manual_skipped == 1
        assert [sample.invoice_id for sample in preview.samples] == [rows["update"]]
        assert preview.samples[0].category == "差旅费"
        assert preview.samples[0].amount_total_cents == 12000
    finally:
        connection.close()


def test_backfill_apply_rewrites_only_candidates_with_audit(tmp_path) -> None:
    connection = make_db(tmp_path)
    try:
        rows = _seed_backfill_rows(connection)
        confidence = {
            "amount_total": "low",
            "invoice_date": "high",
            "category": "low",
            "item": "high",
        }
        connection.execute(
            "UPDATE invoices SET confidence = ? WHERE id = ?",
            (json.dumps(confidence), rows["update"]),
        )
        connection.commit()
        with connection:
            rule = learning.remember_seller_category(
                connection, "示例乙酒店", "餐饮费"
            )
        assert rule is not None
        preview = learning.preview_backfill(connection, rule.id)
        result = learning.apply_backfill(
            connection,
            rule.id,
            preview.token,
            now="2026-10-28T11:00:00+00:00",
        )
        assert (result.updated, result.unchanged, result.manual_skipped) == (1, 1, 1)

        changed = connection.execute(
            "SELECT category, category_rule, confidence, needs_review, warnings"
            " FROM invoices WHERE id = ?",
            (rows["update"],),
        ).fetchone()
        assert changed["category"] == "餐饮费"
        assert changed["category_rule"] == learning.LEARNED_SELLER_RULE
        assert json.loads(changed["confidence"]) == {
            **confidence,
            "category": "high",
        }
        assert changed["needs_review"] == 1
        assert "测试待确认" in changed["warnings"]

        manual = connection.execute(
            "SELECT category, category_rule FROM invoices WHERE id = ?",
            (rows["manual"],),
        ).fetchone()
        assert (manual["category"], manual["category_rule"]) == (
            "办公费用",
            learning.MANUAL_CATEGORY_RULE,
        )
        untouched = connection.execute(
            "SELECT category FROM invoices WHERE id IN (?, ?) ORDER BY id",
            (rows["same"], rows["other"]),
        ).fetchall()
        assert [row["category"] for row in untouched] == ["餐饮费", "差旅费"]

        audits = connection.execute(
            "SELECT invoice_id, field, old_value, new_value, edited_at"
            " FROM invoice_edits ORDER BY id"
        ).fetchall()
        assert [
            (
                row["invoice_id"],
                row["field"],
                row["old_value"],
                row["new_value"],
                row["edited_at"],
            )
            for row in audits
        ] == [
            (
                rows["update"],
                "category",
                "差旅费",
                "餐饮费",
                "2026-10-28T11:00:00+00:00",
            ),
            (
                rows["update"],
                "category_rule",
                "item.travel",
                learning.LEARNED_SELLER_RULE,
                "2026-10-28T11:00:00+00:00",
            ),
        ]
    finally:
        connection.close()


def test_backfill_requires_an_enabled_rule(tmp_path) -> None:
    connection = make_db(tmp_path)
    try:
        with connection:
            rule = learning.remember_seller_category(
                connection, "示例乙酒店", "餐饮费"
            )
        assert rule is not None
        with connection:
            learning.set_rule_enabled(connection, rule.id, False)
        with pytest.raises(learning.RuleError, match="停用"):
            learning.preview_backfill(connection, rule.id)
        with pytest.raises(learning.RuleError, match="停用"):
            learning.apply_backfill(connection, rule.id, "token")
    finally:
        connection.close()


@pytest.mark.parametrize(
    "raw",
    ["not-json", "[]", "123", '"str"', "null"],
)
def test_backfill_aborts_the_whole_batch_on_damaged_confidence(
    tmp_path, raw: str
) -> None:
    connection = make_db(tmp_path)
    try:
        # The good row sorts first, so a passing test proves its update was
        # rolled back rather than never attempted.
        good = add_invoice(
            connection,
            index=1,
            number="RB1",
            date="2026-07-12",
            seller="示例乙酒店",
            category="差旅费",
            category_rule="item.travel",
        )
        damaged = add_invoice(
            connection,
            index=2,
            number="RB2",
            date="2026-07-10",
            seller="示例乙酒店",
            category="办公费用",
            category_rule="item.office",
        )
        connection.execute(
            "UPDATE invoices SET confidence = ? WHERE id = ?", (raw, damaged)
        )
        connection.commit()
        with connection:
            rule = learning.remember_seller_category(
                connection, "示例乙酒店", "餐饮费"
            )
        assert rule is not None
        preview = learning.preview_backfill(connection, rule.id)
        assert [sample.invoice_id for sample in preview.samples] == [good, damaged]

        with pytest.raises(learning.RuleError, match="损坏"):
            learning.apply_backfill(connection, rule.id, preview.token)

        categories = connection.execute(
            "SELECT category FROM invoices WHERE id IN (?, ?) ORDER BY id",
            (good, damaged),
        ).fetchall()
        assert [row["category"] for row in categories] == ["差旅费", "办公费用"]
        assert (
            connection.execute("SELECT COUNT(*) FROM invoice_edits").fetchone()[0] == 0
        )
    finally:
        connection.close()


def test_backfill_applies_multiple_candidates_in_one_batch(tmp_path) -> None:
    connection = make_db(tmp_path)
    try:
        first = add_invoice(
            connection,
            index=1,
            number="MB1",
            date="2026-07-12",
            seller="示例乙酒店",
            category="差旅费",
            category_rule="item.travel",
        )
        second = add_invoice(
            connection,
            index=2,
            number="MB2",
            date="2026-07-11",
            seller="示例乙酒店",
            category="办公费用",
            category_rule="item.office",
        )
        with connection:
            rule = learning.remember_seller_category(
                connection, "示例乙酒店", "餐饮费"
            )
        assert rule is not None
        preview = learning.preview_backfill(connection, rule.id)
        result = learning.apply_backfill(connection, rule.id, preview.token)
        assert result.updated == 2

        audits = connection.execute(
            "SELECT invoice_id, field, old_value, new_value FROM invoice_edits"
            " ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in audits] == [
            (first, "category", "差旅费", "餐饮费"),
            (first, "category_rule", "item.travel", learning.LEARNED_SELLER_RULE),
            (second, "category", "办公费用", "餐饮费"),
            (second, "category_rule", "item.office", learning.LEARNED_SELLER_RULE),
        ]
    finally:
        connection.close()


def test_backfill_rejects_a_stale_or_missing_preview_token(tmp_path) -> None:
    connection = make_db(tmp_path)
    try:
        add_invoice(
            connection,
            index=1,
            number="ST1",
            seller="示例乙酒店",
            category="差旅费",
            category_rule="item.travel",
        )
        with connection:
            rule = learning.remember_seller_category(
                connection, "示例乙酒店", "餐饮费"
            )
        assert rule is not None
        preview = learning.preview_backfill(connection, rule.id)

        add_invoice(
            connection,
            index=2,
            number="ST2",
            seller="示例乙酒店",
            category="办公费用",
            category_rule="item.office",
        )
        with pytest.raises(learning.RuleError, match="重新预览"):
            learning.apply_backfill(connection, rule.id, preview.token)
        with pytest.raises(learning.RuleError, match="预览"):
            learning.apply_backfill(connection, rule.id, "")

        assert (
            connection.execute("SELECT COUNT(*) FROM invoice_edits").fetchone()[0] == 0
        )
        categories = connection.execute(
            "SELECT category FROM invoices ORDER BY id"
        ).fetchall()
        assert [row["category"] for row in categories] == ["差旅费", "办公费用"]
    finally:
        connection.close()


def test_backfill_token_binds_candidate_categories(tmp_path) -> None:
    """A category change with the same candidate id set must still fail."""
    connection = make_db(tmp_path)
    try:
        candidate = add_invoice(
            connection,
            index=1,
            number="TC1",
            seller="示例乙酒店",
            category="差旅费",
            category_rule="item.travel",
        )
        with connection:
            rule = learning.remember_seller_category(
                connection, "示例乙酒店", "餐饮费"
            )
        assert rule is not None
        preview = learning.preview_backfill(connection, rule.id)

        # Same id, still a candidate, but the values the user confirmed have
        # changed. The fingerprint must catch it before any write.
        connection.execute(
            "UPDATE invoices SET category = '办公费用', category_rule = 'item.office'"
            " WHERE id = ?",
            (candidate,),
        )
        connection.commit()
        with pytest.raises(learning.RuleError, match="重新预览"):
            learning.apply_backfill(connection, rule.id, preview.token)

        row = connection.execute(
            "SELECT category, category_rule FROM invoices WHERE id = ?",
            (candidate,),
        ).fetchone()
        assert (row["category"], row["category_rule"]) == ("办公费用", "item.office")
        assert (
            connection.execute("SELECT COUNT(*) FROM invoice_edits").fetchone()[0] == 0
        )
    finally:
        connection.close()
