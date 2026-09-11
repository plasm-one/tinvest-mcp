from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from tinvest_mcp.config.env import DEFAULT_ALLOWED_INSTRUMENT_TYPES, Settings
from tinvest_mcp.risk_engine import (
    OrderContext,
    PlanState,
    PlanStep,
    daily_turnover,
    evaluate,
    evaluate_plan,
)
from tinvest_mcp.schemas import (
    InvestmentInstrument,
    InvestmentProfile,
    MarketSnapshot,
    PortfolioSummary,
    TargetAllocation,
)
from tinvest_mcp.session_calendar import MSK, moex_session_state


def make_settings(**overrides) -> Settings:
    base = {
        "mode": "sandbox",
        "sandbox_token": "t",
        "readonly_token": None,
        "fullaccess_token": None,
        "account_id": "acc",
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


def make_instrument(**overrides) -> InvestmentInstrument:
    base = {
        "uid": "uid-1",
        "figi": "FIGI",
        "ticker": "SBER",
        "name": "Sber",
        "instrument_type": "share",
        "currency": "rub",
        "lot": 10,
        "min_price_increment": Decimal("0.01"),
        "price_quote_unit": "currency",
        "api_trade_available": True,
        "buy_available": True,
        "sell_available": True,
        "short_enabled": False,
        "qualified_investor_only": False,
        "trading_status": "SECURITY_TRADING_STATUS_NORMAL_TRADING",
    }
    base.update(overrides)
    return InvestmentInstrument(**base)


def make_snapshot(**overrides) -> MarketSnapshot:
    base = {
        "instrument_uid": "uid-1",
        "last_price": Decimal("100"),
        "trading_status": "SECURITY_TRADING_STATUS_NORMAL_TRADING",
        "api_trade_available": True,
        "age_seconds": 2.0,
        "is_fresh": True,
    }
    base.update(overrides)
    return MarketSnapshot(**base)


def make_portfolio(total="10000", cash="10000") -> PortfolioSummary:
    return PortfolioSummary(
        mode="sandbox",
        total_value=Decimal(total),
        cash=Decimal(cash),
        asset_allocation={},
        positions=[],
        concentration={},
    )


def make_ctx(**overrides) -> OrderContext:
    base = {
        "direction": "BUY",
        "order_type": "LIMIT",
        "quantity_lots": 1,
        "limit_price": Decimal("100"),
        "order_total": Decimal("1000"),
        "available_cash": Decimal("10000"),
        "portfolio_value_before": Decimal("10000"),
        "position_value_before": Decimal("0"),
        "max_lots": 10,
    }
    base.update(overrides)
    return OrderContext(**base)


def codes_failed(checks):
    return {c.code for c in checks if not c.passed}


def setup_function(_):
    # Reset the in-process turnover accumulator between tests.
    daily_turnover._total = Decimal("0")
    daily_turnover._day = None


def test_happy_path_all_pass():
    checks = evaluate(make_ctx(), make_instrument(), make_snapshot(), make_portfolio(), make_settings())
    assert codes_failed(checks) == set()


def test_market_session_blocks_during_clearing_pause():
    pause = moex_session_state(datetime(2026, 7, 21, 18, 45, tzinfo=MSK))  # evening clearing
    checks = evaluate(
        make_ctx(), make_instrument(), make_snapshot(), make_portfolio(), make_settings(), session_state=pause
    )
    assert "MARKET_SESSION" in codes_failed(checks)
    sess = next(c for c in checks if c.code == "MARKET_SESSION")
    assert sess.severity == "error"


def test_market_session_passes_during_main_session():
    live = moex_session_state(datetime(2026, 7, 21, 12, 0, tzinfo=MSK))
    checks = evaluate(
        make_ctx(), make_instrument(), make_snapshot(), make_portfolio(), make_settings(), session_state=live
    )
    assert "MARKET_SESSION" not in codes_failed(checks)


def test_market_session_warns_near_boundary_without_blocking():
    soon = moex_session_state(datetime(2026, 7, 21, 18, 39, tzinfo=MSK), buffer_seconds=120)
    checks = evaluate(
        make_ctx(), make_instrument(), make_snapshot(), make_portfolio(), make_settings(), session_state=soon
    )
    sess = next(c for c in checks if c.code == "MARKET_SESSION")
    assert sess.passed is True
    assert sess.severity == "warning"


def test_no_market_session_check_when_state_absent():
    checks = evaluate(make_ctx(), make_instrument(), make_snapshot(), make_portfolio(), make_settings())
    assert not any(c.code == "MARKET_SESSION" for c in checks)


def test_max_order_value_exceeded():
    checks = evaluate(
        make_ctx(order_total=Decimal("2000")), make_instrument(), make_snapshot(), make_portfolio(), make_settings()
    )
    assert "MAX_ORDER_VALUE" in codes_failed(checks)


def test_daily_turnover_exceeded():
    daily_turnover.add(Decimal("2500"))
    checks = evaluate(
        make_ctx(order_total=Decimal("1000")), make_instrument(), make_snapshot(), make_portfolio(), make_settings()
    )
    assert "DAILY_TURNOVER" in codes_failed(checks)


def test_position_weight_exceeded():
    # buying 1000 into a 1000 portfolio with no other assets -> weight ~1.0 > 0.40
    checks = evaluate(
        make_ctx(order_total=Decimal("1000"), portfolio_value_before=Decimal("1000")),
        make_instrument(),
        make_snapshot(),
        make_portfolio(total="1000"),
        make_settings(),
    )
    assert "POSITION_WEIGHT" in codes_failed(checks)


def test_insufficient_cash():
    checks = evaluate(
        make_ctx(order_total=Decimal("1000"), available_cash=Decimal("500")),
        make_instrument(),
        make_snapshot(),
        make_portfolio(cash="500"),
        make_settings(),
    )
    assert "SUFFICIENT_CASH" in codes_failed(checks)


def test_forbidden_instrument_type():
    checks = evaluate(
        make_ctx(), make_instrument(instrument_type="futures"), make_snapshot(), make_portfolio(), make_settings()
    )
    assert "INSTRUMENT_TYPE_ALLOWED" in codes_failed(checks)


def test_qualified_only_rejected():
    checks = evaluate(
        make_ctx(), make_instrument(qualified_investor_only=True), make_snapshot(), make_portfolio(), make_settings()
    )
    assert "NOT_QUALIFIED_ONLY" in codes_failed(checks)


def test_market_order_rejected():
    checks = evaluate(
        make_ctx(order_type="MARKET"), make_instrument(), make_snapshot(), make_portfolio(), make_settings()
    )
    assert "NO_MARKET_ORDER" in codes_failed(checks)


def test_unknown_direction_rejected():
    checks = evaluate(
        make_ctx(direction="SHORT"), make_instrument(), make_snapshot(), make_portfolio(), make_settings()
    )
    assert "DIRECTION_ALLOWED" in codes_failed(checks)


# --- SELL checks (stage 7) ---------------------------------------------------


def make_sell_ctx(**overrides) -> OrderContext:
    base = {"direction": "SELL", "position_available_lots": 5}
    base.update(overrides)
    return make_ctx(**base)


def by_code(checks, code):
    return [c for c in checks if c.code == code]


def test_sell_with_position_passes():
    checks = evaluate(
        make_sell_ctx(quantity_lots=3), make_instrument(), make_snapshot(), make_portfolio(), make_settings()
    )
    assert codes_failed(checks) == set()


def test_sell_without_position_rejected():
    checks = evaluate(
        make_sell_ctx(position_available_lots=None),
        make_instrument(),
        make_snapshot(),
        make_portfolio(),
        make_settings(),
    )
    assert "POSITION_EXISTS" in codes_failed(checks)


def test_sell_more_than_held_rejected():
    checks = evaluate(
        make_sell_ctx(quantity_lots=10, position_available_lots=5),
        make_instrument(),
        make_snapshot(),
        make_portfolio(),
        make_settings(),
    )
    assert "POSITION_EXISTS" in codes_failed(checks)


def test_sell_skips_cash_and_weight_checks():
    # A sell frees cash and reduces the weight — neither BUY check applies.
    checks = evaluate(
        make_sell_ctx(order_total=Decimal("1000"), available_cash=Decimal("0")),
        make_instrument(),
        make_snapshot(),
        make_portfolio(cash="0"),
        make_settings(),
    )
    codes = {c.code for c in checks}
    assert "SUFFICIENT_CASH" not in codes
    assert "POSITION_WEIGHT" not in codes


def test_sell_uses_sell_available_flag():
    checks = evaluate(
        make_sell_ctx(), make_instrument(sell_available=False), make_snapshot(), make_portfolio(), make_settings()
    )
    assert "API_TRADE_AVAILABLE" in codes_failed(checks)


def test_sell_tax_impact_is_informational():
    checks = evaluate(
        make_sell_ctx(estimated_gain=Decimal("200"), estimated_tax=Decimal("26")),
        make_instrument(),
        make_snapshot(),
        make_portfolio(),
        make_settings(),
    )
    (tax,) = by_code(checks, "TAX_IMPACT")
    assert tax.passed and tax.severity == "info"
    assert "26" in tax.message


def test_ldv_warning_inside_window():
    eligible = date.today() + timedelta(days=60)  # ~2 months < default 6
    checks = evaluate(
        make_sell_ctx(ldv_eligible_on=eligible), make_instrument(), make_snapshot(), make_portfolio(), make_settings()
    )
    (ldv,) = by_code(checks, "LDV_WARNING")
    assert ldv.passed and ldv.severity == "warning"


def test_ldv_info_outside_window_and_when_eligible():
    far = date.today() + timedelta(days=365)
    checks = evaluate(
        make_sell_ctx(ldv_eligible_on=far), make_instrument(), make_snapshot(), make_portfolio(), make_settings()
    )
    assert by_code(checks, "LDV_WARNING")[0].severity == "info"

    past = date.today() - timedelta(days=10)
    checks = evaluate(
        make_sell_ctx(ldv_eligible_on=past), make_instrument(), make_snapshot(), make_portfolio(), make_settings()
    )
    assert by_code(checks, "LDV_WARNING")[0].severity == "info"


def test_corporate_action_soon_warning():
    soon = date.today() + timedelta(days=10)
    checks = evaluate(
        make_sell_ctx(corporate_action_kind="call/offer", corporate_action_date=soon),
        make_instrument(),
        make_snapshot(),
        make_portfolio(),
        make_settings(),
    )
    (ca,) = by_code(checks, "CORPORATE_ACTION_SOON")
    assert ca.passed and ca.severity == "warning"


def test_corporate_action_far_not_emitted():
    far = date.today() + timedelta(days=200)
    checks = evaluate(
        make_sell_ctx(corporate_action_kind="maturity", corporate_action_date=far),
        make_instrument(),
        make_snapshot(),
        make_portfolio(),
        make_settings(),
    )
    assert not by_code(checks, "CORPORATE_ACTION_SOON")


def test_buy_has_no_sell_checks():
    checks = evaluate(make_ctx(), make_instrument(), make_snapshot(), make_portfolio(), make_settings())
    codes = {c.code for c in checks}
    assert not codes & {"POSITION_EXISTS", "TAX_IMPACT", "LDV_WARNING", "CORPORATE_ACTION_SOON"}


def test_price_deviation_too_large():
    checks = evaluate(
        make_ctx(limit_price=Decimal("110")),
        make_instrument(),
        make_snapshot(last_price=Decimal("100")),
        make_portfolio(),
        make_settings(),
    )
    assert "PRICE_DEVIATION" in codes_failed(checks)


def test_price_increment_misaligned():
    checks = evaluate(
        make_ctx(limit_price=Decimal("100.117")),
        make_instrument(min_price_increment=Decimal("0.01")),
        make_snapshot(),
        make_portfolio(),
        make_settings(),
    )
    assert "PRICE_INCREMENT" in codes_failed(checks)


def test_allowlist_enforced():
    s = make_settings(instrument_allowlist=("OTHER",))
    checks = evaluate(make_ctx(), make_instrument(uid="uid-1", ticker="SBER"), make_snapshot(), make_portfolio(), s)
    assert "INSTRUMENT_ALLOWLIST" in codes_failed(checks)


def test_max_lots_exceeded():
    checks = evaluate(
        make_ctx(quantity_lots=20, max_lots=5), make_instrument(), make_snapshot(), make_portfolio(), make_settings()
    )
    assert "MAX_LOTS" in codes_failed(checks)


# --- plan-level checks (stage 7) ----------------------------------------------


def make_profile(allocation=None, **overrides) -> InvestmentProfile:
    allocation = allocation or {"bonds": 55, "equity": 35, "cash": 10}
    base = {
        "risk_profile": "moderate",
        "horizon": "medium",
        "target_allocation": TargetAllocation(
            risk_profile="moderate",
            horizon="medium",
            horizon_description="1-3 years",
            allocation=allocation,
            asset_class_roles={"bonds": "anchor", "equity": "growth", "cash": "buffer"},
            source="rule_table",
            rationale="test",
        ),
        "saved_at": datetime.now(UTC),
    }
    base.update(overrides)
    return InvestmentProfile(**base)


def make_state(**overrides) -> PlanState:
    # 100k portfolio: 55k bonds + 35k equity + 10k cash, exactly on target.
    base = {
        "cash": Decimal("10000"),
        "position_units": {"bond-1": Decimal("55"), "share-1": Decimal("100")},
        "class_values": {"bonds": Decimal("55000"), "equity": Decimal("35000")},
        "sector_values": {"government": Decimal("55000"), "financial": Decimal("35000")},
        "issuer_values": {"OFZ": Decimal("55000"), "Sber": Decimal("35000")},
    }
    base.update(overrides)
    return PlanState(**base)


def make_plan_step(**overrides) -> PlanStep:
    base = {
        "instrument_uid": "share-1",
        "ticker": "SBER",
        "direction": "BUY",
        "quantity_lots": 1,
        "quantity_units": Decimal("10"),
        "estimated_value": Decimal("3000"),
        "asset_class": "equity",
        "sector": "financial",
        "issuer": "Sber",
    }
    base.update(overrides)
    return PlanStep(**base)


def run_plan(steps, profile=None, state=None, **limits):
    base = {
        "rebalance_threshold_pct": Decimal("5"),
        "min_cash_pct": Decimal("5"),
        "max_issuer_weight": Decimal("0.60"),
        "max_sector_weight": Decimal("0.60"),
    }
    base.update(limits)
    return evaluate_plan(profile or make_profile(), state or make_state(), steps, **base)


def test_plan_happy_path():
    # Sell 3k equity, buy 3k bonds: stays on target, cash untouched at the end.
    steps = [
        make_plan_step(direction="SELL", estimated_value=Decimal("3000"), quantity_units=Decimal("10")),
        make_plan_step(
            instrument_uid="bond-1",
            ticker="OFZ",
            direction="BUY",
            estimated_value=Decimal("3000"),
            quantity_units=Decimal("3"),
            asset_class="bonds",
            sector="government",
            issuer="OFZ",
        ),
    ]
    result = run_plan(steps)
    assert {c.code for c in result.checks if not c.passed} == set()
    assert result.cash_after == Decimal("10000")
    assert result.cash_after_steps == [Decimal("13000"), Decimal("10000")]


def test_plan_step_cash_insufficient_mid_sequence():
    # Buy before the sell that would have funded it → step 1 fails on cash.
    steps = [
        make_plan_step(direction="BUY", estimated_value=Decimal("12000")),
        make_plan_step(direction="SELL", estimated_value=Decimal("12000"), quantity_units=Decimal("40")),
    ]
    result = run_plan(steps)
    failed = [c for c in result.checks if not c.passed]
    assert [c.code for c in failed] == ["PLAN_STEP_CASH"]
    assert "Step 1" in failed[0].message


def test_plan_step_sell_more_than_held():
    steps = [make_plan_step(direction="SELL", quantity_units=Decimal("500"), estimated_value=Decimal("5000"))]
    result = run_plan(steps)
    assert "PLAN_STEP_POSITION" in {c.code for c in result.checks if not c.passed}


def test_plan_allocation_mandate_breach():
    # Dump 20k of cash+bond sales into equity → equity way above target band.
    steps = [
        make_plan_step(
            instrument_uid="bond-1",
            ticker="OFZ",
            direction="SELL",
            estimated_value=Decimal("15000"),
            quantity_units=Decimal("15"),
            asset_class="bonds",
            sector="government",
            issuer="OFZ",
        ),
        make_plan_step(direction="BUY", estimated_value=Decimal("20000"), quantity_units=Decimal("60")),
    ]
    result = run_plan(steps)
    failed = {c.code for c in result.checks if not c.passed}
    assert "PLAN_ALLOCATION_MANDATE" in failed
    assert result.allocation_after_pct["equity"] == Decimal("55.00")


def _currency_check(result):
    return next(c for c in result.checks if c.code == "PLAN_CURRENCY_EXPOSURE")


def test_plan_flags_new_currency_exposure_without_an_opt_in():
    # Buying a yuan bond is a currency bet; an unopted mandate must say so.
    steps = [
        make_plan_step(
            instrument_uid="cny-1",
            ticker="GAZP-CNY",
            direction="BUY",
            estimated_value=Decimal("9000"),
            quantity_units=Decimal("1"),
            asset_class="bonds",
            sector="energy",
            issuer="Gazprom",
            denomination_currency="cny",
        )
    ]
    check = _currency_check(run_plan(steps))
    assert not check.passed and check.severity == "warning"
    assert "CNY" in check.message and "allow_fx_linked" in check.message


def test_plan_currency_exposure_silent_for_ruble_only_plans():
    check = _currency_check(run_plan([make_plan_step()]))
    assert check.passed and check.severity == "info"


def test_plan_currency_exposure_within_cap_once_opted_in():
    profile = make_profile(allow_fx_linked=True)
    steps = [
        make_plan_step(
            instrument_uid="cny-1",
            direction="BUY",
            estimated_value=Decimal("9000"),
            quantity_units=Decimal("1"),
            asset_class="bonds",
            sector="energy",
            issuer="Gazprom",
            denomination_currency="cny",
        )
    ]
    # 9k of a ~100k portfolio is under the 20% cap.
    assert _currency_check(run_plan(steps, profile=profile)).passed


def test_plan_currency_exposure_blocks_deepening_a_breach_when_opted_in():
    profile = make_profile(allow_fx_linked=True, max_fx_exposure_pct=Decimal("10"))
    # Already 30k in yuan bonds — well past the 10% cap — and the plan adds more.
    state = make_state(currency_values={"rub": Decimal("60000"), "cny": Decimal("30000")})
    steps = [
        make_plan_step(
            instrument_uid="cny-1",
            direction="BUY",
            estimated_value=Decimal("9000"),
            quantity_units=Decimal("1"),
            asset_class="bonds",
            sector="energy",
            issuer="Gazprom",
            denomination_currency="cny",
        )
    ]
    check = _currency_check(run_plan(steps, profile=profile, state=state))
    assert not check.passed and check.severity == "error"


def test_plan_min_cash_floor():
    steps = [make_plan_step(direction="BUY", estimated_value=Decimal("8000"), quantity_units=Decimal("25"))]
    result = run_plan(steps, min_cash_pct=Decimal("5"))
    failed = {c.code for c in result.checks if not c.passed}
    assert "PLAN_MIN_CASH" in failed  # 2k of 100k = 2% < 5%


def test_plan_issuer_and_sector_limits():
    steps = [make_plan_step(direction="BUY", estimated_value=Decimal("9000"), quantity_units=Decimal("30"))]
    result = run_plan(steps, max_issuer_weight=Decimal("0.40"), max_sector_weight=Decimal("0.40"))
    failed = {c.code for c in result.checks if not c.passed}
    assert {"PLAN_ISSUER_LIMIT", "PLAN_SECTOR_LIMIT"} <= failed
    issuer_check = next(c for c in result.checks if c.code == "PLAN_ISSUER_LIMIT")
    assert "Sber" in issuer_check.message


def test_plan_unknown_sector_skipped_in_limits():
    state = make_state(sector_values={"unknown": Decimal("90000")})
    steps = [
        make_plan_step(direction="BUY", estimated_value=Decimal("1000"), quantity_units=Decimal("3"), sector="unknown")
    ]
    result = run_plan(steps, state=state, max_sector_weight=Decimal("0.10"))
    sector_check = next(c for c in result.checks if c.code == "PLAN_SECTOR_LIMIT")
    assert sector_check.passed


# --- ratchet (feature A) + stepped severity (feature D) -----------------------


def _empty_state(**overrides) -> PlanState:
    """An all-cash portfolio (nothing deployed yet) — the small-portfolio case."""
    base = {
        "cash": Decimal("100000"),
        "position_units": {},
        "class_values": {},
        "sector_values": {},
        "issuer_values": {},
    }
    base.update(overrides)
    return PlanState(**base)


def test_deploying_all_cash_to_target_is_accepted_not_rejected():
    # The core bug: an all-cash book buying exactly to the target must not be blocked
    # just because every position is a brand-new "breach" against a zero baseline.
    profile = make_profile({"bonds": 55, "equity": 35, "cash": 10})
    steps = [
        make_plan_step(
            instrument_uid="bond-1",
            ticker="OFZ",
            direction="BUY",
            estimated_value=Decimal("55000"),
            quantity_units=Decimal("55"),
            asset_class="bonds",
            sector="government",
            issuer="OFZ",
        ),
        make_plan_step(direction="BUY", estimated_value=Decimal("35000"), quantity_units=Decimal("100")),  # Sber equity
    ]
    result = run_plan(
        steps,
        profile=profile,
        state=_empty_state(),
        max_issuer_weight=Decimal("0.60"),
        max_sector_weight=Decimal("0.60"),
    )
    alloc = next(c for c in result.checks if c.code == "PLAN_ALLOCATION_MANDATE")
    assert alloc.passed  # lands exactly on target
    assert not any((not c.passed) and c.severity == "error" for c in result.checks)


def test_new_concentration_from_idle_cash_is_warning_not_error():
    # Deploying idle cash into a fresh over-cap name → warning (acknowledgeable), not a block.
    steps = [
        make_plan_step(
            instrument_uid="y1",
            ticker="YDEX",
            issuer="Yandex",
            sector="tech",
            estimated_value=Decimal("9000"),
            quantity_units=Decimal("3"),
        )
    ]
    result = run_plan(steps, max_issuer_weight=Decimal("0.05"), max_sector_weight=Decimal("0.60"))
    issuer = next(c for c in result.checks if c.code == "PLAN_ISSUER_LIMIT")
    assert not issuer.passed and issuer.severity == "warning"
    assert "Yandex" in issuer.message


def test_deepening_an_existing_breach_is_a_blocking_error():
    # Sber already 35% > 30% cap; buying more makes it worse → hard error.
    steps = [
        make_plan_step(direction="BUY", estimated_value=Decimal("9000"), quantity_units=Decimal("30"))
    ]  # more Sber
    result = run_plan(steps, max_issuer_weight=Decimal("0.30"), max_sector_weight=Decimal("0.60"))
    issuer = next(c for c in result.checks if c.code == "PLAN_ISSUER_LIMIT")
    assert not issuer.passed and issuer.severity == "error"


def test_shrinking_an_existing_breach_toward_the_band_passes():
    # A single-issuer book at 65% > 40% cap; selling part moves toward the band → pass.
    state = _empty_state(
        cash=Decimal("35000"),
        position_units={"share-1": Decimal("100")},
        class_values={"equity": Decimal("65000")},
        sector_values={"financial": Decimal("65000")},
        issuer_values={"Sber": Decimal("65000")},
    )
    steps = [
        make_plan_step(direction="SELL", estimated_value=Decimal("20000"), quantity_units=Decimal("66"))
    ]  # sell Sber
    result = run_plan(steps, state=state, max_issuer_weight=Decimal("0.40"), max_sector_weight=Decimal("0.90"))
    issuer = next(c for c in result.checks if c.code == "PLAN_ISSUER_LIMIT")
    assert issuer.passed  # 65% → 45%: still above cap, but improving → not flagged


def test_funds_are_exempt_from_the_issuer_cap():
    # A brand-new fund position above the single-name cap is exempt (diversified underlying).
    steps = [
        make_plan_step(
            instrument_uid="etf-1",
            ticker="ETF",
            issuer="BroadETF",
            sector="diversified",
            estimated_value=Decimal("40000"),
            quantity_units=Decimal("3"),
        )
    ]
    result = run_plan(
        steps,
        state=_empty_state(),
        max_issuer_weight=Decimal("0.05"),
        max_sector_weight=Decimal("0.90"),
        issuer_caps={"BroadETF": None},
    )
    issuer = next(c for c in result.checks if c.code == "PLAN_ISSUER_LIMIT")
    assert issuer.passed


def test_reported_small_all_cash_deploy_warns_but_is_not_rejected():
    # The originally reported case: a ~51k all-cash book deployed into three names.
    # Before the fix this was RISK_REJECTED; now the allocation lands in-band (ratchet),
    # only the 43% single name trips the (size-scaled 30%) issuer cap → one warning.
    profile = make_profile({"bonds": 45, "equity": 50, "cash": 5})
    state = _empty_state(cash=Decimal("50984.54"))
    steps = [
        make_plan_step(
            instrument_uid="kz",
            ticker="RU000A101RZ3",
            issuer="Республика Казахстан 11",
            direction="BUY",
            asset_class="bonds",
            sector="unknown",
            estimated_value=Decimal("22089"),
            quantity_units=Decimal("30"),
        ),
        make_plan_step(
            instrument_uid="sber",
            ticker="SBER",
            issuer="Сбер Банк",
            direction="BUY",
            asset_class="equity",
            sector="financial",
            estimated_value=Decimal("12663.30"),
            quantity_units=Decimal("51"),
        ),
        make_plan_step(
            instrument_uid="ydex",
            ticker="YDEX",
            issuer="Яндекс",
            direction="BUY",
            asset_class="equity",
            sector="technology",
            estimated_value=Decimal("9879"),
            quantity_units=Decimal("3"),
        ),
    ]
    result = run_plan(
        steps,
        profile=profile,
        state=state,
        min_cash_pct=Decimal("0"),
        max_issuer_weight=Decimal("0.30"),
        max_sector_weight=Decimal("0.30"),
    )
    checks = {c.code: c for c in result.checks}
    assert checks["PLAN_ALLOCATION_MANDATE"].passed  # equity 0→44% toward the 50% target
    assert checks["PLAN_SECTOR_LIMIT"].passed  # each sector under 30%
    issuer = checks["PLAN_ISSUER_LIMIT"]
    assert not issuer.passed and issuer.severity == "warning"
    assert "Казахстан" in issuer.message
    assert "Сбер" not in issuer.message and "Яндекс" not in issuer.message
    # Nothing hard-blocks: the plan is confirmable with acknowledgement, not rejected.
    assert not any((not c.passed) and c.severity == "error" for c in result.checks)


def test_sovereign_cap_overrides_the_single_name_cap():
    # 30% of the book in one sovereign bond: over the 0.20 single cap, under the 0.35 sovereign cap.
    steps = [
        make_plan_step(
            instrument_uid="bond-1",
            ticker="OFZ",
            issuer="OFZ",
            sector="government",
            asset_class="bonds",
            estimated_value=Decimal("30000"),
            quantity_units=Decimal("30"),
        )
    ]
    result = run_plan(
        steps,
        state=_empty_state(),
        max_issuer_weight=Decimal("0.20"),
        max_sector_weight=Decimal("0.90"),
        issuer_caps={"OFZ": Decimal("0.35")},
    )
    issuer = next(c for c in result.checks if c.code == "PLAN_ISSUER_LIMIT")
    assert issuer.passed
