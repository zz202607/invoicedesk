# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-dir build for the desktop workbench.

The UI is bundled as data under ``app/ui`` so ``runtime.resource_path`` finds it
inside ``_internal``. The EXE is windowed (no console); startup failures are
shown as dialogs and written to the local log instead.

Build it through ``python tools/build_app.py``, which also generates the icon
and runs the frozen ``--smoke`` check.
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve()
ICON = ROOT / "build" / "icon.ico"

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "app" / "ui"), "app/ui")],
    hiddenimports=[
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
        "clr",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "PIL", "numpy", "pandas", "matplotlib", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="InvoiceDesk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="InvoiceDesk",
)
