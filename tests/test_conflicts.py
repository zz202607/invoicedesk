"""Duplicate/same-number review: grouping, archiving, restoring, exclusions."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.api import Api
from app.core import db, invoices, learning, reports, workbench
from app.core.export import ExportFilters, collect_rows
from app.core.importer import CONFLICT_WARNING
from app.core.reports import ReportError
from tests.report_factory import add_invoice, add_line, make_db, make_report


@pytest.fixture
def connection(tmp_path):
    conn = make_db(tmp_path)
    yield conn
    conn.close()


def _row(connection, invoice_id: int):
    row = connection.execute(
        "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
    ).fetchone()
    assert row is not None
    return row


def _warnings(connection, invoice_id: int) -> list[str]:
    return json.loads(_row(connection, invoice_id)["warnings"])


def _pair(connection):
    """Two active records sharing one number but with different identity."""
    first = add_invoice(
        connection,
        index=1,
        number="N1",
        date="2026-07-01",
        seller="甲销售方",
        amount=Decimal("10.00"),
    )
    second = add_invoice(
        connection,
        index=2,
        number="N1",
        date="2026-07-02",
        seller="乙销售方",
        amount=Decimal("20.00"),
    )
    for invoice_id in (first, second):
        db.append_warning(connection, invoice_id, CONFLICT_WARNING)
    connection.commit()
    return first, second


def test_conflict_groups_classify_duplicates_and_conflicts(connection) -> None:
    duplicate_a = add_invoice(
        connection, index=1, number="D1", date="2026-07-01", seller="甲"
    )
    add_invoice(connection, index=2, number="D1", date="2026-07-01", seller="甲")
    add_invoice(connection, index=3, number="C1", date="2026-07-01", seller="甲")
    add_invoice(connection, index=4, number="C1", date="2026-07-02", seller="甲")
    add_invoice(connection, index=5, number="ONLY1")

    groups = workbench.list_conflict_groups(connection)
    kinds = {group.invoice_number: group.kind for group in groups}
    assert kinds == {"D1": "duplicate", "C1": "conflict"}
    assert all(len(group.rows) >= 2 for group in groups)

    # Archiving resolves one group without touching the other number.
    conflict_group = next(group for group in groups if group.invoice_number == "C1")
    run_archive = [
        row.id for row in conflict_group.rows if row.id != conflict_group.rows[0].id
    ]
    invoices.resolve_conflicts(
        connection, conflict_group.rows[0].id, run_archive
    )
    assert [
        group.invoice_number for group in workbench.list_conflict_groups(connection)
    ] == ["D1"]
    # A single remaining record is not a conflict.
    kept = conflict_group.rows[0].id
    assert workbench.get_invoice_row(connection, kept) is not None
    assert duplicate_a


def test_resolve_archives_duplicates_and_audits_both_sides(connection) -> None:
    keep, archive = _pair(connection)
    result = invoices.resolve_conflicts(connection, keep, [archive, archive])

    assert result.kept_id == keep
    assert result.archived_ids == (archive,)
    assert CONFLICT_WARNING in result.cleared_warnings

    archived = _row(connection, archive)
    assert archived["archived_at"]
    assert CONFLICT_WARNING not in _warnings(connection, archive)
    assert [row.id for row in workbench.list_invoices(connection)] == [keep]
    assert [row.id for row in workbench.list_invoices(connection, status="archived")] == [
        archive
    ]
    assert {
        row.id for row in workbench.list_invoices(connection, status="all")
    } == {keep, archive}

    kept = _row(connection, keep)
    assert CONFLICT_WARNING not in _warnings(connection, keep)
    # Resolution is not a silent confirmation: the record stays in review.
    assert kept["needs_review"] == 1

    archive_audit = connection.execute(
        "SELECT field, old_value, new_value FROM invoice_edits"
        " WHERE invoice_id = ? AND field = 'archive'",
        (archive,),
    ).fetchone()
    assert archive_audit["old_value"] == "保留"
    assert f"#{keep}" in archive_audit["new_value"]
    keep_audit = connection.execute(
        "SELECT new_value FROM invoice_edits"
        " WHERE invoice_id = ? AND field = 'conflict'",
        (keep,),
    ).fetchone()
    assert f"#{archive}" in keep_audit["new_value"]


def test_resolve_validation_rejects_bad_targets(connection) -> None:
    keep, archive = _pair(connection)
    other = add_invoice(connection, index=9, number="OTHER")

    with pytest.raises(invoices.InvoiceError, match="请选择"):
        invoices.resolve_conflicts(connection, keep, [])
    with pytest.raises(invoices.InvoiceError, match="保留的发票不能"):
        invoices.resolve_conflicts(connection, keep, [keep])
    with pytest.raises(invoices.InvoiceError, match="票号相同"):
        invoices.resolve_conflicts(connection, keep, [other])
    with pytest.raises(invoices.InvoiceError, match="没有票号"):
        invoices.resolve_conflicts(
            connection,
            add_invoice(connection, index=10, number=""),
            [archive],
        )

    invoices.resolve_conflicts(connection, keep, [archive])
    with pytest.raises(invoices.InvoiceError, match="已归档"):
        invoices.resolve_conflicts(connection, keep, [archive])
    with pytest.raises(invoices.InvoiceError, match="已归档"):
        invoices.resolve_conflicts(connection, archive, [keep])


def test_resolve_refuses_to_archive_a_linked_invoice(connection) -> None:
    keep, archive = _pair(connection)
    report_id = make_report(connection)
    add_line(connection, report_id, invoice_ids=[archive])

    with pytest.raises(invoices.InvoiceError, match="已关联报销单"):
        invoices.resolve_conflicts(connection, keep, [archive])
    assert _row(connection, archive)["archived_at"] == ""


def test_restore_reflags_remaining_conflict(connection) -> None:
    keep, archive = _pair(connection)
    invoices.resolve_conflicts(connection, keep, [archive])

    result = invoices.restore_invoice(connection, archive)
    assert result.invoice_id == archive
    assert result.conflict_warning == CONFLICT_WARNING

    restored = _row(connection, archive)
    assert restored["archived_at"] == ""
    assert restored["needs_review"] == 1
    assert CONFLICT_WARNING in _warnings(connection, archive)
    # The peer is flagged again so the pair is visible from either side.
    assert CONFLICT_WARNING in _warnings(connection, keep)
    assert _row(connection, keep)["needs_review"] == 1

    audit = connection.execute(
        "SELECT old_value, new_value FROM invoice_edits"
        " WHERE invoice_id = ? AND field = 'archive' ORDER BY id DESC",
        (archive,),
    ).fetchone()
    assert audit["old_value"] == "归档"
    assert audit["new_value"].startswith("恢复")

    with pytest.raises(invoices.InvoiceError, match="没有归档"):
        invoices.restore_invoice(connection, archive)


def test_archived_rows_never_leak_into_reads_or_exports(connection) -> None:
    keep, archive = _pair(connection)
    reports.add_line(
        connection,
        make_report(connection),
        line_date="2026-07-01",
        category="办公费用",
        description="测试",
        amount_cents=1000,
    )
    invoices.resolve_conflicts(connection, keep, [archive])

    assert [row.id for row in collect_rows(connection, ExportFilters())] == [keep]
    assert [
        row.id for row in workbench.list_invoices(connection, review_only=True)
    ] == [keep]
    assert db.invoice_index(connection)["N1"] == [
        (keep, ("2026-07-01", 1000, "甲销售方"))
    ]
    assert learning.seller_usage(connection).get(learning.seller_key("乙销售方"), 0) == 0
    stats = db.batch_stats(connection)
    assert sum(stat.invoice_count for stat in stats) == 1

    # The archived record cannot be linked to a new report or turned into a line.
    with pytest.raises(ReportError, match="发票不存在"):
        reports.add_line(
            connection,
            make_report(connection, month="2026-08"),
            line_date="2026-07-02",
            category="办公费用",
            description="重复票",
            amount_cents=2000,
            invoice_ids=[archive],
        )
    from_invoices = reports.from_invoices(
        connection, make_report(connection, month="2026-09")
    )
    assert from_invoices.added == 1


def test_archived_rows_are_skipped_by_rule_backfill(connection) -> None:
    keep, archive = _pair(connection)
    invoices.resolve_conflicts(connection, keep, [archive])
    rule = learning.remember_seller_category(
        connection, "乙销售方", "餐饮费"
    )
    preview = learning.preview_backfill(connection, rule.id)
    assert all(sample.invoice_id != archive for sample in preview.samples)


def test_api_exposes_conflict_review_endpoints(tmp_path: Path) -> None:
    api = Api(db_path=tmp_path / "workbench.db", out_dir=tmp_path / "out")
    connection = db.connect(tmp_path / "workbench.db")
    try:
        keep, archive = _pair(connection)
    finally:
        connection.close()

    boot = api.bootstrap()
    assert boot["ok"] is True
    assert boot["data"]["conflict_group_count"] == 1

    listing = api.list_conflicts()
    assert listing["ok"] is True
    group = listing["data"]["groups"][0]
    assert group["invoice_number"] == "N1"
    assert group["kind"] == "conflict"
    assert {row["id"] for row in group["invoices"]} == {keep, archive}
    assert listing["data"]["archived"] == []

    resolved = api.resolve_conflict(keep, [archive])
    assert resolved["ok"] is True, resolved
    assert resolved["data"]["archived_ids"] == [archive]
    assert resolved["data"]["invoice"]["id"] == keep
    assert api.bootstrap()["data"]["conflict_group_count"] == 0

    after = api.list_conflicts()
    assert after["data"]["groups"] == []
    assert [row["id"] for row in after["data"]["archived"]] == [archive]

    restored = api.restore_invoice(archive)
    assert restored["ok"] is True
    assert restored["data"]["conflict_warning"] == CONFLICT_WARNING
    assert restored["data"]["invoice"]["archived_at"] == ""

    bad = api.resolve_conflict(keep, [keep])
    assert bad["ok"] is False
    assert "归档列表" in bad["error"]


def test_identity_edit_ignores_archived_peer(connection) -> None:
    keep, archive = _pair(connection)
    invoices.resolve_conflicts(connection, keep, [archive])

    result = invoices.update_invoice(connection, keep, seller_name="丙销售方")
    assert result.conflict is False
    assert CONFLICT_WARNING not in _warnings(connection, keep)
    assert _row(connection, archive)["archived_at"]
