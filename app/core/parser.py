"""Invoice parser.

Design: geometry first, text order never. Values are located relative to their
labels, and the amount is cross-checked against the Chinese uppercase amount and
the net+tax row. Anything uncertain is marked for review instead of guessed
silently.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import fitz

from app.core.amounts import (
    has_amount_token,
    parse_chinese_amount,
    parse_money,
    text_has_negative_amount,
)
from app.core.categories import categorize
from app.core.dates import extract_date
from app.core.models import Confidence, InvoiceType, ParsedInvoice
from app.core.text import (
    TextLine,
    extract_lines,
    find_lines,
    nearest_line,
    nearest_right,
    page_text,
    strip_label,
)

NUMBER_RE = re.compile(r"\d{8,20}")
ITEM_RE = re.compile(r"\*([^*]+)\*([^*]+)")
TAX_ID_RE = re.compile(
    r"(?<![0-9A-Z])(?:(?=[0-9A-Z]*[A-Z])[0-9A-Z]{15,20}|\d{15,18})(?![0-9A-Z])"
)


def _confidence(value: Confidence) -> str:
    return value.value


def _digits(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _infer_ocr_type(text: str) -> InvoiceType | None:
    """Name the e-invoice type from OCR title fragments around the red seal."""
    if "发票号码" not in text:
        return None
    if "专用" in text:
        return InvoiceType.FULL_DIGITAL_SPECIAL
    if "普通" in text or "电子发" in text:
        return InvoiceType.FULL_DIGITAL_NORMAL
    return None


def _detect_type(text: str) -> InvoiceType:
    if "铁路电子客票" in text:
        return InvoiceType.TRAIN
    if "航空运输电子客票行程单" in text or ("航空" in text and "行程单" in text):
        return InvoiceType.AIR_ITINERARY
    if "通行费" in text and ("车牌号" in text or "通行日期" in text):
        return InvoiceType.TOLL
    if "运输服务" in text and "客运服务费" in text:
        return InvoiceType.TAXI
    if "增值税电子普通发票" in text:
        return InvoiceType.VAT_ELECTRONIC
    if "电子发票" in text:
        if "专用发票" in text:
            return InvoiceType.FULL_DIGITAL_SPECIAL
        # The red seal over the title often hides 「普通」from OCR; an
        # e-invoice number is still strong evidence of a normal invoice.
        if "普通发票" in text or "发票号码" in text:
            return InvoiceType.FULL_DIGITAL_NORMAL
    return InvoiceType.UNKNOWN


def _number_candidates(lines: Sequence[TextLine]) -> list[TextLine]:
    return [line for line in lines if NUMBER_RE.fullmatch(_digits(line.text))]


def _extract_invoice_number(lines: Sequence[TextLine]) -> tuple[str, str, str]:
    candidates = _number_candidates(lines)
    anchors = find_lines(lines, r"发票号码")

    # Values belonging to 发票代码 / 校验码 / 机器编号 are not invoice numbers.
    excluded: set[int] = set()
    for label in find_lines(lines, r"发票代码|校验码|机器编号"):
        value = nearest_right(label, candidates)
        if value is not None:
            excluded.add(id(value))

    for anchor in anchors:
        inline = re.search(r"发票号码\s*[:：]?\s*(\d{8,20})", anchor.text)
        if inline:
            return inline.group(1), _confidence(Confidence.HIGH), ""
        value = nearest_right(anchor, candidates)
        if value is not None:
            return _digits(value.text), _confidence(Confidence.HIGH), ""
        value = nearest_line(
            anchor,
            [item for item in candidates if id(item) not in excluded],
            max_distance=90,
        )
        if value is not None:
            return _digits(value.text), _confidence(Confidence.MEDIUM), "发票号码按相邻值推断"

    remaining = [item for item in candidates if id(item) not in excluded]
    if remaining:
        remaining.sort(key=lambda item: (0 if len(_digits(item.text)) == 20 else 1, item.y0))
        return (
            _digits(remaining[0].text),
            _confidence(Confidence.MEDIUM),
            "发票号码按候选推断",
        )
    return "", _confidence(Confidence.LOW), "发票号码缺失"


def _extract_invoice_date(lines: Sequence[TextLine]) -> tuple[str, str, str]:
    dated = [(line, extract_date(line.text, allow_compact=True)) for line in lines]
    dated = [(line, value) for line, value in dated if value]
    anchors = find_lines(lines, r"开票日期")

    for anchor in anchors:
        inline = extract_date(anchor.text, allow_compact=True)
        if inline:
            return inline, _confidence(Confidence.HIGH), ""
        value_line = nearest_right(anchor, [line for line, _ in dated])
        if value_line is not None:
            return extract_date(value_line.text, allow_compact=True) or "", _confidence(Confidence.HIGH), ""
        value_line = nearest_line(anchor, [line for line, _ in dated], max_distance=100)
        if value_line is not None:
            return (
                extract_date(value_line.text, allow_compact=True) or "",
                _confidence(Confidence.MEDIUM),
                "开票日期按相邻值推断",
            )

    if dated:
        return dated[0][1] or "", _confidence(Confidence.MEDIUM), "开票日期按候选推断"
    return "", _confidence(Confidence.LOW), "开票日期缺失"


def _clean_name(text: str) -> str:
    value = strip_label(text, "名称")
    value = re.sub(r"^[\s:：]+", "", value)
    # Some invoices append the buyer's phone account to the name line.
    value = re.sub(r"[（(](?:号码|电话|手机)[^）)]*[）)]?.*$", "", value)
    value = re.sub(r"[（(][^）)]*[:：][^）)]*[）)]?.*$", "", value)
    return value.strip()


def _name_candidates(lines: Sequence[TextLine]) -> list[TextLine]:
    result: list[TextLine] = []
    skip = (
        "纳税人识别号",
        "统一社会信用代码",
        "地址",
        "开户行",
        "电话",
        "购买方",
        "销售方",
        "发票",
        "税务",
        "密码区",
        "校验码",
        "日期",
        "金额",
        "合计",
        "备注",
        "项目",
        "单位",
        "数量",
        "单价",
        "税率",
        "税额",
        "开票人",
        "收款人",
        "复核",
    )
    for line in lines:
        value = _clean_name(line.text)[:200]
        if not 2 <= len(value) <= 60:
            continue
        if not re.search(r"[一-鿿]", value):
            continue
        if any(token in value for token in skip):
            continue
        if value.startswith("*") or ITEM_RE.search(value):
            # Item rows (``*类别*名称``) sit close to the party block but are
            # never company names.
            continue
        if re.search(r"[¥￥]", value) or re.search(r"[零壹贰叁肆伍陆柒捌玖]", value):
            # Currency and uppercase-amount lines sit near the party block on
            # OCR output but are never company names.
            continue
        if re.search(r"\d{4,}", value) or re.search(r"[:：]", value):
            continue
        result.append(TextLine(value, line.x0, line.y0, line.x1, line.y1))
    return result


def _split_at(line: TextLine, start: int, end: int) -> TextLine:
    """Slice a text line horizontally at character positions.

    OCR sometimes merges the buyer and seller tax-ID rows into one wide box.
    Character positions are not pixel-exact, but they are enough to keep the
    two parties attached to the correct name column.
    """
    length = max(len(line.text), 1)
    width = line.x1 - line.x0
    x0 = line.x0 + width * (start / length)
    x1 = line.x0 + width * (end / length)
    return TextLine(line.text[start:end], x0, line.y0, x1, line.y1)


def _tax_id_groups(
    lines: Sequence[TextLine],
) -> list[tuple[TextLine, list[tuple[int, int, str]]]]:
    """Tax IDs per text line, de-duplicated and keeping character offsets."""
    result: list[tuple[TextLine, list[tuple[int, int, str]]]] = []
    seen: set[str] = set()
    for line in lines:
        matches: list[tuple[int, int, str]] = []
        for match in TAX_ID_RE.finditer(line.text):
            tax_id = match.group(0)
            if tax_id in seen:
                continue
            seen.add(tax_id)
            matches.append((match.start(), match.end(), tax_id))
        if matches:
            result.append((line, matches))
    return result


def _party_distance(label: TextLine, pair: tuple[TextLine, str, TextLine]) -> float:
    tax_line = pair[0]
    return abs(label.cx - tax_line.cx) + abs(label.cy - tax_line.cy) * 1.5


def _order_parties(
    pairs: list[tuple[TextLine, str, TextLine]],
    lines: Sequence[TextLine],
    page_width: float,
) -> list[tuple[TextLine, str, TextLine]]:
    """Put the buyer block first, using the printed 购买方/销售方 labels.

    OCR can report the two tax-ID rows a few points out of vertical order, so
    "topmost pair is the buyer" is not reliable. The labels sit next to their
    blocks; when they are missing, a left/right layout is the fallback, and a
    vertically stacked layout keeps the row order.
    """
    if len(pairs) < 2:
        return pairs
    head = pairs[:2]
    rest = pairs[2:]
    buyer_labels = [line for line in lines if "购买方" in line.text]
    seller_labels = [line for line in lines if "销售方" in line.text]
    if buyer_labels and seller_labels:
        buyer_label, seller_label = buyer_labels[0], seller_labels[0]
        direct = _party_distance(buyer_label, head[0]) + _party_distance(seller_label, head[1])
        swapped = _party_distance(buyer_label, head[1]) + _party_distance(seller_label, head[0])
        if swapped < direct:
            head.reverse()
    elif buyer_labels or seller_labels:
        buyer_first = bool(buyer_labels)
        label = (buyer_labels or seller_labels)[0]
        closest = min(range(2), key=lambda index: _party_distance(label, head[index]))
        if (closest == 0) != buyer_first:
            head.reverse()
    else:
        first, second = head
        side_by_side = (
            abs(first[0].cy - second[0].cy) <= 40
            and abs(first[0].x0 - second[0].x0) >= page_width * 0.2
        )
        head.sort(key=lambda pair: pair[0].x0 if side_by_side else pair[0].cy)
    return [*head, *rest]


def _partner_name(
    names: Sequence[TextLine],
    used_names: set[int],
    anchor: TextLine,
    *,
    to_right: bool,
) -> TextLine | None:
    """Find the unlabeled party next to a known one.

    OCR sometimes loses one tax-ID line entirely while still reading both
    names; the surviving pair tells us which role is missing, and the nearest
    unused name on the other column can fill it.
    """
    best: TextLine | None = None
    best_score = float("inf")
    for name in names:
        if id(name) in used_names or id(name) == id(anchor):
            continue
        if abs(name.cy - anchor.cy) > 25:
            continue
        if to_right and name.x0 <= anchor.x0 + 40:
            continue
        if not to_right and name.x0 >= anchor.x0 - 40:
            continue
        score = abs(name.x0 - anchor.x0)
        if score < best_score:
            best, best_score = name, score
    return best


def _extract_parties(
    lines: Sequence[TextLine], invoice_type: InvoiceType, page_width: float
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    warnings: list[str] = []
    names = _name_candidates(lines)

    if invoice_type is InvoiceType.TRAIN:
        buyer_name = ""
        label = find_lines(lines, r"购买方名称")
        if label:
            value = nearest_right(label[0], names)
            if value is not None:
                buyer_name = value.text

    pairs: list[tuple[TextLine, str, TextLine]] = []
    used_names: set[int] = set()
    unpaired: list[tuple[TextLine, str]] = []

    def _segment(line: TextLine, start: int, end: int) -> TextLine:
        if start == 0 and end == len(line.text):
            return line
        return _split_at(line, start, end)

    for line, matches in _tax_id_groups(lines):
        if len(matches) > 1:
            # OCR merged the buyer and seller rows into one wide line. Column
            # order is reliable even when pixel estimates are not: pair the
            # left-to-right tax IDs with the left-to-right names on that row.
            row_names = [
                name
                for name in names
                if id(name) not in used_names
                and -8 <= line.cy - name.cy <= 60
                and line.x0 - 20 <= name.x0 <= line.x1 + 20
            ]
            row_names.sort(key=lambda item: item.x0)
            paired = min(len(matches), len(row_names))
            for (start, end, tax_id), name in zip(matches[:paired], row_names):
                pairs.append((_segment(line, start, end), tax_id, name))
                used_names.add(id(name))
            unpaired.extend(
                (_segment(line, start, end), tax_id)
                for start, end, tax_id in matches[paired:]
            )
        else:
            start, end, tax_id = matches[0]
            unpaired.append((_segment(line, start, end), tax_id))

    for tax_line, tax_id in unpaired:
        best_name: TextLine | None = None
        best_score = float("inf")
        for name in names:
            if id(name) in used_names or id(name) == id(tax_line):
                continue
            dy = tax_line.cy - name.cy
            if dy < -8 or dy > 60:
                continue
            dx = abs(name.x0 - tax_line.x0)
            if dx > 260:
                continue
            # Column alignment (x0) is more stable than centers when the
            # buyer/seller names have different lengths.
            score = max(dy, 0) * 2 + dx * 0.25
            if score < best_score:
                best_name, best_score = name, score
        if best_name is not None:
            pairs.append((tax_line, tax_id, best_name))
            used_names.add(id(best_name))

    if invoice_type is InvoiceType.TRAIN:
        pairs.sort(key=lambda item: (round(item[0].cy, 1), item[0].x0))
    else:
        pairs = _order_parties(pairs, lines, page_width)

    values: dict[str, str] = {
        "buyer_name": "",
        "buyer_tax_id": "",
        "seller_name": "",
        "seller_tax_id": "",
    }
    confidence: dict[str, str] = {
        "buyer_name": _confidence(Confidence.LOW),
        "seller_name": _confidence(Confidence.LOW),
    }

    if invoice_type is InvoiceType.TRAIN:
        if buyer_name:
            values["buyer_name"] = buyer_name
        if pairs:
            values["buyer_tax_id"] = pairs[0][1]
        if len(pairs) > 1:
            values["seller_name"] = pairs[1][2].text
            values["seller_tax_id"] = pairs[1][1]
        else:
            values["seller_name"] = "中国铁路"
        confidence["buyer_name"] = (
            _confidence(Confidence.HIGH) if buyer_name else _confidence(Confidence.LOW)
        )
        confidence["seller_name"] = _confidence(Confidence.MEDIUM)
        return values, confidence, warnings

    if len(pairs) >= 2:
        values["buyer_name"] = pairs[0][2].text
        values["buyer_tax_id"] = pairs[0][1]
        values["seller_name"] = pairs[1][2].text
        values["seller_tax_id"] = pairs[1][1]
        confidence["buyer_name"] = _confidence(Confidence.HIGH)
        confidence["seller_name"] = _confidence(Confidence.HIGH)
    elif len(pairs) == 1:
        only = pairs[0]
        if only[0].x0 >= page_width * 0.5:
            values["seller_name"] = only[2].text
            values["seller_tax_id"] = only[1]
            confidence["seller_name"] = _confidence(Confidence.MEDIUM)
            partner = _partner_name(names, used_names, only[2], to_right=False)
            if partner is not None:
                values["buyer_name"] = partner.text
                confidence["buyer_name"] = _confidence(Confidence.MEDIUM)
                warnings.append("仅识别到销售方税号，购买方名称按位置推断")
            else:
                warnings.append("仅识别到销售方，购买方信息缺失")
        else:
            values["buyer_name"] = only[2].text
            values["buyer_tax_id"] = only[1]
            confidence["buyer_name"] = _confidence(Confidence.MEDIUM)
            partner = _partner_name(names, used_names, only[2], to_right=True)
            if partner is not None:
                values["seller_name"] = partner.text
                confidence["seller_name"] = _confidence(Confidence.MEDIUM)
                warnings.append("仅识别到购买方税号，销售方名称按位置推断")
            else:
                warnings.append("仅识别到一个交易方，无法区分购买方/销售方")
    else:
        warnings.append("销售方缺失")

    return values, confidence, warnings


def _amount_candidates(lines: Sequence[TextLine]) -> list[tuple[TextLine, Decimal]]:
    result: list[tuple[TextLine, Decimal]] = []
    for line in lines:
        value = parse_money(line.text)
        if value is not None:
            result.append((line, value))
    return result


def _extract_amounts(
    lines: Sequence[TextLine], invoice_type: InvoiceType
) -> tuple[dict[str, Decimal | None], dict[str, str], list[str]]:
    warnings: list[str] = []
    candidates = _amount_candidates(lines)
    candidate_lines = [line for line, _ in candidates]
    if invoice_type is InvoiceType.TRAIN:
        anchors = find_lines(lines, r"票价")
    else:
        anchors = [line for line in lines if "小写" in line.text or "价税合计" in line.text]

    total: Decimal | None = None
    total_conf = Confidence.LOW

    # Prefer the (小写) anchor; 价税合计 / 票价 are fallbacks.
    anchors.sort(
        key=lambda line: (
            0 if "(小写)" in line.text else 1 if "价税合计" in line.text else 2,
            line.y0,
        )
    )

    for anchor in anchors:
        inline = parse_money(anchor.text)
        if inline is not None:
            total, total_conf = inline, Confidence.HIGH
            break
        value_line = nearest_right(anchor, candidate_lines, max_dy=10, max_dx=260)
        if value_line is not None:
            total = parse_money(value_line.text)
            total_conf = Confidence.HIGH
            break
        value_line = nearest_line(anchor, candidate_lines, max_distance=90)
        if value_line is not None:
            total = parse_money(value_line.text)
            total_conf = Confidence.MEDIUM
            break

    if total is None and candidates:
        _, best_value = max(candidates, key=lambda item: item[1])
        total, total_conf = best_value, Confidence.LOW
        warnings.append("金额未找到锚点，按候选最大值推断，请人工确认")

    # Uppercase amount is the authority when both exist.
    uppercase: Decimal | None = None
    for line in lines:
        if ("圆" in line.text or "元" in line.text) and re.search(
            r"[零壹贰叁肆伍陆柒捌玖]", line.text
        ):
            uppercase = parse_chinese_amount(line.text)
            if uppercase is not None:
                break

    if total is not None and uppercase is not None:
        if abs(total - uppercase) > Decimal("0.01"):
            warnings.append("小写金额与大写金额不一致，采用大写金额")
            total, total_conf = uppercase, Confidence.MEDIUM
        elif total_conf is Confidence.LOW:
            total_conf = Confidence.MEDIUM
    elif total is None and uppercase is not None:
        total = uppercase
        total_conf = Confidence.MEDIUM
        warnings.append("仅识别到大写金额")
    elif total is not None and uppercase is None and total_conf is Confidence.HIGH:
        total_conf = Confidence.MEDIUM

    net: Decimal | None = None
    tax: Decimal | None = None
    part_anchors = [
        line
        for line in lines
        if re.fullmatch(r"合\s*计|合|计", line.text.strip())
    ]
    for anchor in part_anchors:
        row = [
            (line, value)
            for line, value in candidates
            if abs(line.cy - anchor.cy) <= 8 and line.x0 >= anchor.x1 - 10
        ]
        row.sort(key=lambda item: item[0].x0)
        if len(row) >= 2:
            net, tax = row[0][1], row[1][1]
            break

    if net is not None and tax is not None:
        if total is not None and abs(net + tax - total) > Decimal("0.02"):
            warnings.append("价税合计与「金额+税额」不一致")
        if total is None:
            total = net + tax
            total_conf = Confidence.MEDIUM
            warnings.append("价税合计缺失，按「金额+税额」计算")

    total_anchors = [
        line for line in lines if "小写" in line.text or "价税合计" in line.text or "票价" in line.text
    ]
    amount_like = [line for line in lines if has_amount_token(line.text)]
    negative = any(
        "红字" in line.text or "负数" in line.text for line in total_anchors
    ) or any(
        text_has_negative_amount(line.text)
        for line in lines
        if line in total_anchors
    )
    if not negative:
        for anchor in total_anchors:
            nearby = [
                line
                for line in amount_like
                if abs(line.cy - anchor.cy) <= 12
                or (line.y0 >= anchor.y1 and line.y0 - anchor.y1 <= 60)
            ]
            if any(text_has_negative_amount(line.text) for line in nearby):
                negative = True
                break

    if negative:
        warnings.append("红字/负数发票，金额需人工确认")
        total, net, tax = None, None, None
        total_conf = Confidence.LOW
    elif total is None:
        warnings.append("金额缺失")

    return (
        {"total": total, "net": net, "tax": tax},
        {"amount_total": _confidence(total_conf)},
        warnings,
    )


def _plausible_item(category: str, name: str) -> bool:
    """Reject password-area fragments that also contain ``*``."""
    if not category or not name or len(category) > 12 or len(name) > 60:
        return False
    if not re.search(r"[一-鿿]", category) or not re.search(r"[一-鿿]", name):
        return False
    if re.search(r"[0-9]", category):
        return False
    if re.search(r"[<>/\\+=*|]", name):
        return False
    return len(re.findall(r"[一-鿿]", name)) >= 2


def _extract_items(lines: Sequence[TextLine], invoice_type: InvoiceType) -> str:
    items: list[str] = []
    for line in lines:
        for match in ITEM_RE.finditer(line.text):
            category, name = match.group(1).strip(), match.group(2).strip()
            name = re.sub(r"\s+", " ", name)
            if _plausible_item(category, name) and name not in items:
                items.append(name)
    if items:
        return "；".join(items)

    if invoice_type is InvoiceType.TRAIN:
        stations = [
            line.text.strip()
            for line in lines
            if line.text.strip().endswith("站")
            and 2 <= len(line.text.strip()) <= 8
            and "旁" not in line.text
        ]
        if len(stations) >= 2:
            return f"铁路客票（{stations[0]}→{stations[1]}）"
        return "铁路客票"
    if invoice_type is InvoiceType.TAXI:
        return "客运服务费"
    return ""


OCR_WARNING = "票面无可用文本层，已用 OCR 识别，请核对"


def _needs_ocr(text: str, page: fitz.Page) -> bool:
    stripped = text.strip()
    if len(stripped) < 20:
        return True
    if _detect_type(text) is not InvoiceType.UNKNOWN:
        return False
    # A text layer that has almost no Chinese is usually a font-encoding
    # artifact (CID fonts without ToUnicode) while the page itself is a raster.
    cjk = len(re.findall(r"[一-鿿]", stripped))
    return cjk < 8 and bool(page.get_images(full=True))


def _ocr_lines(page: fitz.Page) -> tuple[list[TextLine] | None, str]:
    """Return OCR lines for ``page`` or an error message; never raises."""
    try:
        from app.core.ocr import OcrError, page_lines

        lines = page_lines(page)
    except OcrError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - OCR is an optional recovery path
        return None, f"OCR 不可用：{exc}"
    return lines, ""


def parse_page(page: fitz.Page, *, source_path: str, page_index: int, page_count: int) -> ParsedInvoice:
    lines = extract_lines(page)
    text = page_text(lines)
    parsed = ParsedInvoice(
        source_path=source_path,
        page_index=page_index,
        page_count=page_count,
        text_length=len(text),
    )

    ocr_used = False
    ocr_error = ""
    if _needs_ocr(text, page):
        ocr_result, ocr_error = _ocr_lines(page)
        if ocr_result is not None and len(page_text(ocr_result).strip()) >= 20:
            lines = ocr_result
            text = page_text(lines)
            ocr_used = True
            parsed.text_length = len(text)

    if len(text.strip()) < 20:
        parsed.invoice_type = InvoiceType.SCANNED
        if ocr_used:  # pragma: no cover - OCR returned text then lost it
            parsed.warnings.append("无文本层，OCR 结果为空")
        elif ocr_error:
            parsed.warnings.append(f"无文本层，{ocr_error}")
        else:
            parsed.warnings.append("无文本层，可能是扫描件/图片发票")
        parsed.confidence = {"invoice_type": _confidence(Confidence.LOW)}
        return parsed

    parsed.invoice_type = _detect_type(text)
    type_note = ""
    if ocr_used and parsed.invoice_type is InvoiceType.UNKNOWN:
        inferred = _infer_ocr_type(text)
        if inferred is not None:
            parsed.invoice_type = inferred
            type_note = "票型按 OCR 标题片段推断"
    parsed.invoice_number, number_conf, number_warning = _extract_invoice_number(lines)
    parsed.invoice_date, date_conf, date_warning = _extract_invoice_date(lines)
    parties, party_conf, party_warnings = _extract_parties(
        lines, parsed.invoice_type, page.rect.width
    )
    amounts, amount_conf, amount_warnings = _extract_amounts(lines, parsed.invoice_type)
    parsed.item_summary = _extract_items(lines, parsed.invoice_type)
    category = categorize(
        item_summary=parsed.item_summary,
        seller_name=parties["seller_name"],
        file_name=Path(source_path).name,
        full_text=text,
    )
    parsed.category = category.category
    parsed.category_rule = category.rule

    parsed.buyer_name = parties["buyer_name"]
    parsed.buyer_tax_id = parties["buyer_tax_id"]
    parsed.seller_name = parties["seller_name"]
    parsed.seller_tax_id = parties["seller_tax_id"]
    parsed.amount_total = amounts["total"]
    parsed.amount_net = amounts["net"]
    parsed.tax_amount = amounts["tax"]

    parsed.confidence = {
        "invoice_type": _confidence(
            Confidence.HIGH if parsed.invoice_type is not InvoiceType.UNKNOWN else Confidence.LOW
        ),
        "invoice_number": number_conf,
        "invoice_date": date_conf,
        "seller_name": party_conf["seller_name"],
        "buyer_name": party_conf["buyer_name"],
        "amount_total": amount_conf["amount_total"],
        "item": _confidence(Confidence.HIGH if parsed.item_summary else Confidence.LOW),
        "category": _confidence(
            Confidence.MEDIUM if parsed.category_rule == "fallback" else Confidence.HIGH
        ),
    }

    for warning in (
        type_note,
        number_warning,
        date_warning,
        *party_warnings,
        *amount_warnings,
    ):
        if warning and warning not in parsed.warnings:
            parsed.warnings.append(warning)
    if parsed.invoice_type is InvoiceType.UNKNOWN:
        parsed.warnings.append("未识别票型")
    if len(find_lines(lines, r"发票号码")) > 1 or len(find_lines(lines, r"小写")) > 1:
        parsed.warnings.append("页面可能包含多张发票，需人工拆分")
    if ocr_used:
        parsed.confidence = {
            key: (_confidence(Confidence.MEDIUM) if value == _confidence(Confidence.HIGH) else value)
            for key, value in parsed.confidence.items()
        }
        parsed.warnings.insert(0, OCR_WARNING)
    return parsed


def parse_pdf(path: str | Path) -> list[ParsedInvoice]:
    source = str(path)
    try:
        document = fitz.open(source)
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - corrupted files
        return [
            ParsedInvoice(
                source_path=source,
                invoice_type=InvoiceType.ERROR,
                warnings=[f"无法打开 PDF：{exc}"],
            )
        ]

    try:
        if document.needs_pass:
            return [
                ParsedInvoice(
                    source_path=source,
                    invoice_type=InvoiceType.ERROR,
                    warnings=["PDF 已加密，无法解析"],
                )
            ]
        if document.page_count == 0:
            return [
                ParsedInvoice(
                    source_path=source,
                    invoice_type=InvoiceType.ERROR,
                    warnings=["PDF 没有页面"],
                )
            ]
        results: list[ParsedInvoice] = []
        for index, page in enumerate(document):
            try:
                results.append(
                    parse_page(page, source_path=source, page_index=index, page_count=document.page_count)
                )
            except Exception as exc:  # noqa: BLE001  # pragma: no cover - per-page guard
                results.append(
                    ParsedInvoice(
                        source_path=source,
                        page_index=index,
                        page_count=document.page_count,
                        invoice_type=InvoiceType.ERROR,
                        warnings=[f"页面解析失败：{exc}"],
                    )
                )
        return results
    finally:
        document.close()


@dataclass(frozen=True)
class ParseSummary:
    total: int
    review: int
    amount: Decimal


def summarize(invoices: Sequence[ParsedInvoice]) -> ParseSummary:
    return ParseSummary(
        total=len(invoices),
        review=sum(1 for invoice in invoices if invoice.needs_review),
        amount=sum((invoice.amount_total or Decimal(0) for invoice in invoices), Decimal(0)),
    )
