"""Generate the application icon with the standard library only.

Draws a blue rounded tile, a white invoice sheet, and a green check, then
writes a multi-size Windows ``.ico`` (uncompressed 32-bit BMP entries, the most
compatible layout) and, optionally, a PNG preview.

    python tools/make_icon.py --out build/icon.ico --preview build/icon.png
"""

from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path

Color = tuple[int, int, int, int]

BACKGROUND: Color = (31, 95, 175, 255)
SHEET: Color = (255, 255, 255, 255)
RULE: Color = (148, 163, 184, 255)
CHECK_BG: Color = (22, 163, 74, 255)
CHECK_FG: Color = (255, 255, 255, 255)

# Normalized layout; every render size shares the same geometry.
SHEET_BOX = (0.24, 0.16, 0.76, 0.84, 0.045)
RULE_X = (0.32, 0.68)
RULE_YS = (0.33, 0.42, 0.51)
RULE_HALF = 0.012
CHECK_CENTER = (0.72, 0.76)
CHECK_RADIUS = 0.15
CHECK_POINTS = ((0.66, 0.76), (0.705, 0.805), (0.785, 0.715))
CHECK_HALF = 0.0175
SIZES = (16, 24, 32, 48, 64, 128, 256)
SUPERSAMPLE = 4


def _rounded(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    qx = max(abs(x - cx) - ((x1 - x0) / 2 - r), 0.0)
    qy = max(abs(y - cy) - ((y1 - y0) / 2 - r), 0.0)
    return qx * qx + qy * qy <= r * r


def _circle(x: float, y: float, cx: float, cy: float, r: float) -> bool:
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _segment(
    x: float, y: float, ax: float, ay: float, bx: float, by: float, half: float
) -> bool:
    vx, vy = bx - ax, by - ay
    length = vx * vx + vy * vy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((x - ax) * vx + (y - ay) * vy) / length))
    dx, dy = x - (ax + t * vx), y - (ay + t * vy)
    return dx * dx + dy * dy <= half * half


def _sample(x: float, y: float) -> Color | None:
    if not _rounded(x, y, 0.03, 0.03, 0.97, 0.97, 0.16):
        return None
    color = BACKGROUND
    x0, y0, x1, y1, radius = SHEET_BOX
    if _rounded(x, y, x0, y0, x1, y1, radius):
        color = SHEET
    for rule_y in RULE_YS:
        if _segment(x, y, RULE_X[0], rule_y, RULE_X[1], rule_y, RULE_HALF):
            color = RULE
    cx, cy = CHECK_CENTER
    if _circle(x, y, cx, cy, CHECK_RADIUS):
        color = CHECK_BG
        (ax, ay), (bx, by), (dx, dy) = CHECK_POINTS
        if (
            _segment(x, y, ax, ay, bx, by, CHECK_HALF)
            or _segment(x, y, bx, by, dx, dy, CHECK_HALF)
        ):
            color = CHECK_FG
    return color


def render(size: int, supersample: int = SUPERSAMPLE) -> bytes:
    """Return RGBA pixels (top-down) for one square icon size."""
    hi = size * supersample
    premul = [[0, 0, 0, 0] for _ in range(size * size)]
    scale = 1.0 / hi
    for py in range(hi):
        y = (py + 0.5) * scale
        row = (py // supersample) * size
        for px in range(hi):
            color = _sample((px + 0.5) * scale, y)
            if color is None:
                continue
            bucket = premul[row + px // supersample]
            alpha = color[3]
            bucket[0] += color[0] * alpha
            bucket[1] += color[1] * alpha
            bucket[2] += color[2] * alpha
            bucket[3] += alpha

    out = bytearray(size * size * 4)
    samples = supersample * supersample
    for index, (r, g, b, a) in enumerate(premul):
        if a == 0:
            continue
        out[index * 4 : index * 4 + 4] = bytes(
            (r // a, g // a, b // a, a // samples)
        )
    return bytes(out)


def _bmp_entry(rgba: bytes, size: int) -> bytes:
    xor = bytearray()
    for y in range(size - 1, -1, -1):
        row = rgba[y * size * 4 : (y + 1) * size * 4]
        for offset in range(0, len(row), 4):
            r, g, b, a = row[offset : offset + 4]
            xor.extend((b, g, r, a))
    stride = ((size + 31) // 32) * 4
    mask = bytes(stride * size)
    header = struct.pack(
        "<IiiHHIIiiII",
        40,
        size,
        size * 2,
        1,
        32,
        0,
        len(xor) + len(mask),
        0,
        0,
        0,
        0,
    )
    return header + bytes(xor) + mask


def build_ico(size_data: list[tuple[int, bytes]]) -> bytes:
    entries = bytearray()
    payload = bytearray()
    offset = 6 + 16 * len(size_data)
    for size, rgba in size_data:
        data = _bmp_entry(rgba, size)
        entries.extend(
            struct.pack(
                "<BBBBHHII",
                0 if size >= 256 else size,
                0 if size >= 256 else size,
                0,
                0,
                1,
                32,
                len(data),
                offset + len(payload),
            )
        )
        payload.extend(data)
    return struct.pack("<HHH", 0, 1, len(size_data)) + bytes(entries) + bytes(payload)


def png_bytes(rgba: bytes, size: int) -> bytes:
    rows = bytearray()
    stride = size * 4
    for y in range(size):
        rows.append(0)
        rows.extend(rgba[y * stride : (y + 1) * stride])

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def write_icon(path: str | Path, *, preview: str | Path | None = None) -> Path:
    rendered = [(size, render(size)) for size in SIZES]
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build_ico(rendered))
    if preview is not None:
        preview_path = Path(preview)
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(png_bytes(dict(rendered)[256], 256))
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("build") / "icon.ico")
    parser.add_argument("--preview", type=Path, default=None)
    args = parser.parse_args()
    written = write_icon(args.out, preview=args.preview)
    print(f"wrote {written} ({written.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
