"""Offline OCR for invoice pages that have no usable text layer.

Some invoice PDFs are exported as a single raster image (screenshots or scans)
and ``page.get_text`` returns nothing. The OCR engine and its Chinese models
are bundled with the app, so recognition stays on this machine; nothing is
uploaded. OCR output is converted into the same ``TextLine`` geometry the
regular parser uses, which keeps one parsing path for both sources.
"""

from __future__ import annotations

import threading

import fitz

from app.core.text import TextLine, normalize

# 200 DPI keeps small printed amounts legible without making the detector scan
# an enormous image. The detector side limit is raised from the library default
# so dense invoice tables are not shrunk into unreadable strips.
OCR_DPI = 200
DET_LIMIT_SIDE = 1600
MIN_SCORE = 0.30

_LOCK = threading.RLock()
_ENGINE = None


class OcrError(RuntimeError):
    """OCR could not be loaded or failed on a page."""


def is_available() -> bool:
    """Cheap check used by smoke tests and the parser before attempting OCR."""
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure means "not available"
        return False
    return True


def _engine():
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            try:
                from rapidocr_onnxruntime import RapidOCR

                _ENGINE = RapidOCR(
                    det_limit_side_len=DET_LIMIT_SIDE,
                    det_limit_type="max",
                )
            except Exception as exc:
                raise OcrError(f"OCR 组件加载失败：{exc}") from exc
        return _ENGINE


def page_lines(page: fitz.Page) -> list[TextLine]:
    """OCR one PDF page into text lines carrying PDF-space coordinates.

    Raises :class:`OcrError` when the engine is missing or inference fails; the
    caller falls back to the "scanned" review state instead of guessing.
    """
    engine = _engine()
    pixmap = page.get_pixmap(dpi=OCR_DPI, colorspace=fitz.csGRAY, alpha=False)
    if pixmap.width < 8 or pixmap.height < 8:
        return []
    image = pixmap.tobytes("png")
    try:
        with _LOCK:
            result, _ = engine(image)
    except Exception as exc:
        raise OcrError(f"OCR 识别失败：{exc}") from exc

    scale_x = page.rect.width / pixmap.width
    scale_y = page.rect.height / pixmap.height
    lines: list[TextLine] = []
    for item in result or []:
        try:
            box, text, score = item[0], item[1], float(item[2])
        except (TypeError, ValueError, IndexError):
            continue
        value = normalize(str(text))
        if not value or score < MIN_SCORE:
            continue
        try:
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
        except (TypeError, ValueError, IndexError):
            continue
        if not xs or not ys:
            continue
        lines.append(
            TextLine(
                text=value,
                x0=min(xs) * scale_x,
                y0=min(ys) * scale_y,
                x1=max(xs) * scale_x,
                y1=max(ys) * scale_y,
            )
        )
    lines.sort(key=lambda item: (round(item.y0, 1), item.x0))
    return lines


def self_check() -> None:
    """Load the models and recognize a generated amount; raises ``OcrError``."""
    engine = _engine()
    doc = fitz.open()
    try:
        page = doc.new_page(width=320, height=96)
        page.insert_text((12, 56), "发票 1234.56", fontname="china-s", fontsize=24)
        pixmap = page.get_pixmap(dpi=144, colorspace=fitz.csGRAY, alpha=False)
        with _LOCK:
            result, _ = engine(pixmap.tobytes("png"))
        text = normalize("".join(str(item[1]) for item in result or []))
        if "1234" not in text.replace(" ", ""):
            raise OcrError("OCR 自检未识别出测试文字")
    finally:
        doc.close()
