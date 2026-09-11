"""High-level orchestration used by the MCP tools.

Each function returns normalized Pydantic models (never raw SDK objects) and
keeps the risk-engine / proposal lifecycle in one place so the tool layer stays
thin.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from . import trade_plan as trade_plan_mod
from .adapter import TInvestAdapter
from .allocation import (
    PORTFOLIO_CLASS_TO_TARGET,
    IssuerCapPolicy,
    build_issuer_caps,
    compute_drift,
    issuer_cap_category,
    mandate_limits,
    scaled_issuer_cap,
    stricter_issuer_category,
)
from .audit import audit_event
from .config.env import Settings, mask
from .converters import (
    account_to_model,
    enum_name,
    instrument_to_model,
    operation_to_model,
    position_to_model,
    search_hit_to_model,
    snapshot_to_model,
)
from .errors import (
    TInvestAccountNotFoundError,
    TInvestConfigurationError,
    TInvestDataUnavailableError,
    TInvestInstrumentNotFoundError,
    TInvestPermissionError,
    TInvestProposalError,
    TInvestRealTradingDisabledError,
    broker_error_meta,
    format_broker_error,
    is_definitive_broker_reject,
)
from .fx import (
    BASE_CURRENCY,
    FxRate,
    currency_of,
    denomination_currency,
    fx_note,
    get_fx_rate,
    is_fx_linked,
    normalize_currency,
    to_rub,
)
from .money import money_to_decimal, quantize_to_increment, quotation_to_decimal
from .pricing_hints import (
    build_price_vs_hints,
    compute_buy_price_hints,
    compute_sell_price_hints,
    resolve_buy_limit_price,
)
from .profile_store import load_profile
from .proposals import OrderProposal, get_store
from .risk_engine import (
    OrderContext,
    PlanState,
    PlanStep,
    all_passed,
    daily_turnover,
    evaluate,
    evaluate_plan,
    has_blocking_failure,
)
from .schemas import (
    ASSET_CLASSES,
    EXECUTION_STATUSES,
    AnalystForecast,
    AnalystTarget,
    BondCouponItem,
    BondEventItem,
    BondSchedule,
    BondScreenHit,
    BrokerAccount,
    BrokerOperation,
    EtfDetails,
    EtfScreenHit,
    ExecutingOrderSummary,
    InstrumentAnalytics,
    InstrumentFundamentals,
    InstrumentSearchHit,
    InvestmentInstrument,
    InvestmentProfile,
    LiquidityInfo,
    MarketSnapshot,
    OperationsPage,
    OperationsTotals,
    OrderPreview,
    OrderResult,
    PlanExecutionResult,
    PlannedTrade,
    PlanStepPreview,
    PlanStepState,
    PortfolioAnalytics,
    PortfolioPosition,
    PortfolioSummary,
    PriceVsHints,
    RiskCheck,
    SellTaxImpact,
    ShareScreenHit,
    SkippedPlanItem,
    TopPositionWeight,
    TradePlan,
    TradePlanAllocationPreview,
    TradePlanItemInput,
    TradePlanPreview,
    TradePlanState,
    TradePlanStepInput,
    TradePlanStepPreview,
    TradePlanVerificationReport,
    price_quote_unit_for,
)
from .sdk import OperationState, OperationType
from .session_calendar import MoexSessionState, moex_session_state

_ZERO = Decimal("0")
_HUNDRED_DEC = Decimal("100")
_ANALYTICS_RETRY_DELAY_SECONDS = 0.5
_ANALYTICS_RETRY_MAX_WORKERS = 2

logger = logging.getLogger(__name__)

_EXEC_STATUS_MAP = {
    "EXECUTION_REPORT_STATUS_FILL": "FILLED",
    "EXECUTION_REPORT_STATUS_PARTIALLYFILL": "PARTIALLY_FILLED",
    "EXECUTION_REPORT_STATUS_NEW": "SUBMITTED",
    "EXECUTION_REPORT_STATUS_REJECTED": "REJECTED",
    "EXECUTION_REPORT_STATUS_CANCELLED": "CANCELLED",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _current_session_state(settings: Settings) -> MoexSessionState | None:
    """MOEX session state for the wall clock, or None when the check is disabled."""
    if not settings.market_session_check_enabled:
        return None
    return moex_session_state(_now(), buffer_seconds=settings.market_session_close_buffer_seconds)


def _d(value: Decimal | None) -> Decimal:
    return value if value is not None else _ZERO


def _status_name(value) -> str:
    return getattr(value, "name", str(value))


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def list_accounts(adapter: TInvestAdapter, settings: Settings) -> list[BrokerAccount]:
    """Return read accounts enriched with independently verified execution access.

    ``GetAccounts`` is intentionally called with the research token first.  In
    production with real trading enabled, a second read-only capability probe is
    made through the trade token.  This prevents callers (and especially an LLM)
    from mistaking the research token's READ_ONLY level for an account-wide ban.
    """
    read_accounts = list(adapter.get_accounts())
    models = [account_to_model(account) for account in read_accounts]

    if settings.is_sandbox:
        for model in models:
            model.execution_access_level = model.research_access_level
            model.execution_available = True
        return models

    if not settings.real_trading_enabled:
        for model in models:
            model.execution_block_reason = "Real trading is disabled by configuration."
        return models

    if not settings.fullaccess_token:
        for model in models:
            model.execution_block_reason = "No full-access trade token is configured."
        return models

    try:
        trade_accounts = list(adapter.get_trade_accounts())
    except Exception as exc:  # noqa: BLE001 - capability failure must not break research
        audit_event(
            "trade_access_probe",
            status="failed",
            metadata={"error_type": type(exc).__name__},
        )
        for model in models:
            model.execution_block_reason = "The trade token capability check failed."
        return models

    trade_by_id = {str(account.id): account for account in trade_accounts}
    for model in models:
        trade_account = trade_by_id.get(model.id)
        if trade_account is None:
            model.execution_block_reason = "The trade token does not have access to this account."
            continue
        model.execution_access_level = enum_name(getattr(trade_account, "access_level", None))
        if "FULL_ACCESS" not in (model.execution_access_level or "").upper():
            model.execution_block_reason = "The trade token is not full-access for this account."
            continue
        model.execution_available = True

    return models


def _account_is_open(account) -> bool:
    status = getattr(getattr(account, "status", None), "name", "") or ""
    # Treat unknown/empty status as usable; only exclude explicitly closed ones.
    return "CLOSED" not in status.upper()


def resolve_account_id(adapter: TInvestAdapter, settings: Settings) -> str:
    """Resolve the account id from ``GetAccounts`` — NOT from env.

    Resolution order:
      1. If a token is account-scoped / has a single open account → use it
         automatically (the normal case for a dedicated sandbox/pilot account).
      2. ``[tinvest].account_id`` is an OPTIONAL override, only needed to
         disambiguate when a token can see several accounts.

    Never silently selects "the first of many": multiple visible accounts without
    an override is an error (and in prod with strict isolation it is blocked).
    """
    accounts = adapter.get_accounts()
    ids = [a.id for a in accounts]
    if not ids:
        hint = " Create one with open_sandbox_account." if settings.is_sandbox else ""
        raise TInvestAccountNotFoundError("No brokerage accounts are visible to this token." + hint)

    # Optional explicit override (kept for multi-account tokens).
    configured = settings.account_id
    if configured:
        if configured not in ids:
            audit_event(
                "account_isolation_violation",
                status="failed",
                account_id=configured,
                metadata={"visible_count": len(ids)},
            )
            raise TInvestAccountNotFoundError("Configured [tinvest].account_id is not visible to this token.")
        return configured

    # Auto-resolve from GetAccounts.
    open_accounts = [a for a in accounts if _account_is_open(a)] or accounts
    open_ids = [a.id for a in open_accounts]

    if len(open_ids) == 1:
        return open_ids[0]

    masked = ", ".join(mask(i) or "?" for i in open_ids)
    if settings.is_prod and settings.require_single_account_token:
        raise TInvestPermissionError(
            f"Token can see multiple accounts ({masked}) and strict single-account isolation is on. "
            "Use an account-scoped token, or set [tinvest].account_id in config.toml "
            "to disambiguate."
        )
    raise TInvestConfigurationError(
        f"Token sees {len(open_ids)} accounts ({masked}); cannot auto-resolve. "
        "Set [tinvest].account_id in config.toml to one of them to disambiguate."
    )


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------


def get_portfolio_summary(adapter: TInvestAdapter, settings: Settings) -> PortfolioSummary:
    account_id = resolve_account_id(adapter, settings)
    portfolio = adapter.get_portfolio(account_id)

    total = _d(money_to_decimal(getattr(portfolio, "total_amount_portfolio", None)))
    cash = _d(money_to_decimal(getattr(portfolio, "total_amount_currencies", None)))
    shares = _d(money_to_decimal(getattr(portfolio, "total_amount_shares", None)))
    bonds = _d(money_to_decimal(getattr(portfolio, "total_amount_bonds", None)))
    etf = _d(money_to_decimal(getattr(portfolio, "total_amount_etf", None)))

    def weight(part: Decimal) -> Decimal:
        return (part / total) if total > 0 else _ZERO

    positions: list[PortfolioPosition] = []
    largest = _ZERO
    for raw in getattr(portfolio, "positions", []) or []:
        pos = position_to_model(raw)
        if (pos.instrument_type or "").lower() == "currency":
            continue
        # The broker prices a foreign-currency position in that currency, while
        # total_value is the ruble account total — weighing one against the other
        # unconverted understates the position by the exchange rate.
        if is_fx_linked(pos.currency) and pos.current_value is not None:
            rate = get_fx_rate(adapter, pos.currency)
            converted = to_rub(pos.current_value, pos.currency, rate)
            if converted is not None:
                pos.current_value = converted
                pos.fx_rate_rub = rate.rate if rate else None
        positions.append(pos)
        if pos.current_value and total > 0:
            largest = max(largest, pos.current_value / total)

    return PortfolioSummary(
        mode=settings.mode,
        total_value=total,
        cash=cash,
        asset_allocation={
            "shares": weight(shares),
            "bonds": weight(bonds),
            "funds": weight(etf),
            "cash": weight(cash),
        },
        positions=positions,
        concentration={"largest_position_weight": largest},
    )


_FEE_OPERATION_TYPES = frozenset({"broker_fee", "service_fee", "margin_fee", "success_fee"})
_TAX_OPERATION_TYPES = frozenset({"tax", "dividend_tax", "bond_tax", "benefit_tax", "tax_correction"})
_BUY_OPERATION_TYPES = frozenset({"buy", "buy_card", "buy_margin", "delivery_buy"})
_SELL_OPERATION_TYPES = frozenset({"sell", "sell_card", "sell_margin", "delivery_sell"})

_CLASS_LABELS = {
    "share": "shares",
    "bond": "bonds",
    "etf": "funds",
    "currency": "cash",
}


def _resolve_operation_types(names: list[str] | None) -> list[OperationType] | None:
    if not names:
        return None
    resolved: list[OperationType] = []
    for raw in names:
        token = (raw or "").strip().upper().removeprefix("OPERATION_TYPE_")
        if not token:
            continue
        val = getattr(OperationType, f"OPERATION_TYPE_{token}", None)
        if val is not None:
            resolved.append(val)
    return resolved or None


def _accumulate_operation_totals(items: list[BrokerOperation]) -> OperationsTotals:
    commissions = _ZERO
    dividends = _ZERO
    coupons = _ZERO
    taxes = _ZERO
    trades_buy = _ZERO
    trades_sell = _ZERO
    for op in items:
        payment = op.payment or _ZERO
        commission = op.commission or _ZERO
        if op.type in _FEE_OPERATION_TYPES:
            commissions += abs(payment) + abs(commission)
        elif op.type == "dividend":
            dividends += payment
        elif op.type == "coupon":
            coupons += payment
        elif op.type in _TAX_OPERATION_TYPES:
            taxes += abs(payment)
        elif op.type in _BUY_OPERATION_TYPES:
            trades_buy += abs(payment)
            commissions += abs(commission)
        elif op.type in _SELL_OPERATION_TYPES:
            trades_sell += abs(payment)
            commissions += abs(commission)
        elif commission:
            commissions += abs(commission)
    return OperationsTotals(
        commissions=commissions,
        dividends=dividends,
        coupons=coupons,
        taxes=taxes,
        trades_buy=trades_buy,
        trades_sell=trades_sell,
    )


def get_operations(
    adapter: TInvestAdapter,
    settings: Settings,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    operation_types: list[str] | None = None,
    instrument_uid: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
    include_canceled: bool = False,
) -> OperationsPage:
    """Paginated brokerage operations: trades, fees, coupons, dividends, taxes."""
    account_id = resolve_account_id(adapter, settings)
    today = _now().date()
    end = to_date or today
    start = from_date or (end - timedelta(days=90))
    if start > end:
        raise TInvestConfigurationError("from_date must be on or before to_date")

    from_dt = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    to_dt = datetime.combine(end, datetime.max.time().replace(microsecond=0), tzinfo=UTC)
    state = None if include_canceled else OperationState.OPERATION_STATE_EXECUTED
    resp = adapter.get_operations_by_cursor(
        account_id,
        from_=from_dt,
        to=to_dt,
        cursor=cursor or "",
        limit=limit,
        operation_types=_resolve_operation_types(operation_types),
        state=state,
        instrument_id=instrument_uid,
    )
    items = [operation_to_model(raw) for raw in getattr(resp, "items", []) or []]
    notes = [
        "Operation ids may change over time; do not use them as stable primary keys.",
        "Coupon/dividend/tax operations may omit instrument quantity.",
    ]
    if include_canceled:
        notes.append("Canceled operations are included (state filter disabled).")
    return OperationsPage(
        mode=settings.mode,
        from_date=start,
        to_date=end,
        items=items,
        totals=_accumulate_operation_totals(items),
        has_next=bool(getattr(resp, "has_next", False)),
        next_cursor=(getattr(resp, "next_cursor", "") or None) or None,
        notes=notes,
    )


def _add_bucket(buckets: dict[str, Decimal], key: str, value: Decimal, total: Decimal) -> None:
    if value <= 0 or total <= 0:
        return
    buckets[key] = buckets.get(key, _ZERO) + value


def _weight_map(weights: dict[str, Decimal], total: Decimal) -> dict[str, Decimal]:
    if total <= 0:
        return dict.fromkeys(weights, _ZERO)
    return {k: (v / total).quantize(Decimal("0.0001")) for k, v in weights.items()}


def _position_yield_pct(
    adapter: TInvestAdapter,
    settings: Settings,
    instrument: InvestmentInstrument,
    *,
    price: Decimal | None,
) -> Decimal | None:
    itype = (instrument.instrument_type or "").lower()
    now = _now()
    if itype == "bond":
        if not instrument.nominal or not price or price <= 0:
            return None
        try:
            far_dt = now + timedelta(days=365 * 30)
            coupons = adapter.get_bond_coupons(instrument.uid, now, far_dt)
            ytm, _ = _compute_bond_ytm(
                now=now,
                nominal=instrument.nominal,
                clean_price_pct=price,
                aci=_ZERO,
                coupons=coupons,
                maturity=instrument.maturity_date,
                call=instrument.call_date,
            )
            return ytm
        except Exception:
            return None
    if itype == "share":
        try:
            divs = adapter.get_dividends(instrument.uid, now - timedelta(days=370), now + timedelta(days=190))
            if not divs:
                return None
            last = sorted(divs, key=lambda d: getattr(d, "payment_date", now))[-1]
            y = quotation_to_decimal(getattr(last, "yield_value", None))
            if y is not None:
                return y
            div_val = money_to_decimal(getattr(last, "dividend_net", None))
            if div_val and price and price > 0:
                return (div_val / price * Decimal(100)).quantize(Decimal("0.01"))
        except Exception:
            return None
    return None


def _compute_bond_macaulay_duration(
    *,
    now: datetime,
    nominal: Decimal | None,
    clean_price_pct: Decimal | None,
    aci: Decimal | None,
    coupons: list,
    maturity: date | None,
    call: date | None,
) -> Decimal | None:
    ytm_pct, _ = _compute_bond_ytm(
        now=now,
        nominal=nominal,
        clean_price_pct=clean_price_pct,
        aci=aci,
        coupons=coupons,
        maturity=maturity,
        call=call,
    )
    if ytm_pct is None or not nominal or not clean_price_pct or clean_price_pct <= 0:
        return None
    dirty = float(nominal * clean_price_pct / Decimal(100) + (aci or _ZERO))
    if dirty <= 0:
        return None
    rate = float(ytm_pct / Decimal(100))

    coupon_cfs: list[tuple[date, float]] = []
    for c in coupons:
        cd = _bond_date(getattr(c, "coupon_date", None))
        val = money_to_decimal(getattr(c, "pay_one_bond", None))
        if cd and val and val > 0:
            coupon_cfs.append((cd, float(val)))

    horizons: list[date] = []
    if maturity and maturity > now.date():
        horizons.append(maturity)
    if call and call > now.date():
        horizons.append(call)
    if not horizons:
        return None
    horizon = min(horizons)
    par = float(nominal)
    weighted = 0.0
    pv_sum = 0.0
    for cd, val in coupon_cfs:
        if cd <= horizon:
            t = (cd - now.date()).days / 365.0
            pv = val / (1.0 + rate) ** t
            weighted += t * pv
            pv_sum += pv
    t_end = (horizon - now.date()).days / 365.0
    pv_end = par / (1.0 + rate) ** t_end
    weighted += t_end * pv_end
    pv_sum += pv_end
    if pv_sum <= 0:
        return None
    return Decimal(str(weighted / pv_sum)).quantize(Decimal("0.01"))


def get_portfolio_analytics(
    adapter: TInvestAdapter,
    settings: Settings,
    *,
    include_bond_metrics: bool = True,
    top_n: int = 5,
) -> PortfolioAnalytics:
    """Portfolio aggregates: allocation, concentration, weighted yield, bond duration."""
    summary = get_portfolio_summary(adapter, settings)
    total = summary.total_value
    notes = [
        "Allocation and concentration use current market values from get_portfolio_summary.",
        "weighted_yield_pct and bond duration are estimates from market data, not guarantees.",
    ]

    class_values: dict[str, Decimal] = {}
    sector_values: dict[str, Decimal] = {}
    currency_values: dict[str, Decimal] = {}
    issuer_values: dict[str, Decimal] = {}

    if summary.cash > 0:
        _add_bucket(class_values, "cash", summary.cash, total)
        _add_bucket(currency_values, (summary.currency or "rub").lower(), summary.cash, total)

    yield_weighted_sum = _ZERO
    yield_weight_base = _ZERO
    bond_duration_weighted_sum = _ZERO
    bond_duration_weight_base = _ZERO
    now = _now()

    enriched: list[tuple[PortfolioPosition, InvestmentInstrument]] = []
    for pos in summary.positions:
        try:
            instrument = load_instrument(adapter, pos.instrument_uid)
        except Exception:
            continue
        pos.name = instrument.name
        enriched.append((pos, instrument))

        value = pos.current_value or _ZERO
        itype = (pos.instrument_type or instrument.instrument_type or "").lower()
        class_key = _CLASS_LABELS.get(itype, itype or "other")
        _add_bucket(class_values, class_key, value, total)
        sector_key = (instrument.sector or "unknown").lower()
        _add_bucket(sector_values, sector_key, value, total)
        currency_key = (pos.currency or instrument.currency or summary.currency or "unknown").lower()
        _add_bucket(currency_values, currency_key, value, total)
        issuer_key = instrument.name or pos.ticker or pos.instrument_uid
        _add_bucket(issuer_values, issuer_key, value, total)

        y = _position_yield_pct(adapter, settings, instrument, price=pos.current_price)
        if y is not None and value > 0:
            yield_weighted_sum += y * value
            yield_weight_base += value

        if include_bond_metrics and itype == "bond" and value > 0:
            try:
                far_dt = now + timedelta(days=365 * 30)
                coupons = adapter.get_bond_coupons(instrument.uid, now, far_dt)
                duration = _compute_bond_macaulay_duration(
                    now=now,
                    nominal=instrument.nominal,
                    clean_price_pct=pos.current_price,
                    aci=_ZERO,
                    coupons=coupons,
                    maturity=instrument.maturity_date,
                    call=instrument.call_date,
                )
                if duration is not None:
                    bond_duration_weighted_sum += duration * value
                    bond_duration_weight_base += value
            except Exception:
                pass

    ranked = sorted(
        enriched,
        key=lambda pair: pair[0].current_value or _ZERO,
        reverse=True,
    )
    all_weights = [((pos.current_value or _ZERO) / total) if total > 0 else _ZERO for pos, _ in ranked]
    top_positions: list[TopPositionWeight] = []
    for pos, instrument in ranked[: max(1, top_n)]:
        value = pos.current_value or _ZERO
        weight = (value / total) if total > 0 else _ZERO
        top_positions.append(
            TopPositionWeight(
                instrument_uid=pos.instrument_uid,
                ticker=pos.ticker or instrument.ticker,
                name=instrument.name,
                instrument_type=(pos.instrument_type or instrument.instrument_type or "").lower(),
                weight=weight,
                value=value,
            )
        )

    hhi = sum(w * w for w in all_weights)
    concentration = {
        "largest_position_weight": all_weights[0] if all_weights else _ZERO,
        "top3_weight": sum(all_weights[:3]),
        "top5_weight": sum(all_weights[:5]),
        "hhi": hhi.quantize(Decimal("0.0001")) if hhi else _ZERO,
    }

    weighted_yield = None
    if yield_weight_base > 0:
        weighted_yield = (yield_weighted_sum / yield_weight_base).quantize(Decimal("0.01"))

    bond_duration = None
    if bond_duration_weight_base > 0:
        bond_duration = (bond_duration_weighted_sum / bond_duration_weight_base).quantize(Decimal("0.01"))

    drift = None
    profile = load_profile(settings.investment_profile_path)
    if profile is not None:
        drift = compute_drift(
            profile,
            class_values,
            total,
            issuer_weights=_weight_map(issuer_values, total),
            sector_weights=_weight_map(sector_values, total),
            max_issuer_weight=settings.mandate_max_issuer_weight,
            max_sector_weight=settings.mandate_max_sector_weight,
            rebalance_threshold_pct=settings.rebalance_threshold_pct,
        )
        notes.append(
            "drift compares the portfolio against the saved target allocation; "
            "amounts are estimates before commissions and lot rounding."
        )
    else:
        notes.append(
            "No saved investment profile — drift not computed. "
            "Run propose_target_allocation and save_investment_profile first."
        )

    return PortfolioAnalytics(
        mode=settings.mode,
        currency=summary.currency,
        total_value=total,
        by_class=_weight_map(class_values, total),
        by_sector=_weight_map(sector_values, total),
        by_currency=_weight_map(currency_values, total),
        by_issuer=_weight_map(issuer_values, total),
        weighted_yield_pct=weighted_yield,
        bond_portfolio_duration_years=bond_duration,
        concentration=concentration,
        top_positions=top_positions,
        drift=drift,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------


def search_instruments(
    adapter: TInvestAdapter,
    query: str,
    *,
    instrument_types: list[str] | None = None,
    api_trade_available: bool | None = True,
    qualified_only: bool | None = False,
    limit: int = 20,
) -> list[InstrumentSearchHit]:
    type_filter = (instrument_types or [None])[0] if instrument_types else None
    hits = [
        search_hit_to_model(s)
        for s in adapter.find_instrument(query, instrument_type=type_filter, api_trade_available=api_trade_available)
    ]
    if instrument_types:
        wanted = {t.lower() for t in instrument_types}
        hits = [h for h in hits if h.instrument_type.lower() in wanted]
    if qualified_only is False:
        hits = [h for h in hits if not h.qualified_investor_only]
    return hits[:limit]


class _InstrumentCache:
    """Short-TTL cache for STATIC instrument reference data.

    ``load_instrument`` costs 1-2 RPCs (2 for bonds: GetInstrumentBy + BondBy) and
    is called once per leg per plan validation — and a plan is validated twice per
    step (preview + execute boundary). On a six-leg plan that is ~50 redundant
    round-trips per step against a 100 req/min OrdersService budget, which is what
    made steps take ~25s each. Lot size, nominal, increment and maturity do not
    change intraday, so a short TTL is safe; prices are NEVER cached here.
    """

    def __init__(self) -> None:
        self._items: dict[str, tuple[datetime, InvestmentInstrument]] = {}
        self._lock = threading.Lock()

    def _ttl(self) -> int:
        # Resolved lazily: settings are not importable at module-import time here.
        from .config.env import get_settings

        return get_settings().instrument_cache_ttl_seconds

    def get(self, uid: str) -> InvestmentInstrument | None:
        ttl = self._ttl()
        if ttl <= 0:
            return None
        with self._lock:
            hit = self._items.get(uid)
        if hit is None:
            return None
        cached_at, instrument = hit
        if (_now() - cached_at).total_seconds() > ttl:
            return None
        return instrument.model_copy(deep=True)

    def put(self, uid: str, instrument: InvestmentInstrument) -> None:
        with self._lock:
            self._items[uid] = (_now(), instrument.model_copy(deep=True))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_instrument_cache = _InstrumentCache()


def load_instrument(adapter: TInvestAdapter, uid: str) -> InvestmentInstrument:
    cached = _instrument_cache.get(uid)
    if cached is not None:
        return cached
    instrument = _load_instrument_uncached(adapter, uid)
    _instrument_cache.put(uid, instrument)
    return instrument


def _load_instrument_uncached(adapter: TInvestAdapter, uid: str) -> InvestmentInstrument:
    raw = adapter.get_instrument_by_uid(uid)
    if raw is None:
        raise TInvestInstrumentNotFoundError("Instrument not found")
    itype = (getattr(raw, "instrument_type", "") or "").lower()
    instrument = instrument_to_model(raw, itype)
    # Enrich bonds with nominal / maturity (generic Instrument lacks them).
    if itype == "bond":
        try:
            bond = adapter.get_bond_by_uid(uid)
            instrument.nominal = money_to_decimal(getattr(bond, "nominal", None))
            # The generic Instrument record carries no nominal at all, so the
            # denomination currency is only knowable from the bond record — and
            # it is exactly what separates a ruble bond from a yuan one listed
            # on a ruble board.
            instrument.nominal_currency = currency_of(getattr(bond, "nominal", None)) or instrument.nominal_currency
            instrument.initial_nominal = money_to_decimal(getattr(bond, "initial_nominal", None))
            instrument.aci_value = money_to_decimal(getattr(bond, "aci_value", None))
            instrument.maturity_date = _bond_date(getattr(bond, "maturity_date", None))
            instrument.call_date = _bond_date(getattr(bond, "call_date", None))
            instrument.liquidity_flag = bool(getattr(bond, "liquidity_flag", False))
            instrument.for_iis_flag = bool(getattr(bond, "for_iis_flag", False))
            instrument.issue_kind = (getattr(bond, "issue_kind", "") or "") or None
            instrument.issue_size = int(getattr(bond, "issue_size", 0) or 0) or None
        except Exception:
            pass
    elif itype == "etf":
        try:
            etf = adapter.get_etf_by_uid(uid)
            instrument.isin = getattr(etf, "isin", None) or instrument.isin
            instrument.liquidity_flag = bool(getattr(etf, "liquidity_flag", False))
            instrument.for_iis_flag = bool(getattr(etf, "for_iis_flag", False))
            instrument.released_date = _bond_date(getattr(etf, "released_date", None))
        except Exception:
            pass
    return instrument


def get_market_snapshot(adapter: TInvestAdapter, settings: Settings, uid: str) -> MarketSnapshot:
    last_price = adapter.get_last_price(uid)
    try:
        order_book = adapter.get_order_book(uid, depth=10)
    except Exception:
        order_book = None
    try:
        trading_status = adapter.get_trading_status(uid)
    except Exception:
        trading_status = None

    age_seconds: float | None = None
    is_fresh = False
    price_time = getattr(last_price, "time", None) if last_price else None
    if price_time is not None:
        if price_time.tzinfo is None:
            price_time = price_time.replace(tzinfo=UTC)
        age_seconds = (_now() - price_time).total_seconds()
        is_fresh = 0 <= age_seconds <= settings.market_data_max_age_seconds

    return _attach_liquidity(
        adapter,
        _attach_buy_price_hints(
            adapter,
            settings,
            _enrich_snapshot_quote_unit(
                adapter,
                snapshot_to_model(
                    uid,
                    last_price,
                    order_book,
                    trading_status,
                    age_seconds=age_seconds,
                    is_fresh=is_fresh,
                ),
            ),
            session_state=_current_session_state(settings),
        ),
    )


def _attach_liquidity(adapter: TInvestAdapter, snap: MarketSnapshot) -> MarketSnapshot:
    """Best-effort liquidity block: ~30 sessions of volume/turnover + current spread."""
    try:
        instrument = load_instrument(adapter, snap.instrument_uid)
        candles = adapter.get_daily_candles(snap.instrument_uid, _now() - timedelta(days=60), _now())
        avg_lots, avg_turnover, days = _liquidity_from_candles(
            candles,
            lot=instrument.lot,
            nominal=instrument.nominal,
            is_bond=(snap.instrument_type or instrument.instrument_type or "").lower() == "bond",
        )
        spread_pct = None
        if snap.best_bid and snap.best_ask and snap.best_ask > 0:
            spread_pct = ((snap.best_ask - snap.best_bid) / snap.best_ask * Decimal(100)).quantize(Decimal("0.01"))
        if avg_lots is None and avg_turnover is None and spread_pct is None:
            return snap
        return snap.model_copy(
            update={
                "liquidity": LiquidityInfo(
                    avg_daily_volume_lots=avg_lots,
                    avg_daily_turnover=avg_turnover,
                    spread_pct=spread_pct,
                    days_sampled=days or None,
                )
            }
        )
    except Exception:
        return snap


def _attach_buy_price_hints(
    adapter: TInvestAdapter,
    settings: Settings,
    snap: MarketSnapshot,
    session_state: MoexSessionState | None = None,
) -> MarketSnapshot:
    try:
        instrument = load_instrument(adapter, snap.instrument_uid)
        hints = compute_buy_price_hints(snap, instrument, settings, session_state=session_state)
        return snap.model_copy(update={"buy_price_hints": hints})
    except Exception:
        return snap


def _enrich_snapshot_quote_unit(adapter: TInvestAdapter, snap: MarketSnapshot) -> MarketSnapshot:
    """Attach instrument_type and price_quote_unit so agents know bond prices are % of nominal."""
    try:
        raw = adapter.get_instrument_by_uid(snap.instrument_uid)
        if raw is None:
            return snap
        itype = (getattr(raw, "instrument_type", "") or "").lower()
        return snap.model_copy(
            update={
                "instrument_type": itype or None,
                "price_quote_unit": price_quote_unit_for(itype),
            }
        )
    except Exception:
        return snap


# Sort keys that need the batched last-price call.
_PRICE_SORT_KEYS = frozenset({"price"})

# Per-type sort keys.
#   cheap     -> resolved from static catalogue data (no extra calls)
#   analytics -> need one per-instrument analytics call (auto-caps the pool)
_BOND_CHEAP_KEYS = frozenset({"name", "ticker", "maturity", "risk", "coupons", "nominal"})
_BOND_ANALYTICS_KEYS = frozenset({"yield", "ytm", "return", "volatility", "drawdown"})
_SHARE_CHEAP_KEYS = frozenset({"name", "ticker"})
_SHARE_ANALYTICS_KEYS = frozenset({"dividend_yield", "return", "volatility", "drawdown"})
_ETF_CHEAP_KEYS = frozenset({"name", "ticker", "commission", "focus", "released"})
_ETF_ANALYTICS_KEYS = frozenset({"return", "volatility", "drawdown"})

# Sort keys computed from the daily candle series — the rankings that become
# meaningless if the candle fetch degrades, whatever the instrument type.
_CANDLE_SORT_KEYS = frozenset({"return", "volatility", "drawdown"})

# Fee sort keys — ETFs only. TER lives in the asset-level record, one
# get_asset_by call per fund, so the pool is capped like analytics.
_ETF_FEE_KEYS = frozenset({"ter"})

# Forecast (analyst consensus) sort keys — shares only.
#   recommendation -> ranks on consensus strength (buy − sell), no price needed
#   upside         -> consensus target vs. last_price, needs a batched price call
_SHARE_FORECAST_KEYS = frozenset({"recommendation", "upside"})

# Fundamentals sort keys — shares only (all pre-computed by the data vendor).
_SHARE_FUNDAMENTAL_KEYS = frozenset(
    {
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
    }
)

# Recommendation enum (T-Invest) -> compact label, plus an ordinal for sorting.
_RECOMMENDATION_MAP = {
    "RECOMMENDATION_BUY": "buy",
    "RECOMMENDATION_HOLD": "hold",
    "RECOMMENDATION_SELL": "sell",
}

# Ordinal for risk_level so it sorts low < moderate < high.
_RISK_ORDER = {"low": 0, "moderate": 1, "high": 2}

# When sorting by price without analytics, evaluate prices for this many
# top candidates (after cheap pre-sort) so the price ranking is meaningful
# without pulling the entire catalogue's market data.
_PRICE_POOL_CAP = 200


def _sort_value(hit, key: str):
    """Resolve a sort key off any screener row via attribute access."""
    mapping = {
        "name": (getattr(hit, "name", "") or "").lower(),
        "ticker": (getattr(hit, "ticker", "") or "").lower(),
        "maturity": getattr(hit, "maturity_date", None),
        "risk": _RISK_ORDER.get(getattr(hit, "risk_level", None) or ""),
        "coupons": getattr(hit, "coupons_per_year", None),
        "nominal": getattr(hit, "nominal", None),
        "price": getattr(hit, "last_price", None),
        "yield": getattr(hit, "current_yield_pct", None),
        "ytm": getattr(hit, "ytm_pct", None),
        "dividend_yield": getattr(hit, "dividend_yield_pct", None),
        "return": getattr(hit, "historical_return_pct", None),
        "volatility": getattr(hit, "volatility_annual_pct", None),
        "drawdown": getattr(hit, "max_drawdown_pct", None),
        "upside": getattr(hit, "target_upside_pct", None),
        "recommendation": _recommendation_score(hit),
        "pe": getattr(hit, "pe_ratio", None),
        "ps": getattr(hit, "price_to_sales", None),
        "pb": getattr(hit, "price_to_book", None),
        "ev_ebitda": getattr(hit, "ev_to_ebitda", None),
        "roe": getattr(hit, "roe_pct", None),
        "roa": getattr(hit, "roa_pct", None),
        "roic": getattr(hit, "roic_pct", None),
        "net_margin": getattr(hit, "net_margin_pct", None),
        "eps": getattr(hit, "eps_ttm", None),
        "market_cap": getattr(hit, "market_cap", None),
        "debt_to_equity": getattr(hit, "debt_to_equity", None),
        "net_debt_ebitda": getattr(hit, "net_debt_to_ebitda", None),
        "dividend_yield_fund": getattr(hit, "dividend_yield_fund_pct", None),
        "revenue_growth": getattr(hit, "revenue_growth_5y_pct", None),
        "beta": getattr(hit, "beta", None),
        "commission": getattr(hit, "fixed_commission_pct", None),
        "ter": getattr(hit, "total_expense_pct", None),
        "focus": (getattr(hit, "focus_type", "") or "").lower(),
        "released": getattr(hit, "released_date", None),
    }
    return mapping.get(key)


def _recommendation_score(hit) -> int | None:
    """Net analyst conviction (buy − sell). ``None`` when no consensus is attached."""
    buy = getattr(hit, "analysts_buy", None)
    hold = getattr(hit, "analysts_hold", None)
    sell = getattr(hit, "analysts_sell", None)
    if buy is None and hold is None and sell is None:
        return None
    return (buy or 0) - (sell or 0)


def _sort_hits(hits: list, sort_by: str, descending: bool) -> list:
    """Sort by *sort_by*, always pushing rows with a missing value to the end."""
    present = [h for h in hits if _sort_value(h, sort_by) is not None]
    missing = [h for h in hits if _sort_value(h, sort_by) is None]
    present.sort(key=lambda h: _sort_value(h, sort_by), reverse=descending)
    return present + missing


def _attach_last_prices(adapter: TInvestAdapter, hits: list) -> None:
    """Best-effort: fill ``last_price`` for *hits* with one batched call."""
    if not hits:
        return
    try:
        prices = adapter.get_last_prices([h.uid for h in hits])
    except Exception:
        return
    by_id: dict = {}
    for p in prices:
        value = quotation_to_decimal(getattr(p, "price", None))
        uid = getattr(p, "instrument_uid", None)
        figi = getattr(p, "figi", None)
        if uid:
            by_id[uid] = value
        if figi:
            by_id.setdefault(figi, value)
    for h in hits:
        h.last_price = by_id.get(h.uid) or (by_id.get(h.figi) if h.figi else None)


def _apply_analytics(h, a) -> None:
    """Copy the screener-relevant analytics fields onto a row.

    Only sets fields the concrete row type actually declares (e.g. ``yield`` for
    bonds, ``dividend_yield`` for shares), so each row stays free of irrelevant
    keys.
    """
    h.historical_return_pct = a.historical_return_pct
    h.volatility_annual_pct = a.volatility_annual_pct
    h.max_drawdown_pct = a.max_drawdown_pct
    h.avg_daily_volume_lots = a.avg_daily_volume_lots
    h.avg_daily_turnover = a.avg_daily_turnover
    h.avg_daily_turnover_rub = a.avg_daily_turnover_rub
    if h.last_price is None:
        h.last_price = a.current_price
    fields = type(h).model_fields
    if "current_yield_pct" in fields:
        h.current_yield_pct = a.current_yield_pct
    if "ytm_pct" in fields:
        h.ytm_pct = a.ytm_pct
        h.ytm_to_offer = a.ytm_to_offer
    if "duration_years" in fields:
        h.duration_years = a.macaulay_duration_years
    if "dividend_yield_pct" in fields:
        h.dividend_yield_pct = a.dividend_yield_pct


@dataclass(frozen=True)
class _AnalyticsEnrichmentReport:
    requested: int
    retried: int
    unresolved: dict[str, tuple[str, ...]]


def _analytics_components(value: InstrumentAnalytics | None) -> tuple[str, ...]:
    if value is None:
        return ("analytics",)
    return tuple(dict.fromkeys(value.unavailable_components))


def _analytics_quality(
    value: InstrumentAnalytics | None,
    required_components: frozenset,
) -> tuple[int, int, int]:
    """Lower is better, prioritizing recovery of screen-critical components."""
    components = set(_analytics_components(value))
    required_failures = len(components & required_components)
    if "analytics" in components:
        required_failures = max(1, len(required_components))
    return (1 if value is None else 0, required_failures, len(components))


def _enrich_with_analytics(
    adapter: TInvestAdapter,
    settings: Settings,
    hits: list,
    instrument_type: str,
    required_components: frozenset = frozenset(),
) -> _AnalyticsEnrichmentReport:
    """Fetch per-instrument analytics and retry only degraded rows once.

    Uses the screening path (:func:`_screen_analytics`), which reuses the
    catalogue row and the batched last price instead of re-resolving both per
    instrument.

    The first pass uses the configured bounded concurrency. Rows whose analytics
    call failed, or whose result declares an unavailable market-data component,
    are retried once after a short backoff with lower concurrency. The best of
    the two attempts is applied and unresolved components are returned to the
    screener so it can reject misleading empty or heavily partial results.
    """
    if not hits:
        return _AnalyticsEnrichmentReport(requested=0, retried=0, unresolved={})

    def fetch(h):
        try:
            return h, _screen_analytics(adapter, h, instrument_type), None
        except Exception as exc:  # noqa: BLE001 - converted to safe diagnostics below
            return h, None, type(exc).__name__

    def fetch_many(rows: list, workers: int) -> list:
        if workers == 1:
            return [fetch(h) for h in rows]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(fetch, rows))

    workers = max(1, min(settings.analytics_concurrency, len(hits)))
    first_results = fetch_many(hits, workers)
    first_by_uid = {h.uid: (h, analytics, error_type) for h, analytics, error_type in first_results}
    retry_hits = [h for h, analytics, _ in first_results if _analytics_components(analytics)]

    retry_by_uid: dict[str, tuple] = {}
    if retry_hits:
        logger.warning(
            "analytics_enrichment_retry degraded=%d requested=%d delay_seconds=%.1f",
            len(retry_hits),
            len(hits),
            _ANALYTICS_RETRY_DELAY_SECONDS,
        )
        time.sleep(_ANALYTICS_RETRY_DELAY_SECONDS)
        retry_workers = max(1, min(_ANALYTICS_RETRY_MAX_WORKERS, workers, len(retry_hits)))
        retry_by_uid = {
            h.uid: (h, analytics, error_type) for h, analytics, error_type in fetch_many(retry_hits, retry_workers)
        }

    unresolved: dict[str, tuple[str, ...]] = {}
    error_types: set[str] = set()
    for h in hits:
        _, first_analytics, first_error = first_by_uid[h.uid]
        selected = first_analytics
        selected_error = first_error
        retry_result = retry_by_uid.get(h.uid)
        if retry_result is not None:
            _, retry_analytics, retry_error = retry_result
            if _analytics_quality(retry_analytics, required_components) < _analytics_quality(
                first_analytics, required_components
            ):
                selected = retry_analytics
                selected_error = retry_error
        if selected is not None:
            _apply_analytics(h, selected)
        components = _analytics_components(selected)
        if components:
            unresolved[h.uid] = components
            if selected_error:
                error_types.add(selected_error)

    if unresolved:
        logger.error(
            "analytics_enrichment_unresolved failed=%d requested=%d components=%s error_types=%s",
            len(unresolved),
            len(hits),
            sorted({component for values in unresolved.values() for component in values}),
            sorted(error_types),
        )
    return _AnalyticsEnrichmentReport(
        requested=len(hits),
        retried=len(retry_hits),
        unresolved=unresolved,
    )


def _attach_share_forecasts(adapter: TInvestAdapter, hits: list) -> None:
    """Best-effort: attach analyst consensus to share rows via ONE bulk sweep.

    Uses ``get_consensus_forecasts`` (paged) so the whole returned set costs a
    handful of calls regardless of how many rows we enrich, then maps each item
    onto a row by instrument uid. Failures leave the forecast fields empty.
    ``target_upside_pct`` is filled later, once ``last_price`` is known.
    """
    if not hits:
        return
    try:
        items = adapter.get_consensus_forecasts()
    except Exception:
        return
    # Consensus rows join on the ASSET uid, NOT the instrument uid.
    by_asset: dict = {}
    for it in items:
        asset = getattr(it, "asset_uid", None)
        if asset:
            by_asset[asset] = it
    for h in hits:
        it = by_asset.get(getattr(h, "asset_uid", None))
        if it is None:
            continue
        h.consensus_recommendation = _RECOMMENDATION_MAP.get(enum_name(getattr(it, "consensus", None)) or "")
        h.consensus_target_price = quotation_to_decimal(getattr(it, "best_target_price", None))
        h.analysts_buy = int(getattr(it, "total_buy_recommend", 0) or 0) or None
        h.analysts_hold = int(getattr(it, "total_hold_recommend", 0) or 0) or None
        h.analysts_sell = int(getattr(it, "total_sell_recommend", 0) or 0) or None


def _compute_share_upside(hits: list) -> None:
    """Fill ``target_upside_pct`` = (consensus target / last_price − 1) * 100."""
    for h in hits:
        target = getattr(h, "consensus_target_price", None)
        price = getattr(h, "last_price", None)
        if target and price and price > 0:
            h.target_upside_pct = ((target / price - 1) * Decimal(100)).quantize(Decimal("0.01"))


def _fnum(value) -> Decimal | None:
    """A fundamentals double → Decimal. Treats the vendor's 0.0 sentinel as missing."""
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except Exception:
        return None
    return d if d != 0 else None


def _attach_share_fundamentals(adapter: TInvestAdapter, hits: list) -> None:
    """Best-effort: attach a compact set of fundamentals to share rows.

    One batched ``get_asset_fundamentals`` sweep over the pool's asset uids, then
    mapped back by ``asset_uid``. Failures leave the fields empty.
    """
    assets = [a for a in {getattr(h, "asset_uid", None) for h in hits} if a]
    if not assets:
        return
    try:
        stats = adapter.get_asset_fundamentals(assets)
    except Exception:
        return
    by_asset = {getattr(s, "asset_uid", None): s for s in stats if getattr(s, "asset_uid", None)}
    for h in hits:
        s = by_asset.get(getattr(h, "asset_uid", None))
        if s is None:
            continue
        h.pe_ratio = _fnum(getattr(s, "pe_ratio_ttm", None))
        h.price_to_sales = _fnum(getattr(s, "price_to_sales_ttm", None))
        h.price_to_book = _fnum(getattr(s, "price_to_book_ttm", None))
        h.ev_to_ebitda = _fnum(getattr(s, "ev_to_ebitda_mrq", None))
        h.roe_pct = _fnum(getattr(s, "roe", None))
        h.roa_pct = _fnum(getattr(s, "roa", None))
        h.roic_pct = _fnum(getattr(s, "roic", None))
        h.net_margin_pct = _fnum(getattr(s, "net_margin_mrq", None))
        h.eps_ttm = _fnum(getattr(s, "eps_ttm", None))
        h.market_cap = _fnum(getattr(s, "market_capitalization", None))
        h.debt_to_equity = _fnum(getattr(s, "total_debt_to_equity_mrq", None))
        h.net_debt_to_ebitda = _fnum(getattr(s, "net_debt_to_ebitda", None))
        h.dividend_yield_fund_pct = _fnum(getattr(s, "dividend_yield_daily_ttm", None))
        h.revenue_growth_5y_pct = _fnum(getattr(s, "five_year_annual_revenue_growth_rate", None))
        h.beta = _fnum(getattr(s, "beta", None))


# The catalogue's ``focus_type`` is not a usable asset-class filter: on the MOEX
# board LQDT (money market), SBGB / OBLG / SBCB (bonds), GOLD and AKGD (gold) and
# every "заблокированные активы" shell all report focus_type="equity". The fund
# NAME does carry the class, and it is free catalogue data, so it can narrow the
# FULL set before any paid enrichment. Order matters — the first match wins.
_ETF_ASSET_CLASS_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("blocked", ("заблокированны", "blocked")),
    ("money_market", ("ликвидност", "денежный рынок", "money market")),
    ("commodity", ("золото", "серебр", "драгоценн", "gold", "silver")),
    ("bonds", ("облигаци", "офз", "bond")),
    ("mixed", ("вечный портфель", "сбалансированн", "смарт", "моя цель", "mixed")),
    ("equity", ("акци", "индекс мосбиржи", "крупнейшие компании", "equity", "shares")),
)

# Which inferred classes a requested ``focus_type`` accepts. A row whose class
# cannot be inferred is KEPT — the classifier only removes clear contradictions.
_FOCUS_TO_ASSET_CLASSES: dict[str, frozenset] = {
    "equity": frozenset({"equity"}),
    "fixed_income": frozenset({"bonds", "money_market"}),
    "mixed_allocation": frozenset({"mixed"}),
    "alternative_investment": frozenset({"commodity"}),
}

# TER costs one get_asset_by per ASSET and nothing else, so a fee-ranked screen
# can cover the whole filtered catalogue instead of an arbitrary front slice.
# Only when analytics runs too does the tighter analytics_limit take over.
_ETF_FEE_POOL_CAP = 120


def _etf_asset_class(name: str) -> str | None:
    """Asset class inferred from the fund name; ``None`` when nothing matches."""
    lowered = (name or "").lower()
    for asset_class, needles in _ETF_ASSET_CLASS_PATTERNS:
        if any(needle in lowered for needle in needles):
            return asset_class
    return None


def _attach_etf_fees(adapter: TInvestAdapter, hits: list, workers: int = 1) -> None:
    """Best-effort: attach TER (total_expense) to ETF rows.

    The catalogue record only carries ``fixed_commission``; the all-in TER lives
    in the asset-level ``AssetEtf`` record — one ``get_asset_by`` call per ASSET.
    Funds are fetched by distinct asset uid (share classes of one fund resolve to
    the same asset) and in parallel, since this is the whole cost of a fee-ranked
    screen. Failures leave the field empty.
    """
    by_asset: dict[str, list] = {}
    for h in hits:
        asset_uid = getattr(h, "asset_uid", None)
        if asset_uid:
            by_asset.setdefault(asset_uid, []).append(h)
    if not by_asset:
        return

    def fetch(asset_uid: str):
        try:
            return asset_uid, adapter.get_asset_by(asset_uid)
        except Exception:  # noqa: BLE001 - best effort, leaves the field empty
            return asset_uid, None

    asset_uids = list(by_asset)
    workers = max(1, min(workers, len(asset_uids)))
    if workers == 1:
        results = [fetch(uid) for uid in asset_uids]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(fetch, asset_uids))

    for asset_uid, resp in results:
        asset = getattr(resp, "asset", None) if resp is not None else None
        sec = getattr(asset, "security", None) if asset else None
        ext = getattr(sec, "etf", None) if sec else None
        if ext is None:
            continue
        # A zero TER is "not disclosed", not "free" — only 4 of the 13 ruble
        # equity funds on the board publish one. Left as 0 it would sort to the
        # TOP of a cheapest-first ranking and present undisclosed funds as the
        # best deal; as None it sorts to the end with the other unknowns.
        ter = quotation_to_decimal(getattr(ext, "total_expense", None)) or None
        for h in by_asset[asset_uid]:
            h.total_expense_pct = ter


def _base_kwargs(obj) -> dict:
    cur = (getattr(obj, "currency", "") or "").lower()
    return {
        "uid": obj.uid,
        "asset_uid": getattr(obj, "asset_uid", None) or None,
        "figi": getattr(obj, "figi", None) or None,
        "ticker": getattr(obj, "ticker", "") or "",
        "name": getattr(obj, "name", "") or "",
        "currency": cur or None,
        "lot": int(getattr(obj, "lot", 0) or 0) or None,
        "api_trade_available": bool(getattr(obj, "api_trade_available_flag", False)),
        "qualified_investor_only": bool(getattr(obj, "for_qual_investor_flag", False)),
        "sector": (getattr(obj, "sector", "") or "") or None,
        "country_of_risk": (getattr(obj, "country_of_risk", "") or "") or None,
    }


def _clean_enum(value, prefix: str) -> str | None:
    name = enum_name(value)
    if name and name.startswith(prefix):
        name = name[len(prefix) :]
    return name.lower() if name else None


def _screen(
    adapter: TInvestAdapter,
    settings: Settings | None,
    instrument_type: str,
    *,
    builder,
    extra_filter,
    cheap_keys: frozenset,
    analytics_keys: frozenset,
    currency: str | None,
    api_trade_available: bool | None,
    qualified_only: bool | None,
    sort_by: str | None,
    descending: bool,
    include_analytics: bool,
    analytics_limit: int,
    limit: int,
    full_enrichers: tuple = (),
    pool_enrichers: tuple = (),
    price_finalizers: tuple = (),
    pool_sort_keys: frozenset = frozenset(),
    pool_cap: int | None = None,
    post_filters: tuple = (),
    required_analytics_components: frozenset = frozenset(),
) -> list:
    """Shared screening pipeline used by the per-type list_* functions.

    Tiers, cheapest first:
      - cheap filters/sorts run over the FULL catalogue (no extra calls);
      - ``full_enrichers`` (e.g. analyst consensus) are cheap bulk sweeps applied
        to the FULL set, so keys they produce can also sort the full catalogue;
      - ``last_price``, ``pool_enrichers`` (e.g. fundamentals) and per-instrument
        analytics run on a bounded top-N pool to respect rate limits;
      - ``price_finalizers`` (e.g. consensus upside) run once prices are attached.

    ``pool_sort_keys`` lists the keys whose values only exist after pool-level
    enrichment, so the final ranking is applied on the (already enriched) pool.
    ``pool_cap`` bounds the pool when a pool enricher costs one API call per
    row (e.g. ETF fees via get_asset_by) — the result is capped too, mirroring
    the ``include_analytics`` semantics.
    ``post_filters`` are predicates over enriched rows (e.g. liquidity/duration
    thresholds); they run on the pool AFTER enrichment, so with such a filter the
    result may hold fewer than ``limit`` rows even when the catalogue has more.
    ``required_analytics_components`` identifies upstream data whose failure makes
    the requested ranking/filter unreliable. Failed components are retried once;
    a misleading empty or heavily partial result becomes an explicit tool error.
    """
    raw = adapter.list_instruments(instrument_type)

    hits: list = []
    for obj in raw:
        if api_trade_available and not getattr(obj, "api_trade_available_flag", False):
            continue
        if qualified_only is False and getattr(obj, "for_qual_investor_flag", False):
            continue
        cur = (getattr(obj, "currency", "") or "").lower()
        if currency and cur != currency.lower():
            continue
        hit = builder(obj)
        if extra_filter is not None and not extra_filter(obj, hit):
            continue
        hits.append(hit)

    # Sorting by an analytics key implies we must compute analytics.
    if sort_by in analytics_keys:
        include_analytics = True

    # Cheap bulk enrichment (e.g. analyst consensus) over the FULL set.
    for enrich in full_enrichers:
        enrich(adapter, hits)

    # Cheap pre-sort over the full catalogue (static or full-enriched keys).
    if sort_by in cheap_keys:
        hits = _sort_hits(hits, sort_by, descending)

    # Pool size: analytics is the tightest cap; price/pool-enriched ranking gets
    # a wider window; everything else just returns the requested page.
    if include_analytics:
        pool = hits[: max(1, analytics_limit)]
    elif sort_by in (_PRICE_SORT_KEYS | pool_sort_keys):
        pool = hits[: max(limit, _PRICE_POOL_CAP)]
    else:
        pool = hits[:limit]
    if pool_cap is not None:
        pool = pool[: max(1, pool_cap)]

    analytics_report = _AnalyticsEnrichmentReport(requested=0, retried=0, unresolved={})
    _attach_last_prices(adapter, pool)
    if include_analytics and settings is not None:
        analytics_report = _enrich_with_analytics(
            adapter,
            settings,
            pool,
            instrument_type,
            required_components=required_analytics_components,
        )
    for enrich in pool_enrichers:
        enrich(adapter, pool)
    for finalize in price_finalizers:
        finalize(pool)

    for keep in post_filters:
        pool = [h for h in pool if keep(h)]

    # A degraded sweep is only misleading when the ranking or a filter actually
    # rests on the missing data. With no required component the analytics tier
    # was asked for its columns alone, so the caller gets the rows plus null
    # fields rather than an error over something nothing depended on.
    relevant_unresolved = {
        uid: components
        for uid, components in analytics_report.unresolved.items()
        if required_analytics_components
        and ("analytics" in components or bool(set(components) & required_analytics_components))
    }
    failed = len(relevant_unresolved)
    unreliable = bool(
        failed
        and (
            failed == analytics_report.requested
            or (not pool and bool(post_filters))
            or failed * 2 >= analytics_report.requested
        )
    )
    if unreliable:
        components = sorted({component for values in relevant_unresolved.values() for component in values})
        result_kind = "empty" if not pool else "partial"
        raise TInvestDataUnavailableError(
            "Instrument screening analytics remained unavailable after one retry for "
            f"{failed}/{analytics_report.requested} candidates "
            f"(components: {', '.join(components)}). Refusing to return a misleading "
            f"{result_kind} result. Retry the screener later or remove analytics-dependent "
            "filters/sorting for a catalogue-only fallback."
        )
    if failed:
        logger.warning(
            "analytics_screening_partial unresolved=%d requested=%d survivors=%d",
            failed,
            analytics_report.requested,
            len(pool),
        )

    if sort_by in (_PRICE_SORT_KEYS | analytics_keys | pool_sort_keys):
        pool = _sort_hits(pool, sort_by, descending)
    return pool[:limit]


def _liquidity_post_filter(min_avg_daily_turnover: Decimal):
    """Keep rows whose ~30-session average daily turnover clears the bar.

    The threshold is in RUBLES, so the comparison uses the converted
    ``avg_daily_turnover_rub``: an FX-linked bond's native turnover is in its own
    currency and reads ~11x too small against a ruble bar.

    Rows without turnover data (no/zero volume in candles) are DROPPED — an
    instrument that barely trades is exactly what the filter must cut. So are
    rows whose turnover could not be converted, since an unconverted figure
    cannot be judged against a ruble threshold either way.
    """

    def keep(h) -> bool:
        turnover = h.avg_daily_turnover_rub
        return turnover is not None and turnover >= min_avg_daily_turnover

    return keep


def list_bonds(
    adapter: TInvestAdapter,
    *,
    settings: Settings | None = None,
    currency: str | None = None,
    denomination_currency: str | None = None,
    api_trade_available: bool | None = True,
    qualified_only: bool | None = False,
    risk_level: str | None = None,
    max_bond_risk_level: str | None = None,
    excluded_sectors: list[str] | None = None,
    max_maturity_years: float | None = None,
    max_duration_years: float | None = None,
    min_avg_daily_turnover: Decimal | None = None,
    sort_by: str | None = None,
    descending: bool = True,
    include_analytics: bool = False,
    analytics_limit: int = 25,
    limit: int = 100,
) -> list[BondScreenHit]:
    """Screen the bond catalogue (risk_level, maturity, duration, liquidity, coupons, yield)."""
    max_maturity = None
    if max_maturity_years is not None:
        max_maturity = (_now() + timedelta(days=int(max_maturity_years * 365))).date()
    max_risk_rank = _BOND_RISK_RANK.get(max_bond_risk_level.lower()) if max_bond_risk_level else None
    excluded = {s.lower() for s in excluded_sectors} if excluded_sectors else None
    want_denomination = (denomination_currency or "").lower() or None

    def builder(obj) -> BondScreenHit:
        return BondScreenHit(
            **_base_kwargs(obj),
            isin=getattr(obj, "isin", None) or None,
            # The nominal's own currency, which is what the yields end up in.
            nominal_currency=currency_of(getattr(obj, "nominal", None)),
            nominal=money_to_decimal(getattr(obj, "nominal", None)),
            initial_nominal=money_to_decimal(getattr(obj, "initial_nominal", None)),
            maturity_date=_bond_date(getattr(obj, "maturity_date", None)),
            call_date=_bond_date(getattr(obj, "call_date", None)),
            risk_level=_RISK_LEVEL_MAP.get(enum_name(getattr(obj, "risk_level", None)) or ""),
            coupons_per_year=int(getattr(obj, "coupon_quantity_per_year", 0) or 0) or None,
            floating_coupon=bool(getattr(obj, "floating_coupon_flag", False)),
            amortization=bool(getattr(obj, "amortization_flag", False)),
            perpetual=bool(getattr(obj, "perpetual_flag", False)),
            subordinated=bool(getattr(obj, "subordinated_flag", False)),
            liquidity_flag=bool(getattr(obj, "liquidity_flag", False)),
            for_iis_flag=bool(getattr(obj, "for_iis_flag", False)),
            issue_kind=(getattr(obj, "issue_kind", "") or "") or None,
            issue_size=int(getattr(obj, "issue_size", 0) or 0) or None,
            aci_value=money_to_decimal(getattr(obj, "aci_value", None)),
        )

    def extra_filter(obj, hit: BondScreenHit) -> bool:
        if want_denomination and (hit.yield_currency or "") != want_denomination:
            return False
        if risk_level and (hit.risk_level or "") != risk_level.lower():
            return False
        if max_risk_rank is not None:
            # Unknown risk tier is treated as above any cap (conservative).
            rank = _BOND_RISK_RANK.get(hit.risk_level or "")
            if rank is None or rank > max_risk_rank:
                return False
        if excluded and (hit.sector or "").lower() in excluded:
            return False
        return not (max_maturity and hit.maturity_date and hit.maturity_date > max_maturity)

    post_filters: list = []
    if min_avg_daily_turnover is not None:
        post_filters.append(_liquidity_post_filter(min_avg_daily_turnover))
    if max_duration_years is not None:
        cap = Decimal(str(max_duration_years))
        # Duration comes from the analytics tier; rows without it are dropped
        # (perpetual/undated bonds have unbounded rate risk).
        post_filters.append(lambda h: h.duration_years is not None and h.duration_years <= cap)
    if post_filters:
        include_analytics = True  # both filters need analytics-tier fields
    # Only components the ranking or a filter genuinely rests on are required;
    # analytics asked for purely as extra columns must not turn a degraded
    # sweep into an error.
    required_components: set = set()
    if min_avg_daily_turnover is not None or sort_by in _CANDLE_SORT_KEYS:
        required_components.add("candles")
    if sort_by in {"yield", "ytm"} or max_duration_years is not None:
        required_components.add("bond_coupons")

    hits = _screen(
        adapter,
        settings,
        "bond",
        builder=builder,
        extra_filter=extra_filter,
        cheap_keys=_BOND_CHEAP_KEYS,
        analytics_keys=_BOND_ANALYTICS_KEYS,
        currency=currency,
        api_trade_available=api_trade_available,
        qualified_only=qualified_only,
        sort_by=sort_by,
        descending=descending,
        include_analytics=include_analytics,
        analytics_limit=analytics_limit,
        limit=limit,
        post_filters=tuple(post_filters),
        required_analytics_components=frozenset(required_components),
    )
    _reject_cross_currency_yield_ranking(hits, sort_by)
    return hits


def _reject_cross_currency_yield_ranking(hits: list[BondScreenHit], sort_by: str | None) -> None:
    """Refuse to hand back a yield ranking that mixes denomination currencies.

    Sorting a yuan-denominated 8.7% next to a ruble 15.7% produces a table that
    reads like a yield ladder but is really a currency forecast: the gap is the
    expected CNY/RUB move, not extra income. Rather than emit a plausible-looking
    misleading ranking, make the caller name the currency it wants to rank in —
    the same "refuse rather than mislead" rule the analytics tier already uses.
    """
    if sort_by not in {"yield", "ytm"}:
        return
    currencies = sorted({h.yield_currency for h in hits if h.yield_currency and h.ytm_pct is not None})
    if len(currencies) < 2:
        return
    raise TInvestConfigurationError(
        "Refusing to rank bond yields across denomination currencies "
        f"({', '.join(currencies)}): those yields are not comparable — an FX-linked "
        "bond's yield is a yield in its own currency, so the difference is an exchange-rate "
        "bet, not extra income. Re-run with denomination_currency='rub' (or another single "
        "currency) to rank inside one currency, and compare the currencies separately."
    )


def list_shares(
    adapter: TInvestAdapter,
    *,
    settings: Settings | None = None,
    currency: str | None = None,
    api_trade_available: bool | None = True,
    qualified_only: bool | None = False,
    sector: str | None = None,
    excluded_sectors: list[str] | None = None,
    pays_dividends: bool | None = None,
    min_avg_daily_turnover: Decimal | None = None,
    sort_by: str | None = None,
    descending: bool = True,
    include_analytics: bool = False,
    include_forecast: bool = False,
    include_fundamentals: bool = False,
    analytics_limit: int = 25,
    limit: int = 100,
) -> list[ShareScreenHit]:
    """Screen the share catalogue (sector, dividends, liquidity, return, volatility, consensus, fundamentals)."""
    excluded = {s.lower() for s in excluded_sectors} if excluded_sectors else None

    def builder(obj) -> ShareScreenHit:
        return ShareScreenHit(
            **_base_kwargs(obj),
            share_type=_clean_enum(getattr(obj, "share_type", None), "SHARE_TYPE_"),
            pays_dividends=bool(getattr(obj, "div_yield_flag", False)),
        )

    def extra_filter(obj, hit: ShareScreenHit) -> bool:
        if sector and (hit.sector or "").lower() != sector.lower():
            return False
        if excluded and (hit.sector or "").lower() in excluded:
            return False
        return not (pays_dividends is not None and bool(hit.pays_dividends) != pays_dividends)

    post_filters: list = []
    if min_avg_daily_turnover is not None:
        post_filters.append(_liquidity_post_filter(min_avg_daily_turnover))
        include_analytics = True

    forecast_active = include_forecast or sort_by in _SHARE_FORECAST_KEYS
    fundamentals_active = include_fundamentals or sort_by in _SHARE_FUNDAMENTAL_KEYS

    cheap_keys = set(_SHARE_CHEAP_KEYS)
    full_enrichers: list = []
    pool_enrichers: list = []
    price_finalizers: list = []
    pool_sort_keys: set = set()

    if forecast_active:
        full_enrichers.append(_attach_share_forecasts)  # cheap bulk sweep over the full set
        price_finalizers.append(_compute_share_upside)
        cheap_keys.add("recommendation")  # rank full catalogue (no price)
        pool_sort_keys.add("upside")  # needs last_price
    if fundamentals_active:
        pool_enrichers.append(_attach_share_fundamentals)
        pool_sort_keys |= _SHARE_FUNDAMENTAL_KEYS
    # See list_bonds: require only what the ranking or a filter rests on.
    required_components: set = set()
    if min_avg_daily_turnover is not None or sort_by in _CANDLE_SORT_KEYS:
        required_components.add("candles")
    if sort_by == "dividend_yield":
        required_components.add("dividends")

    return _screen(
        adapter,
        settings,
        "share",
        builder=builder,
        extra_filter=extra_filter,
        cheap_keys=frozenset(cheap_keys),
        analytics_keys=_SHARE_ANALYTICS_KEYS,
        currency=currency,
        api_trade_available=api_trade_available,
        qualified_only=qualified_only,
        sort_by=sort_by,
        descending=descending,
        include_analytics=include_analytics,
        analytics_limit=analytics_limit,
        limit=limit,
        full_enrichers=tuple(full_enrichers),
        pool_enrichers=tuple(pool_enrichers),
        price_finalizers=tuple(price_finalizers),
        pool_sort_keys=frozenset(pool_sort_keys),
        post_filters=tuple(post_filters),
        required_analytics_components=frozenset(required_components),
    )


def list_etfs(
    adapter: TInvestAdapter,
    *,
    settings: Settings | None = None,
    currency: str | None = None,
    api_trade_available: bool | None = True,
    qualified_only: bool | None = False,
    sector: str | None = None,
    excluded_sectors: list[str] | None = None,
    focus_type: str | None = None,
    min_avg_daily_turnover: Decimal | None = None,
    sort_by: str | None = None,
    descending: bool = True,
    include_analytics: bool = False,
    include_fees: bool = False,
    analytics_limit: int = 25,
    limit: int = 100,
) -> list[EtfScreenHit]:
    """Screen the ETF / fund catalogue (focus, commission, TER, liquidity, return, volatility)."""
    excluded = {s.lower() for s in excluded_sectors} if excluded_sectors else None

    def builder(obj) -> EtfScreenHit:
        name = getattr(obj, "name", "") or ""
        return EtfScreenHit(
            **_base_kwargs(obj),
            isin=getattr(obj, "isin", None) or None,
            focus_type=(getattr(obj, "focus_type", "") or "") or None,
            asset_class=_etf_asset_class(name),
            rebalancing_freq=(getattr(obj, "rebalancing_freq", "") or "") or None,
            num_shares=quotation_to_decimal(getattr(obj, "num_shares", None)),
            # Same convention as TER: 0 in the catalogue means undisclosed.
            fixed_commission_pct=quotation_to_decimal(getattr(obj, "fixed_commission", None)) or None,
            liquidity_flag=bool(getattr(obj, "liquidity_flag", False)),
            for_iis_flag=bool(getattr(obj, "for_iis_flag", False)),
            released_date=_bond_date(getattr(obj, "released_date", None)),
        )

    def extra_filter(obj, hit: EtfScreenHit) -> bool:
        if sector and (hit.sector or "").lower() != sector.lower():
            return False
        if excluded and (hit.sector or "").lower() in excluded:
            return False
        # Shells holding frozen foreign assets stay in the catalogue with
        # api_trade_available=true but never print a price — never a candidate.
        if hit.asset_class == "blocked":
            return False
        if focus_type:
            if (hit.focus_type or "").lower() != focus_type.lower():
                return False
            allowed = _FOCUS_TO_ASSET_CLASSES.get(focus_type.lower())
            if allowed and hit.asset_class is not None and hit.asset_class not in allowed:
                return False
        return True

    fees_active = include_fees or sort_by in _ETF_FEE_KEYS

    post_filters: list = []
    required_components: set = set()
    if min_avg_daily_turnover is not None:
        post_filters.append(_liquidity_post_filter(min_avg_daily_turnover))
        include_analytics = True
        # The bar cannot be enforced without the candles it is measured from.
        required_components.add("candles")
    if sort_by in _CANDLE_SORT_KEYS:
        include_analytics = True
        required_components.add("candles")

    pool_enrichers: list = []
    pool_sort_keys: set = set()
    pool_cap = None
    if fees_active:
        workers = max(1, settings.analytics_concurrency) if settings is not None else 1
        pool_enrichers.append(lambda a, rows: _attach_etf_fees(a, rows, workers=workers))
        pool_sort_keys |= _ETF_FEE_KEYS
        # Fees alone are cheap enough to rank the whole filtered set; when
        # analytics also runs, its tighter per-row budget bounds the pool.
        pool_cap = analytics_limit if include_analytics else _ETF_FEE_POOL_CAP

    return _screen(
        adapter,
        settings,
        "etf",
        builder=builder,
        extra_filter=extra_filter,
        cheap_keys=_ETF_CHEAP_KEYS,
        analytics_keys=_ETF_ANALYTICS_KEYS,
        currency=currency,
        api_trade_available=api_trade_available,
        qualified_only=qualified_only,
        sort_by=sort_by,
        descending=descending,
        include_analytics=include_analytics,
        analytics_limit=analytics_limit,
        limit=limit,
        pool_enrichers=tuple(pool_enrichers),
        pool_sort_keys=frozenset(pool_sort_keys),
        pool_cap=pool_cap,
        post_filters=tuple(post_filters),
        required_analytics_components=frozenset(required_components),
    )


# ---------------------------------------------------------------------------
# Instrument analytics (yield + risk signals)
# ---------------------------------------------------------------------------

# Sessions used for the liquidity averages (avg volume / turnover).
_LIQUIDITY_SAMPLE_DAYS = 30


def _instrument_fx(adapter: TInvestAdapter, instrument) -> tuple[str | None, FxRate | None]:
    """(denomination currency, ruble rate) for an instrument.

    Bond prices are a percent of the NOMINAL, so a yuan nominal makes every
    price-derived figure a yuan figure even when the board settles in rubles —
    the denomination, not ``instrument.currency``, decides what a number means.
    Returns a ``None`` rate for ruble instruments (nothing to convert) and also
    when the FX board cannot be read, so callers must distinguish the two by
    checking the currency.
    """
    denom = denomination_currency(getattr(instrument, "currency", None), getattr(instrument, "nominal_currency", None))
    if not is_fx_linked(denom):
        return denom, None
    return denom, get_fx_rate(adapter, denom)


def _liquidity_from_candles(
    candles: list,
    *,
    lot: int | None,
    nominal: Decimal | None,
    is_bond: bool,
    days: int = _LIQUIDITY_SAMPLE_DAYS,
) -> tuple[Decimal | None, Decimal | None, int]:
    """(avg_daily_volume_lots, avg_daily_turnover_money, days_sampled) from daily candles.

    Candle volume is in LOTS. Money value per unit: bonds are quoted as % of
    nominal (money = nominal * close/100), everything else trades in currency.
    Best-effort: candles without volume yield (None, None, 0).
    """
    sample = candles[-days:]
    volumes: list[tuple[Decimal, Decimal | None]] = []
    for c in sample:
        vol = getattr(c, "volume", None)
        if vol is None or vol <= 0:
            continue
        close = quotation_to_decimal(getattr(c, "close", None))
        volumes.append((Decimal(vol), close))
    if not volumes:
        return None, None, 0

    n = len(volumes)
    avg_lots = (sum(v for v, _ in volumes) / n).quantize(Decimal("0.01"))

    lot_size = Decimal(lot or 1)
    turnover_sum = _ZERO
    turnover_days = 0
    for vol, close in volumes:
        if close is None or close <= 0:
            continue
        unit_money = (nominal * close / Decimal(100)) if (is_bond and nominal) else close
        turnover_sum += vol * lot_size * unit_money
        turnover_days += 1
    avg_turnover = (turnover_sum / turnover_days).quantize(Decimal("0.01")) if turnover_days else None
    return avg_lots, avg_turnover, n


def _candle_metrics(closes: list[Decimal]) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Return (historical_return_pct, annualized_volatility_pct, max_drawdown_pct)."""
    closes = [c for c in closes if c is not None and c > 0]
    if len(closes) < 2:
        return None, None, None
    hundred = Decimal(100)
    total_return = (closes[-1] / closes[0] - 1) * hundred

    rets = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1 if n > 1 else 1)
    try:
        vol_daily = var.sqrt()
        vol_annual = vol_daily * Decimal(252).sqrt() * hundred
    except Exception:
        vol_annual = None

    peak = closes[0]
    max_dd = Decimal(0)
    for c in closes:
        if c > peak:
            peak = c
        dd = (peak - c) / peak
        if dd > max_dd:
            max_dd = dd
    return total_return, vol_annual, (max_dd * hundred)


_RISK_LEVEL_MAP = {
    "RISK_LEVEL_LOW": "low",
    "RISK_LEVEL_MODERATE": "moderate",
    "RISK_LEVEL_HIGH": "high",
}

# Ordering for the max_bond_risk_level cap ("low" admits only low, etc.).
_BOND_RISK_RANK = {"low": 0, "moderate": 1, "high": 2}

_COUPON_TYPE_MAP = {
    "COUPON_TYPE_CONSTANT": "constant",
    "COUPON_TYPE_FLOATING": "floating",
    "COUPON_TYPE_DISCOUNT": "discount",
    "COUPON_TYPE_MORTGAGE": "mortgage",
    "COUPON_TYPE_FIX": "fix",
    "COUPON_TYPE_VARIABLE": "variable",
    "COUPON_TYPE_OTHER": "other",
}

_EVENT_TYPE_MAP = {
    "EVENT_TYPE_CPN": "coupon",
    "EVENT_TYPE_CALL": "call",
    "EVENT_TYPE_MTY": "maturity",
    "EVENT_TYPE_CONV": "conversion",
}


def _bond_date(value):
    """Normalize an SDK bond date to a ``date``, treating the epoch-zero / pre-1971
    sentinel (used for 'no date', e.g. undated/callable bonds) as missing."""
    if value is None:
        return None
    d = value.date() if isinstance(value, datetime) else value
    try:
        if d.year <= 1971:
            return None
    except AttributeError:
        return None
    return d


def _npv(rate: float, cfs: list[tuple[float, float]]) -> float:
    return sum(amt / (1.0 + rate) ** t for t, amt in cfs)


def _solve_ytm(dirty_price: float, cfs: list[tuple[float, float]]) -> float | None:
    """Solve the annual effective yield where discounted cashflows == dirty price.

    ``cfs`` is a list of (years_from_now, amount) future cashflows. Uses bisection
    (NPV is monotonically decreasing in the rate), returning the rate as a decimal
    fraction (0.13 = 13%) or ``None`` when it cannot be bracketed.
    """
    cfs = [(t, a) for t, a in cfs if t > 0 and a > 0]
    if not cfs or dirty_price <= 0:
        return None
    lo, hi = -0.9499, 10.0
    f_lo = _npv(lo, cfs) - dirty_price
    f_hi = _npv(hi, cfs) - dirty_price
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = _npv(mid, cfs) - dirty_price
        if abs(f_mid) < 1e-7:
            return mid
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def _compute_bond_ytm(
    *,
    now: datetime,
    nominal: Decimal | None,
    clean_price_pct: Decimal | None,
    aci: Decimal | None,
    coupons: list,
    maturity: date | None,
    call: date | None,
) -> tuple[Decimal | None, bool | None]:
    """Yield-to-worst across the maturity and the call/offer horizons.

    Bonds are quoted as a percent of nominal, so the dirty money price is
    ``nominal * price%/100 + ACI``. Principal is assumed redeemed at par on the
    horizon date (a standard approximation for puttable/callable bonds).
    Returns ``(ytm_percent, to_offer)`` or ``(None, None)``.
    """
    if not nominal or nominal <= 0 or not clean_price_pct or clean_price_pct <= 0:
        return None, None
    dirty = float(nominal * clean_price_pct / Decimal(100) + (aci or _ZERO))
    if dirty <= 0:
        return None, None

    # Coupon cashflows by pay date (absolute money per bond).
    coupon_cfs: list[tuple[date, float]] = []
    for c in coupons:
        cd = _bond_date(getattr(c, "coupon_date", None))
        val = money_to_decimal(getattr(c, "pay_one_bond", None))
        if cd and val and val > 0:
            coupon_cfs.append((cd, float(val)))

    horizons: list[tuple[date, bool]] = []
    if maturity and maturity > now.date():
        horizons.append((maturity, False))
    if call and call > now.date():
        horizons.append((call, True))
    if not horizons:
        return None, None

    par = float(nominal)
    best: tuple[Decimal, bool] | None = None
    for horizon, to_offer in horizons:
        cfs: list[tuple[float, float]] = []
        for cd, val in coupon_cfs:
            if cd <= horizon:
                cfs.append(((cd - now.date()).days / 365.0, val))
        cfs.append(((horizon - now.date()).days / 365.0, par))
        rate = _solve_ytm(dirty, cfs)
        if rate is None:
            continue
        ytm = (Decimal(str(rate)) * Decimal(100)).quantize(Decimal("0.01"))
        # Yield-to-worst: keep the lowest yield across horizons.
        if best is None or ytm < best[0]:
            best = (ytm, to_offer)
    if best is None:
        return None, None
    return best[0], best[1]


def get_instrument_analytics(adapter: TInvestAdapter, settings: Settings, uid: str) -> InstrumentAnalytics:
    """Yield + risk signals for one instrument.

    Each sub-fetch is best-effort: a failure leaves that field empty rather than
    failing the whole call. Computed yields/returns are approximations, never a
    guarantee — surfaced in ``notes``.
    """
    instrument = load_instrument(adapter, uid)
    itype = (instrument.instrument_type or "").lower()
    # Seed values only: pydantic copies these lists into the model, so every
    # append AFTER `out` is built must go through `out.notes` / the model's own
    # `unavailable_components` to actually reach the caller.
    notes: list[str] = ["Computed figures are estimates from market data, not guaranteed future returns."]
    unavailable_components: list[str] = []

    # Current price.
    price: Decimal | None = None
    try:
        snap = get_market_snapshot(adapter, settings, uid)
        price = snap.last_price
    except Exception:
        price = None
        unavailable_components.append("market_snapshot")

    # Denomination currency drives what every yield and money figure below MEANS.
    denom, fx_rate = _instrument_fx(adapter, instrument)

    out = InstrumentAnalytics(
        instrument_uid=uid,
        ticker=instrument.ticker,
        name=instrument.name,
        instrument_type=itype,
        currency=instrument.currency or "rub",
        nominal_currency=instrument.nominal_currency,
        fx_rate_rub=fx_rate.rate if fx_rate else None,
        current_price=price,
        nominal=instrument.nominal,
        maturity_date=instrument.maturity_date,
        unavailable_components=unavailable_components,
        notes=notes,
    )
    if is_fx_linked(denom):
        out.notes.append(fx_note(denom, fx_rate) or "")
        if fx_rate is None:
            out.unavailable_components.append("fx_rate")

    _fill_analytics_from_market_data(
        adapter,
        out,
        lot=instrument.lot,
        denom=denom,
        fx_rate=fx_rate,
    )
    return out


def _fill_analytics_from_market_data(
    adapter: TInvestAdapter,
    out: InstrumentAnalytics,
    *,
    lot: int | None,
    denom: str | None,
    fx_rate,
    aci: Decimal | None = None,
    maturity: date | None = None,
    call: date | None = None,
    load_bond_details: bool = True,
) -> None:
    """Fetch candles (plus coupons for bonds, dividends for shares) onto *out*.

    Shared by the standalone analytics tool and the screening path. ``out`` must
    already carry the identity fields, ``current_price`` and ``nominal``; every
    field filled here needs a market-data round trip.

    A screener already holds the catalogue record, so it passes the bond fields
    it read for free together with ``load_bond_details=False`` — that drops the
    ``BondBy`` call, which for a screen would only re-fetch data the row has.
    """
    uid = out.instrument_uid
    itype = (out.instrument_type or "").lower()
    now = _now()
    # Historical metrics from ~1y of daily candles.
    try:
        candles = adapter.get_daily_candles(uid, now - timedelta(days=370), now)
        closes = [quotation_to_decimal(getattr(c, "close", None)) for c in candles]
        if out.current_price is None and closes:
            out.current_price = closes[-1]
        ret, vol, dd = _candle_metrics([c for c in closes if c is not None])
        out.history_days = len([c for c in closes if c is not None]) or None
        out.historical_return_pct = ret
        out.volatility_annual_pct = vol
        out.max_drawdown_pct = dd
        avg_lots, avg_turnover, _ = _liquidity_from_candles(
            candles,
            lot=lot,
            nominal=out.nominal,
            is_bond=itype == "bond",
        )
        out.avg_daily_volume_lots = avg_lots
        out.avg_daily_turnover = avg_turnover
        out.avg_daily_turnover_rub = to_rub(avg_turnover, denom, fx_rate)
    except Exception:
        out.unavailable_components.append("candles")
        out.notes.append("Historical candle data unavailable.")

    price = out.current_price

    if itype == "bond":
        if load_bond_details:
            aci = None
            maturity = _bond_date(out.maturity_date)
            call = None
            try:
                bond = adapter.get_bond_by_uid(uid)
                out.risk_level = _RISK_LEVEL_MAP.get(enum_name(getattr(bond, "risk_level", None)) or "")
                out.coupons_per_year = int(getattr(bond, "coupon_quantity_per_year", 0) or 0) or None
                out.initial_nominal = money_to_decimal(getattr(bond, "initial_nominal", None))
                out.liquidity_flag = bool(getattr(bond, "liquidity_flag", False))
                out.for_iis_flag = bool(getattr(bond, "for_iis_flag", False))
                out.issue_kind = (getattr(bond, "issue_kind", "") or "") or None
                out.issue_size = int(getattr(bond, "issue_size", 0) or 0) or None
                aci = money_to_decimal(getattr(bond, "aci_value", None))
                maturity = _bond_date(getattr(bond, "maturity_date", None))
                call = _bond_date(getattr(bond, "call_date", None))
                out.maturity_date = maturity
                out.call_date = call
                if getattr(bond, "floating_coupon_flag", False):
                    out.notes.append(
                        "Floating-coupon bond: future coupons/YTM are estimates from the latest fixed rate."
                    )
            except Exception:
                out.unavailable_components.append("bond_details")
                out.notes.append("Bond details unavailable.")
        try:
            # Fetch the whole remaining coupon schedule once (reused for current
            # yield and YTM). Cap the window for perpetual/undated bonds.
            far = (
                max(d for d in (maturity, call) if d) if (maturity or call) else (now + timedelta(days=365 * 30)).date()
            )
            far_dt = (
                datetime(far.year, far.month, far.day, tzinfo=UTC) + timedelta(days=1)
                if isinstance(far, date)
                else now + timedelta(days=365 * 30)
            )
            coupons = adapter.get_bond_coupons(uid, now, far_dt)
            coupons = sorted(coupons, key=lambda c: getattr(c, "coupon_date", now))
            # Current yield: coupons paid within the next 12 months.
            horizon_12m = now + timedelta(days=366)
            annual = _ZERO
            for c in coupons:
                cd = _aware(getattr(c, "coupon_date", now))
                val = money_to_decimal(getattr(c, "pay_one_bond", None))
                if val and cd <= horizon_12m:
                    annual += val
            if coupons:
                first = coupons[0]
                out.next_coupon_date = _bond_date(getattr(first, "coupon_date", None))
                out.next_coupon_value = money_to_decimal(getattr(first, "pay_one_bond", None))
            money_price = price
            if out.nominal and price and price > 0:
                money_price = out.nominal * price / Decimal(100)
            if annual > 0 and money_price and money_price > 0:
                out.current_yield_pct = (annual / money_price * Decimal(100)).quantize(Decimal("0.01"))
            # Yield to maturity / offer.
            ytm, to_offer = _compute_bond_ytm(
                now=now,
                nominal=out.nominal,
                clean_price_pct=price,
                aci=aci,
                coupons=coupons,
                maturity=maturity,
                call=call,
            )
            out.ytm_pct = ytm
            out.ytm_to_offer = to_offer
            # Macaulay duration from the same coupon schedule (no extra calls).
            out.macaulay_duration_years = _compute_bond_macaulay_duration(
                now=now,
                nominal=out.nominal,
                clean_price_pct=price,
                aci=aci,
                coupons=coupons,
                maturity=maturity,
                call=call,
            )
            yield_ccy = (denom or BASE_CURRENCY).upper()
            out.notes.append(
                f"current_yield = next-12m coupons / clean price; ytm = effective "
                f"yield-to-worst to maturity or offer. Both are {yield_ccy} yields "
                f"(the coupon schedule and nominal are in {yield_ccy})."
            )
        except Exception:
            out.unavailable_components.append("bond_coupons")
            out.notes.append("Bond coupon data unavailable.")

    elif itype == "share":
        try:
            divs = adapter.get_dividends(uid, now - timedelta(days=370), now + timedelta(days=190))
            past = [d for d in divs if getattr(d, "payment_date", None) and _aware(d.payment_date) <= now]
            past = sorted(past, key=lambda d: d.payment_date)
            if past:
                last = past[-1]
                out.last_dividend_value = money_to_decimal(getattr(last, "dividend_net", None))
                out.last_dividend_date = getattr(last, "payment_date", None)
                y = quotation_to_decimal(getattr(last, "yield_value", None))
                if y and y > 0:
                    out.dividend_yield_pct = y
                elif out.last_dividend_value and price and price > 0:
                    out.dividend_yield_pct = out.last_dividend_value / price * Decimal(100)
            out.notes.append("Dividend yield reflects the latest payout; analyst forecasts are not guarantees.")
        except Exception:
            out.unavailable_components.append("dividends")
            out.notes.append("Dividend data unavailable.")


def _screen_analytics(adapter: TInvestAdapter, hit, itype: str) -> InstrumentAnalytics:
    """Analytics for ONE screener row, skipping what the row already carries.

    The standalone tool starts from a bare uid, so it must resolve the
    instrument and a full market snapshot first. A screener has neither need:
    the catalogue record is already in ``hit`` and ``_attach_last_prices`` has
    filled ``last_price`` for the whole pool in one batched call. Of the
    snapshot, analytics reads only ``last_price`` — the order book, trading
    status, price hints and liquidity block are computed and thrown away.

    Dropping ``load_instrument`` and ``get_market_snapshot`` takes one row from
    ~12 round trips to one (plus the coupon schedule for bonds and dividends
    for shares), which is what keeps a 25-row screen inside the per-minute
    InstrumentsService budget.
    """
    denom = denomination_currency(hit.currency, getattr(hit, "nominal_currency", None))
    fx_rate = get_fx_rate(adapter, denom) if is_fx_linked(denom) else None

    out = InstrumentAnalytics(
        instrument_uid=hit.uid,
        ticker=hit.ticker,
        name=hit.name,
        instrument_type=itype,
        currency=hit.currency or "rub",
        nominal_currency=getattr(hit, "nominal_currency", None),
        fx_rate_rub=fx_rate.rate if fx_rate else None,
        current_price=hit.last_price,
        nominal=getattr(hit, "nominal", None),
        maturity_date=getattr(hit, "maturity_date", None),
        unavailable_components=[],
        notes=["Computed figures are estimates from market data, not guaranteed future returns."],
    )
    if is_fx_linked(denom):
        out.notes.append(fx_note(denom, fx_rate) or "")
        if fx_rate is None:
            out.unavailable_components.append("fx_rate")

    _fill_analytics_from_market_data(
        adapter,
        out,
        lot=hit.lot,
        denom=denom,
        fx_rate=fx_rate,
        aci=getattr(hit, "aci_value", None),
        maturity=_bond_date(getattr(hit, "maturity_date", None)),
        call=_bond_date(getattr(hit, "call_date", None)),
        load_bond_details=False,
    )
    return out


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _as_date(value):
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


def _bond_far_dt(now: datetime, maturity: date | None, call: date | None) -> datetime:
    """Upper bound for fetching the remaining schedule of a (possibly undated) bond."""
    far = max([d for d in (maturity, call) if d], default=None)
    if far is None:
        return now + timedelta(days=365 * 30)
    return datetime(far.year, far.month, far.day, tzinfo=UTC) + timedelta(days=1)


def get_bond_schedule(adapter: TInvestAdapter, settings: Settings, uid: str, *, max_coupons: int = 80) -> BondSchedule:
    """Full upcoming coupon schedule + lifecycle events (call/offer, maturity) for a bond.

    Coupons come from ``GetBondCoupons`` and events from ``GetBondEvents``. For
    floating-rate bonds only the already-fixed coupons are reliable (flagged in
    ``notes``). This is the per-bond detail behind ``list_bonds``' summary fields.
    """
    instrument = load_instrument(adapter, uid)
    if (instrument.instrument_type or "").lower() != "bond":
        raise TInvestInstrumentNotFoundError("Instrument is not a bond")

    now = _now()
    maturity = _bond_date(instrument.maturity_date)
    call = _bond_date(instrument.call_date)
    far_dt = _bond_far_dt(now, maturity, call)
    notes: list[str] = []

    coupon_items: list[BondCouponItem] = []
    floating = False
    try:
        coupons = sorted(
            adapter.get_bond_coupons(uid, now, far_dt),
            key=lambda c: _aware(getattr(c, "coupon_date", now)),
        )
        for c in coupons[:max_coupons]:
            ct = _COUPON_TYPE_MAP.get(enum_name(getattr(c, "coupon_type", None)) or "")
            if ct in ("floating", "variable"):
                floating = True
            coupon_items.append(
                BondCouponItem(
                    coupon_date=_bond_date(getattr(c, "coupon_date", None)),
                    coupon_number=int(getattr(c, "coupon_number", 0) or 0) or None,
                    pay_one_bond=money_to_decimal(getattr(c, "pay_one_bond", None)),
                    coupon_type=ct,
                    coupon_period_days=int(getattr(c, "coupon_period", 0) or 0) or None,
                    fix_date=_bond_date(getattr(c, "fix_date", None)),
                )
            )
    except Exception:
        notes.append("Coupon schedule unavailable.")

    event_items: list[BondEventItem] = []
    try:
        events = sorted(
            adapter.get_bond_events(uid, from_=now, to=far_dt),
            key=lambda e: _aware(getattr(e, "event_date", now)),
        )
        for e in events:
            ed = _bond_date(getattr(e, "event_date", None))
            if ed is None or ed < now.date():
                continue
            event_items.append(
                BondEventItem(
                    event_type=_EVENT_TYPE_MAP.get(enum_name(getattr(e, "event_type", None)) or ""),
                    event_date=ed,
                    pay_one_bond=money_to_decimal(getattr(e, "pay_one_bond", None)),
                    fix_date=_bond_date(getattr(e, "fix_date", None)),
                )
            )
    except Exception:
        notes.append("Lifecycle events unavailable.")

    if floating:
        notes.append("Floating-rate bond: only already-fixed coupons are reliable; later coupons are estimates.")
    if not maturity and call:
        notes.append("No maturity date set — this bond is redeemed or repriced at the call/offer date.")

    return BondSchedule(
        instrument_uid=uid,
        ticker=instrument.ticker,
        name=instrument.name,
        currency=instrument.currency or None,
        nominal=instrument.nominal,
        maturity_date=maturity,
        call_date=call,
        coupons=coupon_items,
        events=event_items,
        notes=notes,
    )


def _str_field(*values) -> str | None:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def get_etf_details(
    adapter: TInvestAdapter,
    settings: Settings,
    uid: str,
) -> EtfDetails:
    """Extended ETF / BPIF metadata via ``GetAssetBy`` (AssetEtf).

    Merges the tradeable instrument record with the asset-level ETF extension:
    full fee breakdown, benchmark/index, strategy text, tracking error, etc.
    Portfolio composition and NAV premium are not exposed by the API (see
    ``notes``).
    """
    instrument = load_instrument(adapter, uid)
    if (instrument.instrument_type or "").lower() != "etf":
        raise TInvestInstrumentNotFoundError("Instrument is not an ETF")

    etf = adapter.get_etf_by_uid(uid)
    asset_uid = getattr(etf, "asset_uid", None) or None
    ext = None
    notes: list[str] = [
        "Portfolio holdings and NAV premium/discount are not available via the T-Invest API.",
    ]

    try:
        if asset_uid:
            resp = adapter.get_asset_by(asset_uid)
            asset = getattr(resp, "asset", None)
            sec = getattr(asset, "security", None) if asset else None
            ext = getattr(sec, "etf", None) if sec else None
    except Exception:
        notes.append("Extended asset metadata unavailable.")

    def qp(obj, name: str):
        if obj is None:
            return None
        return quotation_to_decimal(getattr(obj, name, None))

    focus_type = _str_field(
        getattr(ext, "focus_type", None) if ext else None,
        getattr(etf, "focus_type", None),
    )
    rebalancing_freq = _str_field(
        getattr(ext, "rebalancing_freq", None) if ext else None,
        getattr(etf, "rebalancing_freq", None),
    )
    num_shares = qp(ext, "num_share") if ext else None
    if num_shares is None:
        num_shares = quotation_to_decimal(getattr(etf, "num_shares", None))

    fixed_commission = qp(ext, "fixed_commission") if ext else None
    if fixed_commission is None:
        fixed_commission = quotation_to_decimal(getattr(etf, "fixed_commission", None))

    if ext and not any([qp(ext, "total_expense"), fixed_commission, qp(ext, "expense_commission")]):
        notes.append("Fee fields may be 0 when the issuer does not disclose them in the API.")

    return EtfDetails(
        instrument_uid=uid,
        asset_uid=asset_uid,
        ticker=instrument.ticker,
        name=instrument.name,
        currency=instrument.currency or None,
        isin=getattr(etf, "isin", None) or instrument.isin,
        focus_type=focus_type,
        rebalancing_freq=rebalancing_freq,
        rebalancing_flag=bool(getattr(ext, "rebalancing_flag", False)) if ext else None,
        num_shares=num_shares,
        released_date=_bond_date(getattr(ext, "released_date", None) if ext else None)
        or _bond_date(getattr(etf, "released_date", None))
        or instrument.released_date,
        liquidity_flag=bool(getattr(etf, "liquidity_flag", False)),
        for_iis_flag=bool(getattr(etf, "for_iis_flag", False)),
        fixed_commission_pct=fixed_commission,
        total_expense_pct=qp(ext, "total_expense"),
        expense_commission_pct=qp(ext, "expense_commission"),
        hurdle_rate_pct=qp(ext, "hurdle_rate"),
        performance_fee_pct=qp(ext, "performance_fee"),
        payment_type=_str_field(getattr(ext, "payment_type", None) if ext else None),
        primary_index=_str_field(getattr(ext, "primary_index", None) if ext else None),
        primary_index_description=_str_field(getattr(ext, "primary_index_description", None) if ext else None),
        primary_index_company=_str_field(getattr(ext, "primary_index_company", None) if ext else None),
        tracking_error_pct=qp(ext, "primary_index_tracking_error"),
        management_type=_str_field(getattr(ext, "management_type", None) if ext else None),
        leveraged_flag=bool(getattr(ext, "leveraged_flag", False)) if ext else None,
        div_yield_flag=bool(getattr(ext, "div_yield_flag", False)) if ext else None,
        ucits_flag=bool(getattr(ext, "ucits_flag", False)) if ext else None,
        description=_str_field(getattr(ext, "description", None) if ext else None),
        buy_premium_pct=qp(ext, "buy_premium"),
        sell_discount_pct=qp(ext, "sell_discount"),
        inav_code=_str_field(getattr(ext, "inav_code", None) if ext else None),
        tax_rate=_str_field(getattr(ext, "tax_rate", None) if ext else None),
        rebalancing_plan=_str_field(getattr(ext, "rebalancing_plan", None) if ext else None),
        issue_kind=_str_field(getattr(ext, "issue_kind", None) if ext else None),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Fundamentals (P/E, ROE, margins, debt, dividends, growth …)
# ---------------------------------------------------------------------------


def get_instrument_fundamentals(adapter: TInvestAdapter, settings: Settings, uid: str) -> InstrumentFundamentals:
    """Company financial metrics & valuation ratios for one instrument.

    Resolves the instrument's asset id, then pulls ``GetAssetFundamentals`` — the
    same numbers shown on the app's "Показатели" tab (P/E, P/B, P/S, EV/EBITDA,
    ROE/ROA/ROIC, margins, debt ratios, dividend yield, growth). Third-party
    vendor data; missing values stay ``None`` (see ``notes``).
    """
    raw = adapter.get_instrument_by_uid(uid)
    if raw is None:
        raise TInvestInstrumentNotFoundError("Instrument not found")
    asset_uid = getattr(raw, "asset_uid", None) or None
    notes: list[str] = [
        "Fundamentals are provided by a third-party data vendor; some fields may be empty.",
    ]
    out = InstrumentFundamentals(
        instrument_uid=uid,
        asset_uid=asset_uid,
        ticker=getattr(raw, "ticker", "") or "",
        name=getattr(raw, "name", "") or "",
        currency=(getattr(raw, "currency", "") or "") or None,
        notes=notes,
    )
    if not asset_uid:
        notes.append("This instrument has no linked asset id; fundamentals are unavailable.")
        return out

    try:
        stats = adapter.get_asset_fundamentals([asset_uid])
    except Exception:
        notes.append("Fundamentals are unavailable for this instrument.")
        return out
    s = stats[0] if stats else None
    if s is None:
        notes.append("No fundamentals reported for this instrument.")
        return out

    out.currency = (getattr(s, "currency", "") or "") or out.currency
    # Size / market
    out.market_cap = _fnum(getattr(s, "market_capitalization", None))
    out.shares_outstanding = _fnum(getattr(s, "shares_outstanding", None))
    out.free_float_pct = _fnum(getattr(s, "free_float", None))
    out.beta = _fnum(getattr(s, "beta", None))
    out.high_52w = _fnum(getattr(s, "high_price_last_52_weeks", None))
    out.low_52w = _fnum(getattr(s, "low_price_last_52_weeks", None))
    # Valuation
    out.pe_ratio = _fnum(getattr(s, "pe_ratio_ttm", None))
    out.price_to_sales = _fnum(getattr(s, "price_to_sales_ttm", None))
    out.price_to_book = _fnum(getattr(s, "price_to_book_ttm", None))
    out.price_to_fcf = _fnum(getattr(s, "price_to_free_cash_flow_ttm", None))
    out.ev_to_ebitda = _fnum(getattr(s, "ev_to_ebitda_mrq", None))
    out.ev_to_sales = _fnum(getattr(s, "ev_to_sales", None))
    out.enterprise_value = _fnum(getattr(s, "total_enterprise_value_mrq", None))
    # Profitability
    out.roe_pct = _fnum(getattr(s, "roe", None))
    out.roa_pct = _fnum(getattr(s, "roa", None))
    out.roic_pct = _fnum(getattr(s, "roic", None))
    out.net_margin_pct = _fnum(getattr(s, "net_margin_mrq", None))
    # Income / per-share
    out.eps_ttm = _fnum(getattr(s, "eps_ttm", None))
    out.diluted_eps_ttm = _fnum(getattr(s, "diluted_eps_ttm", None))
    out.revenue_ttm = _fnum(getattr(s, "revenue_ttm", None))
    out.ebitda_ttm = _fnum(getattr(s, "ebitda_ttm", None))
    out.net_income_ttm = _fnum(getattr(s, "net_income_ttm", None))
    out.free_cash_flow_ttm = _fnum(getattr(s, "free_cash_flow_ttm", None))
    # Leverage / liquidity
    out.total_debt = _fnum(getattr(s, "total_debt_mrq", None))
    out.debt_to_equity = _fnum(getattr(s, "total_debt_to_equity_mrq", None))
    out.net_debt_to_ebitda = _fnum(getattr(s, "net_debt_to_ebitda", None))
    out.current_ratio = _fnum(getattr(s, "current_ratio_mrq", None))
    # Dividends
    out.dividend_yield_pct = _fnum(getattr(s, "dividend_yield_daily_ttm", None))
    out.dividend_rate_ttm = _fnum(getattr(s, "dividend_rate_ttm", None))
    out.dividend_payout_ratio_pct = _fnum(getattr(s, "dividend_payout_ratio_fy", None))
    out.dividends_per_share = _fnum(getattr(s, "dividends_per_share", None))
    out.ex_dividend_date = _as_date(getattr(s, "ex_dividend_date", None))
    # Growth
    out.revenue_growth_5y_pct = _fnum(getattr(s, "five_year_annual_revenue_growth_rate", None))
    out.revenue_growth_3y_pct = _fnum(getattr(s, "three_year_annual_revenue_growth_rate", None))
    out.revenue_growth_1y_pct = _fnum(getattr(s, "one_year_annual_revenue_growth_rate", None))

    return out


# ---------------------------------------------------------------------------
# Analyst forecast (consensus rating + target prices)
# ---------------------------------------------------------------------------


def get_instrument_forecast(adapter: TInvestAdapter, settings: Settings, uid: str) -> AnalystForecast:
    """Analyst consensus + per-analyst targets for one instrument (GetForecastBy).

    Returns the consensus recommendation (buy/hold/sell), the consensus target
    price with its min/max band, the implied upside vs. the current price, the
    buy/hold/sell analyst split and each analyst's individual target. This is a
    THIRD-PARTY opinion, not a market fact and not a guarantee (see ``notes``).
    """
    instrument = load_instrument(adapter, uid)
    notes: list[str] = [
        "Analyst consensus is a third-party opinion, not a guarantee of future price.",
    ]
    out = AnalystForecast(
        instrument_uid=uid,
        ticker=instrument.ticker,
        name=instrument.name,
        currency=instrument.currency or "rub",
        notes=notes,
    )

    try:
        resp = adapter.get_forecast(uid)
    except Exception:
        notes.append("Analyst forecast is unavailable for this instrument.")
        return out

    consensus = getattr(resp, "consensus", None)
    if consensus is not None:
        out.recommendation = _RECOMMENDATION_MAP.get(enum_name(getattr(consensus, "recommendation", None)) or "")
        out.current_price = quotation_to_decimal(getattr(consensus, "current_price", None))
        out.consensus_target_price = quotation_to_decimal(getattr(consensus, "consensus", None))
        out.min_target_price = quotation_to_decimal(getattr(consensus, "min_target", None))
        out.max_target_price = quotation_to_decimal(getattr(consensus, "max_target", None))
        out.upside_pct = quotation_to_decimal(getattr(consensus, "price_change_rel", None))

    targets: list[AnalystTarget] = []
    buy = hold = sell = 0
    for t in getattr(resp, "targets", []) or []:
        rec = _RECOMMENDATION_MAP.get(enum_name(getattr(t, "recommendation", None)) or "")
        if rec == "buy":
            buy += 1
        elif rec == "hold":
            hold += 1
        elif rec == "sell":
            sell += 1
        targets.append(
            AnalystTarget(
                company=(getattr(t, "company", "") or "") or None,
                recommendation=rec,
                target_price=quotation_to_decimal(getattr(t, "target_price", None)),
                current_price=quotation_to_decimal(getattr(t, "current_price", None)),
                upside_pct=quotation_to_decimal(getattr(t, "price_change_rel", None)),
                currency=(getattr(t, "currency", "") or "") or None,
                recommendation_date=getattr(t, "recommendation_date", None),
            )
        )
    out.targets = targets
    out.analysts_buy = buy or None
    out.analysts_hold = hold or None
    out.analysts_sell = sell or None
    out.analyst_count = len(targets) or None

    # Fall back to a computed upside if the API did not provide a relative change.
    if out.upside_pct is None and out.consensus_target_price and out.current_price and out.current_price > 0:
        out.upside_pct = ((out.consensus_target_price / out.current_price - 1) * Decimal(100)).quantize(Decimal("0.01"))

    return out


# ---------------------------------------------------------------------------
# Proposal creation (risk engine + preview)
# ---------------------------------------------------------------------------

_LDV_YEARS = 3  # НК РФ ст. 219.1: 3 года владения → льгота долгосрочного владения


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # Feb 29 -> Feb 28
        return d.replace(year=d.year + years, day=28)


def _find_position(portfolio: PortfolioSummary, uid: str) -> PortfolioPosition | None:
    for pos in portfolio.positions:
        if pos.instrument_uid == uid:
            return pos
    return None


def _earliest_buy_date(adapter: TInvestAdapter, account_id: str, uid: str) -> date | None:
    """Earliest executed BUY of the instrument — approximate start of the holding period."""
    try:
        to_dt = _now()
        from_dt = to_dt - timedelta(days=365 * (_LDV_YEARS + 2))
        earliest: date | None = None
        cursor = ""
        for _ in range(20):  # page cap; sandbox histories are short
            resp = adapter.get_operations_by_cursor(
                account_id,
                from_=from_dt,
                to=to_dt,
                cursor=cursor,
                limit=100,
                operation_types=[OperationType.OPERATION_TYPE_BUY],
                state=OperationState.OPERATION_STATE_EXECUTED,
                instrument_id=uid,
            )
            for item in getattr(resp, "items", []) or []:
                dt = getattr(item, "date", None)
                if dt is not None and (earliest is None or dt.date() < earliest):
                    earliest = dt.date()
            cursor = getattr(resp, "next_cursor", "") or ""
            if not getattr(resp, "has_next", False) or not cursor:
                break
        return earliest
    except Exception:
        return None


def _sell_context_fields(
    adapter: TInvestAdapter,
    settings: Settings,
    *,
    account_id: str,
    instrument: InvestmentInstrument,
    position: PortfolioPosition | None,
    quantity_lots: int,
) -> tuple[dict[str, object], SellTaxImpact]:
    """SELL inputs for the risk engine plus the preview tax block (stage 7)."""
    fields: dict[str, object] = {}
    tax_notes: list[str] = []
    lot = max(1, instrument.lot)

    estimated_gain: Decimal | None = None
    estimated_tax: Decimal | None = None
    if position is not None:
        units_held = _d(position.quantity)
        sellable = units_held - _d(position.blocked)
        fields["position_available_lots"] = int(sellable // lot) if sellable > 0 else 0
        pnl = position.expected_yield_absolute
        if pnl is not None and units_held > 0:
            # Proportional share of the CURRENT unrealized result — unit-safe for
            # bonds (% of nominal) and shares alike; the broker's FIFO may differ.
            units_to_sell = min(Decimal(quantity_lots * lot), units_held)
            estimated_gain = (pnl * units_to_sell / units_held).quantize(Decimal("0.01"))
            estimated_tax = (
                (estimated_gain * settings.sell_tax_rate).quantize(Decimal("0.01")) if estimated_gain > 0 else _ZERO
            )
            tax_notes.append(
                "Estimate from the position's current unrealized result; the broker's FIFO "
                "lot accounting, commissions and ЛДВ may change the actual tax."
            )
        else:
            tax_notes.append("Position P&L unavailable — gain/tax not estimated.")
    else:
        tax_notes.append("No position found — gain/tax not estimated.")
    fields["estimated_gain"] = estimated_gain
    fields["estimated_tax"] = estimated_tax

    earliest = _earliest_buy_date(adapter, account_id, instrument.uid)
    if earliest is not None:
        fields["ldv_eligible_on"] = _add_years(earliest, _LDV_YEARS)

    today = date.today()
    events = [
        (kind, when)
        for kind, when in (("call/offer", instrument.call_date), ("maturity", instrument.maturity_date))
        if when is not None and when >= today
    ]
    if events:
        kind, when = min(events, key=lambda e: e[1])
        fields["corporate_action_kind"] = kind
        fields["corporate_action_date"] = when

    tax_impact = SellTaxImpact(
        estimated_gain=estimated_gain,
        estimated_tax=estimated_tax,
        tax_rate_pct=(settings.sell_tax_rate * Decimal(100)).normalize(),
        notes=tax_notes,
    )
    return fields, tax_impact


@dataclass(frozen=True)
class _OrderCost:
    """What one order really costs, in rubles and in its own currency."""

    total_rub: Decimal  # BUY: cash outlay; SELL: gross proceeds
    commission_rub: Decimal
    native_total: Decimal  # the same total before conversion
    native_commission: Decimal
    currency: str | None  # currency the native figures are in
    fx_rate: FxRate | None  # None for ruble instruments


def _order_cost(
    adapter: TInvestAdapter,
    account_id: str,
    instrument: InvestmentInstrument,
    price: Decimal,
    quantity_lots: int,
    direction: str,
) -> _OrderCost:
    """Cost of an order, computed locally and priced in rubles.

    The clean value is derived here rather than taken from ``GetOrderPrice``:
    that RPC treats ``price`` as MONEY PER UNIT, while bonds are quoted (and
    ordered) as a percent of nominal, so feeding it the quote returns a total an
    order of magnitude too small — 98 instead of 980 for a 1000-nominal bond, and
    121 ₽ instead of ~11 400 ₽ for a yuan bond on a ruble board. Those totals
    feed ``max_order_rub`` and the cash check, so they have to be right.

    The broker is still the source for the two things it reports in real money:
    commission and accrued interest (which it converts to the settlement currency
    itself). It is called with the money price so its commission is scaled to the
    actual trade size.
    """
    is_sell = direction.upper() == "SELL"
    denom, fx_rate = _instrument_fx(adapter, instrument)
    native_clean = _step_money_value(instrument, price, quantity_lots)

    clean_rub = to_rub(native_clean, denom, fx_rate)
    if clean_rub is None:
        raise TInvestDataUnavailableError(
            f"Order value is denominated in {(denom or '').upper()} and the "
            f"{(denom or '').upper()}/RUB rate could not be read, so it cannot be checked "
            "against the ruble risk limits or your cash balance. Retry once the FX board "
            "is reachable."
        )

    # Money price per unit in the SETTLEMENT currency — what OrdersService wants.
    settlement = normalize_currency(instrument.currency) or denom
    unit_money = _unit_money_price(instrument, price, denom=denom, fx_rate=fx_rate)

    commission_rub = _ZERO
    native_commission = _ZERO
    aci_rub = _ZERO
    try:
        price_resp = adapter.get_order_price(
            account_id,
            instrument.uid,
            unit_money,
            direction,
            quantity_lots,
        )
        raw_commission = _d(money_to_decimal(getattr(price_resp, "executed_commission", None)))
        commission_currency = currency_of(getattr(price_resp, "executed_commission", None)) or settlement
        native_commission = raw_commission
        commission_rub = _d(_amount_to_rub(adapter, raw_commission, commission_currency))

        extra_bond = getattr(price_resp, "extra_bond", None)
        aci_value = getattr(extra_bond, "aci_value", None) if extra_bond is not None else None
        aci_rub = _d(
            _amount_to_rub(
                adapter,
                _d(money_to_decimal(aci_value)),
                currency_of(aci_value) or settlement,
            )
        )
    except Exception as exc:
        logger.warning(
            "GetOrderPrice failed for %s — totals fall back to price*lots without commission/ACI: %s",
            instrument.ticker or instrument.uid,
            format_broker_error(broker_error_meta(exc)),
        )
        commission_rub = _ZERO
        native_commission = _ZERO
        aci_rub = _ZERO

    # BUY: full cash outlay; SELL: gross proceeds (commission netted in the preview).
    total_rub = clean_rub + aci_rub + (_ZERO if is_sell else commission_rub)
    scale = (total_rub / clean_rub) if clean_rub > 0 else Decimal(1)
    return _OrderCost(
        total_rub=total_rub.quantize(_PLAN_CENT),
        commission_rub=commission_rub,
        native_total=(native_clean * scale).quantize(_PLAN_CENT),
        native_commission=native_commission,
        currency=denom,
        fx_rate=fx_rate,
    )


def _unit_money_price(
    instrument: InvestmentInstrument,
    quote_price: Decimal,
    *,
    denom: str | None,
    fx_rate: FxRate | None,
) -> Decimal:
    """Convert a QUOTED price into MONEY PER UNIT in the settlement currency.

    Every OrdersService RPC — ``GetOrderPrice``, ``GetMaxLots`` and, critically,
    ``PostOrder`` — takes ``price`` as money per unit, never as the quote. Bonds
    are quoted as a percent of nominal, so sending the raw quote makes the broker
    read 99.93 ₽ for a bond whose price band is ~598-1396 ₽ and reject the order
    with ``INVALID_ARGUMENT 30099`` ("price is outside the limits"). Shares/ETFs
    are quoted in currency, so for them this is the identity.
    """
    units = Decimal(max(1, instrument.lot))
    native_unit = _step_money_value(instrument, quote_price, 1) / units
    settlement = normalize_currency(instrument.currency) or denom
    if settlement != denom:
        converted = to_rub(native_unit, denom, fx_rate) if settlement == BASE_CURRENCY else None
        if converted is not None:
            return converted
    return native_unit


def _amount_to_rub(adapter: TInvestAdapter, amount: Decimal | None, currency: str | None) -> Decimal | None:
    """Convert a broker-reported amount to rubles, resolving its rate on demand."""
    if amount is None:
        return None
    if not is_fx_linked(currency):
        return Decimal(amount)
    return to_rub(amount, currency, get_fx_rate(adapter, currency))


def create_order_proposal(
    adapter: TInvestAdapter,
    settings: Settings,
    *,
    instrument_uid: str,
    direction: str,
    order_type: str,
    quantity_lots: int,
    limit_price: Decimal | None = None,
    urgency: str | None = None,
    rationale: str | None = None,
    user_request_id: str | None = None,
) -> OrderPreview:
    direction = direction.upper()
    order_type = order_type.upper()
    is_sell = direction == "SELL"

    account_id = resolve_account_id(adapter, settings)
    instrument = load_instrument(adapter, instrument_uid)
    snapshot = get_market_snapshot(adapter, settings, instrument_uid)
    portfolio = get_portfolio_summary(adapter, settings)
    session_state = _current_session_state(settings)

    if is_sell:
        hints = compute_sell_price_hints(snapshot, instrument, settings, session_state=session_state)
    else:
        hints = snapshot.buy_price_hints or compute_buy_price_hints(
            snapshot, instrument, settings, session_state=session_state
        )
    user_supplied_price = limit_price is not None
    try:
        chosen_price, urgency_used = resolve_buy_limit_price(
            hints,
            limit_price=limit_price,
            urgency=urgency,
        )
    except ValueError as exc:
        raise TInvestConfigurationError(str(exc)) from exc

    # Snap the limit price to the increment grid without worsening the order:
    # BUY rounds down (the fast tier up, to stay at the ask); SELL rounds up
    # (the fast tier down, to stay at the bid).
    aggressive = urgency_used == "fast" and not user_supplied_price
    norm_price = quantize_to_increment(
        chosen_price,
        instrument.min_price_increment,
        direction=direction,
        round_up=aggressive and not is_sell,
        round_down=aggressive and is_sell,
    )
    price_selection = build_price_vs_hints(
        norm_price,
        hints,
        urgency_used=urgency_used,
        user_supplied_price=user_supplied_price,
        direction=direction,
    )

    cost = _order_cost(
        adapter,
        account_id,
        instrument,
        norm_price,
        quantity_lots,
        direction,
    )
    order_total = cost.native_total
    total_currency = cost.currency
    fx_rate = cost.fx_rate
    order_total_rub = cost.total_rub
    commission_rub = cost.commission_rub

    max_lots: int | None = None
    try:
        max_resp = adapter.get_max_lots(account_id, instrument_uid, norm_price)
        limits = getattr(max_resp, "sell_limits" if is_sell else "buy_limits", None)
        if limits is not None:
            attr = "sell_max_lots" if is_sell else "buy_max_lots"
            max_lots = int(getattr(limits, attr, 0) or 0)
    except Exception as exc:
        logger.warning(
            "GetMaxLots failed for %s — MAX_LOTS check skipped: %s",
            instrument.ticker or instrument_uid,
            format_broker_error(broker_error_meta(exc)),
        )
        max_lots = None

    # Existing position for concentration math and (for SELL) the sell checks.
    position = _find_position(portfolio, instrument_uid)
    position_value_before = _d(position.current_value) if position is not None else _ZERO

    sell_fields: dict[str, object] = {}
    tax_impact: SellTaxImpact | None = None
    if is_sell:
        sell_fields, tax_impact = _sell_context_fields(
            adapter,
            settings,
            account_id=account_id,
            instrument=instrument,
            position=position,
            quantity_lots=quantity_lots,
        )

    ctx = OrderContext(
        direction=direction,
        order_type=order_type,
        quantity_lots=quantity_lots,
        limit_price=norm_price,
        order_total=order_total_rub,
        available_cash=portfolio.cash,
        portfolio_value_before=portfolio.total_value,
        position_value_before=position_value_before,
        max_lots=max_lots,
        native_total=order_total,
        native_currency=total_currency,
        fx_rate_rub=fx_rate.rate if fx_rate else None,
        **sell_fields,
    )
    checks = evaluate(ctx, instrument, snapshot, portfolio, settings, session_state=session_state)
    passed = all_passed(checks)

    weight_before = (position_value_before / portfolio.total_value) if portfolio.total_value > 0 else _ZERO
    if is_sell:
        remaining = position_value_before - order_total_rub
        weight_after = (remaining / portfolio.total_value) if portfolio.total_value > 0 and remaining > 0 else _ZERO
    else:
        weight_denominator = portfolio.total_value + order_total_rub
        weight_after = (
            ((position_value_before + order_total_rub) / weight_denominator) if weight_denominator > 0 else _ZERO
        )

    store = get_store(settings.confirmation_ttl_seconds)
    proposal = store.create(
        account_id=account_id,
        instrument_uid=instrument_uid,
        figi=instrument.figi,
        ticker=instrument.ticker,
        name=instrument.name,
        instrument_type=instrument.instrument_type,
        currency=instrument.currency or "rub",
        lot=instrument.lot,
        direction=direction,
        order_type=order_type,
        quantity_lots=quantity_lots,
        limit_price=norm_price,
        min_price_increment=instrument.min_price_increment,
        rationale=rationale,
        user_request_id=user_request_id,
        status="READY_FOR_CONFIRMATION" if passed else "RISK_REJECTED",
        reference_last_price=snapshot.last_price,
        # Stored in RUBLES so the re-validation before PostOrder checks the same
        # ruble limits this proposal was accepted against.
        estimated_total=order_total_rub,
    )

    preview = _build_preview(
        proposal,
        instrument,
        checks,
        passed,
        order_total=order_total_rub,
        commission=commission_rub,
        weight_before=weight_before,
        weight_after=weight_after,
        cash_before=portfolio.cash,
        price_selection=price_selection,
        urgency_used=urgency_used,
        tax_impact=tax_impact,
        native_total=order_total if is_fx_linked(total_currency) else None,
        native_currency=total_currency if is_fx_linked(total_currency) else None,
        fx_rate_rub=fx_rate.rate if (fx_rate and is_fx_linked(total_currency)) else None,
    )
    store.update(proposal.proposal_id, preview=preview)

    audit_event(
        "create_proposal",
        order_id=proposal.proposal_id,
        status="submitted" if passed else "failed",
        account_id=account_id,
        metadata={
            "instrument_uid": instrument_uid,
            "ticker": instrument.ticker,
            "direction": direction,
            "order_type": order_type,
            "quantity_lots": quantity_lots,
            "initial_price": str(chosen_price),
            "normalized_price": str(norm_price),
            "urgency": urgency_used,
            "user_supplied_price": user_supplied_price,
            "estimated_total": str(order_total_rub),
            "estimated_total_native": str(order_total),
            "denomination_currency": total_currency,
            "risk_passed": passed,
            "failed_checks": [c.code for c in checks if not c.passed],
        },
    )
    return preview


def _build_preview(
    proposal: OrderProposal,
    instrument: InvestmentInstrument,
    checks: list[RiskCheck],
    passed: bool,
    *,
    order_total: Decimal,
    commission: Decimal,
    weight_before: Decimal,
    weight_after: Decimal,
    cash_before: Decimal,
    price_selection: PriceVsHints | None = None,
    urgency_used: str | None = None,
    tax_impact: SellTaxImpact | None = None,
    native_total: Decimal | None = None,
    native_currency: str | None = None,
    fx_rate_rub: Decimal | None = None,
) -> OrderPreview:
    """Build the user-facing preview. Money figures are in RUBLES throughout;
    an FX-linked instrument additionally reports the untranslated figure so the
    conversion is visible rather than implied."""
    quantity_units = proposal.quantity_lots * proposal.lot
    if proposal.direction == "SELL":
        cash_after = cash_before + order_total - commission  # order_total = gross proceeds
    else:
        cash_after = cash_before - order_total
    order_block = {
        "direction": proposal.direction,
        "type": proposal.order_type,
        "quantity_lots": str(proposal.quantity_lots),
        "lot_size": str(proposal.lot),
        "quantity_units": str(quantity_units),
        "limit_price": format(proposal.limit_price, "f"),
        "price_quote_unit": instrument.price_quote_unit,
        "estimated_total": format(order_total, "f"),
        "commission": format(commission, "f"),
        "currency": BASE_CURRENCY,
        "settlement_currency": proposal.currency,
    }
    if native_currency and native_total is not None:
        order_block["denomination_currency"] = native_currency
        order_block["estimated_total_denomination_currency"] = format(native_total, "f")
        if fx_rate_rub is not None:
            order_block["fx_rate_rub"] = format(fx_rate_rub, "f")
    if urgency_used:
        order_block["urgency"] = urgency_used
    return OrderPreview(
        proposal_id=proposal.proposal_id,
        status=proposal.status,
        expires_at=proposal.expires_at,
        mode=proposal.preview.mode if proposal.preview else proposal_mode(proposal),
        instrument={
            "uid": instrument.uid,
            "ticker": instrument.ticker,
            "name": instrument.name,
            "type": instrument.instrument_type,
            "price_quote_unit": instrument.price_quote_unit,
        },
        order=order_block,
        portfolio_impact={
            "position_weight_before": format(weight_before, "f"),
            "position_weight_after": format(weight_after, "f"),
            "cash_before": format(cash_before, "f"),
            "cash_after_estimated": format(cash_after, "f"),
        },
        risk_checks=checks,
        all_passed=passed,
        rationale=proposal.rationale,
        price_selection=price_selection,
        tax_impact=tax_impact,
    )


def proposal_mode(proposal: OrderProposal) -> str:
    # Mode is captured for display; resolved from settings at build time.
    from .config.env import get_settings

    return get_settings().mode


# ---------------------------------------------------------------------------
# Plan validation (stage 7 — plan-level checks against the personal mandate)
# ---------------------------------------------------------------------------


def _step_money_value(instrument: InvestmentInstrument, price: Decimal, quantity_lots: int) -> Decimal:
    """Estimated money value of one plan step (bond prices are % of nominal)."""
    units = Decimal(quantity_lots * max(1, instrument.lot))
    if instrument.price_quote_unit == "pct_of_nominal" and instrument.nominal:
        return (instrument.nominal * price / Decimal(100) * units).quantize(Decimal("0.01"))
    return (price * units).quantize(Decimal("0.01"))


def _target_class_for(instrument: InvestmentInstrument) -> str:
    itype = (instrument.instrument_type or "").lower()
    bucket = _CLASS_LABELS.get(itype, itype or "other")
    return PORTFOLIO_CLASS_TO_TARGET.get(bucket, "other")


def _merge_issuer_category(categories: dict[str, str], issuer: str, instrument: InvestmentInstrument) -> None:
    """Record the (strictest) issuer-cap category for an issuer bucket."""
    category = issuer_cap_category(instrument.instrument_type, instrument.sector)
    existing = categories.get(issuer)
    categories[issuer] = stricter_issuer_category(existing, category) if existing else category


def _portfolio_plan_state(portfolio: PortfolioSummary, resolve_instrument, notes: list[str]) -> PlanState:
    """Current portfolio in plan-engine terms (securities bucketed by target class)."""
    position_units: dict[str, Decimal] = {}
    class_values: dict[str, Decimal] = {}
    sector_values: dict[str, Decimal] = {}
    issuer_values: dict[str, Decimal] = {}
    issuer_categories: dict[str, str] = {}
    currency_values: dict[str, Decimal] = {}
    for pos in portfolio.positions:
        try:
            instrument = resolve_instrument(pos.instrument_uid)
        except Exception:
            notes.append(f"Position {pos.ticker or pos.instrument_uid} skipped: instrument data unavailable.")
            continue
        value = _d(pos.current_value)
        sellable = _d(pos.quantity) - _d(pos.blocked)
        position_units[pos.instrument_uid] = sellable if sellable > 0 else _ZERO
        cls = _target_class_for(instrument)
        if cls != "cash":
            class_values[cls] = class_values.get(cls, _ZERO) + value
        sector = (instrument.sector or "unknown").lower()
        sector_values[sector] = sector_values.get(sector, _ZERO) + value
        issuer = instrument.name or pos.ticker or pos.instrument_uid
        issuer_values[issuer] = issuer_values.get(issuer, _ZERO) + value
        _merge_issuer_category(issuer_categories, issuer, instrument)
        # Bucket by what the position actually PAYS in, so already-held yuan
        # bonds count toward the currency exposure a plan is measured against.
        denom = denomination_currency(instrument.currency, instrument.nominal_currency) or BASE_CURRENCY
        currency_values[denom] = currency_values.get(denom, _ZERO) + value
    return PlanState(
        cash=portfolio.cash,
        position_units=position_units,
        class_values=class_values,
        sector_values=sector_values,
        issuer_values=issuer_values,
        issuer_categories=issuer_categories,
        currency_values=currency_values,
    )


def _plan_issuer_caps(
    profile: InvestmentProfile,
    settings: Settings,
    state: PlanState,
    plan_steps: list[PlanStep],
    step_instruments: dict[str, InvestmentInstrument],
    total_value: Decimal,
    notes: list[str],
) -> tuple[dict[str, Decimal | None], Decimal]:
    """Resolve per-issuer caps (features B + C1). Returns (issuer_caps, single_name_cap).

    Single-name cap scales with portfolio size unless the profile pins an explicit
    ``max_issuer_weight_pct``; funds are exempt; sovereign bonds get a higher cap.
    """
    if profile.max_issuer_weight_pct is not None:
        single_cap = profile.max_issuer_weight_pct / _HUNDRED_DEC
    else:
        base = settings.mandate_max_issuer_weight
        single_cap = scaled_issuer_cap(
            total_value,
            base_cap=base,
            small_portfolio_rub=settings.mandate_small_portfolio_rub,
            small_cap=settings.mandate_small_issuer_weight,
            mid_portfolio_rub=settings.mandate_mid_portfolio_rub,
            mid_cap=settings.mandate_mid_issuer_weight,
        )
        if single_cap > base:
            notes.append(
                f"Per-issuer cap relaxed to {(single_cap * _HUNDRED_DEC).quantize(_ZERO)}% because the "
                f"portfolio (~{total_value.quantize(_ZERO)} ₽) is below the "
                f"{settings.mandate_small_portfolio_rub.quantize(_ZERO)} ₽ small-portfolio threshold; "
                f"the full {(base * _HUNDRED_DEC).quantize(_ZERO)}% cap applies to larger portfolios."
            )
    policy = IssuerCapPolicy(
        single_cap=single_cap,
        sovereign_cap=settings.mandate_sovereign_issuer_weight,
        fund_cap=None,
    )
    categories = dict(state.issuer_categories)
    for step in plan_steps:
        instrument = step_instruments.get(step.instrument_uid)
        if instrument is not None:
            _merge_issuer_category(categories, step.issuer, instrument)
    return build_issuer_caps(categories, policy), single_cap


def validate_trade_plan(
    adapter: TInvestAdapter,
    settings: Settings,
    *,
    steps: list[TradePlanStepInput],
) -> TradePlanPreview:
    """Simulate the order sequence and check the RESULTING portfolio against the mandate.

    Plan-level twin of ``create_order_proposal``: read-only, deterministic, and
    keyed to the PERSONAL mandate (saved profile) instead of the global env
    limits. Each step still needs its own order proposal at execution time.
    """
    if not steps:
        raise TInvestConfigurationError("Plan must contain at least one step")
    profile = load_profile(settings.investment_profile_path)
    if profile is None:
        raise TInvestConfigurationError(
            "No saved investment profile — plan checks are mandate-based. "
            "Run propose_target_allocation and save_investment_profile first."
        )

    portfolio = get_portfolio_summary(adapter, settings)

    instruments: dict[str, InvestmentInstrument] = {}

    def _instrument(uid: str) -> InvestmentInstrument:
        if uid not in instruments:
            instruments[uid] = load_instrument(adapter, uid)
        return instruments[uid]

    notes: list[str] = [
        "Step values are estimates at current/limit prices, excluding commissions and taxes.",
        "Plan checks validate the RESULTING portfolio; run create_order_proposal per step "
        "(per-order checks) before executing anything.",
    ]

    state = _portfolio_plan_state(portfolio, _instrument, notes)

    plan_steps: list[PlanStep] = []
    priced: list[tuple[TradePlanStepInput, InvestmentInstrument, Decimal, str, Decimal]] = []
    for step in steps:
        instrument = _instrument(step.instrument_uid)
        if step.limit_price is not None:
            price, source = step.limit_price, "limit_price"
        else:
            snapshot = get_market_snapshot(adapter, settings, step.instrument_uid)
            if snapshot.last_price is None or snapshot.last_price <= 0:
                raise TInvestConfigurationError(
                    f"No market price for {instrument.ticker or step.instrument_uid} — pass limit_price for this step"
                )
            price, source = snapshot.last_price, "last_price"
        native_value = _step_money_value(instrument, price, step.quantity_lots)
        # Mandate weights are ruble shares of a ruble portfolio; a yuan-priced
        # step left unconverted would look ~11x smaller than the bet it is.
        step_currency, step_fx = _instrument_fx(adapter, instrument)
        value = to_rub(native_value, step_currency, step_fx)
        if value is None:
            raise TInvestDataUnavailableError(
                f"{instrument.ticker or step.instrument_uid} is denominated in "
                f"{(step_currency or '').upper()} and the {(step_currency or '').upper()}/RUB "
                "rate is unavailable, so its weight in the portfolio cannot be checked "
                "against the mandate."
            )
        plan_steps.append(
            PlanStep(
                instrument_uid=step.instrument_uid,
                ticker=instrument.ticker,
                direction=step.direction,
                quantity_lots=step.quantity_lots,
                quantity_units=Decimal(step.quantity_lots * max(1, instrument.lot)),
                estimated_value=value,
                asset_class=_target_class_for(instrument),
                sector=(instrument.sector or "unknown").lower(),
                issuer=instrument.name or instrument.ticker or step.instrument_uid,
                denomination_currency=step_currency,
            )
        )
        priced.append((step, instrument, price, source, value))

    limits = mandate_limits(
        profile,
        default_max_issuer_weight=settings.mandate_max_issuer_weight,
        default_max_sector_weight=settings.mandate_max_sector_weight,
        rebalance_threshold_pct=settings.rebalance_threshold_pct,
    )
    total_value = state.cash + sum(state.class_values.values(), _ZERO)
    issuer_caps, single_cap = _plan_issuer_caps(profile, settings, state, plan_steps, instruments, total_value, notes)
    evaluation = evaluate_plan(
        profile,
        state,
        plan_steps,
        rebalance_threshold_pct=settings.rebalance_threshold_pct,
        min_cash_pct=limits.min_cash_pct,
        max_issuer_weight=single_cap,
        max_sector_weight=limits.max_sector_weight,
        issuer_caps=issuer_caps,
        min_progress_pp=settings.mandate_min_progress_pp,
        max_fx_exposure=settings.mandate_max_fx_exposure,
    )

    step_previews = [
        TradePlanStepPreview(
            step=index + 1,
            instrument_uid=inp.instrument_uid,
            ticker=instrument.ticker,
            name=instrument.name,
            instrument_type=instrument.instrument_type,
            asset_class=plan_steps[index].asset_class,
            direction=inp.direction,
            quantity_lots=inp.quantity_lots,
            price_used=price,
            price_source=source,
            estimated_value=value,
            cash_after=evaluation.cash_after_steps[index],
        )
        for index, (inp, instrument, price, source, value) in enumerate(priced)
    ]

    passed = all_passed(evaluation.checks)
    audit_event(
        "validate_trade_plan",
        status="submitted" if passed else "failed",
        account_id=settings.account_id,
        metadata={
            "steps": [{"uid": s.instrument_uid, "direction": s.direction, "lots": s.quantity_lots} for s in steps],
            "all_passed": passed,
            "failed_checks": [c.code for c in evaluation.checks if not c.passed],
        },
    )

    return TradePlanPreview(
        mode=settings.mode,
        currency=portfolio.currency,
        steps=step_previews,
        cash_before=portfolio.cash,
        cash_after=evaluation.cash_after,
        cash_after_pct=evaluation.allocation_after_pct.get("cash", _ZERO),
        total_value_after=evaluation.total_value_after,
        allocation_before_pct=evaluation.allocation_before_pct,
        allocation_after_pct=evaluation.allocation_after_pct,
        target_allocation_pct=dict(profile.target_allocation.allocation),
        mandate={
            "min_cash_pct": limits.min_cash_pct,
            "max_issuer_weight": limits.max_issuer_weight,
            "max_sector_weight": limits.max_sector_weight,
            "rebalance_threshold_pct": settings.rebalance_threshold_pct,
        },
        plan_checks=evaluation.checks,
        all_passed=passed,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Trade plan (advisory stage 6): sized SELL→BUY basket with costs, taxes,
# allocation preview and a cost-benefit verdict. Plan checks come from the
# stage-7 engine (risk_engine.evaluate_plan); execution stays per-leg behind
# the whole-plan and per-step UI confirmation gates.
# ---------------------------------------------------------------------------

_PLAN_CENT = Decimal("0.01")
_PLAN_PCT = Decimal("0.01")


@dataclass
class _PlanLeg:
    """One sized intent, ready for costing (internal to create_trade_plan)."""

    action: str  # BUY | SELL
    instrument: InvestmentInstrument
    price: Decimal  # reference price in the instrument quote unit
    unit_money: Decimal  # money per unit IN RUBLES (bonds converted from % of nominal, then FX)
    aci_per_unit: Decimal  # НКД per unit in rubles (0 for non-bonds)
    lots: int
    requested_amount: Decimal | None
    warnings: list[str]
    denomination_currency: str | None = None  # currency the cash flows are in
    fx_rate_rub: Decimal | None = None  # rubles per unit of it (None when ruble)


def _settlement_unit_price(leg: _PlanLeg) -> Decimal:
    """Money price per unit in the instrument's SETTLEMENT currency.

    ``leg.unit_money`` is already in rubles; a board that settles in a foreign
    currency needs it back in that currency before the broker sees it.
    """
    settlement = normalize_currency(leg.instrument.currency) or BASE_CURRENCY
    if settlement == BASE_CURRENCY or not leg.fx_rate_rub or leg.fx_rate_rub <= 0:
        return leg.unit_money
    return leg.unit_money / leg.fx_rate_rub


def _leg_amount_to_rub(amount: Decimal | None, currency: str | None, leg: _PlanLeg) -> Decimal | None:
    """Convert a broker figure to rubles using the rate the leg was sized with.

    Reusing the leg's rate (instead of re-fetching) keeps every money field of a
    leg internally consistent. ``None`` means "cannot be expressed in rubles" —
    never treat it as 1:1.
    """
    if amount is None:
        return None
    if not is_fx_linked(currency):
        return Decimal(amount)
    if leg.fx_rate_rub is None:
        return None
    return to_rub(amount, currency, FxRate(currency=currency or "", rate=leg.fx_rate_rub))


def _instrument_operations(
    adapter: TInvestAdapter,
    account_id: str,
    uid: str,
    *,
    max_pages: int = 10,
) -> list[BrokerOperation]:
    """Full executed-operation history for one instrument (feeds FIFO tax lots)."""
    to_dt = _now()
    from_dt = to_dt - timedelta(days=365 * 10)
    out: list[BrokerOperation] = []
    cursor = ""
    for _ in range(max_pages):
        resp = adapter.get_operations_by_cursor(
            account_id,
            from_=from_dt,
            to=to_dt,
            cursor=cursor,
            limit=1000,
            state=OperationState.OPERATION_STATE_EXECUTED,
            instrument_id=uid,
        )
        out.extend(operation_to_model(raw) for raw in getattr(resp, "items", []) or [])
        cursor = getattr(resp, "next_cursor", "") or ""
        if not getattr(resp, "has_next", False) or not cursor:
            break
    return out


def _sale_fallback_basis(position: PortfolioPosition | None, instrument: InvestmentInstrument) -> Decimal | None:
    """Money cost basis per unit from the position's average price (bonds: % → money)."""
    if position is None or position.average_price is None:
        return None
    if instrument.price_quote_unit == "pct_of_nominal":
        return trade_plan_mod.bond_unit_money(position.average_price, instrument.nominal)
    return position.average_price


def create_trade_plan(
    adapter: TInvestAdapter,
    settings: Settings,
    *,
    items: list[TradePlanItemInput],
    user_request_id: str | None = None,
) -> TradePlan:
    """Assemble the stage-6 basket: size intents into lots, sequence SELL→BUY,
    cost every leg (commission, НКД, FIFO/ЛДВ tax), preview the allocation and
    give the cost-benefit verdict. Read-only — no orders are placed."""
    if not items:
        raise TInvestConfigurationError("Trade plan needs at least one item")
    profile = load_profile(settings.investment_profile_path)
    if profile is None:
        raise TInvestConfigurationError(
            "No saved investment profile — the plan is measured against the target allocation. "
            "Run propose_target_allocation and save_investment_profile first."
        )

    account_id = resolve_account_id(adapter, settings)
    portfolio = get_portfolio_summary(adapter, settings)
    positions = {p.instrument_uid: p for p in portfolio.positions}

    instruments: dict[str, InvestmentInstrument] = {}

    def _instrument(uid: str) -> InvestmentInstrument:
        if uid not in instruments:
            instruments[uid] = load_instrument(adapter, uid)
        return instruments[uid]

    notes: list[str] = [
        "SELL legs are sequenced before BUY legs so sale proceeds fund the purchases.",
        "Prices are current bid (SELL) / ask (BUY) estimates; actual fills depend on the order book.",
        "Estimated НДФЛ is not deducted from running cash — the broker withholds it later.",
        "The plan places no orders. After whole-plan confirmation, preview and execute one "
        "immutable leg at a time using plan_id; the trusted UI owns both confirmation gates.",
    ]

    # --- size every intent into whole lots at current prices ------------------
    sized: list[_PlanLeg] = []
    skipped: list[SkippedPlanItem] = []
    for item in items:
        action = item.action.upper()

        # `item`/`action` are bound as defaults rather than captured: the
        # closure is only called inside this iteration today, and binding keeps
        # that true however the loop is edited later.
        def _skip(reason: str, *, item=item, action=action) -> None:
            skipped.append(
                SkippedPlanItem(
                    instrument_uid=item.instrument_uid,
                    action=action,
                    reason=reason,
                )
            )

        if (item.quantity_lots is None) == (item.amount is None):
            _skip("Pass exactly one of quantity_lots / amount.")
            continue
        try:
            instrument = _instrument(item.instrument_uid)
        except Exception:
            _skip("Instrument not found or unavailable.")
            continue
        snapshot = get_market_snapshot(adapter, settings, item.instrument_uid)
        book_side = snapshot.best_bid if action == "SELL" else snapshot.best_ask
        ref_price = book_side or snapshot.last_price
        if ref_price is None or ref_price <= 0:
            _skip("No market price available.")
            continue
        warnings: list[str] = []
        if book_side is None:
            warnings.append("Order book empty — last price used for estimates.")

        if instrument.price_quote_unit == "pct_of_nominal":
            unit_money = trade_plan_mod.bond_unit_money(ref_price, instrument.nominal)
            if unit_money is None:
                _skip("Bond nominal unknown — cannot convert % of nominal to money.")
                continue
            aci_per_unit = _d(instrument.aci_value)
        else:
            unit_money = ref_price
            aci_per_unit = _ZERO

        # Those figures are in the instrument's DENOMINATION currency (a yuan
        # bond prices off a yuan nominal even on a ruble board). The whole plan —
        # running cash, sizing from `amount`, mandate weights — is in portfolio
        # currency, so convert here, once, before any of it is used.
        leg_currency, leg_fx = _instrument_fx(adapter, instrument)
        if is_fx_linked(leg_currency):
            converted = to_rub(unit_money, leg_currency, leg_fx)
            converted_aci = to_rub(aci_per_unit, leg_currency, leg_fx)
            if converted is None or converted_aci is None:
                _skip(
                    f"{(leg_currency or '').upper()}-denominated instrument and no "
                    f"{(leg_currency or '').upper()}/RUB rate — cannot express the leg in "
                    "portfolio currency."
                )
                continue
            unit_money, aci_per_unit = converted, converted_aci
            warnings.append(
                f"FX-linked: nominal and coupons are in {(leg_currency or '').upper()}, so its "
                f"yield is a {(leg_currency or '').upper()} yield and the ruble result also "
                f"depends on {(leg_currency or '').upper()}/RUB. Money below converted at "
                f"{leg_fx.rate.quantize(Decimal('0.0001'))}."
            )

        lot = max(1, instrument.lot)
        lot_cash = (unit_money + aci_per_unit) * lot
        if item.quantity_lots is not None:
            lots = item.quantity_lots
            requested_amount = None
        else:
            try:
                amount = Decimal(str(item.amount))
            except (InvalidOperation, ValueError):
                _skip(f"Invalid amount {item.amount!r}.")
                continue
            lots = trade_plan_mod.lots_from_amount(amount, lot_cash)
            requested_amount = amount
            if lots <= 0:
                _skip(f"Amount {amount} is below the cost of one lot ({lot_cash}).")
                continue

        if action == "SELL":
            pos = positions.get(item.instrument_uid)
            sellable_units = _d(pos.quantity) - _d(pos.blocked) if pos else _ZERO
            sellable_lots = int(sellable_units // lot) if sellable_units > 0 else 0
            if sellable_lots <= 0:
                _skip("No sellable position for this instrument.")
                continue
            if lots > sellable_lots:
                warnings.append(f"Requested {lots} lots, only {sellable_lots} sellable — capped.")
                lots = sellable_lots

        sized.append(
            _PlanLeg(
                action=action,
                instrument=instrument,
                price=ref_price,
                unit_money=unit_money,
                aci_per_unit=aci_per_unit,
                lots=lots,
                requested_amount=requested_amount,
                warnings=warnings,
                denomination_currency=leg_currency,
                fx_rate_rub=leg_fx.rate if leg_fx else None,
            )
        )

    # --- sequence SELL before BUY and cost every leg ---------------------------
    ordered = [leg for leg in sized if leg.action == "SELL"] + [leg for leg in sized if leg.action == "BUY"]

    legs: list[PlannedTrade] = []
    plan_steps: list[PlanStep] = []
    cash = portfolio.cash
    min_cash = cash
    commissions_total = _ZERO
    taxes_total = _ZERO
    sell_net_total = _ZERO
    buy_cost_total = _ZERO
    turnover_planned = _ZERO
    today = date.today()

    for sequence, leg in enumerate(ordered, start=1):
        instrument = leg.instrument
        lot = max(1, instrument.lot)
        units = leg.lots * lot
        value = (leg.unit_money * units).quantize(_PLAN_CENT)
        aci_total = (leg.aci_per_unit * units).quantize(_PLAN_CENT)

        commission_source = "estimated"
        try:
            price_resp = adapter.get_order_price(
                account_id,
                instrument.uid,
                # GetOrderPrice wants MONEY per unit, not the % quote — feeding it
                # `leg.price` would scale the commission to a tenth of the trade.
                _settlement_unit_price(leg),
                leg.action,
                leg.lots,
            )
            raw_commission = _d(money_to_decimal(getattr(price_resp, "executed_commission", None)))
            # The broker bills in the instrument's currency; `value` above is
            # already in rubles, so the commission has to join it there. An
            # unconvertible commission falls through to the estimate rather than
            # silently entering the plan at its foreign face value.
            commission_currency = (
                currency_of(getattr(price_resp, "executed_commission", None)) or leg.denomination_currency
            )
            converted_commission = _leg_amount_to_rub(raw_commission, commission_currency, leg)
            if converted_commission is None:
                raise TInvestDataUnavailableError("commission currency not convertible")
            commission = converted_commission
            commission_source = "broker"
        except Exception:
            commission = (value * settings.plan_fallback_commission_pct / Decimal(100)).quantize(_PLAN_CENT)

        tax_model = None
        if leg.action == "SELL":
            try:
                operations = _instrument_operations(adapter, account_id, instrument.uid)
            except Exception:
                operations = []
            tax_lots, lot_notes = trade_plan_mod.build_tax_lots(operations)
            tax_model = trade_plan_mod.estimate_sale_tax(
                tax_lots,
                Decimal(units),
                leg.unit_money,
                as_of=today,
                tax_rate_pct=(settings.sell_tax_rate * Decimal(100)).normalize(),
                ldv_min_holding_years=_LDV_YEARS,
                fallback_cost_per_unit=_sale_fallback_basis(positions.get(instrument.uid), instrument),
            )
            tax_model.notes.extend(lot_notes)
            taxes_total += tax_model.tax
            cash_effect = value + aci_total - commission
            sell_net_total += cash_effect
        else:
            cash_effect = -(value + aci_total + commission)
            buy_cost_total += value + aci_total + commission

        commissions_total += commission
        turnover_planned += value + aci_total
        cash += cash_effect
        min_cash = min(min_cash, cash)

        legs.append(
            PlannedTrade(
                sequence=sequence,
                action=leg.action,
                instrument_uid=instrument.uid,
                ticker=instrument.ticker,
                name=instrument.name,
                instrument_type=instrument.instrument_type,
                currency=instrument.currency or "rub",
                denomination_currency=leg.denomination_currency,
                fx_rate_rub=leg.fx_rate_rub,
                price_quote_unit=instrument.price_quote_unit,
                quantity_lots=leg.lots,
                lot_size=lot,
                quantity_units=units,
                requested_amount=leg.requested_amount,
                price=leg.price,
                unit_price_money=leg.unit_money,
                estimated_value=value,
                accrued_interest=aci_total,
                commission=commission,
                commission_source=commission_source,
                tax=tax_model,
                cash_effect=cash_effect,
                cash_after=cash,
                warnings=leg.warnings,
            )
        )
        plan_steps.append(
            PlanStep(
                instrument_uid=instrument.uid,
                ticker=instrument.ticker,
                direction=leg.action,
                quantity_lots=leg.lots,
                quantity_units=Decimal(units),
                estimated_value=value,
                asset_class=_target_class_for(instrument),
                sector=(instrument.sector or "unknown").lower(),
                issuer=instrument.name or instrument.ticker or instrument.uid,
                denomination_currency=leg.denomination_currency,
            )
        )

    # --- stage-7 plan checks on the resulting portfolio ------------------------
    state = _portfolio_plan_state(portfolio, _instrument, notes)
    limits = mandate_limits(
        profile,
        default_max_issuer_weight=settings.mandate_max_issuer_weight,
        default_max_sector_weight=settings.mandate_max_sector_weight,
        rebalance_threshold_pct=settings.rebalance_threshold_pct,
    )
    total_value = state.cash + sum(state.class_values.values(), _ZERO)
    issuer_caps, single_cap = _plan_issuer_caps(profile, settings, state, plan_steps, instruments, total_value, notes)
    evaluation = evaluate_plan(
        profile,
        state,
        plan_steps,
        rebalance_threshold_pct=settings.rebalance_threshold_pct,
        min_cash_pct=limits.min_cash_pct,
        max_issuer_weight=single_cap,
        max_sector_weight=limits.max_sector_weight,
        issuer_caps=issuer_caps,
        min_progress_pp=settings.mandate_min_progress_pp,
        max_fx_exposure=settings.mandate_max_fx_exposure,
    )
    plan_checks = list(evaluation.checks)
    plan_checks.append(
        RiskCheck(
            code="PLAN_CASH_WITH_COSTS",
            passed=min_cash >= 0,
            message=(
                f"Cash stays ≥ 0 through the sequence including НКД and commissions (minimum {min_cash})"
                if min_cash >= 0
                else f"Cash goes negative ({min_cash}) once НКД and commissions are included — "
                "shrink the buys or sell more first"
            ),
        )
    )
    turnover_after = daily_turnover.current() + turnover_planned
    plan_checks.append(
        RiskCheck(
            code="PLAN_DAILY_TURNOVER",
            passed=turnover_after <= settings.max_daily_turnover_rub,
            message=(
                f"Total planned turnover {turnover_planned} keeps the daily total at {turnover_after} "
                f"(limit {settings.max_daily_turnover_rub})"
            ),
        )
    )
    passed = all_passed(plan_checks)
    blocking = has_blocking_failure(plan_checks)
    warned = any((not c.passed) and c.severity == "warning" for c in plan_checks)

    # --- allocation preview + cost-benefit verdict ------------------------------
    target = profile.target_allocation.allocation
    allocation_preview = [
        TradePlanAllocationPreview(
            asset_class=cls,
            before_pct=evaluation.allocation_before_pct.get(cls, _ZERO),
            after_pct=evaluation.allocation_after_pct.get(cls, _ZERO),
            target_pct=Decimal(target.get(cls, 0)),
            deviation_before_pct=(
                evaluation.allocation_before_pct.get(cls, _ZERO) - Decimal(target.get(cls, 0))
            ).quantize(_PLAN_PCT),
            deviation_after_pct=(
                evaluation.allocation_after_pct.get(cls, _ZERO) - Decimal(target.get(cls, 0))
            ).quantize(_PLAN_PCT),
        )
        for cls in ASSET_CLASSES
    ]
    total_value_before = portfolio.cash + sum(state.class_values.values(), _ZERO)
    cost_benefit = trade_plan_mod.build_cost_benefit(
        allocation_before_pct=evaluation.allocation_before_pct,
        allocation_after_pct=evaluation.allocation_after_pct,
        total_value_before=total_value_before,
        total_value_after=evaluation.total_value_after,
        target_allocation=target,
        commissions=commissions_total,
        taxes=taxes_total,
        rebalance_threshold_pct=settings.rebalance_threshold_pct,
        max_cost_to_benefit_ratio=settings.plan_max_cost_to_benefit_ratio,
    )

    if not legs:
        status = "EMPTY"
    elif blocking:
        status = "RISK_REJECTED"
    elif cost_benefit.verdict == "NOT_WORTH_IT":
        status = "NOT_WORTH_IT"
    elif warned:
        status = "READY_WITH_WARNINGS"
    else:
        status = "READY_FOR_CONFIRMATION"

    store = trade_plan_mod.get_plan_store(settings.trade_plan_ttl_seconds)
    plan_id, created_at, expires_at = store.new_id_and_window()
    plan = TradePlan(
        plan_id=plan_id,
        status=status,
        mode=settings.mode,
        created_at=created_at,
        expires_at=expires_at,
        currency=portfolio.currency,
        cash_before=portfolio.cash,
        cash_after=cash,
        items=legs,
        skipped=skipped,
        plan_checks=plan_checks,
        all_passed=passed,
        allocation_preview=allocation_preview,
        cost_benefit=cost_benefit,
        totals={
            "sell_proceeds_net": sell_net_total,
            "buy_cost_total": buy_cost_total,
            "commissions": commissions_total,
            "taxes": taxes_total,
            "net_cash_change": cash - portfolio.cash,
        },
        notes=notes,
    )
    store.put(plan, account_id=account_id)

    audit_event(
        "create_trade_plan",
        order_id=plan_id,
        status="failed" if blocking else "submitted",
        account_id=account_id,
        metadata={
            "user_request_id": user_request_id,
            "status": status,
            "verdict": cost_benefit.verdict,
            "warning_checks": [c.code for c in plan_checks if not c.passed and c.severity == "warning"],
            "legs": [{"uid": leg.instrument_uid, "action": leg.action, "lots": leg.quantity_lots} for leg in legs],
            "skipped": len(skipped),
            "failed_checks": [c.code for c in plan_checks if not c.passed],
        },
    )
    return plan


# ---------------------------------------------------------------------------
# Plan confirmation / sequential execution / verification (stages 8-10)
# ---------------------------------------------------------------------------

_PLAN_IN_FLIGHT = frozenset({"SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN_REQUIRES_RECONCILIATION"})
_PLAN_FAILED_STEP = frozenset({"CANCELLED", "REJECTED", "RISK_REJECTED", "EXPIRED"})
_PLAN_TERMINAL = frozenset({"COMPLETED", "CANCELLED", "EXPIRED", "RISK_REJECTED", "NOT_WORTH_IT", "EMPTY"})


def _require_plan(settings: Settings, plan_id: str):
    store = trade_plan_mod.get_plan_store(settings.trade_plan_ttl_seconds)
    plan = store.get(plan_id)
    record = store.get_execution(plan_id)
    if plan is None or record is None:
        raise TInvestProposalError("Unknown plan_id")
    return store, plan, record


def _current_plan_step(record) -> PlanStepState | None:
    return next(
        (step for step in record.steps if step.status not in {"FILLED", "SKIPPED"}),
        None,
    )


def _plan_leg(plan: TradePlan, sequence: int) -> PlannedTrade:
    for leg in plan.items:
        if leg.sequence == sequence:
            return leg
    raise TInvestProposalError(f"Plan step {sequence} not found")


def _remaining_plan_inputs(
    plan: TradePlan,
    record,
    *,
    current_proposal: OrderProposal | None = None,
) -> list[TradePlanStepInput]:
    """Rebuild immutable remaining legs for a fresh plan-level risk check."""
    inputs: list[TradePlanStepInput] = []
    for step in record.steps:
        if step.status in {"FILLED", "SKIPPED"}:
            continue
        leg = _plan_leg(plan, step.sequence)
        price = (
            current_proposal.limit_price
            if current_proposal is not None and step.sequence == current_proposal.plan_sequence
            else None
        )
        inputs.append(
            TradePlanStepInput(
                instrument_uid=leg.instrument_uid,
                direction=leg.action,
                quantity_lots=leg.quantity_lots,
                limit_price=price,
            )
        )
    return inputs


def _set_plan_status(store, plan_id: str, record, status: str, reason: str | None = None) -> None:
    store.sync_status(plan_id, status)
    record.paused_reason = reason


def _apply_plan_order_result(store, plan: TradePlan, record, step: PlanStepState, result: OrderResult) -> None:
    step.status = result.status
    # Cancellation responses are often sparse. Preserve the latest fill facts
    # instead of erasing partial-execution evidence with nulls.
    for field_name in (
        "broker_order_id",
        "lots_requested",
        "lots_executed",
        "executed_price",
        "total_amount",
        "commission",
    ):
        value = getattr(result, field_name)
        if value is not None:
            setattr(step, field_name, value)
    step.updated_at = _now()
    step.message = result.message

    if all(s.status in {"FILLED", "SKIPPED"} for s in record.steps):
        _set_plan_status(store, plan.plan_id, record, "COMPLETED")
    elif result.status == "FILLED":
        # Deliberately stop between legs. The next preview + confirmation is a
        # new user-visible execution card; no following order is auto-submitted.
        _set_plan_status(store, plan.plan_id, record, "CONFIRMED")
    elif result.status in _PLAN_IN_FLIGHT:
        _set_plan_status(
            store,
            plan.plan_id,
            record,
            "PAUSED",
            f"Step {step.sequence} is {result.status}; wait/poll, cancel it, or review urgency. "
            "No later step will be submitted automatically.",
        )
    elif result.status in _PLAN_FAILED_STEP:
        _set_plan_status(
            store,
            plan.plan_id,
            record,
            "PAUSED",
            f"Step {step.sequence} ended as {result.status}; review the rejection/cancellation "
            "before deciding whether to retry or cancel the remaining plan.",
        )


def _to_trade_plan_state(settings: Settings, plan: TradePlan, record) -> TradePlanState:
    current = _current_plan_step(record)
    proposal_ready = False
    if current is not None and current.proposal_id:
        proposal = get_store(settings.confirmation_ttl_seconds).get(current.proposal_id)
        proposal_ready = bool(
            proposal is not None and proposal.status == "READY_FOR_CONFIRMATION" and not proposal.is_expired
        )
    retryable = bool(current is not None and current.status in _PLAN_FAILED_STEP and (current.lots_executed or 0) == 0)
    can_preview = bool(
        record.confirmed_at is not None
        and record.status not in _PLAN_TERMINAL
        and current is not None
        and (current.status == "PENDING" or retryable)
    )
    return TradePlanState(
        plan_id=plan.plan_id,
        status=record.status,
        created_at=plan.created_at,
        expires_at=plan.expires_at,
        confirmed_at=record.confirmed_at,
        paused_reason=record.paused_reason,
        next_step_sequence=current.sequence if current is not None else None,
        can_preview_next=can_preview,
        can_execute_next=proposal_ready,
        is_terminal=record.status in _PLAN_TERMINAL,
        steps=[step.model_copy(deep=True) for step in record.steps],
    )


def confirm_trade_plan(
    adapter: TInvestAdapter,
    settings: Settings,
    plan_id: str,
    *,
    acknowledge_warnings: bool = False,
) -> TradePlanState:
    """Gate 1: record explicit approval of the whole still-valid plan.

    A plan whose only failing checks are soft mandate warnings has status
    ``READY_WITH_WARNINGS`` and can be confirmed, but only with
    ``acknowledge_warnings=True`` (the user has seen and accepted the warnings).
    """
    store, plan, record = _require_plan(settings, plan_id)
    with store.lock_for(plan_id):
        # Idempotent UI retries return the existing state.
        if record.confirmed_at is not None:
            return _to_trade_plan_state(settings, plan, record)
        if record.status == "EXPIRED" or _now() >= plan.expires_at:
            _set_plan_status(store, plan_id, record, "EXPIRED", "Plan confirmation TTL expired.")
            raise TInvestProposalError("Plan expired — build and review a fresh plan")
        if record.status not in {"READY_FOR_CONFIRMATION", "READY_WITH_WARNINGS"}:
            raise TInvestProposalError(
                f"Plan cannot be confirmed (status={record.status}, all_passed={plan.all_passed})"
            )
        warnings = [c for c in plan.plan_checks if not c.passed and c.severity == "warning"]
        if record.status == "READY_WITH_WARNINGS" and not acknowledge_warnings:
            raise TInvestProposalError(
                "Plan has soft mandate warnings — show them to the user and re-confirm with "
                "acknowledge_warnings=true. Warnings: " + "; ".join(c.message for c in warnings)
            )
        record.confirmed_at = _now()
        _set_plan_status(store, plan_id, record, "CONFIRMED")
        audit_event(
            "confirm_trade_plan",
            order_id=plan_id,
            status="confirmed",
            account_id=record.account_id,
            metadata={
                "steps": len(record.steps),
                "expires_at": plan.expires_at.isoformat(),
                "acknowledged_warnings": [c.code for c in warnings] if warnings else [],
            },
        )
        return _to_trade_plan_state(settings, plan, record)


def preview_plan_step(
    adapter: TInvestAdapter,
    settings: Settings,
    plan_id: str,
    *,
    urgency: str | None = None,
) -> PlanStepPreview:
    """Build Gate-2 preview for the next immutable leg, accepting no raw order size/price."""
    store, plan, record = _require_plan(settings, plan_id)
    with store.lock_for(plan_id):
        if record.confirmed_at is None:
            raise TInvestProposalError("Confirm the whole plan before previewing an execution step")
        if record.status in _PLAN_TERMINAL:
            raise TInvestProposalError(f"Plan is in terminal state {record.status}")
        step = _current_plan_step(record)
        if step is None:
            raise TInvestProposalError("Plan has no remaining steps")
        if step.status in _PLAN_IN_FLIGHT:
            raise TInvestProposalError(
                f"Step {step.sequence} is {step.status}; poll get_plan_state before any new preview"
            )
        if step.status in _PLAN_FAILED_STEP:
            if (step.lots_executed or 0) > 0:
                raise TInvestProposalError(
                    "The failed/cancelled step was partially filled; rebuild the remaining plan "
                    "from a fresh portfolio snapshot instead of silently trading the difference"
                )
            step.status = "PENDING"
            step.proposal_id = None
            step.preview_expires_at = None
            step.broker_order_id = None
            step.message = None

        leg = _plan_leg(plan, step.sequence)
        if leg.action == "BUY" and any(s.action == "SELL" and s.status != "FILLED" for s in record.steps):
            _set_plan_status(
                store,
                plan_id,
                record,
                "PAUSED",
                "BUY is blocked until every financing SELL step is FILLED.",
            )
            raise TInvestProposalError(record.paused_reason)

        # Fresh whole-remaining-plan risk check before even issuing a step card.
        remaining = _remaining_plan_inputs(plan, record)
        validation = validate_trade_plan(adapter, settings, steps=remaining)
        record.last_plan_checks = list(validation.plan_checks)
        if has_blocking_failure(validation.plan_checks):
            blockers = [c for c in validation.plan_checks if not c.passed and c.severity == "error"]
            reason = "; ".join(c.message for c in blockers)
            _set_plan_status(store, plan_id, record, "PAUSED", f"Plan re-check failed: {reason}")
            audit_event(
                "plan_revalidation_failed",
                order_id=plan_id,
                status="failed",
                account_id=record.account_id,
                metadata={"phase": "preview", "failed_checks": [c.code for c in blockers]},
            )
            raise TInvestProposalError(record.paused_reason)

        # A confirmed plan should actually execute: mid-spread limits can rest for
        # many minutes and stall every later leg behind them.
        normalized_urgency = urgency or settings.plan_execution_urgency or "balanced"
        proposal_store = get_store(settings.confirmation_ttl_seconds)
        if step.proposal_id:
            existing = proposal_store.get(step.proposal_id)
            existing_urgency = (
                existing.preview.order.get("urgency") if existing is not None and existing.preview is not None else None
            )
            if (
                existing is not None
                and existing.status == "READY_FOR_CONFIRMATION"
                and not existing.is_expired
                and existing_urgency == normalized_urgency
            ):
                return PlanStepPreview(
                    plan_id=plan_id,
                    plan_status=record.status,
                    step_sequence=step.sequence,
                    preview=existing.preview,
                    remaining_plan_checks=validation.plan_checks,
                )
            if existing is not None and existing.status == "READY_FOR_CONFIRMATION":
                proposal_store.set_status(existing.proposal_id, "CANCELLED")

        preview = create_order_proposal(
            adapter,
            settings,
            instrument_uid=leg.instrument_uid,
            direction=leg.action,
            order_type="LIMIT",
            quantity_lots=leg.quantity_lots,
            urgency=normalized_urgency,
            rationale=f"Confirmed plan {plan_id}, step {step.sequence}: {leg.action} {leg.ticker or leg.name or leg.instrument_uid}",
            user_request_id=f"plan:{plan_id}:step:{step.sequence}",
        )
        proposal_store.update(
            preview.proposal_id,
            plan_id=plan_id,
            plan_sequence=step.sequence,
        )
        step.proposal_id = preview.proposal_id
        step.preview_expires_at = preview.expires_at
        step.updated_at = _now()
        if preview.all_passed:
            _set_plan_status(store, plan_id, record, "CONFIRMED")
        else:
            session_block = next(
                (
                    c
                    for c in preview.risk_checks
                    if c.code == "MARKET_SESSION" and not c.passed and c.severity == "error"
                ),
                None,
            )
            paused_reason = (
                f"Market closed for trading — {session_block.message} The step will submit once the session resumes."
                if session_block is not None
                else "The next step preview failed its per-order risk checks; review the card and rebuild/retry."
            )
            _set_plan_status(store, plan_id, record, "PAUSED", paused_reason)
        audit_event(
            "preview_plan_step",
            order_id=plan_id,
            status="submitted" if preview.all_passed else "failed",
            account_id=record.account_id,
            metadata={
                "sequence": step.sequence,
                "proposal_id": preview.proposal_id,
                "urgency": normalized_urgency,
                "risk_passed": preview.all_passed,
            },
        )
        return PlanStepPreview(
            plan_id=plan_id,
            plan_status=record.status,
            step_sequence=step.sequence,
            preview=preview,
            remaining_plan_checks=validation.plan_checks,
        )


def _execution_next_action(record, result: OrderResult) -> str:
    if record.status == "COMPLETED":
        return "VERIFY_PLAN"
    if result.status == "FILLED":
        return "PREVIEW_NEXT_STEP"
    if result.status in _PLAN_IN_FLIGHT:
        return "WAIT_OR_CANCEL"
    if result.status in _PLAN_FAILED_STEP:
        return "REVIEW_REJECTION"
    return "POLL_PLAN_STATE"


def execute_plan_step(
    adapter: TInvestAdapter,
    settings: Settings,
    plan_id: str,
) -> PlanExecutionResult:
    """Gate 2: idempotently submit or refresh exactly the next plan leg."""
    store, plan, record = _require_plan(settings, plan_id)
    with store.lock_for(plan_id):
        if record.confirmed_at is None:
            raise TInvestProposalError("Confirm the whole plan before execution")
        if record.status == "COMPLETED":
            return PlanExecutionResult(
                plan_id=plan_id,
                plan_status="COMPLETED",
                message="All plan steps are already complete; no order was submitted.",
                next_action="VERIFY_PLAN",
            )
        if record.status in {"CANCELLED", "EXPIRED"}:
            raise TInvestProposalError(f"Plan is in terminal state {record.status}")

        step = _current_plan_step(record)
        if step is None:
            _set_plan_status(store, plan_id, record, "COMPLETED")
            return PlanExecutionResult(
                plan_id=plan_id,
                plan_status="COMPLETED",
                message="All plan steps are complete; no order was submitted.",
                next_action="VERIFY_PLAN",
            )

        # An idempotent retry never calls PostOrder again while the existing
        # broker order is in flight; it only refreshes that same proposal.
        if step.status in _PLAN_IN_FLIGHT and step.proposal_id:
            result = get_order_state(adapter, settings, step.proposal_id)
            _apply_plan_order_result(store, plan, record, step, result)
            return PlanExecutionResult(
                plan_id=plan_id,
                plan_status=record.status,
                step_sequence=step.sequence,
                order=result,
                message=(record.paused_reason or f"Step {step.sequence} refreshed as {result.status}."),
                next_action=_execution_next_action(record, result),
            )
        if step.status in _PLAN_FAILED_STEP:
            return PlanExecutionResult(
                plan_id=plan_id,
                plan_status=record.status,
                step_sequence=step.sequence,
                message=record.paused_reason or f"Step {step.sequence} is {step.status}; no order submitted.",
                next_action="REVIEW_REJECTION",
            )
        if not step.proposal_id:
            raise TInvestProposalError(
                "No fresh execution preview for the next step; call preview_plan_step(plan_id) "
                "and show it to the user first"
            )

        proposal_store = get_store(settings.confirmation_ttl_seconds)
        proposal = proposal_store.get(step.proposal_id)
        if proposal is None or proposal.status == "EXPIRED" or proposal.is_expired:
            step.proposal_id = None
            step.preview_expires_at = None
            _set_plan_status(
                store,
                plan_id,
                record,
                "PAUSED",
                "Execution preview expired; generate and confirm a fresh step preview.",
            )
            raise TInvestProposalError(record.paused_reason)
        if proposal.plan_id != plan_id or proposal.plan_sequence != step.sequence:
            raise TInvestProposalError("Plan/proposal linkage mismatch; refusing execution")
        if proposal.status != "READY_FOR_CONFIRMATION":
            raise TInvestProposalError(f"Step proposal is not ready for execution (status={proposal.status})")
        if step.action == "BUY" and any(s.action == "SELL" and s.status != "FILLED" for s in record.steps):
            _set_plan_status(
                store,
                plan_id,
                record,
                "PAUSED",
                "BUY is blocked until every financing SELL step is FILLED.",
            )
            raise TInvestProposalError(record.paused_reason)

        # Repeat BOTH risk layers at the execution boundary: the whole remaining
        # plan and the per-order engine inside post_order.
        validation = validate_trade_plan(
            adapter,
            settings,
            steps=_remaining_plan_inputs(plan, record, current_proposal=proposal),
        )
        record.last_plan_checks = list(validation.plan_checks)
        if has_blocking_failure(validation.plan_checks):
            blockers = [c for c in validation.plan_checks if not c.passed and c.severity == "error"]
            proposal_store.set_status(proposal.proposal_id, "RISK_REJECTED")
            result = OrderResult(
                proposal_id=proposal.proposal_id,
                status="RISK_REJECTED",
                direction=proposal.direction,
                message="Plan re-validation failed: " + "; ".join(c.message for c in blockers),
            )
            _apply_plan_order_result(store, plan, record, step, result)
            audit_event(
                "plan_revalidation_failed",
                order_id=plan_id,
                status="failed",
                account_id=record.account_id,
                metadata={"phase": "execute", "failed_checks": [c.code for c in blockers]},
            )
            return PlanExecutionResult(
                plan_id=plan_id,
                plan_status=record.status,
                step_sequence=step.sequence,
                order=result,
                message=record.paused_reason or result.message or "Plan re-validation failed.",
                next_action="REVIEW_REJECTION",
            )

        _set_plan_status(store, plan_id, record, "EXECUTING")
        result = post_order(
            adapter,
            settings,
            proposal.proposal_id,
            allow_plan_proposal=True,
        )
        _apply_plan_order_result(store, plan, record, step, result)
        audit_event(
            "execute_plan_step",
            order_id=plan_id,
            status="confirmed" if result.status in {"FILLED", "SUBMITTED", "PARTIALLY_FILLED"} else "failed",
            account_id=record.account_id,
            metadata={
                "sequence": step.sequence,
                "proposal_id": proposal.proposal_id,
                "order_status": result.status,
            },
        )
        return PlanExecutionResult(
            plan_id=plan_id,
            plan_status=record.status,
            step_sequence=step.sequence,
            order=result,
            message=(
                "Step filled; the next leg still requires a fresh preview and confirmation."
                if result.status == "FILLED" and record.status != "COMPLETED"
                else record.paused_reason or f"Step {step.sequence} returned {result.status}."
            ),
            next_action=_execution_next_action(record, result),
        )


def get_plan_state(
    adapter: TInvestAdapter,
    settings: Settings,
    plan_id: str,
    *,
    refresh: bool = True,
) -> TradePlanState:
    """Return FILLED/SUBMITTED/PENDING/SKIPPED per leg and optionally poll the active order."""
    store, plan, record = _require_plan(settings, plan_id)
    with store.lock_for(plan_id):
        if refresh:
            active = next(
                (s for s in record.steps if s.status in _PLAN_IN_FLIGHT and s.proposal_id),
                None,
            )
            if active is not None:
                result = get_order_state(adapter, settings, active.proposal_id)
                _apply_plan_order_result(store, plan, record, active, result)
        return _to_trade_plan_state(settings, plan, record)


def cancel_trade_plan(
    adapter: TInvestAdapter,
    settings: Settings,
    plan_id: str,
) -> TradePlanState:
    """Cancel the active order (if any) and explicitly SKIP every remaining leg."""
    store, plan, record = _require_plan(settings, plan_id)
    with store.lock_for(plan_id):
        if record.status == "COMPLETED":
            return _to_trade_plan_state(settings, plan, record)
        for step in record.steps:
            if step.status in _PLAN_IN_FLIGHT and step.proposal_id:
                result = cancel_order(adapter, settings, step.proposal_id)
                _apply_plan_order_result(store, plan, record, step, result)
            elif step.status == "PENDING":
                if step.proposal_id:
                    proposal = get_store(settings.confirmation_ttl_seconds).get(step.proposal_id)
                    if proposal is not None and proposal.status == "READY_FOR_CONFIRMATION":
                        get_store(settings.confirmation_ttl_seconds).set_status(proposal.proposal_id, "CANCELLED")
                step.status = "SKIPPED"
                step.updated_at = _now()
                step.message = "Remaining plan cancelled by the user."
        _set_plan_status(
            store,
            plan_id,
            record,
            "CANCELLED",
            "The active order was cancelled when possible; every unsubmitted remaining step was skipped.",
        )
        audit_event(
            "cancel_trade_plan",
            order_id=plan_id,
            status="confirmed",
            account_id=record.account_id,
            metadata={"steps": {str(s.sequence): s.status for s in record.steps}},
        )
        return _to_trade_plan_state(settings, plan, record)


def verify_trade_plan(
    adapter: TInvestAdapter,
    settings: Settings,
    plan_id: str,
) -> TradePlanVerificationReport:
    """Take a fresh portfolio/analytics snapshot and close the explainability loop."""
    store, plan, record = _require_plan(settings, plan_id)
    with store.lock_for(plan_id):
        if any(step.status in _PLAN_IN_FLIGHT for step in record.steps):
            raise TInvestProposalError("Plan still has an in-flight order; verify only after it is terminal")
        if record.status not in {"COMPLETED", "CANCELLED"}:
            raise TInvestProposalError(
                f"Plan is not finished (status={record.status}); complete or cancel the remainder first"
            )
        summary = get_portfolio_summary(adapter, settings)
        analytics = get_portfolio_analytics(adapter, settings)

        before = {row.asset_class: row.before_pct for row in plan.allocation_preview}
        target = {row.asset_class: row.target_pct for row in plan.allocation_preview}
        if analytics.drift is not None:
            after = {row.asset_class: row.current_pct for row in analytics.drift.items}
            max_after = analytics.drift.max_abs_deviation_pct
        else:
            after = dict.fromkeys(ASSET_CLASSES, _ZERO)
            for bucket, weight in analytics.by_class.items():
                mapped = PORTFOLIO_CLASS_TO_TARGET.get(bucket)
                if mapped is not None:
                    after[mapped] += weight * Decimal(100)
            after = {key: value.quantize(Decimal("0.01")) for key, value in after.items()}
            max_after = max((abs(after[c] - target.get(c, _ZERO)) for c in ASSET_CLASSES), default=_ZERO)

        max_before = plan.cost_benefit.max_abs_deviation_before_pct
        drift_reduction = (max_before - max_after).quantize(Decimal("0.01"))
        actual_commissions = sum((_d(step.commission) for step in record.steps), _ZERO).quantize(Decimal("0.01"))
        estimated_taxes = plan.cost_benefit.taxes
        total_costs = (actual_commissions + estimated_taxes).quantize(Decimal("0.01"))
        order = list(ASSET_CLASSES)
        before_text = "/".join(format(before.get(cls, _ZERO), "f") for cls in order)
        after_text = "/".join(format(after.get(cls, _ZERO), "f") for cls in order)
        report = TradePlanVerificationReport(
            plan_id=plan_id,
            plan_status=record.status,
            generated_at=_now(),
            allocation_before_pct=before,
            allocation_after_pct=after,
            target_allocation_pct=target,
            max_abs_drift_before_pct=max_before,
            max_abs_drift_after_pct=max_after,
            drift_reduction_pct_points=drift_reduction,
            planned_commissions=plan.cost_benefit.commissions,
            actual_commissions=actual_commissions,
            estimated_taxes=estimated_taxes,
            total_costs_estimate=total_costs,
            portfolio_summary=summary,
            portfolio_analytics=analytics,
            summary=(
                f"Allocation (bonds/equity/cash) changed from {before_text}% to {after_text}%; "
                f"maximum drift changed from {max_before} pp to {max_after} pp; "
                f"actual commissions + estimated tax = {total_costs} {summary.currency}."
            ),
            notes=[
                "Tax remains the pre-trade estimate; use the broker tax report for the final withheld amount.",
                "The after snapshot uses current market values, so price moves can differ from the static plan preview.",
            ],
        )
        record.verification_report = report
        audit_event(
            "verify_trade_plan",
            order_id=plan_id,
            status="confirmed",
            account_id=record.account_id,
            metadata={
                "plan_status": record.status,
                "allocation_before_pct": {k: str(v) for k, v in before.items()},
                "allocation_after_pct": {k: str(v) for k, v in after.items()},
                "max_drift_before_pct": str(max_before),
                "max_drift_after_pct": str(max_after),
                "actual_commissions": str(actual_commissions),
                "estimated_taxes": str(estimated_taxes),
            },
        )
        return report


def log_recommendation(
    settings: Settings,
    plan_id: str,
    *,
    rationale: str,
    alternatives_considered: list[str] | None = None,
) -> dict[str, object]:
    """Append the human rationale for the selected securities to the audit journal."""
    store, plan, record = _require_plan(settings, plan_id)
    if not rationale.strip():
        raise TInvestConfigurationError("Recommendation rationale must not be empty")
    with store.lock_for(plan_id):
        record.recommendation_entries += 1
        audit_event(
            "log_recommendation",
            order_id=plan_id,
            status="confirmed",
            account_id=record.account_id,
            metadata={
                "rationale": rationale.strip(),
                "alternatives_considered": alternatives_considered or [],
                "instruments": [
                    {"uid": leg.instrument_uid, "ticker": leg.ticker, "action": leg.action} for leg in plan.items
                ],
                "entry_number": record.recommendation_entries,
            },
        )
        return {
            "plan_id": plan_id,
            "logged": True,
            "entry_number": record.recommendation_entries,
        }


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def post_order(
    adapter: TInvestAdapter,
    settings: Settings,
    proposal_id: str,
    *,
    allow_plan_proposal: bool = False,
) -> OrderResult:
    store = get_store(settings.confirmation_ttl_seconds)
    proposal = store.get(proposal_id)
    if proposal is None:
        raise TInvestProposalError("Unknown proposal_id")
    if proposal.plan_id is not None and not allow_plan_proposal:
        raise TInvestProposalError(
            "This proposal belongs to a confirmed trade plan and cannot be submitted via "
            "post_order(proposal_id); use execute_plan_step(plan_id) after the execution preview "
            "is explicitly confirmed."
        )
    if proposal.status == "EXPIRED" or proposal.is_expired:
        store.set_status(proposal_id, "EXPIRED")
        raise TInvestProposalError("Proposal expired — create a new proposal/preview")
    if proposal.is_terminal:
        raise TInvestProposalError(f"Proposal is in terminal state {proposal.status}; cannot execute")
    if proposal.status not in {"READY_FOR_CONFIRMATION", "UNKNOWN_REQUIRES_RECONCILIATION"}:
        raise TInvestProposalError(f"Proposal not ready for execution (status={proposal.status})")
    if settings.is_prod and not settings.real_trading_enabled:
        raise TInvestRealTradingDisabledError(
            'Real trading is disabled. Set [tinvest].mode = "prod" and enable_real_trading = true in config.toml.'
        )

    # Re-validate just before sending.
    instrument = load_instrument(adapter, proposal.instrument_uid)
    snapshot = get_market_snapshot(adapter, settings, proposal.instrument_uid)
    portfolio = get_portfolio_summary(adapter, settings)
    position = _find_position(portfolio, proposal.instrument_uid)
    position_value_before = _d(position.current_value) if position is not None else _ZERO
    sell_fields: dict[str, object] = {}
    if proposal.direction == "SELL":
        sell_fields, _ = _sell_context_fields(
            adapter,
            settings,
            account_id=proposal.account_id,
            instrument=instrument,
            position=position,
            quantity_lots=proposal.quantity_lots,
        )
    ctx = OrderContext(
        direction=proposal.direction,
        order_type=proposal.order_type,
        quantity_lots=proposal.quantity_lots,
        limit_price=proposal.limit_price,
        order_total=_d(proposal.estimated_total),
        available_cash=portfolio.cash,
        portfolio_value_before=portfolio.total_value,
        position_value_before=position_value_before,
        max_lots=None,
        **sell_fields,
    )
    checks = evaluate(
        ctx,
        instrument,
        snapshot,
        portfolio,
        settings,
        session_state=_current_session_state(settings),
    )
    if not all_passed(checks):
        store.set_status(proposal_id, "RISK_REJECTED")
        audit_event(
            "revalidation_failed",
            order_id=proposal_id,
            status="failed",
            account_id=proposal.account_id,
            metadata={"failed_checks": [c.code for c in checks if not c.passed]},
        )
        return OrderResult(
            proposal_id=proposal_id,
            status="RISK_REJECTED",
            message="Re-validation failed: " + "; ".join(c.message for c in checks if not c.passed),
        )

    # PostOrder takes money per unit, not the quote (bonds are quoted in % of
    # nominal) — send the same money price GetOrderPrice was validated against.
    denom, fx_rate = _instrument_fx(adapter, instrument)
    wire_price = _unit_money_price(instrument, proposal.limit_price, denom=denom, fx_rate=fx_rate)

    # Idempotency key persisted BEFORE the call.
    idem = store.ensure_idempotency_key(proposal_id)
    store.set_status(proposal_id, "SUBMITTING")
    audit_event(
        "submit_order",
        order_id=idem,
        status="submitted",
        account_id=proposal.account_id,
        metadata={
            "instrument_uid": proposal.instrument_uid,
            "ticker": proposal.ticker,
            "direction": proposal.direction,
            "quantity_lots": proposal.quantity_lots,
            "normalized_price": str(proposal.limit_price),
            "wire_price_money": str(wire_price),
            "mode": settings.mode,
        },
    )

    try:
        resp = adapter.post_order(
            account_id=proposal.account_id,
            uid=proposal.instrument_uid,
            quantity=proposal.quantity_lots,
            price=wire_price,
            direction=proposal.direction,
            order_type=proposal.order_type,
            order_id=idem,
        )
    except Exception as exc:
        err = broker_error_meta(exc)
        err_line = format_broker_error(err)
        context = {
            **err,
            "instrument_uid": proposal.instrument_uid,
            "ticker": proposal.ticker,
            "direction": proposal.direction,
            "quantity_lots": proposal.quantity_lots,
            "limit_price": str(proposal.limit_price),
            "wire_price_money": str(wire_price),
        }
        if is_definitive_broker_reject(exc):
            # The broker refused the RPC outright (e.g. 30099 price outside the
            # instrument's limits) — no order exists, so REJECTED, not UNKNOWN:
            # the plan step becomes retryable with a fresh preview instead of
            # dead-ending in manual reconciliation.
            logger.error("PostOrder rejected by broker: %s", err_line)
            store.set_status(proposal_id, "REJECTED")
            audit_event(
                "submit_rejected", order_id=idem, status="failed", account_id=proposal.account_id, metadata=context
            )
            return OrderResult(
                proposal_id=proposal_id,
                status="REJECTED",
                idempotency_key=idem,
                message=(
                    f"Broker rejected the order before placement — {err_line}. "
                    "No order was created; rebuild the step preview (fresh price) and retry."
                ),
            )
        # Network/timeout AFTER submit → outcome unknown; reconcile, never re-key.
        logger.error("PostOrder outcome unknown: %s", err_line)
        store.set_status(proposal_id, "UNKNOWN_REQUIRES_RECONCILIATION")
        audit_event("submit_unknown", order_id=idem, status="failed", account_id=proposal.account_id, metadata=context)
        return OrderResult(
            proposal_id=proposal_id,
            status="UNKNOWN_REQUIRES_RECONCILIATION",
            idempotency_key=idem,
            message=(f"Order submission outcome unknown — {err_line}; reconcile with get_order_state before retrying."),
        )

    return _finalize_order(store, settings, proposal, resp, idem)


def _finalize_order(store, settings, proposal, resp, idem) -> OrderResult:
    broker_order_id = getattr(resp, "order_id", None) or idem
    status_name = _status_name(getattr(resp, "execution_report_status", None))
    mapped = _EXEC_STATUS_MAP.get(status_name, "SUBMITTED")
    total = money_to_decimal(getattr(resp, "total_order_amount", None))
    commission = money_to_decimal(getattr(resp, "executed_commission", None))
    executed_price = money_to_decimal(getattr(resp, "executed_order_price", None))

    store.update(proposal.proposal_id, status=mapped, broker_order_id=broker_order_id, idempotency_key=idem)
    if total:
        daily_turnover.add(total)
    audit_event(
        "order_result",
        order_id=broker_order_id,
        status="confirmed" if mapped in {"FILLED", "PARTIALLY_FILLED", "SUBMITTED"} else "failed",
        account_id=proposal.account_id,
        metadata={
            "proposal_id": proposal.proposal_id,
            "idempotency_key": idem,
            "execution_report_status": status_name,
            "lots_requested": getattr(resp, "lots_requested", None),
            "lots_executed": getattr(resp, "lots_executed", None),
            "fill_price": str(executed_price) if executed_price else None,
            "commission": str(commission) if commission else None,
            "mode": settings.mode,
        },
    )
    return OrderResult(
        proposal_id=proposal.proposal_id,
        status=mapped,
        broker_order_id=broker_order_id,
        idempotency_key=idem,
        lots_requested=getattr(resp, "lots_requested", None),
        lots_executed=getattr(resp, "lots_executed", None),
        executed_price=executed_price,
        total_amount=total,
        commission=commission,
        direction=proposal.direction,
        message=getattr(resp, "message", None) or None,
    )


def get_order_state(adapter: TInvestAdapter, settings: Settings, proposal_id: str) -> OrderResult:
    store = get_store(settings.confirmation_ttl_seconds)
    proposal = store.get(proposal_id)
    if proposal is None:
        raise TInvestProposalError("Unknown proposal_id")
    if not proposal.broker_order_id:
        return OrderResult(proposal_id=proposal_id, status=proposal.status, message="No broker order associated yet")
    try:
        resp = adapter.get_order_state(proposal.account_id, proposal.broker_order_id)
    except Exception as exc:
        err = broker_error_meta(exc)
        # "Order not found" (50005) means the order is no longer in the broker's
        # ACTIVE book — filled-and-archived, cancelled, or wiped by clearing. It
        # must never propagate: polling get_plan_state would 409 and the whole
        # plan would die on a routine lifecycle event. Surface it as a state that
        # needs reconciliation and keep the plan pollable.
        if err.get("grpc_code") == "NOT_FOUND":
            logger.warning(
                "GetOrderState: order %s is no longer active (%s)",
                proposal.broker_order_id,
                format_broker_error(err),
            )
            store.set_status(proposal_id, "UNKNOWN_REQUIRES_RECONCILIATION")
            audit_event(
                "order_state_not_found",
                order_id=proposal.broker_order_id,
                status="failed",
                account_id=proposal.account_id,
                metadata=err,
            )
            return OrderResult(
                proposal_id=proposal_id,
                status="UNKNOWN_REQUIRES_RECONCILIATION",
                broker_order_id=proposal.broker_order_id,
                idempotency_key=proposal.idempotency_key,
                direction=proposal.direction,
                message=(
                    "The broker no longer lists this order as active (it may have filled, "
                    "been cancelled, or been cleared at session end). Check the position in "
                    "your portfolio before retrying — no new order was sent."
                ),
            )
        raise
    status_name = _status_name(getattr(resp, "execution_report_status", None))
    mapped = _EXEC_STATUS_MAP.get(status_name, proposal.status)
    store.set_status(proposal_id, mapped)
    return OrderResult(
        proposal_id=proposal_id,
        status=mapped,
        broker_order_id=proposal.broker_order_id,
        idempotency_key=proposal.idempotency_key,
        lots_requested=getattr(resp, "lots_requested", None),
        lots_executed=getattr(resp, "lots_executed", None),
        executed_price=money_to_decimal(getattr(resp, "executed_order_price", None)),
        total_amount=money_to_decimal(getattr(resp, "total_order_amount", None)),
        commission=money_to_decimal(getattr(resp, "executed_commission", None)),
        direction=proposal.direction,
    )


def cancel_order(adapter: TInvestAdapter, settings: Settings, proposal_id: str) -> OrderResult:
    store = get_store(settings.confirmation_ttl_seconds)
    proposal = store.get(proposal_id)
    if proposal is None:
        raise TInvestProposalError("Unknown proposal_id")
    if not proposal.broker_order_id:
        store.set_status(proposal_id, "CANCELLED")
        return OrderResult(proposal_id=proposal_id, status="CANCELLED", message="No broker order to cancel")
    adapter.cancel_order(proposal.account_id, proposal.broker_order_id)
    store.set_status(proposal_id, "CANCELLED")
    audit_event(
        "cancel_order",
        order_id=proposal.broker_order_id,
        status="confirmed",
        account_id=proposal.account_id,
        metadata={"proposal_id": proposal_id},
    )
    return OrderResult(proposal_id=proposal_id, status="CANCELLED", broker_order_id=proposal.broker_order_id)


def _proposal_to_executing_summary(
    proposal: OrderProposal,
    *,
    lots_requested: int | None = None,
    lots_executed: int | None = None,
    executed_price: Decimal | None = None,
    total_amount: Decimal | None = None,
    commission: Decimal | None = None,
    message: str | None = None,
) -> ExecutingOrderSummary:
    return ExecutingOrderSummary(
        proposal_id=proposal.proposal_id,
        status=proposal.status,
        broker_order_id=proposal.broker_order_id,
        idempotency_key=proposal.idempotency_key,
        created_at=proposal.created_at,
        instrument={
            "uid": proposal.instrument_uid,
            "ticker": proposal.ticker,
            "name": proposal.name,
            "type": proposal.instrument_type,
        },
        order={
            "direction": proposal.direction,
            "type": proposal.order_type,
            "quantity_lots": str(proposal.quantity_lots),
            "limit_price": str(proposal.limit_price),
            "currency": proposal.currency,
        },
        lots_requested=lots_requested,
        lots_executed=lots_executed,
        executed_price=executed_price,
        total_amount=total_amount,
        commission=commission,
        message=message,
    )


def list_executing_orders(
    adapter: TInvestAdapter,
    settings: Settings,
    *,
    refresh: bool = True,
) -> list[ExecutingOrderSummary]:
    """List proposals whose broker order is still in flight (SUBMITTED / PARTIAL / …)."""
    store = get_store(settings.confirmation_ttl_seconds)
    summaries: list[ExecutingOrderSummary] = []
    for proposal in store.list_executing():
        current = proposal
        if refresh and current.broker_order_id:
            state = get_order_state(adapter, settings, current.proposal_id)
            refreshed = store.get(current.proposal_id)
            if refreshed is None or refreshed.status not in EXECUTION_STATUSES:
                continue
            current = refreshed
            summaries.append(
                _proposal_to_executing_summary(
                    current,
                    lots_requested=state.lots_requested,
                    lots_executed=state.lots_executed,
                    executed_price=state.executed_price,
                    total_amount=state.total_amount,
                    commission=state.commission,
                    message=state.message,
                )
            )
        else:
            summaries.append(_proposal_to_executing_summary(current))
    return summaries
