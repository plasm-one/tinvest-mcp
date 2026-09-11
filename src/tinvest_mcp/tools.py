"""Tool implementations for the T-Invest MCP server.

These functions carry the ``Annotated[..., Field(...)]`` signatures FastMCP uses
to build the JSON schema. They are registered in :mod:`tinvest_mcp.server`.

Tool safety boundary:
- AI-facing tools build read-only previews; the UI owns both confirmation gates.
- ``post_order`` accepts only ``proposal_id``; plan execution accepts only
  ``plan_id``. Neither execution path accepts raw account/price/quantity.
- Plan-linked proposals cannot be sent through ``post_order``; only
  ``execute_plan_step(plan_id)`` may submit them after a whole-plan confirmation
  and a fresh per-step preview. Transfer / real-pay-in tools are absent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from pydantic import Field

from . import profile_store, services
from .adapter import TInvestAdapter
from .allocation import (
    MandateScreenFilters,
    mandate_screen_filters,
    propose_allocation,
    validate_allocation,
)
from .audit import audit_event
from .config.env import get_settings
from .errors import TInvestError
from .schemas import (
    AnalystForecast,
    BondSchedule,
    BondScreenHit,
    BrokerAccount,
    EtfDetails,
    EtfScreenHit,
    ExecutingOrderSummary,
    InstrumentAnalytics,
    InstrumentFundamentals,
    InstrumentSearchHit,
    InvestmentHorizon,
    InvestmentInstrument,
    InvestmentProfile,
    MarketSnapshot,
    OperationsPage,
    OrderPreview,
    OrderResult,
    PlanExecutionResult,
    PlanStepPreview,
    PortfolioAnalytics,
    PortfolioSummary,
    RiskProfile,
    ShareScreenHit,
    TargetAllocation,
    TradePlan,
    TradePlanItemInput,
    TradePlanPreview,
    TradePlanState,
    TradePlanStepInput,
    TradePlanVerificationReport,
)

# Real, stable instrument UIDs (global) used as schema examples for the LLM.
EX_SHARE_UID = "e6123145-9665-43e0-8413-cd61b8aa9b13"  # SBER
EX_BOND_UID = "33672905-be3c-4a02-a1c3-4be155814bb5"  # RU000A0ZYX28 (~98% of nominal)
EX_ETF_UID = "555dcd42-2c14-43d5-ba93-8a4a42160638"  # AMRE (etf)


def _adapter() -> TInvestAdapter:
    return TInvestAdapter(get_settings())


def _to_decimal(value: str, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ToolError(f"Invalid decimal value for '{field}': {value!r}") from exc


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except TInvestError as exc:
        # Our errors never contain tokens; surface the message cleanly. The cause
        # is chained for the stderr log, not for the model — ToolError carries
        # only the message.
        raise ToolError(str(exc)) from exc


def _mandate_filters() -> MandateScreenFilters:
    """Screener filters from the saved profile; error if the stage was skipped."""
    settings = get_settings()
    profile = profile_store.load_profile(settings.investment_profile_path)
    if profile is None:
        raise ToolError(
            "apply_mandate=true, but no investment profile is saved. Complete the allocation "
            "stage first: propose_target_allocation → user confirms → save_investment_profile."
        )
    return mandate_screen_filters(profile)


# ---------------------------------------------------------------------------
# Read / research tools (AI-safe)
# ---------------------------------------------------------------------------


def get_accounts() -> list[BrokerAccount]:
    """List accounts with separate research, planning and execution capabilities.

    ``research_access_level`` is scoped to the least-privileged research token. It must
    never be used to infer whether planning or execution is available; use the
    explicit ``planning_available`` and ``execution_available`` fields instead.
    """
    settings = get_settings()
    return _guard(services.list_accounts, _adapter(), settings)


def get_portfolio_summary() -> PortfolioSummary:
    """Brokerage portfolio: total value, cash, allocation, positions, concentration.

    Call at the **start** of any purchase flow (step 1) and **after FILLED** (step 9)
    to verify cash and new position."""
    settings = get_settings()
    return _guard(services.get_portfolio_summary, _adapter(), settings)


def get_operations(
    from_date: Annotated[
        str | None,
        Field(
            description="Period start (YYYY-MM-DD, UTC). Default: 90 days before to_date.",
            default=None,
            examples=["2026-01-01"],
        ),
    ] = None,
    to_date: Annotated[
        str | None,
        Field(description="Period end (YYYY-MM-DD, UTC). Default: today.", default=None, examples=["2026-06-30"]),
    ] = None,
    operation_types: Annotated[
        list[str] | None,
        Field(
            description="Filter by operation types, e.g. buy, sell, coupon, dividend, broker_fee, tax.",
            default=None,
            examples=[["buy", "sell"], ["dividend", "coupon"]],
        ),
    ] = None,
    instrument_uid: Annotated[
        str | None,
        Field(description="Filter operations for one instrument uid.", default=None, examples=[EX_SHARE_UID]),
    ] = None,
    cursor: Annotated[
        str | None,
        Field(description="Pagination cursor from a previous response (next_cursor).", default=None),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Max operations per page (3–1000, API recommends >2).", ge=3, le=1000, default=100),
    ] = 100,
    include_canceled: Annotated[
        bool,
        Field(description="Include canceled operations (default: executed only).", default=False),
    ] = False,
) -> OperationsPage:
    """Brokerage operation history: trades, commissions, coupons, dividends, withheld taxes.

    Returns one page plus ``totals`` for the current page and ``next_cursor`` when more data exists.
    Does not include tax-lot accounting — use for cash-flow and activity review only."""
    settings = get_settings()

    def _parse_day(value: str | None, field: str) -> date | None:
        if value is None:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ToolError(f"Invalid date for '{field}': {value!r} (expected YYYY-MM-DD)") from exc

    return _guard(
        services.get_operations,
        _adapter(),
        settings,
        from_date=_parse_day(from_date, "from_date"),
        to_date=_parse_day(to_date, "to_date"),
        operation_types=operation_types,
        instrument_uid=instrument_uid,
        cursor=cursor,
        limit=limit,
        include_canceled=include_canceled,
    )


def get_portfolio_analytics(
    include_bond_metrics: Annotated[
        bool,
        Field(description="Compute bond-portfolio Macaulay duration (extra API calls per bond).", default=True),
    ] = True,
    top_n: Annotated[
        int,
        Field(description="How many largest positions to return in top_positions.", ge=1, le=20, default=5),
    ] = 5,
) -> PortfolioAnalytics:
    """Portfolio aggregates: allocation by class/sector/currency/issuer, concentration, weighted yield, bond duration.

    When an investment profile is saved (``save_investment_profile``), the response
    also carries ``drift`` — the GAP ANALYSIS against the target allocation:

      - per asset class: ``current_pct`` vs ``target_pct``, ``deviation_pct``,
        ``amount_to_trade`` (positive = buy, negative = sell), ``action``
        (``hold`` when the deviation is under the rebalance threshold)
      - ``mandate_violations``: issuer/sector weights above mandate limits,
        with the approximate ``excess_value`` to shed

    All drift numbers are computed by code — translate them for the user and
    highlight the biggest gaps, do not recompute. ``drift`` is null until the
    allocation stage is completed. Amounts ignore commissions and lot rounding."""
    settings = get_settings()
    return _guard(
        services.get_portfolio_analytics,
        _adapter(),
        settings,
        include_bond_metrics=include_bond_metrics,
        top_n=top_n,
    )


def search_instruments(
    query: Annotated[
        str, Field(description="Search text: ticker, name or ISIN (e.g. 'ОФЗ', 'SBER').", examples=["SBER", "ОФЗ"])
    ],
    instrument_types: Annotated[
        list[str] | None,
        Field(
            description="Filter by types: any of 'share', 'bond', 'etf'.", default=None, examples=[["share"], ["bond"]]
        ),
    ] = None,
    api_trade_available: Annotated[
        bool, Field(description="Only instruments available for API trading.", default=True)
    ] = True,
    qualified_only: Annotated[
        bool, Field(description="If false (default), exclude qualified-investor-only instruments.", default=False)
    ] = False,
    limit: Annotated[int, Field(description="Max results.", default=20, ge=1, le=50)] = 20,
) -> list[InstrumentSearchHit]:
    """Search the T-Invest catalogue for tradeable instruments."""
    return _guard(
        services.search_instruments,
        _adapter(),
        query,
        instrument_types=instrument_types,
        api_trade_available=api_trade_available,
        qualified_only=qualified_only,
        limit=limit,
    )


def list_bonds(
    currency: Annotated[
        str | None,
        Field(
            description="Filter by SETTLEMENT currency (what you pay with), e.g. 'rub'. Does NOT exclude foreign-denominated bonds — a yuan bond can settle in rubles. Use denomination_currency for that.",
            default=None,
        ),
    ] = None,
    denomination_currency: Annotated[
        str | None,
        Field(
            description=(
                "Filter by the currency the bond actually PAYS in (nominal, coupons, redemption) — "
                "which is also the currency its yields are quoted in. Pass 'rub' for a pure ruble "
                "ladder, 'cny' to look at yuan-linked issues on purpose. REQUIRED when sorting by "
                "yield/ytm across a mixed catalogue: ranking a yuan 8.7% next to a ruble 15.7% is "
                "an exchange-rate forecast dressed up as a yield table, so that call is rejected."
            ),
            default=None,
            examples=["rub"],
        ),
    ] = None,
    api_trade_available: Annotated[bool, Field(description="Only API-tradeable instruments.", default=True)] = True,
    qualified_only: Annotated[
        bool, Field(description="If false (default), exclude qualified-investor-only.", default=False)
    ] = False,
    risk_level: Annotated[
        Literal["low", "moderate", "high"] | None,
        Field(description="Keep only this risk tier (T-Invest risk_level).", default=None, examples=["low"]),
    ] = None,
    max_maturity_years: Annotated[
        float | None,
        Field(description="Keep bonds maturing within this many years from now.", default=None, examples=[3]),
    ] = None,
    max_duration_years: Annotated[
        float | None,
        Field(
            description=(
                "Keep bonds with Macaulay duration ≤ this many years (ladder: duration ≤ horizon). "
                "Needs the analytics tier (auto-enabled, capped by analytics_limit); bonds without "
                "computable duration (perpetual/undated) are dropped."
            ),
            default=None,
            examples=[3],
        ),
    ] = None,
    min_avg_daily_turnover_rub: Annotated[
        float | None,
        Field(
            description=(
                "LIQUIDITY floor: keep bonds whose ~30-session average daily traded value (money) "
                "is at least this. Illiquid bonds are easy to buy but exit costs several % on the "
                "spread. Needs the analytics tier (auto-enabled, capped); bonds without volume "
                "data are dropped. E.g. 1000000 = 1 млн/day."
            ),
            default=None,
            examples=[1000000],
        ),
    ] = None,
    apply_mandate: Annotated[
        bool,
        Field(
            description=(
                "Apply the saved investment profile's mandate: drop excluded_sectors, cap "
                "risk_level by profile (conservative→low, moderate→moderate, aggressive→high, "
                "unless overridden in the profile) and default max_duration_years from the "
                "horizon (short→1y, medium→3y, long→no cap). Errors if no profile is saved."
            ),
            default=False,
        ),
    ] = False,
    sort_by: Annotated[
        Literal[
            "name",
            "ticker",
            "maturity",
            "risk",
            "coupons",
            "nominal",
            "price",
            "yield",
            "ytm",
            "return",
            "volatility",
            "drawdown",
        ]
        | None,
        Field(
            description=(
                "Sort key. Cheap (no extra calls): name, ticker, maturity, risk "
                "(low→high), coupons, nominal. Needs market data: price. Needs analytics "
                "(auto-enables include_analytics, capped): yield (current coupon yield), "
                "ytm (yield to maturity/offer — effective annual yield, the main metric for "
                "comparing bonds), return (1y historical), volatility, drawdown."
            ),
            default=None,
            examples=["ytm", "maturity", "risk"],
        ),
    ] = None,
    descending: Annotated[
        bool, Field(description="Sort high→low (default). Set false for low→high.", default=True)
    ] = True,
    include_analytics: Annotated[
        bool,
        Field(
            description=(
                "When true, compute current_yield_pct/return/volatility/drawdown for the top "
                "candidates (one get_instrument_analytics call each → capped by analytics_limit, "
                "slower). Without this (and without sort_by return/volatility/drawdown) those "
                "fields are null in the response — not missing data."
            ),
            default=False,
        ),
    ] = False,
    analytics_limit: Annotated[
        int,
        Field(
            description="Max bonds to enrich when include_analytics is on. Keep at least 25 during discovery; reduce only after a broad pool has already been ranked.",
            default=25,
            ge=1,
            le=50,
        ),
    ] = 25,
    limit: Annotated[
        int,
        Field(
            description="Max results. Candidate discovery should request at least 25; the final 2-3 user-facing choices are selected from that broader result.",
            default=100,
            ge=1,
            le=1000,
        ),
    ] = 100,
) -> list[BondScreenHit]:
    """Screen the BOND catalogue. Rows carry isin, nominal/initial_nominal,
    maturity_date, call_date (оферта), risk_level, coupons/yr, structural flags
    (floating/amortization/perpetual/subordinated), liquidity_flag, for_iis_flag,
    issue_kind/issue_size, НКД and a batched last_price.

    CANDIDATE SCREENING (stage 5): combine apply_mandate=true (profile mandate),
    max_duration_years (ladder: duration ≤ horizon) and min_avg_daily_turnover_rub
    (liquidity floor) — then compare the survivors by ytm/volatility.

    BOND PRICING: last_price and order prices are % of nominal (face value), NOT rubles
    (see price_quote_unit on each row). E.g. last_price 98.0 = 98% of nominal.

    CURRENCY: check nominal_currency / yield_currency on every row before comparing
    yields. Some MOEX bonds settle in rubles but are denominated in yuan, so their
    ytm_pct is a YUAN yield: the gap to a ruble OFZ is the market's CNY/RUB
    expectation, not extra income, and the position is a currency bet on top of the
    credit bet. Rank yields inside ONE yield_currency (pass denomination_currency),
    and use avg_daily_turnover_rub — not avg_daily_turnover — to compare liquidity."""
    excluded_sectors = None
    max_bond_risk_level = None
    if apply_mandate:
        mandate = _mandate_filters()
        excluded_sectors = list(mandate.excluded_sectors) or None
        max_bond_risk_level = mandate.max_bond_risk_level
        if max_duration_years is None:
            max_duration_years = mandate.max_bond_duration_years
    return _guard(
        services.list_bonds,
        _adapter(),
        settings=get_settings(),
        currency=currency,
        denomination_currency=denomination_currency,
        api_trade_available=api_trade_available,
        qualified_only=qualified_only,
        risk_level=risk_level,
        max_bond_risk_level=max_bond_risk_level,
        excluded_sectors=excluded_sectors,
        max_maturity_years=max_maturity_years,
        max_duration_years=max_duration_years,
        min_avg_daily_turnover=_to_decimal(str(min_avg_daily_turnover_rub), "min_avg_daily_turnover_rub")
        if min_avg_daily_turnover_rub is not None
        else None,
        sort_by=sort_by,
        descending=descending,
        include_analytics=include_analytics,
        analytics_limit=analytics_limit,
        limit=limit,
    )


def list_shares(
    currency: Annotated[str | None, Field(description="Filter by currency, e.g. 'rub'.", default=None)] = None,
    api_trade_available: Annotated[bool, Field(description="Only API-tradeable instruments.", default=True)] = True,
    qualified_only: Annotated[
        bool, Field(description="If false (default), exclude qualified-investor-only.", default=False)
    ] = False,
    sector: Annotated[
        str | None,
        Field(description="Filter by sector, e.g. 'financial', 'it', 'energy'.", default=None, examples=["financial"]),
    ] = None,
    pays_dividends: Annotated[
        bool | None,
        Field(description="If set, keep only shares that do (true) / don't (false) pay dividends.", default=None),
    ] = None,
    min_avg_daily_turnover_rub: Annotated[
        float | None,
        Field(
            description=(
                "LIQUIDITY floor: keep shares whose ~30-session average daily traded value (money) "
                "is at least this. Needs the analytics tier (auto-enabled, capped); shares without "
                "volume data are dropped."
            ),
            default=None,
            examples=[10000000],
        ),
    ] = None,
    apply_mandate: Annotated[
        bool,
        Field(
            description=(
                "Apply the saved investment profile's mandate: drop excluded_sectors. Errors if no profile is saved."
            ),
            default=False,
        ),
    ] = False,
    sort_by: Annotated[
        Literal[
            "name",
            "ticker",
            "price",
            "dividend_yield",
            "return",
            "volatility",
            "drawdown",
            "recommendation",
            "upside",
            "pe",
            "ps",
            "pb",
            "ev_ebitda",
            "roe",
            "roa",
            "roic",
            "net_margin",
            "eps",
            "market_cap",
            "debt_to_equity",
            "net_debt_ebitda",
            "dividend_yield_fund",
            "revenue_growth",
            "beta",
        ]
        | None,
        Field(
            description=(
                "Sort key. Cheap (no extra calls): name, ticker. Needs market data: "
                "price. Needs analytics (auto-enables include_analytics, capped): "
                "dividend_yield, return (1y historical), volatility, drawdown. "
                "Needs analyst forecast (auto-enables include_forecast, ONE bulk sweep): "
                "recommendation (consensus buy>hold>sell, ranked over the FULL list), "
                "upside (consensus target vs. price). Needs fundamentals "
                "(auto-enables include_fundamentals, batched, capped): valuation pe/ps/pb/"
                "ev_ebitda, returns roe/roa/roic, net_margin, eps, market_cap, leverage "
                "debt_to_equity/net_debt_ebitda, dividend_yield_fund, revenue_growth (5y), beta. "
                "Tip: for cheap ratios (pe, ev_ebitda, debt_to_equity, net_debt_ebitda) sort "
                "ascending (descending=false). "
                "(Shares have no risk tier — use volatility/drawdown/beta as the risk proxy.)"
            ),
            default=None,
            examples=["recommendation", "pe", "roe"],
        ),
    ] = None,
    descending: Annotated[
        bool, Field(description="Sort high→low (default). Set false for low→high.", default=True)
    ] = True,
    include_analytics: Annotated[
        bool,
        Field(
            description=(
                "When true, compute dividend_yield_pct/return/volatility/drawdown for the top "
                "candidates (one get_instrument_analytics call each → capped by analytics_limit, "
                "slower). Without this (and without sort_by return/volatility/drawdown) those "
                "fields are null in the response — not missing data."
            ),
            default=False,
        ),
    ] = False,
    include_forecast: Annotated[
        bool,
        Field(
            description="Attach analyst consensus (recommendation, target price, upside, buy/hold/sell split) via ONE bulk forecast sweep. Cheap — covers the whole list.",
            default=False,
        ),
    ] = False,
    include_fundamentals: Annotated[
        bool,
        Field(
            description="Attach key fundamentals (P/E, P/B, ROE, market cap, EV/EBITDA) for the top candidates via a batched sweep (capped). Use get_instrument_fundamentals for the full set.",
            default=False,
        ),
    ] = False,
    analytics_limit: Annotated[
        int,
        Field(
            description="Max shares to enrich when include_analytics is on. Keep at least 25 during discovery; reduce only after a broad pool has already been ranked.",
            default=25,
            ge=1,
            le=50,
        ),
    ] = 25,
    limit: Annotated[
        int,
        Field(
            description="Max results. Candidate discovery should request at least 25; the final 2-3 user-facing choices are selected from that broader result.",
            default=100,
            ge=1,
            le=1000,
        ),
    ] = 100,
) -> list[ShareScreenHit]:
    """Screen the SHARE catalogue. Rows carry sector, share_type, pays_dividends
    and a batched last_price. Shares have NO risk_level in the API — use
    include_analytics for computed volatility / max drawdown (risk proxy),
    dividend yield and historical return on the top candidates. Set
    include_forecast for the ANALYST CONSENSUS (recommendation, target, upside %,
    buy/hold/sell) — a third-party rating fetched in one bulk sweep. Set
    include_fundamentals for key valuation ratios (P/E, P/B, ROE, market cap,
    EV/EBITDA). Filter by sector / pays_dividends; sort by
    recommendation/upside/pe/roe/dividend_yield/volatility/price."""
    excluded_sectors = None
    if apply_mandate:
        excluded_sectors = list(_mandate_filters().excluded_sectors) or None
    return _guard(
        services.list_shares,
        _adapter(),
        settings=get_settings(),
        currency=currency,
        api_trade_available=api_trade_available,
        qualified_only=qualified_only,
        sector=sector,
        excluded_sectors=excluded_sectors,
        pays_dividends=pays_dividends,
        min_avg_daily_turnover=_to_decimal(str(min_avg_daily_turnover_rub), "min_avg_daily_turnover_rub")
        if min_avg_daily_turnover_rub is not None
        else None,
        sort_by=sort_by,
        descending=descending,
        include_analytics=include_analytics,
        include_forecast=include_forecast,
        include_fundamentals=include_fundamentals,
        analytics_limit=analytics_limit,
        limit=limit,
    )


def list_etfs(
    currency: Annotated[str | None, Field(description="Filter by currency, e.g. 'rub'.", default=None)] = None,
    api_trade_available: Annotated[bool, Field(description="Only API-tradeable instruments.", default=True)] = True,
    qualified_only: Annotated[
        bool, Field(description="If false (default), exclude qualified-investor-only.", default=False)
    ] = False,
    sector: Annotated[
        str | None,
        Field(description="Filter by sector if set on the fund.", default=None),
    ] = None,
    focus_type: Annotated[
        str | None,
        Field(
            description=(
                "Filter by focus: equity, fixed_income, mixed_allocation, "
                "alternative_investment. The catalogue's own focus_type is not "
                "trustworthy on its own — money-market (LQDT), bond (SBGB) and gold "
                "(GOLD) funds all report 'equity' — so the filter additionally drops "
                "rows whose asset_class, inferred from the fund name, contradicts the "
                "requested focus. Funds whose class cannot be inferred are kept; check "
                "asset_class on the returned rows."
            ),
            default=None,
            examples=["equity"],
        ),
    ] = None,
    min_avg_daily_turnover_rub: Annotated[
        float | None,
        Field(
            description=(
                "LIQUIDITY floor: keep funds whose ~30-session average daily traded value (money) "
                "is at least this. Needs the analytics tier (auto-enabled, capped); funds without "
                "volume data are dropped."
            ),
            default=None,
            examples=[1000000],
        ),
    ] = None,
    apply_mandate: Annotated[
        bool,
        Field(
            description=(
                "Apply the saved investment profile's mandate: drop excluded_sectors. Errors if no profile is saved."
            ),
            default=False,
        ),
    ] = False,
    sort_by: Annotated[
        Literal["name", "ticker", "price", "commission", "focus", "released", "return", "volatility", "drawdown", "ter"]
        | None,
        Field(
            description=(
                "Sort key. Cheap (no extra calls): name, ticker, commission "
                "(fixed_commission_pct), focus (focus_type), released (inception date). "
                "Needs market data: price. Needs analytics (auto-enables include_analytics, "
                "capped): return (1y historical), volatility, drawdown. Needs fees "
                "(auto-enables include_fees, capped): ter (total_expense_pct; use "
                "descending=false for cheapest first)."
            ),
            default=None,
            examples=["return", "commission", "released", "ter"],
        ),
    ] = None,
    descending: Annotated[
        bool, Field(description="Sort high→low (default). Set false for low→high.", default=True)
    ] = True,
    include_analytics: Annotated[
        bool,
        Field(
            description=(
                "When true, compute return/volatility/drawdown for the top candidates "
                "(one get_instrument_analytics call each → capped by analytics_limit, slower). "
                "Without this (and without sort_by return/volatility/drawdown) those fields are "
                "null in the response — not missing data. Set analytics_limit to match limit when "
                "comparing a shortlist."
            ),
            default=False,
        ),
    ] = False,
    include_fees: Annotated[
        bool,
        Field(
            description=(
                "When true, fetch TER (total_expense_pct) — one get_asset_by call per "
                "distinct asset. On its own this ranks the WHOLE filtered set; combined "
                "with include_analytics (or a liquidity floor) the tighter analytics_limit "
                "bounds it. Without this (and without sort_by='ter') total_expense_pct is "
                "null in the response — not missing data."
            ),
            default=False,
        ),
    ] = False,
    analytics_limit: Annotated[
        int,
        Field(
            description="Max ETFs to enrich when include_analytics is on (also bounds include_fees when both run). Keep at least 25 during discovery; reduce only after a broad pool has already been ranked.",
            default=25,
            ge=1,
            le=50,
        ),
    ] = 25,
    limit: Annotated[
        int,
        Field(
            description="Max results. Candidate discovery should request at least 25; the final 2-3 user-facing choices are selected from that broader result.",
            default=100,
            ge=1,
            le=1000,
        ),
    ] = 100,
) -> list[EtfScreenHit]:
    """Screen the ETF / fund catalogue. Rows carry isin, focus_type, asset_class,
    rebalancing_freq, num_shares, fixed_commission_pct, liquidity_flag,
    for_iis_flag, released_date and a batched last_price. Filter by focus_type /
    sector; sort by commission/ter/focus/released/return/volatility/price.

    Rank asset classes off ``asset_class`` (inferred from the fund name), NOT the
    catalogue's ``focus_type``, which labels money-market, bond and gold funds
    'equity'. Blocked-asset shells (frozen foreign holdings, price 0) are always
    excluded.

    historical_return_pct / volatility_annual_pct / max_drawdown_pct are null unless
    include_analytics=true (or sort_by is return/volatility/drawdown). total_expense_pct
    (TER, the all-in annual fee) is null unless include_fees=true (or sort_by='ter').
    For full fee breakdown, benchmark and strategy text use get_etf_details."""
    excluded_sectors = None
    if apply_mandate:
        excluded_sectors = list(_mandate_filters().excluded_sectors) or None
    return _guard(
        services.list_etfs,
        _adapter(),
        settings=get_settings(),
        currency=currency,
        api_trade_available=api_trade_available,
        qualified_only=qualified_only,
        sector=sector,
        excluded_sectors=excluded_sectors,
        focus_type=focus_type,
        min_avg_daily_turnover=_to_decimal(str(min_avg_daily_turnover_rub), "min_avg_daily_turnover_rub")
        if min_avg_daily_turnover_rub is not None
        else None,
        sort_by=sort_by,
        descending=descending,
        include_analytics=include_analytics,
        include_fees=include_fees,
        analytics_limit=analytics_limit,
        limit=limit,
    )


def get_instrument_details(
    instrument_uid: Annotated[str, Field(description="Instrument UID from search results.", examples=[EX_SHARE_UID])],
) -> InvestmentInstrument:
    """Instrument parameters for order sizing (purchase playbook step 3).

    Key fields for trading:
      - ``lot`` — units per lot (quantity_lots × lot = units bought)
      - ``min_price_increment`` — price grid for limits
      - ``price_quote_unit`` — bonds: pct_of_nominal; shares/ETFs: currency
      - ``nominal`` — bond face value (for money cost ≈ nominal × price% / 100)
      - ``api_trade_available``, ``buy_available`` — must be true to trade via API"""
    return _guard(services.load_instrument, _adapter(), instrument_uid)


def get_instrument_analytics(
    instrument_uid: Annotated[str, Field(description="Instrument UID.", examples=[EX_BOND_UID])],
) -> InstrumentAnalytics:
    """Yield & risk signals for one instrument: historical return / volatility / max drawdown
    (from ~1y daily candles), plus bond risk_level & current yield (coupons) or share dividend
    yield. Figures are estimates, not guarantees (see `notes`).

    READ `yield_currency` BEFORE USING ANY YIELD. Bonds whose nominal is in a foreign
    currency (`fx_linked=true` — yuan issues settled in rubles are the common case)
    yield in THAT currency: an 8.7% CNY ytm and a 15.7% RUB ytm are not 7 points
    apart, they differ by what the market expects CNY/RUB to do, and the quoted
    volatility excludes that exchange-rate risk entirely. Say so instead of ranking
    them together, and use `avg_daily_turnover_rub` for liquidity."""
    return _guard(services.get_instrument_analytics, _adapter(), get_settings(), instrument_uid)


def get_instrument_forecast(
    instrument_uid: Annotated[str, Field(description="Instrument UID (best for shares).", examples=[EX_SHARE_UID])],
) -> AnalystForecast:
    """Analyst CONSENSUS rating & target prices for one instrument.

    Returns the consensus recommendation (buy/hold/sell), the consensus 12-month
    target price with its min/max band, the implied `upside_pct` vs. the current
    price, the buy/hold/sell analyst split and each analyst's individual target.
    Use this to judge how promising a share is. It is a third-party opinion, NOT
    a guarantee (see `notes`). Mostly populated for liquid shares."""
    return _guard(services.get_instrument_forecast, _adapter(), get_settings(), instrument_uid)


def get_instrument_fundamentals(
    instrument_uid: Annotated[str, Field(description="Instrument UID (best for shares).", examples=[EX_SHARE_UID])],
) -> InstrumentFundamentals:
    """Company FINANCIAL metrics & valuation ratios for one instrument.

    The numbers from the app's "Показатели" tab: market cap, P/E, P/B, P/S,
    EV/EBITDA, ROE/ROA/ROIC, net margin, EPS, revenue/EBITDA/net income (TTM),
    debt ratios (D/E, net debt/EBITDA), dividend yield & payout, and revenue
    growth rates. Use it to judge valuation & quality. Third-party vendor data;
    missing fields are `null`. Mostly populated for shares."""
    return _guard(services.get_instrument_fundamentals, _adapter(), get_settings(), instrument_uid)


def get_etf_details(
    instrument_uid: Annotated[str, Field(description="ETF instrument UID.", examples=[EX_ETF_UID])],
) -> EtfDetails:
    """Extended ETF / BPIF metadata for one fund.

    Full fee breakdown (TER, fixed/expense commission, performance fee), benchmark/
    index, strategy description, tracking error, management style (passive/active),
    distribution flag, iNAV code and tax notes — the detail behind ``list_etfs``.
    Portfolio holdings and NAV premium are NOT in the API (see ``notes``)."""
    return _guard(services.get_etf_details, _adapter(), get_settings(), instrument_uid)


def get_bond_schedule(
    instrument_uid: Annotated[str, Field(description="Bond instrument UID.", examples=[EX_BOND_UID])],
) -> BondSchedule:
    """Full COUPON SCHEDULE + lifecycle events for one bond.

    Lists every upcoming coupon (date, amount per bond, type, record/fix date) and
    lifecycle events — the call/offer (оферта), the maturity redemption. Use it to
    see exactly when and how much a bond pays, and whether there is an offer before
    maturity. For floating-rate bonds only the already-fixed coupons are reliable
    (see `notes`)."""
    return _guard(services.get_bond_schedule, _adapter(), get_settings(), instrument_uid)


def get_market_snapshot(
    instrument_uid: Annotated[str, Field(description="Instrument UID.", examples=[EX_SHARE_UID])],
) -> MarketSnapshot:
    """Market data — call **before** create_order_proposal (purchase playbook step 4).

    Returns:
      - ``price_quote_unit``: ``pct_of_nominal`` (bonds) or ``currency`` (shares/ETFs).
      - ``last_price``, ``best_bid``, ``best_ask``, exchange corridor ``limit_up``/``limit_down``.
      - ``is_fresh`` / ``age_seconds``: quote age (informational; see ``buy_price_hints.session_warnings``).
      - ``buy_price_hints``: algorithmic BUY limits — use with ``create_order_proposal.urgency``:
        * ``patient`` → join ``best_bid`` (cheapest, slowest)
        * ``balanced`` → mid spread (default)
        * ``fast`` → cross ``best_ask`` (quickest fill in trading session)

    **Bonds:** all prices are % of nominal (e.g. 99.09), NOT rubles. Never convert to rubles
    before passing to create_order_proposal.

    ``liquidity``: avg daily volume/turnover over ~30 sessions + current spread_pct.
    Check it before proposing a candidate — a wide spread (>1%) or thin turnover means
    the position is expensive to exit."""
    settings = get_settings()
    return _guard(services.get_market_snapshot, _adapter(), settings, instrument_uid)


def create_order_proposal(
    instrument_uid: Annotated[str, Field(description="Instrument UID to trade.", examples=[EX_BOND_UID])],
    direction: Annotated[str, Field(description="'BUY' or 'SELL'.", examples=["BUY", "SELL"])],
    order_type: Annotated[str, Field(description="'LIMIT' (only LIMIT is allowed in this MVP).", examples=["LIMIT"])],
    quantity_lots: Annotated[int, Field(description="Number of lots to trade.", ge=1, examples=[1])],
    limit_price: Annotated[
        str | None,
        Field(
            description=(
                "Optional limit price in the instrument quote unit. "
                "Omit to use urgency (default balanced). "
                "BONDS: % of nominal (e.g. '99.09'). SHARES/ETFs: currency per unit."
            ),
            default=None,
            examples=["99.09", "98.0"],
        ),
    ] = None,
    urgency: Annotated[
        str | None,
        Field(
            description=(
                "When limit_price is omitted: 'balanced' (mid spread, default), 'patient', 'fast'. "
                "BUY: patient=join bid, fast=cross ask. SELL mirrors: patient=join ask, fast=cross bid."
            ),
            default=None,
            examples=["fast", "balanced", "patient"],
        ),
    ] = None,
    rationale: Annotated[str | None, Field(description="Short reason for this order.", default=None)] = None,
    user_request_id: Annotated[str | None, Field(description="Caller correlation id.", default=None)] = None,
) -> OrderPreview:
    """Risk-checked order PREVIEW — **does NOT place an order** (playbook step 5).

    **Recommended:** omit ``limit_price``, set ``urgency``:
      - ``balanced`` (default) — compromise price/speed
      - ``fast`` — trade quickly (BUY: cross ask; SELL: cross bid)
      - ``patient`` — best price, may wait in queue

    BUY prices come from ``get_market_snapshot.buy_price_hints``; SELL prices are the
    mirrored tiers computed server-side from the same snapshot.

    **Explicit price:** pass ``limit_price`` only if the user named a specific limit.
    Response includes ``price_selection`` with warnings (e.g. limit off the spread).

    **Before showing preview to user, verify:**
      - ``all_passed`` is true
      - ``status`` is ``READY_FOR_CONFIRMATION``
      - Explain ``order.estimated_total``, ``portfolio_impact``, ``price_selection.warning``
      - SELL: also show ``tax_impact`` (estimated НДФЛ) and any ``risk_checks`` with
        ``severity=warning`` (``LDV_WARNING`` — 3-year exemption at stake;
        ``CORPORATE_ACTION_SOON`` — offer/maturity close; both pass but MUST reach the user)

    **If ``RISK_REJECTED``:** read ``risk_checks`` where ``passed=false``:
      - ``PRICE_DEVIATION`` → wrong unit (bonds: use % not rubles) or use urgency
      - ``SUFFICIENT_CASH`` / ``MAX_ORDER_VALUE`` → reduce ``quantity_lots``
      - ``POSITION_EXISTS`` (SELL) → not enough sellable lots; check get_portfolio_summary

    **Next step:** after user confirms in UI, the trusted UI/execution controller
    (never the AI agent) invokes ``post_order(proposal_id)``.
    Proposal expires in ~60s (``expires_at``).

    Bonds: ``limit_price`` and hints are % of nominal, same as ``get_market_snapshot``."""
    settings = get_settings()
    price = _to_decimal(limit_price, "limit_price") if limit_price is not None else None
    return _guard(
        services.create_order_proposal,
        _adapter(),
        settings,
        instrument_uid=instrument_uid,
        direction=direction,
        order_type=order_type,
        quantity_lots=quantity_lots,
        limit_price=price,
        urgency=urgency,
        rationale=rationale,
        user_request_id=user_request_id,
    )


def validate_trade_plan(
    steps: Annotated[
        list[TradePlanStepInput],
        Field(
            description=(
                "Orders of the plan IN EXECUTION SEQUENCE (sells that free cash first). "
                "Each: instrument_uid, direction (BUY|SELL), quantity_lots, optional "
                "limit_price (bonds: % of nominal; omit to value at the market price)."
            ),
            min_length=1,
        ),
    ],
) -> TradePlanPreview:
    """PLAN-LEVEL risk preview — validates the whole rebalance plan, places nothing.

    Simulates the sequence on the live portfolio and checks the RESULTING portfolio
    against the PERSONAL mandate (saved investment profile), not the global limits:
      - ``PLAN_STEP_CASH`` / ``PLAN_STEP_POSITION`` — every step is affordable in
        sequence (enough cash for buys, enough held units for sells)
      - ``PLAN_ALLOCATION_MANDATE`` — post-plan bonds/equity/cash within the
        target allocation ± rebalance threshold
      - ``PLAN_MIN_CASH`` — cash does not drop below the mandate floor
      - ``PLAN_ISSUER_LIMIT`` / ``PLAN_SECTOR_LIMIT`` — no issuer/sector above its cap

    Requires a saved profile (errors otherwise). Use BEFORE proposing a multi-order
    rebalance: if a check fails, rework the plan (fewer lots, different instrument,
    reorder steps so sells come first) and validate again. Once ``all_passed`` is
    true, run ``create_order_proposal`` per step — per-order checks still apply."""
    return _guard(services.validate_trade_plan, _adapter(), get_settings(), steps=steps)


def create_trade_plan(
    items: Annotated[
        list[TradePlanItemInput],
        Field(
            description=(
                "Trade intents from the gap analysis, in any order. Each: instrument_uid, "
                "action (BUY|SELL) and EITHER quantity_lots OR amount (money in rubles — "
                "the server converts amounts to whole lots at current prices, НКД included). "
                "The server re-sequences legs SELL-first automatically."
            ),
            min_length=1,
        ),
    ],
    user_request_id: Annotated[str | None, Field(description="Caller correlation id.", default=None)] = None,
) -> TradePlan:
    """STAGE 6: assemble the full rebalance BASKET — one coherent plan, no orders placed.

    Takes the intents from the gap analysis («sell 10 lots of X, buy ОФЗ-1 for 50k…»)
    and returns a sequenced plan the user can judge as a whole:
      - amounts are converted to WHOLE LOTS at current bid/ask prices
      - SELL legs come first (their proceeds fund the buys); running ``cash_after`` per leg
      - every leg carries ``commission``, ``accrued_interest`` (НКД) and, for sells,
        ``tax`` — estimated НДФЛ from FIFO tax lots rebuilt from the operation
        history, with the 3-year ЛДВ exemption
      - ``allocation_preview`` — asset-class % before/after vs the target
      - ``cost_benefit`` — deterministic verdict: ``WORTH_IT`` or ``NOT_WORTH_IT``
        (drift below the threshold, or costs eat the benefit → honestly say "do nothing")
      - ``plan_checks`` — the stage-7 engine run (cash/position feasibility per step,
        mandate band, cash floor, issuer/sector caps) plus cost-inclusive cash and
        daily-turnover checks

    Requires a saved investment profile. Statuses: ``READY_FOR_CONFIRMATION`` (show the
    plan and ask the user), ``READY_WITH_WARNINGS`` (confirmable, but show the soft
    mandate warnings first — ``plan_checks`` with ``severity='warning'`` — and confirm
    with ``acknowledge_warnings=true``), ``NOT_WORTH_IT`` (recommend doing nothing, show
    why), ``RISK_REJECTED`` (read the blocking ``plan_checks`` with ``severity='error'``),
    ``EMPTY`` (see ``skipped`` reasons).
    Present the WHOLE plan to the user — not isolated orders. Then follow both gates:
    the UI calls ``confirm_trade_plan(plan_id)`` after whole-plan approval; use
    ``preview_plan_step(plan_id)`` for each execution card; the UI calls
    ``execute_plan_step(plan_id)`` only after that card is approved. The agent never
    confirms or executes autonomously. Plan confirmation expires at ``expires_at``."""
    return _guard(
        services.create_trade_plan,
        _adapter(),
        get_settings(),
        items=items,
        user_request_id=user_request_id,
    )


def confirm_trade_plan(
    plan_id: Annotated[
        str,
        Field(description="Plan id returned by create_trade_plan."),
    ],
    acknowledge_warnings: Annotated[
        bool,
        Field(
            description=(
                "Set true ONLY after the user has seen and accepted the soft mandate "
                "warnings of a READY_WITH_WARNINGS plan (per-issuer/sector concentration "
                "or allocation band). Ignored for a clean READY_FOR_CONFIRMATION plan."
            ),
        ),
    ] = False,
) -> TradePlanState:
    """GATE 1 — record the user's explicit confirmation of the WHOLE plan.

    This action belongs to the UI button «Подтверждаю план»; the agent must not
    infer confirmation from conversation or call it autonomously. The server
    rejects expired, risk-rejected and NOT_WORTH_IT plans. It places no order.

    A ``READY_WITH_WARNINGS`` plan (soft mandate breaches only) is confirmable, but
    the user must first see the warnings; pass ``acknowledge_warnings=true`` to
    record that acceptance. A blocking ``RISK_REJECTED`` plan can never be confirmed.

    Next: call ``preview_plan_step(plan_id)`` to build the first execution card.
    """
    return _guard(
        services.confirm_trade_plan,
        _adapter(),
        get_settings(),
        plan_id,
        acknowledge_warnings=acknowledge_warnings,
    )


def preview_plan_step(
    plan_id: Annotated[
        str,
        Field(description="Already confirmed plan id; no raw order fields are accepted."),
    ],
    urgency: Annotated[
        Literal["patient", "balanced", "fast"] | None,
        Field(
            description=(
                "Price/speed tier for the NEXT immutable plan leg. Default balanced. "
                "This changes only the fresh preview; instrument and quantity remain server-owned."
            ),
            default=None,
        ),
    ] = None,
) -> PlanStepPreview:
    """Read-only execution card for the next plan leg (Gate 2 preview).

    Runs a fresh remaining-plan risk-check and a fresh per-order risk preview.
    It never places an order. Show the returned card to the user and wait for a
    separate execution confirmation before ``execute_plan_step(plan_id)``.
    """
    return _guard(
        services.preview_plan_step,
        _adapter(),
        get_settings(),
        plan_id,
        urgency=urgency,
    )


def execute_plan_step(
    plan_id: Annotated[
        str,
        Field(description="Confirmed plan id; the server owns every order parameter."),
    ],
) -> PlanExecutionResult:
    """GATE 2 — execute exactly the next user-confirmed execution card.

    The UI invokes this only after the user confirms the current step preview;
    the agent never invokes it autonomously. Input is only ``plan_id``. Before
    sending, the server repeats whole-plan and per-order risk checks. Calls are
    idempotent: if the step is already SUBMITTED, the same order is polled and
    no duplicate is created. BUY remains blocked until all financing SELL steps
    are FILLED. No following leg is submitted automatically.
    """
    return _guard(services.execute_plan_step, _adapter(), get_settings(), plan_id)


def get_plan_state(
    plan_id: Annotated[str, Field(description="Plan id to inspect.")],
    refresh: Annotated[
        bool,
        Field(description="Poll the active broker order before returning (default true).", default=True),
    ] = True,
) -> TradePlanState:
    """Plan status with every leg as FILLED/SUBMITTED/PENDING/SKIPPED/etc.

    A SUBMITTED, partial, cancelled or rejected step pauses the plan. Explain
    the returned ``paused_reason`` and offer to wait, cancel, or generate a new
    preview with a different urgency. Never continue to the next leg silently.
    """
    return _guard(
        services.get_plan_state,
        _adapter(),
        get_settings(),
        plan_id,
        refresh=refresh,
    )


def cancel_trade_plan(
    plan_id: Annotated[str, Field(description="Plan id whose active order/remainder the user cancelled.")],
) -> TradePlanState:
    """Cancel the active plan order when possible and mark every remaining leg SKIPPED.

    This is a user-confirmed UI action. It never liquidates or compensates for
    already FILLED/partially-filled trades.
    """
    return _guard(services.cancel_trade_plan, _adapter(), get_settings(), plan_id)


def verify_trade_plan(
    plan_id: Annotated[str, Field(description="Completed/cancelled plan id to verify.")],
) -> TradePlanVerificationReport:
    """STAGE 10 — fresh portfolio snapshot and before/after execution report.

    Returns current allocation, target, drift reduction and costs together with
    ``get_portfolio_summary`` + ``get_portfolio_analytics`` evidence. Taxes stay
    explicitly estimated until the broker tax report is available.
    """
    return _guard(services.verify_trade_plan, _adapter(), get_settings(), plan_id)


def log_recommendation(
    plan_id: Annotated[str, Field(description="Plan id this recommendation explains.")],
    rationale: Annotated[
        str,
        Field(description="Why the selected instruments fit the mandate and were preferred."),
    ],
    alternatives_considered: Annotated[
        list[str] | None,
        Field(description="Short list of considered alternatives and why they lost.", default=None),
    ] = None,
) -> dict[str, object]:
    """Append the recommendation rationale to the audit journal (no order is placed)."""
    return _guard(
        services.log_recommendation,
        get_settings(),
        plan_id,
        rationale=rationale,
        alternatives_considered=alternatives_considered,
    )


# ---------------------------------------------------------------------------
# Investment profile & target allocation (advisory stage 3 — before picking
# instruments). Percentages come from a deterministic rule table in
# tinvest_mcp.allocation, never from the LLM.
# ---------------------------------------------------------------------------


def propose_target_allocation(
    risk_profile: Annotated[
        RiskProfile,
        Field(description="User's risk tolerance: conservative | moderate | aggressive.", examples=["conservative"]),
    ],
    horizon: Annotated[
        InvestmentHorizon,
        Field(description="Investment horizon: short (<1y) | medium (1-3y) | long (>3y).", examples=["medium"]),
    ],
) -> TargetAllocation:
    """Deterministic asset-class mix for a risk profile and horizon.

    Pure rule-table lookup — the same inputs always return the same percentages.
    Explain the result to the user in plain language (bonds = anchor, equity =
    growth, cash = buffer) and get their confirmation, then persist it with
    ``save_investment_profile``. Do NOT invent or adjust percentages yourself;
    if the user insists on different numbers, pass them as ``custom_allocation``
    to ``save_investment_profile``."""
    return propose_allocation(risk_profile, horizon)


def save_investment_profile(
    risk_profile: Annotated[
        RiskProfile,
        Field(description="Confirmed risk tolerance: conservative | moderate | aggressive.", examples=["conservative"]),
    ],
    horizon: Annotated[
        InvestmentHorizon,
        Field(description="Confirmed horizon: short (<1y) | medium (1-3y) | long (>3y).", examples=["medium"]),
    ],
    custom_allocation: Annotated[
        dict[str, int] | None,
        Field(
            description=(
                "ONLY when the user explicitly requested percentages different from the "
                "rule table: asset_class -> % (bonds/equity/cash), must sum to 100. "
                "Omit to use the deterministic rule-table allocation."
            ),
            default=None,
            examples=[{"bonds": 60, "equity": 30, "cash": 10}],
        ),
    ] = None,
    excluded_sectors: Annotated[
        list[str] | None,
        Field(
            description=(
                "MANDATE: sectors the user does not want to hold (e.g. because of ethics or "
                "overexposure at work). Mandate-aware screeners (apply_mandate=true) drop them."
            ),
            default=None,
            examples=[["it", "energy"]],
        ),
    ] = None,
    max_bond_risk_level: Annotated[
        Literal["low", "moderate", "high"] | None,
        Field(
            description=(
                "MANDATE: cap on bond issuer risk tier. Omit to derive from risk_profile "
                "(conservative→low, moderate→moderate, aggressive→high)."
            ),
            default=None,
        ),
    ] = None,
    min_cash_pct: Annotated[
        str | None,
        Field(
            description=(
                "MANDATE: cash floor for plan-level checks, % of portfolio (e.g. '5'). "
                "Omit to derive: max(0, target cash % - rebalance threshold)."
            ),
            default=None,
            examples=["5"],
        ),
    ] = None,
    max_issuer_weight_pct: Annotated[
        str | None,
        Field(
            description="MANDATE: per-issuer weight cap, % of portfolio (e.g. '15'). Omit for the server default.",
            default=None,
            examples=["15"],
        ),
    ] = None,
    max_sector_weight_pct: Annotated[
        str | None,
        Field(
            description="MANDATE: per-sector weight cap, % of portfolio (e.g. '30'). Omit for the server default.",
            default=None,
            examples=["30"],
        ),
    ] = None,
    allow_fx_linked: Annotated[
        bool | None,
        Field(
            description=(
                "MANDATE: set true ONLY after the user has knowingly agreed to non-ruble "
                "exposure (yuan-linked bonds and the like — their yields are foreign-currency "
                "yields, so the position is partly a bet on the exchange rate). Left unset, a "
                "plan that adds such an instrument raises a CURRENCY_EXPOSURE warning the user "
                "must acknowledge."
            ),
            default=None,
        ),
    ] = None,
    max_fx_exposure_pct: Annotated[
        str | None,
        Field(
            description=(
                "MANDATE: cap on the share of the portfolio denominated in foreign currency, "
                "% of portfolio (e.g. '20'). Meaningful with allow_fx_linked=true; omit for "
                "the server default."
            ),
            default=None,
            examples=["20"],
        ),
    ] = None,
    notes: Annotated[
        str | None,
        Field(
            description="Short free-form context, e.g. the user's goal.",
            default=None,
            examples=["Goal: down payment in ~2 years."],
        ),
    ] = None,
) -> InvestmentProfile:
    """Persist the USER-CONFIRMED profile and target allocation.

    Call only after the user agreed to the allocation shown from
    ``propose_target_allocation``. Overwrites any previously saved profile.
    The saved allocation is the reference point for structuring purchases,
    for measuring drift against ``get_portfolio_analytics`` and for the
    mandate applied by screeners with ``apply_mandate=true``."""
    settings = get_settings()
    if custom_allocation is not None:
        try:
            normalized = validate_allocation(custom_allocation)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        target = propose_allocation(risk_profile, horizon).model_copy(
            update={
                "allocation": normalized,
                "source": "custom",
                "rationale": "User-confirmed custom allocation (overrides the rule table).",
            }
        )
    else:
        target = propose_allocation(risk_profile, horizon)

    profile = InvestmentProfile(
        risk_profile=risk_profile,
        horizon=horizon,
        target_allocation=target,
        excluded_sectors=[s.lower() for s in (excluded_sectors or [])],
        max_bond_risk_level=max_bond_risk_level,
        min_cash_pct=_to_decimal(min_cash_pct, "min_cash_pct") if min_cash_pct is not None else None,
        max_issuer_weight_pct=(
            _to_decimal(max_issuer_weight_pct, "max_issuer_weight_pct") if max_issuer_weight_pct is not None else None
        ),
        max_sector_weight_pct=(
            _to_decimal(max_sector_weight_pct, "max_sector_weight_pct") if max_sector_weight_pct is not None else None
        ),
        allow_fx_linked=allow_fx_linked,
        max_fx_exposure_pct=(
            _to_decimal(max_fx_exposure_pct, "max_fx_exposure_pct") if max_fx_exposure_pct is not None else None
        ),
        notes=notes,
        saved_at=datetime.now(UTC),
    )
    profile_store.save_profile(settings.investment_profile_path, profile)
    audit_event(
        "save_investment_profile",
        status="confirmed",
        account_id=settings.account_id,
        metadata={
            "risk_profile": risk_profile,
            "horizon": horizon,
            "allocation": target.allocation,
            "source": target.source,
            "excluded_sectors": profile.excluded_sectors,
            "max_bond_risk_level": max_bond_risk_level,
            "min_cash_pct": min_cash_pct,
            "max_issuer_weight_pct": max_issuer_weight_pct,
            "max_sector_weight_pct": max_sector_weight_pct,
            "allow_fx_linked": allow_fx_linked,
            "max_fx_exposure_pct": max_fx_exposure_pct,
        },
    )
    return profile


def get_investment_profile() -> InvestmentProfile | None:
    """Return the saved investment profile and target allocation, or null.

    Null means the profile stage has not been completed yet — run
    ``propose_target_allocation`` and confirm with the user before advising
    on specific instruments."""
    settings = get_settings()
    return profile_store.load_profile(settings.investment_profile_path)


# ---------------------------------------------------------------------------
# Execution tools (gated — invoked only after explicit user confirmation)
# ---------------------------------------------------------------------------


def post_order(
    proposal_id: Annotated[
        str,
        Field(
            description="Proposal id returned by create_order_proposal.",
            examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
        ),
    ],
) -> OrderResult:
    """Execute a user-confirmed proposal (trusted UI/controller boundary).

    **The AI agent must never call this tool.** The trusted UI/execution
    controller calls it only after the user explicitly confirmed the preview.
    The request carries only ``proposal_id``, never constructed order fields.

    Re-validates risk server-side, uses idempotency key, sends LIMIT order to broker.
    Typical first response: ``status=SUBMITTED``, ``lots_executed=0`` (order in book).
    Poll ``get_order_state`` until ``FILLED`` or explain why still ``SUBMITTED``
    (weekend, limit below ask, stale session)."""
    settings = get_settings()
    return _guard(services.post_order, _adapter(), settings, proposal_id)


def get_order_state(
    proposal_id: Annotated[
        str, Field(description="Proposal id whose order to inspect.", examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"])
    ],
) -> OrderResult:
    """Poll execution status after post_order (playbook step 7).

    Key fields:
      - ``status``: SUBMITTED | PARTIALLY_FILLED | FILLED | CANCELLED | REJECTED
      - ``lots_executed`` / ``lots_requested``: 0 executed = not bought yet
      - ``executed_price``, ``total_amount``, ``commission``: meaningful after fill

    ``SUBMITTED`` with ``lots_executed=0`` is normal outside trading hours or when
    limit is below best ask. Use ``cancel_order`` if user aborts."""
    settings = get_settings()
    return _guard(services.get_order_state, _adapter(), settings, proposal_id)


def list_executing_orders(
    refresh: Annotated[
        bool,
        Field(
            description="When true (default), poll the broker for each in-flight order before returning.",
            default=True,
        ),
    ] = True,
) -> list[ExecutingOrderSummary]:
    """List all proposals currently at the execution stage (broker order in flight).

    Includes ``SUBMITTED``, ``PARTIALLY_FILLED``, ``SUBMITTING``, and
    ``UNKNOWN_REQUIRES_RECONCILIATION``. Terminal orders (``FILLED``, ``CANCELLED``,
    ``REJECTED``) are omitted. Use to see every open order without polling each
    ``proposal_id`` individually."""
    settings = get_settings()
    return _guard(services.list_executing_orders, _adapter(), settings, refresh=refresh)


def cancel_order(
    proposal_id: Annotated[
        str, Field(description="Proposal id whose order to cancel.", examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"])
    ],
) -> OrderResult:
    """Cancel an unfilled broker order linked to a proposal.

    Use when ``get_order_state`` shows ``SUBMITTED`` (or partial) and the user
    no longer wants the trade. Returns ``status=CANCELLED``."""
    settings = get_settings()
    return _guard(services.cancel_order, _adapter(), settings, proposal_id)


# ---------------------------------------------------------------------------
# Sandbox-only helpers (no real-money equivalents are exposed)
# ---------------------------------------------------------------------------


def open_sandbox_account(
    name: Annotated[
        str, Field(description="Display name for the sandbox account.", default="ai-treasury-sandbox")
    ] = "ai-treasury-sandbox",
) -> dict[str, Any]:
    """Create a sandbox brokerage account (sandbox mode only)."""
    account_id = _guard(_adapter().open_sandbox_account, name)
    return {"account_id": account_id, "mode": "sandbox"}


def sandbox_pay_in(
    account_id: Annotated[
        str, Field(description="Sandbox account id.", examples=["750c962a-d318-4cb3-b662-7a66aa17c6ee"])
    ],
    amount: Annotated[str, Field(description="Amount to add as a decimal STRING, e.g. '50000'.", examples=["100000"])],
    currency: Annotated[str, Field(description="Currency code.", default="rub")] = "rub",
) -> dict[str, Any]:
    """Fund a sandbox account with virtual money (sandbox mode only)."""
    value = _to_decimal(amount, "amount")
    _guard(_adapter().sandbox_pay_in, account_id, value, currency)
    return {"account_id": account_id, "paid_in": amount, "currency": currency, "mode": "sandbox"}
