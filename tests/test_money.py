from decimal import Decimal
from types import SimpleNamespace

from tinvest_mcp.money import (
    decimal_to_quotation_parts,
    is_aligned_to_increment,
    quantize_to_increment,
    quotation_to_decimal,
)


def q(units, nano):
    return SimpleNamespace(units=units, nano=nano)


def test_quotation_to_decimal_positive():
    assert quotation_to_decimal(q(114, 250000000)) == Decimal("114.25")


def test_quotation_to_decimal_negative():
    assert quotation_to_decimal(q(-1, -500000000)) == Decimal("-1.5")


def test_quotation_to_decimal_none():
    assert quotation_to_decimal(None) is None


def test_decimal_to_quotation_parts_roundtrip():
    for value in ["0", "1.5", "100.10", "-2.25", "98.000000001"]:
        units, nano = decimal_to_quotation_parts(Decimal(value))
        assert quotation_to_decimal(q(units, nano)) == Decimal(value)


def test_decimal_to_quotation_parts_negative_signs_match():
    units, nano = decimal_to_quotation_parts(Decimal("-1.5"))
    assert units == -1 and nano == -500000000


def test_quantize_buy_rounds_down():
    # never pay more than intended on a BUY
    assert quantize_to_increment(Decimal("100.117"), Decimal("0.01"), direction="buy") == Decimal("100.11")


def test_quantize_buy_round_up():
    assert quantize_to_increment(
        Decimal("99.175"),
        Decimal("0.01"),
        direction="buy",
        round_up=True,
    ) == Decimal("99.18")


def test_quantize_no_increment_is_passthrough():
    assert quantize_to_increment(Decimal("100.117"), None) == Decimal("100.117")


def test_is_aligned_to_increment():
    assert is_aligned_to_increment(Decimal("100.11"), Decimal("0.01")) is True
    assert is_aligned_to_increment(Decimal("100.117"), Decimal("0.01")) is False
    assert is_aligned_to_increment(Decimal("100.117"), None) is True
