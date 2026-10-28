"""Repository and local-storage policy for financial data.

Invoice databases and JSON dumps contain real amounts and tax IDs. They may live
under the ignored ``data/``, ``out/``, or ``tests/local/`` directories, or
anywhere outside the repository — never elsewhere inside the repo. The database
also stays on a local fixed/removable disk: network shares and OneDrive are
refused by default so financial data does not leave the machine.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ALLOWED_REPO_DIRS = {("out",), ("data",)}
ALLOWED_REPO_SUBDIRS = {("tests", "local")}
SYNC_ENV_ROOTS = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def is_unsafe_repo_path(path: Path) -> bool:
    """True when ``path`` is inside the repository outside the allowed dirs."""
    try:
        relative = path.resolve().relative_to(repo_root())
    except ValueError:
        return False
    return not (
        relative.parts[:1] in ALLOWED_REPO_DIRS
        or relative.parts[:2] in ALLOWED_REPO_SUBDIRS
    )


def _is_unc(path: Path) -> bool:
    text = str(path)
    return text.startswith(("\\\\", "//"))


def _windows_drive_type(path: Path) -> int | None:
    if sys.platform != "win32":
        return None
    text = str(path)
    if len(text) < 2 or text[1] != ":":
        return None
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetDriveTypeW(text[:2] + "\\"))
    except (AttributeError, OSError, ValueError):  # pragma: no cover - platform guard
        return None


def _sync_root(path: Path) -> Path | None:
    resolved = path.resolve()
    roots = [Path.home() / "OneDrive"]
    roots.extend(Path(os.environ[name]) for name in SYNC_ENV_ROOTS if os.environ.get(name))
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        return root
    return None


def remote_storage_reason(path: Path) -> str | None:
    """Return why ``path`` is not local storage, or None when it is local."""
    if _is_unc(path):
        return "网络共享路径"
    drive_type = _windows_drive_type(path)
    if drive_type == 4:
        return "网络驱动器"
    if drive_type in {0, 1}:
        return "不可用的盘符"
    if _sync_root(path) is not None:
        return "云同步目录"
    return None


def output_path_reason(path: Path) -> str | None:
    """Why an output directory is not local/ignored storage, or None.

    Shared by the CLI exports and the desktop workbench so both surfaces check
    the exact resolved directory that receives the files.
    """
    remote = remote_storage_reason(path)
    if remote is not None:
        return f"位于{remote}"
    if is_unsafe_repo_path(path):
        return "在仓库内非忽略目录"
    return None
