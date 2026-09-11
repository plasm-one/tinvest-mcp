"""In-process proposal store with TTL, a status machine and idempotency keys.

Per the de-scoped architecture (reuse existing infra, no Postgres/Redis) the
proposal lifecycle lives in memory in the MCP server process. This is sufficient
for the single-instance MVP and keeps confirmation tokens / idempotency keys off
the client and out of the DB.
"""

from __future__ import annotations

import secrets
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .schemas import EXECUTION_STATUSES, TERMINAL_STATUSES, OrderPreview, ProposalStatus


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class OrderProposal:
    """Everything needed to (re-)validate and execute an order later."""

    proposal_id: str
    account_id: str
    instrument_uid: str
    figi: str | None
    ticker: str | None
    name: str | None
    instrument_type: str
    currency: str
    lot: int
    direction: str  # BUY | SELL
    order_type: str  # LIMIT | MARKET
    quantity_lots: int
    limit_price: Decimal
    min_price_increment: Decimal | None
    rationale: str | None
    user_request_id: str | None
    created_at: datetime
    expires_at: datetime
    status: ProposalStatus = "READY_FOR_CONFIRMATION"
    confirmation_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    idempotency_key: str | None = None
    broker_order_id: str | None = None
    reference_last_price: Decimal | None = None
    estimated_total: Decimal | None = None
    preview: OrderPreview | None = None
    # Set only for a proposal derived from an immutable trade-plan leg. Public
    # post_order(proposal_id) rejects these; execute_plan_step(plan_id) is the
    # only path that may submit them, so the plan gate cannot be bypassed.
    plan_id: str | None = None
    plan_sequence: int | None = None

    @property
    def is_expired(self) -> bool:
        return _now() >= self.expires_at

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class ProposalStore:
    """Thread-safe in-memory store keyed by ``proposal_id``."""

    def __init__(self, ttl_seconds: int = 60) -> None:
        self._ttl = ttl_seconds
        self._items: dict[str, OrderProposal] = {}
        self._lock = threading.Lock()

    def create(self, **kwargs) -> OrderProposal:
        proposal_id = str(uuid.uuid4())
        now = _now()
        proposal = OrderProposal(
            proposal_id=proposal_id,
            created_at=now,
            expires_at=now + timedelta(seconds=self._ttl),
            **kwargs,
        )
        with self._lock:
            self._items[proposal_id] = proposal
        return proposal

    def get(self, proposal_id: str) -> OrderProposal | None:
        with self._lock:
            proposal = self._items.get(proposal_id)
        if proposal is None:
            return None
        # Lazily expire: flip READY proposals to EXPIRED once past TTL.
        if proposal.status == "READY_FOR_CONFIRMATION" and proposal.is_expired:
            proposal.status = "EXPIRED"
        return proposal

    def set_status(self, proposal_id: str, status: ProposalStatus) -> None:
        with self._lock:
            proposal = self._items.get(proposal_id)
            if proposal is not None:
                proposal.status = status

    def ensure_idempotency_key(self, proposal_id: str) -> str:
        """Return the stored key, generating (and persisting) one on first use.

                Critical for safe retries: a network error AFTER ``PostOrder`` must NOT
                create a new key — the same key is reused to query order state
        .
        """
        with self._lock:
            proposal = self._items[proposal_id]
            if proposal.idempotency_key is None:
                proposal.idempotency_key = str(uuid.uuid4())
            return proposal.idempotency_key

    def update(self, proposal_id: str, **changes) -> None:
        with self._lock:
            proposal = self._items.get(proposal_id)
            if proposal is None:
                return
            for key, value in changes.items():
                setattr(proposal, key, value)

    def list_executing(self) -> list[OrderProposal]:
        """Return proposals whose order is in flight at the broker (newest first)."""
        with self._lock:
            items = list(self._items.values())
        executing: list[OrderProposal] = []
        for proposal in items:
            if proposal.status == "READY_FOR_CONFIRMATION" and proposal.is_expired:
                proposal.status = "EXPIRED"
            if proposal.status in EXECUTION_STATUSES:
                executing.append(proposal)
        executing.sort(key=lambda p: p.created_at, reverse=True)
        return executing


# Module-level singleton, configured at server startup.
_store: ProposalStore | None = None


def get_store(ttl_seconds: int = 60) -> ProposalStore:
    global _store
    if _store is None:
        _store = ProposalStore(ttl_seconds=ttl_seconds)
    return _store
