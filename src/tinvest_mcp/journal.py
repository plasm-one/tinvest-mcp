"""Append-only audit journal (JSONL).

Every brokerage action that leaves the process — an order submitted, cancelled,
a plan leg executed — lands here before anything else. The file is the record
you reach for when you need to answer "what did the agent actually do, and
when", so the writer is deliberately boring: one JSON object per line, opened in
append mode, flushed on close, guarded by a process lock.

What is **never** written: API tokens, authorization headers, or an unmasked
account id. :mod:`tinvest_mcp.audit` masks the account and strips token-shaped
keys before calling in here.

Location: ``[tinvest].audit_log`` in ``config.toml`` (default ``audit.jsonl``),
resolved against the state directory — see
:func:`tinvest_mcp.config.sources.state_dir`.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config.sources import read_config_str, resolve_state_path

DEFAULT_JOURNAL_NAME = "audit.jsonl"

_lock = threading.Lock()


class AuditRecord(BaseModel):
    """One audited brokerage action."""

    agent: str = Field(description="Which server wrote the record")
    action: str = Field(description="Operation kind: post_order, cancel_order, execute_plan_step, …")
    order_id: str = Field(default="n/a", description="Broker order id, or n/a when none was issued")
    status: Literal["submitted", "confirmed", "failed"] = Field(default="submitted")
    metadata: dict[str, Any] | None = Field(
        default=None, description="Masked context: account, instrument, lots, price, risk verdict"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def journal_path() -> Path:
    """Absolute path of the audit journal, creating its directory if needed."""

    configured = (
        read_config_str("tinvest", "audit_log", DEFAULT_JOURNAL_NAME, env_key="TINVEST_AUDIT_LOG")
        or DEFAULT_JOURNAL_NAME
    )
    path = resolve_state_path(configured)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def record_event(record: AuditRecord | dict[str, Any]) -> AuditRecord:
    """Append one record to the journal and return the validated model."""

    audit_record = record if isinstance(record, AuditRecord) else AuditRecord(**record)
    payload = audit_record.model_dump()

    with _lock:
        path = journal_path()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")

    return audit_record


def load_events(limit: int | None = None) -> list[AuditRecord]:
    """Read the journal. Unparseable lines are skipped, not fatal."""

    path = journal_path()
    if not path.exists():
        return []

    records: list[AuditRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                records.append(AuditRecord(**json.loads(line)))
            except Exception:
                continue

    if limit is not None and limit > 0:
        return records[-limit:]
    return records


__all__ = ["AuditRecord", "journal_path", "load_events", "record_event"]
