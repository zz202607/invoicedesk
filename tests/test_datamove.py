"""Moving a packaged app home to a new data folder, verified before the switch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import datamove, db
from app.core.paths import repo_root
from tests.report_factory import add_invoice


def _make_home(tmp_path: Path) -> Path:
    home = tmp_path / "app-home"
    home.mkdir()
    connection = db.connect(home / "invoicedesk.db")
    try:
        add_invoice(connection, index=1, number="MOV1", source_path=r"E:\invoices\a.pdf")
        add_invoice(connection, index=2, number="MOV2", source_path=r"E:\invoices\b.pdf")
    finally:
        connection.close()
    (home / "backups").mkdir()
    (home / "backups" / "invoicedesk-20260101-000000-startup.db").write_bytes(b"snap")
    (home / "out").mkdir()
    (home / "out" / "发票明细.xlsx").write_bytes(b"xlsx")
    (home / "logs").mkdir()
    (home / "logs" / "invoicedesk.log").write_text("log", encoding="utf-8")
    (home / "invoicedesk.db.bak-20260101-000000").write_bytes(b"old")
    (home / "config.json").write_text(
        json.dumps({"welcome_done": True}), encoding="utf-8"
    )
    return home


def test_summarize_reads_counts_without_writing(tmp_path) -> None:
    home = _make_home(tmp_path)
    db_path = home / "invoicedesk.db"
    before = db_path.read_bytes()
    summary = datamove.summarize(db_path)
    assert summary.invoices == 2
    assert summary.batches == 2
    assert summary.archived == 0
    assert summary.integrity == "ok"
    assert db_path.read_bytes() == before


def test_summarize_rejects_a_foreign_database(tmp_path) -> None:
    import sqlite3

    other = tmp_path / "random.db"
    connection = sqlite3.connect(other)
    connection.execute("CREATE TABLE t (id INTEGER)")
    connection.close()
    with pytest.raises(datamove.DataMoveError, match="不是发票管家"):
        datamove.summarize(other)


def test_copy_data_dir_verifies_and_repoints_config(tmp_path) -> None:
    home = _make_home(tmp_path)
    target = tmp_path / "data" / "InvoiceDeskData"

    result = datamove.copy_data_dir(home, target)

    assert result["copied_database"] is True
    assert (target / "invoicedesk.db").is_file()
    assert datamove.summarize(target / "invoicedesk.db").invoices == 2
    assert (target / "out" / "发票明细.xlsx").is_file()
    assert (target / "backups" / "invoicedesk-20260101-000000-startup.db").is_file()
    assert (target / "logs" / "invoicedesk.log").is_file()
    assert (target / "invoicedesk.db.bak-20260101-000000").is_file()
    config = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert config["data_dir"] == str(target)
    assert config["welcome_done"] is True
    # The source copy is intentionally left in place.
    assert (home / "invoicedesk.db").is_file()


def test_copy_data_dir_is_idempotent_for_the_database(tmp_path) -> None:
    home = _make_home(tmp_path)
    target = tmp_path / "InvoiceDeskData"
    datamove.copy_data_dir(home, target)
    (home / "out" / "new.txt").write_text("new", encoding="utf-8")

    second = datamove.copy_data_dir(home, target)

    assert second["copied_database"] is False
    assert (target / "out" / "new.txt").is_file()
    assert datamove.summarize(target / "invoicedesk.db").invoices == 2


def test_copy_data_dir_refuses_a_different_target_database(tmp_path) -> None:
    home = _make_home(tmp_path)
    target = tmp_path / "InvoiceDeskData"
    target.mkdir()
    db.connect(target / "invoicedesk.db").close()

    with pytest.raises(datamove.DataMoveError, match="不同的发票库"):
        datamove.copy_data_dir(home, target)


def test_copy_data_dir_refuses_repository_internals(tmp_path) -> None:
    home = _make_home(tmp_path)
    with pytest.raises(datamove.DataMoveError, match="仓库"):
        datamove.copy_data_dir(home, repo_root() / "app" / "data")
