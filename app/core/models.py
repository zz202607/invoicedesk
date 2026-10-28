"""Domain models shared by the parser, database, and exporters."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from app.core.text import normalize


class InvoiceType(str, Enum):
    FULL_DIGITAL_NORMAL = "full_digital_normal"
    FULL_DIGITAL_SPECIAL = "full_digital_special"
    VAT_ELECTRONIC = "vat_electronic"
    TOLL = "toll"
    TRAIN = "train"
    AIR_ITINERARY = "air_itinerary"
    TAXI = "taxi"
    UNKNOWN = "unknown"
    SCANNED = "scanned"
    ERROR = "error"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class ParsedInvoice:
    source_path: str
    page_index: int = 0
    page_count: int = 1
    invoice_type: InvoiceType = InvoiceType.UNKNOWN
    invoice_number: str = ""
    invoice_date: str = ""
    seller_name: str = ""
    seller_tax_id: str = ""
    buyer_name: str = ""
    buyer_tax_id: str = ""
    amount_total: Decimal | None = None
    amount_net: Decimal | None = None
    tax_amount: Decimal | None = None
    item_summary: str = ""
    category: str = "其他费用"
    category_rule: str = "fallback"
    confidence: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    text_length: int = 0

    @property
    def needs_review(self) -> bool:
        if self.invoice_type in {InvoiceType.UNKNOWN, InvoiceType.SCANNED, InvoiceType.ERROR}:
            return True
        if not self.invoice_number or not self.invoice_date or self.amount_total is None:
            return True
        return bool(self.warnings)

    def to_dict(
        self,
        *,
        include_absolute_path: bool = False,
        include_tax_ids: bool = False,
    ) -> dict[str, Any]:
        """Serialize without leaking paths or full tax IDs by default.

        Amounts are strings to avoid float drift. Local JSON output can opt into
        the full path/tax IDs for debugging, but it must not be committed.
        """
        data: dict[str, Any] = {
            "source_name": normalize(Path(self.source_path).name),
            "page": self.page_index + 1,
            "page_count": self.page_count,
            "invoice_type": self.invoice_type.value,
            "invoice_number": self.invoice_number,
            "invoice_date": self.invoice_date,
            "seller_name": normalize(self.seller_name),
            "seller_tax_id": self.seller_tax_id if include_tax_ids else _mask_tax(self.seller_tax_id),
            "buyer_name": normalize(self.buyer_name),
            "buyer_tax_id": self.buyer_tax_id if include_tax_ids else _mask_tax(self.buyer_tax_id),
            "amount_total": _money(self.amount_total),
            "amount_net": _money(self.amount_net),
            "tax_amount": _money(self.tax_amount),
            "item_summary": normalize(self.item_summary),
            "category": self.category,
            "category_rule": self.category_rule,
            "confidence": dict(self.confidence),
            "warnings": [normalize(warning) for warning in self.warnings],
            "text_length": self.text_length,
            "needs_review": self.needs_review,
        }
        if include_absolute_path:
            data["source_path"] = normalize(self.source_path)
        return data


def _money(value: Decimal | None) -> str | None:
    """Serialize money as a fixed two-decimal string to avoid float drift."""
    return None if value is None else format(value, ".2f")


def _mask_tax(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]
