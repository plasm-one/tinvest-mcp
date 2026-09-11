"""Unit tests for algorithmic BUY price hints."""

from datetime import UTC, datetime
from decimal import Decimal

from tinvest_mcp.config.env import DEFAULT_ALLOWED_INSTRUMENT_TYPES, Settings
from tinvest_mcp.pricing_hints import (
    build_price_vs_hints,
    compute_buy_price_hints,
    resolve_buy_limit_price,
)
from tinvest_mcp.schemas import InvestmentInstrument, MarketSnapshot
from tinvest_mcp.session_calendar import MSK, moex_session_state


def make_settings(**overrides) -> Settings:
    base = {
        "mode": "sandbox",
        "sandbox_token": "t",
        "readonly_token": None,
        "fullaccess_token": None,
        "account_id": "acc-1",
        "max_order_rub": Decimal("1500"),
        "max_daily_turnover_rub": Decimal("3000"),
        "max_position_weight": Decimal("0.40"),
        "confirmation_ttl_seconds": 60,
        "max_price_deviation_pct": Decimal("1.0"),
        "market_data_max_age_seconds": 10,
        "allow_market_orders": False,
        "allow_margin": False,
        "allow_shorts": False,
        "require_single_account_token": True,
        "enable_real_trading": False,
        "allowed_instrument_types": DEFAULT_ALLOWED_INSTRUMENT_TYPES,
        "instrument_allowlist": (),
    }
    base.update(overrides)
    return Settings(**base)


def bond_snapshot(**overrides) -> MarketSnapshot:
    data = {
        "instrument_uid": "bond-1",
        "instrument_type": "bond",
        "price_quote_unit": "pct_of_nominal",
        "last_price": Decimal("99.09"),
        "last_price_time": datetime.now(UTC),
        "best_bid": Decimal("99.03"),
        "best_ask": Decimal("99.18"),
        "limit_up": Decimal("102.07"),
        "limit_down": Decimal("96.13"),
        "trading_status": "SECURITY_TRADING_STATUS_NORMAL_TRADING",
        "api_trade_available": True,
        "age_seconds": 1.0,
        "is_fresh": True,
    }
    data.update(overrides)
    return MarketSnapshot(**data)


def bond_instrument(**overrides) -> InvestmentInstrument:
    data = {
        "uid": "bond-1",
        "figi": "F",
        "ticker": "BOND",
        "name": "Test bond",
        "instrument_type": "bond",
        "currency": "rub",
        "lot": 1,
        "min_price_increment": Decimal("0.01"),
        "price_quote_unit": "pct_of_nominal",
        "api_trade_available": True,
        "buy_available": True,
        "sell_available": True,
    }
    data.update(overrides)
    return InvestmentInstrument(**data)


def test_compute_hints_bond_tiers():
    snap = bond_snapshot()
    inst = bond_instrument()
    hints = compute_buy_price_hints(snap, inst, make_settings())

    assert hints.hints["patient"].limit_price == Decimal("99.03")
    assert hints.hints["balanced"].limit_price == Decimal("99.10")  # mid spread, rounded down
    assert hints.hints["fast"].limit_price == Decimal("99.18")
    assert hints.spread.width_pct == Decimal("0.15")
    assert hints.hints["fast"].fill_expectation == "immediate_when_session_open"


def test_resolve_urgency_fast():
    snap = bond_snapshot()
    hints = compute_buy_price_hints(snap, bond_instrument(), make_settings())
    price, tier = resolve_buy_limit_price(hints, urgency="fast")
    assert tier == "fast"
    assert price == Decimal("99.18")


def test_resolve_default_balanced():
    snap = bond_snapshot()
    hints = compute_buy_price_hints(snap, bond_instrument(), make_settings())
    price, tier = resolve_buy_limit_price(hints)
    assert tier == "balanced"
    assert price == Decimal("99.10")


def test_price_vs_hints_warns_below_ask():
    snap = bond_snapshot()
    hints = compute_buy_price_hints(snap, bond_instrument(), make_settings())
    vs = build_price_vs_hints(
        Decimal("99.04"),
        hints,
        urgency_used="balanced",
        user_supplied_price=True,
    )
    assert vs.crosses_spread is False
    assert vs.warning is not None
    assert "below best_ask" in vs.warning


def test_fast_clamped_when_deviation_tight():
    snap = bond_snapshot()
    settings = make_settings(max_price_deviation_pct=Decimal("0.05"))
    hints = compute_buy_price_hints(snap, bond_instrument(), settings)
    # max buy ≈ 99.14 — below ask 99.18
    assert hints.hints["fast"].limit_price < snap.best_ask
    assert hints.hints["fast"].fill_expectation == "may_not_cross_spread"


def test_session_info_populated_during_clearing_pause():
    # trading_status is still NORMAL_TRADING, but the clock says we are in the
    # 18:40–19:05 evening clearing pause — the calendar must surface it.
    snap = bond_snapshot()
    pause = moex_session_state(datetime(2026, 7, 21, 18, 45, tzinfo=MSK))
    hints = compute_buy_price_hints(snap, bond_instrument(), make_settings(), session_state=pause)
    assert hints.session.tradeable_now is False
    assert hints.session.session_phase == "PAUSE"
    assert hints.session.resumes_at is not None
    assert any("clearing pause" in w for w in hints.session.warnings)


def test_session_info_none_without_state():
    hints = compute_buy_price_hints(bond_snapshot(), bond_instrument(), make_settings())
    assert hints.session.tradeable_now is None
    assert hints.session.session_phase is None
    assert hints.session.warnings == []
