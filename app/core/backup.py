"""Rolling local snapshots of the invoice database.

The database is the only copy of the pool; exports and PDFs are derived files.
Snapshots live in a ``backups/`` folder beside the database, so they inherit its
locality guards and never leave the machine. Copies go through the SQLite backup
API: the result is a consistent database even while another connection is open,
and a half-written copy never takes the place of the live file.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core import db

BACKUP_DIRNAME = "backups"
MAX_BACKUPS = 10
REASON_LABELS = {
    "startup": "启动",
    "import": "导入后",
    "manual": "手动",
    "pre-restore": "恢复前",
}

# ``<db name>-YYYYmmdd-HHMMSS-<reason>[-<n>].db``; only files we wrote are
# listed, pruned, or restorable. Anything else in the folder is left alone.
_NAME_RE = re.compile(
    r"^(?P<prefix>.+)-(?P<date>\d{8})-(?P<time>\d{6})-"
    r"(?P<reason>[a-z]+(?:-[a-z]+)*?)(?:-(?P<seq>\d+))?\.db$"
)


class BackupError(RuntimeError):
    """A snapshot could not be written, read, or restored."""


@dataclass(frozen=True)
class BackupInfo:
    name: str
    path: Path
    reason: str
    created_at: datetime
    size_bytes: int


@dataclass(frozen=True)
class RestoreResult:
    restored: BackupInfo
    snapshot: BackupInfo | None


def backup_dir(db_path: str | Path) -> Path:
    """Folder that holds snapshots of ``db_path`` (same local drive)."""
    return Path(db_path).parent / BACKUP_DIRNAME


def _parse_name(name: str, prefix: str) -> tuple[str, datetime, int] | None:
    """Reason, local creation time, and same-second sequence of one snapshot."""
    match = _NAME_RE.match(name)
    if match is None or match.group("prefix") != prefix:
        return None
    try:
        created_at = datetime.strptime(
            match.group("date") + match.group("time"), "%Y%m%d%H%M%S"
        )
    except ValueError:
        return None
    sequence = int(match.group("seq") or 1)
    return match.group("reason"), created_at, sequence


def _info(path: Path, reason: str, created_at: datetime) -> BackupInfo:
    return BackupInfo(
        name=path.name,
        path=path,
        reason=reason,
        created_at=created_at,
        size_bytes=path.stat().st_size,
    )


def list_backups(db_path: str | Path) -> list[BackupInfo]:
    """Snapshots of this database, newest first; foreign files are ignored."""
    path = Path(db_path)
    root = backup_dir(path)
    if not root.is_dir():
        return []
    found: list[BackupInfo] = []
    for entry in root.iterdir():
        if not entry.is_file():
            continue
        parsed = _parse_name(entry.name, path.stem)
        if parsed is None:
            continue
        reason, created_at, sequence = parsed
        try:
            found.append((_info(entry, reason, created_at), sequence))
        except OSError:  # vanished between iterdir and stat
            continue
    found.sort(key=lambda pair: (pair[0].created_at, pair[1], pair[0].name), reverse=True)
    return [item for item, _ in found]


def latest_backup(db_path: str | Path) -> BackupInfo | None:
    backups = list_backups(db_path)
    return backups[0] if backups else None


def _check_integrity(path: Path) -> None:
    """Refuse anything that is not a healthy SQLite database of a known version."""
    try:
        connection = sqlite3.connect(str(path))
    except sqlite3.Error as exc:
        raise BackupError("备份文件无法打开，可能已损坏。") from exc
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise BackupError("备份文件已损坏，无法使用。")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > db.DB_VERSION:
            raise BackupError(
                "该备份来自更新版本的程序，请升级程序后再恢复。"
            )
    except sqlite3.Error as exc:
        raise BackupError("备份文件无法读取，可能已损坏。") from exc
    finally:
        connection.close()


def _snapshot_db(source: Path, target: Path) -> None:
    """Consistent copy through the SQLite backup API (no half-written files)."""
    source_connection = sqlite3.connect(str(source))
    try:
        target_connection = sqlite3.connect(str(target))
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
    finally:
        source_connection.close()


def _snapshot_raw(source: Path, target: Path) -> None:
    """Byte copy for sources SQLite cannot read (used before a restore)."""
    shutil.copy2(source, target)


def _write_snapshot(
    db_path: Path, *, reason: str, keep: int, now: datetime | None, allow_raw: bool
) -> BackupInfo:
    created = (now or datetime.now()).replace(microsecond=0)
    target_dir = backup_dir(db_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{db_path.stem}-{created.strftime('%Y%m%d-%H%M%S')}-{reason}"
    # Two snapshots in the same second (double click, restore + manual) get
    # ``-2``, ``-3``… instead of silently replacing the earlier one.
    target = target_dir / f"{stem}.db"
    sequence = 2
    while target.exists():
        target = target_dir / f"{stem}-{sequence}.db"
        sequence += 1
    name = target.name
    temp = target.with_name(name + ".tmp")
    try:
        temp.unlink()  # a leftover from an interrupted run must not be reused
    except OSError:
        pass
    try:
        try:
            _snapshot_db(db_path, temp)
            _check_integrity(temp)
        except (sqlite3.Error, BackupError) as exc:
            if not allow_raw:
                raise BackupError("数据库无法读取，备份失败。") from exc
            _snapshot_raw(db_path, temp)
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
    prune_backups(db_path, keep=keep)
    return _info(target, reason, created)


def create_backup(
    db_path: str | Path,
    *,
    reason: str = "manual",
    keep: int = MAX_BACKUPS,
    now: datetime | None = None,
    allow_raw: bool = False,
) -> BackupInfo:
    """Snapshot the database into ``backups/``; returns the new snapshot.

    ``keep`` bounds how many of our snapshots stay on disk (oldest go first).
    ``allow_raw`` falls back to a byte copy when SQLite cannot read the source;
    only the restore path uses it, so a corrupt database is still preserved.
    """
    path = Path(db_path)
    if not path.is_file():
        raise BackupError("数据库尚未创建，暂无可备份的数据。")
    if not re.fullmatch(r"[a-z]+(?:-[a-z]+)*", reason):
        raise BackupError("备份类型不合法。")
    return _write_snapshot(
        path, reason=reason, keep=max(1, keep), now=now, allow_raw=allow_raw
    )


def prune_backups(db_path: str | Path, *, keep: int = MAX_BACKUPS) -> list[str]:
    """Delete our oldest snapshots beyond ``keep``; returns their names."""
    path = Path(db_path)
    backups = list_backups(path)
    removed: list[str] = []
    for item in backups[max(1, keep) :]:
        try:
            item.path.unlink()
        except OSError:  # locked or already gone; never fail the backup itself
            continue
        removed.append(item.name)
    return removed


def _invoice_count(path: Path) -> int | None:
    try:
        connection = sqlite3.connect(str(path))
    except sqlite3.Error:
        return None
    try:
        return int(
            connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        )
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def startup_backup(
    db_path: str | Path,
    *,
    keep: int = MAX_BACKUPS,
    now: datetime | None = None,
) -> BackupInfo | None:
    """Snapshot before the first write of a session; skip an empty pool."""
    path = Path(db_path)
    if not path.is_file():
        return None
    count = _invoice_count(path)
    if count == 0:
        return None
    return create_backup(path, reason="startup", keep=keep, now=now)


def restore_backup(
    db_path: str | Path,
    name: str,
    *,
    keep: int = MAX_BACKUPS,
    now: datetime | None = None,
) -> RestoreResult:
    """Replace the live database with snapshot ``name``.

    The current database is first saved as a ``pre-restore`` snapshot, so a
    restore can itself be undone. The swap is a single ``os.replace`` of a
    completed copy; stale ``-wal``/``-shm`` sidecars from the replaced file are
    removed so SQLite cannot replay them into the restored database.
    """
    path = Path(db_path)
    root = backup_dir(path).resolve()
    candidate = (root / name).resolve()
    if candidate.parent != root or candidate.name != name:
        raise BackupError("备份名称不合法。")
    parsed = _parse_name(candidate.name, path.stem)
    if not candidate.is_file() or parsed is None:
        raise BackupError("找不到该备份。")
    _check_integrity(candidate)

    temp = path.with_name(path.name + ".restore")
    snapshot: BackupInfo | None = None
    try:
        try:
            temp.unlink()
        except OSError:
            pass
        try:
            shutil.copyfile(candidate, temp)
            if path.exists():
                snapshot = create_backup(
                    path, reason="pre-restore", keep=keep, now=now, allow_raw=True
                )
            os.replace(temp, path)
        except OSError as exc:
            raise BackupError("无法替换数据库文件，请关闭其他程序后重试。") from exc
    finally:
        if temp.exists():
            temp.unlink()
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        try:
            side.unlink()
        except OSError:
            pass
    reason, created_at, _sequence = parsed
    return RestoreResult(restored=_info(candidate, reason, created_at), snapshot=snapshot)
