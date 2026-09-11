"""Audit trail for brokerage actions.

Writes through to the append-only JSONL journal in :mod:`tinvest_mcp.journal`.
Tokens and authorization headers are NEVER written and the account id is always
masked, so the journal can be attached to a bug report as-is.

Auditing must never break execution: a failure to write is swallowed. The order
already reached the broker by then, and losing the log line is strictly better
than raising over a completed trade.
"""

from __future__ import annotations

import contextlib
from typing import Any

from .config.env import mask
from .journal import record_event

AGENT = "tinvest-mcp"

_VALID_STATUSES = {"submitted", "confirmed", "failed"}


def audit_event(
    action: str,
    *,
    order_id: str = "",
    status: str = "submitted",
    account_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append one audit record. Never raises into the caller's flow."""

    payload: dict[str, Any] = dict(metadata or {})
    if account_id is not None:
        payload["account_id"] = mask(account_id)
    # Defensive: strip anything token-shaped that a caller may have passed in.
    for key in list(payload.keys()):
        if "token" in key.lower() or "authorization" in key.lower():
            payload.pop(key, None)

    # Audit must never break execution: by the time we write, the order has
    # already reached the broker, so losing a log line beats raising over a
    # completed trade.
    with contextlib.suppress(Exception):  # pragma: no cover
        record_event(
            {
                "agent": AGENT,
                "action": action,
                "order_id": order_id or "n/a",
                "status": status if status in _VALID_STATUSES else "submitted",
                "metadata": payload,
            }
        )


__all__ = ["AGENT", "audit_event"]
