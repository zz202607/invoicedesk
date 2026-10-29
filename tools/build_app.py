"""Build the one-dir Windows bundle and verify it without opening a window.

    python -m pip install -r requirements-build.txt
    python tools/build_app.py

The script regenerates the icon, runs PyInstaller with ``InvoiceDesk.spec``,
then executes the frozen EXE with ``--smoke`` in a temporary working directory.
The smoke check reads bundled UI assets and opens a throwaway database, so a
packaging regression (missing datas, broken imports) fails the build here
instead of on the user's machine.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from make_icon import write_icon  # noqa: E402 - script directory bootstrap

DIST = ROOT / "dist" / "InvoiceDesk"
EXE = DIST / "InvoiceDesk.exe"
REPORT = ROOT / "build" / "smoke-report.json"
SMOKE_TIMEOUT = 120


def _bundle_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _smoke(exe: Path) -> int:
    if not exe.is_file():
        print(f"missing executable: {exe}")
        return 1
    REPORT.unlink(missing_ok=True)
    env = os.environ.copy()
    env["INVOICEDESK_SMOKE"] = "1"
    env["INVOICEDESK_SMOKE_REPORT"] = str(REPORT)
    for name in ("INVOICEDESK_HOME", "INVOICEDESK_DB", "INVOICEDESK_OUT"):
        env.pop(name, None)

    # A random working directory proves the bundle does not rely on the source
    # checkout or on relative data/out paths.
    with tempfile.TemporaryDirectory(prefix="invoicedesk-build-") as cwd:
        completed = subprocess.run(
            [str(exe)],
            cwd=cwd,
            env=env,
            timeout=SMOKE_TIMEOUT,
            check=False,
        )
    print(f"frozen smoke exit code: {completed.returncode}")
    if not REPORT.is_file():
        print("frozen smoke wrote no report")
        return 1
    text = REPORT.read_text(encoding="utf-8")
    print(text)
    try:
        report = json.loads(text)
    except json.JSONDecodeError:
        print("frozen smoke report is not valid JSON")
        return 1
    if completed.returncode != 0 or not report.get("ok"):
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true", help="reuse dist/")
    parser.add_argument("--skip-smoke", action="store_true", help="build only")
    args = parser.parse_args()

    try:
        import PyInstaller  # noqa: F401 - fail early with a readable hint
    except ImportError:
        print("PyInstaller is not installed; run: python -m pip install -r requirements-build.txt")
        return 1

    if not args.skip_build:
        icon = write_icon(ROOT / "build" / "icon.ico")
        print(f"icon: {icon} ({icon.stat().st_size} bytes)")
        started = time.monotonic()
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "InvoiceDesk.spec",
                "--noconfirm",
                "--clean",
            ],
            cwd=ROOT,
            check=False,
        )
        if completed.returncode != 0:
            print(f"PyInstaller failed with exit code {completed.returncode}")
            return completed.returncode
        print(f"build finished in {time.monotonic() - started:.1f}s")

    if not EXE.is_file():
        print(f"missing executable: {EXE}")
        return 1
    print(f"bundle: {EXE} ({_bundle_size(DIST) / 1024 / 1024:.1f} MB)")

    if args.skip_smoke:
        return 0
    return _smoke(EXE)


if __name__ == "__main__":
    raise SystemExit(main())
