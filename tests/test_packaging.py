"""Guard the PyInstaller build contract and the generated icon."""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import tomllib
from pathlib import Path

from app import __version__

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "InvoiceDesk.spec"
BUILD_REQUIREMENTS = ROOT / "requirements-build.txt"


def test_build_requirements_pin_pyinstaller() -> None:
    text = BUILD_REQUIREMENTS.read_text(encoding="utf-8")
    assert "-r requirements.txt" in text
    assert "pyinstaller" in text.lower()
    assert "<7" in text


def test_package_version_matches_pyproject() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    assert version == __version__
    assert __version__.split(".")[0] == "1", "v1.0 release line"


def test_spec_bundles_ui_assets_and_builds_a_windowed_exe() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert '"app/ui"' in text or "'app/ui'" in text
    assert "console=False" in text
    assert "webview.platforms.winforms" in text
    assert "webview.platforms.edgechromium" in text
    assert "clr" in text
    assert "version_info.txt" in text
    assert "version=str(VERSION_FILE)" in text
    compile(text, str(SPEC), "exec")


def test_version_resource_generator_embeds_the_release_version(tmp_path) -> None:
    target = tmp_path / "version_info.txt"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "make_version.py"),
            "--out",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    text = target.read_text(encoding="utf-8")
    numbers = [int(part) for part in __version__.split(".")]
    pair = tuple(numbers + [0] * (4 - len(numbers)))
    assert f"filevers=({pair[0]}, {pair[1]}, {pair[2]}, {pair[3]})" in text
    assert f"StringStruct('FileVersion', '{__version__}')" in text
    assert f"StringStruct('ProductVersion', '{__version__}')" in text
    assert "StringStruct('OriginalFilename', 'InvoiceDesk.exe')" in text
    compile(text, str(target), "exec")


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
