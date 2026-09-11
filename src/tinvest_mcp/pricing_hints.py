"""Algorithmic limit-price hints (BUY and SELL) from market snapshot + risk bounds.

Pure functions — no SDK. Used by ``get_market_snapshot`` and ``create_order_proposal``.
For SELL the tiers mirror the BUY ones: patient joins the ask (best price, slow),
balanced sits mid-spread, fast crosses to the bid (quickest fill).
"""

from __future__ import annotations

from decimal import Decimal

from .config.env import Settings
from .money import quantize_to_increment
from .schemas import (
    BuyPriceHints,
    BuyUrgency,
    InvestmentInstrument,
    MarketSnapshot,
    PriceHintTier,
    PriceVsHints,
    RiskBounds,
    SessionInfo,
    SpreadInfo,
)
from .session_calendar import MoexSessionState

_ZERO = Decimal("0")
_FALLBACK_BPS = Decimal("0.05")  # 0.05% when the order book is empty

_TRADEABLE_STATUSES = {
    "SECURITY_TRADING_STATUS_NORMAL_TRADING",
    "SECURITY_TRADING_STATUS_DEALER_NORMAL_TRADING",
}

_VALID_URGENCIES = frozenset({"patient", "balanced", "fast"})


def _clamp(price: Decimal, low: Decimal | None, high: Decimal | None) -> Decimal:
    if low is not None and price < low:
        price = low
    if high is not None and price > high:
        price = high
    return price


def _deviation_bounds(
    last_price: Decimal,
    settings: Settings,
    limit_down: Decimal | None,
    limit_up: Decimal | None,
) -> tuple[Decimal, Decimal]:
    max_buy = last_price * (Decimal(1) + settings.max_price_deviation_pct / Decimal(100))
    min_buy = last_price * (Decimal(1) - settings.max_price_deviation_pct / Decimal(100))
    return _clamp(min_buy, limit_down, limit_up), _clamp(max_buy, limit_down, limit_up)


def _snap(
    price: Decimal,
    increment: Decimal | None,
    *,
    round_up: bool = False,
) -> Decimal:
    return quantize_to_increment(price, increment, direction="buy", round_up=round_up)


def _spread_width_pct(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    if bid is None or ask is None or ask <= 0:
        return None
    return ((ask - bid) / ask * Decimal(100)).quantize(Decimal("0.01"))


def _session_warnings(
    snapshot: MarketSnapshot,
    settings: Settings,
    session_state: MoexSessionState | None = None,
) -> list[str]:
    warnings: list[str] = []
    if not snapshot.is_fresh:
        age = snapshot.age_seconds if snapshot.age_seconds is not None else -1
        warnings.append(f"Market data is stale (age={age:.0f}s, max={settings.market_data_max_age_seconds}s)")
    if snapshot.trading_status and snapshot.trading_status not in _TRADEABLE_STATUSES:
        warnings.append(f"Trading status may block execution: {snapshot.trading_status}")
    if not snapshot.api_trade_available:
        warnings.append("API trading is not currently available for this instrument")
    # The MOEX clock catches clearing pauses / closed hours that trading_status
    # does not flag (it stays NORMAL_TRADING through the 18:40–19:05 break).
    if session_state is not None and (not session_state.tradeable_now or session_state.closing_soon):
        warnings.append(session_state.message)
    if warnings:
        warnings.append(
            "Even a 'fast' limit may stay SUBMITTED until the exchange session is open and liquidity is available."
        )
    return warnings


def _session_info(
    snapshot: MarketSnapshot,
    settings: Settings,
    session_state: MoexSessionState | None,
) -> SessionInfo:
    return SessionInfo(
        trading_status=snapshot.trading_status,
        api_trade_available=snapshot.api_trade_available,
        market_data_fresh=snapshot.is_fresh,
        tradeable_now=session_state.tradeable_now if session_state else None,
        session_phase=session_state.phase if session_state else None,
        closing_soon=session_state.closing_soon if session_state else None,
        resumes_at=session_state.resumes_at if session_state else None,
        warnings=_session_warnings(snapshot, settings, session_state),
    )


def _tier(
    *,
    limit_price: Decimal,
    label: str,
    fill_expectation: str,
    note: str,
) -> PriceHintTier:
    return PriceHintTier(
        limit_price=limit_price,
        label=label,
        fill_expectation=fill_expectation,  # type: ignore[arg-type]
        note=note,
    )


def compute_buy_price_hints(
    snapshot: MarketSnapshot,
    instrument: InvestmentInstrument,
    settings: Settings,
    session_state: MoexSessionState | None = None,
) -> BuyPriceHints:
    """Derive patient / balanced / fast BUY limit prices that respect exchange and risk bounds."""
    increment = instrument.min_price_increment
    bid = snapshot.best_bid
    ask = snapshot.best_ask
    last = snapshot.last_price
    limit_down = snapshot.limit_down
    limit_up = snapshot.limit_up

    min_buy = limit_down
    max_buy = limit_up
    if last is not None and last > 0:
        min_buy, max_buy = _deviation_bounds(last, settings, limit_down, limit_up)

    def _prepare(raw: Decimal, *, round_up: bool) -> Decimal:
        return _snap(_clamp(raw, min_buy, max_buy), increment, round_up=round_up)

    epsilon = (last * _FALLBACK_BPS / Decimal(100)) if last and last > 0 else _FALLBACK_BPS

    patient_raw = bid if bid is not None else ((last - epsilon) if last else _ZERO)
    balanced_raw = (
        (bid + ask) / Decimal(2) if bid is not None and ask is not None else (last if last is not None else patient_raw)
    )
    fast_raw = ask if ask is not None else ((last + epsilon) if last else _ZERO)

    patient_price = _prepare(patient_raw, round_up=False) if patient_raw > 0 else _ZERO
    balanced_price = _prepare(balanced_raw, round_up=False) if balanced_raw > 0 else _ZERO
    fast_price = _prepare(fast_raw, round_up=True) if fast_raw > 0 else _ZERO

    fast_expectation = "immediate_when_session_open"
    fast_note = "Crosses the spread at best ask when possible; usually fills quickly in an active session."
    if ask is not None and fast_price < ask:
        fast_expectation = "may_not_cross_spread"
        fast_note = "Fast price is below best ask after risk/corridor clamps — execution may be delayed."

    hints: dict[str, PriceHintTier] = {
        "patient": _tier(
            limit_price=patient_price,
            label="join_bid",
            fill_expectation="slow",
            note="Join the best bid queue; cheapest, slowest — fills when a seller accepts your price.",
        ),
        "balanced": _tier(
            limit_price=balanced_price,
            label="mid_spread",
            fill_expectation="medium",
            note="Mid spread (or last price); compromise between price and speed.",
        ),
        "fast": _tier(
            limit_price=fast_price,
            label="cross_ask",
            fill_expectation=fast_expectation,
            note=fast_note,
        ),
    }

    return BuyPriceHints(
        spread=SpreadInfo(
            bid=bid,
            ask=ask,
            width_pct=_spread_width_pct(bid, ask),
        ),
        hints=hints,
        risk_bounds=RiskBounds(
            min_buy_limit_price=min_buy,
            max_buy_limit_price=max_buy,
            max_deviation_pct=settings.max_price_deviation_pct,
        ),
        session=_session_info(snapshot, settings, session_state),
        recommended_urgency="balanced",
    )


def compute_sell_price_hints(
    snapshot: MarketSnapshot,
    instrument: InvestmentInstrument,
    settings: Settings,
    session_state: MoexSessionState | None = None,
) -> BuyPriceHints:
    """Derive patient / balanced / fast SELL limit prices (mirror of the BUY tiers).

    patient = join the best ask (highest price, waits for a buyer);
    balanced = mid spread; fast = cross to the best bid (quickest fill).
    Same deviation / exchange-corridor clamps as the BUY hints.
    """
    increment = instrument.min_price_increment
    bid = snapshot.best_bid
    ask = snapshot.best_ask
    last = snapshot.last_price
    limit_down = snapshot.limit_down
    limit_up = snapshot.limit_up

    min_sell = limit_down
    max_sell = limit_up
    if last is not None and last > 0:
        min_sell, max_sell = _deviation_bounds(last, settings, limit_down, limit_up)

    def _prepare(raw: Decimal, *, round_down: bool) -> Decimal:
        clamped = _clamp(raw, min_sell, max_sell)
        # SELL default rounding is UP (never sell cheaper than intended);
        # the fast tier rounds DOWN so the snapped price stays at/below the bid.
        return quantize_to_increment(clamped, increment, direction="sell", round_down=round_down)

    epsilon = (last * _FALLBACK_BPS / Decimal(100)) if last and last > 0 else _FALLBACK_BPS

    patient_raw = ask if ask is not None else ((last + epsilon) if last else _ZERO)
    balanced_raw = (
        (bid + ask) / Decimal(2) if bid is not None and ask is not None else (last if last is not None else patient_raw)
    )
    fast_raw = bid if bid is not None else ((last - epsilon) if last else _ZERO)

    patient_price = _prepare(patient_raw, round_down=False) if patient_raw > 0 else _ZERO
    balanced_price = _prepare(balanced_raw, round_down=False) if balanced_raw > 0 else _ZERO
    fast_price = _prepare(fast_raw, round_down=True) if fast_raw > 0 else _ZERO

    fast_expectation = "immediate_when_session_open"
    fast_note = "Crosses the spread at best bid when possible; usually fills quickly in an active session."
    if bid is not None and fast_price > bid:
        fast_expectation = "may_not_cross_spread"
        fast_note = "Fast price is above best bid after risk/corridor clamps — execution may be delayed."

    hints: dict[str, PriceHintTier] = {
        "patient": _tier(
            limit_price=patient_price,
            label="join_ask",
            fill_expectation="slow",
            note="Join the best ask queue; highest price, slowest — fills when a buyer accepts your price.",
        ),
        "balanced": _tier(
            limit_price=balanced_price,
            label="mid_spread",
            fill_expectation="medium",
            note="Mid spread (or last price); compromise between price and speed.",
        ),
        "fast": _tier(
            limit_price=fast_price,
            label="cross_bid",
            fill_expectation=fast_expectation,
            note=fast_note,
        ),
    }

    return BuyPriceHints(
        spread=SpreadInfo(
            bid=bid,
            ask=ask,
            width_pct=_spread_width_pct(bid, ask),
        ),
        hints=hints,
        risk_bounds=RiskBounds(
            min_buy_limit_price=min_sell,
            max_buy_limit_price=max_sell,
            max_deviation_pct=settings.max_price_deviation_pct,
        ),
        session=_session_info(snapshot, settings, session_state),
        recommended_urgency="balanced",
    )


def resolve_buy_limit_price(
    hints: BuyPriceHints,
    *,
    limit_price: Decimal | None = None,
    urgency: str | None = None,
) -> tuple[Decimal, BuyUrgency]:
    """Pick the limit price from explicit input or an urgency tier (default: balanced)."""
    if limit_price is not None:
        return limit_price, _normalize_urgency(urgency) or "balanced"

    tier = _normalize_urgency(urgency) or "balanced"
    tier_hint = hints.hints.get(tier)
    if tier_hint is None or tier_hint.limit_price <= 0:
        raise ValueError(f"No usable {tier} price hint — market data may be missing.")
    return tier_hint.limit_price, tier


def _normalize_urgency(urgency: str | None) -> BuyUrgency | None:
    if urgency is None:
        return None
    key = urgency.strip().lower()
    if key not in _VALID_URGENCIES:
        raise ValueError(f"Invalid urgency {urgency!r}; expected one of: patient, balanced, fast")
    return key  # type: ignore[return-value]


def build_price_vs_hints(
    your_limit: Decimal,
    hints: BuyPriceHints,
    *,
    urgency_used: BuyUrgency,
    user_supplied_price: bool,
    direction: str = "BUY",
) -> PriceVsHints:
    """Compare the chosen limit with algorithmic tiers and surface actionable warnings."""
    fast = hints.hints.get("fast")
    patient = hints.hints.get("patient")
    balanced = hints.hints.get("balanced")
    ask = hints.spread.ask if hints.spread else None
    bid = hints.spread.bid if hints.spread else None
    is_sell = direction.upper() == "SELL"

    if is_sell:
        crosses_spread = bid is not None and your_limit <= bid
    else:
        crosses_spread = ask is not None and your_limit >= ask
    warning: str | None = None

    if user_supplied_price:
        if is_sell:
            if bid is not None and your_limit > bid:
                warning = f"Limit {your_limit} is above best_bid {bid} — order may queue until buyers meet your price."
            elif fast is not None and your_limit > fast.limit_price:
                warning = f"Limit is above the recommended fast price ({fast.limit_price}) — execution may be slower."
        elif ask is not None and your_limit < ask:
            warning = f"Limit {your_limit} is below best_ask {ask} — order may queue until sellers meet your price."
        elif fast is not None and your_limit < fast.limit_price:
            warning = f"Limit is below the recommended fast price ({fast.limit_price}) — execution may be slower."
    elif urgency_used == "patient":
        warning = (
            "Patient tier — expect to wait in the ask queue unless the market moves up."
            if is_sell
            else "Patient tier — expect to wait in the bid queue unless the market moves down."
        )
    elif urgency_used == "fast" and fast and fast.fill_expectation == "may_not_cross_spread":
        warning = fast.note

    return PriceVsHints(
        your_limit=your_limit,
        recommended_patient=patient.limit_price if patient else None,
        recommended_balanced=balanced.limit_price if balanced else None,
        recommended_fast=fast.limit_price if fast else None,
        crosses_spread=crosses_spread,
        urgency_used=urgency_used,
        user_supplied_price=user_supplied_price,
        warning=warning,
    )
