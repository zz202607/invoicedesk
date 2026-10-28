"""Text layout helpers.

Invoices are parsed by geometry, not by text order: the same label/value pair
can be split into different blocks by the PDF generator. ``TextLine`` keeps the
bbox so callers can ask for "the value to the right of this label".
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import fitz

MAX_LINE_CHARS = 2000


@dataclass(frozen=True)
class TextLine:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 1.0)


def normalize(text: str) -> str:
    """NFKC fixes the Kangxi radicals some invoice generators emit.

    Invoice PDFs are untrusted input; drop all Cc/Cf characters (C0/C1 controls,
    bidi overrides, zero-width joiners) so extracted text cannot inject terminal
    escape sequences or reorder displayed amounts.
    """
    text = unicodedata.normalize("NFKC", text)
    text = "".join(char for char in text if unicodedata.category(char) not in {"Cc", "Cf"})
    text = text.replace("　", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def extract_lines(page: fitz.Page) -> list[TextLine]:
    data = page.get_text("dict")
    lines: list[TextLine] = []
    for block in data.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            text = normalize("".join(span.get("text", "") for span in line.get("spans", [])))
            if not text:
                continue
            if len(text) > MAX_LINE_CHARS:
                text = text[:MAX_LINE_CHARS]
            x0, y0, x1, y1 = line["bbox"]
            lines.append(TextLine(text=text, x0=x0, y0=y0, x1=x1, y1=y1))
    lines.sort(key=lambda item: (round(item.y0, 1), item.x0))
    return lines


def page_text(lines: Sequence[TextLine]) -> str:
    return "\n".join(line.text for line in lines)


def same_row(left: TextLine, right: TextLine, tolerance: float = 8.0) -> bool:
    return abs(left.cy - right.cy) <= max(tolerance, (left.height + right.height) / 2)


def find_lines(lines: Iterable[TextLine], pattern: str) -> list[TextLine]:
    regex = re.compile(pattern)
    return [line for line in lines if regex.search(line.text)]


def nearest_right(
    anchor: TextLine,
    candidates: Sequence[TextLine],
    *,
    max_dy: float = 8.0,
    max_dx: float = 400.0,
) -> TextLine | None:
    """Pick the closest value line to the right of an anchor on the same row."""
    best: TextLine | None = None
    best_score = float("inf")
    for candidate in candidates:
        if candidate is anchor:
            continue
        if candidate.x0 < anchor.x1 - 8:
            continue
        if abs(candidate.cy - anchor.cy) > max(max_dy, anchor.height * 0.9):
            continue
        dx = max(candidate.x0 - anchor.x1, 0.0)
        if dx > max_dx:
            continue
        score = dx + abs(candidate.cy - anchor.cy) * 4
        if score < best_score:
            best, best_score = candidate, score
    return best


def nearest_line(
    anchor: TextLine,
    candidates: Sequence[TextLine],
    *,
    max_distance: float = 150.0,
) -> TextLine | None:
    best: TextLine | None = None
    best_score = float("inf")
    for candidate in candidates:
        if candidate is anchor:
            continue
        dx = max(candidate.x0 - anchor.x1, 0.0)
        dy = max(candidate.y0 - anchor.y1, 0.0) if candidate.y0 >= anchor.y1 else 0.0
        if candidate.y0 < anchor.y0:
            dy = anchor.y0 - candidate.y1
        score = dx + dy * 1.5 + abs(candidate.cy - anchor.cy)
        if score < best_score and score <= max_distance:
            best, best_score = candidate, score
    return best


def strip_label(text: str, *labels: str) -> str:
    result = text
    for label in labels:
        result = re.sub(rf"^{re.escape(label)}\s*[:：]?\s*", "", result)
    return result.strip()
