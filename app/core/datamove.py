"""Moving data folders: app home → chosen folder, and legacy → app home.

Legacy builds kept the pool, backups, exports, and logs beside the invoices in
``InvoiceDeskData``. :func:`split_data_dir` copies the pool, backups, and logs
back to the app home and leaves the generated files in ``报销发票整理`` beside the
invoices, then rewrites the pointer file. The legacy folder is never deleted
here: deleting it is a separate, explicitly confirmed step.

The database is copied through SQLite's backup API rather than by file copy, so
a WAL or hot journal cannot produce a torn file. Every copy is re-opened and
checked (integrity, schema version, row counts) before the pointer moves.
"""

from __future__ import annotations

import contextlib
import filecmp
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
# Legacy output wrappers that the flattened export folder no longer uses.
FLATTENED_OUTPUT_DIRS = {"export", "report"}


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


def _copy_file(source: Path, destination: Path) -> bool:
    """Copy one file unless it already exists byte-for-byte. Never overwrites."""
    if destination.exists():
        if (
            destination.is_file()
            and source.stat().st_size == destination.stat().st_size
            and filecmp.cmp(source, destination, shallow=False)
        ):
            return False
        raise DataMoveError(f"目标已有不同文件，已停止：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


def _copy_tree_safely(source: Path, target: Path) -> list[str]:
    """Copy files below ``source`` into ``target`` without overwriting."""
    copied: list[str] = []
    if not source.is_dir():
        return copied
    for item in sorted(source.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(source)
        if _copy_file(item, target / relative):
            copied.append(str(relative))
    return copied


def split_data_dir(
    source: str | Path,
    home: str | Path,
    export_dir: str | Path | None = None,
    config_file: str | Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Empty a legacy data folder beside invoices, leaving only exports.

    Copies the pool, backups, logs, and any other data-folder files into
    ``home``; copies ``source/out`` into ``export_dir`` (default
    ``source.parent/报销发票整理``), flattening the legacy ``export/`` and
    ``report/`` wrapper folders so files sit directly in the export folder;
    then points ``config_file`` at both. The source folder is left untouched so
    the user can verify before deleting it.
    """
    source_path = Path(source).expanduser().resolve()
    source_db = source_path / appconfig.DB_FILENAME
    if not source_path.is_dir() or not source_db.is_file():
        raise DataMoveError(f"源目录没有 {appconfig.DB_FILENAME}：{source_path}")
    export_path = (
        Path(export_dir).expanduser().resolve()
        if export_dir is not None
        else source_path.parent / appconfig.EXPORT_SUBDIR
    )
    try:
        home_path = appconfig.resolve_data_dir(home)
        export_path = appconfig.resolve_data_dir(export_path)
    except appconfig.ConfigError as exc:
        raise DataMoveError(str(exc)) from exc
    for label, other in (("数据目录", home_path), ("导出目录", export_path)):
        if (
            other == source_path
            or other.is_relative_to(source_path)
            or source_path.is_relative_to(other)
        ):
            raise DataMoveError(f"{label}不能与旧数据目录互相包含：{other}")
        sync_folder = appconfig.known_sync_folder(other)
        if sync_folder is not None:
            raise DataMoveError(
                f"{label}位于 {sync_folder} 同步目录中，财务数据只允许保存在本机固定盘：{other}"
            )
    config_path = (
        Path(config_file).expanduser().resolve()
        if config_file is not None
        else home_path / appconfig.CONFIG_FILENAME
    )

    source_summary = summarize(source_db)
    if source_summary.integrity != "ok":
        raise DataMoveError(f"源发票库完整性检查未通过：{source_summary.integrity}")
    result: dict = {
        "source": str(source_path),
        "home": str(home_path),
        "export": str(export_path),
        "config": str(config_path),
        "summary": asdict(source_summary),
        "copied_database": False,
        "copied_home": [],
        "copied_export": [],
        "dry_run": dry_run,
    }
    if dry_run:
        return result

    home_db = home_path / appconfig.DB_FILENAME
    if home_db.is_file():
        if summarize(home_db) != source_summary:
            raise DataMoveError(f"数据目录已有不同的发票库，已停止：{home_db}")
    else:
        _copy_database(source_db, home_db)
        result["copied_database"] = True
        if summarize(home_db) != source_summary:
            raise DataMoveError("复制后的发票库校验不一致，已停止。")

    copied_home: list[str] = []
    for entry in sorted(source_path.iterdir(), key=lambda item: item.name):
        if entry.name in SKIP_ENTRIES or entry.name == "out":
            continue
        if entry.is_dir():
            for relative in _copy_tree_safely(entry, home_path / entry.name):
                copied_home.append(f"{entry.name}/{relative}")
        elif _copy_file(entry, home_path / entry.name):
            copied_home.append(entry.name)
    result["copied_home"] = copied_home

    copied_export: list[str] = []
    source_out = source_path / "out"
    if source_out.is_dir():
        for child in sorted(source_out.iterdir(), key=lambda item: item.name):
            if child.is_dir() and child.name in FLATTENED_OUTPUT_DIRS:
                # Older builds wrote out/export/... and out/report/...; the
                # new layout wants those files directly in the export folder.
                copied_export.extend(_copy_tree_safely(child, export_path))
            elif child.is_dir():
                for relative in _copy_tree_safely(child, export_path / child.name):
                    copied_export.append(f"{child.name}/{relative}")
            elif _copy_file(child, export_path / child.name):
                copied_export.append(child.name)
    result["copied_export"] = copied_export

    values = _read_config(config_path)
    values[appconfig.DATA_DIR_KEY] = str(home_path)
    values[appconfig.EXPORT_DIR_KEY] = str(export_path)
    try:
        _write_config(config_path, values)
    except OSError as exc:
        raise DataMoveError(f"无法更新设置文件：{config_path}（{exc}）") from exc
    return result
