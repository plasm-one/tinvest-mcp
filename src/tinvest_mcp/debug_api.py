"""Debug REST API for hand-testing the T-Invest tools through Swagger.

DEVELOPMENT ONLY. This is NOT part of the MCP protocol and must never be
exposed: it is a thin REST wrapper around the same ``services`` layer the MCP
tools use, with no authentication, so anything that reaches the port can trade
with your token. It binds ``127.0.0.1`` for that reason — keep it that way.

Requires the ``debug-api`` extra::

    uv sync --extra debug-api
    uv run python -m tinvest_mcp.debug_api
    # or: uv run uvicorn tinvest_mcp.debug_api:app --reload --port 8099

Then open http://127.0.0.1:8099/docs

Mode and token come from the same ``config.toml`` / ``.env`` the MCP server
uses — see ``docs/configuration.md``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from . import services
from .adapter import TInvestAdapter
from .config.env import get_settings, mask
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
    InvestmentInstrument,
    MarketSnapshot,
    OperationsPage,
    OrderPreview,
    OrderResult,
    PortfolioAnalytics,
    PortfolioSummary,
    ShareScreenHit,
)
from .startup_checks import startup_summary

app = FastAPI(
    title="T-Invest MCP — DEBUG REST API",
    version="0.1.0",
    description=(
        "Development-only REST wrapper around the tinvest-mcp tools for manual "
        "testing. Mode and token are read from config.toml / .env, exactly as the "
        "MCP server reads them. Default mode is sandbox; real trading stays "
        "behind [tinvest].enable_real_trading."
    ),
)


# Real, stable instrument UIDs (global — identical in sandbox and prod) used as
# ready-to-run Swagger examples.
EX_SHARE_UID = "e6123145-9665-43e0-8413-cd61b8aa9b13"  # SBER (share)
EX_BOND_UID = "33672905-be3c-4a02-a1c3-4be155814bb5"  # RU000A0ZYX28 (bond, ~98 RUB/lot)
EX_ETF_UID = "555dcd42-2c14-43d5-ba93-8a4a42160638"  # AMRE (etf)
EX_PROPOSAL_ID = "paste-proposal_id-from-POST-/create_order_proposal"
EX_ACCOUNT_ID = "750c962a-d318-4cb3-b662-7a66aa17c6ee"  # example sandbox account id


def _adapter() -> TInvestAdapter:
    return TInvestAdapter(get_settings())


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except TInvestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface the message for debugging
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


def _to_decimal(value: str, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid decimal for '{field}': {value!r}") from exc


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class ProposalBody(BaseModel):
    instrument_uid: str = Field(
        ..., description="Instrument UID (from /list_bonds, /list_shares, /list_etfs or /search_instruments)"
    )
    direction: str = Field("BUY", description="Only BUY is allowed in this MVP")
    order_type: str = Field("LIMIT", description="Only LIMIT is allowed in this MVP")
    quantity_lots: int = Field(1, ge=1)
    limit_price: str | None = Field(
        None,
        description="Optional limit in quote unit (% of nominal for bonds). Omit to use urgency.",
    )
    urgency: str | None = Field(
        None,
        description="patient | balanced | fast — used when limit_price is omitted (default: balanced).",
    )
    rationale: str | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "instrument_uid": EX_BOND_UID,
                    "direction": "BUY",
                    "order_type": "LIMIT",
                    "quantity_lots": 1,
                    "urgency": "fast",
                    "rationale": "Conservative short bond for the stability sleeve",
                },
                {
                    "instrument_uid": EX_BOND_UID,
                    "direction": "BUY",
                    "order_type": "LIMIT",
                    "quantity_lots": 1,
                    "limit_price": "98.0",
                    "rationale": "Explicit limit price",
                },
            ]
        }
    )


class PayInBody(BaseModel):
    amount: str = Field("100000", description="Amount as a decimal string, e.g. '100000'")
    currency: str = "rub"

    model_config = ConfigDict(json_schema_extra={"examples": [{"amount": "100000", "currency": "rub"}]})


class SandboxAccountBody(BaseModel):
    name: str = "ai-treasury-debug"

    model_config = ConfigDict(json_schema_extra={"examples": [{"name": "ai-treasury-debug"}]})


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@app.get("/health", tags=["diagnostics"])
def health() -> dict:
    s = get_settings()
    return {
        "ok": True,
        "mode": s.mode,
        "real_trading_enabled": s.real_trading_enabled,
        "account_id": mask(s.account_id),
        "read_token_present": bool(s.active_read_token()),
        "trade_token_present": bool(s.active_trade_token()),
    }


@app.get("/startup-checks", tags=["diagnostics"])
def startup_checks() -> dict:
    return startup_summary()


# ---------------------------------------------------------------------------
# Read / research
# ---------------------------------------------------------------------------


@app.get("/get_accounts", response_model=list[BrokerAccount], tags=["read"])
def get_accounts() -> list[BrokerAccount]:
    return _guard(services.list_accounts, _adapter(), get_settings())


@app.get("/get_portfolio_summary", response_model=PortfolioSummary, tags=["read"])
def get_portfolio_summary() -> PortfolioSummary:
    return _guard(services.get_portfolio_summary, _adapter(), get_settings())


@app.get("/get_operations", response_model=OperationsPage, tags=["read"])
def get_operations(
    from_date: Annotated[str | None, Query(examples=["2026-01-01"])] = None,
    to_date: Annotated[str | None, Query(examples=["2026-06-30"])] = None,
    operation_types: Annotated[str | None, Query(description="Comma-separated: buy,sell,coupon,dividend")] = None,
    instrument_uid: Annotated[str | None, Query(examples=[EX_SHARE_UID])] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=3, le=1000)] = 100,
    include_canceled: bool = False,
) -> OperationsPage:
    types = [t.strip() for t in operation_types.split(",")] if operation_types else None
    parsed_from = date.fromisoformat(from_date) if from_date else None
    parsed_to = date.fromisoformat(to_date) if to_date else None
    return _guard(
        services.get_operations,
        _adapter(),
        get_settings(),
        from_date=parsed_from,
        to_date=parsed_to,
        operation_types=types,
        instrument_uid=instrument_uid,
        cursor=cursor,
        limit=limit,
        include_canceled=include_canceled,
    )


@app.get("/get_portfolio_analytics", response_model=PortfolioAnalytics, tags=["read"])
def get_portfolio_analytics(
    include_bond_metrics: bool = True,
    top_n: Annotated[int, Query(ge=1, le=20)] = 5,
) -> PortfolioAnalytics:
    return _guard(
        services.get_portfolio_analytics,
        _adapter(),
        get_settings(),
        include_bond_metrics=include_bond_metrics,
        top_n=top_n,
    )


@app.get("/search_instruments", response_model=list[InstrumentSearchHit], tags=["read"])
def search_instruments(
    query: Annotated[str, Query(examples=["SBER"], description="Ticker / name / ISIN")] = "SBER",
    instrument_types: Annotated[
        str | None, Query(examples=["share"], description="Comma-separated: share,bond,etf")
    ] = "share",
    api_trade_available: bool = True,
    qualified_only: bool = False,
    limit: Annotated[int, Query(examples=[20])] = 20,
) -> list[InstrumentSearchHit]:
    types = [t.strip() for t in instrument_types.split(",")] if instrument_types else None
    return _guard(
        services.search_instruments,
        _adapter(),
        query,
        instrument_types=types,
        api_trade_available=api_trade_available,
        qualified_only=qualified_only,
        limit=limit,
    )


@app.get("/list_bonds", response_model=list[BondScreenHit], tags=["read"])
def list_bonds(
    currency: Annotated[str | None, Query(examples=["rub"])] = "rub",
    api_trade_available: bool = True,
    qualified_only: bool = False,
    risk_level: Annotated[Literal["low", "moderate", "high"] | None, Query(examples=["low"])] = None,
    max_maturity_years: Annotated[float | None, Query(examples=[3])] = None,
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
        Query(examples=["ytm"]),
    ] = None,
    descending: bool = True,
    include_analytics: bool = False,
    analytics_limit: Annotated[int, Query(examples=[25])] = 25,
    limit: Annotated[int, Query(examples=[20])] = 100,
) -> list[BondScreenHit]:
    """Screen the bond catalogue."""
    return _guard(
        services.list_bonds,
        _adapter(),
        settings=get_settings(),
        currency=currency,
        api_trade_available=api_trade_available,
        qualified_only=qualified_only,
        risk_level=risk_level,
        max_maturity_years=max_maturity_years,
        sort_by=sort_by,
        descending=descending,
        include_analytics=include_analytics,
        analytics_limit=analytics_limit,
        limit=limit,
    )


@app.get("/list_shares", response_model=list[ShareScreenHit], tags=["read"])
def list_shares(
    currency: Annotated[str | None, Query(examples=["rub"])] = "rub",
    api_trade_available: bool = True,
    qualified_only: bool = False,
    sector: Annotated[str | None, Query(examples=["financial"])] = None,
    pays_dividends: Annotated[bool | None, Query()] = None,
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
        Query(examples=["recommendation"]),
    ] = None,
    descending: bool = True,
    include_analytics: bool = False,
    include_forecast: Annotated[
        bool, Query(description="Attach analyst consensus (rating/target/upside) via one bulk sweep.")
    ] = False,
    include_fundamentals: Annotated[
        bool,
        Query(description="Attach key fundamentals (P/E, P/B, ROE, market cap, EV/EBITDA) for the top candidates."),
    ] = False,
    analytics_limit: Annotated[int, Query(examples=[25])] = 25,
    limit: Annotated[int, Query(examples=[20])] = 100,
) -> list[ShareScreenHit]:
    """Screen the share catalogue. Set include_forecast (or sort by
    recommendation/upside) for the analyst consensus, and include_fundamentals
    (or sort by pe/pb/roe/market_cap/ev_ebitda) for valuation ratios."""
    return _guard(
        services.list_shares,
        _adapter(),
        settings=get_settings(),
        currency=currency,
        api_trade_available=api_trade_available,
        qualified_only=qualified_only,
        sector=sector,
        pays_dividends=pays_dividends,
        sort_by=sort_by,
        descending=descending,
        include_analytics=include_analytics,
        include_forecast=include_forecast,
        include_fundamentals=include_fundamentals,
        analytics_limit=analytics_limit,
        limit=limit,
    )


@app.get("/list_etfs", response_model=list[EtfScreenHit], tags=["read"])
def list_etfs(
    currency: Annotated[str | None, Query(examples=["rub"])] = "rub",
    api_trade_available: bool = True,
    qualified_only: bool = False,
    sector: Annotated[str | None, Query()] = None,
    focus_type: Annotated[str | None, Query(examples=["equity"])] = None,
    sort_by: Annotated[
        Literal["name", "ticker", "price", "commission", "focus", "released", "return", "volatility", "drawdown"]
        | None,
        Query(examples=["commission"]),
    ] = None,
    descending: bool = True,
    include_analytics: bool = False,
    analytics_limit: Annotated[int, Query(examples=[25])] = 25,
    limit: Annotated[int, Query(examples=[20])] = 100,
) -> list[EtfScreenHit]:
    """Screen the ETF / fund catalogue."""
    return _guard(
        services.list_etfs,
        _adapter(),
        settings=get_settings(),
        currency=currency,
        api_trade_available=api_trade_available,
        qualified_only=qualified_only,
        sector=sector,
        focus_type=focus_type,
        sort_by=sort_by,
        descending=descending,
        include_analytics=include_analytics,
        analytics_limit=analytics_limit,
        limit=limit,
    )


@app.get("/get_instrument_details/{instrument_uid}", response_model=InvestmentInstrument, tags=["read"])
def get_instrument_details(
    instrument_uid: Annotated[str, Path(examples=[EX_SHARE_UID], description="SBER share in this example")],
) -> InvestmentInstrument:
    return _guard(services.load_instrument, _adapter(), instrument_uid)


@app.get("/get_instrument_analytics/{instrument_uid}", response_model=InstrumentAnalytics, tags=["read"])
def get_instrument_analytics(
    instrument_uid: Annotated[str, Path(examples=[EX_BOND_UID], description="RU000A0ZYX28 bond in this example")],
) -> InstrumentAnalytics:
    """Yield & risk signals (historical return/vol/drawdown, bond/share yields)."""
    return _guard(services.get_instrument_analytics, _adapter(), get_settings(), instrument_uid)


@app.get("/get_instrument_forecast/{instrument_uid}", response_model=AnalystForecast, tags=["read"])
def get_instrument_forecast(
    instrument_uid: Annotated[str, Path(examples=[EX_SHARE_UID], description="SBER share in this example")],
) -> AnalystForecast:
    """Analyst consensus rating + target prices (buy/hold/sell, target, upside %). Best for shares."""
    return _guard(services.get_instrument_forecast, _adapter(), get_settings(), instrument_uid)


@app.get("/get_instrument_fundamentals/{instrument_uid}", response_model=InstrumentFundamentals, tags=["read"])
def get_instrument_fundamentals(
    instrument_uid: Annotated[str, Path(examples=[EX_SHARE_UID], description="SBER share in this example")],
) -> InstrumentFundamentals:
    """Company financials & ratios (P/E, P/B, ROE/ROA/ROIC, margins, debt, dividends, growth)."""
    return _guard(services.get_instrument_fundamentals, _adapter(), get_settings(), instrument_uid)


@app.get("/get_etf_details/{instrument_uid}", response_model=EtfDetails, tags=["read"])
def get_etf_details(
    instrument_uid: Annotated[str, Path(examples=[EX_ETF_UID], description="An ETF UID")],
) -> EtfDetails:
    """Extended ETF metadata: TER/fees, benchmark, strategy, tracking error."""
    return _guard(services.get_etf_details, _adapter(), get_settings(), instrument_uid)


@app.get("/get_bond_schedule/{instrument_uid}", response_model=BondSchedule, tags=["read"])
def get_bond_schedule(
    instrument_uid: Annotated[str, Path(examples=[EX_BOND_UID], description="A bond UID")],
) -> BondSchedule:
    """Full upcoming coupon schedule + call/offer & maturity events for one bond."""
    return _guard(services.get_bond_schedule, _adapter(), get_settings(), instrument_uid)


@app.get("/get_market_snapshot/{instrument_uid}", response_model=MarketSnapshot, tags=["read"])
def get_market_snapshot(
    instrument_uid: Annotated[str, Path(examples=[EX_SHARE_UID])],
) -> MarketSnapshot:
    return _guard(services.get_market_snapshot, _adapter(), get_settings(), instrument_uid)


# ---------------------------------------------------------------------------
# Propose -> execute
# ---------------------------------------------------------------------------


@app.post("/create_order_proposal", response_model=OrderPreview, tags=["orders"])
def create_order_proposal(body: ProposalBody) -> OrderPreview:
    """Build risk-checked preview. Prefer ``urgency`` over manual ``limit_price``.
    See tinvest-mcp README «Процесс покупки актива» for the full flow."""
    limit = _to_decimal(body.limit_price, "limit_price") if body.limit_price is not None else None
    return _guard(
        services.create_order_proposal,
        _adapter(),
        get_settings(),
        instrument_uid=body.instrument_uid,
        direction=body.direction,
        order_type=body.order_type,
        quantity_lots=body.quantity_lots,
        limit_price=limit,
        urgency=body.urgency,
        rationale=body.rationale,
    )


_PROPOSAL_PATH = Annotated[
    str,
    Path(examples=[EX_PROPOSAL_ID], description="proposal_id returned by POST /create_order_proposal"),
]


@app.post("/post_order/{proposal_id}", response_model=OrderResult, tags=["orders"])
def post_order(proposal_id: _PROPOSAL_PATH) -> OrderResult:
    """Execute a confirmed proposal (sandbox always; real only behind the flag)."""
    return _guard(services.post_order, _adapter(), get_settings(), proposal_id)


@app.get("/get_order_state/{proposal_id}", response_model=OrderResult, tags=["orders"])
def get_order_state(proposal_id: _PROPOSAL_PATH) -> OrderResult:
    return _guard(services.get_order_state, _adapter(), get_settings(), proposal_id)


@app.get("/list_executing_orders", response_model=list[ExecutingOrderSummary], tags=["orders"])
def list_executing_orders(
    refresh: Annotated[
        bool,
        Query(description="Poll broker for each in-flight order before returning.", examples=[True]),
    ] = True,
) -> list[ExecutingOrderSummary]:
    """List proposals at the execution stage (SUBMITTED, PARTIALLY_FILLED, …)."""
    return _guard(services.list_executing_orders, _adapter(), get_settings(), refresh=refresh)


@app.post("/cancel_order/{proposal_id}", response_model=OrderResult, tags=["orders"])
def cancel_order(proposal_id: _PROPOSAL_PATH) -> OrderResult:
    return _guard(services.cancel_order, _adapter(), get_settings(), proposal_id)


# ---------------------------------------------------------------------------
# Sandbox-only helpers
# ---------------------------------------------------------------------------


@app.post("/open_sandbox_account", tags=["sandbox"])
def open_sandbox_account(body: SandboxAccountBody) -> dict:
    account_id = _guard(_adapter().open_sandbox_account, body.name)
    return {"account_id": account_id, "mode": "sandbox"}


@app.post("/sandbox_pay_in/{account_id}", tags=["sandbox"])
def sandbox_pay_in(
    account_id: Annotated[str, Path(examples=[EX_ACCOUNT_ID], description="sandbox account id from GET /get_accounts")],
    body: PayInBody,
) -> dict:
    _guard(_adapter().sandbox_pay_in, account_id, _to_decimal(body.amount, "amount"), body.currency)
    return {"account_id": account_id, "paid_in": body.amount, "currency": body.currency}


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8099)


if __name__ == "__main__":
    main()
