"""Money parsing, including Chinese uppercase amounts.

The uppercase amount is the strongest signal on an old VAT invoice: the printed
``(小写)`` total may be split far away from its label, while the uppercase words
stay together.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.core.text import normalize

MONEY_RE = re.compile(r"[¥￥]\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
PLAIN_MONEY_RE = re.compile(r"(?<![\d.])([0-9][0-9,]*\.[0-9]{2})(?![\d.%])")

CN_DIGITS = {
    "零": 0,
    "壹": 1,
    "贰": 2,
    "叁": 3,
    "肆": 4,
    "伍": 5,
    "陆": 6,
    "柒": 7,
    "捌": 8,
    "玖": 9,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
UPPER_DIGITS = "零壹贰叁肆伍陆柒捌玖"
UPPER_UNITS = ("", "拾", "佰", "仟")
UPPER_GROUPS = ("", "万", "亿", "万亿")
CN_SMALL_UNITS = {"拾": 10, "十": 10, "佰": 100, "百": 100, "仟": 1000, "千": 1000}
CN_AMOUNT_RE = re.compile(r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整一二三四五六七八九十百千]+")


def quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _match_is_negative(text: str, match: re.Match[str]) -> bool:
    prefix = text[: match.start(1)]
    suffix = text[match.end(1) :]
    if re.search(r"[（(]\s*[¥￥]?\s*$", prefix) and re.match(r"\s*[）)]", suffix):
        return True
    if re.search(r"[-−]\s*[¥￥]?\s*$", prefix):
        return True
    return bool(re.match(r"\s*[-−]", suffix))


def has_amount_token(text: str) -> bool:
    return MONEY_RE.search(text) is not None or PLAIN_MONEY_RE.search(text) is not None


def text_has_negative_amount(text: str) -> bool:
    """Detect red-invoice notation: -1,060 / 1,060- / (1,060) / ￥-1,060."""
    for regex in (MONEY_RE, PLAIN_MONEY_RE):
        for match in regex.finditer(text):
            if _match_is_negative(text, match):
                return True
    return False


def parse_money(text: str) -> Decimal | None:
    """Parse a currency amount.

    Prefers a token with ¥/￥ so percentages and quantities are ignored. Falls
    back to a two-decimal token for layouts that drop the currency symbol.
    """
    match = MONEY_RE.search(text)
    if match is None:
        match = PLAIN_MONEY_RE.search(text)
    if match is None:
        return None
    # Do not turn a red (negative) invoice into a positive amount.
    if _match_is_negative(text, match):
        return None
    raw = match.group(1).replace(",", "")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if value <= 0 or value >= Decimal(100000000):
        return None
    return quantize(value)


def _parse_integer_part(text: str) -> int:
    total = 0
    section = 0
    number = 0
    for char in text:
        if char in CN_DIGITS:
            number = CN_DIGITS[char]
        elif char in CN_SMALL_UNITS:
            section += (number or 1) * CN_SMALL_UNITS[char]
            number = 0
        elif char == "万":
            section += number
            total += section * 10_000
            section = 0
            number = 0
        elif char == "亿":
            section += number
            total = (total + section) * 100_000_000
            section = 0
            number = 0
    return total + section + number


def _upper_group(value: int) -> str:
    """Render one four-digit group (0-9999) without leading zeroes."""
    text = ""
    pending_zero = False
    for position, unit in zip((1000, 100, 10, 1), UPPER_UNITS[::-1], strict=True):
        digit = value // position % 10
        if digit == 0:
            if text:
                pending_zero = True
            continue
        if pending_zero:
            text += "零"
            pending_zero = False
        text += UPPER_DIGITS[digit] + unit
    return text


def _upper_integer(value: int) -> str:
    """Render a non-negative integer in financial uppercase (``壹万零贰拾``)."""
    if value == 0:
        return "零"
    groups: list[int] = []
    while value:
        groups.append(value % 10000)
        value //= 10000
    if len(groups) > len(UPPER_GROUPS):
        raise ValueError("金额超出可转换范围")
    text = ""
    pending_zero = False
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if group == 0:
            if text and any(groups[lower] for lower in range(index)):
                pending_zero = True
            continue
        if text and (pending_zero or group < 1000):
            text += "零"
        text += _upper_group(group) + UPPER_GROUPS[index]
        pending_zero = False
    return text


def chinese_upper(value: Decimal | int | None) -> str:
    """Render an amount (yuan) as the financial uppercase string used on forms.

    ``15.48`` -> ``壹拾伍元肆角捌分``; whole yuan end in ``整``; a missing
    jiao digit is written as ``零X分`` (``1.05`` -> ``壹元零伍分``). Negative
    (red) totals keep a leading ``负``.
    """
    if value is None:
        return ""
    amount = quantize(Decimal(value))
    negative = amount < 0
    cents = int(abs(amount) * 100)
    integer, remainder = divmod(cents, 100)
    jiao, fen = divmod(remainder, 10)
    text = _upper_integer(integer) + "元"
    if jiao == 0 and fen == 0:
        text += "整"
    elif jiao == 0:
        text += "零" + UPPER_DIGITS[fen] + "分"
    elif fen == 0:
        text += UPPER_DIGITS[jiao] + "角"
    else:
        text += UPPER_DIGITS[jiao] + "角" + UPPER_DIGITS[fen] + "分"
    return ("负" + text) if negative else text


def chinese_upper_cents(value: int | None) -> str:
    """Uppercase rendering for an integer-cents amount (the database form)."""
    if value is None:
        return ""
    return chinese_upper(Decimal(value) / 100)


def parse_chinese_amount(text: str) -> Decimal | None:
    """Parse ``壹万贰仟圆整`` / ``贰佰叁拾肆圆肆角陆分`` into a Decimal."""
    source = normalize(text).replace("整", "")
    match = CN_AMOUNT_RE.search(source)
    if match is None:
        return None
    token = match.group(0)
    parts = re.split(r"[圆元]", token, maxsplit=1)
    integer_text = parts[0]
    decimal_text = parts[1] if len(parts) > 1 else ""

    integer = _parse_integer_part(integer_text)
    jiao = 0
    fen = 0
    jiao_match = re.search(r"([零壹贰叁肆伍陆柒捌玖一二三四五六七八九])角", decimal_text)
    fen_match = re.search(r"([零壹贰叁肆伍陆柒捌玖一二三四五六七八九])分", decimal_text)
    if jiao_match:
        jiao = CN_DIGITS[jiao_match.group(1)]
    if fen_match:
        fen = CN_DIGITS[fen_match.group(1)]

    value = Decimal(integer) + Decimal(jiao) / Decimal(10) + Decimal(fen) / Decimal(100)
    if value <= 0 or value >= Decimal(100000000):
        return None
    return quantize(value)
