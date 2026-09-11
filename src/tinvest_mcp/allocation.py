"""Deterministic target-allocation rule table (stage 3 of the advisory flow).

The LLM must never invent allocation percentages: the numbers come from this
code, the LLM only explains them to the user. Rules are keyed by
``(risk_profile, horizon)`` and always sum to exactly 100%.

Asset classes are intentionally coarse (bonds / equity / cash) — they map onto
the classes reported by ``get_portfolio_analytics`` so a confirmed allocation
can later be compared against the live portfolio (drift / rebalance).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from .schemas import (
    ASSET_CLASSES,
    AllocationDrift,
    DriftItem,
    InvestmentHorizon,
    InvestmentProfile,
    MandateViolation,
    RiskProfile,
    TargetAllocation,
)

# Human meaning of each horizon bucket (also surfaced to the LLM).
HORIZON_DESCRIPTIONS: dict[str, str] = {
    "short": "less than 1 year",
    "medium": "1-3 years",
    "long": "more than 3 years",
}

# Role each asset class plays; deterministic wording the LLM can build on.
ASSET_CLASS_ROLES: dict[str, str] = {
    "bonds": "anchor: predictable coupon income, low drawdown",
    "equity": "growth: shares and equity ETFs, higher volatility",
    "cash": "liquidity buffer: instantly available, no market risk",
}

# (risk_profile, horizon) -> {asset_class: target %}. Every row sums to 100.
# Shorter horizon caps equity; higher risk tolerance pushes toward that cap.
ALLOCATION_RULES: dict[tuple[str, str], dict[str, int]] = {
    ("conservative", "short"): {"bonds": 40, "equity": 0, "cash": 60},
    ("conservative", "medium"): {"bonds": 70, "equity": 20, "cash": 10},
    ("conservative", "long"): {"bonds": 60, "equity": 30, "cash": 10},
    ("moderate", "short"): {"bonds": 50, "equity": 20, "cash": 30},
    ("moderate", "medium"): {"bonds": 55, "equity": 35, "cash": 10},
    ("moderate", "long"): {"bonds": 45, "equity": 50, "cash": 5},
    ("aggressive", "short"): {"bonds": 50, "equity": 30, "cash": 20},
    ("aggressive", "medium"): {"bonds": 40, "equity": 55, "cash": 5},
    ("aggressive", "long"): {"bonds": 20, "equity": 75, "cash": 5},
}


def validate_allocation(allocation: Mapping[str, int]) -> dict[str, int]:
    """Validate a caller-supplied allocation; returns a normalized copy.

    Raises ``ValueError`` with an actionable message on bad input.
    """
    unknown = set(allocation) - set(ASSET_CLASSES)
    if unknown:
        raise ValueError(f"Unknown asset classes: {sorted(unknown)}. Allowed: {list(ASSET_CLASSES)}.")
    normalized = {cls: int(allocation.get(cls, 0)) for cls in ASSET_CLASSES}
    negative = [cls for cls, pct in normalized.items() if pct < 0]
    if negative:
        raise ValueError(f"Negative percentages not allowed: {negative}.")
    total = sum(normalized.values())
    if total != 100:
        raise ValueError(f"Allocation must sum to 100%, got {total}%.")
    return normalized


def propose_allocation(risk_profile: RiskProfile, horizon: InvestmentHorizon) -> TargetAllocation:
    """Look up the deterministic allocation for a profile. Pure function."""
    allocation = ALLOCATION_RULES[(risk_profile, horizon)]
    return TargetAllocation(
        risk_profile=risk_profile,
        horizon=horizon,
        horizon_description=HORIZON_DESCRIPTIONS[horizon],
        allocation=dict(allocation),
        asset_class_roles={cls: ASSET_CLASS_ROLES[cls] for cls in ASSET_CLASSES},
        source="rule_table",
        rationale=(
            f"Deterministic rule for a {risk_profile} investor with a {horizon} horizon "
            f"({HORIZON_DESCRIPTIONS[horizon]}): equity share is capped by the horizon, "
            "risk tolerance sets how close the mix sits to that cap."
        ),
    )


# --- mandate-aware screening (advisory stage 5) ------------------------------

# Deterministic mandate defaults derived from the profile; overridable via the
# optional fields on InvestmentProfile (save_investment_profile).
DEFAULT_BOND_RISK_BY_PROFILE: dict[str, str] = {
    "conservative": "low",
    "moderate": "moderate",
    "aggressive": "high",
}

# Bond duration should not exceed the horizon ("ladder"): rate risk you cannot
# sit out. None = no cap.
HORIZON_MAX_BOND_DURATION_YEARS: dict[str, float | None] = {
    "short": 1.0,
    "medium": 3.0,
    "long": None,
}

BOND_RISK_ORDER: dict[str, int] = {"low": 0, "moderate": 1, "high": 2}


@dataclass(frozen=True)
class MandateScreenFilters:
    """Screener-ready filters derived from the saved profile. Computed by code."""

    excluded_sectors: tuple[str, ...]
    max_bond_risk_level: str
    max_bond_duration_years: float | None


def mandate_screen_filters(profile: InvestmentProfile) -> MandateScreenFilters:
    """Translate the saved profile into deterministic screener filters."""
    return MandateScreenFilters(
        excluded_sectors=tuple(s.lower() for s in profile.excluded_sectors),
        max_bond_risk_level=(profile.max_bond_risk_level or DEFAULT_BOND_RISK_BY_PROFILE[profile.risk_profile]),
        max_bond_duration_years=HORIZON_MAX_BOND_DURATION_YEARS[profile.horizon],
    )


@dataclass(frozen=True)
class MandateLimits:
    """Personal-mandate limits for plan-level checks: profile overrides, server defaults."""

    min_cash_pct: Decimal  # % of portfolio
    max_issuer_weight: Decimal  # fraction (0.15 = 15%)
    max_sector_weight: Decimal  # fraction


def mandate_limits(
    profile: InvestmentProfile,
    *,
    default_max_issuer_weight: Decimal,
    default_max_sector_weight: Decimal,
    rebalance_threshold_pct: Decimal,
) -> MandateLimits:
    """Resolve plan-check limits from the saved profile with deterministic fallbacks.

    ``min_cash_pct`` defaults to the target cash share minus the rebalance band —
    a plan may spend cash down to the lower edge of the mandate corridor.
    """
    if profile.min_cash_pct is not None:
        min_cash = profile.min_cash_pct
    else:
        target_cash = Decimal(profile.target_allocation.allocation.get("cash", 0))
        min_cash = max(Decimal(0), target_cash - rebalance_threshold_pct)
    return MandateLimits(
        min_cash_pct=min_cash,
        max_issuer_weight=(
            profile.max_issuer_weight_pct / _HUNDRED
            if profile.max_issuer_weight_pct is not None
            else default_max_issuer_weight
        ),
        max_sector_weight=(
            profile.max_sector_weight_pct / _HUNDRED
            if profile.max_sector_weight_pct is not None
            else default_max_sector_weight
        ),
    )


# --- adaptive per-issuer concentration caps (plan checks: features B + C1) ---

# Categories that drive the per-issuer cap. A diversified fund is not a single
# issuer bet; a sovereign/quasi-sovereign bond carries lower issuer risk than a
# corporate name of the same weight.
ISSUER_CAP_SINGLE = "single"
ISSUER_CAP_SOVEREIGN = "sovereign"
ISSUER_CAP_FUND = "fund"

# Instrument types treated as internally diversified funds (excluded from the cap).
_FUND_TYPES = frozenset({"etf", "fund"})
# T-Invest sector tokens we treat as sovereign / quasi-sovereign. Detection is
# sector-based on purpose: e.g. the Kazakhstan sovereign RU000A101RZ3 lists on a
# corporate board with sector=null, so it stays a single name (no over-relaxing).
SOVEREIGN_BOND_SECTORS = frozenset({"government", "municipal"})

# Strictness order for merging categories when one issuer bucket mixes types
# (higher = stricter / lower cap; the strictest wins).
_ISSUER_CAP_STRICTNESS: dict[str, int] = {
    ISSUER_CAP_FUND: 0,
    ISSUER_CAP_SOVEREIGN: 1,
    ISSUER_CAP_SINGLE: 2,
}


def issuer_cap_category(instrument_type: str | None, sector: str | None) -> str:
    """Classify an instrument for the per-issuer cap. Pure function."""
    itype = (instrument_type or "").lower()
    if itype in _FUND_TYPES:
        return ISSUER_CAP_FUND
    if itype == "bond" and (sector or "").lower() in SOVEREIGN_BOND_SECTORS:
        return ISSUER_CAP_SOVEREIGN
    return ISSUER_CAP_SINGLE


def stricter_issuer_category(a: str, b: str) -> str:
    """Return the stricter of two categories (used when a bucket mixes instruments)."""
    return a if _ISSUER_CAP_STRICTNESS.get(a, 2) >= _ISSUER_CAP_STRICTNESS.get(b, 2) else b


def scaled_issuer_cap(
    total_value: Decimal,
    *,
    base_cap: Decimal,
    small_portfolio_rub: Decimal,
    small_cap: Decimal,
    mid_portfolio_rub: Decimal,
    mid_cap: Decimal,
) -> Decimal:
    """Single-name cap scaled by portfolio size (C1): smaller portfolios get a higher cap.

    Never drops below ``base_cap`` — the tiers only relax the limit for small books.
    """
    if total_value <= 0:
        return base_cap
    if total_value <= small_portfolio_rub:
        return max(base_cap, small_cap)
    if total_value <= mid_portfolio_rub:
        return max(base_cap, mid_cap)
    return base_cap


@dataclass(frozen=True)
class IssuerCapPolicy:
    """Resolved caps per issuer category. ``fund_cap=None`` excludes funds from the cap."""

    single_cap: Decimal
    sovereign_cap: Decimal
    fund_cap: Decimal | None = None


def resolve_issuer_cap(category: str, policy: IssuerCapPolicy) -> Decimal | None:
    """Cap for one issuer bucket. ``None`` means the issuer cap does not apply."""
    if category == ISSUER_CAP_FUND:
        return policy.fund_cap
    if category == ISSUER_CAP_SOVEREIGN:
        return max(policy.sovereign_cap, policy.single_cap)
    return policy.single_cap


def build_issuer_caps(categories: Mapping[str, str], policy: IssuerCapPolicy) -> dict[str, Decimal | None]:
    """Resolve a per-issuer-bucket cap map (issuer -> cap fraction, or None to skip)."""
    return {issuer: resolve_issuer_cap(cat, policy) for issuer, cat in categories.items()}


# --- gap analysis (advisory stage 4) ----------------------------------------

# get_portfolio_analytics class buckets -> target allocation classes.
# Funds are counted as equity regardless of focus (coarse MVP mapping).
PORTFOLIO_CLASS_TO_TARGET: dict[str, str] = {
    "bonds": "bonds",
    "shares": "equity",
    "funds": "equity",
    "cash": "cash",
}

_PCT = Decimal("0.01")
_HUNDRED = Decimal("100")


def compute_drift(
    profile: InvestmentProfile,
    class_values: Mapping[str, Decimal],
    total_value: Decimal,
    *,
    issuer_weights: Mapping[str, Decimal],
    sector_weights: Mapping[str, Decimal],
    max_issuer_weight: Decimal,
    max_sector_weight: Decimal,
    rebalance_threshold_pct: Decimal,
) -> AllocationDrift:
    """Compare the live portfolio against the saved target allocation. Pure function.

    ``class_values`` are absolute values per get_portfolio_analytics class bucket
    (shares/bonds/funds/cash/...); issuer/sector weights are fractions of total.
    """
    target = profile.target_allocation.allocation
    notes: list[str] = []

    current: dict[str, Decimal] = {cls: Decimal(0) for cls in ASSET_CLASSES}
    unmapped = Decimal(0)
    for bucket, value in class_values.items():
        mapped = PORTFOLIO_CLASS_TO_TARGET.get(bucket)
        if mapped is None:
            unmapped += value
        else:
            current[mapped] += value
    if any(cls in class_values for cls in ("funds",)):
        notes.append("Funds/ETFs are counted as equity regardless of focus (coarse mapping).")
    if unmapped > 0:
        notes.append("Some portfolio value is outside bonds/equity/cash and excluded from drift math.")

    items: list[DriftItem] = []
    max_abs_dev = Decimal(0)
    for cls in ASSET_CLASSES:
        target_pct = Decimal(target.get(cls, 0))
        current_value = current[cls]
        if total_value > 0:
            current_pct = (current_value / total_value * _HUNDRED).quantize(_PCT)
            target_value = (total_value * target_pct / _HUNDRED).quantize(_PCT)
        else:
            current_pct = Decimal(0)
            target_value = Decimal(0)
        deviation = (current_pct - target_pct).quantize(_PCT)
        amount = (target_value - current_value).quantize(_PCT)
        if abs(deviation) < rebalance_threshold_pct or amount == 0:
            action = "hold"
        else:
            action = "buy" if amount > 0 else "sell"
        max_abs_dev = max(max_abs_dev, abs(deviation))
        items.append(
            DriftItem(
                asset_class=cls,
                current_pct=current_pct,
                target_pct=target_pct,
                deviation_pct=deviation,
                current_value=current_value,
                target_value=target_value,
                amount_to_trade=amount,
                action=action,
            )
        )

    violations: list[MandateViolation] = []
    for kind, weights, limit in (
        ("issuer_weight", issuer_weights, max_issuer_weight),
        ("sector_weight", sector_weights, max_sector_weight),
    ):
        for subject, weight in sorted(weights.items(), key=lambda kv: kv[1], reverse=True):
            if kind == "sector_weight" and subject == "unknown":
                continue
            if weight <= limit:
                continue
            excess = ((weight - limit) * total_value).quantize(_PCT) if total_value > 0 else Decimal(0)
            violations.append(
                MandateViolation(
                    kind=kind,
                    subject=subject,
                    weight=weight,
                    limit=limit,
                    excess_value=excess,
                    message=(
                        f"{'Issuer' if kind == 'issuer_weight' else 'Sector'} '{subject}' is "
                        f"{(weight * _HUNDRED).quantize(_PCT)}% of portfolio, mandate limit is "
                        f"{(limit * _HUNDRED).quantize(_PCT)}%."
                    ),
                )
            )
    if "unknown" in sector_weights:
        notes.append("Positions with unknown sector are skipped in the sector mandate check.")

    if total_value <= 0:
        notes.append("Portfolio value is zero — drift amounts are not meaningful yet.")

    return AllocationDrift(
        risk_profile=profile.risk_profile,
        horizon=profile.horizon,
        rebalance_threshold_pct=rebalance_threshold_pct,
        rebalance_needed=any(item.action != "hold" for item in items),
        max_abs_deviation_pct=max_abs_dev,
        items=items,
        unmapped_value=unmapped,
        mandate_violations=violations,
        notes=notes,
    )
