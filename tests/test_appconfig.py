"""Settings that live outside the database: location, data folder, welcome flag."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.core import appconfig, runtime
from app.core.paths import repo_root


def _dev_mode(monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv(runtime.HOME_ENV, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    monkeypatch.setattr(appconfig, "config_path", lambda: path)
    return path


def test_source_checkout_keeps_settings_under_data(monkeypatch) -> None:
    _dev_mode(monkeypatch)
    assert appconfig.config_path() == repo_root() / "data" / "config.json"


def test_packaged_build_puts_settings_beside_the_database(monkeypatch, tmp_path) -> None:
    _dev_mode(monkeypatch)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert (
        appconfig.config_path()
        == tmp_path / runtime.APP_DIR_NAME / appconfig.CONFIG_FILENAME
    )


def test_home_override_owns_the_settings_file(monkeypatch, tmp_path) -> None:
    _dev_mode(monkeypatch)
    home = tmp_path / "portable"
    monkeypatch.setenv(runtime.HOME_ENV, str(home))
    assert appconfig.config_path() == home / appconfig.CONFIG_FILENAME


def test_save_and_load_round_trip_a_chinese_path(cfg, tmp_path) -> None:
    data_dir = tmp_path / "发票数据"
    appconfig.save({"data_dir": str(data_dir), "welcome_done": True})
    assert json.loads(cfg.read_text(encoding="utf-8"))["data_dir"] == str(data_dir)
    assert appconfig.load() == {"data_dir": str(data_dir), "welcome_done": True}


def test_corrupt_or_non_object_settings_read_as_empty(cfg) -> None:
    cfg.write_text("{not json", encoding="utf-8")
    assert appconfig.load() == {}
    cfg.write_text("[1, 2]", encoding="utf-8")
    assert appconfig.load() == {}
    cfg.unlink()
    assert appconfig.load() == {}


def test_data_dir_ignores_relative_and_non_string_values(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(appconfig, "load", lambda: {"data_dir": str(tmp_path)})
    assert appconfig.data_dir() == tmp_path
    monkeypatch.setattr(appconfig, "load", lambda: {"data_dir": "relative/data"})
    assert appconfig.data_dir() is None
    monkeypatch.setattr(appconfig, "load", lambda: {"data_dir": 42})
    assert appconfig.data_dir() is None
    monkeypatch.setattr(appconfig, "load", dict)
    assert appconfig.data_dir() is None


def test_data_dir_normalizes_dot_segments(monkeypatch, tmp_path) -> None:
    messy = str(tmp_path / "data" / ".." / "data")
    monkeypatch.setattr(appconfig, "load", lambda: {"data_dir": messy})
    assert appconfig.data_dir() == tmp_path / "data"


def test_mark_welcome_done_preserves_other_settings(cfg, tmp_path) -> None:
    appconfig.save({"data_dir": str(tmp_path), "extra": 1})
    appconfig.mark_welcome_done()
    data = appconfig.load()
    assert data == {"data_dir": str(tmp_path), "extra": 1, "welcome_done": True}
    assert appconfig.welcome_done() is True


def test_remember_data_dir_keeps_the_welcome_flag(cfg, tmp_path) -> None:
    appconfig.save({"welcome_done": True})
    appconfig.remember_data_dir(tmp_path)
    assert appconfig.data_dir() == tmp_path
    assert appconfig.welcome_done() is True


def test_resolve_data_dir_rejects_blank_and_relative_paths() -> None:
    with pytest.raises(appconfig.ConfigError, match="选择"):
        appconfig.resolve_data_dir("")
    with pytest.raises(appconfig.ConfigError, match="完整路径"):
        appconfig.resolve_data_dir("发票数据")


def test_resolve_data_dir_strips_quotes_from_a_pasted_path(tmp_path) -> None:
    target = tmp_path / "发票数据"
    assert appconfig.resolve_data_dir(f'"{target}"') == target.resolve()


def test_resolve_data_dir_rejects_repository_internals() -> None:
    with pytest.raises(appconfig.ConfigError, match="仓库"):
        appconfig.resolve_data_dir(str(repo_root() / "app"))
    with pytest.raises(appconfig.ConfigError, match="仓库"):
        appconfig.resolve_data_dir(str(repo_root()))


def test_resolve_data_dir_rejects_synced_storage(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(appconfig, "remote_storage_reason", lambda path: "云同步目录")
    with pytest.raises(appconfig.ConfigError, match="本机固定盘"):
        appconfig.resolve_data_dir(str(tmp_path / "发票数据"))


def test_resolve_data_dir_rejects_a_file(tmp_path) -> None:
    target = tmp_path / "not-a-folder"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(appconfig.ConfigError, match="不是文件夹"):
        appconfig.resolve_data_dir(str(target))


def test_ensure_writable_dir_creates_nested_folders(tmp_path) -> None:
    target = tmp_path / "a" / "b"
    assert appconfig.ensure_writable_dir(target) == target
    assert target.is_dir()
    assert not list(target.glob(".invoicedesk-write-*"))


def test_ensure_writable_dir_reports_an_impossible_target(tmp_path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(appconfig.ConfigError, match="写入"):
        appconfig.ensure_writable_dir(blocker / "child")


def test_settings_survive_a_corrupt_write(tmp_path, monkeypatch) -> None:
    target = Path(tmp_path) / "config.json"
    monkeypatch.setattr(appconfig, "config_path", lambda: target)
    appconfig.save({"welcome_done": True})
    # A stale temp file from an interrupted write must not win.
    (tmp_path / "config.json.tmp").write_text("junk", encoding="utf-8")
    appconfig.save({"welcome_done": False})
    assert appconfig.load() == {"welcome_done": False}


def test_data_dir_for_import_creates_a_subfolder(tmp_path) -> None:
    folder = tmp_path / "2026年10月" / "发票"
    folder.mkdir(parents=True)
    expected = (folder / appconfig.IMPORT_DATA_SUBDIR).resolve()
    assert appconfig.data_dir_for_import(folder) == expected
    assert appconfig.data_dir_for_import(str(folder)) == expected


def test_data_dir_for_import_uses_the_parent_of_a_file(tmp_path) -> None:
    folder = tmp_path / "invoices"
    folder.mkdir()
    pdf = folder / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert (
        appconfig.data_dir_for_import(pdf)
        == (folder / appconfig.IMPORT_DATA_SUBDIR).resolve()
    )


def test_known_sync_folder_matches_only_the_named_directory(tmp_path) -> None:
    assert appconfig.known_sync_folder(tmp_path / "Dropbox" / "发票") == "Dropbox"
    assert (
        appconfig.known_sync_folder(tmp_path / "Google Drive" / "发票")
        == "Google Drive"
    )
    assert appconfig.known_sync_folder(tmp_path / "发票" / "dropbox") == "dropbox"
    assert appconfig.known_sync_folder(tmp_path / "发票") is None
