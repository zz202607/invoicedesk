"""Rolling database snapshots: create, prune, validate, restore, startup."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import app.main as main_module
from app.api import Api
from app.core import backup, db
from tests.pdf_factory import full_digital_lines, make_pdf
from tests.report_factory import add_invoice

STAMP = datetime(2026, 10, 29, 15, 30, 12)


def _populated_db(tmp_path: Path) -> Path:
    path = tmp_path / "invoicedesk.db"
    connection = db.connect(path)
    try:
        add_invoice(connection, index=1, number="BK1")
    finally:
        connection.close()
    return path


def _count(path: Path, table: str = "invoices") -> int:
    connection = sqlite3.connect(str(path))
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def test_create_backup_writes_a_consistent_copy(tmp_path) -> None:
    path = _populated_db(tmp_path)

    info = backup.create_backup(path, now=STAMP)

    assert info.name == "invoicedesk-20261029-153012-manual.db"
    assert info.reason == "manual"
    assert info.path.is_file()
    assert _count(info.path) == 1
    assert [item.name for item in backup.list_backups(path)] == [info.name]


def test_create_backup_requires_an_existing_database(tmp_path) -> None:
    with pytest.raises(backup.BackupError):
        backup.create_backup(tmp_path / "missing.db")


def test_create_backup_rejects_unknown_reasons(tmp_path) -> None:
    path = _populated_db(tmp_path)
    with pytest.raises(backup.BackupError):
        backup.create_backup(path, reason="../escape")


def test_prune_keeps_newest_and_leaves_foreign_files_alone(tmp_path) -> None:
    path = _populated_db(tmp_path)
    root = backup.backup_dir(path)
    root.mkdir(parents=True, exist_ok=True)
    (root / "notes.txt").write_text("keep me", encoding="utf-8")
    foreign = root / "other-20260101-000000-manual.db"
    foreign.write_bytes(b"x")

    for day in range(4):
        backup.create_backup(
            path, keep=3, now=STAMP + timedelta(days=day), reason="manual"
        )

    names = [item.name for item in backup.list_backups(path)]
    assert len(names) == 3
    assert names == sorted(names, reverse=True)
    assert (root / "notes.txt").is_file()
    assert foreign.is_file()


def test_list_backups_only_matches_its_own_database(tmp_path) -> None:
    path = _populated_db(tmp_path)
    other = tmp_path / "second.db"
    sqlite3.connect(str(other)).close()
    backup.create_backup(other, now=STAMP)

    assert backup.list_backups(path) == []
    assert len(backup.list_backups(other)) == 1


def test_startup_backup_skips_an_empty_pool(tmp_path) -> None:
    path = tmp_path / "empty.db"
    db.connect(path).close()

    assert backup.startup_backup(path, now=STAMP) is None
    assert backup.list_backups(path) == []
    assert backup.startup_backup(tmp_path / "missing.db") is None


def test_startup_backup_snapshots_a_populated_pool(tmp_path) -> None:
    path = _populated_db(tmp_path)

    info = backup.startup_backup(path, now=STAMP)

    assert info is not None
    assert info.reason == "startup"
    assert backup.latest_backup(path).name == info.name


def test_restore_replaces_database_and_keeps_pre_restore_snapshot(tmp_path) -> None:
    path = _populated_db(tmp_path)
    snapshot = backup.create_backup(path, now=STAMP)
    connection = db.connect(path)
    try:
        connection.execute("DELETE FROM invoices")
        connection.commit()
    finally:
        connection.close()
    assert _count(path) == 0

    result = backup.restore_backup(
        path, snapshot.name, now=STAMP + timedelta(minutes=5)
    )

    assert result.restored.name == snapshot.name
    assert result.snapshot is not None
    assert result.snapshot.reason == "pre-restore"
    assert _count(path) == 1
    assert "pre-restore" in [item.reason for item in backup.list_backups(path)]


@pytest.mark.parametrize(
    "name",
    [
        "../escape.db",
        "notes.txt",
        "invoicedesk-20990101-000000-manual.db",
        "",
    ],
)
def test_restore_rejects_names_outside_the_backup_folder(tmp_path, name) -> None:
    path = _populated_db(tmp_path)

    with pytest.raises(backup.BackupError):
        backup.restore_backup(path, name)

    assert _count(path) == 1


def test_restore_rejects_a_corrupt_snapshot(tmp_path) -> None:
    path = _populated_db(tmp_path)
    backup.create_backup(path, now=STAMP)
    junk = backup.backup_dir(path) / "invoicedesk-20261029-160000-manual.db"
    junk.write_bytes(b"not a database")

    with pytest.raises(backup.BackupError):
        backup.restore_backup(path, junk.name)

    assert _count(path) == 1


def test_restore_preserves_an_unreadable_live_database(tmp_path) -> None:
    path = _populated_db(tmp_path)
    snapshot = backup.create_backup(path, now=STAMP)
    path.write_bytes(b"not a database")

    result = backup.restore_backup(path, snapshot.name, now=STAMP)

    assert result.snapshot is not None
    assert result.snapshot.path.read_bytes() == b"not a database"
    assert _count(path) == 1


def test_api_lists_and_creates_backups(tmp_path) -> None:
    api = Api(db_path=tmp_path / "workbench.db", out_dir=tmp_path / "out")
    assert api.bootstrap()["ok"] is True

    created = api.create_backup()
    assert created["ok"] is True
    assert created["data"]["label"] == "手动"

    listing = api.list_backups()
    assert listing["ok"] is True
    assert listing["data"]["backups"][0]["name"] == created["data"]["name"]
    assert listing["data"]["backups"][0]["size"].endswith(("B", "KB", "MB"))
    assert listing["data"]["max"] == backup.MAX_BACKUPS
    assert listing["data"]["dir"] == str(
        backup.backup_dir(tmp_path / "workbench.db")
    )


def test_api_restore_round_trip_and_error_envelope(tmp_path) -> None:
    db_path = tmp_path / "workbench.db"
    api = Api(db_path=db_path, out_dir=tmp_path / "out")
    assert api.bootstrap()["ok"] is True
    connection = db.connect(db_path)
    try:
        add_invoice(connection, index=1, number="API-BK")
    finally:
        connection.close()
    created = api.create_backup()["data"]

    connection = db.connect(db_path)
    try:
        connection.execute("DELETE FROM invoices")
        connection.commit()
    finally:
        connection.close()

    response = api.restore_backup(created["name"])
    assert response["ok"] is True, response
    assert response["data"]["restored"]["name"] == created["name"]
    assert response["data"]["snapshot"]["reason"] == "pre-restore"
    assert api.list_invoices()["data"]["summary"]["count"] == 1

    rejected = api.restore_backup("../escape.db")
    assert rejected["ok"] is False
    assert "备份" in rejected["error"]


def test_import_creates_a_snapshot(tmp_path) -> None:
    api = Api(db_path=tmp_path / "workbench.db", out_dir=tmp_path / "out")
    assert api.bootstrap()["ok"] is True
    folder = tmp_path / "october"
    folder.mkdir()
    make_pdf(folder, full_digital_lines(), name="a.pdf")

    response = api.import_folder(str(folder))

    assert response["ok"] is True, response
    reasons = [item.reason for item in backup.list_backups(tmp_path / "workbench.db")]
    assert reasons == ["import"]


def test_prepare_database_reports_fresh_install(tmp_path) -> None:
    path = tmp_path / "missing.db"

    assert main_module.prepare_database(path) == "fresh"
    assert not path.exists()
    assert not backup.backup_dir(path).exists()


def test_prepare_database_snapshots_before_opening(tmp_path) -> None:
    path = _populated_db(tmp_path)

    assert main_module.prepare_database(path) == "ok"

    assert [item.reason for item in backup.list_backups(path)] == ["startup"]


def test_prepare_database_restores_the_latest_backup(tmp_path) -> None:
    path = _populated_db(tmp_path)
    backup.create_backup(path, now=STAMP)
    path.write_bytes(b"not a database")
    asked: list[str] = []

    def confirm(title: str, message: str) -> bool:
        asked.append(message)
        return True

    assert main_module.prepare_database(path, confirm=confirm) == "restored"

    assert asked and "最近的备份" in asked[0]
    assert _count(path) == 1


def test_prepare_database_declines_when_confirmation_is_no(tmp_path) -> None:
    path = _populated_db(tmp_path)
    backup.create_backup(path, now=STAMP)
    path.write_bytes(b"not a database")

    assert main_module.prepare_database(path, confirm=lambda *_: False) == "unavailable"
    assert path.read_bytes() == b"not a database"


def test_prepare_database_without_a_backup_leaves_the_file_alone(tmp_path) -> None:
    path = tmp_path / "broken.db"
    path.write_bytes(b"not a database")

    assert main_module.prepare_database(path, confirm=lambda *_: True) == "unavailable"
    assert path.read_bytes() == b"not a database"
