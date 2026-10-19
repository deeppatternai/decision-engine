"""Shared validation for the GE Chat wire-price contract."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

_PRICE_SCALE = 1_000_000
_PRICE_QUANTUM = Decimal("0.000001")
MAX_SAFE_MICRO_UNITS = (1 << 53) - 1
_CANONICAL_PRICE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$")


def validate_chat_price(value: object) -> int | str:
    """Return an integer or canonical decimal string exact in JS micro-units."""

    if isinstance(value, bool):
        raise ValueError("invalid_ge_chat_price")
    try:
        if type(value) is int:
            amount = Decimal(value)
        elif type(value) is Decimal:
            amount = value
        elif type(value) is str and _CANONICAL_PRICE.fullmatch(value):
            amount = Decimal(value)
        else:
            raise ValueError("invalid_ge_chat_price")
        quantized = amount.quantize(_PRICE_QUANTUM)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid_ge_chat_price") from None
    micro_units = amount * _PRICE_SCALE
    if (
        not amount.is_finite()
        or amount < 0
        or (amount.is_zero() and amount.is_signed())
        or amount != quantized
        or micro_units != micro_units.to_integral_value()
        or micro_units > MAX_SAFE_MICRO_UNITS
    ):
        raise ValueError("invalid_ge_chat_price")
    if type(value) is int:
        return value
    rendered = format(amount, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"
