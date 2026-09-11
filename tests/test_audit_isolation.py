"""Guards that the test suite never appends to the production audit trail."""

import json
from pathlib import Path

from tinvest_mcp import journal
from tinvest_mcp.audit import audit_event


def test_audit_event_writes_to_the_redirected_sink(audit_log_path):
    audit_event(
        "post_order",
        order_id="test-order-1",
        status="submitted",
        account_id="2000123456",
        metadata={"ticker": "SBER"},
    )

    lines = audit_log_path.read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[-1])
    assert record["agent"] == "tinvest-mcp"
    assert record["order_id"] == "test-order-1"
    assert record["metadata"]["ticker"] == "SBER"


def test_account_id_is_masked_and_tokens_are_stripped(audit_log_path):
    audit_event(
        "post_order",
        order_id="test-order-2",
        account_id="2000123456",
        metadata={"ticker": "SBER", "api_token": "t.secret", "Authorization": "Bearer x"},
    )

    record = json.loads(audit_log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["metadata"]["account_id"] == "200***456"
    assert "2000123456" not in json.dumps(record)
    assert "api_token" not in record["metadata"]
    assert "Authorization" not in record["metadata"]
    assert "t.secret" not in json.dumps(record)


def test_resolved_log_path_is_never_the_real_one(audit_log_path):
    resolved = journal.journal_path()
    assert resolved == audit_log_path
    assert resolved != Path.cwd() / "audit.jsonl"
