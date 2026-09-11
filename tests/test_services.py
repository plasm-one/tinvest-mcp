"""Integration test for the services orchestration using a fake adapter.

Validates create_order_proposal (risk engine + preview) and post_order
(re-validation, idempotency, status mapping) end-to-end without the gRPC SDK.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

import tinvest_mcp.proposals as proposals_mod
from tinvest_mcp import fx, profile_store, services, tools
from tinvest_mcp.config.env import DEFAULT_ALLOWED_INSTRUMENT_TYPES, Settings
from tinvest_mcp.errors import TInvestConfigurationError, TInvestDataUnavailableError
from tinvest_mcp.money import quotation_to_decimal
from tinvest_mcp.risk_engine import daily_turnover
from tinvest_mcp.schemas import InvestmentProfile, TargetAllocation, TradePlanStepInput


def setup_function(_):
    # Reset module-level singletons so tests don't leak turnover / proposals.
    daily_turnover._total = Decimal("0")
    daily_turnover._day = None
    proposals_mod._store = None


def q(units, nano=0):
    return SimpleNamespace(units=units, nano=nano)


def money(units, nano=0, currency="rub"):
    return SimpleNamespace(units=units, nano=nano, currency=currency)


def enum(name):
    return SimpleNamespace(name=name)


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
        "max_price_deviation_pct": Decimal("5.0"),
        "market_data_max_age_seconds": 120,
        "allow_market_orders": False,
        "allow_margin": False,
        "allow_shorts": False,
        "require_single_account_token": True,
        "enable_real_trading": False,
        "allowed_instrument_types": DEFAULT_ALLOWED_INSTRUMENT_TYPES,
        "instrument_allowlist": (),
        # Off by default so wall-clock time never makes these tests flaky; the
        # MOEX session gate has dedicated coverage (test_session_calendar,
        # test_risk_engine) and is exercised explicitly below.
        "market_session_check_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


class FakeAdapter:
    def __init__(self):
        self.posted = []

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
            total_amount_currencies=money(100000),
            total_amount_shares=money(0),
            total_amount_bonds=money(0),
            total_amount_etf=money(0),
            positions=[],
        )

    def get_instrument_by_uid(self, uid):
        return SimpleNamespace(
            uid=uid,
            instrument_type="share",
            figi="FIGI",
            ticker="SBER",
            name="Sberbank",
            currency="rub",
            lot=10,
            min_price_increment=q(0, 10000000),  # 0.01
            api_trade_available_flag=True,
            buy_available_flag=True,
            sell_available_flag=True,
            short_enabled_flag=False,
            for_qual_investor_flag=False,
            exchange="MOEX",
            class_code="TQBR",
            nominal=None,
            isin="RU000",
            sector="financial",
            country_of_risk="RU",
            trading_status=enum("SECURITY_TRADING_STATUS_NORMAL_TRADING"),
        )

    def get_last_price(self, uid):
        return SimpleNamespace(price=q(100, 0), time=datetime.now(UTC), instrument_uid=uid, figi="FIGI")

    # Deterministic per-uid prices so price sorting is testable.
    _PRICES = {"s1": 300, "s3": 50, "b1": 98, "b2": 101, "b3": 95}

    def get_last_prices(self, uids):
        out = []
        for u in uids:
            if u in self._PRICES:
                out.append(
                    SimpleNamespace(price=q(self._PRICES[u], 0), instrument_uid=u, figi=None, time=datetime.now(UTC))
                )
        return out

    def get_order_book(self, uid, depth=10):
        return SimpleNamespace(
            bids=[SimpleNamespace(price=q(99, 900000000), quantity=5)],
            asks=[SimpleNamespace(price=q(100, 100000000), quantity=5)],
            limit_up=q(110),
            limit_down=q(90),
        )

    def get_trading_status(self, uid):
        return SimpleNamespace(
            trading_status=enum("SECURITY_TRADING_STATUS_NORMAL_TRADING"), api_trade_available_flag=True
        )

    def get_order_price(self, account_id, uid, price, direction, quantity):
        # Like the real RPC, `price` is MONEY PER UNIT and the commission scales
        # with the trade size: 0.3% of price × lot × quantity.
        gross = quotation_to_decimal(price) if hasattr(price, "units") else Decimal(str(price))
        gross = gross * Decimal(10) * Decimal(quantity)
        commission = (gross * Decimal("0.003")).quantize(Decimal("0.01"))
        return SimpleNamespace(
            total_order_amount=money(int(gross)),
            executed_commission=money(int(commission), int((commission % 1) * 10**9)),
            extra_bond=None,
        )

    def get_max_lots(self, account_id, uid, price):
        return SimpleNamespace(
            buy_limits=SimpleNamespace(buy_max_lots=50), sell_limits=SimpleNamespace(sell_max_lots=50)
        )

    def post_order(self, **kwargs):
        self.posted.append(kwargs)
        return SimpleNamespace(
            order_id="broker-1",
            execution_report_status=enum("EXECUTION_REPORT_STATUS_FILL"),
            total_order_amount=money(1000),
            executed_commission=money(3),
            executed_order_price=money(100),
            lots_requested=1,
            lots_executed=1,
            message="",
        )

    def get_order_state(self, account_id, order_id):
        status = getattr(self, "_order_state_status", enum("EXECUTION_REPORT_STATUS_FILL"))
        lots_executed = 0 if status.name == "EXECUTION_REPORT_STATUS_NEW" else 1
        return SimpleNamespace(
            execution_report_status=status,
            lots_requested=1,
            lots_executed=lots_executed,
            executed_order_price=money(100),
            total_order_amount=money(1000),
            executed_commission=money(3),
        )

    def list_instruments(self, instrument_type):
        if instrument_type == "bond":
            base = datetime.now(UTC).date()
            return [
                SimpleNamespace(
                    uid="b1",
                    figi="FB1",
                    ticker="OFZ1",
                    name="OFZ short",
                    currency="rub",
                    lot=1,
                    api_trade_available_flag=True,
                    for_qual_investor_flag=False,
                    nominal=money(1000),
                    maturity_date=base + timedelta(days=400),
                    risk_level=enum("RISK_LEVEL_LOW"),
                    coupon_quantity_per_year=2,
                    sector="government",
                    country_of_risk="RU",
                ),
                SimpleNamespace(
                    uid="b2",
                    figi="FB2",
                    ticker="HIYLD",
                    name="High yield",
                    currency="rub",
                    lot=1,
                    api_trade_available_flag=True,
                    for_qual_investor_flag=False,
                    nominal=money(1000),
                    maturity_date=base + timedelta(days=3000),
                    risk_level=enum("RISK_LEVEL_HIGH"),
                    coupon_quantity_per_year=4,
                    sector="financial",
                    country_of_risk="RU",
                ),
                SimpleNamespace(
                    uid="b3",
                    figi="FB3",
                    ticker="MIDB",
                    name="Mid bond",
                    currency="rub",
                    lot=1,
                    api_trade_available_flag=True,
                    for_qual_investor_flag=False,
                    nominal=money(1000),
                    maturity_date=base + timedelta(days=900),
                    risk_level=enum("RISK_LEVEL_MODERATE"),
                    coupon_quantity_per_year=2,
                    sector="financial",
                    country_of_risk="RU",
                ),
            ]
        return [
            SimpleNamespace(
                uid="s1",
                figi="F1",
                ticker="SBER",
                name="Sber",
                currency="rub",
                lot=10,
                api_trade_available_flag=True,
                for_qual_investor_flag=False,
            ),
            SimpleNamespace(
                uid="s2",
                figi="F2",
                ticker="QUAL",
                name="Qual only",
                currency="rub",
                lot=1,
                api_trade_available_flag=True,
                for_qual_investor_flag=True,
            ),
            SimpleNamespace(
                uid="s3",
                figi="F3",
                ticker="AAPL",
                name="Apple",
                currency="usd",
                lot=1,
                api_trade_available_flag=True,
                for_qual_investor_flag=False,
            ),
            SimpleNamespace(
                uid="s4",
                figi="F4",
                ticker="NOAPI",
                name="No API",
                currency="rub",
                lot=1,
                api_trade_available_flag=False,
                for_qual_investor_flag=False,
            ),
        ]

    def get_daily_candles(self, uid, from_, to):
        base = datetime.now(UTC)
        closes = [100, 101, 99, 103, 105]
        return [
            SimpleNamespace(close=q(c, 0), time=base - timedelta(days=len(closes) - i)) for i, c in enumerate(closes)
        ]

    def get_dividends(self, uid, from_, to):
        return [
            SimpleNamespace(
                dividend_net=money(10),
                payment_date=datetime.now(UTC) - timedelta(days=30),
                yield_value=q(5, 0),
                record_date=None,
            )
        ]

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
        now = datetime.now(UTC)
        items = [
            SimpleNamespace(
                id="op-buy-1",
                date=now - timedelta(days=5),
                type=enum("OPERATION_TYPE_BUY"),
                state=enum("OPERATION_STATE_EXECUTED"),
                name="Покупка",
                description=None,
                instrument_uid="uid-1",
                ticker="SBER",
                figi="FIGI",
                instrument_type="share",
                payment=money(-1000),
                price=money(100),
                commission=money(3),
                quantity=q(10, 0),
                accrued_int=None,
                trades_info=SimpleNamespace(
                    trades=[
                        SimpleNamespace(num="t1", date=now - timedelta(days=5), quantity=10, price=money(100)),
                    ]
                ),
            ),
            SimpleNamespace(
                id="op-div-1",
                date=now - timedelta(days=2),
                type=enum("OPERATION_TYPE_DIVIDEND"),
                state=enum("OPERATION_STATE_EXECUTED"),
                name="Дивиденды",
                description=None,
                instrument_uid="uid-1",
                ticker="SBER",
                figi="FIGI",
                instrument_type="share",
                payment=money(50),
                price=None,
                commission=None,
                quantity=None,
                accrued_int=None,
                trades_info=None,
            ),
        ]
        return SimpleNamespace(items=items, has_next=False, next_cursor="")


def test_get_operations_page_and_totals():
    adapter = FakeAdapter()
    page = services.get_operations(adapter, make_settings(), limit=50)
    assert len(page.items) == 2
    assert page.items[0].type == "buy"
    assert page.items[1].type == "dividend"
    assert page.totals.trades_buy == Decimal("1000")
    assert page.totals.dividends == Decimal("50")
    assert page.totals.commissions == Decimal("3")
    assert page.has_next is False


def test_get_portfolio_analytics_allocations():
    class PortfolioAdapter(FakeAdapter):
        def get_portfolio(self, account_id):
            return SimpleNamespace(
                total_amount_portfolio=money(10000),
                total_amount_currencies=money(7000),
                total_amount_shares=money(3000),
                total_amount_bonds=money(0),
                total_amount_etf=money(0),
                positions=[
                    SimpleNamespace(
                        instrument_uid="uid-1",
                        figi="FIGI",
                        ticker="SBER",
                        instrument_type="share",
                        quantity=q(10, 0),
                        quantity_lots=q(1, 0),
                        average_position_price=money(280),
                        current_price=money(300),
                        expected_yield=q(7, 0),
                    ),
                ],
            )

    adapter = PortfolioAdapter()
    analytics = services.get_portfolio_analytics(adapter, make_settings(), top_n=3)
    assert analytics.total_value == Decimal("10000")
    assert analytics.by_class["shares"] == Decimal("0.3000")
    assert analytics.by_sector["financial"] == Decimal("0.3000")
    assert analytics.weighted_yield_pct == Decimal("5")
    assert len(analytics.top_positions) == 1
    assert analytics.top_positions[0].ticker == "SBER"
    assert analytics.concentration["largest_position_weight"] == Decimal("0.3000")


def test_create_proposal_with_urgency_fast():
    adapter = FakeAdapter()
    settings = make_settings()
    preview = services.create_order_proposal(
        adapter,
        settings,
        instrument_uid="uid-1",
        direction="BUY",
        order_type="LIMIT",
        quantity_lots=1,
        urgency="fast",
        rationale="test",
    )
    assert preview.all_passed, [c for c in preview.risk_checks if not c.passed]
    assert preview.order["urgency"] == "fast"
    assert preview.price_selection is not None
    assert preview.price_selection.crosses_spread is True
    # Fake ask is 100.10 -> fast limit should be ask
    assert preview.order["limit_price"] == "100.1"


def test_create_proposal_passes_and_executes():
    adapter = FakeAdapter()
    settings = make_settings()

    preview = services.create_order_proposal(
        adapter,
        settings,
        instrument_uid="uid-1",
        direction="BUY",
        order_type="LIMIT",
        quantity_lots=1,
        limit_price=Decimal("100"),
        rationale="test",
    )
    assert preview.all_passed, [c for c in preview.risk_checks if not c.passed]
    assert preview.status == "READY_FOR_CONFIRMATION"
    # 100 × 10 units = 1000 clean + 3.00 commission (0.3%).
    assert preview.order["estimated_total"] == "1003.00"

    result = services.post_order(adapter, settings, preview.proposal_id)
    assert result.status == "FILLED"
    assert result.broker_order_id == "broker-1"
    assert len(adapter.posted) == 1
    # idempotency key persisted and passed as order_id
    assert adapter.posted[0]["order_id"]


def test_post_order_sends_money_price_for_bonds_not_the_percent_quote():
    """OrdersService takes money per unit. Sending the % quote for a bond made the
    broker read 99.93 ₽ against a ~598-1396 ₽ band -> INVALID_ARGUMENT 30099."""

    class BondAdapter(FakeAdapter):
        def get_instrument_by_uid(self, uid):
            return SimpleNamespace(
                uid=uid,
                instrument_type="bond",
                figi="FIGI",
                ticker="RU000A10E6D0",
                name="Bond",
                currency="rub",
                lot=1,
                min_price_increment=q(0, 10000000),  # 0.01
                api_trade_available_flag=True,
                buy_available_flag=True,
                sell_available_flag=True,
                short_enabled_flag=False,
                for_qual_investor_flag=False,
                exchange="MOEX",
                class_code="TQCB",
                nominal=money(1000),
                isin="RU000A10E6D0",
                sector="corporate",
                country_of_risk="RU",
                trading_status=enum("SECURITY_TRADING_STATUS_NORMAL_TRADING"),
            )

    adapter = BondAdapter()
    settings = make_settings(
        max_order_rub=Decimal("50000"),
        max_daily_turnover_rub=Decimal("500000"),
    )
    preview = services.create_order_proposal(
        adapter,
        settings,
        instrument_uid="uid-1",
        direction="BUY",
        order_type="LIMIT",
        quantity_lots=7,
        limit_price=Decimal("99.93"),
    )
    # The human-facing preview keeps the bond convention: percent of nominal.
    assert preview.order["limit_price"] == "99.93"
    assert preview.order["price_quote_unit"] == "pct_of_nominal"

    services.post_order(adapter, settings, preview.proposal_id)
    # ...but the wire price is money per unit: 99.93% of a 1000 nominal.
    assert adapter.posted[0]["price"] == Decimal("999.30")


def test_post_order_sends_quote_unchanged_for_shares():
    """Shares/ETFs are quoted in currency, so quote == money price (identity)."""
    adapter = FakeAdapter()  # default instrument is a share, lot 10
    settings = make_settings()
    preview = services.create_order_proposal(
        adapter,
        settings,
        instrument_uid="uid-1",
        direction="BUY",
        order_type="LIMIT",
        quantity_lots=1,
        limit_price=Decimal("100"),
    )
    services.post_order(adapter, settings, preview.proposal_id)
    assert adapter.posted[0]["price"] == Decimal("100")


def test_post_order_broker_reject_is_definitive_with_full_error(monkeypatch):
    """A gRPC INVALID_ARGUMENT (e.g. 30099 price out of limits) means the broker
    refused the order outright — REJECTED with the broker's own message, never
    UNKNOWN_REQUIRES_RECONCILIATION."""
    from grpc import StatusCode
    from t_tech.invest.exceptions import RequestError
    from t_tech.invest.logging import Metadata

    class RejectingAdapter(FakeAdapter):
        def post_order(self, **kwargs):
            raise RequestError(
                StatusCode.INVALID_ARGUMENT,
                "30099",
                Metadata(
                    tracking_id="track-1",
                    ratelimit_limit="200, 200;w=60",
                    ratelimit_remaining=199,
                    ratelimit_reset=42,
                    message="Цена вне лимитов по инструменту",
                ),
            )

    adapter = RejectingAdapter()
    settings = make_settings()
    preview = services.create_order_proposal(
        adapter,
        settings,
        instrument_uid="uid-1",
        direction="BUY",
        order_type="LIMIT",
        quantity_lots=1,
        limit_price=Decimal("100"),
    )
    assert preview.all_passed

    result = services.post_order(adapter, settings, preview.proposal_id)
    assert result.status == "REJECTED"
    assert "INVALID_ARGUMENT" in result.message
    assert "30099" in result.message
    assert "Цена вне лимитов" in result.message
    assert "track-1" in result.message
    assert "No order was created" in result.message
    # The proposal is terminal REJECTED (retry goes through a fresh preview).
    stored = proposals_mod.get_store(settings.confirmation_ttl_seconds).get(preview.proposal_id)
    assert stored.status == "REJECTED"


def test_get_order_state_not_found_does_not_propagate():
    """50005 'Order not found' is a routine lifecycle event (filled-and-archived,
    cancelled, cleared at session end). It must never escape and 409 the caller —
    that killed a live plan mid-flight."""
    from grpc import StatusCode
    from t_tech.invest.exceptions import RequestError
    from t_tech.invest.logging import Metadata

    class VanishingOrderAdapter(FakeAdapter):
        def get_order_state(self, account_id, order_id):
            raise RequestError(
                StatusCode.NOT_FOUND,
                "50005",
                Metadata(
                    tracking_id="t-9",
                    ratelimit_limit="100, 100;w=60",
                    ratelimit_remaining=99,
                    ratelimit_reset=33,
                    message="Order not found",
                ),
            )

    adapter = VanishingOrderAdapter()
    settings = make_settings()
    preview = services.create_order_proposal(
        adapter,
        settings,
        instrument_uid="uid-1",
        direction="BUY",
        order_type="LIMIT",
        quantity_lots=1,
        limit_price=Decimal("100"),
    )
    services.post_order(adapter, settings, preview.proposal_id)

    result = services.get_order_state(adapter, settings, preview.proposal_id)
    assert result.status == "UNKNOWN_REQUIRES_RECONCILIATION"
    assert "no longer lists this order as active" in result.message
    assert "no new order was sent" in result.message


def test_post_order_network_fault_stays_unknown():
    class FlakyAdapter(FakeAdapter):
        def post_order(self, **kwargs):
            raise TimeoutError("socket timed out")

    adapter = FlakyAdapter()
    settings = make_settings()
    preview = services.create_order_proposal(
        adapter,
        settings,
        instrument_uid="uid-1",
        direction="BUY",
        order_type="LIMIT",
        quantity_lots=1,
        limit_price=Decimal("100"),
    )
    result = services.post_order(adapter, settings, preview.proposal_id)
    assert result.status == "UNKNOWN_REQUIRES_RECONCILIATION"
    assert "TimeoutError" in result.message
    assert result.idempotency_key  # reconcile key preserved, never re-keyed


def test_session_gate_blocks_proposal_during_clearing_pause(monkeypatch):
    from tinvest_mcp.session_calendar import MSK, moex_session_state

    adapter = FakeAdapter()
    settings = make_settings()
    pause = moex_session_state(datetime(2026, 7, 21, 18, 45, tzinfo=MSK))  # evening clearing
    monkeypatch.setattr(services, "_current_session_state", lambda _s: pause)

    preview = services.create_order_proposal(
        adapter,
        settings,
        instrument_uid="uid-1",
        direction="BUY",
        order_type="LIMIT",
        quantity_lots=1,
        limit_price=Decimal("100"),
        rationale="test",
    )
    assert preview.status == "RISK_REJECTED"
    sess = next(c for c in preview.risk_checks if c.code == "MARKET_SESSION")
    assert sess.passed is False and sess.severity == "error"
    assert "resumes" in sess.message


def test_session_gate_allows_proposal_during_main_session(monkeypatch):
    from tinvest_mcp.session_calendar import MSK, moex_session_state

    adapter = FakeAdapter()
    settings = make_settings()
    live = moex_session_state(datetime(2026, 7, 21, 12, 0, tzinfo=MSK))
    monkeypatch.setattr(services, "_current_session_state", lambda _s: live)

    preview = services.create_order_proposal(
        adapter,
        settings,
        instrument_uid="uid-1",
        direction="BUY",
        order_type="LIMIT",
        quantity_lots=1,
        limit_price=Decimal("100"),
        rationale="test",
    )
    assert preview.all_passed, [c for c in preview.risk_checks if not c.passed]
    sess = next(c for c in preview.risk_checks if c.code == "MARKET_SESSION")
    assert sess.passed is True


class SellAdapter(FakeAdapter):
    """Portfolio holds 30 units (3 lots) of uid-1 bought at 280, now at 300."""

    def get_portfolio(self, account_id):
        return SimpleNamespace(
            total_amount_portfolio=money(100000),
            total_amount_currencies=money(91000),
            total_amount_shares=money(9000),
            total_amount_bonds=money(0),
            total_amount_etf=money(0),
            positions=[
                SimpleNamespace(
                    instrument_uid="uid-1",
                    figi="FIGI",
                    ticker="SBER",
                    instrument_type="share",
                    quantity=q(30),
                    quantity_lots=q(3),
                    average_position_price=money(280),
                    current_price=money(300),
                    expected_yield=q(7),
                ),
            ],
        )


def test_create_sell_proposal_with_tax_and_ldv():
    adapter = SellAdapter()
    settings = make_settings(max_order_rub=Decimal("5000"), max_daily_turnover_rub=Decimal("10000"))
    preview = services.create_order_proposal(
        adapter,
        settings,
        instrument_uid="uid-1",
        direction="SELL",
        order_type="LIMIT",
        quantity_lots=2,
        limit_price=Decimal("100"),
    )
    assert preview.all_passed, [c for c in preview.risk_checks if not c.passed]
    assert preview.status == "READY_FOR_CONFIRMATION"
    # SELL: estimated_total is the gross proceeds (100 × 10 units × 2 lots);
    # cash grows by proceeds - commission (0.3% = 6).
    assert preview.order["estimated_total"] == "2000.00"
    assert preview.portfolio_impact["cash_after_estimated"] == "92994.00"
    # Tax estimate: unrealized P&L 600 on 30 units -> 400 for 20 units, 13% = 52.
    assert preview.tax_impact is not None
    assert preview.tax_impact.estimated_gain == Decimal("400.00")
    assert preview.tax_impact.estimated_tax == Decimal("52.00")
    checks = {c.code: c for c in preview.risk_checks}
    assert checks["TAX_IMPACT"].severity == "info"
    # Bought 5 days ago (fake operations) -> LDV is ~3 years away, info not warning.
    assert checks["LDV_WARNING"].severity == "info"
    # Weight decreases: 9% before, 7% after selling 2000 of the 9000 position.
    assert preview.portfolio_impact["position_weight_before"] == "0.09"
    assert preview.portfolio_impact["position_weight_after"] == "0.07"

    result = services.post_order(adapter, settings, preview.proposal_id)
    assert result.status == "FILLED"
    assert adapter.posted[0]["direction"] == "SELL"


def test_sell_more_than_held_risk_rejected():
    adapter = SellAdapter()
    settings = make_settings(max_order_rub=Decimal("50000"), max_daily_turnover_rub=Decimal("50000"))
    preview = services.create_order_proposal(
        adapter,
        settings,
        instrument_uid="uid-1",
        direction="SELL",
        order_type="LIMIT",
        quantity_lots=5,
        limit_price=Decimal("100"),  # 50 units vs 30 held
    )
    assert not preview.all_passed
    assert preview.status == "RISK_REJECTED"
    failed = {c.code for c in preview.risk_checks if not c.passed}
    assert failed == {"POSITION_EXISTS"}


def test_sell_urgency_fast_crosses_to_bid():
    adapter = SellAdapter()
    preview = services.create_order_proposal(
        adapter,
        make_settings(),
        instrument_uid="uid-1",
        direction="SELL",
        order_type="LIMIT",
        quantity_lots=1,
        urgency="fast",
    )
    assert preview.all_passed, [c for c in preview.risk_checks if not c.passed]
    # Fake bid is 99.90 -> fast SELL joins/crosses the bid.
    assert preview.order["limit_price"] == "99.9"
    assert preview.price_selection is not None
    assert preview.price_selection.crosses_spread is True


def test_double_execution_blocked():
    adapter = FakeAdapter()
    settings = make_settings()
    preview = services.create_order_proposal(
        adapter,
        settings,
        instrument_uid="uid-1",
        direction="BUY",
        order_type="LIMIT",
        quantity_lots=1,
        limit_price=Decimal("100"),
    )
    services.post_order(adapter, settings, preview.proposal_id)
    # Second attempt must refuse (terminal FILLED state).
    with pytest.raises(Exception):
        services.post_order(adapter, settings, preview.proposal_id)


def test_risk_rejected_blocks_post_order():
    adapter = FakeAdapter()
    settings = make_settings(max_order_rub=Decimal("100"))  # 1003 > 100 -> reject
    preview = services.create_order_proposal(
        adapter,
        settings,
        instrument_uid="uid-1",
        direction="BUY",
        order_type="LIMIT",
        quantity_lots=1,
        limit_price=Decimal("100"),
    )
    assert not preview.all_passed
    assert preview.status == "RISK_REJECTED"
    with pytest.raises(Exception):
        services.post_order(adapter, settings, preview.proposal_id)


def test_list_executing_orders_includes_submitted_and_excludes_terminal():
    adapter = FakeAdapter()
    settings = make_settings()
    preview = services.create_order_proposal(
        adapter,
        settings,
        instrument_uid="uid-1",
        direction="BUY",
        order_type="LIMIT",
        quantity_lots=1,
        limit_price=Decimal("100"),
    )
    store = proposals_mod.get_store(settings.confirmation_ttl_seconds)
    store.set_status(preview.proposal_id, "SUBMITTED")
    store.update(preview.proposal_id, broker_order_id="broker-open")

    adapter._order_state_status = enum("EXECUTION_REPORT_STATUS_NEW")

    hits = services.list_executing_orders(adapter, settings, refresh=True)
    assert len(hits) == 1
    assert hits[0].proposal_id == preview.proposal_id
    assert hits[0].status == "SUBMITTED"
    assert hits[0].lots_executed == 0

    store.set_status(preview.proposal_id, "FILLED")
    assert services.list_executing_orders(adapter, settings, refresh=False) == []


def test_list_shares_filters():
    adapter = FakeAdapter()
    hits = services.list_shares(adapter, currency="rub", api_trade_available=True, qualified_only=False, limit=100)
    tickers = {h.ticker for h in hits}
    # SBER stays; QUAL excluded (qualified), AAPL excluded (usd), NOAPI excluded (no api).
    assert tickers == {"SBER"}
    sber = next(h for h in hits if h.ticker == "SBER")
    assert sber.instrument_type == "share"
    assert sber.last_price == Decimal("300")
    # Share rows must not carry bond-only fields.
    assert not hasattr(sber, "risk_level")
    assert not hasattr(sber, "current_yield_pct")


def test_list_bonds_static_fields_and_filters():
    adapter = FakeAdapter()
    # Only low-risk bonds maturing within ~2 years -> just OFZ1 (b1).
    hits = services.list_bonds(adapter, risk_level="low", max_maturity_years=2)
    assert {h.ticker for h in hits} == {"OFZ1"}
    ofz = hits[0]
    assert ofz.instrument_type == "bond"
    assert ofz.risk_level == "low"
    assert ofz.coupons_per_year == 2
    assert ofz.nominal == Decimal("1000")
    assert ofz.maturity_date is not None


def test_list_bonds_mandate_filters_risk_cap_and_excluded_sectors():
    adapter = FakeAdapter()
    # Cap at moderate: HIGH-risk b2 is dropped; unknown tiers would be dropped too.
    hits = services.list_bonds(adapter, max_bond_risk_level="moderate")
    assert {h.ticker for h in hits} == {"OFZ1", "MIDB"}
    # Excluded sectors cut financial bonds (b2, b3), leaving the government one.
    hits = services.list_bonds(adapter, excluded_sectors=["Financial"])
    assert {h.ticker for h in hits} == {"OFZ1"}


def test_liquidity_from_candles_bond_vs_share_units():
    # 3 sessions, volume in lots. Bond: close is % of nominal -> money = nominal*close/100.
    candles = [
        SimpleNamespace(close=q(100, 0), volume=50),
        SimpleNamespace(close=q(98, 0), volume=100),
        SimpleNamespace(close=q(102, 0), volume=150),
    ]
    avg_lots, avg_turnover, days = services._liquidity_from_candles(
        candles, lot=1, nominal=Decimal("1000"), is_bond=True
    )
    assert days == 3
    assert avg_lots == Decimal("100.00")
    # (50*1000 + 100*980 + 150*1020) / 3 = 100333.33
    assert avg_turnover == Decimal("100333.33")

    # Share: close is money per unit; lot multiplies units per lot.
    avg_lots, avg_turnover, days = services._liquidity_from_candles(candles, lot=10, nominal=None, is_bond=False)
    assert avg_lots == Decimal("100.00")
    # (50*10*100 + 100*10*98 + 150*10*102) / 3 = 100333.33
    assert avg_turnover == Decimal("100333.33")


def test_liquidity_from_candles_no_volume_is_none():
    candles = [
        SimpleNamespace(close=q(100, 0)),  # no volume attribute at all
        SimpleNamespace(close=q(101, 0), volume=0),
    ]
    avg_lots, avg_turnover, days = services._liquidity_from_candles(candles, lot=1, nominal=None, is_bond=False)
    assert (avg_lots, avg_turnover, days) == (None, None, 0)


def test_liquidity_post_filter_drops_missing_and_thin():
    keep = services._liquidity_post_filter(Decimal("1000000"))
    liquid = SimpleNamespace(avg_daily_turnover_rub=Decimal("2000000"))
    thin = SimpleNamespace(avg_daily_turnover_rub=Decimal("50000"))
    unknown = SimpleNamespace(avg_daily_turnover_rub=None)
    assert keep(liquid)
    assert not keep(thin)
    assert not keep(unknown)


def test_liquidity_post_filter_uses_converted_turnover_for_fx_bonds():
    # A yuan bond trading ~900k CNY/day is ~10m RUB/day: judged on its native
    # figure it would fail a 1m ₽ bar it actually clears by 10x.
    keep = services._liquidity_post_filter(Decimal("1000000"))
    cny_bond = SimpleNamespace(
        avg_daily_turnover=Decimal("900000"),
        avg_daily_turnover_rub=Decimal("10400000"),
    )
    assert keep(cny_bond)
    # Without a rate there is no ruble figure to judge, so the row is dropped.
    unconverted = SimpleNamespace(
        avg_daily_turnover=Decimal("900000"),
        avg_daily_turnover_rub=None,
    )
    assert not keep(unconverted)


def test_list_bonds_sort_by_risk_then_price():
    adapter = FakeAdapter()
    # Cheap sort across all bonds: low < moderate < high ascending.
    hits = services.list_bonds(adapter, sort_by="risk", descending=False)
    assert [h.ticker for h in hits] == ["OFZ1", "MIDB", "HIYLD"]
    # Price sort (descending) uses the batched prices: b2=101 > b1=98 > b3=95.
    by_price = services.list_bonds(adapter, sort_by="price", descending=True)
    assert [h.ticker for h in by_price] == ["HIYLD", "OFZ1", "MIDB"]


def test_list_shares_include_analytics_caps_and_enriches():
    adapter = FakeAdapter()
    hits = services.list_shares(
        adapter,
        currency="rub",
        include_analytics=True,
        analytics_limit=5,
        settings=make_settings(),
    )
    sber = next(h for h in hits if h.ticker == "SBER")
    # Analytics computed from the fake candles/dividends.
    assert sber.historical_return_pct == Decimal("5")
    assert sber.dividend_yield_pct == Decimal("5")


class FlakyCandlesAdapter(FakeAdapter):
    def __init__(self, *, permanent: bool = False):
        super().__init__()
        self.permanent = permanent
        self.candle_attempts = {}

    def get_daily_candles(self, uid, from_, to):
        self.candle_attempts[uid] = self.candle_attempts.get(uid, 0) + 1
        # A screening analytics attempt touches candles exactly ONCE, so failing
        # the first call fails the whole first pass and the retry then succeeds.
        if self.permanent or self.candle_attempts[uid] <= 1:
            raise RuntimeError("temporary candle outage")
        base = datetime.now(UTC)
        closes = [100, 101, 102]
        return [
            SimpleNamespace(
                close=q(close, 0),
                volume=2_000,
                time=base - timedelta(days=len(closes) - index),
            )
            for index, close in enumerate(closes)
        ]


def test_analytics_screening_retries_degraded_rows_once(monkeypatch):
    monkeypatch.setattr(services, "_ANALYTICS_RETRY_DELAY_SECONDS", 0)
    adapter = FlakyCandlesAdapter()

    hits = services.list_shares(
        adapter,
        settings=make_settings(analytics_concurrency=1),
        currency="rub",
        min_avg_daily_turnover=Decimal("1000000"),
        include_analytics=True,
        analytics_limit=1,
        limit=1,
    )

    assert [hit.ticker for hit in hits] == ["SBER"]
    # One candle call per attempt: the failed first pass, then the retry.
    assert adapter.candle_attempts == {"s1": 2}
    assert hits[0].avg_daily_turnover == Decimal("2020000.00")


def test_analytics_screening_surfaces_error_after_retry(monkeypatch):
    monkeypatch.setattr(services, "_ANALYTICS_RETRY_DELAY_SECONDS", 0)
    adapter = FlakyCandlesAdapter(permanent=True)

    with pytest.raises(TInvestDataUnavailableError, match="after one retry for 1/1"):
        services.list_shares(
            adapter,
            settings=make_settings(analytics_concurrency=1),
            currency="rub",
            min_avg_daily_turnover=Decimal("1000000"),
            include_analytics=True,
            analytics_limit=1,
            limit=1,
        )

    assert adapter.candle_attempts == {"s1": 2}


def test_data_unavailable_error_is_exposed_as_tool_error():
    def fail():
        raise TInvestDataUnavailableError("analytics unavailable after retry")

    with pytest.raises(ToolError, match="analytics unavailable after retry"):
        tools._guard(fail)


def test_analytics_screening_keeps_legitimate_empty_result(monkeypatch):
    monkeypatch.setattr(services, "_ANALYTICS_RETRY_DELAY_SECONDS", 0)
    # FakeAdapter returns candles without volume. The data call succeeds, so an
    # empty liquidity-filtered result is real and must not be reported as an outage.
    hits = services.list_shares(
        FakeAdapter(),
        settings=make_settings(analytics_concurrency=1),
        currency="rub",
        min_avg_daily_turnover=Decimal("1000000"),
        include_analytics=True,
        analytics_limit=1,
        limit=1,
    )
    assert hits == []


def test_instrument_analytics_reports_unavailable_components():
    analytics = services.get_instrument_analytics(
        FlakyCandlesAdapter(permanent=True),
        make_settings(),
        "s1",
    )
    assert analytics.unavailable_components == ["candles"]


def test_list_shares_filter_pays_dividends():
    adapter = FakeAdapter()
    # FakeAdapter shares have no div_yield_flag -> pays_dividends False for all.
    assert services.list_shares(adapter, currency="rub", pays_dividends=True) == []
    assert {h.ticker for h in services.list_shares(adapter, currency="rub", pays_dividends=False)} == {"SBER"}


def test_parallel_analytics_maps_results_to_correct_rows():
    # Each bond yields a DISTINCT historical return; with concurrency > 1 the
    # results must still land on the matching row (no cross-wiring).
    returns_by_uid = {"b1": [100, 110], "b2": [100, 120], "b3": [100, 130]}

    class PerUidAdapter(FakeAdapter):
        def get_daily_candles(self, uid, from_, to):
            base = datetime.now(UTC)
            closes = returns_by_uid[uid]
            return [
                SimpleNamespace(close=q(c, 0), time=base - timedelta(days=len(closes) - i))
                for i, c in enumerate(closes)
            ]

    adapter = PerUidAdapter()
    settings = make_settings(analytics_concurrency=3)
    hits = services.list_bonds(adapter, include_analytics=True, analytics_limit=10, settings=settings)
    got = {h.ticker: h.historical_return_pct for h in hits}
    assert got == {"OFZ1": Decimal("10"), "HIYLD": Decimal("20"), "MIDB": Decimal("30")}


def test_get_instrument_analytics_share():
    adapter = FakeAdapter()
    a = services.get_instrument_analytics(adapter, make_settings(), "uid-1")
    assert a.instrument_type == "share"
    assert a.history_days == 5
    # closes 100->105 => +5% return
    assert a.historical_return_pct == Decimal("5")
    assert a.volatility_annual_pct is not None and a.volatility_annual_pct > 0
    assert a.max_drawdown_pct is not None and a.max_drawdown_pct > 0  # 101->99 dip
    assert a.dividend_yield_pct == Decimal("5")
    assert a.notes  # caveats present


# --- FX-denominated bonds (yuan issues listed on a ruble board) --------------


class CnyBondAdapter(FakeAdapter):
    """A CNY-denominated bond settled in rubles, plus the CNYRUB_TOM fixing.

    Mirrors the real shape of Газпром капитал БО-003Р-20 on TQCB: the SDK reports
    currency='rub' while nominal / ACI / coupons all carry currency='cny'.
    """

    RATE = Decimal("10")  # 10 ₽ per 1 ¥ keeps the expected numbers readable

    def get_instrument_by_uid(self, uid):
        return SimpleNamespace(
            uid=uid,
            instrument_type="bond",
            figi="FCNY",
            ticker="RU000CNY",
            name="Yuan bond",
            currency="rub",
            lot=1,
            min_price_increment=q(0, 10000000),
            api_trade_available_flag=True,
            buy_available_flag=True,
            sell_available_flag=True,
            short_enabled_flag=False,
            for_qual_investor_flag=False,
            exchange="MOEX",
            class_code="TQCB",
            nominal=money(1000, 0, currency="cny"),
            maturity_date=datetime.now(UTC).date() + timedelta(days=900),
            isin="RU000CNY",
            sector="energy",
            country_of_risk="RU",
            trading_status=enum("SECURITY_TRADING_STATUS_NORMAL_TRADING"),
        )

    def get_bond_by_uid(self, uid):
        return SimpleNamespace(
            uid=uid,
            risk_level=enum("RISK_LEVEL_MODERATE"),
            coupon_quantity_per_year=12,
            nominal=money(1000, 0, currency="cny"),
            initial_nominal=money(1000, 0, currency="cny"),
            liquidity_flag=True,
            for_iis_flag=True,
            issue_kind="non_documentary",
            issue_size=3000000,
            aci_value=money(2, 0, currency="cny"),
            maturity_date=datetime.now(UTC).date() + timedelta(days=900),
            call_date=None,
            floating_coupon_flag=False,
        )

    def get_bond_coupons(self, uid, from_, to):
        base = datetime.now(UTC)
        return [
            SimpleNamespace(coupon_date=base + timedelta(days=30 * i), pay_one_bond=money(6, 800000000, currency="cny"))
            for i in range(1, 13)
        ]

    def get_last_price(self, uid):
        price = q(11, 561500000) if uid == "cny-fx" else q(98, 0)
        return SimpleNamespace(price=price, time=datetime.now(UTC), instrument_uid=uid, figi="F")

    def get_last_prices(self, uids):
        return [self.get_last_price(u) for u in uids]

    def get_daily_candles(self, uid, from_, to):
        base = datetime.now(UTC)
        closes = [97, 98, 97, 98, 98]
        return [
            SimpleNamespace(close=q(c, 0), volume=100, time=base - timedelta(days=len(closes) - i))
            for i, c in enumerate(closes)
        ]

    def list_currencies(self):
        return [
            SimpleNamespace(
                uid="cny-fx", ticker="CNYRUB_TOM", iso_currency_name="cny", nominal=money(1, 0, currency="cny")
            )
        ]

    def get_order_price(self, account_id, uid, price, direction, quantity):
        # The yuan board bills in yuan; НКД arrives inside extra_bond, as in the
        # real response. `price` is money per unit, so the caller must have
        # converted the % quote already.
        return SimpleNamespace(
            total_order_amount=money(980, 0, currency="cny"),
            executed_commission=money(3, 0, currency="cny"),
            extra_bond=SimpleNamespace(aci_value=money(2, 0, currency="cny")),
        )


def test_analytics_labels_a_yuan_bond_yield_as_yuan():
    fx.clear_cache()
    a = services.get_instrument_analytics(CnyBondAdapter(), make_settings(), "cny-bond")
    assert a.currency == "rub"  # settlement
    assert a.nominal_currency == "cny"  # denomination
    assert a.yield_currency == "cny"  # so the yields are yuan yields
    assert a.fx_linked is True
    assert a.ytm_pct is not None
    assert any("CNY" in note for note in a.notes)


def test_analytics_converts_yuan_turnover_to_rubles():
    fx.clear_cache()
    a = services.get_instrument_analytics(CnyBondAdapter(), make_settings(), "cny-bond")
    # 100 lots/day × 1000 ¥ nominal × ~98% ≈ 98k ¥, which is ~1.13m ₽ — the
    # difference between "too thin to trade" and "fine" against a ruble bar.
    assert a.avg_daily_turnover is not None
    assert a.avg_daily_turnover_rub == pytest.approx(a.avg_daily_turnover * Decimal("11.5615"), rel=Decimal("0.001"))
    assert a.fx_rate_rub == Decimal("11.5615")


def test_ruble_instrument_needs_no_fx_lookup():
    fx.clear_cache()

    class NoFxAdapter(FakeAdapter):
        def list_currencies(self):  # pragma: no cover - must never be reached
            raise AssertionError("ruble instruments must not touch the FX board")

    a = services.get_instrument_analytics(NoFxAdapter(), make_settings(), "uid-1")
    assert a.fx_linked is False
    assert a.avg_daily_turnover_rub == a.avg_daily_turnover


def test_order_proposal_checks_the_ruble_value_of_a_yuan_order():
    fx.clear_cache()
    adapter = CnyBondAdapter()
    # One bond at 98% of a 1000 ¥ nominal = 980 ¥ clean, +2 ¥ НКД +3 ¥ commission,
    # all of it ≈ 11 388 ₽ at 11.5615 — an order that blows through the 1500 ₽
    # per-order limit while its raw yuan figure would sail under it.
    preview = services.create_order_proposal(
        adapter,
        make_settings(),
        instrument_uid="cny-bond",
        direction="BUY",
        order_type="LIMIT",
        quantity_lots=1,
        limit_price=Decimal("98.0"),
    )
    assert preview.order["currency"] == "rub"
    assert preview.order["denomination_currency"] == "cny"
    cent = Decimal("0.01")
    expected_rub = (
        (Decimal("980") * Decimal("11.5615")).quantize(cent)  # clean
        + (Decimal("2") * Decimal("11.5615")).quantize(cent)  # НКД
        + (Decimal("3") * Decimal("11.5615")).quantize(cent)  # commission
    )
    assert Decimal(preview.order["estimated_total"]) == expected_rub
    max_order = next(c for c in preview.risk_checks if c.code == "MAX_ORDER_VALUE")
    assert not max_order.passed
    exposure = next(c for c in preview.risk_checks if c.code == "CURRENCY_EXPOSURE")
    assert exposure.severity == "warning" and "CNY" in exposure.message


def test_bond_order_value_uses_money_not_percent_of_nominal():
    """A bond order costs nominal × price%, not `price` — including for rubles.

    GetOrderPrice takes MONEY per unit, so passing the % quote used to return a
    total ten times too small for a 1000-nominal bond, and that total is what
    max_order_rub and the cash check are measured against.
    """
    fx.clear_cache()

    class RubBondAdapter(CnyBondAdapter):
        """Same shape, but a plain ruble bond: nominal and ACI in rubles."""

        def get_instrument_by_uid(self, uid):
            raw = super().get_instrument_by_uid(uid)
            raw.nominal = money(1000, 0, currency="rub")
            return raw

        def get_bond_by_uid(self, uid):
            bond = super().get_bond_by_uid(uid)
            bond.nominal = money(1000, 0, currency="rub")
            bond.aci_value = money(2, 0, currency="rub")
            return bond

        def get_order_price(self, account_id, uid, price, direction, quantity):
            self.seen_price = price
            return SimpleNamespace(
                total_order_amount=money(980, 0, currency="rub"),
                executed_commission=money(3, 0, currency="rub"),
                extra_bond=SimpleNamespace(aci_value=money(2, 0, currency="rub")),
            )

    adapter = RubBondAdapter()
    preview = services.create_order_proposal(
        adapter,
        make_settings(max_order_rub=Decimal("5000")),
        instrument_uid="rub-bond",
        direction="BUY",
        order_type="LIMIT",
        quantity_lots=1,
        limit_price=Decimal("98.0"),
    )
    # 98% of a 1000 ₽ nominal = 980 ₽ clean, + 2 ₽ НКД + 3 ₽ commission.
    assert preview.order["estimated_total"] == "985.00"
    assert Decimal(preview.order["limit_price"]) == Decimal("98")  # order still in %
    assert adapter.seen_price == Decimal("980")  # the broker gets money per unit
    # A ruble bond carries no currency bet, so neither block nor check appears.
    assert "denomination_currency" not in preview.order
    assert not [c for c in preview.risk_checks if c.code == "CURRENCY_EXPOSURE"]


def test_list_bonds_refuses_to_rank_yields_across_currencies():
    rows = [
        SimpleNamespace(yield_currency="rub", ytm_pct=Decimal("15.7")),
        SimpleNamespace(yield_currency="cny", ytm_pct=Decimal("8.7")),
    ]
    with pytest.raises(TInvestConfigurationError) as exc:
        services._reject_cross_currency_yield_ranking(rows, "ytm")
    assert "denomination_currency" in str(exc.value)
    # One currency, or a non-yield sort, ranks normally.
    services._reject_cross_currency_yield_ranking(rows, "maturity")
    services._reject_cross_currency_yield_ranking(rows[:1], "ytm")


def test_account_auto_resolved_without_env():
    # No TINVEST_ACCOUNT_ID set, single visible account -> auto-resolved.
    adapter = FakeAdapter()
    settings = make_settings(account_id=None)
    summary = services.get_portfolio_summary(adapter, settings)
    assert summary.total_value == Decimal("100000")


def test_list_accounts_separates_research_and_execution_access():
    class SplitAccessAdapter(FakeAdapter):
        def get_accounts(self):
            account = super().get_accounts()[0]
            account.access_level = enum("ACCOUNT_ACCESS_LEVEL_READ_ONLY")
            return [account]

        def get_trade_accounts(self):
            account = super().get_accounts()[0]
            account.access_level = enum("ACCOUNT_ACCESS_LEVEL_FULL_ACCESS")
            return [account]

    accounts = services.list_accounts(
        SplitAccessAdapter(),
        make_settings(
            mode="prod",
            readonly_token="read-token",
            fullaccess_token="trade-token",
            enable_real_trading=True,
        ),
    )

    assert len(accounts) == 1
    account = accounts[0]
    assert account.research_access_level == "ACCOUNT_ACCESS_LEVEL_READ_ONLY"
    assert account.planning_available is True
    assert account.execution_available is True
    assert account.execution_access_level == "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"
    assert account.execution_block_reason is None
    assert "access_level" not in account.model_dump()


def test_list_accounts_reports_execution_block_without_trade_token():
    class ReadOnlyAdapter(FakeAdapter):
        def get_accounts(self):
            account = super().get_accounts()[0]
            account.access_level = enum("ACCOUNT_ACCESS_LEVEL_READ_ONLY")
            return [account]

    account = services.list_accounts(
        ReadOnlyAdapter(),
        make_settings(
            mode="prod",
            readonly_token="read-token",
            fullaccess_token=None,
            enable_real_trading=True,
        ),
    )[0]

    assert account.planning_available is True
    assert account.execution_available is False
    assert account.execution_access_level is None
    assert account.execution_block_reason == "No full-access trade token is configured."


def test_no_accounts_raises():
    from tinvest_mcp.errors import TInvestAccountNotFoundError

    class EmptyAdapter(FakeAdapter):
        def get_accounts(self):
            return []

    with pytest.raises(TInvestAccountNotFoundError):
        services.resolve_account_id(EmptyAdapter(), make_settings(account_id=None))


def test_multiple_accounts_requires_override():
    from tinvest_mcp.errors import TInvestConfigurationError

    class MultiAdapter(FakeAdapter):
        def get_accounts(self):
            base = super().get_accounts()[0]
            second = SimpleNamespace(
                id="acc-2",
                type=enum("ACCOUNT_TYPE_TINKOFF"),
                status=enum("ACCOUNT_STATUS_OPEN"),
                name="other",
                opened_date=None,
                closed_date=None,
                access_level=enum("ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"),
            )
            return [base, second]

    # Sandbox + 2 accounts, no override -> ambiguous.
    with pytest.raises(TInvestConfigurationError):
        services.resolve_account_id(MultiAdapter(), make_settings(account_id=None))
    # With an explicit override it resolves.
    assert services.resolve_account_id(MultiAdapter(), make_settings(account_id="acc-2")) == "acc-2"


def test_prod_real_trading_disabled_blocks():
    adapter = FakeAdapter()
    settings = make_settings(mode="prod", fullaccess_token="ft", enable_real_trading=False)
    preview = services.create_order_proposal(
        adapter,
        settings,
        instrument_uid="uid-1",
        direction="BUY",
        order_type="LIMIT",
        quantity_lots=1,
        limit_price=Decimal("100"),
    )
    # Risk passes, but execution is blocked by the real-trading flag.
    from tinvest_mcp.errors import TInvestRealTradingDisabledError

    with pytest.raises(TInvestRealTradingDisabledError):
        services.post_order(adapter, settings, preview.proposal_id)


def test_get_etf_details_maps_asset_etf():
    class EtfAdapter(FakeAdapter):
        def get_instrument_by_uid(self, uid):
            return SimpleNamespace(
                uid=uid,
                instrument_type="etf",
                figi="F",
                ticker="LQDT",
                name="Liquidity",
                currency="rub",
                lot=1,
                min_price_increment=q(0, 10000000),
                api_trade_available_flag=True,
                buy_available_flag=True,
                sell_available_flag=True,
                short_enabled_flag=False,
                for_qual_investor_flag=False,
                exchange="MOEX",
                class_code="TQBR",
                isin="RU000",
                sector="other",
                country_of_risk="RU",
                trading_status=enum("SECURITY_TRADING_STATUS_NORMAL_TRADING"),
                asset_uid="asset-1",
            )

        def get_etf_by_uid(self, uid):
            return SimpleNamespace(
                uid=uid,
                asset_uid="asset-1",
                isin="RU000A111",
                ticker="LQDT",
                focus_type="equity",
                rebalancing_freq=None,
                num_shares=q(0),
                fixed_commission=q(0),
                liquidity_flag=True,
                for_iis_flag=True,
                released_date=datetime(2019, 11, 28, tzinfo=UTC),
            )

        def get_asset_by(self, asset_uid):
            ext = SimpleNamespace(
                total_expense=q(1, 599000000),
                expense_commission=q(0, 99000000),
                fixed_commission=q(1, 500000000),
                focus_type="fixed_income",
                rebalancing_freq="semi_annual",
                rebalancing_flag=True,
                primary_index="OFZ index",
                primary_index_description="Adaptive OFZ strategy",
                management_type="passive",
                div_yield_flag=False,
                description="OFZ ladder fund",
                inav_code="TOFZA",
                num_share=q(0),
                released_date=datetime(2024, 11, 7, tzinfo=UTC),
                primary_index_tracking_error=q(0),
                buy_premium=q(0),
                sell_discount=q(0),
                hurdle_rate=q(0),
                performance_fee=q(0),
                leveraged_flag=False,
                ucits_flag=False,
            )
            sec = SimpleNamespace(etf=ext)
            asset = SimpleNamespace(security=sec, name="BPIF TOFZ")
            return SimpleNamespace(asset=asset)

    out = services.get_etf_details(EtfAdapter(), make_settings(), "etf-uid")
    assert out.ticker == "LQDT"
    assert out.total_expense_pct == Decimal("1.599")
    assert out.fixed_commission_pct == Decimal("1.5")
    assert out.focus_type == "fixed_income"
    assert out.inav_code == "TOFZA"
    assert out.liquidity_flag is True
    assert any("holdings" in n.lower() for n in out.notes)


class EtfScreenAdapter(FakeAdapter):
    _TER = {"asset-1": q(1, 90000000), "asset-2": q(0, 790000000)}  # 1.09 / 0.79

    def __init__(self):
        super().__init__()
        self.asset_calls = []

    def list_instruments(self, instrument_type):
        assert instrument_type == "etf"
        return [
            SimpleNamespace(
                uid="e1",
                asset_uid="asset-1",
                figi="FE1",
                ticker="TMOS",
                name="Index fund",
                currency="rub",
                lot=1,
                api_trade_available_flag=True,
                for_qual_investor_flag=False,
                fixed_commission=q(0, 790000000),
            ),
            SimpleNamespace(
                uid="e2",
                asset_uid="asset-2",
                figi="FE2",
                ticker="LQDT",
                name="Liquidity fund",
                currency="rub",
                lot=1,
                api_trade_available_flag=True,
                for_qual_investor_flag=False,
                fixed_commission=q(0, 300000000),
            ),
        ]

    def get_asset_by(self, asset_uid):
        self.asset_calls.append(asset_uid)
        ext = SimpleNamespace(total_expense=self._TER[asset_uid])
        return SimpleNamespace(asset=SimpleNamespace(security=SimpleNamespace(etf=ext)))


def test_list_etfs_include_fees_attaches_ter():
    adapter = EtfScreenAdapter()
    out = services.list_etfs(adapter, settings=make_settings(), include_fees=True)
    assert sorted(adapter.asset_calls) == ["asset-1", "asset-2"]
    by_uid = {h.uid: h for h in out}
    assert by_uid["e1"].total_expense_pct == Decimal("1.09")
    assert by_uid["e2"].total_expense_pct == Decimal("0.79")


def test_list_etfs_without_fees_skips_asset_calls():
    adapter = EtfScreenAdapter()
    out = services.list_etfs(adapter, settings=make_settings())
    assert adapter.asset_calls == []
    assert all(h.total_expense_pct is None for h in out)


def test_list_etfs_sort_by_ter_auto_enables_fees():
    adapter = EtfScreenAdapter()
    out = services.list_etfs(adapter, settings=make_settings(), sort_by="ter", descending=False)
    assert [h.uid for h in out] == ["e2", "e1"]  # cheapest TER first


class CountingAdapter(FakeAdapter):
    """Counts the per-instrument round trips a screen makes."""

    def __init__(self):
        super().__init__()
        self.calls = {}

    def _bump(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    def get_instrument_by_uid(self, uid):
        self._bump("get_instrument_by_uid")
        return super().get_instrument_by_uid(uid)

    def get_share_by_uid(self, uid):
        self._bump("get_share_by_uid")
        return super().get_share_by_uid(uid)

    def get_order_book(self, uid, depth=10):
        self._bump("get_order_book")
        return super().get_order_book(uid, depth=depth)

    def get_trading_status(self, uid):
        self._bump("get_trading_status")
        return super().get_trading_status(uid)

    def get_last_price(self, uid):
        self._bump("get_last_price")
        return super().get_last_price(uid)

    def get_last_prices(self, uids):
        self._bump("get_last_prices")
        return super().get_last_prices(uids)

    def get_daily_candles(self, uid, from_, to):
        self._bump("get_daily_candles")
        return super().get_daily_candles(uid, from_, to)


def test_screening_analytics_skips_snapshot_and_instrument_lookups():
    # A screen already holds the catalogue row and a batched last price, so the
    # analytics tier must not re-resolve either. Anything else re-fetches data
    # the row has and burns the per-minute InstrumentsService budget.
    adapter = CountingAdapter()
    services.list_shares(
        adapter,
        settings=make_settings(analytics_concurrency=1),
        currency="rub",
        include_analytics=True,
        analytics_limit=5,
    )

    assert adapter.calls.get("get_instrument_by_uid", 0) == 0
    assert adapter.calls.get("get_order_book", 0) == 0
    assert adapter.calls.get("get_trading_status", 0) == 0
    assert adapter.calls.get("get_last_price", 0) == 0
    # Prices come from ONE batched sweep; candles are fetched once per row.
    assert adapter.calls["get_last_prices"] == 1
    assert adapter.calls["get_daily_candles"] == 1


class EtfClassAdapter(FakeAdapter):
    """Catalogue rows as the real board reports them: everything focus=equity."""

    _ROWS = [
        ("e-eq", "asset-eq", "EQMX", "ВИМ – Индекс МосБиржи"),
        ("e-mm", "asset-mm", "LQDT", "ВИМ – Ликвидность"),
        ("e-bond", "asset-bond", "SBGB", "Первая – Фонд Государственные облигации"),
        ("e-gold", "asset-gold", "GOLD", "ВИМ – Фонд Золото"),
        ("e-mixed", "asset-mixed", "TUSD", "Вечный портфель Д"),
        ("e-blocked", "asset-blocked", "TSPX2", "Тинькофф США 500 заблокированные активы"),
        ("e-unknown", "asset-unknown", "AKQU", "Альфа-Капитал Квант"),
    ]

    def __init__(self):
        super().__init__()
        self.asset_calls = []

    def list_instruments(self, instrument_type):
        assert instrument_type == "etf"
        return [
            SimpleNamespace(
                uid=uid,
                asset_uid=asset_uid,
                figi=None,
                ticker=ticker,
                name=name,
                currency="rub",
                lot=1,
                api_trade_available_flag=True,
                for_qual_investor_flag=False,
                focus_type="equity",
                fixed_commission=q(0, 0),
            )
            for uid, asset_uid, ticker, name in self._ROWS
        ]

    def get_asset_by(self, asset_uid):
        self.asset_calls.append(asset_uid)
        ext = SimpleNamespace(total_expense=q(1, 0))
        return SimpleNamespace(asset=SimpleNamespace(security=SimpleNamespace(etf=ext)))


def test_list_etfs_asset_class_drops_mislabelled_equity_funds():
    out = services.list_etfs(EtfClassAdapter(), settings=make_settings(), focus_type="equity")
    # Money-market, bond, gold and mixed funds all claim focus_type='equity';
    # only the real index fund and the unclassifiable one survive.
    assert {h.ticker for h in out} == {"EQMX", "AKQU"}
    by_ticker = {h.ticker: h for h in out}
    assert by_ticker["EQMX"].asset_class == "equity"
    assert by_ticker["AKQU"].asset_class is None  # unknown, so kept


def test_list_etfs_drops_blocked_asset_shells_from_every_screen():
    out = services.list_etfs(EtfClassAdapter(), settings=make_settings())
    assert "TSPX2" not in {h.ticker for h in out}
    assert {h.ticker for h in out} == {"EQMX", "LQDT", "SBGB", "GOLD", "TUSD", "AKQU"}


class FixedIncomeEtfAdapter(FakeAdapter):
    """Funds the catalogue does tag 'fixed_income' — one of them wrongly."""

    _ROWS = [
        ("f-bond", "Первая – Фонд Корпоративные облигации"),
        ("f-mm", "ВИМ – Ликвидность"),
        ("f-gold", "ВИМ – Фонд Золото"),
    ]

    def list_instruments(self, instrument_type):
        return [
            SimpleNamespace(
                uid=uid,
                asset_uid=f"asset-{uid}",
                figi=None,
                ticker=uid,
                name=name,
                currency="rub",
                lot=1,
                api_trade_available_flag=True,
                for_qual_investor_flag=False,
                focus_type="fixed_income",
                fixed_commission=q(0, 0),
            )
            for uid, name in self._ROWS
        ]


def test_list_etfs_fixed_income_accepts_bonds_and_money_market_but_not_gold():
    out = services.list_etfs(FixedIncomeEtfAdapter(), settings=make_settings(), focus_type="fixed_income")
    # Bond and money-market funds both belong in fixed income; a gold fund
    # tagged fixed_income by the catalogue does not.
    assert {h.uid for h in out} == {"f-bond", "f-mm"}


def test_list_etfs_fee_pool_is_not_capped_by_analytics_limit():
    # TER costs one call per asset and nothing else, so a fee ranking covers the
    # whole filtered set rather than the first `analytics_limit` catalogue rows.
    adapter = EtfClassAdapter()
    out = services.list_etfs(adapter, settings=make_settings(), sort_by="ter", descending=False, analytics_limit=1)
    assert len(adapter.asset_calls) == 6  # every non-blocked row, not just one
    assert len(out) == 6


class UndisclosedTerAdapter(EtfScreenAdapter):
    _TER = {"asset-1": q(0, 0), "asset-2": q(0, 790000000)}  # not disclosed / 0.79


def test_undisclosed_ter_sorts_last_instead_of_cheapest():
    # Most MOEX funds report total_expense=0, meaning "not disclosed". Kept as 0
    # it would head a cheapest-first ranking and read as a zero-fee fund.
    adapter = UndisclosedTerAdapter()
    out = services.list_etfs(adapter, settings=make_settings(), sort_by="ter", descending=False)
    assert [h.uid for h in out] == ["e2", "e1"]
    by_uid = {h.uid: h for h in out}
    assert by_uid["e2"].total_expense_pct == Decimal("0.79")
    assert by_uid["e1"].total_expense_pct is None


def test_etf_fees_fetch_each_asset_once():
    # Two share classes of one fund resolve to the same asset uid.
    hits = [
        SimpleNamespace(asset_uid="asset-1", total_expense_pct=None),
        SimpleNamespace(asset_uid="asset-1", total_expense_pct=None),
        SimpleNamespace(asset_uid="asset-2", total_expense_pct=None),
    ]
    adapter = EtfScreenAdapter()
    services._attach_etf_fees(adapter, hits, workers=2)
    assert sorted(adapter.asset_calls) == ["asset-1", "asset-2"]
    assert [h.total_expense_pct for h in hits] == [
        Decimal("1.09"),
        Decimal("1.09"),
        Decimal("0.79"),
    ]


class DeadCandlesEtfAdapter(EtfScreenAdapter):
    def get_daily_candles(self, uid, from_, to):
        raise RuntimeError("candle outage")


def test_decorative_analytics_outage_returns_rows_instead_of_erroring():
    # include_analytics with no analytics-dependent sort or filter asks for extra
    # COLUMNS. Losing them must not fail a screen nothing else depended on.
    out = services.list_etfs(DeadCandlesEtfAdapter(), settings=make_settings(), include_analytics=True)
    assert {h.ticker for h in out} == {"TMOS", "LQDT"}
    assert all(h.historical_return_pct is None for h in out)


def test_analytics_outage_still_errors_when_the_ranking_needs_candles():
    with pytest.raises(TInvestDataUnavailableError, match="after one retry for 2/2"):
        services.list_etfs(DeadCandlesEtfAdapter(), settings=make_settings(), sort_by="volatility")


# ---------------------------------------------------------------------------
# validate_trade_plan (stage 7 — plan-level checks against the personal mandate)
# ---------------------------------------------------------------------------


class PlanAdapter(FakeAdapter):
    """100k portfolio: 45k shares (SBER), 40k bonds (OFZ), 15k cash.

    Target 55/35/10 -> the sensible plan is: sell 10k of equity, buy 15k of bonds.
    """

    def get_portfolio(self, account_id):
        return SimpleNamespace(
            total_amount_portfolio=money(100000),
            total_amount_currencies=money(15000),
            total_amount_shares=money(45000),
            total_amount_bonds=money(40000),
            total_amount_etf=money(0),
            positions=[
                SimpleNamespace(
                    instrument_uid="uid-1",
                    figi="FIGI",
                    ticker="SBER",
                    instrument_type="share",
                    quantity=q(150),
                    quantity_lots=q(15),
                    average_position_price=money(280),
                    current_price=money(300),
                    expected_yield=q(7),
                ),
                SimpleNamespace(
                    instrument_uid="bond-1",
                    figi="FB1",
                    ticker="OFZ1",
                    instrument_type="bond",
                    quantity=q(40),
                    quantity_lots=q(40),
                    average_position_price=money(1000),
                    current_price=money(1000),
                    expected_yield=q(0),
                ),
            ],
        )

    def get_instrument_by_uid(self, uid):
        if uid == "bond-1":
            return SimpleNamespace(
                uid=uid,
                instrument_type="bond",
                figi="FB1",
                ticker="OFZ1",
                name="OFZ 26240",
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
        return super().get_instrument_by_uid(uid)


def plan_settings(tmp_path, **overrides) -> Settings:
    return make_settings(investment_profile_path=str(tmp_path / "profile.json"), **overrides)


def save_plan_profile(settings, **overrides):
    base = {
        "risk_profile": "moderate",
        "horizon": "medium",
        "target_allocation": TargetAllocation(
            risk_profile="moderate",
            horizon="medium",
            horizon_description="1-3 years",
            allocation={"bonds": 55, "equity": 35, "cash": 10},
            asset_class_roles={"bonds": "anchor", "equity": "growth", "cash": "buffer"},
            source="rule_table",
            rationale="test",
        ),
        "max_issuer_weight_pct": Decimal("60"),
        "max_sector_weight_pct": Decimal("60"),
        "saved_at": datetime.now(UTC),
    }
    base.update(overrides)
    profile_store.save_profile(settings.investment_profile_path, InvestmentProfile(**base))


def test_validate_trade_plan_happy_path(tmp_path):
    settings = plan_settings(tmp_path)
    save_plan_profile(settings)
    result = services.validate_trade_plan(
        PlanAdapter(),
        settings,
        steps=[
            TradePlanStepInput(instrument_uid="uid-1", direction="SELL", quantity_lots=4, limit_price=Decimal("250")),
            TradePlanStepInput(instrument_uid="bond-1", direction="BUY", quantity_lots=15, limit_price=Decimal("100")),
        ],
    )
    assert result.all_passed, [c for c in result.plan_checks if not c.passed]
    # Sell 4 lots x 10 units x 250 = 10000; buy 15 bonds at 100% of 1000 nominal = 15000.
    assert result.steps[0].estimated_value == Decimal("10000.00")
    assert result.steps[1].estimated_value == Decimal("15000.00")
    assert result.steps[0].cash_after == Decimal("25000.00")
    assert result.cash_after == Decimal("10000.00")
    assert result.allocation_after_pct == {
        "bonds": Decimal("55.00"),
        "equity": Decimal("35.00"),
        "cash": Decimal("10.00"),
    }
    # Personal mandate resolved from the profile (overrides) + derived cash floor.
    assert result.mandate["max_issuer_weight"] == Decimal("0.6")
    assert result.mandate["min_cash_pct"] == Decimal("5")


def test_validate_trade_plan_buy_before_sell_fails_step_cash(tmp_path):
    settings = plan_settings(tmp_path)
    save_plan_profile(settings)
    result = services.validate_trade_plan(
        PlanAdapter(),
        settings,
        steps=[
            TradePlanStepInput(
                instrument_uid="bond-1", direction="BUY", quantity_lots=20, limit_price=Decimal("100")
            ),  # 20000 > 15000 cash
            TradePlanStepInput(instrument_uid="uid-1", direction="SELL", quantity_lots=4, limit_price=Decimal("250")),
        ],
    )
    assert not result.all_passed
    failed = {c.code for c in result.plan_checks if not c.passed}
    assert "PLAN_STEP_CASH" in failed


def test_validate_trade_plan_sell_more_than_held(tmp_path):
    settings = plan_settings(tmp_path)
    save_plan_profile(settings)
    result = services.validate_trade_plan(
        PlanAdapter(),
        settings,
        steps=[
            TradePlanStepInput(instrument_uid="uid-1", direction="SELL", quantity_lots=20, limit_price=Decimal("250"))
        ],  # 200 units vs 150
    )
    failed = {c.code for c in result.plan_checks if not c.passed}
    assert "PLAN_STEP_POSITION" in failed


def test_validate_trade_plan_requires_profile(tmp_path):
    settings = plan_settings(tmp_path)  # no profile saved
    with pytest.raises(TInvestConfigurationError):
        services.validate_trade_plan(
            PlanAdapter(),
            settings,
            steps=[TradePlanStepInput(instrument_uid="uid-1", direction="SELL", quantity_lots=1)],
        )


def test_validate_trade_plan_market_price_fallback(tmp_path):
    settings = plan_settings(tmp_path)
    save_plan_profile(settings)
    result = services.validate_trade_plan(
        PlanAdapter(),
        settings,
        steps=[TradePlanStepInput(instrument_uid="uid-1", direction="SELL", quantity_lots=1)],
    )
    step = result.steps[0]
    assert step.price_source == "last_price"
    assert step.price_used == Decimal("100")  # fake last price
    assert step.estimated_value == Decimal("1000.00")
