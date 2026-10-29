"""Guard the PyInstaller build contract and the generated icon."""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "InvoiceDesk.spec"
BUILD_REQUIREMENTS = ROOT / "requirements-build.txt"


def test_build_requirements_pin_pyinstaller() -> None:
    text = BUILD_REQUIREMENTS.read_text(encoding="utf-8")
    assert "-r requirements.txt" in text
    assert "pyinstaller" in text.lower()
    assert "<7" in text


def test_spec_bundles_ui_assets_and_builds_a_windowed_exe() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert '"app/ui"' in text or "'app/ui'" in text
    assert "console=False" in text
    assert "webview.platforms.winforms" in text
    assert "webview.platforms.edgechromium" in text
    assert "clr" in text
    compile(text, str(SPEC), "exec")


def test_build_artifacts_are_ignored() -> None:
    if shutil.which("git") is None:  # pragma: no cover - CI has git
        return
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "dist/InvoiceDesk/InvoiceDesk.exe"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0


def test_icon_generator_writes_a_multi_size_ico(tmp_path) -> None:
    target = tmp_path / "icon.ico"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "make_icon.py"),
            "--out",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    data = target.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1)
    assert count >= 4

    widths = []
    for index in range(count):
        width, height, _, _, planes, bpp, size, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + index * 16
        )
        assert planes == 1
        assert bpp == 32
        assert size > 0
        assert offset + size <= len(data)
        widths.append(256 if width == 0 else width)
        assert height == width
    assert 16 in widths
    assert 256 in widths
    assert widths == sorted(widths)
