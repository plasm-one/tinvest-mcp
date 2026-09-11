from decimal import Decimal

from tinvest_mcp.proposals import ProposalStore


def _make(store, **overrides):
    base = {
        "account_id": "acc",
        "instrument_uid": "uid-1",
        "figi": "F",
        "ticker": "SBER",
        "name": "Sber",
        "instrument_type": "share",
        "currency": "rub",
        "lot": 10,
        "direction": "BUY",
        "order_type": "LIMIT",
        "quantity_lots": 1,
        "limit_price": Decimal("100"),
        "min_price_increment": Decimal("0.01"),
        "rationale": "r",
        "user_request_id": None,
    }
    base.update(overrides)
    return store.create(**base)


def test_proposal_created_ready():
    store = ProposalStore(ttl_seconds=60)
    p = _make(store)
    assert p.status == "READY_FOR_CONFIRMATION"
    assert store.get(p.proposal_id) is p


def test_proposal_expires():
    store = ProposalStore(ttl_seconds=0)
    p = _make(store)
    fetched = store.get(p.proposal_id)
    assert fetched.status == "EXPIRED"
    assert fetched.is_expired


def test_idempotency_key_is_stable():
    store = ProposalStore(ttl_seconds=60)
    p = _make(store)
    k1 = store.ensure_idempotency_key(p.proposal_id)
    k2 = store.ensure_idempotency_key(p.proposal_id)
    assert k1 == k2  # never re-keyed (safe retries)


def test_terminal_state_blocks_reexecution():
    store = ProposalStore(ttl_seconds=60)
    p = _make(store)
    store.set_status(p.proposal_id, "FILLED")
    assert store.get(p.proposal_id).is_terminal


def test_confirmation_token_is_present_but_server_side():
    store = ProposalStore(ttl_seconds=60)
    p = _make(store)
    assert p.confirmation_token and len(p.confirmation_token) >= 16


def test_list_executing_filters_and_orders_newest_first():
    from tinvest_mcp.schemas import EXECUTION_STATUSES

    store = ProposalStore(ttl_seconds=60)
    p1 = _make(store)
    p2 = _make(store)
    p3 = _make(store)
    store.set_status(p1.proposal_id, "SUBMITTED")
    store.set_status(p2.proposal_id, "PARTIALLY_FILLED")
    store.set_status(p3.proposal_id, "READY_FOR_CONFIRMATION")
    executing = store.list_executing()
    assert {p.proposal_id for p in executing} == {p1.proposal_id, p2.proposal_id}
    assert executing[0].created_at >= executing[1].created_at
    assert {"SUBMITTED", "PARTIALLY_FILLED"} <= EXECUTION_STATUSES
