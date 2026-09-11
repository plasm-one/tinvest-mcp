from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tinvest_mcp.allocation import (
    ALLOCATION_RULES,
    HORIZON_DESCRIPTIONS,
    ISSUER_CAP_FUND,
    ISSUER_CAP_SINGLE,
    ISSUER_CAP_SOVEREIGN,
    IssuerCapPolicy,
    build_issuer_caps,
    compute_drift,
    issuer_cap_category,
    mandate_screen_filters,
    propose_allocation,
    resolve_issuer_cap,
    scaled_issuer_cap,
    stricter_issuer_category,
    validate_allocation,
)
from tinvest_mcp.profile_store import load_profile, save_profile
from tinvest_mcp.schemas import ASSET_CLASSES, InvestmentProfile

RISK_PROFILES = ("conservative", "moderate", "aggressive")
HORIZONS = ("short", "medium", "long")


def test_rule_table_covers_every_combination():
    assert set(ALLOCATION_RULES) == {(r, h) for r in RISK_PROFILES for h in HORIZONS}
    assert set(HORIZON_DESCRIPTIONS) == set(HORIZONS)


@pytest.mark.parametrize("key", sorted(ALLOCATION_RULES))
def test_every_rule_sums_to_100_over_known_classes(key):
    rule = ALLOCATION_RULES[key]
    assert set(rule) == set(ASSET_CLASSES)
    assert all(pct >= 0 for pct in rule.values())
    assert sum(rule.values()) == 100


# --- adaptive per-issuer caps (features B + C1) -------------------------------


def test_issuer_cap_category_classifies_by_type_and_sector():
    # Funds are internally diversified.
    assert issuer_cap_category("etf", "financial") == ISSUER_CAP_FUND
    assert issuer_cap_category("fund", None) == ISSUER_CAP_FUND
    # Government / municipal bonds are sovereign / quasi-sovereign.
    assert issuer_cap_category("bond", "government") == ISSUER_CAP_SOVEREIGN
    assert issuer_cap_category("bond", "MUNICIPAL") == ISSUER_CAP_SOVEREIGN
    # A corporate bond, a share, and a bond without a sector are single names.
    assert issuer_cap_category("bond", "financial") == ISSUER_CAP_SINGLE
    assert issuer_cap_category("share", "financial") == ISSUER_CAP_SINGLE
    # The Kazakhstan sovereign lists on a corporate board with sector=null → single.
    assert issuer_cap_category("bond", None) == ISSUER_CAP_SINGLE


def test_stricter_issuer_category_prefers_the_lower_cap():
    assert stricter_issuer_category(ISSUER_CAP_FUND, ISSUER_CAP_SINGLE) == ISSUER_CAP_SINGLE
    assert stricter_issuer_category(ISSUER_CAP_SOVEREIGN, ISSUER_CAP_FUND) == ISSUER_CAP_SOVEREIGN
    assert stricter_issuer_category(ISSUER_CAP_SINGLE, ISSUER_CAP_SINGLE) == ISSUER_CAP_SINGLE


def test_scaled_issuer_cap_relaxes_for_small_portfolios():
    kwargs = {
        "base_cap": Decimal("0.15"),
        "small_portfolio_rub": Decimal("100000"),
        "small_cap": Decimal("0.30"),
        "mid_portfolio_rub": Decimal("500000"),
        "mid_cap": Decimal("0.20"),
    }
    assert scaled_issuer_cap(Decimal("50000"), **kwargs) == Decimal("0.30")  # small book
    assert scaled_issuer_cap(Decimal("100000"), **kwargs) == Decimal("0.30")  # boundary inclusive
    assert scaled_issuer_cap(Decimal("300000"), **kwargs) == Decimal("0.20")  # mid book
    assert scaled_issuer_cap(Decimal("900000"), **kwargs) == Decimal("0.15")  # large book → floor
    # Tiers only ever relax: a high base is never lowered.
    assert scaled_issuer_cap(Decimal("50000"), **{**kwargs, "base_cap": Decimal("0.40")}) == Decimal("0.40")
    assert scaled_issuer_cap(Decimal("0"), **kwargs) == Decimal("0.15")  # unknown size → base


def test_resolve_and_build_issuer_caps():
    policy = IssuerCapPolicy(single_cap=Decimal("0.30"), sovereign_cap=Decimal("0.35"), fund_cap=None)
    assert resolve_issuer_cap(ISSUER_CAP_FUND, policy) is None  # funds exempt
    assert resolve_issuer_cap(ISSUER_CAP_SOVEREIGN, policy) == Decimal("0.35")
    assert resolve_issuer_cap(ISSUER_CAP_SINGLE, policy) == Decimal("0.30")
    # Sovereign never falls below the (possibly relaxed) single cap.
    relaxed = IssuerCapPolicy(single_cap=Decimal("0.40"), sovereign_cap=Decimal("0.35"), fund_cap=None)
    assert resolve_issuer_cap(ISSUER_CAP_SOVEREIGN, relaxed) == Decimal("0.40")

    caps = build_issuer_caps(
        {"OFZ": ISSUER_CAP_SOVEREIGN, "Sber": ISSUER_CAP_SINGLE, "BroadETF": ISSUER_CAP_FUND},
        policy,
    )
    assert caps == {"OFZ": Decimal("0.35"), "Sber": Decimal("0.30"), "BroadETF": None}


def test_propose_allocation_is_deterministic_and_matches_table():
    first = propose_allocation("conservative", "medium")
    second = propose_allocation("conservative", "medium")
    assert first == second
    assert first.allocation == ALLOCATION_RULES[("conservative", "medium")]
    assert first.allocation == {"bonds": 70, "equity": 20, "cash": 10}
    assert first.source == "rule_table"


def test_equity_grows_with_risk_and_horizon():
    for horizon in HORIZONS:
        equities = [ALLOCATION_RULES[(r, horizon)]["equity"] for r in RISK_PROFILES]
        assert equities == sorted(equities), f"equity not monotonic in risk for {horizon}"
    for risk in RISK_PROFILES:
        short_eq = ALLOCATION_RULES[(risk, "short")]["equity"]
        long_eq = ALLOCATION_RULES[(risk, "long")]["equity"]
        assert short_eq <= long_eq, f"equity should not shrink with horizon for {risk}"


def test_validate_allocation_accepts_valid_and_fills_missing_classes():
    assert validate_allocation({"bonds": 60, "equity": 40}) == {
        "bonds": 60,
        "equity": 40,
        "cash": 0,
    }


@pytest.mark.parametrize(
    "bad, match",
    [
        ({"bonds": 50, "equity": 40}, "sum to 100"),
        ({"bonds": 110, "equity": -10, "cash": 0}, "Negative"),
        ({"bonds": 50, "crypto": 50}, "Unknown asset classes"),
    ],
)
def test_validate_allocation_rejects_invalid(bad, match):
    with pytest.raises(ValueError, match=match):
        validate_allocation(bad)


def test_profile_store_roundtrip(tmp_path):
    path = str(tmp_path / "nested" / "profile.json")
    assert load_profile(path) is None

    profile = InvestmentProfile(
        risk_profile="conservative",
        horizon="medium",
        target_allocation=propose_allocation("conservative", "medium"),
        notes="Goal: down payment in ~2 years.",
        saved_at=datetime.now(UTC),
    )
    save_profile(path, profile)
    loaded = load_profile(path)
    assert loaded == profile

    # Overwrite wins.
    updated = profile.model_copy(
        update={"horizon": "long", "target_allocation": propose_allocation("conservative", "long")}
    )
    save_profile(path, updated)
    assert load_profile(path) == updated


def test_profile_store_corrupt_file_returns_none(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_profile(str(path)) is None


# --- gap analysis (stage 4) --------------------------------------------------


def _profile(risk="conservative", horizon="medium"):
    return InvestmentProfile(
        risk_profile=risk,
        horizon=horizon,
        target_allocation=propose_allocation(risk, horizon),
        saved_at=datetime.now(UTC),
    )


def _drift(
    profile, class_values, total, *, issuers=None, sectors=None, max_issuer="0.15", max_sector="0.30", threshold="5"
):
    return compute_drift(
        profile,
        {k: Decimal(v) for k, v in class_values.items()},
        Decimal(total),
        issuer_weights={k: Decimal(v) for k, v in (issuers or {}).items()},
        sector_weights={k: Decimal(v) for k, v in (sectors or {}).items()},
        max_issuer_weight=Decimal(max_issuer),
        max_sector_weight=Decimal(max_sector),
        rebalance_threshold_pct=Decimal(threshold),
    )


def test_drift_from_scratch_all_cash_gap_equals_target():
    # Scenario "с нуля": 100% cash → gap is the whole target allocation.
    drift = _drift(_profile(), {"cash": "300000"}, "300000")
    by_class = {item.asset_class: item for item in drift.items}

    assert by_class["bonds"].action == "buy"
    assert by_class["bonds"].amount_to_trade == Decimal("210000.00")  # 70%
    assert by_class["equity"].action == "buy"
    assert by_class["equity"].amount_to_trade == Decimal("60000.00")  # 20%
    assert by_class["cash"].action == "sell"  # 100% -> 10%
    assert by_class["cash"].amount_to_trade == Decimal("-270000.00")
    assert drift.rebalance_needed
    assert drift.max_abs_deviation_pct == Decimal("90.00")
    # Trades net to zero: rebalancing only moves money between classes.
    assert sum(i.amount_to_trade for i in drift.items) == 0


def test_drift_underweight_bonds_example_from_plan():
    # "облигаций 40% вместо 70% → докупить на ~90 тыс." (total 300k).
    drift = _drift(
        _profile(),
        {"bonds": "120000", "shares": "90000", "funds": "60000", "cash": "30000"},
        "300000",
    )
    by_class = {item.asset_class: item for item in drift.items}
    bonds = by_class["bonds"]
    assert bonds.current_pct == Decimal("40.00")
    assert bonds.target_pct == 70
    assert bonds.deviation_pct == Decimal("-30.00")
    assert bonds.amount_to_trade == Decimal("90000.00")
    assert bonds.action == "buy"
    # shares+funds = 50% vs equity target 20% → sell.
    assert by_class["equity"].current_pct == Decimal("50.00")
    assert by_class["equity"].action == "sell"


def test_drift_within_threshold_is_hold():
    # 72/19/9 vs 70/20/10 with threshold 5pp → everything holds.
    drift = _drift(
        _profile(),
        {"bonds": "72000", "shares": "19000", "cash": "9000"},
        "100000",
    )
    assert not drift.rebalance_needed
    assert all(item.action == "hold" for item in drift.items)


def test_drift_mandate_violations_issuer_and_sector():
    drift = _drift(
        _profile(),
        {"bonds": "70000", "shares": "20000", "cash": "10000"},
        "100000",
        issuers={"Компания X": "0.25", "Компания Y": "0.10"},
        sectors={"it": "0.40", "energy": "0.20", "unknown": "0.99"},
    )
    kinds = {(v.kind, v.subject) for v in drift.mandate_violations}
    assert ("issuer_weight", "Компания X") in kinds
    assert ("sector_weight", "it") in kinds
    assert ("issuer_weight", "Компания Y") not in kinds
    assert not any(v.subject == "unknown" for v in drift.mandate_violations)

    issuer_v = next(v for v in drift.mandate_violations if v.kind == "issuer_weight")
    assert issuer_v.excess_value == Decimal("10000.00")  # (0.25-0.15) * 100000
    assert "25.00%" in issuer_v.message and "15.00%" in issuer_v.message


def test_drift_unmapped_class_excluded_with_note():
    drift = _drift(_profile(), {"bonds": "70000", "other": "30000"}, "100000")
    assert drift.unmapped_value == Decimal("30000")
    assert any("outside bonds/equity/cash" in n for n in drift.notes)


def test_drift_zero_portfolio_is_safe():
    drift = _drift(_profile(), {}, "0")
    assert not drift.rebalance_needed
    assert all(item.amount_to_trade == 0 for item in drift.items)
    assert any("zero" in n for n in drift.notes)


# --- mandate screen filters (stage 5) ----------------------------------------


def test_mandate_defaults_derived_from_profile():
    f = mandate_screen_filters(_profile("conservative", "medium"))
    assert f.max_bond_risk_level == "low"
    assert f.max_bond_duration_years == 3.0
    assert f.excluded_sectors == ()

    f = mandate_screen_filters(_profile("aggressive", "long"))
    assert f.max_bond_risk_level == "high"
    assert f.max_bond_duration_years is None

    f = mandate_screen_filters(_profile("moderate", "short"))
    assert f.max_bond_risk_level == "moderate"
    assert f.max_bond_duration_years == 1.0


def test_mandate_profile_overrides_win():
    profile = _profile("aggressive", "long").model_copy(
        update={
            "excluded_sectors": ["IT", "Energy"],
            "max_bond_risk_level": "low",
        }
    )
    f = mandate_screen_filters(profile)
    assert f.excluded_sectors == ("it", "energy")  # normalized to lowercase
    assert f.max_bond_risk_level == "low"  # explicit cap beats profile default
