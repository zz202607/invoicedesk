"""Runtime file locations, WebView2 detection, and local logging."""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
from pathlib import Path

import pytest

from app.api import Api
from app.core import appconfig, logs, runtime
from app.core.paths import repo_root


@pytest.fixture
def clean_logger():
    logger = logging.getLogger(logs.LOGGER_NAME)
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_propagate = logger.propagate
    saved_flag = getattr(logger, "_invoicedesk_configured", None)
    saved_streams = (sys.stdout, sys.stderr)
    saved_hooks = (sys.excepthook, threading.excepthook)
    yield logger
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for handler in saved_handlers:
        logger.addHandler(handler)
    logger.setLevel(saved_level)
    logger.propagate = saved_propagate
    if saved_flag is None:
        with contextlib.suppress(AttributeError):
            del logger._invoicedesk_configured  # type: ignore[attr-defined]
    else:
        logger._invoicedesk_configured = saved_flag  # type: ignore[attr-defined]
    sys.stdout, sys.stderr = saved_streams
    sys.excepthook, threading.excepthook = saved_hooks


def _dev_mode(monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv(runtime.HOME_ENV, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    # A developer's real data/config.json must not leak into these defaults.
    monkeypatch.setattr(appconfig, "load", dict)


def test_source_checkout_keeps_repo_relative_defaults(monkeypatch) -> None:
    _dev_mode(monkeypatch)
    assert runtime.is_frozen() is False
    assert runtime.uses_app_home() is False
    assert runtime.default_db_path() == Path("data") / "invoicedesk.db"
    assert runtime.default_out_dir() == Path("out")
    assert runtime.log_file() == repo_root() / "data" / "logs" / "invoicedesk.log"


def test_packaged_build_uses_localappdata(monkeypatch, tmp_path) -> None:
    _dev_mode(monkeypatch)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    home = tmp_path / runtime.APP_DIR_NAME
    assert runtime.uses_app_home() is True
    assert runtime.app_home() == home
    assert runtime.default_db_path() == home / "invoicedesk.db"
    assert runtime.default_out_dir() == home / "out"
    assert runtime.log_file() == home / "logs" / "invoicedesk.log"


def test_home_override_wins_in_source_checkout(monkeypatch, tmp_path) -> None:
    _dev_mode(monkeypatch)
    monkeypatch.setenv(runtime.HOME_ENV, str(tmp_path / "custom"))
    custom = tmp_path / "custom"
    assert runtime.uses_app_home() is True
    assert runtime.app_home() == custom
    assert runtime.default_db_path() == custom / "invoicedesk.db"
    assert runtime.default_out_dir() == custom / "out"


def test_configured_data_dir_moves_every_default(monkeypatch, tmp_path) -> None:
    _dev_mode(monkeypatch)
    monkeypatch.setattr(appconfig, "load", lambda: {"data_dir": str(tmp_path)})
    assert runtime.default_db_path() == tmp_path / "invoicedesk.db"
    assert runtime.default_out_dir() == tmp_path / "out"
    assert runtime.log_file() == tmp_path / "logs" / "invoicedesk.log"


def test_configured_data_dir_wins_over_the_home_override(
    monkeypatch, tmp_path
) -> None:
    _dev_mode(monkeypatch)
    monkeypatch.setenv(runtime.HOME_ENV, str(tmp_path / "home"))
    monkeypatch.setattr(
        appconfig, "load", lambda: {"data_dir": str(tmp_path / "chosen")}
    )
    assert runtime.default_db_path() == tmp_path / "chosen" / "invoicedesk.db"


def test_relative_configured_data_dir_is_ignored(monkeypatch) -> None:
    _dev_mode(monkeypatch)
    monkeypatch.setattr(appconfig, "load", lambda: {"data_dir": "custom"})
    assert runtime.default_db_path() == Path("data") / "invoicedesk.db"
    assert runtime.default_out_dir() == Path("out")


def test_suggested_data_dir_is_local(monkeypatch, tmp_path) -> None:
    _dev_mode(monkeypatch)
    assert runtime.suggested_data_dir() == repo_root() / "data"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert runtime.suggested_data_dir() == tmp_path / runtime.APP_DIR_NAME


def test_api_defaults_follow_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(runtime, "default_db_path", lambda: tmp_path / "default.db")
    monkeypatch.setattr(runtime, "default_out_dir", lambda: tmp_path / "default-out")
    api = Api()
    assert api._db_path == tmp_path / "default.db"
    assert api._out_dir == tmp_path / "default-out"


def test_resource_path_points_at_the_ui_in_a_checkout(monkeypatch) -> None:
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert runtime.resource_root() == repo_root()
    index = runtime.resource_path("app/ui/index.html")
    assert index == repo_root() / "app" / "ui" / "index.html"
    assert index.is_file()


@pytest.mark.skipif(sys.platform != "win32", reason="WebView2 registry probe is Windows-only")
def test_webview2_probe_uses_the_first_available_registry_key() -> None:
    seen: list[tuple[str, str]] = []

    def probe(hive: str, path: str, name: str) -> str:
        seen.append((hive, path))
        if "WOW6432Node" in path:
            return "156.0.0.0"
        raise OSError(2, "missing")

    assert runtime.webview2_version(probe=probe) == "156.0.0.0"
    assert [hive for hive, _ in seen] == ["HKLM", "HKLM"]
    seen.clear()
    assert runtime.webview2_available(probe=probe) is True
    assert len(seen) == 2


def test_webview2_is_missing_when_no_key_resolves() -> None:
    def probe(hive: str, path: str, name: str) -> str:
        raise OSError(2, "missing")

    assert runtime.webview2_version(probe=probe) is None
    assert runtime.webview2_available(probe=probe) is False


def test_logging_writes_a_rotating_file_and_replaces_handlers(
    clean_logger, tmp_path
) -> None:
    first = logs.setup_logging(tmp_path / "first.log", force=True)
    assert first == tmp_path / "first.log"
    clean_logger.info("first entry")
    for handler in clean_logger.handlers:
        handler.flush()
    assert "first entry" in first.read_text(encoding="utf-8")

    second = logs.setup_logging(tmp_path / "nested" / "second.log", force=True)
    clean_logger.info("second entry")
    for handler in clean_logger.handlers:
        handler.flush()
    assert second is not None
    assert "second entry" in second.read_text(encoding="utf-8")
    assert len(clean_logger.handlers) == 1


def test_logging_falls_back_when_the_directory_cannot_be_created(
    clean_logger, tmp_path
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    assert logs.setup_logging(blocker / "app.log", force=True) is None
    assert len(clean_logger.handlers) == 1


def test_exception_hook_logs_the_traceback_and_calls_the_dialog(
    clean_logger, tmp_path
) -> None:
    target = logs.setup_logging(tmp_path / "errors.log", force=True)
    seen: list[tuple[str, str]] = []
    logs.install_exception_hooks(
        target, lambda title, message: seen.append((title, message))
    )
    try:
        raise ValueError("boom")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    for handler in clean_logger.handlers:
        handler.flush()

    assert target is not None
    text = target.read_text(encoding="utf-8")
    assert "ValueError: boom" in text
    assert seen and seen[0][0] == "发票管家 InvoiceDesk"
    assert str(target) in seen[0][1]
