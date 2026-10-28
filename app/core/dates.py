"""Invoice date normalization."""

from __future__ import annotations

import re
from datetime import date

STRICT_DATE_RE = re.compile(
    r"(?<!\d)(\d{4})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?(?!\d)"
)
SPACED_DATE_RE = re.compile(r"(?<!\d)(\d{4})\s+(\d{1,2})\s+(\d{1,2})(?!\d)")
COMPACT_DATE_RE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)")


def _to_iso(year: str, month: str, day: str) -> str | None:
    try:
        parsed = date(int(year), int(month), int(day))
    except ValueError:
        return None
    if not 1990 <= parsed.year <= 2100:
        return None
    return parsed.isoformat()


def extract_date(text: str, *, allow_compact: bool = False) -> str | None:
    for regex in (STRICT_DATE_RE, SPACED_DATE_RE):
        for match in regex.finditer(text):
            value = _to_iso(*match.groups())
            if value:
                return value
    if allow_compact:
        for match in COMPACT_DATE_RE.finditer(text):
            value = _to_iso(*match.groups())
            if value:
                return value
    return None


def has_date(text: str) -> bool:
    return extract_date(text, allow_compact=True) is not None
