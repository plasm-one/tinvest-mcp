"""Unit tests for the currency-awareness helpers (no SDK, no network)."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tinvest_mcp import fx


def money(units, nano=0, currency="cny"):
    return SimpleNamespace(units=units, nano=nano, currency=currency)


def setup_function(_):
    fx.clear_cache()


def test_denomination_currency_prefers_the_nominal():
    # The trap this module exists for: a yuan bond listed on a ruble board.
    assert fx.denomination_currency("rub", "cny") == "cny"
    assert fx.denomination_currency("rub", None) == "rub"
    assert fx.denomination_currency("RUB", "") == "rub"
    assert fx.denomination_currency(None, None) is None


def test_is_fx_linked():
    assert fx.is_fx_linked("cny")
    assert not fx.is_fx_linked("rub")
    assert not fx.is_fx_linked(None)


def test_rate_from_instrument_divides_by_the_nominal():
    # CNYRUB_TOM quotes rubles per 1 CNY.
    cny = SimpleNamespace(nominal=money(1, 0, "cny"))
    assert fx.rate_from_instrument(cny, Decimal("11.5615")) == Decimal("11.5615")
    # KZTRUB_TOM quotes rubles per 100 KZT — the price alone is not a rate.
    kzt = SimpleNamespace(nominal=money(100, 0, "kzt"))
    assert fx.rate_from_instrument(kzt, Decimal("18.5")) == Decimal("0.185")
    assert fx.rate_from_instrument(cny, None) is None
    assert fx.rate_from_instrument(cny, Decimal("0")) is None


class FxAdapter:
    """Minimal currency catalogue with a call counter for the cache test."""

    def __init__(self):
        self.currency_calls = 0
        self.price_calls = 0

    def list_currencies(self):
        self.currency_calls += 1
        return [
            SimpleNamespace(uid="kzt-uid", ticker="KZTRUB_TOM", iso_currency_name="kzt", nominal=money(100, 0, "kzt")),
            SimpleNamespace(uid="cny-uid", ticker="CNYRUB_TOM", iso_currency_name="cny", nominal=money(1, 0, "cny")),
        ]

    def get_last_price(self, uid):
        self.price_calls += 1
        return SimpleNamespace(
            price=SimpleNamespace(units=11, nano=561500000),
            time=datetime(2026, 7, 22, tzinfo=UTC),
        )


def test_get_fx_rate_resolves_and_caches():
    adapter = FxAdapter()
    rate = fx.get_fx_rate(adapter, "cny")
    assert rate is not None
    assert rate.rate == Decimal("11.5615")
    assert rate.source_ticker == "CNYRUB_TOM"
    # Second lookup is served from cache — no second catalogue sweep.
    assert fx.get_fx_rate(adapter, "cny").rate == Decimal("11.5615")
    assert adapter.currency_calls == 1


def test_get_fx_rate_base_currency_is_free():
    adapter = FxAdapter()
    rate = fx.get_fx_rate(adapter, "rub")
    assert rate is not None and rate.rate == Decimal("1")
    assert adapter.currency_calls == 0


def test_get_fx_rate_unknown_currency_is_none_and_not_retried():
    adapter = FxAdapter()
    assert fx.get_fx_rate(adapter, "zwl") is None
    assert fx.get_fx_rate(adapter, "zwl") is None
    assert adapter.currency_calls == 1  # negative result cached too


def test_to_rub_requires_a_matching_rate():
    rate = fx.FxRate(currency="cny", rate=Decimal("11.5615"))
    assert fx.to_rub(Decimal("1000"), "cny", rate) == Decimal("11561.50")
    assert fx.to_rub(Decimal("1000"), "rub", None) == Decimal("1000")
    # A missing rate must never silently mean 1:1.
    assert fx.to_rub(Decimal("1000"), "cny", None) is None
    assert fx.to_rub(Decimal("1000"), "cny", fx.FxRate(currency="usd", rate=Decimal("80"))) is None


def test_fx_breakeven_annual_pct():
    # 8.7% in CNY only matches 15.7% in RUB if the yuan gains ~6.44%/yr.
    breakeven = fx.fx_breakeven_annual_pct(Decimal("8.7"), Decimal("15.7"))
    assert breakeven == pytest.approx(Decimal("6.44"), abs=Decimal("0.01"))
    # Equal yields need no currency move.
    assert fx.fx_breakeven_annual_pct(Decimal("12"), Decimal("12")) == Decimal("0")
    assert fx.fx_breakeven_annual_pct(None, Decimal("15")) is None


def test_fx_note_mentions_the_currency_and_the_rate():
    note = fx.fx_note("cny", fx.FxRate(currency="cny", rate=Decimal("11.5615")))
    assert "CNY" in note and "11.5615" in note
    assert fx.fx_note("rub", None) is None
    assert "unavailable" in fx.fx_note("cny", None)
