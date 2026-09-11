"""Normalized domain models exposed by the T-Invest MCP server.

All monetary / price fields are typed as :class:`~decimal.Decimal` and serialized
to **strings** (never floats) via :data:`Money` so JSON payloads sent to the LLM
and frontend keep full precision.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, computed_field

from .fx import denomination_currency as _denomination_currency
from .fx import is_fx_linked as _is_fx_linked

# A Decimal that always serializes to a plain decimal string ("100.10", not 100.1).
Money = Annotated[
    Decimal,
    PlainSerializer(lambda v: format(v, "f") if v is not None else None, return_type=str, when_used="json"),
]

Direction = Literal["BUY", "SELL"]
OrderKind = Literal["LIMIT", "MARKET"]
BuyUrgency = Literal["patient", "balanced", "fast"]

# Investment profile axes (advisory stages 3-4).
RiskProfile = Literal["conservative", "moderate", "aggressive"]
InvestmentHorizon = Literal["short", "medium", "long"]
BondRiskLevel = Literal["low", "moderate", "high"]

# Coarse asset classes the target allocation is expressed in; align with the
# class-level breakdown of get_portfolio_analytics so drift can be measured.
ASSET_CLASSES = ("bonds", "equity", "cash")

# Trade plan lifecycle (advisory + execution stages 6-10). EMPTY = nothing
# survived lot sizing; NOT_WORTH_IT = risk checks pass but costs eat the
# rebalance benefit. A confirmed plan is deliberately paused while an order is
# in flight or after a failed step: the next leg is never submitted implicitly.
TradePlanStatus = Literal[
    "READY_FOR_CONFIRMATION",
    "READY_WITH_WARNINGS",
    "RISK_REJECTED",
    "NOT_WORTH_IT",
    "EMPTY",
    "CONFIRMED",
    "EXECUTING",
    "PAUSED",
    "COMPLETED",
    "CANCELLED",
    "EXPIRED",
]
PlanStepStatus = Literal[
    "PENDING",
    "SUBMITTED",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCELLED",
    "REJECTED",
    "RISK_REJECTED",
    "EXPIRED",
    "UNKNOWN_REQUIRES_RECONCILIATION",
    "SKIPPED",
]
CostBenefitVerdict = Literal["WORTH_IT", "NOT_WORTH_IT"]
FillExpectation = Literal["slow", "medium", "immediate_when_session_open", "may_not_cross_spread"]

# How market/order prices are quoted in T-Invest for this instrument type.
PriceQuoteUnit = Literal["currency", "pct_of_nominal"]

_ANALYTICS_NULL_NOTE = (
    "null unless include_analytics=true on list_bonds/list_shares/list_etfs "
    "(or sort_by is return/volatility/drawdown, which auto-enables analytics). "
    "null means analytics was not computed for that call, not missing catalogue data."
)


BOND_PRICE_QUOTE_NOTE = (
    "Bond prices (last_price, bid/ask, limit_price in orders) are quoted as "
    "% of nominal (face value), NOT in rubles. Example: 99.09 = 99.09% of nominal; "
    "approximate money price per bond ≈ nominal × price% / 100 (+ accrued coupon on purchase)."
)


def price_quote_unit_for(instrument_type: str) -> PriceQuoteUnit:
    return "pct_of_nominal" if (instrument_type or "").lower() == "bond" else "currency"


# Analyst consensus recommendation (T-Invest GetForecastBy / GetConsensusForecasts).
Recommendation = Literal["buy", "hold", "sell"]

ProposalStatus = Literal[
    "DRAFT",
    "RISK_REJECTED",
    "READY_FOR_CONFIRMATION",
    "CONFIRMED",
    "SUBMITTING",
    "SUBMITTED",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCELLED",
    "REJECTED",
    "EXPIRED",
    "UNKNOWN_REQUIRES_RECONCILIATION",
]

# Statuses from which a proposal can never be (re-)executed.
TERMINAL_STATUSES = frozenset({"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "RISK_REJECTED"})

# Proposals with an order in flight at the broker (playbook phase 3 — execution).
EXECUTION_STATUSES = frozenset({"SUBMITTING", "SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN_REQUIRES_RECONCILIATION"})


class BrokerAccount(BaseModel):
    id: str
    name: str | None = None
    status: str
    account_type: str
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    research_access_level: str | None = Field(
        default=None,
        description=(
            "Permission reported for the research/read token used by get_accounts. "
            "This field does not describe execution capability; use execution_available."
        ),
    )
    planning_available: bool = Field(
        default=True,
        description=(
            "Trade-plan creation is a read-only calculation and remains available even when "
            "access_level is ACCOUNT_ACCESS_LEVEL_READ_ONLY."
        ),
    )
    execution_available: bool = Field(
        default=False,
        description=(
            "True only when this account can be executed through the separately configured "
            "trade token and the current mode permits execution."
        ),
    )
    execution_access_level: str | None = Field(
        default=None,
        description="Permission independently reported by the trade token for this same account.",
    )
    execution_block_reason: str | None = Field(
        default=None,
        description="Deterministic reason execution_available is false; null when execution is available.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "750c962a-d318-4cb3-b662-7a66aa17c6ee",
                    "name": "ai-treasury-debug",
                    "status": "ACCOUNT_STATUS_OPEN",
                    "account_type": "ACCOUNT_TYPE_TINKOFF",
                    "opened_at": "2026-06-14T18:54:50Z",
                    "closed_at": None,
                    "research_access_level": "ACCOUNT_ACCESS_LEVEL_READ_ONLY",
                    "planning_available": True,
                    "execution_available": True,
                    "execution_access_level": "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS",
                    "execution_block_reason": None,
                }
            ]
        }
    )


class PortfolioPosition(BaseModel):
    instrument_uid: str
    figi: str | None = None
    ticker: str | None = None
    name: str | None = None
    instrument_type: str
    quantity: Money | None = None
    quantity_lots: Money | None = None
    average_price: Money | None = None
    current_price: Money | None = None
    current_value: Money | None = Field(
        default=None,
        description=(
            "Position value in the PORTFOLIO currency (rubles) — converted at fx_rate_rub "
            "when the broker prices the position in another currency, so it can be weighed "
            "against total_value."
        ),
    )
    # NOTE: this is the CURRENT unrealized result of the position vs. average
    # price — NOT a forecast of future yield.
    expected_yield_absolute: Money | None = None
    expected_yield_percent: Money | None = None
    currency: str | None = Field(
        default=None,
        description="Currency the broker prices this position in (average_price / current_price).",
    )
    fx_rate_rub: Money | None = Field(
        default=None,
        description="Rubles per one unit of `currency`, when a conversion was applied (else null).",
    )
    blocked: Money | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "instrument_uid": "e6123145-9665-43e0-8413-cd61b8aa9b13",
                    "figi": "BBG004730N88",
                    "ticker": "SBER",
                    "name": "Сбер Банк",
                    "instrument_type": "share",
                    "quantity": "10",
                    "quantity_lots": "10",
                    "average_price": "300.00",
                    "current_price": "323.16",
                    "current_value": "3231.60",
                    "expected_yield_absolute": "231.60",
                    "expected_yield_percent": "7.72",
                    "currency": "rub",
                    "blocked": "0",
                }
            ]
        }
    )


class PortfolioSummary(BaseModel):
    """Compact, LLM-safe portfolio view — no tokens / ids."""

    mode: str
    currency: str = "rub"
    total_value: Money
    cash: Money
    asset_allocation: dict[str, Money]
    positions: list[PortfolioPosition] = Field(default_factory=list)
    concentration: dict[str, Money] = Field(default_factory=dict)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "mode": "sandbox",
                    "currency": "rub",
                    "total_value": "100000",
                    "cash": "97000",
                    "asset_allocation": {"shares": "0.03", "bonds": "0", "funds": "0", "cash": "0.97"},
                    "positions": [
                        {
                            "instrument_uid": "e6123145-9665-43e0-8413-cd61b8aa9b13",
                            "figi": "BBG004730N88",
                            "ticker": "SBER",
                            "name": "Сбер Банк",
                            "instrument_type": "share",
                            "quantity": "10",
                            "quantity_lots": "10",
                            "average_price": "300.00",
                            "current_price": "323.16",
                            "current_value": "3231.60",
                            "expected_yield_absolute": "231.60",
                            "expected_yield_percent": "7.72",
                            "currency": "rub",
                            "blocked": "0",
                        }
                    ],
                    "concentration": {"largest_position_weight": "0.03"},
                }
            ]
        }
    )


class OperationTradeItem(BaseModel):
    trade_id: str | None = None
    date_time: datetime | None = None
    quantity: int | None = None
    price: Money | None = None
    currency: str | None = None


class BrokerOperation(BaseModel):
    id: str
    date: datetime
    type: str = Field(description="Normalized type: buy, sell, coupon, dividend, broker_fee, tax, …")
    type_raw: str = Field(description="SDK enum name, e.g. OPERATION_TYPE_BUY.")
    state: str
    name: str | None = None
    description: str | None = None
    instrument_uid: str | None = None
    ticker: str | None = None
    figi: str | None = None
    instrument_type: str | None = None
    payment: Money | None = Field(default=None, description="Net cash flow of the operation.")
    price: Money | None = None
    commission: Money | None = None
    quantity: Money | None = None
    accrued_int: Money | None = None
    currency: str | None = None
    trades: list[OperationTradeItem] = Field(default_factory=list)


class OperationsTotals(BaseModel):
    commissions: Money = Field(description="Sum of broker/service fees and per-trade commissions.")
    dividends: Money
    coupons: Money
    taxes: Money = Field(description="Withheld taxes (absolute sum, typically negative payments).")
    trades_buy: Money
    trades_sell: Money


class OperationsPage(BaseModel):
    mode: str
    from_date: date
    to_date: date
    items: list[BrokerOperation] = Field(default_factory=list)
    totals: OperationsTotals
    has_next: bool = False
    next_cursor: str | None = None
    notes: list[str] = Field(default_factory=list)


class TopPositionWeight(BaseModel):
    instrument_uid: str
    ticker: str | None = None
    name: str | None = None
    instrument_type: str
    weight: Money
    value: Money


class DriftItem(BaseModel):
    """Gap between current and target share of one asset class (stage 4)."""

    asset_class: str = Field(description="Target asset class: bonds | equity | cash.")
    current_pct: Money = Field(description="Current share, % of portfolio value.")
    target_pct: Money = Field(description="Target share from the saved profile, %.")
    deviation_pct: Money = Field(description="current_pct - target_pct (negative = underweight).")
    current_value: Money
    target_value: Money
    amount_to_trade: Money = Field(
        description="target_value - current_value: positive = buy this much, negative = sell."
    )
    action: Literal["buy", "sell", "hold"] = Field(
        description="'hold' when |deviation_pct| is below the rebalance threshold."
    )


class MandateViolation(BaseModel):
    """A concentration limit breach (issuer / sector), computed by code."""

    kind: Literal["issuer_weight", "sector_weight"]
    subject: str = Field(description="Issuer name or sector.")
    weight: Money = Field(description="Current weight, fraction of portfolio (0.25 = 25%).")
    limit: Money = Field(description="Mandate limit, fraction of portfolio.")
    excess_value: Money = Field(description="Approximate value to shed to get back under the limit.")
    message: str


class AllocationDrift(BaseModel):
    """Gap analysis: saved target allocation vs live portfolio. All numbers computed by code."""

    risk_profile: RiskProfile = Field(description="Risk profile of the saved investment profile.")
    horizon: str
    rebalance_threshold_pct: Money = Field(
        description="Deviations under this many percentage points are treated as 'hold'."
    )
    rebalance_needed: bool
    max_abs_deviation_pct: Money
    items: list[DriftItem]
    unmapped_value: Money = Field(
        description="Portfolio value in classes that do not map onto bonds/equity/cash (e.g. 'other')."
    )
    mandate_violations: list[MandateViolation] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "risk_profile": "conservative",
                    "horizon": "medium",
                    "rebalance_threshold_pct": "5",
                    "rebalance_needed": True,
                    "max_abs_deviation_pct": "30.00",
                    "items": [
                        {
                            "asset_class": "bonds",
                            "current_pct": "40.00",
                            "target_pct": "70",
                            "deviation_pct": "-30.00",
                            "current_value": "120000",
                            "target_value": "210000",
                            "amount_to_trade": "90000",
                            "action": "buy",
                        }
                    ],
                    "unmapped_value": "0",
                    "mandate_violations": [
                        {
                            "kind": "issuer_weight",
                            "subject": "Компания X",
                            "weight": "0.25",
                            "limit": "0.15",
                            "excess_value": "30000",
                            "message": "Issuer 'Компания X' is 25.00% of portfolio, mandate limit is 15.00%.",
                        }
                    ],
                    "notes": [],
                }
            ]
        }
    )


class PortfolioAnalytics(BaseModel):
    mode: str
    currency: str = "rub"
    total_value: Money
    by_class: dict[str, Money] = Field(default_factory=dict)
    by_sector: dict[str, Money] = Field(default_factory=dict)
    by_currency: dict[str, Money] = Field(default_factory=dict)
    by_issuer: dict[str, Money] = Field(default_factory=dict)
    weighted_yield_pct: Money | None = Field(
        default=None,
        description="Portfolio-weighted mix of bond YTM and share dividend yield (estimate).",
    )
    bond_portfolio_duration_years: Money | None = Field(
        default=None,
        description="Value-weighted Macaulay duration of bond holdings (years).",
    )
    concentration: dict[str, Money] = Field(default_factory=dict)
    top_positions: list[TopPositionWeight] = Field(default_factory=list)
    drift: AllocationDrift | None = Field(
        default=None,
        description=(
            "Gap analysis vs the saved investment profile (save_investment_profile). "
            "null when no profile is saved — complete the allocation stage first."
        ),
    )
    notes: list[str] = Field(default_factory=list)


class InvestmentInstrument(BaseModel):
    uid: str
    figi: str | None = None
    ticker: str
    name: str
    instrument_type: str
    currency: str = Field(
        description=(
            "SETTLEMENT currency — what you pay with. For a CNY-linked bond listed on a "
            "ruble board this is 'rub' even though the bond itself pays yuan; check "
            "nominal_currency before treating any figure as rubles."
        ),
    )
    nominal_currency: str | None = Field(
        default=None,
        description=(
            "DENOMINATION currency of the bond nominal, coupons and redemption. When it "
            "differs from `currency` the instrument is FX-linked: prices are a percent of "
            "a foreign face value, so price-derived money is in THIS currency, and its "
            "yields are yields in THIS currency."
        ),
    )
    lot: int
    min_price_increment: Money | None = None
    price_quote_unit: PriceQuoteUnit = Field(
        description=(
            "Unit for market and limit prices: 'currency' = rubles (or instrument currency) per share/ETF unit; "
            "'pct_of_nominal' = percent of bond face value (NOT rubles). "
            "For bonds, pass limit_price to create_order_proposal in this same unit."
        ),
    )

    api_trade_available: bool = False
    buy_available: bool = False
    sell_available: bool = False
    short_enabled: bool = False
    qualified_investor_only: bool = False

    exchange: str | None = None
    class_code: str | None = None

    nominal: Money | None = None
    initial_nominal: Money | None = None
    maturity_date: date | None = None
    call_date: date | None = None
    coupon_rate: Money | None = None
    aci_value: Money | None = Field(
        default=None,
        description="Accrued coupon income (НКД) per bond, in currency; paid on buy, received on sell.",
    )
    liquidity_flag: bool | None = None
    for_iis_flag: bool | None = None
    issue_kind: str | None = None
    issue_size: int | None = None
    released_date: date | None = None

    isin: str | None = None
    sector: str | None = None
    country_of_risk: str | None = None
    trading_status: str | None = None

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            "Currency the cash flows are really in (nominal_currency when known, else "
            "currency). Yields and price-derived money figures are in THIS currency."
        ),
    )
    @property
    def denomination_currency(self) -> str | None:
        return _denomination_currency(self.currency, self.nominal_currency)

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            "True when the cash flows are NOT in rubles. Holding it is a currency bet on "
            "top of the credit/rate bet, whatever the settlement currency says."
        ),
    )
    @property
    def fx_linked(self) -> bool:
        return _is_fx_linked(self.denomination_currency)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "uid": "e6123145-9665-43e0-8413-cd61b8aa9b13",
                    "figi": "BBG004730N88",
                    "ticker": "SBER",
                    "name": "Сбер Банк",
                    "instrument_type": "share",
                    "currency": "rub",
                    "lot": 1,
                    "min_price_increment": "0.01",
                    "price_quote_unit": "currency",
                    "api_trade_available": True,
                    "buy_available": True,
                    "sell_available": True,
                    "short_enabled": False,
                    "qualified_investor_only": False,
                    "exchange": "MOEX_PLUS",
                    "class_code": "TQBR",
                    "nominal": None,
                    "maturity_date": None,
                    "coupon_rate": None,
                    "isin": "RU0009029540",
                    "sector": "financial",
                    "country_of_risk": "RU",
                    "trading_status": "SECURITY_TRADING_STATUS_NORMAL_TRADING",
                }
            ]
        }
    )


class InstrumentSearchHit(BaseModel):
    """Lean catalogue row returned by the text search (``search_instruments``)."""

    uid: str
    figi: str | None = None
    ticker: str
    name: str
    instrument_type: str
    currency: str | None = None
    lot: int | None = None
    api_trade_available: bool = False
    qualified_investor_only: bool = False

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "uid": "e6123145-9665-43e0-8413-cd61b8aa9b13",
                    "figi": "BBG004730N88",
                    "ticker": "SBER",
                    "name": "Сбер Банк",
                    "instrument_type": "share",
                    "currency": "rub",
                    "lot": 1,
                    "api_trade_available": True,
                    "qualified_investor_only": False,
                }
            ]
        }
    )


class _ScreenHit(BaseModel):
    """Fields shared by every per-type screener row.

    Cost tiers:
      - Always present: static reference data (free, from the catalogue).
      - ``last_price``: one batched market-data call for the returned page.
      - Analytics fields (return / volatility / drawdown and the per-type yield):
        only filled when ``include_analytics=True`` — one extra call per
        instrument, so the enriched set is capped. They are estimates from market
        data, NOT guarantees of future return.
    """

    uid: str = Field(description="Instrument UID — pass this to other tools (analytics, forecast, snapshot, order).")
    asset_uid: str | None = Field(
        default=None, description="UID of the underlying ASSET; the join key for analyst forecasts and fundamentals."
    )
    figi: str | None = Field(default=None, description="FIGI identifier of the instrument.")
    ticker: str = Field(description="Exchange ticker, e.g. 'SBER'.")
    name: str = Field(description="Human-readable instrument name.")
    currency: str | None = Field(default=None, description="Trading/settlement currency, e.g. 'rub'.")
    lot: int | None = Field(
        default=None, description="Lot size — the minimum tradeable quantity (orders are placed in whole lots)."
    )
    api_trade_available: bool = Field(default=False, description="Whether the instrument can be traded via the API.")
    qualified_investor_only: bool = Field(
        default=False, description="True if the instrument is restricted to qualified investors."
    )
    sector: str | None = Field(default=None, description="Industry sector, e.g. 'financial', 'energy', 'it'.")
    country_of_risk: str | None = Field(default=None, description="Country of risk (ISO code), e.g. 'RU'.")

    last_price: Money | None = Field(
        default=None,
        description=(
            "Latest trade price (one batched market-data call for the returned page). "
            "Unit: see price_quote_unit on BondScreenHit / ShareScreenHit / EtfScreenHit "
            "(bonds = % of nominal, shares/ETFs = currency per unit)."
        ),
    )

    # Analytics tier — only filled with include_analytics=True (computed from ~1y of daily candles).
    historical_return_pct: Money | None = Field(
        default=None,
        description=(
            f"Price return over ~1 year, in percent (12.34 = +12.34%). {_ANALYTICS_NULL_NOTE} Estimate, not a forecast."
        ),
    )
    volatility_annual_pct: Money | None = Field(
        default=None,
        description=(f"Annualized volatility of daily returns, in percent. Higher = riskier. {_ANALYTICS_NULL_NOTE}"),
    )
    max_drawdown_pct: Money | None = Field(
        default=None,
        description=(f"Largest peak-to-trough drop over the period, in percent (risk proxy). {_ANALYTICS_NULL_NOTE}"),
    )
    avg_daily_volume_lots: Money | None = Field(
        default=None,
        description=(f"Average daily traded volume over ~30 recent sessions, in LOTS. {_ANALYTICS_NULL_NOTE}"),
    )
    avg_daily_turnover: Money | None = Field(
        default=None,
        description=(
            "Average daily traded value over ~30 recent sessions, in the instrument's own "
            "denomination currency — for an FX-linked bond that is yuan, not rubles. "
            "Compare liquidity across instruments with avg_daily_turnover_rub instead. "
            f"{_ANALYTICS_NULL_NOTE}"
        ),
    )
    avg_daily_turnover_rub: Money | None = Field(
        default=None,
        description=(
            "avg_daily_turnover converted to rubles at the current exchange rate — the "
            "cross-instrument liquidity yardstick, and what min_avg_daily_turnover filters "
            "on. Equals avg_daily_turnover for ruble instruments; null when the FX rate "
            "could not be fetched."
        ),
    )


class BondScreenHit(_ScreenHit):
    """A bond screener row (``list_bonds``)."""

    instrument_type: Literal["bond"] = "bond"
    price_quote_unit: Literal["pct_of_nominal"] = Field(
        default="pct_of_nominal",
        description=BOND_PRICE_QUOTE_NOTE,
    )
    last_price: Money | None = Field(
        default=None,
        description="Latest trade price as % of nominal (NOT rubles). E.g. 98.0 = 98% of face value.",
    )

    isin: str | None = Field(default=None, description="ISIN code of the bond.")
    nominal_currency: str | None = Field(
        default=None,
        description=(
            "Currency of the nominal, coupons and redemption. When it differs from "
            "`currency` (CNY-linked issues settled in rubles are the usual case) the row "
            "is FX-linked: `ytm_pct` and `current_yield_pct` below are yields in THIS "
            "currency and are NOT comparable to ruble yields."
        ),
    )
    nominal: Money | None = Field(
        default=None,
        description="Current face value per bond, in nominal_currency (decreases over time for amortized bonds).",
    )
    initial_nominal: Money | None = Field(
        default=None, description="Face value at issue (differs from nominal after amortization)."
    )
    maturity_date: date | None = Field(default=None, description="Redemption date. Null for perpetual/undated bonds.")
    call_date: date | None = Field(
        default=None,
        description="Next call/offer (оферта) date, if any — the bond may be redeemed/put here before maturity.",
    )
    risk_level: str | None = Field(
        default=None, description="Issuer risk tier per T-Invest: 'low', 'moderate' or 'high'."
    )
    coupons_per_year: int | None = Field(default=None, description="Number of coupon payments per year.")
    floating_coupon: bool | None = Field(
        default=None, description="True if the coupon rate floats (future coupons/yield are estimates)."
    )
    amortization: bool | None = Field(
        default=None, description="True if the principal is repaid in installments before maturity."
    )
    perpetual: bool | None = Field(default=None, description="True if the bond has no maturity date.")
    subordinated: bool | None = Field(
        default=None, description="True if subordinated (lower repayment priority, higher risk)."
    )
    liquidity_flag: bool | None = Field(
        default=None, description="True if the bond is flagged as liquid (easier to trade)."
    )
    for_iis_flag: bool | None = Field(default=None, description="True if available on an IIS (ИИС) account.")
    issue_kind: str | None = Field(default=None, description="Issue form, e.g. 'documentary' / 'non_documentary'.")
    issue_size: int | None = Field(
        default=None, description="Number of bonds placed in the issue (outstanding volume)."
    )
    aci_value: Money | None = Field(
        default=None, description="Accrued coupon income (НКД) per bond, added to the price on purchase."
    )

    current_yield_pct: Money | None = Field(
        default=None,
        description="Current yield = next-12m coupons / clean money price, in percent (with include_analytics), denominated in yield_currency. NOT yield-to-maturity.",
    )
    ytm_pct: Money | None = Field(
        default=None,
        description="Yield to maturity (or to offer) — annualized effective yield from the coupon schedule and dirty price, in percent (with include_analytics), denominated in yield_currency.",
    )
    ytm_to_offer: bool | None = Field(
        default=None,
        description="True if ytm_pct is computed to the call/offer date instead of final maturity (yield-to-worst).",
    )
    duration_years: Money | None = Field(
        default=None,
        description="Macaulay duration in years — interest-rate sensitivity; match it to the investment horizon (with include_analytics).",
    )

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            "Currency current_yield_pct / ytm_pct are expressed in. Rank yields only "
            "within one yield_currency: a 'cny' 8.7% and a 'rub' 15.7% differ by the "
            "expected CNY/RUB move, not by 7 points of income."
        ),
    )
    @property
    def yield_currency(self) -> str | None:
        return _denomination_currency(self.currency, self.nominal_currency)

    @computed_field(  # type: ignore[prop-decorator]
        description="True when the bond's cash flows are not in rubles (a currency bet).",
    )
    @property
    def fx_linked(self) -> bool:
        return _is_fx_linked(self.yield_currency)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "uid": "33672905-be3c-4a02-a1c3-4be155814bb5",
                    "figi": "BBG00QXGFHS6",
                    "ticker": "RU000A0ZYX28",
                    "name": "Совкомбанк 1В02",
                    "instrument_type": "bond",
                    "currency": "rub",
                    "lot": 1,
                    "api_trade_available": True,
                    "qualified_investor_only": False,
                    "sector": "financial",
                    "country_of_risk": "RU",
                    "last_price": "98.0",
                    "price_quote_unit": "pct_of_nominal",
                    "isin": "RU000A0ZYX28",
                    "nominal_currency": "rub",
                    "nominal": "100",
                    "initial_nominal": "100",
                    "maturity_date": "2027-09-16",
                    "call_date": None,
                    "risk_level": "low",
                    "coupons_per_year": 2,
                    "floating_coupon": False,
                    "amortization": False,
                    "perpetual": False,
                    "subordinated": False,
                    "liquidity_flag": True,
                    "for_iis_flag": True,
                    "issue_kind": "documentary",
                    "issue_size": 1000000,
                    "aci_value": "1.20",
                    "current_yield_pct": "12.45",
                    "ytm_pct": "13.10",
                    "ytm_to_offer": False,
                }
            ]
        }
    )


class ShareScreenHit(_ScreenHit):
    """A share screener row (``list_shares``)."""

    instrument_type: Literal["share"] = "share"
    price_quote_unit: Literal["currency"] = Field(
        default="currency",
        description="Share prices are quoted in the instrument currency per share (e.g. rubles).",
    )

    share_type: str | None = Field(default=None, description="Share class: 'common', 'preferred', etc.")
    pays_dividends: bool | None = Field(default=None, description="Whether the company is flagged as paying dividends.")

    dividend_yield_pct: Money | None = Field(
        default=None,
        description="Dividend yield from the LATEST payout / price, in percent (only with include_analytics). For the vendor's TTM figure see dividend_yield_fund_pct.",
    )

    # Analyst consensus tier — only with include_forecast=True (third-party signal, NOT a guarantee).
    consensus_recommendation: Recommendation | None = Field(
        default=None, description="Analyst consensus rating: 'buy', 'hold' or 'sell'."
    )
    consensus_target_price: Money | None = Field(
        default=None, description="Consensus 12-month target price (same currency as the instrument)."
    )
    target_upside_pct: Money | None = Field(
        default=None,
        description="Implied upside = (consensus target / last_price - 1) * 100, in percent. Negative = downside.",
    )
    analysts_buy: int | None = Field(default=None, description="Number of analysts recommending BUY.")
    analysts_hold: int | None = Field(default=None, description="Number of analysts recommending HOLD.")
    analysts_sell: int | None = Field(
        default=None,
        description="Number of analysts recommending SELL. (sort_by='recommendation' ranks on buy - sell.)",
    )

    # Fundamentals tier — only with include_fundamentals=True. Sortable subset of the
    # company's ratios; see get_instrument_fundamentals for the full set. Ratios are
    # multiples (4.2 = 4.2x); *_pct fields are already in percent.
    pe_ratio: Money | None = Field(
        default=None, description="Price / Earnings (TTM), a multiple. Lower can mean cheaper. Negative = loss-making."
    )
    price_to_sales: Money | None = Field(default=None, description="Price / Sales (TTM), a multiple.")
    price_to_book: Money | None = Field(default=None, description="Price / Book value, a multiple.")
    ev_to_ebitda: Money | None = Field(
        default=None, description="Enterprise Value / EBITDA, a multiple. Lower can mean cheaper."
    )
    roe_pct: Money | None = Field(
        default=None, description="Return on Equity, in percent. Higher = more profitable use of equity."
    )
    roa_pct: Money | None = Field(default=None, description="Return on Assets, in percent.")
    roic_pct: Money | None = Field(default=None, description="Return on Invested Capital, in percent.")
    net_margin_pct: Money | None = Field(
        default=None, description="Net profit margin (net income / revenue), in percent."
    )
    eps_ttm: Money | None = Field(default=None, description="Earnings per share (TTM), in the instrument currency.")
    market_cap: Money | None = Field(default=None, description="Market capitalization, in the instrument currency.")
    debt_to_equity: Money | None = Field(
        default=None, description="Total debt / equity, a multiple. Lower = less leveraged."
    )
    net_debt_to_ebitda: Money | None = Field(
        default=None, description="Net debt / EBITDA, a multiple. Lower = less leveraged."
    )
    dividend_yield_fund_pct: Money | None = Field(
        default=None, description="Dividend yield (TTM) from the fundamentals vendor, in percent."
    )
    revenue_growth_5y_pct: Money | None = Field(
        default=None, description="Average annual revenue growth over 5 years, in percent."
    )
    beta: Money | None = Field(
        default=None, description="Beta vs. the market. >1 = more volatile than the market, <1 = less."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "uid": "e6123145-9665-43e0-8413-cd61b8aa9b13",
                    "figi": "BBG004730N88",
                    "ticker": "SBER",
                    "name": "Сбер Банк",
                    "instrument_type": "share",
                    "currency": "rub",
                    "lot": 1,
                    "api_trade_available": True,
                    "qualified_investor_only": False,
                    "sector": "financial",
                    "country_of_risk": "RU",
                    "last_price": "323.16",
                    "share_type": "common",
                    "pays_dividends": True,
                    "dividend_yield_pct": "10.40",
                    "historical_return_pct": "18.20",
                    "volatility_annual_pct": "27.30",
                    "max_drawdown_pct": "15.10",
                    "consensus_recommendation": "buy",
                    "consensus_target_price": "385.00",
                    "target_upside_pct": "19.13",
                    "analysts_buy": 12,
                    "analysts_hold": 4,
                    "analysts_sell": 1,
                    "pe_ratio": "4.20",
                    "price_to_sales": "1.80",
                    "price_to_book": "0.95",
                    "ev_to_ebitda": "3.10",
                    "roe_pct": "24.50",
                    "roa_pct": "3.10",
                    "roic_pct": "18.40",
                    "net_margin_pct": "32.10",
                    "eps_ttm": "75.40",
                    "market_cap": "7250000000000",
                    "debt_to_equity": "1.20",
                    "net_debt_to_ebitda": "0.80",
                    "dividend_yield_fund_pct": "10.40",
                    "revenue_growth_5y_pct": "14.20",
                    "beta": "1.10",
                }
            ]
        }
    )


class EtfScreenHit(_ScreenHit):
    """An ETF / fund screener row (``list_etfs``)."""

    instrument_type: Literal["etf"] = "etf"
    price_quote_unit: Literal["currency"] = Field(
        default="currency",
        description="ETF prices are quoted in the instrument currency per fund unit (e.g. rubles).",
    )

    isin: str | None = Field(default=None, description="ISIN code of the fund share.")
    focus_type: str | None = Field(
        default=None,
        description="Fund focus AS REPORTED BY THE CATALOGUE. Unreliable as an asset-class signal — money-market, bond and gold funds are all tagged 'equity' on the MOEX board. Prefer asset_class.",
    )
    asset_class: str | None = Field(
        default=None,
        description="Asset class inferred from the fund name: 'equity', 'bonds', 'money_market', 'commodity', 'mixed'. Null when it could not be determined — that means unknown, not 'other'. Screens drop funds whose inferred class contradicts a requested focus_type, and always drop blocked-asset shells.",
    )
    rebalancing_freq: str | None = Field(
        default=None, description="How often the portfolio is rebalanced, e.g. 'quarterly', 'semi_annual'."
    )
    num_shares: Money | None = Field(
        default=None, description="Number of fund shares outstanding (may be 0 if not provided by the issuer)."
    )
    fixed_commission_pct: Money | None = Field(
        default=None,
        description="Fixed management commission, in percent per year. 0 may mean not disclosed in the catalogue.",
    )
    total_expense_pct: Money | None = Field(
        default=None,
        description="Total expense ratio (TER), percent per year — all-in annual cost. Null unless include_fees=true or sort_by='ter' (one get_asset_by call per fund); 0 may mean not disclosed by the issuer.",
    )
    liquidity_flag: bool | None = Field(
        default=None, description="True if the fund is flagged as liquid (easier to trade)."
    )
    for_iis_flag: bool | None = Field(default=None, description="True if available on an IIS (ИИС) account.")
    released_date: date | None = Field(default=None, description="Fund inception / first release date.")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "uid": "9654b6fe-2479-4c2b-9f24-4c8a0e1d59cb",
                    "figi": "BBG333333333",
                    "ticker": "TMOS",
                    "name": "Т-Капитал Индекс МосБиржи",
                    "instrument_type": "etf",
                    "currency": "rub",
                    "lot": 1,
                    "api_trade_available": True,
                    "qualified_investor_only": False,
                    "sector": "",
                    "country_of_risk": "RU",
                    "last_price": "7.85",
                    "isin": "RU000A1001234",
                    "focus_type": "equity",
                    "rebalancing_freq": "quarterly",
                    "num_shares": "1000000",
                    "fixed_commission_pct": "0.79",
                    "total_expense_pct": "0.99",
                    "liquidity_flag": True,
                    "for_iis_flag": True,
                    "released_date": "2020-03-15",
                    "historical_return_pct": "21.40",
                    "volatility_annual_pct": "19.80",
                    "max_drawdown_pct": "12.60",
                }
            ]
        }
    )


class EtfDetails(BaseModel):
    """Extended ETF / BPIF metadata from ``GetAssetBy`` (AssetEtf).

    The catalogue row (``list_etfs``) carries the basics; this tool adds the
    strategy description, benchmark/index, full fee breakdown, tracking error and
    other fields shown in the T-Invest app. Portfolio holdings and NAV premium
    are NOT available via the API (see ``notes``).
    """

    instrument_uid: str = Field(description="ETF instrument UID.")
    asset_uid: str | None = Field(default=None, description="Underlying asset UID (join key for asset-level data).")
    ticker: str = Field(description="Exchange ticker.")
    name: str = Field(description="Fund name.")
    currency: str | None = Field(default=None, description="Trading/settlement currency.")
    isin: str | None = Field(default=None, description="ISIN of the fund share.")

    focus_type: str | None = Field(
        default=None, description="Fund focus: equity, fixed_income, mixed_allocation, alternative_investment, etc."
    )
    rebalancing_freq: str | None = Field(
        default=None, description="Portfolio rebalancing frequency, e.g. quarterly, semi_annual."
    )
    rebalancing_flag: bool | None = Field(
        default=None, description="True if the fund actively rebalances its portfolio."
    )
    num_shares: Money | None = Field(default=None, description="Outstanding fund shares (may be 0 if not provided).")
    released_date: date | None = Field(default=None, description="Fund inception / first release date.")
    liquidity_flag: bool | None = Field(default=None, description="True if flagged as liquid.")
    for_iis_flag: bool | None = Field(default=None, description="True if available on IIS (ИИС).")

    fixed_commission_pct: Money | None = Field(
        default=None, description="Fixed management commission, percent per year."
    )
    total_expense_pct: Money | None = Field(
        default=None, description="Total expense ratio (TER), percent per year — all-in annual cost."
    )
    expense_commission_pct: Money | None = Field(
        default=None, description="Additional expense commission component, percent per year."
    )
    hurdle_rate_pct: Money | None = Field(default=None, description="Performance fee hurdle rate, percent.")
    performance_fee_pct: Money | None = Field(default=None, description="Performance fee (success fee), percent.")
    payment_type: str | None = Field(default=None, description="Fee payment type as reported by the issuer.")

    primary_index: str | None = Field(default=None, description="Benchmark / primary index name the fund tracks.")
    primary_index_description: str | None = Field(
        default=None, description="Long description of the benchmark or investment strategy."
    )
    primary_index_company: str | None = Field(default=None, description="Index provider / company.")
    tracking_error_pct: Money | None = Field(default=None, description="Primary index tracking error, percent.")

    management_type: str | None = Field(default=None, description="Management style: 'passive' (index) or 'active'.")
    leveraged_flag: bool | None = Field(default=None, description="True if the fund uses leverage.")
    div_yield_flag: bool | None = Field(
        default=None, description="True if the fund is flagged as paying distributions/dividends."
    )
    ucits_flag: bool | None = Field(
        default=None, description="True if UCITS-compliant (mostly relevant for foreign funds)."
    )

    description: str | None = Field(default=None, description="Issuer's fund description / strategy summary.")
    buy_premium_pct: Money | None = Field(default=None, description="Creation (buy) premium over NAV, percent.")
    sell_discount_pct: Money | None = Field(default=None, description="Redemption (sell) discount from NAV, percent.")
    inav_code: str | None = Field(default=None, description="iNAV (indicative NAV) code for intraday fair value.")
    tax_rate: str | None = Field(default=None, description="Tax treatment notes from the issuer.")
    rebalancing_plan: str | None = Field(default=None, description="Rebalancing plan description.")
    issue_kind: str | None = Field(default=None, description="Issue form.")

    notes: list[str] = Field(default_factory=list, description="Caveats (missing data, API limits).")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "instrument_uid": "555dcd42-2c14-43d5-ba93-8a4a42160638",
                    "asset_uid": "a20a8e51-dd4e-4ba3-beb0-a9bf4dd0a983",
                    "ticker": "TOFZ@",
                    "name": "Т-Капитал ОФЗ",
                    "currency": "rub",
                    "isin": "RU000A109876",
                    "focus_type": "fixed_income",
                    "rebalancing_freq": "semi_annual",
                    "rebalancing_flag": True,
                    "released_date": "2024-11-07",
                    "liquidity_flag": True,
                    "for_iis_flag": True,
                    "fixed_commission_pct": "1.50",
                    "total_expense_pct": "1.599",
                    "expense_commission_pct": "0.099",
                    "primary_index_description": "Adaptive OFZ strategy based on key rate outlook.",
                    "management_type": "passive",
                    "div_yield_flag": False,
                    "description": "Exchange-traded fund adapting to key rate changes via OFZ ladder.",
                    "inav_code": "TOFZA",
                    "notes": ["Portfolio holdings and NAV premium are not available via the T-Invest API."],
                }
            ]
        }
    )


class InstrumentAnalytics(BaseModel):
    """Yield + risk signals for one instrument.

    Percent fields are decimals where 12.34 means 12.34%. Computed values are
    approximations from market data, NOT guarantees of future return.
    """

    instrument_uid: str
    ticker: str
    name: str
    instrument_type: str
    currency: str = Field(description="Settlement currency — what you pay with.")
    nominal_currency: str | None = Field(
        default=None,
        description=(
            "Currency of a bond's nominal, coupons and redemption. Differs from `currency` "
            "for FX-linked issues (a yuan bond listed on a ruble board), and then it — not "
            "`currency` — is what every yield and price-derived money figure is in."
        ),
    )
    fx_rate_rub: Money | None = Field(
        default=None,
        description=(
            "Rubles per one unit of the denomination currency at the time of this call "
            "(null for ruble instruments, or when the FX board could not be read)."
        ),
    )
    current_price: Money | None = None

    # Historical (from ~1y of daily candles)
    history_days: int | None = None
    historical_return_pct: Money | None = None
    volatility_annual_pct: Money | None = Field(
        default=None,
        description=(
            "Annualized volatility of the QUOTED price. For an FX-linked bond the quote is "
            "in its own currency, so this excludes the CNY/RUB swings a ruble investor "
            "actually lives through — the total ruble risk is higher."
        ),
    )
    max_drawdown_pct: Money | None = None

    # Liquidity (from ~30 recent daily candles)
    avg_daily_volume_lots: Money | None = None
    avg_daily_turnover: Money | None = Field(
        default=None,
        description="Average daily traded value in the instrument's denomination currency (yuan for a CNY-linked bond).",
    )
    avg_daily_turnover_rub: Money | None = Field(
        default=None,
        description=(
            "avg_daily_turnover in rubles — the figure to compare against other instruments "
            "and against ruble liquidity thresholds. Null when the FX rate is unavailable."
        ),
    )

    # Bonds
    risk_level: str | None = None
    macaulay_duration_years: Money | None = Field(
        default=None,
        description="Macaulay duration in years — match to the investment horizon.",
    )
    nominal: Money | None = None  # face value, in nominal_currency
    initial_nominal: Money | None = None
    maturity_date: date | None = None
    call_date: date | None = None
    coupons_per_year: int | None = None
    current_yield_pct: Money | None = None  # annual coupon income / price, in yield_currency
    ytm_pct: Money | None = Field(
        default=None,
        description=(
            "Yield to maturity / offer, in percent — denominated in yield_currency. Rank it "
            "only against yields in the SAME currency; against a ruble alternative the gap "
            "is a forecast of the exchange rate, not extra income."
        ),
    )
    ytm_to_offer: bool | None = None  # True if ytm is to the call/offer date
    liquidity_flag: bool | None = None
    for_iis_flag: bool | None = None
    issue_kind: str | None = None
    issue_size: int | None = None
    next_coupon_date: date | None = None
    next_coupon_value: Money | None = None

    # Shares
    dividend_yield_pct: Money | None = None
    last_dividend_value: Money | None = None
    last_dividend_date: date | None = None

    unavailable_components: list[str] = Field(
        default_factory=list,
        description=(
            "Market-data components that failed to load (for example candles or "
            "bond_coupons). Screeners retry degraded analytics once and surface an "
            "explicit error when these failures make filtering or ranking unreliable."
        ),
    )
    notes: list[str] = Field(default_factory=list)

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            "Currency every percent yield above is denominated in. Comparing yields across "
            "different yield_currency values is comparing an income figure with a currency "
            "forecast — say so explicitly instead of ranking them."
        ),
    )
    @property
    def yield_currency(self) -> str | None:
        return _denomination_currency(self.currency, self.nominal_currency)

    @computed_field(  # type: ignore[prop-decorator]
        description="True when cash flows are not in rubles — holding it is also a currency bet.",
    )
    @property
    def fx_linked(self) -> bool:
        return _is_fx_linked(self.yield_currency)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "instrument_uid": "33672905-be3c-4a02-a1c3-4be155814bb5",
                    "ticker": "RU000A0ZYX28",
                    "name": "Совкомбанк 1В02",
                    "instrument_type": "bond",
                    "currency": "rub",
                    "nominal_currency": "rub",
                    "current_price": "98.0",
                    "history_days": 227,
                    "historical_return_pct": "-1.58",
                    "volatility_annual_pct": "8.75",
                    "max_drawdown_pct": "5.90",
                    "risk_level": "low",
                    "nominal": "100",
                    "maturity_date": "2027-09-16",
                    "coupons_per_year": 2,
                    "current_yield_pct": "12.45",
                    "next_coupon_date": "2026-09-16",
                    "next_coupon_value": "6.10",
                    "dividend_yield_pct": None,
                    "last_dividend_value": None,
                    "last_dividend_date": None,
                    "notes": [
                        "Computed figures are estimates from market data, not guaranteed future returns.",
                        "Bond current yield = sum of next-12m coupons / clean money price (not YTM).",
                    ],
                }
            ]
        }
    )


class BondCouponItem(BaseModel):
    """One scheduled coupon payment (T-Invest GetBondCoupons)."""

    coupon_date: date | None = Field(default=None, description="Date the coupon is paid.")
    coupon_number: int | None = Field(default=None, description="Sequential coupon number.")
    pay_one_bond: Money | None = Field(default=None, description="Coupon amount paid per one bond, in currency.")
    coupon_type: str | None = Field(
        default=None, description="Coupon type: 'constant', 'floating', 'discount', 'fix', 'variable', etc."
    )
    coupon_period_days: int | None = Field(default=None, description="Length of the coupon period, in days.")
    fix_date: date | None = Field(
        default=None, description="Record date — own the bond by this date to receive the coupon."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "coupon_date": "2026-09-16",
                    "coupon_number": 12,
                    "pay_one_bond": "6.10",
                    "coupon_type": "fix",
                    "coupon_period_days": 182,
                    "fix_date": "2026-09-13",
                }
            ]
        }
    )


class BondEventItem(BaseModel):
    """One bond lifecycle event (T-Invest GetBondEvents): coupon / call / maturity."""

    event_type: str | None = Field(
        default=None, description="Event type: 'coupon', 'call' (offer), 'maturity' or 'conversion'."
    )
    event_date: date | None = Field(default=None, description="Date of the event.")
    pay_one_bond: Money | None = Field(default=None, description="Payment per one bond for this event, in currency.")
    fix_date: date | None = Field(default=None, description="Record date for the event, if applicable.")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "event_type": "call",
                    "event_date": "2027-03-15",
                    "pay_one_bond": "100.00",
                    "fix_date": "2027-03-12",
                }
            ]
        }
    )


class BondSchedule(BaseModel):
    """Full coupon schedule + lifecycle events for one bond.

    Use it to see every upcoming coupon, the call/offer (оферта) and the
    redemption — the detail behind the screener's summary fields. Floating-coupon
    bonds expose only the already-fixed future coupons (see ``notes``).
    """

    instrument_uid: str = Field(description="Bond instrument UID.")
    ticker: str = Field(description="Exchange ticker.")
    name: str = Field(description="Bond name.")
    currency: str | None = Field(default=None, description="Payment currency.")
    nominal: Money | None = Field(default=None, description="Current face value per bond.")
    maturity_date: date | None = Field(default=None, description="Final redemption date (null if perpetual/undated).")
    call_date: date | None = Field(default=None, description="Next call/offer (оферта) date, if any.")
    coupons: list[BondCouponItem] = Field(default_factory=list, description="Upcoming coupons in date order.")
    events: list[BondEventItem] = Field(
        default_factory=list, description="Upcoming lifecycle events (coupon/call/maturity) in date order."
    )
    notes: list[str] = Field(default_factory=list, description="Caveats (e.g. floating coupons are estimates).")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "instrument_uid": "33672905-be3c-4a02-a1c3-4be155814bb5",
                    "ticker": "RU000A0ZYX28",
                    "name": "Совкомбанк 1В02",
                    "currency": "rub",
                    "nominal": "100",
                    "maturity_date": "2027-09-16",
                    "call_date": None,
                    "coupons": [
                        {
                            "coupon_date": "2026-09-16",
                            "coupon_number": 12,
                            "pay_one_bond": "6.10",
                            "coupon_type": "fix",
                            "coupon_period_days": 182,
                            "fix_date": "2026-09-13",
                        }
                    ],
                    "events": [
                        {
                            "event_type": "maturity",
                            "event_date": "2027-09-16",
                            "pay_one_bond": "100.00",
                            "fix_date": None,
                        }
                    ],
                    "notes": ["Only already-fixed coupons are shown for floating-rate bonds."],
                }
            ]
        }
    )


class AnalystTarget(BaseModel):
    """One analyst / broker's target for an instrument (a row of GetForecastBy)."""

    company: str | None = Field(default=None, description="Analyst / broker that issued the target.")
    recommendation: Recommendation | None = Field(
        default=None, description="This analyst's rating: 'buy', 'hold' or 'sell'."
    )
    target_price: Money | None = Field(default=None, description="This analyst's target price.")
    current_price: Money | None = Field(default=None, description="Price at the time the target was issued.")
    upside_pct: Money | None = Field(
        default=None, description="Implied upside vs. current price, in percent (negative = downside)."
    )
    currency: str | None = Field(default=None, description="Currency of the prices.")
    recommendation_date: datetime | None = Field(default=None, description="When this target was published.")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "company": "Sber CIB",
                    "recommendation": "buy",
                    "target_price": "390.00",
                    "current_price": "323.16",
                    "upside_pct": "20.68",
                    "currency": "rub",
                    "recommendation_date": "2026-05-20T00:00:00Z",
                }
            ]
        }
    )


class AnalystForecast(BaseModel):
    """Aggregated analyst view for one instrument (T-Invest GetForecastBy).

    This is a THIRD-PARTY signal (consensus of brokers/analysts), not a market
    fact and not a guarantee — surfaced in ``notes``. ``upside_pct`` is the
    consensus target vs. the current price.
    """

    instrument_uid: str = Field(description="Instrument UID this forecast is for.")
    ticker: str = Field(description="Exchange ticker.")
    name: str = Field(description="Instrument name.")
    currency: str = Field(default="rub", description="Currency of the prices below.")

    recommendation: Recommendation | None = Field(
        default=None, description="Consensus rating across analysts: 'buy', 'hold' or 'sell'."
    )
    current_price: Money | None = Field(default=None, description="Current price used as the upside baseline.")
    consensus_target_price: Money | None = Field(default=None, description="Consensus (average) 12-month target price.")
    min_target_price: Money | None = Field(default=None, description="Lowest analyst target in the set.")
    max_target_price: Money | None = Field(default=None, description="Highest analyst target in the set.")
    upside_pct: Money | None = Field(
        default=None, description="Consensus upside = (consensus target / current price - 1) * 100, in percent."
    )

    analysts_buy: int | None = Field(default=None, description="Number of analysts recommending BUY.")
    analysts_hold: int | None = Field(default=None, description="Number of analysts recommending HOLD.")
    analysts_sell: int | None = Field(default=None, description="Number of analysts recommending SELL.")
    analyst_count: int | None = Field(default=None, description="Total number of analyst targets in the set.")

    targets: list[AnalystTarget] = Field(
        default_factory=list, description="Per-analyst targets that make up the consensus."
    )
    notes: list[str] = Field(default_factory=list, description="Caveats (e.g. third-party opinion, not a guarantee).")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "instrument_uid": "e6123145-9665-43e0-8413-cd61b8aa9b13",
                    "ticker": "SBER",
                    "name": "Сбер Банк",
                    "currency": "rub",
                    "recommendation": "buy",
                    "current_price": "323.16",
                    "consensus_target_price": "385.00",
                    "min_target_price": "350.00",
                    "max_target_price": "420.00",
                    "upside_pct": "19.13",
                    "analysts_buy": 12,
                    "analysts_hold": 4,
                    "analysts_sell": 1,
                    "analyst_count": 17,
                    "targets": [
                        {
                            "company": "Sber CIB",
                            "recommendation": "buy",
                            "target_price": "390.00",
                            "current_price": "323.16",
                            "upside_pct": "20.68",
                            "currency": "rub",
                            "recommendation_date": "2026-05-20T00:00:00Z",
                        }
                    ],
                    "notes": [
                        "Analyst consensus is a third-party opinion, not a guarantee of future price.",
                    ],
                }
            ]
        }
    )


class InstrumentFundamentals(BaseModel):
    """Company financial statement metrics & valuation ratios (GetAssetFundamentals).

    The same numbers the T-Investments app shows on the "Показатели" tab. Values
    come straight from the data vendor (TTM = trailing-twelve-months, MRQ = most
    recent quarter, FY = fiscal year). Ratios are plain numbers (P/E = 4.2 means
    4.2x); percentage fields (roe_pct, margins, dividend_yield_pct, growth rates)
    are already in percent. Fields the vendor does not provide stay ``None``.
    """

    instrument_uid: str = Field(description="Instrument UID this report is for.")
    asset_uid: str | None = Field(
        default=None, description="Underlying asset UID (the key fundamentals are stored under)."
    )
    ticker: str = Field(description="Exchange ticker.")
    name: str = Field(description="Company / instrument name.")
    currency: str | None = Field(default=None, description="Currency of the monetary figures below.")

    # Size / market
    market_cap: Money | None = Field(default=None, description="Market capitalization (price × shares), in currency.")
    shares_outstanding: Money | None = Field(default=None, description="Number of shares outstanding.")
    free_float_pct: Money | None = Field(
        default=None, description="Share of stock freely traded on the market, in percent."
    )
    beta: Money | None = Field(
        default=None, description="Beta vs. the market. >1 = more volatile than the market, <1 = less."
    )
    high_52w: Money | None = Field(default=None, description="Highest price over the last 52 weeks.")
    low_52w: Money | None = Field(default=None, description="Lowest price over the last 52 weeks.")

    # Valuation ratios (multiples; 4.2 = 4.2x)
    pe_ratio: Money | None = Field(default=None, description="Price / Earnings (TTM). Negative = loss-making.")
    price_to_sales: Money | None = Field(default=None, description="Price / Sales (TTM).")
    price_to_book: Money | None = Field(default=None, description="Price / Book value.")
    price_to_fcf: Money | None = Field(default=None, description="Price / Free Cash Flow (TTM).")
    ev_to_ebitda: Money | None = Field(default=None, description="Enterprise Value / EBITDA (MRQ).")
    ev_to_sales: Money | None = Field(default=None, description="Enterprise Value / Sales.")
    enterprise_value: Money | None = Field(
        default=None, description="Enterprise value = market cap + net debt (MRQ), in currency."
    )

    # Profitability / returns (percent)
    roe_pct: Money | None = Field(default=None, description="Return on Equity, in percent.")
    roa_pct: Money | None = Field(default=None, description="Return on Assets, in percent.")
    roic_pct: Money | None = Field(default=None, description="Return on Invested Capital, in percent.")
    net_margin_pct: Money | None = Field(
        default=None, description="Net profit margin (net income / revenue), in percent."
    )

    # Per-share / income (absolute, in currency)
    eps_ttm: Money | None = Field(default=None, description="Earnings per share (TTM), in currency.")
    diluted_eps_ttm: Money | None = Field(default=None, description="Diluted earnings per share (TTM), in currency.")
    revenue_ttm: Money | None = Field(default=None, description="Revenue / sales (TTM), in currency.")
    ebitda_ttm: Money | None = Field(default=None, description="EBITDA (TTM), in currency.")
    net_income_ttm: Money | None = Field(default=None, description="Net income (TTM), in currency.")
    free_cash_flow_ttm: Money | None = Field(default=None, description="Free cash flow (TTM), in currency.")

    # Leverage / liquidity
    total_debt: Money | None = Field(default=None, description="Total debt (MRQ), in currency.")
    debt_to_equity: Money | None = Field(
        default=None, description="Total debt / equity, a multiple. Lower = less leveraged."
    )
    net_debt_to_ebitda: Money | None = Field(
        default=None, description="Net debt / EBITDA, a multiple. Lower = less leveraged."
    )
    current_ratio: Money | None = Field(
        default=None, description="Current assets / current liabilities (MRQ). >1 = can cover short-term obligations."
    )

    # Dividends
    dividend_yield_pct: Money | None = Field(default=None, description="Dividend yield (TTM), in percent.")
    dividend_rate_ttm: Money | None = Field(
        default=None, description="Total dividends paid per share (TTM), in currency."
    )
    dividend_payout_ratio_pct: Money | None = Field(
        default=None, description="Share of earnings paid out as dividends (FY), in percent."
    )
    dividends_per_share: Money | None = Field(default=None, description="Dividends per share, in currency.")
    ex_dividend_date: date | None = Field(default=None, description="Next/last ex-dividend date.")

    # Growth (percent, average annual)
    revenue_growth_5y_pct: Money | None = Field(
        default=None, description="Average annual revenue growth over 5 years, in percent."
    )
    revenue_growth_3y_pct: Money | None = Field(
        default=None, description="Average annual revenue growth over 3 years, in percent."
    )
    revenue_growth_1y_pct: Money | None = Field(
        default=None, description="Revenue growth over the last year, in percent."
    )

    notes: list[str] = Field(
        default_factory=list, description="Caveats about the data (e.g. third-party source, missing fields)."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "instrument_uid": "e6123145-9665-43e0-8413-cd61b8aa9b13",
                    "asset_uid": "40d89385-a03a-4659-bf4e-d3ecba011782",
                    "ticker": "SBER",
                    "name": "Сбер Банк",
                    "currency": "rub",
                    "market_cap": "7250000000000",
                    "shares_outstanding": "22586948000",
                    "free_float_pct": "48.0",
                    "beta": "1.10",
                    "high_52w": "330.00",
                    "low_52w": "230.00",
                    "pe_ratio": "4.20",
                    "price_to_sales": "1.80",
                    "price_to_book": "0.95",
                    "price_to_fcf": None,
                    "ev_to_ebitda": "3.10",
                    "ev_to_sales": "2.10",
                    "enterprise_value": "8000000000000",
                    "roe_pct": "24.50",
                    "roa_pct": "3.10",
                    "roic_pct": "18.40",
                    "net_margin_pct": "32.10",
                    "eps_ttm": "75.40",
                    "diluted_eps_ttm": "75.10",
                    "revenue_ttm": "4100000000000",
                    "ebitda_ttm": None,
                    "net_income_ttm": "1600000000000",
                    "free_cash_flow_ttm": None,
                    "total_debt": None,
                    "debt_to_equity": None,
                    "net_debt_to_ebitda": None,
                    "current_ratio": None,
                    "dividend_yield_pct": "10.40",
                    "dividend_rate_ttm": "33.30",
                    "dividend_payout_ratio_pct": "50.0",
                    "dividends_per_share": "33.30",
                    "ex_dividend_date": "2026-07-11",
                    "revenue_growth_5y_pct": "14.2",
                    "revenue_growth_3y_pct": "11.0",
                    "revenue_growth_1y_pct": "8.5",
                    "notes": ["Fundamentals are provided by a third-party data vendor; some fields may be empty."],
                }
            ]
        }
    )


class PriceHintTier(BaseModel):
    limit_price: Money
    label: str = Field(description="join_bid | mid_spread | cross_ask")
    fill_expectation: FillExpectation
    note: str


class SpreadInfo(BaseModel):
    bid: Money | None = None
    ask: Money | None = None
    width_pct: Money | None = Field(
        default=None,
        description="Bid-ask spread as % of ask.",
    )


class LiquidityInfo(BaseModel):
    """How easily the instrument trades — check BEFORE proposing it as a candidate.

    A high screener yield is worthless if the position cannot be exited without
    losing several percent on the spread.
    """

    avg_daily_volume_lots: Money | None = Field(
        default=None,
        description="Average daily traded volume over the sampled sessions, in LOTS.",
    )
    avg_daily_turnover: Money | None = Field(
        default=None,
        description="Average daily traded value in instrument currency (money) — comparable across instruments.",
    )
    spread_pct: Money | None = Field(
        default=None,
        description="Current bid-ask spread as % of ask (round-trip cost proxy). >1% = expensive to exit.",
    )
    days_sampled: int | None = Field(
        default=None,
        description="Number of daily candles the volume averages are computed from.",
    )


class RiskBounds(BaseModel):
    min_buy_limit_price: Money | None = Field(
        default=None,
        description="Lowest BUY limit still within risk deviation and exchange corridor.",
    )
    max_buy_limit_price: Money | None = Field(
        default=None,
        description="Highest BUY limit still within risk deviation and exchange corridor.",
    )
    max_deviation_pct: Money


class SessionInfo(BaseModel):
    trading_status: str | None = None
    api_trade_available: bool = False
    market_data_fresh: bool = False
    tradeable_now: bool | None = Field(
        default=None,
        description="MOEX session clock: is the exchange matching orders right now? "
        "None when the session check is disabled. False during clearing pauses / closed hours "
        "even while trading_status still reports NORMAL_TRADING.",
    )
    session_phase: str | None = Field(
        default=None,
        description="MORNING | MAIN | EVENING | PAUSE | CLOSED | WEEKEND.",
    )
    closing_soon: bool | None = Field(
        default=None,
        description="Tradeable now, but a clearing pause / close begins within the warning buffer "
        "— a fresh limit may not fill before matching stops.",
    )
    resumes_at: datetime | None = Field(
        default=None,
        description="When continuous matching next resumes (set when not tradeable now).",
    )
    warnings: list[str] = Field(default_factory=list)


class BuyPriceHints(BaseModel):
    """Algorithmic BUY limit suggestions for create_order_proposal urgency tiers."""

    spread: SpreadInfo | None = None
    hints: dict[str, PriceHintTier] = Field(
        description="patient (join bid), balanced (mid spread), fast (cross ask).",
    )
    risk_bounds: RiskBounds
    session: SessionInfo
    recommended_urgency: BuyUrgency = "balanced"


class PriceVsHints(BaseModel):
    """How the chosen limit compares to algorithmic tiers (create_order_proposal)."""

    your_limit: Money
    recommended_patient: Money | None = None
    recommended_balanced: Money | None = None
    recommended_fast: Money | None = None
    crosses_spread: bool = False
    urgency_used: BuyUrgency
    user_supplied_price: bool = False
    warning: str | None = None


class MarketSnapshot(BaseModel):
    instrument_uid: str
    instrument_type: str | None = Field(
        default=None,
        description="Instrument type: 'bond', 'share', 'etf', etc. Determines price_quote_unit.",
    )
    price_quote_unit: PriceQuoteUnit | None = Field(
        default=None,
        description=(
            "Unit for last_price, best_bid, best_ask, limit_up, limit_down: "
            "'pct_of_nominal' for bonds (% of face value, NOT rubles); "
            "'currency' for shares/ETFs (rubles or instrument currency per unit)."
        ),
    )
    last_price: Money | None = Field(
        default=None,
        description="Latest trade price in the unit given by price_quote_unit.",
    )
    last_price_time: datetime | None = None
    best_bid: Money | None = None
    best_ask: Money | None = None
    limit_up: Money | None = None
    limit_down: Money | None = None
    trading_status: str | None = None
    api_trade_available: bool = False
    age_seconds: float | None = None
    is_fresh: bool = False
    buy_price_hints: BuyPriceHints | None = Field(
        default=None,
        description="Algorithmic patient/balanced/fast BUY limits for create_order_proposal urgency.",
    )
    liquidity: LiquidityInfo | None = Field(
        default=None,
        description="Avg daily volume/turnover (~30 sessions) and current spread — check before proposing a candidate.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "instrument_uid": "d2f51222-5567-42ac-945f-6d4c234d8e6f",
                    "instrument_type": "bond",
                    "price_quote_unit": "pct_of_nominal",
                    "last_price": "99.09",
                    "last_price_time": "2026-06-27T13:15:48.968235Z",
                    "best_bid": "99.03",
                    "best_ask": "99.18",
                    "limit_up": "102.07",
                    "limit_down": "96.13",
                    "trading_status": "SECURITY_TRADING_STATUS_NORMAL_TRADING",
                    "api_trade_available": True,
                    "age_seconds": 1.2,
                    "is_fresh": True,
                    "buy_price_hints": {
                        "spread": {"bid": "99.03", "ask": "99.18", "width_pct": "0.15"},
                        "hints": {
                            "patient": {
                                "limit_price": "99.03",
                                "label": "join_bid",
                                "fill_expectation": "slow",
                                "note": "Join the best bid queue.",
                            },
                            "balanced": {
                                "limit_price": "99.10",
                                "label": "mid_spread",
                                "fill_expectation": "medium",
                                "note": "Mid spread compromise.",
                            },
                            "fast": {
                                "limit_price": "99.18",
                                "label": "cross_ask",
                                "fill_expectation": "immediate_when_session_open",
                                "note": "Cross best ask for faster fill in session.",
                            },
                        },
                        "risk_bounds": {
                            "min_buy_limit_price": "98.10",
                            "max_buy_limit_price": "100.08",
                            "max_deviation_pct": "1.0",
                        },
                        "session": {"market_data_fresh": True, "api_trade_available": True, "warnings": []},
                        "recommended_urgency": "balanced",
                    },
                }
            ]
        }
    )


class RiskCheck(BaseModel):
    code: str
    passed: bool
    message: str
    severity: Literal["error", "warning", "info"] = Field(
        default="error",
        description=(
            "'error' + passed=false blocks execution; 'warning' passes but must be "
            "shown to the user (e.g. LDV_WARNING); 'info' is context (e.g. TAX_IMPACT)."
        ),
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "code": "MAX_ORDER_VALUE",
                    "passed": True,
                    "message": "Order value 98.5 is within the per-order limit 1500",
                    "severity": "error",
                }
            ]
        }
    )


class SellTaxImpact(BaseModel):
    """Estimated НДФЛ effect of a SELL preview. Computed by code, an ESTIMATE only."""

    estimated_gain: Money | None = Field(
        default=None,
        description="Estimated realized P&L of the sold part (proportional to unrealized result).",
    )
    estimated_tax: Money | None = Field(
        default=None,
        description="Estimated НДФЛ withheld on the gain (0 when selling at a loss).",
    )
    tax_rate_pct: Money = Field(description="Tax rate used for the estimate, %.")
    notes: list[str] = Field(default_factory=list)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "estimated_gain": "231.60",
                    "estimated_tax": "30.11",
                    "tax_rate_pct": "13",
                    "notes": ["Estimate from current unrealized result; the broker's FIFO lot accounting may differ."],
                }
            ]
        }
    )


class OrderPreview(BaseModel):
    """Returned by ``create_order_proposal``."""

    proposal_id: str
    status: ProposalStatus
    expires_at: datetime
    mode: str
    instrument: dict[str, str | None]
    order: dict[str, str | None]
    portfolio_impact: dict[str, str | None]
    risk_checks: list[RiskCheck]
    all_passed: bool
    rationale: str | None = None
    price_selection: PriceVsHints | None = Field(
        default=None,
        description="Chosen limit vs algorithmic tiers; present when urgency or hints were used.",
    )
    tax_impact: SellTaxImpact | None = Field(
        default=None,
        description="SELL only: estimated НДФЛ on the realized gain (null for BUY).",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "proposal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "status": "READY_FOR_CONFIRMATION",
                    "expires_at": "2026-06-20T08:15:00Z",
                    "mode": "sandbox",
                    "instrument": {
                        "uid": "33672905-be3c-4a02-a1c3-4be155814bb5",
                        "ticker": "RU000A0ZYX28",
                        "name": "Совкомбанк 1В02",
                        "type": "bond",
                    },
                    "order": {
                        "direction": "BUY",
                        "type": "LIMIT",
                        "quantity_lots": "1",
                        "lot_size": "1",
                        "quantity_units": "1",
                        "limit_price": "98.0",
                        "price_quote_unit": "pct_of_nominal",
                        "estimated_total": "98.5",
                        "commission": "0.05",
                        "currency": "rub",
                    },
                    "portfolio_impact": {
                        "position_weight_before": "0.00",
                        "position_weight_after": "0.00098",
                        "cash_before": "100000.00",
                        "cash_after_estimated": "99901.50",
                    },
                    "risk_checks": [
                        {"code": "MAX_ORDER_VALUE", "passed": True, "message": "Order value is within the limit"},
                        {"code": "SUFFICIENT_CASH", "passed": True, "message": "Enough cash available"},
                    ],
                    "all_passed": True,
                    "rationale": "Conservative short bond for the stability sleeve",
                }
            ]
        }
    )


class ExecutingOrderSummary(BaseModel):
    """One row from ``list_executing_orders`` — a proposal at the broker execution stage."""

    proposal_id: str
    status: ProposalStatus
    broker_order_id: str | None = None
    idempotency_key: str | None = None
    created_at: datetime
    instrument: dict[str, str | None]
    order: dict[str, str | None]
    lots_requested: int | None = None
    lots_executed: int | None = None
    executed_price: Money | None = None
    total_amount: Money | None = None
    commission: Money | None = None
    message: str | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "proposal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "status": "SUBMITTED",
                    "broker_order_id": "a1b2c3d4-0000-1111-2222-333344445555",
                    "idempotency_key": "9e8d7c6b-5a4f-3e2d-1c0b-abcdef012345",
                    "created_at": "2026-06-20T08:10:00Z",
                    "instrument": {
                        "uid": "e6123145-9665-43e0-8413-cd61b8aa9b13",
                        "ticker": "SBER",
                        "name": "Сбер Банк",
                        "type": "share",
                    },
                    "order": {
                        "direction": "BUY",
                        "type": "LIMIT",
                        "quantity_lots": "1",
                        "limit_price": "300.00",
                        "currency": "rub",
                    },
                    "lots_requested": 1,
                    "lots_executed": 0,
                    "executed_price": None,
                    "total_amount": None,
                    "commission": None,
                    "message": None,
                }
            ]
        }
    )


class OrderResult(BaseModel):
    """Returned by ``post_order`` / ``get_order_state``."""

    proposal_id: str
    status: ProposalStatus
    broker_order_id: str | None = None
    idempotency_key: str | None = None
    lots_requested: int | None = None
    lots_executed: int | None = None
    executed_price: Money | None = None
    total_amount: Money | None = None
    commission: Money | None = None
    direction: Direction | None = None
    message: str | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "proposal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "status": "FILLED",
                    "broker_order_id": "a1b2c3d4-0000-1111-2222-333344445555",
                    "idempotency_key": "9e8d7c6b-5a4f-3e2d-1c0b-abcdef012345",
                    "lots_requested": 1,
                    "lots_executed": 1,
                    "executed_price": "98.0",
                    "total_amount": "98.5",
                    "commission": "0.05",
                    "direction": "BUY",
                    "message": None,
                }
            ]
        }
    )


# --- investment profile & target allocation (advisory stage 3) --------------


class TargetAllocation(BaseModel):
    """Deterministic asset-class mix produced by the rule table (never by the LLM)."""

    risk_profile: RiskProfile
    horizon: InvestmentHorizon
    horizon_description: str
    allocation: dict[str, int] = Field(description="asset_class -> target %, sums to 100")
    asset_class_roles: dict[str, str]
    source: Literal["rule_table", "custom"] = "rule_table"
    rationale: str

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "risk_profile": "conservative",
                    "horizon": "medium",
                    "horizon_description": "1-3 years",
                    "allocation": {"bonds": 70, "equity": 20, "cash": 10},
                    "asset_class_roles": {
                        "bonds": "anchor: predictable coupon income, low drawdown",
                        "equity": "growth: shares and equity ETFs, higher volatility",
                        "cash": "liquidity buffer: instantly available, no market risk",
                    },
                    "source": "rule_table",
                    "rationale": "Deterministic rule for a conservative investor with a medium horizon…",
                }
            ]
        }
    )


class InvestmentProfile(BaseModel):
    """User-confirmed profile persisted by ``save_investment_profile``."""

    risk_profile: RiskProfile
    horizon: InvestmentHorizon
    target_allocation: TargetAllocation
    excluded_sectors: list[str] = Field(
        default_factory=list,
        description="Sectors the user does not want to hold; mandate-aware screeners drop them.",
    )
    max_bond_risk_level: BondRiskLevel | None = Field(
        default=None,
        description="Mandate cap on bond issuer risk tier. null = derived from risk_profile.",
    )
    min_cash_pct: Money | None = Field(
        default=None,
        description=(
            "MANDATE: cash floor for plan-level checks, % of portfolio. "
            "null = derived: max(0, target cash % - rebalance_threshold_pct)."
        ),
    )
    max_issuer_weight_pct: Money | None = Field(
        default=None,
        description="MANDATE: per-issuer weight cap, % of portfolio. null = server default.",
    )
    max_sector_weight_pct: Money | None = Field(
        default=None,
        description="MANDATE: per-sector weight cap, % of portfolio. null = server default.",
    )
    allow_fx_linked: bool | None = Field(
        default=None,
        description=(
            "MANDATE: whether the investor knowingly wants non-ruble exposure (CNY-linked "
            "bonds and the like — their yields are foreign-currency yields, so holding them "
            "is a bet on the exchange rate). null / false = not opted in, and plans that "
            "buy such instruments raise a CURRENCY_EXPOSURE warning to be confirmed."
        ),
    )
    max_fx_exposure_pct: Money | None = Field(
        default=None,
        description=(
            "MANDATE: cap on the share of the portfolio denominated in foreign currency, "
            "% of portfolio. Only meaningful with allow_fx_linked=true; null = server default."
        ),
    )
    notes: str | None = None
    saved_at: datetime

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "risk_profile": "conservative",
                    "horizon": "medium",
                    "target_allocation": {
                        "risk_profile": "conservative",
                        "horizon": "medium",
                        "horizon_description": "1-3 years",
                        "allocation": {"bonds": 70, "equity": 20, "cash": 10},
                        "asset_class_roles": {"bonds": "anchor", "equity": "growth", "cash": "buffer"},
                        "source": "rule_table",
                        "rationale": "…",
                    },
                    "notes": "Goal: down payment in ~2 years.",
                    "saved_at": "2026-07-11T10:00:00Z",
                }
            ]
        }
    )


# --- trade plan (advisory stage 6) -------------------------------------------


class TradePlanItemInput(BaseModel):
    """One trade intent from the agent: what to sell/buy and how much.

    Size the trade with EITHER ``quantity_lots`` (exact lots) OR ``amount``
    (money to deploy/raise; the server converts it to whole lots at the current
    market price — including НКД for bonds).
    """

    instrument_uid: str = Field(description="Instrument UID to trade.")
    action: Direction = Field(description="BUY or SELL.")
    quantity_lots: int | None = Field(
        default=None,
        ge=1,
        description="Exact number of lots. Mutually exclusive with amount.",
    )
    amount: str | None = Field(
        default=None,
        description=(
            "Money to deploy (BUY) or raise (SELL), decimal string in the instrument "
            "currency (rubles — NOT % of nominal for bonds). Converted to whole lots "
            "at the current market price; the remainder is left uninvested."
        ),
        examples=["50000"],
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"instrument_uid": "33672905-be3c-4a02-a1c3-4be155814bb5", "action": "BUY", "amount": "50000"},
                {"instrument_uid": "e6123145-9665-43e0-8413-cd61b8aa9b13", "action": "SELL", "quantity_lots": 10},
            ]
        }
    )


class SaleTaxEstimate(BaseModel):
    """Estimated НДФЛ for one planned sale, from FIFO tax lots (with ЛДВ)."""

    method: Literal["fifo", "average_price", "mixed", "none"] = Field(
        description=(
            "Cost-basis source: 'fifo' = tax lots rebuilt from the operation history; "
            "'average_price' = fallback to the position's average price (no history); "
            "'mixed' = history covered only part of the sold quantity; 'none' = no basis "
            "at all (tax overestimated from zero basis)."
        ),
    )
    cost_basis: Money = Field(description="Total clean cost basis of the sold units, in currency.")
    gross_proceeds: Money = Field(description="Clean sale proceeds (price × units, without НКД), in currency.")
    gross_gain: Money = Field(description="gross_proceeds - cost_basis (can be negative).")
    exempt_gain_ldv: Money = Field(
        description="Gain on lots held ≥ the ЛДВ threshold (long-term ownership exemption) — not taxed.",
    )
    taxable_gain: Money = Field(description="Gain on non-exempt lots; tax applies only when positive.")
    tax: Money = Field(description="Estimated НДФЛ = tax_rate_pct × max(taxable_gain, 0).")
    tax_rate_pct: Money
    notes: list[str] = Field(default_factory=list)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "method": "fifo",
                    "cost_basis": "2000.00",
                    "gross_proceeds": "4995.00",
                    "gross_gain": "2995.00",
                    "exempt_gain_ldv": "0",
                    "taxable_gain": "2995.00",
                    "tax": "389.35",
                    "tax_rate_pct": "13",
                    "notes": ["ЛДВ: lots held ≥ 3 years are exempt (3 млн ₽/year cap is not modeled)."],
                }
            ]
        }
    )


class PlannedTrade(BaseModel):
    """One sequenced leg of a trade plan (SELL legs come before BUY legs)."""

    sequence: int = Field(description="1-based execution order; SELL legs first (proceeds fund the buys).")
    action: Direction
    instrument_uid: str
    ticker: str | None = None
    name: str | None = None
    instrument_type: str
    currency: str = Field(description="Settlement currency of the instrument.")
    denomination_currency: str | None = Field(
        default=None,
        description=(
            "Currency the instrument's cash flows are in. When it is not the portfolio "
            "currency the leg is a currency bet, and every money field below has been "
            "converted to the portfolio currency at fx_rate_rub."
        ),
    )
    fx_rate_rub: Money | None = Field(
        default=None,
        description="Rubles per one unit of denomination_currency used for this leg (null for ruble instruments).",
    )
    price_quote_unit: PriceQuoteUnit
    quantity_lots: int
    lot_size: int
    quantity_units: int
    requested_amount: Money | None = Field(
        default=None,
        description="The money amount this leg was sized from, in rubles (null when lots were given).",
    )
    price: Money = Field(
        description="Reference market price in the quote unit (bonds: % of nominal). BUY uses ask, SELL uses bid.",
    )
    unit_price_money: Money = Field(
        description="Money price per unit IN RUBLES (bonds converted from % of nominal, then from denomination_currency)."
    )
    estimated_value: Money = Field(description="Clean value in rubles = unit_price_money × quantity_units.")
    accrued_interest: Money = Field(
        description="Total НКД for the leg in rubles (0 for non-bonds); paid on BUY, received on SELL."
    )
    commission: Money
    commission_source: Literal["broker", "estimated"] = Field(
        description="'broker' = GetOrderPrice quote; 'estimated' = fallback % from config.",
    )
    tax: SaleTaxEstimate | None = Field(default=None, description="Sale tax estimate (SELL legs only).")
    cash_effect: Money = Field(
        description="Signed cash flow of the leg: SELL = +value +НКД -commission; BUY = -(value +НКД +commission).",
    )
    cash_after: Money = Field(
        description="Simulated cash after this leg executes in sequence (commissions and НКД included).",
    )
    warnings: list[str] = Field(default_factory=list)


class SkippedPlanItem(BaseModel):
    """An input intent that could not become a leg (with the reason)."""

    instrument_uid: str
    action: str
    reason: str


class TradePlanAllocationPreview(BaseModel):
    """Allocation before/after the plan vs the target, per asset class."""

    asset_class: str
    before_pct: Money
    after_pct: Money
    target_pct: Money
    deviation_before_pct: Money = Field(description="before_pct - target_pct.")
    deviation_after_pct: Money = Field(description="after_pct - target_pct (closer to 0 is better).")


class TradePlanCostBenefit(BaseModel):
    """Deterministic cost-benefit verdict: is the rebalance worth its costs?"""

    commissions: Money
    taxes: Money
    total_costs: Money = Field(description="commissions + taxes (НКД is not a cost — it is repaid by the coupon).")
    misallocation_before: Money = Field(
        description="Money parked in the wrong asset class before the plan (Σ|current-target|/2).",
    )
    misallocation_after: Money
    drift_reduction_value: Money = Field(description="misallocation_before - misallocation_after.")
    cost_to_benefit_ratio: Money | None = Field(
        default=None,
        description="total_costs / drift_reduction_value (null when the plan reduces nothing).",
    )
    max_abs_deviation_before_pct: Money
    max_abs_deviation_after_pct: Money
    rebalance_threshold_pct: Money
    max_cost_to_benefit_ratio: Money = Field(description="Configured ceiling for cost_to_benefit_ratio.")
    verdict: CostBenefitVerdict
    reason: str

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "commissions": "45.20",
                    "taxes": "389.35",
                    "total_costs": "434.55",
                    "misallocation_before": "90000.00",
                    "misallocation_after": "12000.00",
                    "drift_reduction_value": "78000.00",
                    "cost_to_benefit_ratio": "0.0056",
                    "max_abs_deviation_before_pct": "30.00",
                    "max_abs_deviation_after_pct": "4.00",
                    "rebalance_threshold_pct": "5",
                    "max_cost_to_benefit_ratio": "0.05",
                    "verdict": "WORTH_IT",
                    "reason": "Costs are 0.56% of the misallocation removed (limit 5%).",
                }
            ]
        }
    )


class TradePlan(BaseModel):
    """A sequenced sell/buy basket with costs, taxes, allocation preview and a verdict.

    Advisory stage 6: the plan does NOT place orders. Stages 8-9 use the two
    UI-owned gates ``confirm_trade_plan(plan_id)`` and, per leg,
    ``preview_plan_step(plan_id)`` → ``execute_plan_step(plan_id)``.
    """

    plan_id: str
    status: TradePlanStatus
    mode: str
    created_at: datetime
    expires_at: datetime
    currency: str = "rub"
    cash_before: Money
    cash_after: Money = Field(description="Simulated cash after every leg executes in sequence.")
    items: list[PlannedTrade] = Field(default_factory=list)
    skipped: list[SkippedPlanItem] = Field(default_factory=list)
    plan_checks: list[RiskCheck] = Field(
        default_factory=list,
        description=(
            "Stage-7 plan checks (risk engine): per-step cash/position feasibility, post-plan "
            "allocation mandate band, cash floor, issuer/sector concentration — plus "
            "PLAN_CASH_WITH_COSTS (cash stays ≥ 0 including commissions and НКД)."
        ),
    )
    all_passed: bool = Field(description="Every plan-level risk check passed.")
    allocation_preview: list[TradePlanAllocationPreview] = Field(default_factory=list)
    cost_benefit: TradePlanCostBenefit
    totals: dict[str, Money] = Field(
        default_factory=dict,
        description="sell_proceeds_net / buy_cost_total / commissions / taxes / net_cash_change.",
    )
    notes: list[str] = Field(default_factory=list)


# --- trade plan confirmation / execution / verification (stages 8-10) ------


class PlanStepState(BaseModel):
    """Live state of one immutable leg from a confirmed trade plan."""

    sequence: int
    status: PlanStepStatus = "PENDING"
    action: Direction
    instrument_uid: str
    ticker: str | None = None
    name: str | None = None
    quantity_lots: int
    proposal_id: str | None = None
    preview_expires_at: datetime | None = None
    broker_order_id: str | None = None
    lots_requested: int | None = None
    lots_executed: int | None = None
    executed_price: Money | None = None
    total_amount: Money | None = None
    commission: Money | None = None
    updated_at: datetime | None = None
    message: str | None = None


class TradePlanState(BaseModel):
    """Plan-level state returned while the agent/UI walks the user through legs."""

    plan_id: str
    status: TradePlanStatus
    created_at: datetime
    expires_at: datetime
    confirmed_at: datetime | None = None
    paused_reason: str | None = None
    next_step_sequence: int | None = None
    can_preview_next: bool = False
    can_execute_next: bool = False
    is_terminal: bool = False
    steps: list[PlanStepState] = Field(default_factory=list)


class PlanStepPreview(BaseModel):
    """Fresh per-step execution card; still places no broker order."""

    plan_id: str
    plan_status: TradePlanStatus
    step_sequence: int
    preview: OrderPreview
    remaining_plan_checks: list[RiskCheck] = Field(default_factory=list)


class PlanExecutionResult(BaseModel):
    """Result of one idempotent ``execute_plan_step(plan_id)`` call."""

    plan_id: str
    plan_status: TradePlanStatus
    step_sequence: int | None = None
    order: OrderResult | None = None
    message: str
    next_action: Literal[
        "PREVIEW_NEXT_STEP",
        "POLL_PLAN_STATE",
        "WAIT_OR_CANCEL",
        "REVIEW_REJECTION",
        "VERIFY_PLAN",
        "NONE",
    ]


class TradePlanVerificationReport(BaseModel):
    """Fresh post-execution snapshot and concise before/after evidence trail."""

    plan_id: str
    plan_status: TradePlanStatus
    generated_at: datetime
    allocation_before_pct: dict[str, Money]
    allocation_after_pct: dict[str, Money]
    target_allocation_pct: dict[str, Money]
    max_abs_drift_before_pct: Money
    max_abs_drift_after_pct: Money
    drift_reduction_pct_points: Money
    planned_commissions: Money
    actual_commissions: Money
    estimated_taxes: Money
    total_costs_estimate: Money = Field(description="Actual reported commissions plus the plan's estimated taxes.")
    portfolio_summary: PortfolioSummary
    portfolio_analytics: PortfolioAnalytics
    summary: str
    notes: list[str] = Field(default_factory=list)


# --- trade plan validation (advisory stage 7 — risk-engine preview) ----------


class TradePlanStepInput(BaseModel):
    """One order of a rebalance plan, in execution sequence."""

    instrument_uid: str
    direction: Direction
    quantity_lots: int = Field(ge=1)
    limit_price: Money | None = Field(
        default=None,
        description=(
            "Optional limit price in the instrument quote unit (bonds: % of nominal). "
            "Omit to estimate with the current market price."
        ),
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "instrument_uid": "e6123145-9665-43e0-8413-cd61b8aa9b13",
                    "direction": "SELL",
                    "quantity_lots": 2,
                    "limit_price": None,
                }
            ]
        }
    )


class TradePlanStepPreview(BaseModel):
    """Simulated effect of one plan step (estimates: no commissions, market prices)."""

    step: int = Field(description="1-based position in the execution sequence.")
    instrument_uid: str
    ticker: str | None = None
    name: str | None = None
    instrument_type: str
    asset_class: str = Field(description="Target asset class the step trades: bonds | equity | cash | other.")
    direction: Direction
    quantity_lots: int
    price_used: Money = Field(description="Price the step was valued at (quote unit of the instrument).")
    price_source: Literal["limit_price", "last_price"] = Field(
        description="Where price_used came from: caller's limit or the market snapshot."
    )
    estimated_value: Money = Field(description="Money value of the step (bonds converted from % of nominal).")
    cash_after: Money = Field(description="Estimated cash after this step executes in sequence.")


class TradePlanPreview(BaseModel):
    """Returned by ``validate_trade_plan`` — plan-level risk checks against the personal mandate."""

    mode: str
    currency: str = "rub"
    steps: list[TradePlanStepPreview]
    cash_before: Money
    cash_after: Money
    cash_after_pct: Money = Field(description="Cash share after the plan, % of portfolio value.")
    total_value_after: Money
    allocation_before_pct: dict[str, Money] = Field(description="Asset-class % before the plan (bonds/equity/cash).")
    allocation_after_pct: dict[str, Money] = Field(description="Asset-class % after the plan (bonds/equity/cash).")
    target_allocation_pct: dict[str, int] = Field(description="Saved target allocation from the profile.")
    mandate: dict[str, Money] = Field(
        description="Resolved personal-mandate limits used: min_cash_pct, max_issuer_weight, max_sector_weight, rebalance_threshold_pct."
    )
    plan_checks: list[RiskCheck]
    all_passed: bool
    notes: list[str] = Field(default_factory=list)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "mode": "sandbox",
                    "currency": "rub",
                    "steps": [
                        {
                            "step": 1,
                            "instrument_uid": "e6123145-9665-43e0-8413-cd61b8aa9b13",
                            "ticker": "SBER",
                            "name": "Сбер Банк",
                            "instrument_type": "share",
                            "asset_class": "equity",
                            "direction": "SELL",
                            "quantity_lots": 2,
                            "price_used": "323.16",
                            "price_source": "last_price",
                            "estimated_value": "6463.20",
                            "cash_after": "16463.20",
                        }
                    ],
                    "cash_before": "10000",
                    "cash_after": "16463.20",
                    "cash_after_pct": "16.46",
                    "total_value_after": "100000",
                    "allocation_before_pct": {"bonds": "50.00", "equity": "40.00", "cash": "10.00"},
                    "allocation_after_pct": {"bonds": "50.00", "equity": "33.54", "cash": "16.46"},
                    "target_allocation_pct": {"bonds": 55, "equity": 35, "cash": 10},
                    "mandate": {
                        "min_cash_pct": "5",
                        "max_issuer_weight": "0.15",
                        "max_sector_weight": "0.30",
                        "rebalance_threshold_pct": "5",
                    },
                    "plan_checks": [
                        {
                            "code": "PLAN_STEP_CASH",
                            "passed": True,
                            "severity": "error",
                            "message": "Step 1 (SELL SBER): sell adds cash; cash after = 16463.20",
                        },
                        {
                            "code": "PLAN_ALLOCATION_MANDATE",
                            "passed": True,
                            "severity": "error",
                            "message": "All asset classes within ±5 pp of the target allocation",
                        },
                    ],
                    "all_passed": True,
                    "notes": [
                        "Estimates exclude commissions and taxes; per-order checks still run in create_order_proposal."
                    ],
                }
            ]
        }
    )
