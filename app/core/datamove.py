"""Move an existing app home to a new data folder (one-off migration).

Packaged builds keep the pool, backups, exports, and logs under
``%LOCALAPPDATA%\\InvoiceDesk``. Moving that home to a folder beside the
invoices copies a verified snapshot, then rewrites the pointer file so the next
start uses the new location. The source home is never deleted here: deleting
the old copy is a separate, explicitly confirmed step.

The database is copied through SQLite's backup API rather than by file copy, so
a WAL or hot journal cannot produce a torn file. Every copy is re-opened and
checked (integrity, schema version, row counts) before the pointer moves.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from app.core import appconfig

SKIP_ENTRIES = {
    appconfig.CONFIG_FILENAME,
    appconfig.DB_FILENAME,
    appconfig.DB_FILENAME + "-wal",
    appconfig.DB_FILENAME + "-shm",
}


class DataMoveError(RuntimeError):
    """A migration could not be completed safely."""


@dataclass(frozen=True)
class DataSummary:
    database_version: int
    invoices: int
    archived: int
    batches: int
    reports: int
    files: int
    max_invoice_id: int
    amount_cents: int
    integrity: str


def summarize(db_path: str | Path) -> DataSummary:
    """Read-only description of a pool, used to prove a copy is complete."""
    path = Path(db_path)
    if not path.is_file():
        raise DataMoveError(f"找不到发票库：{path}")
    try:
        with contextlib.closing(
            sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        ) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "invoices" not in tables or "batches" not in tables:
                raise DataMoveError(f"不是发票管家的数据库：{path}")
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(invoices)")
            }
            archived_sql = (
                " WHERE archived_at != ''" if "archived_at" in columns else " WHERE 0"
            )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            return DataSummary(
                database_version=connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0],
                invoices=connection.execute(
                    "SELECT COUNT(*) FROM invoices"
                ).fetchone()[0],
                archived=connection.execute(
                    f"SELECT COUNT(*) FROM invoices{archived_sql}"
                ).fetchone()[0],
                batches=connection.execute(
                    "SELECT COUNT(*) FROM batches"
                ).fetchone()[0],
                reports=(
                    connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
                    if "reports" in tables
                    else 0
                ),
                files=(
                    connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                    if "files" in tables
                    else 0
                ),
                max_invoice_id=int(
                    connection.execute(
                        "SELECT COALESCE(MAX(id), 0) FROM invoices"
                    ).fetchone()[0]
                ),
                amount_cents=int(
                    connection.execute(
                        "SELECT COALESCE(SUM(amount_total_cents), 0) FROM invoices"
                    ).fetchone()[0]
                ),
                integrity=str(integrity),
            )
    except sqlite3.Error as exc:
        raise DataMoveError(f"无法读取发票库：{path}（{exc}）") from exc


def _copy_database(source: Path, target: Path) -> None:
    """Copy through the SQLite backup API so WAL/journal files cannot tear it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(
        sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    ) as src, contextlib.closing(sqlite3.connect(str(target))) as dst:
        src.backup(dst)


def _read_config(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        values = json.loads(raw)
    except ValueError:
        return {}
    return values if isinstance(values, dict) else {}


def _write_config(path: Path, values: dict) -> None:
    payload = json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True)
    temp = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, path)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)


def copy_data_dir(source_home: str | Path, target: str | Path) -> dict:
    """Copy a stopped app home into ``target`` and point the config at it.

    The source home is left untouched. Re-running with the same source and an
    identical target database is a no-op for the database; other folders are
    re-copied so a fresh export or log is carried over.
    """
    source_home = Path(source_home).expanduser().resolve()
    if not source_home.is_dir():
        raise DataMoveError(f"源目录不存在：{source_home}")
    source_db = source_home / appconfig.DB_FILENAME
    if not source_db.is_file():
        raise DataMoveError(f"源目录没有 {appconfig.DB_FILENAME}：{source_home}")

    try:
        target = appconfig.resolve_data_dir(target)
    except appconfig.ConfigError as exc:
        raise DataMoveError(str(exc)) from exc
    if target == source_home:
        raise DataMoveError("目标目录与源目录相同。")
    if target.is_relative_to(source_home) or source_home.is_relative_to(target):
        raise DataMoveError("目标目录不能与源目录互相包含。")

    source_summary = summarize(source_db)
    if source_summary.integrity != "ok":
        raise DataMoveError(f"源发票库完整性检查未通过：{source_summary.integrity}")
    target_db = target / appconfig.DB_FILENAME
    copied_database = False
    if target_db.is_file():
        # The backup API rewrites pages, so byte equality is meaningless.
        # Counts, ids, and the amount total identify the same pool in practice.
        if summarize(target_db) != source_summary:
            raise DataMoveError(
                f"目标目录已有不同的发票库，已停止：{target_db}"
            )
    else:
        _copy_database(source_db, target_db)
        copied_database = True
        if summarize(target_db) != source_summary:
            raise DataMoveError("复制后的发票库校验不一致，已停止。")

    copied: list[str] = []
    for entry in sorted(source_home.iterdir(), key=lambda item: item.name):
        if entry.name in SKIP_ENTRIES:
            continue
        destination = target / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, destination)
        copied.append(entry.name)

    config_file = source_home / appconfig.CONFIG_FILENAME
    values = _read_config(config_file)
    values[appconfig.DATA_DIR_KEY] = str(target)
    try:
        _write_config(config_file, values)
    except OSError as exc:
        raise DataMoveError(f"无法更新设置文件：{config_file}（{exc}）") from exc
    return {
        "source": str(source_home),
        "target": str(target),
        "summary": asdict(source_summary),
        "copied_database": copied_database,
        "copied_entries": copied,
        "config": str(config_file),
    }
