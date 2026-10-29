"""Where a running InvoiceDesk process keeps its files.

A source checkout writes beside the code (``data/`` and ``out/``). A packaged
build must not write into its installation directory: Program Files is
read-only for normal users and every update replaces it. Packaged builds use
``%LOCALAPPDATA%\\InvoiceDesk`` instead, which is local (never OneDrive) and
survives reinstalls. ``INVOICEDESK_HOME`` overrides the root in either mode.

Choosing a default does not weaken the guards in :mod:`app.core.paths`: the
database and export writers still refuse remote, synced, and unsafe repo paths.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from app.core.paths import repo_root

APP_DIR_NAME = "InvoiceDesk"
HOME_ENV = "INVOICEDESK_HOME"

# Evergreen WebView2 Runtime client GUID published by Microsoft. Detection is
# documented for the Evergreen runtime; a fixed-version runtime shipped inside
# an app would not appear here, but this app does not use one.
WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_REGISTRY_KEYS = (
    ("HKLM", rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
    ("HKLM", rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
    ("HKCU", rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
)


def is_frozen() -> bool:
    """True inside a PyInstaller bundle (one-dir or one-file)."""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """Directory that holds bundled data files (the UI assets live here)."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle)
    return repo_root()


def resource_path(relative: str | Path) -> Path:
    return resource_root() / Path(relative)


def _home_override() -> Path | None:
    raw = os.environ.get(HOME_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def uses_app_home() -> bool:
    """True when defaults should live in the per-user app directory."""
    return is_frozen() or _home_override() is not None


def app_home() -> Path:
    """Root for databases, exports, and logs in packaged or overridden runs."""
    override = _home_override()
    if override is not None:
        return override
    if is_frozen():
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_DIR_NAME
        return Path.home() / ".invoicedesk"
    return repo_root()


def default_db_path() -> Path:
    if uses_app_home():
        return app_home() / "invoicedesk.db"
    return Path("data") / "invoicedesk.db"


def default_out_dir() -> Path:
    if uses_app_home():
        return app_home() / "out"
    return Path("out")


def log_file() -> Path:
    if uses_app_home():
        return app_home() / "logs" / "invoicedesk.log"
    return repo_root() / "data" / "logs" / "invoicedesk.log"


def _registry_value(hive_name: str, path: str, name: str) -> str:
    import winreg

    hives = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
    with winreg.OpenKey(hives[hive_name], path) as key:
        value, _ = winreg.QueryValueEx(key, name)
    return str(value)


def webview2_version(
    probe: Callable[[str, str, str], str] | None = None,
) -> str | None:
    """Installed Evergreen WebView2 version, or ``None`` when it is missing."""
    if sys.platform != "win32":
        return None
    read = probe or _registry_value
    for hive, path in WEBVIEW2_REGISTRY_KEYS:
        try:
            version = read(hive, path, "pv")
        except (OSError, ValueError):
            continue
        if version:
            return str(version)
    return None


def webview2_available(probe: Callable[[str, str, str], str] | None = None) -> bool:
    return webview2_version(probe=probe) is not None
