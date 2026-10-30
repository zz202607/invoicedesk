"""Generate the Windows version resource for the packaged EXE.

PyInstaller reads ``build/version_info.txt`` (a ``VSVersionInfo`` literal) when
``InvoiceDesk.spec`` passes ``version=``. The version comes from
``pyproject.toml``; a test keeps ``app.__version__`` in step with it.

    python tools/make_version.py            # write build/version_info.txt
    python tools/make_version.py --out X    # write somewhere else
"""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "build" / "version_info.txt"
PRODUCT = "InvoiceDesk"
FILE_DESCRIPTION = "Local invoice organizer and reimbursement sheet generator"


def read_version(pyproject: Path | None = None) -> str:
    """Read the canonical version from ``pyproject.toml``."""
    path = pyproject or ROOT / "pyproject.toml"
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    version = str(data["project"]["version"]).strip()
    if not version:
        raise ValueError("pyproject.toml has no project.version")
    return version


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """Turn ``1.0.0`` into the four-number tuple a Windows resource needs."""
    parts = version.split(".")
    if not 1 <= len(parts) <= 4 or not all(part.isdigit() for part in parts):
        raise ValueError(f"unsupported version: {version!r}")
    numbers = [int(part) for part in parts]
    return tuple(numbers + [0] * (4 - len(numbers)))  # type: ignore[return-value]


def render(version: str) -> str:
    """Render the version resource PyInstaller evaluates."""
    major, minor, patch, build = version_tuple(version)
    fixed = f"filevers=({major}, {minor}, {patch}, {build})"
    product = f"prodvers=({major}, {minor}, {patch}, {build})"
    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    {fixed},
    {product},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', '{PRODUCT}'),
        StringStruct('FileDescription', '{FILE_DESCRIPTION}'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', '{PRODUCT}'),
        StringStruct('LegalCopyright', 'MIT License'),
        StringStruct('OriginalFilename', '{PRODUCT}.exe'),
        StringStruct('ProductName', '{PRODUCT}'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def write_version_file(target: Path | None = None, version: str | None = None) -> Path:
    """Write the resource file and return its path."""
    path = target or DEFAULT_OUT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(version or read_version()), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--version", default="", help="override pyproject version")
    args = parser.parse_args()
    path = write_version_file(args.out, args.version or None)
    print(f"version resource: {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
