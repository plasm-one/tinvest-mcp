"""Environment-driven configuration for the T-Invest MCP server.

Secrets (tokens) are read from ``.env`` only. Non-secret settings come from
``config.toml`` (section ``tinvest``). Use :func:`get_settings` everywhere; it
is cached so configuration is resolved once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache

from .sources import (
    read_config_bool,
    read_config_int,
    read_config_list,
    read_config_str,
    read_env_str,
)

SANDBOX = "sandbox"
PROD = "prod"

DEFAULT_ALLOWED_INSTRUMENT_TYPES = ("share", "bond", "etf")
_SECTION = "tinvest"

# Prod keeps tight guardrails; sandbox defaults are relaxed for virtual-money experimentation.
_PROD_LIMIT_DEFAULTS = {
    "max_order_rub": "1500",
    "max_daily_turnover_rub": "3000",
    "max_position_weight": "0.40",
}
_SANDBOX_LIMIT_DEFAULTS = {
    "max_order_rub": "500000",
    "max_daily_turnover_rub": "5000000",
    "max_position_weight": "1.0",
}


def _read_bool(key: str, default: bool) -> bool:
    return read_config_bool(_SECTION, key, default)


def _read_decimal(key: str, default: str) -> Decimal:
    raw = read_config_str(_SECTION, key, default)
    try:
        return Decimal(raw) if raw else Decimal(default)
    except Exception:
        return Decimal(default)


def _read_int(key: str, default: int) -> int:
    return read_config_int(_SECTION, key, default)


def _read_list(key: str) -> list[str]:
    return read_config_list(_SECTION, key)


@dataclass(frozen=True)
class Settings:
    """Resolved, non-secret-aware view of the configuration.

    Token fields hold the raw secret but the dataclass ``repr`` is overridden so
    accidental logging never leaks them.
    """

    mode: str
    sandbox_token: str | None
    readonly_token: str | None
    fullaccess_token: str | None
    account_id: str | None

    max_order_rub: Decimal
    max_daily_turnover_rub: Decimal
    max_position_weight: Decimal
    confirmation_ttl_seconds: int
    max_price_deviation_pct: Decimal
    market_data_max_age_seconds: int

    allow_market_orders: bool
    allow_margin: bool
    allow_shorts: bool
    require_single_account_token: bool
    enable_real_trading: bool

    allowed_instrument_types: tuple[str, ...]
    instrument_allowlist: tuple[str, ...] = field(default=())

    analytics_concurrency: int = 4
    investment_profile_path: str = "investment_profile.json"

    # Advisory (non-blocking) gap-analysis parameters for get_portfolio_analytics.drift.
    rebalance_threshold_pct: Decimal = Decimal("5")
    mandate_max_issuer_weight: Decimal = Decimal("0.15")
    mandate_max_sector_weight: Decimal = Decimal("0.30")

    # Adaptive per-issuer concentration cap (plan checks). Small portfolios cannot
    # spread across many names without paying more in commission/lot friction than
    # the diversification is worth, so the single-name cap scales with portfolio size
    # (C1) and by instrument nature (B): funds are internally diversified (excluded),
    # sovereign/quasi-sovereign bonds carry lower issuer risk (higher cap).
    mandate_small_portfolio_rub: Decimal = Decimal("100000")
    mandate_small_issuer_weight: Decimal = Decimal("0.30")
    mandate_mid_portfolio_rub: Decimal = Decimal("500000")
    mandate_mid_issuer_weight: Decimal = Decimal("0.20")
    mandate_sovereign_issuer_weight: Decimal = Decimal("0.35")
    # Cap on the share of the portfolio denominated in a foreign currency, applied
    # once the profile opts into FX exposure (allow_fx_linked). Without that opt-in
    # any FX-denominated buy raises a warning to be acknowledged, whatever its size.
    mandate_max_fx_exposure: Decimal = Decimal("0.20")
    # Ratchet (A): a soft mandate breach that shrinks an existing breach by at least
    # this many pp counts as progress and passes instead of blocking.
    mandate_min_progress_pp: Decimal = Decimal("1")

    # SELL preview parameters (stage 7): НДФЛ estimate rate and warning windows.
    sell_tax_rate: Decimal = Decimal("0.13")
    ldv_warning_months: int = 6
    corporate_action_warning_days: int = 30

    # Trade plan (stage 6): plan TTL, commission fallback when GetOrderPrice is
    # unavailable, and the cost-benefit ceiling (costs / misallocation removed).
    trade_plan_ttl_seconds: int = 900
    plan_fallback_commission_pct: Decimal = Decimal("0.3")
    plan_max_cost_to_benefit_ratio: Decimal = Decimal("0.05")

    # Market-session gate (MOEX fondovy clock): block orders during clearing
    # pauses / closed hours (a limit submitted then just rests unmatched) and
    # warn when a still-open window closes within this many seconds.
    market_session_check_enabled: bool = True
    market_session_close_buffer_seconds: int = 120

    # Default urgency for CONFIRMED plan steps. "balanced" sits mid-spread and can
    # rest unfilled for many minutes (the plan then crawls); "fast" crosses to the
    # ask/bid and normally fills at once, paying at most the spread. Per-step
    # previews may still override it explicitly.
    plan_execution_urgency: str = "fast"
    # Cache TTL for static instrument reference data (lot/nominal/increment).
    instrument_cache_ttl_seconds: int = 300

    @property
    def is_sandbox(self) -> bool:
        return self.mode == SANDBOX

    @property
    def is_prod(self) -> bool:
        return self.mode == PROD

    @property
    def real_trading_enabled(self) -> bool:
        return self.is_prod and self.enable_real_trading

    def active_read_token(self) -> str | None:
        if self.is_sandbox:
            return self.sandbox_token
        return self.readonly_token or self.fullaccess_token

    def active_trade_token(self) -> str | None:
        if self.is_sandbox:
            return self.sandbox_token
        return self.fullaccess_token

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"Settings(mode={self.mode!r}, account_id={mask(self.account_id)!r}, "
            f"real_trading_enabled={self.real_trading_enabled}, "
            f"max_order_rub={self.max_order_rub}, tokens=<redacted>)"
        )


def mask(value: str | None) -> str | None:
    if not value:
        return value
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}***{value[-3:]}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    mode = (read_config_str(_SECTION, "mode", SANDBOX) or SANDBOX).strip().lower()
    if mode not in {SANDBOX, PROD}:
        mode = SANDBOX

    account_id = read_config_str(_SECTION, "account_id") or None
    limit_defaults = _SANDBOX_LIMIT_DEFAULTS if mode == SANDBOX else _PROD_LIMIT_DEFAULTS

    return Settings(
        mode=mode,
        sandbox_token=read_env_str("TINVEST_SANDBOX_TOKEN"),
        readonly_token=read_env_str("TINVEST_READONLY_TOKEN"),
        fullaccess_token=read_env_str("TINVEST_FULLACCESS_TOKEN"),
        account_id=account_id,
        max_order_rub=_read_decimal("max_order_rub", limit_defaults["max_order_rub"]),
        max_daily_turnover_rub=_read_decimal("max_daily_turnover_rub", limit_defaults["max_daily_turnover_rub"]),
        max_position_weight=_read_decimal("max_position_weight", limit_defaults["max_position_weight"]),
        confirmation_ttl_seconds=_read_int("confirmation_ttl_seconds", 60),
        max_price_deviation_pct=_read_decimal("max_price_deviation_pct", "1.0"),
        market_data_max_age_seconds=_read_int("market_data_max_age_seconds", 10),
        allow_market_orders=_read_bool("allow_market_orders", False),
        allow_margin=_read_bool("allow_margin", False),
        allow_shorts=_read_bool("allow_shorts", False),
        require_single_account_token=_read_bool("require_single_account_token", True),
        enable_real_trading=_read_bool("enable_real_trading", False),
        allowed_instrument_types=tuple(
            _read_list("allowed_instrument_types") or list(DEFAULT_ALLOWED_INSTRUMENT_TYPES)
        ),
        instrument_allowlist=tuple(_read_list("instrument_allowlist")),
        analytics_concurrency=max(1, _read_int("analytics_concurrency", 4)),
        investment_profile_path=(
            read_config_str(_SECTION, "investment_profile_path", "investment_profile.json") or "investment_profile.json"
        ),
        rebalance_threshold_pct=_read_decimal("rebalance_threshold_pct", "5"),
        mandate_max_issuer_weight=_read_decimal("mandate_max_issuer_weight", "0.15"),
        mandate_max_sector_weight=_read_decimal("mandate_max_sector_weight", "0.30"),
        mandate_small_portfolio_rub=_read_decimal("mandate_small_portfolio_rub", "100000"),
        mandate_small_issuer_weight=_read_decimal("mandate_small_issuer_weight", "0.30"),
        mandate_mid_portfolio_rub=_read_decimal("mandate_mid_portfolio_rub", "500000"),
        mandate_mid_issuer_weight=_read_decimal("mandate_mid_issuer_weight", "0.20"),
        mandate_sovereign_issuer_weight=_read_decimal("mandate_sovereign_issuer_weight", "0.35"),
        mandate_max_fx_exposure=_read_decimal("mandate_max_fx_exposure", "0.20"),
        mandate_min_progress_pp=_read_decimal("mandate_min_progress_pp", "1"),
        sell_tax_rate=_read_decimal("sell_tax_rate", "0.13"),
        ldv_warning_months=_read_int("ldv_warning_months", 6),
        corporate_action_warning_days=_read_int("corporate_action_warning_days", 30),
        trade_plan_ttl_seconds=_read_int("trade_plan_ttl_seconds", 900),
        plan_fallback_commission_pct=_read_decimal("plan_fallback_commission_pct", "0.3"),
        plan_max_cost_to_benefit_ratio=_read_decimal("plan_max_cost_to_benefit_ratio", "0.05"),
        market_session_check_enabled=_read_bool("market_session_check_enabled", True),
        market_session_close_buffer_seconds=_read_int("market_session_close_buffer_seconds", 120),
        plan_execution_urgency=(read_config_str(_SECTION, "plan_execution_urgency", "fast") or "fast").strip().lower(),
        instrument_cache_ttl_seconds=_read_int("instrument_cache_ttl_seconds", 300),
    )
