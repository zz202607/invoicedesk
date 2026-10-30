"""Persistent choices that cannot live inside the invoice database.

The database location is itself a setting, so it is stored beside the
application instead: a small JSON file in the per-user app directory for
packaged builds, or ``data/config.json`` in a source checkout. The file holds a
local path and a first-run flag only — never invoice data.

Resolution order for the database, exports, and logs:

1. ``INVOICEDESK_DB`` / ``INVOICEDESK_OUT`` environment variables (read by
   :mod:`app.main`),
2. ``data_dir`` in this file,
3. the app home (``INVOICEDESK_HOME`` or ``%LOCALAPPDATA%\\InvoiceDesk``) for
   packaged builds, ``data/`` and ``out/`` for a source checkout.

Switching directories is only offered while the database has no batches and no
reports, so it can never orphan existing data. Picking a folder that already
contains ``invoicedesk.db`` adopts it — that is the reinstall/restore path.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.core import runtime
from app.core.paths import is_unsafe_repo_path, remote_storage_reason, repo_root

CONFIG_FILENAME = "config.json"
DATA_DIR_KEY = "data_dir"
WELCOME_DONE_KEY = "welcome_done"
# Created inside the first import folder when no data folder was pinned. The
# pool, backups, exports, and logs live there, never beside the source PDFs.
IMPORT_DATA_SUBDIR = "InvoiceDeskData"
DB_FILENAME = "invoicedesk.db"

# Sync clients that do not set an environment root (unlike OneDrive). Only the
# automatic adoption of an import folder consults this list; a folder the user
# picks by hand is still checked by :func:`resolve_data_dir` alone.
KNOWN_SYNC_DIR_NAMES = {
    "dropbox",
    "google drive",
    "googledrive",
    "baidusyncdisk",
    "nutstore",
}


class ConfigError(ValueError):
    """A user-facing settings problem the API reports as a message."""


def config_path() -> Path:
    """Where the settings file lives; never inside a chosen data directory."""
    if runtime.uses_app_home():
        return runtime.app_home() / CONFIG_FILENAME
    return repo_root() / "data" / CONFIG_FILENAME


def load() -> dict[str, Any]:
    """Read the settings file; missing, unreadable, or corrupt means empty."""
    try:
        raw = config_path().read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save(values: dict[str, Any]) -> None:
    """Write the settings file atomically so a crash cannot truncate it."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, path)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)


def data_dir() -> Path | None:
    """The chosen data folder, or ``None`` when the default applies.

    The value is normalized so ``..`` in a hand-edited file cannot make the
    same folder compare unequal across calls. Whether the folder is actually
    usable is re-checked when the database opens: :func:`app.core.db.connect`
    still refuses remote, synced, and repository paths, and SQLite refuses a
    database from a newer version or a foreign schema.
    """
    raw = load().get(DATA_DIR_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return None
    return Path(os.path.normpath(path))


def welcome_done() -> bool:
    return bool(load().get(WELCOME_DONE_KEY))


def mark_welcome_done() -> None:
    values = load()
    values[WELCOME_DONE_KEY] = True
    save(values)


def remember_data_dir(path: str | Path) -> None:
    values = load()
    values[DATA_DIR_KEY] = str(path)
    save(values)


def known_sync_folder(path: str | Path) -> str | None:
    """Name of a well-known local sync folder the path sits inside, if any."""
    for part in Path(path).parts:
        folded = part.casefold()
        if folded in KNOWN_SYNC_DIR_NAMES:
            return part
    return None


def data_dir_for_import(folder: str | Path) -> Path:
    """Folder that stores the pool when the first import decides the location.

    The returned path is normalized like :func:`data_dir`; callers still run
    it through :func:`resolve_data_dir` so remote and repository paths are
    refused exactly like a hand-picked folder.
    """
    source = Path(folder).expanduser()
    if source.is_file():
        source = source.parent
    return Path(os.path.normpath(source.resolve() / IMPORT_DATA_SUBDIR))


def resolve_data_dir(raw: str | Path) -> Path:
    """Validate a candidate folder without creating it.

    Financial data stays on a local fixed disk and outside the code
    repository, matching the guards in :mod:`app.core.db`.
    """
    text = str(raw or "").strip().strip("\"'")
    if not text:
        raise ConfigError("请选择一个数据文件夹。")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ConfigError("请使用完整路径，例如 D:\\发票数据。")
    path = path.resolve()
    reason = remote_storage_reason(path)
    if reason is not None:
        raise ConfigError(f"数据目录位于{reason}，财务数据只允许保存在本机固定盘。")
    if is_unsafe_repo_path(path):
        raise ConfigError(
            "数据目录不能放在代码仓库内；请选择仓库外的文件夹，或 data/、out/。"
        )
    if path.exists() and not path.is_dir():
        raise ConfigError("这个路径不是文件夹，请重新选择。")
    return path


def ensure_writable_dir(path: Path) -> Path:
    """Create the folder when needed and prove it accepts a file."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".invoicedesk-write-", dir=path):
            pass
    except OSError as exc:
        raise ConfigError("无法写入该文件夹，请检查权限或换一个位置。") from exc
    return path
