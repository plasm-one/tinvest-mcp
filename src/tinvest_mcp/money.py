"""Money / Quotation conversion helpers.

T-Invest API uses ``MoneyValue`` and ``Quotation`` types that carry ``units``
(integer part) and ``nano`` (10^-9 fractional part). These MUST be converted
through :class:`~decimal.Decimal` and never through ``float``.

Functions here are intentionally SDK-agnostic: they only read the ``units`` /
``nano`` attributes (or accept a Decimal/str/int directly), so they can be unit
tested without the gRPC SDK installed.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

NANO = Decimal("1000000000")  # 10^9


def quotation_to_decimal(value: Any) -> Decimal | None:
    """Convert a ``Quotation`` / ``MoneyValue`` (units+nano) to ``Decimal``.

    Returns ``None`` when *value* is ``None``. Handles negative values where
    ``units`` and ``nano`` share the same sign (the API guarantees this).
    """

    if value is None:
        return None
    units = getattr(value, "units", None)
    nano = getattr(value, "nano", None)
    if units is None and nano is None:
        # Already a plain number / string.
        return Decimal(str(value))
    units = int(units or 0)
    nano = int(nano or 0)
    return Decimal(units) + Decimal(nano) / NANO


# Backwards-friendly alias: MoneyValue has the same units/nano shape.
money_to_decimal = quotation_to_decimal


def decimal_to_quotation_parts(value: Decimal) -> tuple[int, int]:
    """Split a ``Decimal`` into ``(units, nano)`` with consistent signs.

    Both parts carry the sign of *value* (matching the T-Invest convention),
    so ``-1.5`` -> ``(-1, -500000000)``.
    """

    value = Decimal(value)
    # Truncate toward zero for the integer part.
    units = int(value.to_integral_value(rounding=ROUND_DOWN))
    fractional = value - Decimal(units)
    nano = int((fractional * NANO).to_integral_value(rounding=ROUND_DOWN))
    return units, nano


def quantize_to_increment(
    price: Decimal,
    increment: Decimal | None,
    *,
    direction: str = "buy",
    round_up: bool = False,
    round_down: bool = False,
) -> Decimal:
    """Snap *price* to a valid ``min_price_increment`` grid.

    Rounding direction must not worsen the order: for a BUY
    limit we round **down** (never pay more than intended); for a SELL limit we
    round **up**. Pass ``round_up=True`` for aggressive BUY tiers (cross-ask)
    so the snapped price does not fall below the intended level, and
    ``round_down=True`` for aggressive SELL tiers (cross-bid) so it does not
    rise above the bid. When *increment* is missing or non-positive the price
    is returned unchanged.
    """

    if increment is None or increment <= 0:
        return price
    steps = price / increment
    if round_up:
        rounding = ROUND_UP
    elif round_down:
        rounding = ROUND_DOWN
    else:
        rounding = ROUND_DOWN if direction.lower() == "buy" else ROUND_UP
    snapped_steps = steps.to_integral_value(rounding=rounding)
    return (snapped_steps * increment).normalize()


def is_aligned_to_increment(price: Decimal, increment: Decimal | None) -> bool:
    """Return ``True`` when *price* lies exactly on the increment grid."""

    if increment is None or increment <= 0:
        return True
    remainder = price % increment
    return remainder == 0
