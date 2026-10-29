"""Rolling database snapshots: create, prune, validate, restore, startup."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import app.api as api_module
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


def test_second_snapshot_in_the_same_second_gets_a_sequence_suffix(tmp_path) -> None:
    path = _populated_db(tmp_path)

    first = backup.create_backup(path, now=STAMP)
    second = backup.create_backup(path, now=STAMP)

    assert second.name == "invoicedesk-20261029-153012-manual-2.db"
    assert [item.name for item in backup.list_backups(path)] == [
        second.name,
        first.name,
    ]
    assert _count(first.path) == 1 and _count(second.path) == 1


def test_restore_removes_stale_wal_and_shm_sidecars(tmp_path) -> None:
    path = _populated_db(tmp_path)
    snapshot = backup.create_backup(path, now=STAMP)
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    wal.write_bytes(b"stale journal")
    shm.write_bytes(b"stale shm")

    backup.restore_backup(path, snapshot.name, now=STAMP)

    assert not wal.exists()
    assert not shm.exists()
    assert _count(path) == 1


def test_restore_rejects_a_snapshot_from_a_newer_program(tmp_path) -> None:
    path = _populated_db(tmp_path)
    snapshot = backup.create_backup(path, now=STAMP)
    connection = sqlite3.connect(str(snapshot.path))
    try:
        connection.execute(f"PRAGMA user_version = {db.DB_VERSION + 1}")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(backup.BackupError):
        backup.restore_backup(path, snapshot.name)

    assert _count(path) == 1


def test_restore_accepts_a_snapshot_of_the_current_version(tmp_path) -> None:
    path = _populated_db(tmp_path)
    snapshot = backup.create_backup(path, now=STAMP)
    connection = sqlite3.connect(str(snapshot.path))
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db.DB_VERSION
    finally:
        connection.close()

    assert backup.restore_backup(path, snapshot.name).restored.name == snapshot.name


def test_restore_leaves_the_live_database_alone_when_the_copy_fails(
    tmp_path, monkeypatch
) -> None:
    path = _populated_db(tmp_path)
    snapshot = backup.create_backup(path, now=STAMP)
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(backup.shutil, "copyfile", fail)
    with pytest.raises(backup.BackupError):
        backup.restore_backup(path, snapshot.name, now=STAMP)

    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".restore").exists()
    assert [item.name for item in backup.list_backups(path)] == [snapshot.name]


def test_restore_keeps_the_pre_restore_snapshot_when_the_swap_fails(
    tmp_path, monkeypatch
) -> None:
    path = _populated_db(tmp_path)
    snapshot = backup.create_backup(path, now=STAMP)
    before = path.read_bytes()
    real_replace = os.replace

    def locked(source, target, *args, **kwargs):
        if Path(target) == path:
            raise OSError("file is in use")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(backup.os, "replace", locked)
    with pytest.raises(backup.BackupError):
        backup.restore_backup(path, snapshot.name, now=STAMP)

    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".restore").exists()
    assert "pre-restore" in [item.reason for item in backup.list_backups(path)]


def test_restore_prunes_the_oldest_snapshot_to_stay_within_keep(tmp_path) -> None:
    path = _populated_db(tmp_path)
    for day in range(3):
        backup.create_backup(path, keep=3, now=STAMP + timedelta(days=day))
    oldest = backup.list_backups(path)[-1].name
    newest = backup.list_backups(path)[0]

    result = backup.restore_backup(
        path, newest.name, keep=3, now=STAMP + timedelta(days=4)
    )

    names = [item.name for item in backup.list_backups(path)]
    assert len(names) == 3
    assert oldest not in names
    assert result.snapshot is not None
    assert backup.latest_backup(path).reason == "pre-restore"


def test_list_ignores_invalid_dates_and_temporary_files(tmp_path) -> None:
    path = _populated_db(tmp_path)
    root = backup.backup_dir(path)
    root.mkdir(parents=True, exist_ok=True)
    bad_date = root / "invoicedesk-20261341-000000-manual.db"
    bad_date.write_bytes(b"x")
    temp = root / "invoicedesk-20261028-090000-manual.db.tmp"
    temp.write_bytes(b"junk")

    assert backup.list_backups(path) == []
    backup.create_backup(path, keep=1, now=STAMP)

    assert bad_date.is_file()
    assert temp.is_file()
    assert len(backup.list_backups(path)) == 1


def test_create_backup_replaces_a_stale_temp_file(tmp_path) -> None:
    path = _populated_db(tmp_path)
    root = backup.backup_dir(path)
    root.mkdir(parents=True, exist_ok=True)
    stale = root / "invoicedesk-20261029-153012-manual.db.tmp"
    stale.write_bytes(b"junk")

    info = backup.create_backup(path, now=STAMP)

    assert not stale.exists()
    assert _count(info.path) == 1


@pytest.mark.parametrize(
    ("size", "label"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1 KB"),
        (1024 * 1024 - 1, "1024 KB"),
        (1024 * 1024, "1.0 MB"),
        (3 * 1024 * 1024 + 512 * 1024, "3.5 MB"),
    ],
)
def test_file_size_labels(size: int, label: str) -> None:
    assert api_module._file_size(size) == label


@pytest.mark.parametrize("error", [backup.BackupError, OSError])
def test_import_succeeds_when_the_snapshot_fails(tmp_path, monkeypatch, error) -> None:
    api = Api(db_path=tmp_path / "workbench.db", out_dir=tmp_path / "out")
    assert api.bootstrap()["ok"] is True
    folder = tmp_path / "october"
    folder.mkdir()
    make_pdf(folder, full_digital_lines(), name="a.pdf")

    def fail(*args, **kwargs):
        raise error("disk full")

    monkeypatch.setattr(backup, "create_backup", fail)
    response = api.import_folder(str(folder))

    assert response["ok"] is True, response
    assert response["data"]["files_imported"] == 1


def test_reimport_without_new_files_does_not_snapshot(tmp_path) -> None:
    api = Api(db_path=tmp_path / "workbench.db", out_dir=tmp_path / "out")
    assert api.bootstrap()["ok"] is True
    folder = tmp_path / "october"
    folder.mkdir()
    make_pdf(folder, full_digital_lines(), name="a.pdf")

    assert api.import_folder(str(folder))["ok"] is True
    second = api.import_folder(str(folder))

    assert second["ok"] is True
    assert second["data"]["files_imported"] == 0
    reasons = [item.reason for item in backup.list_backups(tmp_path / "workbench.db")]
    assert reasons == ["import"]


@pytest.mark.parametrize("message", ["database is locked", "database is busy"])
def test_prepare_database_reports_a_busy_database_without_restoring(
    tmp_path, monkeypatch, message
) -> None:
    path = _populated_db(tmp_path)
    backup.create_backup(path, now=STAMP)
    asked: list[tuple[str, str]] = []

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(main_module.db, "connect", busy)

    def confirm(title: str, text: str) -> bool:
        asked.append((title, text))
        return True

    assert main_module.prepare_database(path, confirm=confirm) == "busy"
    assert asked == []
    assert _count(path) == 1


def test_prepare_database_offers_a_restore_for_a_newer_database(
    tmp_path, monkeypatch
) -> None:
    path = _populated_db(tmp_path)
    backup.create_backup(path, now=STAMP)

    def newer(*args, **kwargs):
        raise db.DatabaseVersionError("database is newer")

    monkeypatch.setattr(main_module.db, "connect", newer)

    assert main_module.prepare_database(path, confirm=lambda *_: True) == "restored"


def test_prepare_database_propagates_unsafe_paths(tmp_path, monkeypatch) -> None:
    path = _populated_db(tmp_path)

    def unsafe(*args, **kwargs):
        raise db.UnsafeDatabasePathError("inside the repo")

    monkeypatch.setattr(main_module.db, "connect", unsafe)

    with pytest.raises(db.UnsafeDatabasePathError):
        main_module.prepare_database(path)
