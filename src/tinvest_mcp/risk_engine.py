"""Deterministic risk engine.

Pure functions over already-normalized values — NO LLM, NO SDK. Given a proposed
order plus instrument / market / portfolio context it returns a list of
:class:`RiskCheck` results. The order is only executable when every check passes.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .config.env import Settings
from .fx import BASE_CURRENCY, is_fx_linked
from .money import is_aligned_to_increment
from .schemas import (
    ASSET_CLASSES,
    InvestmentInstrument,
    InvestmentProfile,
    MarketSnapshot,
    PortfolioSummary,
    RiskCheck,
)
from .session_calendar import MoexSessionState

# Trading statuses we accept for placing a LIMIT order.
_TRADEABLE_STATUSES = {
    "SECURITY_TRADING_STATUS_NORMAL_TRADING",
    "SECURITY_TRADING_STATUS_DEALER_NORMAL_TRADING",
}

_DAYS_PER_MONTH = Decimal("30.44")


@dataclass
class OrderContext:
    direction: str  # BUY | SELL
    order_type: str  # LIMIT | MARKET
    quantity_lots: int
    limit_price: Decimal
    # Full cost incl. commission/NKD from GetOrderPrice when available, ALWAYS in
    # rubles: limits, cash and weights are ruble quantities, and a foreign total
    # left untranslated would clear max_order_rub by the exchange rate.
    order_total: Decimal
    available_cash: Decimal
    portfolio_value_before: Decimal
    position_value_before: Decimal
    max_lots: int | None = None

    # Currency provenance (set only when the instrument is not ruble-denominated).
    native_total: Decimal | None = None  # order_total before conversion
    native_currency: str | None = None  # currency native_total is in
    fx_rate_rub: Decimal | None = None  # rubles per one unit of native_currency

    # SELL-only context (None / defaults for BUY). Built by services from the
    # portfolio, operations history and instrument lifecycle data.
    position_available_lots: int | None = None  # sellable lots held (blocked excluded)
    estimated_gain: Decimal | None = None  # realized P&L estimate for the sold part
    estimated_tax: Decimal | None = None  # НДФЛ estimate on a positive gain
    ldv_eligible_on: date | None = None  # date the 3-year LDV exemption kicks in
    corporate_action_kind: str | None = None  # "call/offer" | "maturity"
    corporate_action_date: date | None = None


class _DailyTurnover:
    """In-process turnover accumulator (resets each calendar day, UTC)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day: date | None = None
        self._total = Decimal("0")

    def current(self) -> Decimal:
        with self._lock:
            self._roll()
            return self._total

    def add(self, amount: Decimal) -> None:
        with self._lock:
            self._roll()
            self._total += amount

    def _roll(self) -> None:
        today = date.today()
        if self._day != today:
            self._day = today
            self._total = Decimal("0")


daily_turnover = _DailyTurnover()


def _check(code: str, passed: bool, message: str, *, severity: str = "error") -> RiskCheck:
    return RiskCheck(code=code, passed=passed, message=message, severity=severity)


def _sell_checks(ctx: OrderContext, settings: Settings, *, today: date | None = None) -> list[RiskCheck]:
    """SELL-specific checks (stage 7): position, tax preview, LDV & corporate-action warnings."""
    checks: list[RiskCheck] = []
    today = today or date.today()

    if ctx.position_available_lots is None:
        checks.append(
            _check(
                "POSITION_EXISTS",
                False,
                "No sellable position found for this instrument (shorts are not allowed)",
            )
        )
    else:
        checks.append(
            _check(
                "POSITION_EXISTS",
                ctx.quantity_lots <= ctx.position_available_lots,
                f"Selling {ctx.quantity_lots} lots against {ctx.position_available_lots} sellable lots held",
            )
        )

    if ctx.estimated_tax is not None:
        checks.append(
            _check(
                "TAX_IMPACT",
                True,
                f"Estimated НДФЛ on this sale ≈ {ctx.estimated_tax} "
                f"(gain estimate {ctx.estimated_gain}); broker FIFO accounting may differ",
                severity="info",
            )
        )
    else:
        checks.append(
            _check(
                "TAX_IMPACT",
                True,
                "Cannot estimate НДФЛ: position P&L data unavailable",
                severity="info",
            )
        )

    if ctx.ldv_eligible_on is None:
        checks.append(
            _check(
                "LDV_WARNING",
                True,
                "Holding period unknown — long-term ownership exemption (ЛДВ, 3 years) not evaluated",
                severity="info",
            )
        )
    elif ctx.ldv_eligible_on <= today:
        checks.append(
            _check(
                "LDV_WARNING",
                True,
                f"Held since long enough — ЛДВ (3-year exemption) already applies (eligible from {ctx.ldv_eligible_on})",
                severity="info",
            )
        )
    else:
        months_left = Decimal((ctx.ldv_eligible_on - today).days) / _DAYS_PER_MONTH
        within_window = months_left <= Decimal(settings.ldv_warning_months)
        message = f"ЛДВ (3-year tax exemption) starts {ctx.ldv_eligible_on} (~{months_left:.1f} months away)"
        if within_window:
            message += " — selling now forfeits the exemption"
        checks.append(
            _check(
                "LDV_WARNING",
                True,
                message,
                severity="warning" if within_window else "info",
            )
        )

    if ctx.corporate_action_date is not None:
        days_left = (ctx.corporate_action_date - today).days
        if 0 <= days_left <= settings.corporate_action_warning_days:
            checks.append(
                _check(
                    "CORPORATE_ACTION_SOON",
                    True,
                    f"{ctx.corporate_action_kind or 'corporate action'} on {ctx.corporate_action_date} "
                    f"(in {days_left} days) — compare selling now vs holding to the event",
                    severity="warning",
                )
            )

    return checks


def evaluate(
    ctx: OrderContext,
    instrument: InvestmentInstrument,
    snapshot: MarketSnapshot,
    portfolio: PortfolioSummary,
    settings: Settings,
    *,
    session_state: MoexSessionState | None = None,
) -> list[RiskCheck]:
    checks: list[RiskCheck] = []
    is_sell = ctx.direction == "SELL"

    # --- Operation / instrument eligibility ---
    checks.append(
        _check(
            "DIRECTION_ALLOWED",
            ctx.direction in {"BUY", "SELL"},
            f"Direction '{ctx.direction}' is not supported (BUY or SELL)",
        )
    )
    market_ok = ctx.order_type == "LIMIT" or settings.allow_market_orders
    checks.append(
        _check(
            "LIMIT_ORDERS_ONLY",
            ctx.order_type == "LIMIT" or market_ok,
            "Only LIMIT orders are allowed unless market orders are explicitly enabled",
        )
    )
    checks.append(
        _check(
            "NO_MARKET_ORDER",
            not (ctx.order_type == "MARKET" and not settings.allow_market_orders),
            "Market orders are disabled",
        )
    )
    itype = (instrument.instrument_type or "").lower()
    checks.append(
        _check(
            "INSTRUMENT_TYPE_ALLOWED",
            itype in settings.allowed_instrument_types,
            f"Instrument type '{itype}' is not in the allowed set {list(settings.allowed_instrument_types)}",
        )
    )
    if settings.instrument_allowlist:
        in_allow = instrument.uid in settings.instrument_allowlist or instrument.ticker in settings.instrument_allowlist
        checks.append(_check("INSTRUMENT_ALLOWLIST", in_allow, "Instrument is not in the configured allowlist"))
    side_available = instrument.sell_available if is_sell else instrument.buy_available
    checks.append(
        _check(
            "API_TRADE_AVAILABLE",
            bool(instrument.api_trade_available and side_available),
            f"Instrument is not available for API trading / {'selling' if is_sell else 'buying'}",
        )
    )
    checks.append(
        _check(
            "NOT_QUALIFIED_ONLY",
            not instrument.qualified_investor_only,
            "Qualified-investor-only instruments are forbidden",
        )
    )

    # --- Price controls ---
    checks.append(_check("PRICE_POSITIVE", ctx.limit_price > 0, "Limit price must be > 0"))
    checks.append(
        _check(
            "PRICE_INCREMENT",
            is_aligned_to_increment(ctx.limit_price, instrument.min_price_increment),
            "Limit price must align with the instrument's min price increment",
        )
    )
    if snapshot.last_price and snapshot.last_price > 0:
        deviation = abs(ctx.limit_price - snapshot.last_price) / snapshot.last_price * Decimal(100)
        checks.append(
            _check(
                "PRICE_DEVIATION",
                deviation <= settings.max_price_deviation_pct,
                f"Limit price deviates {deviation:.2f}% from last price (max {settings.max_price_deviation_pct}%)",
            )
        )
    else:
        checks.append(_check("PRICE_DEVIATION", False, "No reference last price to validate deviation"))
    status_ok = (snapshot.trading_status in _TRADEABLE_STATUSES) or snapshot.api_trade_available
    checks.append(
        _check("TRADING_STATUS", status_ok, f"Instrument trading status not tradeable: {snapshot.trading_status}")
    )

    # MOEX session clock (complements the broker flag above, which does NOT flip
    # during intraday clearing pauses — a limit submitted then just rests
    # unmatched). Blocks during a pause / closed hours; warns near a boundary.
    if session_state is not None:
        if session_state.should_block:
            checks.append(_check("MARKET_SESSION", False, session_state.message, severity="error"))
        elif session_state.closing_soon:
            checks.append(_check("MARKET_SESSION", True, session_state.message, severity="warning"))
        else:
            checks.append(_check("MARKET_SESSION", True, session_state.message, severity="info"))

    # --- Currency exposure ---
    # A ruble-settled instrument can still pay in a foreign currency (yuan bonds
    # are the common case), which makes the position a bet on the exchange rate
    # and its quoted yield a foreign-currency yield. Informational rather than
    # blocking: the mandate check in evaluate_plan decides whether it is wanted.
    if is_fx_linked(ctx.native_currency) and ctx.native_total is not None:
        code = ctx.native_currency.upper()
        rate = f" at {ctx.fx_rate_rub}" if ctx.fx_rate_rub is not None else ""
        checks.append(
            _check(
                "CURRENCY_EXPOSURE",
                True,
                f"{code}-denominated: {ctx.native_total} {code} ≈ {ctx.order_total} RUB{rate}. "
                f"Its yield is a {code} yield and the ruble result moves with {code}/RUB — "
                "not comparable to ruble instruments on yield alone",
                severity="warning",
            )
        )

    # --- Financial limits ---
    checks.append(
        _check(
            "MAX_ORDER_VALUE",
            ctx.order_total <= settings.max_order_rub,
            f"Order value {ctx.order_total} RUB exceeds the per-order limit {settings.max_order_rub}",
        )
    )
    turnover_after = daily_turnover.current() + ctx.order_total
    checks.append(
        _check(
            "DAILY_TURNOVER",
            turnover_after <= settings.max_daily_turnover_rub,
            f"Daily turnover {turnover_after} would exceed the limit {settings.max_daily_turnover_rub}",
        )
    )
    if not is_sell:
        checks.append(
            _check(
                "SUFFICIENT_CASH",
                ctx.order_total <= ctx.available_cash,
                f"Order value {ctx.order_total} exceeds available cash {ctx.available_cash}",
            )
        )
    if ctx.max_lots is not None:
        checks.append(
            _check(
                "MAX_LOTS",
                ctx.quantity_lots <= ctx.max_lots,
                f"Requested {ctx.quantity_lots} lots exceeds max executable {ctx.max_lots}",
            )
        )

    # --- Concentration — buying only: a sell reduces the weight ---
    if not is_sell:
        denominator = ctx.portfolio_value_before + ctx.order_total
        if denominator > 0:
            weight_after = (ctx.position_value_before + ctx.order_total) / denominator
        else:
            weight_after = Decimal("0")
        checks.append(
            _check(
                "POSITION_WEIGHT",
                weight_after <= settings.max_position_weight,
                f"Post-trade position weight {weight_after:.2%} exceeds the limit {settings.max_position_weight:.0%}",
            )
        )

    # --- SELL checks (stage 7): position, tax, LDV, corporate actions ---
    if is_sell:
        checks.extend(_sell_checks(ctx, settings))

    return checks


def all_passed(checks: list[RiskCheck]) -> bool:
    return all(c.passed for c in checks)


def has_blocking_failure(checks: list[RiskCheck]) -> bool:
    """True if any check hard-fails (passed=False AND severity='error').

    Warning-severity failures (soft mandate breaches under feature D) do not block
    execution — they are surfaced for explicit acknowledgment instead.
    """
    return any((not c.passed) and c.severity == "error" for c in checks)


# ---------------------------------------------------------------------------
# Plan-level checks (stage 7): the whole order sequence against the PERSONAL
# mandate (saved profile), not the global env limits. Pure functions — the
# service layer gathers portfolio / instrument data into plain structures.
# ---------------------------------------------------------------------------

_PCT = Decimal("0.01")
_HUNDRED = Decimal("100")


@dataclass
class PlanStep:
    """One normalized order of the plan, already valued by the service layer."""

    instrument_uid: str
    ticker: str | None
    direction: str  # BUY | SELL
    quantity_lots: int
    quantity_units: Decimal  # lots × lot size
    # Money value in RUBLES (bonds already converted from % of nominal, and from
    # their denomination currency) — the mandate weights it feeds are ruble shares.
    estimated_value: Decimal
    asset_class: str  # bonds | equity | cash | other
    sector: str
    issuer: str
    denomination_currency: str | None = None  # None / "rub" = no currency exposure


@dataclass
class PlanState:
    """Portfolio snapshot the plan is simulated on top of."""

    cash: Decimal
    position_units: dict[str, Decimal] = field(default_factory=dict)  # uid -> sellable units
    class_values: dict[str, Decimal] = field(default_factory=dict)  # securities only, target classes
    sector_values: dict[str, Decimal] = field(default_factory=dict)
    issuer_values: dict[str, Decimal] = field(default_factory=dict)
    # Ruble value held per DENOMINATION currency (ruble positions under "rub"),
    # so the plan can measure how much of the portfolio rides on an exchange rate.
    currency_values: dict[str, Decimal] = field(default_factory=dict)
    # issuer bucket -> cap category (single | sovereign | fund); carrier for the
    # service layer, which resolves the actual per-issuer caps. Empty = all single.
    issuer_categories: dict[str, str] = field(default_factory=dict)


@dataclass
class PlanEvaluation:
    checks: list[RiskCheck]
    cash_after_steps: list[Decimal]  # estimated cash after each step, in sequence
    cash_after: Decimal
    total_value_after: Decimal
    allocation_before_pct: dict[str, Decimal]
    allocation_after_pct: dict[str, Decimal]


def _allocation_pct(class_values: Mapping[str, Decimal], cash: Decimal) -> tuple[dict[str, Decimal], Decimal]:
    total = cash + sum(class_values.values())
    pct: dict[str, Decimal] = {}
    for cls in ASSET_CLASSES:
        value = cash if cls == "cash" else class_values.get(cls, Decimal(0))
        pct[cls] = (value / total * _HUNDRED).quantize(_PCT) if total > 0 else Decimal(0)
    return pct, total


def _bump(buckets: dict[str, Decimal], key: str, delta: Decimal) -> None:
    updated = buckets.get(key, Decimal(0)) + delta
    buckets[key] = updated if updated > 0 else Decimal(0)


# --- ratchet (feature A) + stepped severity (feature D) ----------------------

_SEVERITY_RANK: dict[str | None, int] = {None: 0, "warning": 1, "error": 2}


def _escalate(current: str | None, new: str) -> str:
    return new if _SEVERITY_RANK[new] > _SEVERITY_RANK[current] else current  # type: ignore[return-value]


def _ratchet(before_breach: Decimal, after_breach: Decimal, min_progress: Decimal) -> str:
    """Classify a mandate breach into pass / warning / error (feature A).

    * within the limit, or shrinks an EXISTING breach toward the band → ``pass``;
    * a brand-new breach (nothing before — e.g. deploying idle cash) → ``warning``;
    * deepening an existing breach beyond the dead-band → ``error`` (blocking);
    * an existing breach left roughly unchanged → ``warning``.

    ``before_breach``/``after_breach``/``min_progress`` share one unit (pp or fraction).
    """
    if after_breach <= 0:
        return "pass"
    if before_breach <= 0:
        return "warning"
    if after_breach <= before_breach - min_progress:
        return "pass"
    if after_breach > before_breach + min_progress:
        return "error"
    return "warning"


def _concentration_check(
    code: str,
    label: str,
    before_values: Mapping[str, Decimal],
    after_values: Mapping[str, Decimal],
    total_before: Decimal,
    total_after: Decimal,
    cap_for,
    min_progress: Decimal,
    *,
    skip: tuple[str, ...] = (),
) -> RiskCheck:
    """Per-subject concentration check with ratchet + stepped severity.

    ``cap_for(subject)`` returns the cap fraction, or ``None`` to exempt the
    subject (e.g. internally diversified funds under the issuer cap).
    """
    severity: str | None = None
    lines: list[str] = []
    subjects = sorted(
        set(before_values) | set(after_values),
        key=lambda s: after_values.get(s, Decimal(0)),
        reverse=True,
    )
    for subject in subjects:
        if subject in skip:
            continue
        cap = cap_for(subject)
        if cap is None:
            continue
        w_before = (before_values.get(subject, Decimal(0)) / total_before) if total_before > 0 else Decimal(0)
        w_after = (after_values.get(subject, Decimal(0)) / total_after) if total_after > 0 else Decimal(0)
        breach_before = max(Decimal(0), w_before - cap)
        breach_after = max(Decimal(0), w_after - cap)
        verdict = _ratchet(breach_before, breach_after, min_progress)
        if verdict == "pass":
            continue
        severity = _escalate(severity, verdict)
        lines.append(f"'{subject}' {(w_after * _HUNDRED).quantize(_PCT)}% (cap {(cap * _HUNDRED).quantize(_PCT)}%)")
    passed = severity is None
    message = (
        f"No {label} above its mandate cap after the plan"
        if passed
        else f"Above the {label} mandate cap: " + ", ".join(lines)
    )
    return _check(code, passed, message, severity=severity or "error")


def _step_currency(step: PlanStep) -> str:
    return (step.denomination_currency or BASE_CURRENCY).lower()


def _fx_exposure(currency_values: Mapping[str, Decimal], total: Decimal) -> tuple[Decimal, dict[str, Decimal]]:
    """(foreign share of *total* as a fraction, per-currency ruble values)."""
    foreign = {code: value for code, value in currency_values.items() if is_fx_linked(code) and value > 0}
    if total <= 0:
        return Decimal(0), foreign
    return sum(foreign.values(), Decimal(0)) / total, foreign


def _currency_exposure_check(
    profile: InvestmentProfile,
    before_values: Mapping[str, Decimal],
    after_values: Mapping[str, Decimal],
    total_before: Decimal,
    total_after: Decimal,
    cap: Decimal,
    min_progress: Decimal,
) -> RiskCheck:
    """Does the plan leave the portfolio riding on an exchange rate, and was that asked for?

    Foreign-denominated holdings carry a risk the yield table does not show: an
    8.7% yuan coupon and a 15.7% ruble coupon differ by the market's CNY/RUB
    expectation, not by 7 points of income. So the check has two modes — without
    an ``allow_fx_linked`` opt-in ANY new foreign exposure is a warning the user
    has to acknowledge, and with the opt-in it behaves like the other
    concentration caps (ratcheted against ``max_fx_exposure_pct``).
    """
    before_pct, _ = _fx_exposure(before_values, total_before)
    after_pct, after_foreign = _fx_exposure(after_values, total_after)
    breakdown = (
        ", ".join(
            f"{code.upper()} {(value / total_after * _HUNDRED).quantize(_PCT)}%"
            for code, value in sorted(after_foreign.items(), key=lambda kv: kv[1], reverse=True)
        )
        if total_after > 0
        else ""
    )

    if after_pct <= 0:
        return _check("PLAN_CURRENCY_EXPOSURE", True, "No foreign-currency exposure after the plan", severity="info")

    if not profile.allow_fx_linked:
        increased = after_pct > before_pct
        return _check(
            "PLAN_CURRENCY_EXPOSURE",
            not increased,
            f"Plan leaves {(after_pct * _HUNDRED).quantize(_PCT)}% of the portfolio denominated "
            f"in foreign currency ({breakdown}), up from {(before_pct * _HUNDRED).quantize(_PCT)}%, "
            "but the mandate has not opted into currency exposure (allow_fx_linked). Those "
            "instruments' yields are foreign-currency yields — confirm the currency bet is "
            "wanted, or set allow_fx_linked=true in the profile"
            if increased
            else f"Foreign-currency exposure not increased ({(after_pct * _HUNDRED).quantize(_PCT)}%: {breakdown})",
            severity="warning",
        )

    breach_before = max(Decimal(0), before_pct - cap)
    breach_after = max(Decimal(0), after_pct - cap)
    verdict = _ratchet(breach_before, breach_after, min_progress)
    return _check(
        "PLAN_CURRENCY_EXPOSURE",
        verdict == "pass",
        f"Foreign-currency exposure after the plan {(after_pct * _HUNDRED).quantize(_PCT)}% "
        f"vs cap {(cap * _HUNDRED).quantize(_PCT)}% ({breakdown})",
        severity="error" if verdict == "error" else ("warning" if verdict == "warning" else "info"),
    )


def evaluate_plan(
    profile: InvestmentProfile,
    state: PlanState,
    steps: list[PlanStep],
    *,
    rebalance_threshold_pct: Decimal,
    min_cash_pct: Decimal,
    max_issuer_weight: Decimal,
    max_sector_weight: Decimal,
    issuer_caps: Mapping[str, Decimal | None] | None = None,
    min_progress_pp: Decimal = Decimal("1"),
    max_fx_exposure: Decimal = Decimal("0.20"),
) -> PlanEvaluation:
    """Simulate the order sequence and check the RESULTING portfolio against the mandate.

    Deterministic and side-effect free. Per-order checks (price, limits, freshness)
    still run in ``create_order_proposal`` for every step at execution time.
    """
    checks: list[RiskCheck] = []
    cash = state.cash
    units = dict(state.position_units)
    class_values = dict(state.class_values)
    sector_values = dict(state.sector_values)
    issuer_values = dict(state.issuer_values)
    currency_values = dict(state.currency_values)

    allocation_before, _ = _allocation_pct(state.class_values, state.cash)

    # --- sequence feasibility: cash and position availability at EVERY step ---
    cash_after_steps: list[Decimal] = []
    for index, step in enumerate(steps, start=1):
        label = f"Step {index} ({step.direction} {step.ticker or step.instrument_uid})"
        if step.direction == "BUY":
            checks.append(
                _check(
                    "PLAN_STEP_CASH",
                    step.estimated_value <= cash,
                    f"{label}: cost {step.estimated_value} vs cash {cash} available at this step",
                )
            )
            cash -= step.estimated_value
            _bump(units, step.instrument_uid, step.quantity_units)
            if step.asset_class != "cash":
                _bump(class_values, step.asset_class, step.estimated_value)
            _bump(sector_values, step.sector, step.estimated_value)
            _bump(issuer_values, step.issuer, step.estimated_value)
            _bump(currency_values, _step_currency(step), step.estimated_value)
        else:  # SELL
            held = units.get(step.instrument_uid, Decimal(0))
            checks.append(
                _check(
                    "PLAN_STEP_POSITION",
                    step.quantity_units <= held,
                    f"{label}: selling {step.quantity_units} units against {held} held at this step",
                )
            )
            cash += step.estimated_value
            _bump(units, step.instrument_uid, -step.quantity_units)
            if step.asset_class != "cash":
                _bump(class_values, step.asset_class, -step.estimated_value)
            _bump(sector_values, step.sector, -step.estimated_value)
            _bump(issuer_values, step.issuer, -step.estimated_value)
            _bump(currency_values, _step_currency(step), -step.estimated_value)
        cash_after_steps.append(cash)

    allocation_after, total_after = _allocation_pct(class_values, cash)
    total_before = state.cash + sum(state.class_values.values(), Decimal(0))

    # --- resulting allocation within the personal mandate band (ratchet, feature A) ---
    alloc_severity: str | None = None
    alloc_lines: list[str] = []
    for cls in ASSET_CLASSES:
        target = Decimal(profile.target_allocation.allocation.get(cls, 0))
        breach_before = max(Decimal(0), abs(allocation_before[cls] - target) - rebalance_threshold_pct)
        breach_after = max(Decimal(0), abs(allocation_after[cls] - target) - rebalance_threshold_pct)
        verdict = _ratchet(breach_before, breach_after, min_progress_pp)
        if verdict == "pass":
            continue
        alloc_severity = _escalate(alloc_severity, verdict)
        deviation = allocation_after[cls] - target
        alloc_lines.append(f"{cls} {allocation_after[cls]}% vs target {target}% ({deviation:+.2f} pp)")
    checks.append(
        _check(
            "PLAN_ALLOCATION_MANDATE",
            alloc_severity is None,
            ("All asset classes within ±" + str(rebalance_threshold_pct) + " pp of target, or moving toward it")
            if alloc_severity is None
            else "Post-plan allocation breaks the mandate band: " + "; ".join(alloc_lines),
            severity=alloc_severity or "error",
        )
    )

    # --- cash floor (hard: a liquidity risk, never a warning) ---
    cash_pct_after = allocation_after["cash"]
    checks.append(
        _check(
            "PLAN_MIN_CASH",
            cash_pct_after >= min_cash_pct,
            f"Cash after the plan is {cash_pct_after}% (mandate floor {min_cash_pct}%)",
        )
    )

    # --- issuer / sector concentration after the plan (ratchet + per-issuer caps) ---
    min_progress_frac = min_progress_pp / _HUNDRED

    def _issuer_cap(subject: str) -> Decimal | None:
        if issuer_caps is not None and subject in issuer_caps:
            return issuer_caps[subject]
        return max_issuer_weight

    checks.append(
        _concentration_check(
            "PLAN_ISSUER_LIMIT",
            "per-issuer",
            state.issuer_values,
            issuer_values,
            total_before,
            total_after,
            _issuer_cap,
            min_progress_frac,
        )
    )
    checks.append(
        _concentration_check(
            "PLAN_SECTOR_LIMIT",
            "per-sector",
            state.sector_values,
            sector_values,
            total_before,
            total_after,
            lambda _s: max_sector_weight,
            min_progress_frac,
            skip=("unknown",),
        )
    )
    checks.append(
        _currency_exposure_check(
            profile,
            state.currency_values,
            currency_values,
            total_before,
            total_after,
            (profile.max_fx_exposure_pct / _HUNDRED) if profile.max_fx_exposure_pct is not None else max_fx_exposure,
            min_progress_frac,
        )
    )

    return PlanEvaluation(
        checks=checks,
        cash_after_steps=cash_after_steps,
        cash_after=cash,
        total_value_after=total_after,
        allocation_before_pct=allocation_before,
        allocation_after_pct=allocation_after,
    )
