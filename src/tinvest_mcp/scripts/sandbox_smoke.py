"""Sandbox smoke test.

Runs the full read -> propose -> confirm -> execute loop against the T-Invest
SANDBOX using virtual money. Requires ``TINVEST_SANDBOX_TOKEN`` in the env.

    python -m tinvest_mcp.scripts.sandbox_smoke

Expected tail:
    No real trading performed
"""

from __future__ import annotations

import sys
from decimal import Decimal

from .. import services
from ..adapter import TInvestAdapter
from ..config.env import get_settings


def _line(label: str, value: str = "OK") -> None:
    print(f"{label}: {value}")


def main() -> int:
    settings = get_settings()
    if not settings.is_sandbox:
        print("Refusing to run: [tinvest].mode in config.toml must be 'sandbox'.")
        return 2
    if not settings.sandbox_token:
        print("Skipping live smoke: set TINVEST_SANDBOX_TOKEN to run.")
        return 0

    adapter = TInvestAdapter(settings)

    _line("SDK connection")

    # 1. Account: reuse configured one or open a fresh sandbox account.
    accounts = adapter.get_accounts()
    if settings.account_id and any(a.id == settings.account_id for a in accounts):
        account_id = settings.account_id
        _line("Sandbox account", f"found {account_id[:6]}***")
    elif accounts:
        account_id = accounts[0].id
        _line("Sandbox account", f"existing {account_id[:6]}***")
    else:
        account_id = adapter.open_sandbox_account("ai-treasury-smoke")
        _line("Sandbox account", f"created {account_id[:6]}***")

    # 2. Fund it.
    adapter.sandbox_pay_in(account_id, Decimal("100000"), "rub")
    _line("Sandbox pay-in")

    # Point the resolver at this account for the rest of the flow.
    object.__setattr__(settings, "account_id", account_id)

    # 3. Portfolio.
    portfolio = services.get_portfolio_summary(adapter, settings)
    _line("Portfolio", f"total={portfolio.total_value} cash={portfolio.cash}")

    # 4. Instrument lookup (a liquid blue chip).
    hits = services.search_instruments(adapter, "SBER", instrument_types=["share"], limit=5)
    if not hits:
        print("Instrument lookup: no results")
        return 1
    instrument = next((h for h in hits if h.ticker.upper() == "SBER"), hits[0])
    _line("Instrument lookup", f"{instrument.ticker} {instrument.uid[:6]}***")

    snapshot = services.get_market_snapshot(adapter, settings, instrument.uid)
    if not snapshot.last_price:
        print("Order preview: no market price available (market closed?) — stopping before order.")
        print("No real trading performed")
        return 0

    # 5. Order preview — fast urgency crosses the spread for quicker sandbox fill.
    preview = services.create_order_proposal(
        adapter,
        settings,
        instrument_uid=instrument.uid,
        direction="BUY",
        order_type="LIMIT",
        quantity_lots=1,
        urgency="fast",
        rationale="sandbox smoke",
    )
    _line("Order preview", f"{preview.status} all_passed={preview.all_passed}")

    if preview.all_passed:
        result = services.post_order(adapter, settings, preview.proposal_id)
        _line("Post order", result.status)
        state = services.get_order_state(adapter, settings, preview.proposal_id)
        _line("Order state", state.status)
        if state.status in {"SUBMITTED", "PARTIALLY_FILLED", "NEW"}:
            cancelled = services.cancel_order(adapter, settings, preview.proposal_id)
            _line("Cancel order", cancelled.status)
    else:
        failed = [c.code for c in preview.risk_checks if not c.passed]
        _line("Order preview", f"risk-rejected: {failed}")

    print("No real trading performed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
