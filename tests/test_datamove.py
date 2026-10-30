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


def test_summarize_tolerates_an_old_schema(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE batches (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE invoices (id INTEGER PRIMARY KEY, amount_total_cents INTEGER)"
        )
        connection.execute("PRAGMA user_version = 4")
        connection.execute("INSERT INTO batches DEFAULT VALUES")
        connection.execute("INSERT INTO invoices (amount_total_cents) VALUES (12000)")
        connection.commit()
    finally:
        connection.close()

    summary = datamove.summarize(path)

    assert summary.database_version == 4
    assert summary.invoices == 1
    assert summary.archived == 0
    assert summary.reports == 0
    assert summary.files == 0
    assert summary.amount_cents == 12000


def test_verification_failure_leaves_the_config_untouched(tmp_path, monkeypatch) -> None:
    home = _make_home(tmp_path)
    before = (home / "config.json").read_bytes()
    target = tmp_path / "InvoiceDeskData"

    def broken_copy(source, destination):
        db.connect(destination).close()

    monkeypatch.setattr(datamove, "_copy_database", broken_copy)

    with pytest.raises(datamove.DataMoveError, match="校验不一致"):
        datamove.copy_data_dir(home, target)
    assert (home / "config.json").read_bytes() == before


def _make_legacy(tmp_path: Path) -> Path:
    """A legacy ``InvoiceDeskData`` folder beside the invoices."""
    legacy = tmp_path / "发票" / "InvoiceDeskData"
    legacy.mkdir(parents=True)
    connection = db.connect(legacy / "invoicedesk.db")
    try:
        add_invoice(connection, index=1, number="LEG1", source_path=r"E:\invoices\a.pdf")
    finally:
        connection.close()
    (legacy / "backups").mkdir()
    (legacy / "backups" / "invoicedesk-20260101-000000-startup.db").write_bytes(b"snap")
    (legacy / "out" / "export").mkdir(parents=True)
    (legacy / "out" / "export" / "发票明细.xlsx").write_bytes(b"xlsx")
    (legacy / "out" / "report").mkdir()
    (legacy / "out" / "report" / "报销单.xlsx").write_bytes(b"report")
    (legacy / "out" / "直接文件.xlsx").write_bytes(b"direct")
    (legacy / "out" / "分类合并结果").mkdir()
    (legacy / "out" / "分类合并结果" / "分类.pdf").write_bytes(b"pdf")
    (legacy / "logs").mkdir()
    (legacy / "logs" / "invoicedesk.log").write_text("log", encoding="utf-8")
    return legacy


def test_split_data_dir_moves_the_pool_home_and_leaves_exports(tmp_path) -> None:
    legacy = _make_legacy(tmp_path)
    home = tmp_path / "app-home"
    config = home / "config.json"

    result = datamove.split_data_dir(legacy, home, config_file=config)

    assert result["copied_database"] is True
    assert datamove.summarize(home / "invoicedesk.db").invoices == 1
    assert (home / "backups" / "invoicedesk-20260101-000000-startup.db").is_file()
    assert (home / "logs" / "invoicedesk.log").is_file()
    export = legacy.parent / "报销发票整理"
    # Legacy export/ and report/ wrappers are flattened away.
    assert (export / "发票明细.xlsx").read_bytes() == b"xlsx"
    assert (export / "报销单.xlsx").read_bytes() == b"report"
    assert (export / "直接文件.xlsx").read_bytes() == b"direct"
    assert (export / "分类合并结果" / "分类.pdf").read_bytes() == b"pdf"
    assert not (export / "export").exists()
    assert not (export / "report").exists()
    assert not (home / "out").exists()
    values = json.loads(config.read_text(encoding="utf-8"))
    assert values["data_dir"] == str(home.resolve())
    assert values["export_dir"] == str(export.resolve())
    # The legacy folder is left untouched until the user confirms deletion.
    assert (legacy / "invoicedesk.db").is_file()
    assert (legacy / "out" / "export" / "发票明细.xlsx").is_file()
    assert (legacy / "backups" / "invoicedesk-20260101-000000-startup.db").is_file()


def test_split_data_dir_dry_run_writes_nothing(tmp_path) -> None:
    legacy = _make_legacy(tmp_path)
    home = tmp_path / "app-home"

    result = datamove.split_data_dir(legacy, home, dry_run=True)

    assert result["dry_run"] is True
    assert result["summary"]["invoices"] == 1
    assert not home.exists()
    assert not (legacy.parent / "报销发票整理").exists()


def test_split_data_dir_is_idempotent(tmp_path) -> None:
    legacy = _make_legacy(tmp_path)
    home = tmp_path / "app-home"
    config = home / "config.json"
    datamove.split_data_dir(legacy, home, config_file=config)

    second = datamove.split_data_dir(legacy, home, config_file=config)

    assert second["copied_database"] is False
    assert second["copied_home"] == []
    assert second["copied_export"] == []
    assert datamove.summarize(home / "invoicedesk.db").invoices == 1


def test_split_data_dir_refuses_a_different_existing_pool(tmp_path) -> None:
    legacy = _make_legacy(tmp_path)
    home = tmp_path / "app-home"
    home.mkdir()
    db.connect(home / "invoicedesk.db").close()

    with pytest.raises(datamove.DataMoveError, match="不同的发票库"):
        datamove.split_data_dir(legacy, home)


def test_split_data_dir_refuses_to_overwrite_a_different_export(tmp_path) -> None:
    legacy = _make_legacy(tmp_path)
    export = legacy.parent / "报销发票整理"
    export.mkdir()
    (export / "发票明细.xlsx").write_bytes(b"different")

    with pytest.raises(datamove.DataMoveError, match="不同文件"):
        datamove.split_data_dir(legacy, tmp_path / "app-home", export_dir=export)


def test_split_data_dir_conflict_leaves_config_untouched(tmp_path) -> None:
    legacy = _make_legacy(tmp_path)
    home = tmp_path / "app-home"
    home.mkdir()
    config = home / "config.json"
    config.write_text(json.dumps({"data_dir": "old"}), encoding="utf-8")
    before = config.read_bytes()
    backup = home / "backups" / "invoicedesk-20260101-000000-startup.db"
    backup.parent.mkdir()
    backup.write_bytes(b"different")

    with pytest.raises(datamove.DataMoveError, match="不同文件"):
        datamove.split_data_dir(legacy, home, config_file=config)
    assert config.read_bytes() == before
    assert backup.read_bytes() == b"different"

    backup.unlink()
    again = datamove.split_data_dir(legacy, home, config_file=config)
    assert again["copied_home"]
    assert backup.read_bytes() == b"snap"


def test_split_data_dir_dry_run_reports_a_non_database_conflict(tmp_path) -> None:
    legacy = _make_legacy(tmp_path)
    home = tmp_path / "app-home"
    backup = home / "backups" / "invoicedesk-20260101-000000-startup.db"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"different")

    with pytest.raises(datamove.DataMoveError, match="不同文件"):
        datamove.split_data_dir(legacy, home, dry_run=True)


def test_split_data_dir_dry_run_reports_an_export_conflict(tmp_path) -> None:
    legacy = _make_legacy(tmp_path)
    export = legacy.parent / "报销发票整理"
    export.mkdir()
    (export / "发票明细.xlsx").write_bytes(b"different")

    with pytest.raises(datamove.DataMoveError, match="不同文件"):
        datamove.split_data_dir(legacy, tmp_path / "app-home", dry_run=True)


def test_split_data_dir_dry_run_reports_a_different_pool(tmp_path) -> None:
    legacy = _make_legacy(tmp_path)
    home = tmp_path / "app-home"
    home.mkdir()
    db.connect(home / "invoicedesk.db").close()

    with pytest.raises(datamove.DataMoveError, match="不同的发票库"):
        datamove.split_data_dir(legacy, home, dry_run=True)


def test_split_data_dir_refuses_nested_folders(tmp_path) -> None:
    legacy = _make_legacy(tmp_path)

    with pytest.raises(datamove.DataMoveError, match="互相包含"):
        datamove.split_data_dir(legacy, legacy / "app-home")
    with pytest.raises(datamove.DataMoveError, match="互相包含"):
        datamove.split_data_dir(legacy, tmp_path / "app-home", export_dir=tmp_path)


def test_split_data_dir_refuses_a_sync_folder(tmp_path) -> None:
    legacy = _make_legacy(tmp_path)

    with pytest.raises(datamove.DataMoveError, match="同步目录"):
        datamove.split_data_dir(legacy, tmp_path / "Dropbox" / "pools")


def test_split_data_dir_failure_leaves_config_untouched(tmp_path, monkeypatch) -> None:
    legacy = _make_legacy(tmp_path)
    home = tmp_path / "app-home"
    home.mkdir()
    config = home / "config.json"
    config.write_text(json.dumps({"data_dir": "old"}), encoding="utf-8")
    before = config.read_bytes()

    def fail(source, destination):
        raise OSError("denied")

    monkeypatch.setattr(datamove.shutil, "copy2", fail)

    with pytest.raises(OSError):
        datamove.split_data_dir(legacy, home, config_file=config)
    assert config.read_bytes() == before


def test_split_data_dir_honors_an_explicit_export_folder(tmp_path) -> None:
    legacy = _make_legacy(tmp_path)
    home = tmp_path / "app-home"
    export = tmp_path / "成果"

    result = datamove.split_data_dir(legacy, home, export_dir=export)

    assert result["export"] == str(export.resolve())
    assert (export / "发票明细.xlsx").is_file()
    values = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert values["export_dir"] == str(export.resolve())


def test_split_data_dir_requires_a_source_database(tmp_path) -> None:
    empty = tmp_path / "InvoiceDeskData"
    empty.mkdir()

    with pytest.raises(datamove.DataMoveError, match="invoicedesk.db"):
        datamove.split_data_dir(empty, tmp_path / "app-home")


def test_copy_handles_an_open_wal_source(tmp_path) -> None:
    home = tmp_path / "app-home"
    home.mkdir()
    connection = db.connect(home / "invoicedesk.db")
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        add_invoice(connection, index=1, number="WAL1", source_path=r"E:\invoices\wal.pdf")
        target = tmp_path / "InvoiceDeskData"

        datamove.copy_data_dir(home, target)

        summary = datamove.summarize(target / "invoicedesk.db")
        assert summary.invoices == 1
        assert summary.integrity == "ok"
    finally:
        connection.close()
