"""Typed exceptions for the T-Invest integration."""

from __future__ import annotations

from typing import Any


class TInvestError(Exception):
    """Base error. Messages must never contain tokens or auth headers."""


class TInvestConfigurationError(TInvestError): ...


class TInvestAuthenticationError(TInvestError): ...


class TInvestPermissionError(TInvestError): ...


class TInvestAccountNotFoundError(TInvestError): ...


class TInvestInstrumentNotFoundError(TInvestError): ...


class TInvestRateLimitError(TInvestError): ...


class TInvestDataUnavailableError(TInvestError):
    """Required read-only market data stayed unavailable after fallback retries."""


class TInvestOrderRejectedError(TInvestError): ...


class TInvestRealTradingDisabledError(TInvestError): ...


class TInvestProposalError(TInvestError): ...


class TInvestUnknownExecutionStateError(TInvestError): ...


# gRPC codes for which the broker DEFINITIVELY rejected the request before any
# order was created — safe to mark REJECTED and retry with a fresh preview.
# Everything else (UNAVAILABLE, DEADLINE_EXCEEDED, INTERNAL, UNKNOWN, plain
# network faults) means the outcome is genuinely unknown → reconcile first.
BROKER_REJECT_GRPC_CODES = frozenset(
    {
        "INVALID_ARGUMENT",  # e.g. 30099 price outside instrument limits
        "FAILED_PRECONDITION",
        "OUT_OF_RANGE",
        "NOT_FOUND",
        "ALREADY_EXISTS",
        "PERMISSION_DENIED",
        "UNAUTHENTICATED",
        "RESOURCE_EXHAUSTED",  # rate limit — request never processed
        "UNIMPLEMENTED",
    }
)


def broker_error_meta(exc: BaseException) -> dict[str, Any]:
    """Extract every diagnostic field the T-Invest SDK attaches to an RPC error.

    ``RequestError(code, details, metadata)`` carries the gRPC status, the broker
    API error code in ``details`` (e.g. ``"30099"``) and a ``Metadata`` namedtuple
    with ``tracking_id`` / ``ratelimit_*`` / human-readable ``message``. Losing
    these made failures undiagnosable (a bare ``RequestError`` in the audit log);
    always persist the full picture. Works on any exception — unknown shapes
    degrade to just the type name. Never includes tokens.
    """
    meta: dict[str, Any] = {"error_type": type(exc).__name__}
    code = getattr(exc, "code", None)
    if code is not None:
        meta["grpc_code"] = getattr(code, "name", None) or str(code)
    details = getattr(exc, "details", None)
    if details is not None and not callable(details):
        meta["broker_code"] = str(details)
    md = getattr(exc, "metadata", None)
    if md is not None:
        for field in ("tracking_id", "message", "ratelimit_limit", "ratelimit_remaining", "ratelimit_reset"):
            value = getattr(md, field, None)
            if value not in (None, ""):
                meta["broker_message" if field == "message" else field] = value
    return meta


def is_definitive_broker_reject(exc: BaseException) -> bool:
    """True when the RPC failed with a code that guarantees no order was created."""
    code = getattr(exc, "code", None)
    name = getattr(code, "name", None) or (str(code) if code is not None else None)
    return name in BROKER_REJECT_GRPC_CODES


def format_broker_error(meta: dict[str, Any]) -> str:
    """One-line human summary: ``INVALID_ARGUMENT 30099: Цена вне лимитов… [tracking_id=…]``."""
    parts = [str(meta.get("grpc_code") or meta.get("error_type"))]
    if meta.get("broker_code"):
        parts.append(str(meta["broker_code"]))
    head = " ".join(parts)
    if meta.get("broker_message"):
        head += f": {meta['broker_message']}"
    if meta.get("tracking_id"):
        head += f" [tracking_id={meta['tracking_id']}]"
    return head
