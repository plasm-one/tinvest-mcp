"""Stage-6 trade plan: FIFO/ЛДВ tax lots, lot sizing, cost-benefit verdict and
the create_trade_plan orchestration (fake adapter, no SDK)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

import tinvest_mcp.proposals as proposals_mod
import tinvest_mcp.trade_plan as trade_plan_mod
from tinvest_mcp import services
from tinvest_mcp.allocation import propose_allocation
from tinvest_mcp.config.env import DEFAULT_ALLOWED_INSTRUMENT_TYPES, Settings
from tinvest_mcp.profile_store import save_profile
from tinvest_mcp.risk_engine import daily_turnover
from tinvest_mcp.schemas import BrokerOperation, InvestmentProfile, TargetAllocation
from tinvest_mcp.trade_plan import (
    TaxLot,
    bond_unit_money,
    build_cost_benefit,
    build_tax_lots,
    estimate_sale_tax,
    lots_from_amount,
)


def setup_function(_):
    daily_turnover._total = Decimal("0")
    daily_turnover._day = None
    trade_plan_mod._store = None
    proposals_mod._store = None


# --- pure helpers -------------------------------------------------------------


def test_bond_unit_money_and_missing_nominal():
    assert bond_unit_money(Decimal("100.1"), Decimal("1000")) == Decimal("1001.00")
    assert bond_unit_money(Decimal("99"), None) is None


def test_lots_from_amount_floors_and_rejects_small():
    assert lots_from_amount(Decimal("30000"), Decimal("1011")) == 29
    assert lots_from_amount(Decimal("1000"), Decimal("1011")) == 0
    assert lots_from_amount(Decimal("1000"), Decimal("0")) == 0


# --- FIFO tax lots ------------------------------------------------------------


def _op(
    op_type: str, days_ago: int, *, qty: str, payment: str = None, aci: str = None, price: str = None, op_id: str = "op"
):
    return BrokerOperation(
        id=op_id,
        date=datetime.now(UTC) - timedelta(days=days_ago),
        type=op_type,
        type_raw=f"OPERATION_TYPE_{op_type.upper()}",
        state="OPERATION_STATE_EXECUTED",
        payment=Decimal(payment) if payment is not None else None,
        price=Decimal(price) if price is not None else None,
        quantity=Decimal(qty),
        accrued_int=Decimal(aci) if aci is not None else None,
    )


def test_build_tax_lots_clean_basis_and_fifo_consumption():
    # Buy 10 @ clean 100 (payment includes 50 of НКД), buy 10 @ 120, sell 15.
    ops = [
        _op("buy", 400, qty="10", payment="-1050", aci="50", op_id="b1"),
        _op("buy", 200, qty="10", payment="-1200", op_id="b2"),
        _op("sell", 100, qty="15", payment="1800", op_id="s1"),
    ]
    lots, notes = build_tax_lots(ops)
    # First lot fully consumed, 5 units of the second remain at 120.
    assert len(lots) == 1
    assert lots[0].quantity == Decimal("5")
    assert lots[0].cost_per_unit == Decimal("120")
    assert notes == []


def test_build_tax_lots_oversell_is_noted():
    ops = [_op("sell", 10, qty="5", payment="500")]
    lots, notes = build_tax_lots(ops)
    assert lots == []
    assert any("more sold than bought" in n for n in notes)


def test_estimate_sale_tax_recent_lot_is_taxed():
    lots = [TaxLot(date.today() - timedelta(days=365), Decimal("30"), Decimal("100"))]
    est = estimate_sale_tax(
        lots,
        Decimal("30"),
        Decimal("299"),
        as_of=date.today(),
        tax_rate_pct=Decimal("13"),
        ldv_min_holding_years=3,
    )
    assert est.method == "fifo"
    assert est.taxable_gain == Decimal("5970.00")  # (299-100)*30
    assert est.exempt_gain_ldv == Decimal("0.00")
    assert est.tax == Decimal("776.10")


def test_estimate_sale_tax_ldv_exempts_old_lot():
    lots = [TaxLot(date.today() - timedelta(days=365 * 4), Decimal("10"), Decimal("100"))]
    est = estimate_sale_tax(
        lots,
        Decimal("10"),
        Decimal("150"),
        as_of=date.today(),
        tax_rate_pct=Decimal("13"),
        ldv_min_holding_years=3,
    )
    assert est.exempt_gain_ldv == Decimal("500.00")
    assert est.taxable_gain == Decimal("0.00")
    assert est.tax == Decimal("0.00")


def test_estimate_sale_tax_mixed_uses_fallback_for_uncovered_units():
    lots = [TaxLot(date.today() - timedelta(days=100), Decimal("5"), Decimal("100"))]
    est = estimate_sale_tax(
        lots,
        Decimal("10"),
        Decimal("200"),
        as_of=date.today(),
        tax_rate_pct=Decimal("13"),
        ldv_min_holding_years=3,
        fallback_cost_per_unit=Decimal("150"),
    )
    assert est.method == "mixed"
    # 5 covered: gain 500; 5 fallback: gain 250.
    assert est.taxable_gain == Decimal("750.00")
    assert any("average price" in n for n in est.notes)


def test_estimate_sale_tax_loss_is_untaxed():
    lots = [TaxLot(date.today() - timedelta(days=100), Decimal("10"), Decimal("300"))]
    est = estimate_sale_tax(
        lots,
        Decimal("10"),
        Decimal("250"),
        as_of=date.today(),
        tax_rate_pct=Decimal("13"),
        ldv_min_holding_years=3,
    )
    assert est.gross_gain == Decimal("-500.00")
    assert est.tax == Decimal("0.00")


def test_estimate_sale_tax_no_basis_at_all_overestimates():
    est = estimate_sale_tax(
        [],
        Decimal("10"),
        Decimal("100"),
        as_of=date.today(),
        tax_rate_pct=Decimal("13"),
        ldv_min_holding_years=3,
    )
    assert est.method == "none"
    assert est.cost_basis == Decimal("0.00")
    assert est.tax == Decimal("130.00")


# --- cost-benefit verdict -------------------------------------------------------


def _cb(
    before,
    after,
    *,
    total_before="100000",
    total_after="100000",
    commissions="6",
    taxes="0",
    threshold="5",
    max_ratio="0.05",
):
    target = {"bonds": 70, "equity": 20, "cash": 10}
    return build_cost_benefit(
        allocation_before_pct={k: Decimal(v) for k, v in before.items()},
        allocation_after_pct={k: Decimal(v) for k, v in after.items()},
        total_value_before=Decimal(total_before),
        total_value_after=Decimal(total_after),
        target_allocation=target,
        commissions=Decimal(commissions),
        taxes=Decimal(taxes),
        rebalance_threshold_pct=Decimal(threshold),
        max_cost_to_benefit_ratio=Decimal(max_ratio),
    )


def test_cost_benefit_worth_it_when_costs_are_small():
    cb = _cb({"bonds": "0", "equity": "30", "cash": "70"}, {"bonds": "69", "equity": "21", "cash": "10"})
    assert cb.verdict == "WORTH_IT"
    assert cb.misallocation_before == Decimal("70000.00")
    assert cb.drift_reduction_value > 0
    assert cb.cost_to_benefit_ratio < Decimal("0.05")


def test_cost_benefit_not_worth_it_below_threshold():
    cb = _cb({"bonds": "68", "equity": "21", "cash": "11"}, {"bonds": "70", "equity": "20", "cash": "10"})
    assert cb.verdict == "NOT_WORTH_IT"
    assert "below the rebalance threshold" in cb.reason


def test_cost_benefit_not_worth_it_when_costs_eat_benefit():
    # Tiny improvement (1 pp moved = 500 of misallocation) for 400 in costs.
    cb = _cb(
        {"bonds": "60", "equity": "30", "cash": "10"},
        {"bonds": "61", "equity": "29", "cash": "10"},
        commissions="100",
        taxes="300",
    )
    assert cb.verdict == "NOT_WORTH_IT"
    assert "above the" in cb.reason


def test_cost_benefit_not_worth_it_when_plan_moves_away():
    cb = _cb({"bonds": "60", "equity": "30", "cash": "10"}, {"bonds": "55", "equity": "35", "cash": "10"})
    assert cb.verdict == "NOT_WORTH_IT"
    assert cb.drift_reduction_value <= 0


def test_plan_store_roundtrip_and_ttl():
    store = trade_plan_mod.TradePlanStore(ttl_seconds=900)
    plan_id, created_at, expires_at = store.new_id_and_window()
    assert (expires_at - created_at).total_seconds() == 900


# --- create_trade_plan orchestration (fake adapter) -----------------------------


def q(units, nano=0):
    return SimpleNamespace(units=units, nano=nano)


def money(units, nano=0, currency="rub"):
    return SimpleNamespace(units=units, nano=nano, currency=currency)


def enum(name):
    return SimpleNamespace(name=name)


def make_settings(profile_path, **overrides) -> Settings:
    base = {
        "mode": "sandbox",
        "sandbox_token": "t",
        "readonly_token": None,
        "fullaccess_token": None,
        "account_id": "acc-1",
        "max_order_rub": Decimal("500000"),
        "max_daily_turnover_rub": Decimal("5000000"),
        "max_position_weight": Decimal("1.0"),
        "confirmation_ttl_seconds": 60,
        "max_price_deviation_pct": Decimal("5.0"),
        "market_data_max_age_seconds": 120,
        "allow_market_orders": False,
        "allow_margin": False,
        "allow_shorts": False,
        "require_single_account_token": True,
        "enable_real_trading": False,
        "allowed_instrument_types": DEFAULT_ALLOWED_INSTRUMENT_TYPES,
        "instrument_allowlist": (),
        "investment_profile_path": str(profile_path),
        # Neutralize concentration caps: the scenario buys one bond for most of
        # the portfolio; issuer/sector limits have their own tests.
        "mandate_max_issuer_weight": Decimal("1.0"),
        "mandate_max_sector_weight": Decimal("1.0"),
        # Off by default so wall-clock time never blocks execution in these
        # plan-lifecycle tests; the MOEX session gate is covered separately.
        "market_session_check_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


SHARE_UID = "share-1"
BOND_UID = "bond-1"


class PlanAdapter:
    """Portfolio: 70 000 cash + 100 SBER units (30 000). Bond OFZ at 100.1% ask."""

    _LAST = {SHARE_UID: 300, BOND_UID: 100}
    _BOOK = {SHARE_UID: (299, 301), BOND_UID: ("99.9", "100.1")}

    def get_accounts(self):
        return [
            SimpleNamespace(
                id="acc-1",
                type=enum("ACCOUNT_TYPE_TINKOFF"),
                status=enum("ACCOUNT_STATUS_OPEN"),
                name="pilot",
                opened_date=None,
                closed_date=None,
                access_level=enum("ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"),
            )
        ]

    def get_portfolio(self, account_id):
        return SimpleNamespace(
            total_amount_portfolio=money(100000),
            total_amount_currencies=money(70000),
            total_amount_shares=money(30000),
            total_amount_bonds=money(0),
            total_amount_etf=money(0),
            positions=[
                SimpleNamespace(
                    instrument_uid=SHARE_UID,
                    figi="F1",
                    ticker="SBER",
                    instrument_type="share",
                    quantity=q(100),
                    quantity_lots=q(10),
                    average_position_price=money(100),
                    current_price=money(300),
                    expected_yield=q(0),
                )
            ],
        )

    def get_instrument_by_uid(self, uid):
        if uid == BOND_UID:
            return SimpleNamespace(
                uid=uid,
                instrument_type="bond",
                figi="FB",
                ticker="OFZ1",
                name="OFZ bond",
                currency="rub",
                lot=1,
                min_price_increment=q(0, 10000000),
                api_trade_available_flag=True,
                buy_available_flag=True,
                sell_available_flag=True,
                short_enabled_flag=False,
                for_qual_investor_flag=False,
                exchange="MOEX",
                class_code="TQOB",
                nominal=money(1000),
                isin="RU000B",
                sector="government",
                country_of_risk="RU",
                trading_status=enum("SECURITY_TRADING_STATUS_NORMAL_TRADING"),
            )
        return SimpleNamespace(
            uid=uid,
            instrument_type="share",
            figi="F1",
            ticker="SBER",
            name="Sberbank",
            currency="rub",
            lot=10,
            min_price_increment=q(0, 10000000),
            api_trade_available_flag=True,
            buy_available_flag=True,
            sell_available_flag=True,
            short_enabled_flag=False,
            for_qual_investor_flag=False,
            exchange="MOEX",
            class_code="TQBR",
            nominal=None,
            isin="RU000A",
            sector="financial",
            country_of_risk="RU",
            trading_status=enum("SECURITY_TRADING_STATUS_NORMAL_TRADING"),
        )

    def get_bond_by_uid(self, uid):
        return SimpleNamespace(
            nominal=money(1000),
            initial_nominal=money(1000),
            aci_value=money(10),
            maturity_date=datetime.now(UTC) + timedelta(days=900),
            call_date=None,
            liquidity_flag=True,
            for_iis_flag=True,
            issue_kind="non_documentary",
            issue_size=1000000,
            risk_level=enum("RISK_LEVEL_LOW"),
            coupon_quantity_per_year=2,
            floating_coupon_flag=False,
        )

    def get_last_price(self, uid):
        return SimpleNamespace(price=q(self._LAST[uid], 0), time=datetime.now(UTC), instrument_uid=uid, figi=None)

    def get_order_book(self, uid, depth=10):
        bid, ask = self._BOOK[uid]

        def _q(v):
            d = Decimal(str(v))
            units = int(d)
            nano = int((d - units) * 1_000_000_000)
            return SimpleNamespace(units=units, nano=nano)

        return SimpleNamespace(
            bids=[SimpleNamespace(price=_q(bid), quantity=100)],
            asks=[SimpleNamespace(price=_q(ask), quantity=100)],
            limit_up=_q(Decimal(str(ask)) * 2),
            limit_down=_q(1),
        )

    def get_trading_status(self, uid):
        return SimpleNamespace(
            trading_status=enum("SECURITY_TRADING_STATUS_NORMAL_TRADING"), api_trade_available_flag=True
        )

    def get_daily_candles(self, uid, from_, to):
        base = datetime.now(UTC)
        return [
            SimpleNamespace(close=q(self._LAST[uid], 0), volume=100, time=base - timedelta(days=i))
            for i in range(3, 0, -1)
        ]

    def get_order_price(self, account_id, uid, price, direction, quantity):
        return SimpleNamespace(total_order_amount=money(0), executed_commission=money(3), extra_bond=money(0))

    def get_operations_by_cursor(
        self,
        account_id,
        *,
        from_,
        to,
        cursor="",
        limit=100,
        operation_types=None,
        state=None,
        instrument_id=None,
        without_overnights=False,
    ):
        items = []
        if instrument_id == SHARE_UID:
            items = [
                SimpleNamespace(
                    id="op-buy-1",
                    date=datetime.now(UTC) - timedelta(days=400),
                    type=enum("OPERATION_TYPE_BUY"),
                    state=enum("OPERATION_STATE_EXECUTED"),
                    name="Покупка",
                    description=None,
                    instrument_uid=SHARE_UID,
                    ticker="SBER",
                    figi="F1",
                    instrument_type="share",
                    payment=money(-10000),
                    price=money(100),
                    commission=money(3),
                    quantity=q(100),
                    accrued_int=None,
                    trades_info=None,
                )
            ]
        return SimpleNamespace(items=items, has_next=False, next_cursor="")


def _save_profile(path, allocation=None):
    target = propose_allocation("conservative", "medium")
    if allocation is not None:
        target = TargetAllocation(
            risk_profile="conservative",
            horizon="medium",
            horizon_description="1-3 years",
            allocation=allocation,
            asset_class_roles=target.asset_class_roles,
            source="custom",
            rationale="test",
        )
    save_profile(
        str(path),
        InvestmentProfile(
            risk_profile="conservative",
            horizon="medium",
            target_allocation=target,
            saved_at=datetime.now(UTC),
        ),
    )


def test_create_trade_plan_full_rebalance(tmp_path):
    profile_path = tmp_path / "profile.json"
    _save_profile(profile_path)  # 70 bonds / 20 equity / 10 cash
    settings = make_settings(profile_path)
    adapter = PlanAdapter()

    plan = services.create_trade_plan(
        adapter,
        settings,
        items=[
            # Deliberately buy-first in the input: the server must re-sequence.
            SimpleNamespace(instrument_uid=BOND_UID, action="BUY", quantity_lots=None, amount="70000"),
            SimpleNamespace(instrument_uid=SHARE_UID, action="SELL", quantity_lots=3, amount=None),
        ],
    )

    # SELL leg first, BUY second.
    assert [leg.action for leg in plan.items] == ["SELL", "BUY"]
    sell, buy = plan.items
    assert sell.sequence == 1 and buy.sequence == 2

    # Sizing: 70 000 / (1000×100.1% + 10 НКД) = 69 whole lots.
    assert buy.quantity_lots == 69
    assert buy.unit_price_money == Decimal("1001.00")
    assert buy.accrued_interest == Decimal("690.00")
    assert buy.requested_amount == Decimal("70000")

    # SELL priced at the bid, tax from FIFO lots (basis 100, held 400 days → taxed).
    assert sell.price == Decimal("299")
    assert sell.tax is not None
    assert sell.tax.method == "fifo"
    assert sell.tax.taxable_gain == Decimal("5970.00")  # (299-100)×30
    assert sell.tax.tax == Decimal("776.10")

    # Broker commission on both legs; running cash never negative.
    assert {leg.commission for leg in plan.items} == {Decimal("3")}
    assert {leg.commission_source for leg in plan.items} == {"broker"}
    assert sell.cash_after == Decimal("70000") + Decimal("8970") - Decimal("3")
    assert buy.cash_after == sell.cash_after - Decimal("69069") - Decimal("690") - Decimal("3")

    # Plan-level checks (stage-7 engine + cost-aware extras) all pass.
    codes = {c.code for c in plan.plan_checks}
    assert {
        "PLAN_STEP_CASH",
        "PLAN_STEP_POSITION",
        "PLAN_ALLOCATION_MANDATE",
        "PLAN_MIN_CASH",
        "PLAN_CASH_WITH_COSTS",
        "PLAN_DAILY_TURNOVER",
    } <= codes
    assert plan.all_passed, [c for c in plan.plan_checks if not c.passed]

    # Allocation moves toward target and the verdict is WORTH_IT.
    preview = {row.asset_class: row for row in plan.allocation_preview}
    assert abs(preview["bonds"].deviation_after_pct) < abs(preview["bonds"].deviation_before_pct)
    assert plan.cost_benefit.verdict == "WORTH_IT"
    assert plan.cost_benefit.taxes == Decimal("776.10")
    assert plan.cost_benefit.commissions == Decimal("6.00")
    assert plan.status == "READY_FOR_CONFIRMATION"
    assert plan.skipped == []

    # Stored and retrievable by plan_id.
    store = trade_plan_mod.get_plan_store(settings.trade_plan_ttl_seconds)
    assert store.get(plan.plan_id) is not None


def test_create_trade_plan_not_worth_it_when_drift_below_threshold(tmp_path):
    profile_path = tmp_path / "profile.json"
    # Target matches the live portfolio exactly → nothing to fix.
    _save_profile(profile_path, allocation={"bonds": 0, "equity": 30, "cash": 70})
    settings = make_settings(profile_path)

    plan = services.create_trade_plan(
        PlanAdapter(),
        settings,
        items=[
            SimpleNamespace(instrument_uid=BOND_UID, action="BUY", quantity_lots=2, amount=None),
        ],
    )
    assert plan.status == "NOT_WORTH_IT"
    assert plan.cost_benefit.verdict == "NOT_WORTH_IT"
    assert "below the rebalance threshold" in plan.cost_benefit.reason
    assert plan.all_passed  # risk checks pass; the plan is just not worth doing


def test_create_trade_plan_warns_and_confirm_needs_acknowledgement(tmp_path):
    profile_path = tmp_path / "profile.json"
    _save_profile(profile_path)  # target 70 bonds / 20 equity / 10 cash
    # Realistic caps (the shared make_settings neutralizes them): one big sovereign buy
    # exceeds the per-issuer cap but improves the allocation → a soft warning, not a block.
    settings = make_settings(
        profile_path,
        mandate_max_issuer_weight=Decimal("0.15"),
        mandate_max_sector_weight=Decimal("0.30"),
    )
    adapter = PlanAdapter()

    plan = services.create_trade_plan(
        adapter,
        settings,
        items=[
            SimpleNamespace(instrument_uid=BOND_UID, action="BUY", quantity_lots=40, amount=None),
        ],
    )

    assert plan.status == "READY_WITH_WARNINGS"
    # No blocking (error) failures; the failing checks are warnings only.
    assert not any((not c.passed) and c.severity == "error" for c in plan.plan_checks)
    issuer = next(c for c in plan.plan_checks if c.code == "PLAN_ISSUER_LIMIT")
    assert not issuer.passed and issuer.severity == "warning"
    assert "OFZ bond" in issuer.message
    # The size-scaled cap relaxation is disclosed in the notes.
    assert any("Per-issuer cap relaxed" in n for n in plan.notes)

    # Gate 1 refuses to confirm a warning plan without explicit acknowledgement…
    with pytest.raises(Exception, match="acknowledge_warnings"):
        services.confirm_trade_plan(adapter, settings, plan.plan_id)
    # …and accepts it once the user has seen and accepted the warnings.
    confirmed = services.confirm_trade_plan(adapter, settings, plan.plan_id, acknowledge_warnings=True)
    assert confirmed.status == "CONFIRMED"


def test_create_trade_plan_skips_and_caps(tmp_path):
    profile_path = tmp_path / "profile.json"
    _save_profile(profile_path)
    settings = make_settings(profile_path)

    plan = services.create_trade_plan(
        PlanAdapter(),
        settings,
        items=[
            # Below one lot cost → skipped.
            SimpleNamespace(instrument_uid=BOND_UID, action="BUY", quantity_lots=None, amount="500"),
            # No position in the bond → SELL skipped.
            SimpleNamespace(instrument_uid=BOND_UID, action="SELL", quantity_lots=1, amount=None),
            # 99 lots requested, only 10 sellable → capped with a warning.
            SimpleNamespace(instrument_uid=SHARE_UID, action="SELL", quantity_lots=99, amount=None),
            # Both sizing fields → skipped.
            SimpleNamespace(instrument_uid=SHARE_UID, action="BUY", quantity_lots=1, amount="1000"),
        ],
    )
    reasons = " | ".join(item.reason for item in plan.skipped)
    assert len(plan.skipped) == 3
    assert "below the cost of one lot" in reasons
    assert "No sellable position" in reasons
    assert "exactly one of quantity_lots / amount" in reasons

    assert len(plan.items) == 1
    capped = plan.items[0]
    assert capped.quantity_lots == 10
    assert any("capped" in w for w in capped.warnings)


def test_create_trade_plan_requires_profile(tmp_path):
    settings = make_settings(tmp_path / "missing.json")
    with pytest.raises(Exception, match="investment profile"):
        services.create_trade_plan(
            PlanAdapter(),
            settings,
            items=[
                SimpleNamespace(instrument_uid=BOND_UID, action="BUY", quantity_lots=1, amount=None),
            ],
        )


# --- stages 8-10: two gates + sequential/idempotent execution ----------------


class ExecutingPlanAdapter(PlanAdapter):
    def __init__(self, *, immediate_fill=False):
        self.immediate_fill = immediate_fill
        self.post_calls = 0
        self.cancel_calls = 0
        self.order_status = "EXECUTION_REPORT_STATUS_FILL" if immediate_fill else "EXECUTION_REPORT_STATUS_NEW"
        self.phase = 0

    def post_order(self, **kwargs):
        self.post_calls += 1
        status = self.order_status
        filled = status == "EXECUTION_REPORT_STATUS_FILL"
        if filled:
            self.phase += 1
        return SimpleNamespace(
            order_id=f"broker-{self.post_calls}",
            execution_report_status=enum(status),
            total_order_amount=money(8970 if kwargs["direction"] == "SELL" else 69759),
            executed_commission=money(3),
            executed_order_price=money(299 if kwargs["direction"] == "SELL" else 100),
            lots_requested=kwargs["quantity"],
            lots_executed=kwargs["quantity"] if filled else 0,
            message="",
        )

    def get_order_state(self, account_id, order_id):
        filled = self.order_status == "EXECUTION_REPORT_STATUS_FILL"
        if filled and self.phase == 0:
            self.phase = 1
        return SimpleNamespace(
            execution_report_status=enum(self.order_status),
            lots_requested=3,
            lots_executed=3 if filled else 0,
            executed_order_price=money(299),
            total_order_amount=money(8970),
            executed_commission=money(3),
        )

    def cancel_order(self, account_id, order_id):
        self.cancel_calls += 1
        self.order_status = "EXECUTION_REPORT_STATUS_CANCELLED"

    def get_portfolio(self, account_id):
        if self.phase == 0:
            return super().get_portfolio(account_id)
        if self.phase == 1:
            return SimpleNamespace(
                total_amount_portfolio=money(99967),
                total_amount_currencies=money(78967),
                total_amount_shares=money(21000),
                total_amount_bonds=money(0),
                total_amount_etf=money(0),
                positions=[
                    SimpleNamespace(
                        instrument_uid=SHARE_UID,
                        figi="F1",
                        ticker="SBER",
                        instrument_type="share",
                        quantity=q(70),
                        quantity_lots=q(7),
                        average_position_price=money(100),
                        current_price=money(300),
                        expected_yield=q(0),
                    )
                ],
            )
        return SimpleNamespace(
            total_amount_portfolio=money(99205),
            total_amount_currencies=money(9205),
            total_amount_shares=money(21000),
            total_amount_bonds=money(69000),
            total_amount_etf=money(0),
            positions=[
                SimpleNamespace(
                    instrument_uid=SHARE_UID,
                    figi="F1",
                    ticker="SBER",
                    instrument_type="share",
                    quantity=q(70),
                    quantity_lots=q(7),
                    average_position_price=money(100),
                    current_price=money(300),
                    expected_yield=q(0),
                ),
                SimpleNamespace(
                    instrument_uid=BOND_UID,
                    figi="FB",
                    ticker="OFZ1",
                    instrument_type="bond",
                    quantity=q(69),
                    quantity_lots=q(69),
                    average_position_price=money(1000),
                    current_price=money(1000),
                    expected_yield=q(0),
                ),
            ],
        )


def _ready_execution_plan(tmp_path, adapter):
    profile_path = tmp_path / "profile.json"
    _save_profile(profile_path)
    settings = make_settings(profile_path)
    plan = services.create_trade_plan(
        adapter,
        settings,
        items=[
            SimpleNamespace(instrument_uid=BOND_UID, action="BUY", quantity_lots=None, amount="70000"),
            SimpleNamespace(instrument_uid=SHARE_UID, action="SELL", quantity_lots=3, amount=None),
        ],
    )
    assert plan.status == "READY_FOR_CONFIRMATION"
    return settings, plan


def test_plan_two_gates_direct_post_is_blocked_and_retry_is_idempotent(tmp_path):
    adapter = ExecutingPlanAdapter()
    settings, plan = _ready_execution_plan(tmp_path, adapter)

    with pytest.raises(Exception, match="Confirm the whole plan"):
        services.preview_plan_step(adapter, settings, plan.plan_id)

    confirmed = services.confirm_trade_plan(adapter, settings, plan.plan_id)
    assert confirmed.status == "CONFIRMED"
    assert [s.status for s in confirmed.steps] == ["PENDING", "PENDING"]

    card = services.preview_plan_step(adapter, settings, plan.plan_id, urgency="fast")
    assert card.step_sequence == 1
    assert card.preview.order["direction"] == "SELL"
    proposal_id = card.preview.proposal_id

    # A plan-linked proposal cannot bypass either plan gate.
    with pytest.raises(Exception, match="execute_plan_step"):
        services.post_order(adapter, settings, proposal_id)

    first = services.execute_plan_step(adapter, settings, plan.plan_id)
    assert first.order.status == "SUBMITTED"
    assert first.plan_status == "PAUSED"
    assert adapter.post_calls == 1

    # Retry refreshes the same broker order and never calls PostOrder again.
    retry = services.execute_plan_step(adapter, settings, plan.plan_id)
    assert retry.order.status == "SUBMITTED"
    assert adapter.post_calls == 1

    state = services.get_plan_state(adapter, settings, plan.plan_id)
    assert [s.status for s in state.steps] == ["SUBMITTED", "PENDING"]
    assert not state.can_preview_next
    with pytest.raises(Exception, match="poll get_plan_state"):
        services.preview_plan_step(adapter, settings, plan.plan_id)

    cancelled = services.cancel_trade_plan(adapter, settings, plan.plan_id)
    assert cancelled.status == "CANCELLED"
    assert [s.status for s in cancelled.steps] == ["CANCELLED", "SKIPPED"]
    assert adapter.cancel_calls == 1


def test_unconfirmed_plan_expires_at_plan_gate(tmp_path):
    adapter = ExecutingPlanAdapter()
    profile_path = tmp_path / "profile.json"
    _save_profile(profile_path)
    settings = make_settings(profile_path, trade_plan_ttl_seconds=0)
    plan = services.create_trade_plan(
        adapter,
        settings,
        items=[
            SimpleNamespace(instrument_uid=BOND_UID, action="BUY", quantity_lots=None, amount="70000"),
            SimpleNamespace(instrument_uid=SHARE_UID, action="SELL", quantity_lots=3, amount=None),
        ],
    )
    with pytest.raises(Exception, match="expired"):
        services.confirm_trade_plan(adapter, settings, plan.plan_id)
    state = services.get_plan_state(adapter, settings, plan.plan_id, refresh=False)
    assert state.status == "EXPIRED"
    assert state.is_terminal


def test_plan_fills_sell_then_buy_and_generates_verification_report(tmp_path):
    adapter = ExecutingPlanAdapter(immediate_fill=True)
    settings, plan = _ready_execution_plan(tmp_path, adapter)
    services.confirm_trade_plan(adapter, settings, plan.plan_id)

    sell_card = services.preview_plan_step(adapter, settings, plan.plan_id)
    assert sell_card.preview.order["direction"] == "SELL"
    sell_result = services.execute_plan_step(adapter, settings, plan.plan_id)
    assert sell_result.order.status == "FILLED"
    assert sell_result.next_action == "PREVIEW_NEXT_STEP"

    buy_card = services.preview_plan_step(adapter, settings, plan.plan_id)
    assert buy_card.preview.order["direction"] == "BUY"
    buy_result = services.execute_plan_step(adapter, settings, plan.plan_id)
    assert buy_result.order.status == "FILLED"
    assert buy_result.plan_status == "COMPLETED"
    assert buy_result.next_action == "VERIFY_PLAN"
    assert adapter.post_calls == 2

    state = services.get_plan_state(adapter, settings, plan.plan_id)
    assert [s.status for s in state.steps] == ["FILLED", "FILLED"]
    report = services.verify_trade_plan(adapter, settings, plan.plan_id)
    assert report.plan_status == "COMPLETED"
    assert report.allocation_before_pct["equity"] == Decimal("30.00")
    assert report.allocation_after_pct["bonds"] > Decimal("60")
    assert report.actual_commissions == Decimal("6.00")
    assert report.drift_reduction_pct_points > 0

    logged = services.log_recommendation(
        settings,
        plan.plan_id,
        rationale="OFZ matched the horizon and liquidity mandate.",
        alternatives_considered=["A lower-turnover corporate bond was rejected."],
    )
    assert logged["logged"] is True
