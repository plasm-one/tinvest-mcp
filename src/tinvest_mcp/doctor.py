"""Pre-flight diagnostics: ``tinvest-mcp-doctor``.

Answers the two questions that account for most setup failures, before a client
is ever wired up:

* **Which files did the server actually find?** An MCP client starts the server
  with an arbitrary working directory, so a ``config.toml`` sitting next to the
  repo is routinely not the one in effect. This prints the resolved paths.
* **What is armed right now?** Mode, whether real trading can happen, which
  tokens are present (masked), and the startup security checks.

Reads only. Places nothing, opens no port. The one network call is
``GetAccounts`` inside the startup checks, and only in prod mode with a token
present — it is a non-mutating capability probe.
"""

from __future__ import annotations

import sys

from .config.env import PROD, get_settings, mask
from .config.sources import config_path, env_path, state_dir
from .journal import journal_path
from .runtime import get_runtime_config
from .sdk import SDK_PACKAGE
from .startup_checks import run_startup_checks

_OK = "[ok]  "
_WARN = "[warn]"


def _row(label: str, value: str) -> str:
    return f"{label + ':':<22}{value}"


def _token_state(name: str, value: str | None) -> str:
    return f"present ({mask(value)})" if value else "not set"


def _report() -> tuple[list[str], bool]:
    lines: list[str] = []
    settings = get_settings()
    runtime = get_runtime_config()

    lines.append("tinvest-mcp doctor")
    lines.append("=" * 60)
    lines.append("")
    lines.append("Resolved files")
    lines.append("-" * 60)
    cfg = config_path()
    env = env_path()
    lines.append(_row("config.toml", str(cfg) if cfg else "NOT FOUND — running on built-in defaults"))
    lines.append(_row(".env", str(env) if env else "NOT FOUND — tokens must come from the environment"))
    lines.append(_row("state directory", str(state_dir())))
    lines.append(_row("audit journal", str(journal_path())))
    lines.append("")

    lines.append("Mode")
    lines.append("-" * 60)
    lines.append(_row("mode", settings.mode))
    lines.append(
        _row("real trading", "ENABLED — real orders can be placed" if settings.real_trading_enabled else "disabled")
    )
    lines.append(
        _row(
            "transport",
            f"{runtime.transport} ({runtime.host}:{runtime.port})" if runtime.is_http_like else runtime.transport,
        )
    )
    lines.append(_row("SDK package", SDK_PACKAGE))
    lines.append("")

    lines.append("Tokens (masked)")
    lines.append("-" * 60)
    lines.append(_row("sandbox", _token_state("sandbox", settings.sandbox_token)))
    lines.append(_row("read-only (prod)", _token_state("readonly", settings.readonly_token)))
    lines.append(_row("full access (prod)", _token_state("fullaccess", settings.fullaccess_token)))
    lines.append("")

    lines.append("Risk limits in effect")
    lines.append("-" * 60)
    lines.append(_row("max order", f"{settings.max_order_rub} RUB"))
    lines.append(_row("max daily turnover", f"{settings.max_daily_turnover_rub} RUB"))
    lines.append(_row("max position weight", str(settings.max_position_weight)))
    lines.append(_row("max price deviation", f"{settings.max_price_deviation_pct}%"))
    lines.append(_row("confirmation TTL", f"{settings.confirmation_ttl_seconds}s"))
    lines.append(_row("market orders", "allowed" if settings.allow_market_orders else "blocked"))
    lines.append(
        _row(
            "margin / shorts",
            f"{'allowed' if settings.allow_margin else 'blocked'} / {'allowed' if settings.allow_shorts else 'blocked'}",
        )
    )
    lines.append(_row("instrument types", ", ".join(settings.allowed_instrument_types) or "none"))
    lines.append(
        _row(
            "instrument allowlist",
            f"{len(settings.instrument_allowlist)} uid(s) — everything else refused"
            if settings.instrument_allowlist
            else "empty (no allowlist restriction)",
        )
    )
    lines.append("")

    lines.append("Startup security checks")
    lines.append("-" * 60)
    checks = run_startup_checks(settings)
    for result in checks:
        lines.append(f"{_OK if result.ok else _WARN} {result.name}: {result.message}")
    if not checks:
        lines.append("(none applicable)")
    lines.append("")

    advice = _advice(settings)
    if advice:
        lines.append("Notes")
        lines.append("-" * 60)
        lines.extend(advice)
        lines.append("")

    all_ok = all(r.ok for r in checks)
    return lines, all_ok


def _advice(settings) -> list[str]:
    """Configuration facts worth stating out loud, in severity order."""

    notes: list[str] = []

    if config_path() is None:
        notes.append(
            "* No config.toml found. Defaults are safe (sandbox, no real trading) but your "
            "risk limits are the built-in ones. Copy config.toml.example to "
            "~/.config/tinvest-mcp/config.toml, or set TINVEST_MCP_CONFIG."
        )

    if settings.real_trading_enabled:
        notes.append(
            "* REAL TRADING IS ARMED. Real orders can be placed against your account. "
            "Set enable_real_trading = false in config.toml to disarm."
        )
        if settings.max_order_rub > 50_000:
            notes.append(
                f"* max_order_rub is {settings.max_order_rub} with real trading armed. "
                "That is a large single-order cap — check it is not a sandbox value left behind."
            )

    if settings.mode == PROD and not settings.fullaccess_token:
        notes.append(
            "* Production research-only setup: no execution token is configured, so placing "
            "an order is structurally impossible. This is the recommended production mode."
        )

    if settings.readonly_token and settings.fullaccess_token and settings.readonly_token == settings.fullaccess_token:
        notes.append(
            "* The read-only and full-access tokens are the same string. Issue a genuinely "
            "read-only token for research so the privileged one is used only to execute."
        )

    if not settings.active_read_token():
        notes.append(
            f"* No token for mode '{settings.mode}'. Reads will fail. Set "
            f"{'TINVEST_SANDBOX_TOKEN' if settings.is_sandbox else 'TINVEST_READONLY_TOKEN'} in .env."
        )

    return notes


def main() -> int:
    try:
        lines, all_ok = _report()
    except Exception as exc:  # noqa: BLE001 - a diagnostic must report, not traceback
        print(f"doctor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print("\n".join(lines))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
