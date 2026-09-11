"""Startup security checks.

Advisory by design: the server still starts so read tools work, but every
finding is logged and surfaced via the ``status`` tool. The hard gates that
actually prevent unsafe trading live at execution time (``post_order``
re-checks the real-trading flag; ``resolve_account_id`` enforces isolation).
"""

from __future__ import annotations

from dataclasses import dataclass

from .adapter import TInvestAdapter
from .config.env import Settings, get_settings


@dataclass
class CheckResult:
    name: str
    ok: bool
    message: str


def run_startup_checks(settings: Settings | None = None) -> list[CheckResult]:
    settings = settings or get_settings()
    results: list[CheckResult] = []

    def add(name: str, ok: bool, message: str) -> None:
        results.append(CheckResult(name=name, ok=ok, message=message))

    # 1. Real trading must be off unless explicitly enabled in prod.
    add(
        "real_trading_default_off",
        not settings.real_trading_enabled or settings.is_prod,
        f"real_trading_enabled={settings.enable_real_trading} mode={settings.mode}",
    )

    # 2. Token presence for the active mode.
    if settings.is_sandbox:
        add("token_present", bool(settings.sandbox_token), "sandbox token configured")
    else:
        add("token_present", bool(settings.active_read_token()), "prod read token configured")
        add(
            "trade_token_present_if_real",
            (not settings.real_trading_enabled) or bool(settings.fullaccess_token),
            "full-access token required when real trading is enabled",
        )

    # 3. read-only token must differ from full-access token.
    if settings.readonly_token and settings.fullaccess_token:
        add(
            "tokens_distinct",
            settings.readonly_token != settings.fullaccess_token,
            "read-only and full-access tokens must differ",
        )

    # 4. Limits configured.
    add(
        "limits_configured",
        settings.max_order_rub > 0 and settings.max_daily_turnover_rub > 0,
        f"max_order_rub={settings.max_order_rub} max_daily={settings.max_daily_turnover_rub}",
    )

    # 5. Account isolation — only probed in prod (avoids needless sandbox calls).
    if settings.is_prod and settings.active_read_token():
        try:
            adapter = TInvestAdapter(settings)
            accounts = adapter.get_accounts()
            ids = [a.id for a in accounts]
            single_ok = (not settings.require_single_account_token) or len(ids) <= 1
            add("single_account_isolation", single_ok, f"token sees {len(ids)} account(s)")
            if settings.account_id:
                add(
                    "configured_account_visible",
                    settings.account_id in ids,
                    "configured account present in GetAccounts",
                )

            # Presence of a secret is not proof that it can trade this account.
            # Probe GetAccounts through the trade token (a non-mutating API call)
            # and verify both scope and permission before advertising execution.
            if settings.real_trading_enabled and settings.fullaccess_token:
                trade_accounts = adapter.get_trade_accounts()
                trade_ids = [a.id for a in trade_accounts]
                trade_levels = [getattr(getattr(a, "access_level", None), "name", "") or "" for a in trade_accounts]
                add(
                    "trade_token_full_access",
                    bool(trade_accounts) and all("FULL_ACCESS" in level.upper() for level in trade_levels),
                    f"trade token reports full access for {len(trade_accounts)} account(s)",
                )
                add(
                    "read_trade_account_match",
                    set(ids) == set(trade_ids),
                    "read and trade tokens must expose the same account set",
                )
        except Exception as exc:  # pragma: no cover - network dependent
            add("accounts_reachable", False, f"GetAccounts failed: {type(exc).__name__}")

    return results


def startup_summary() -> dict:
    settings = get_settings()
    results = run_startup_checks(settings)
    return {
        "mode": settings.mode,
        "real_trading_enabled": settings.real_trading_enabled,
        "checks": [{"name": r.name, "ok": r.ok, "message": r.message} for r in results],
        "all_ok": all(r.ok for r in results),
    }
