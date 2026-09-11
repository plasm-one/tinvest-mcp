"""Shared pytest fixtures.

The suite is pure unit-level: no token, no network, no broker. Every adapter is
faked, so `pytest` is safe to run on a machine that has production tokens in its
environment — but the fixtures below still pin the writable paths away from real
files, because "safe by construction" is worth more than "safe if you remember".
"""

import sys
from pathlib import Path

# Allow `pytest` straight from a checkout, without `pip install -e .` first.
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402  (import path must be set up first)


@pytest.fixture(autouse=True, scope="session")
def audit_log_path(tmp_path_factory):
    """Keep test events out of the real audit journal.

    `tinvest_mcp.audit.audit_event` appends to the journal resolved by
    `tinvest_mcp.journal.journal_path()` — a genuine audit trail of real orders.
    Test orders used to interleave with real ones there and had to be filtered
    out by hand during incident forensics.
    """
    from tinvest_mcp import journal

    log_path = tmp_path_factory.mktemp("audit") / "audit.jsonl"
    with pytest.MonkeyPatch.context() as mp:
        # Env override wins in read_config_str, and is inherited by subprocesses.
        mp.setenv("TINVEST_AUDIT_LOG", str(log_path))
        mp.setenv("TINVEST_MCP_STATE_DIR", str(log_path.parent))
        # Belt and braces: pin the resolver too, so neither a config.toml change
        # nor a different cwd can steer writes back at the real file.
        mp.setattr(journal, "journal_path", lambda: log_path)
        yield log_path


@pytest.fixture(autouse=True)
def _reset_instrument_cache():
    """Instrument reference data is cached process-wide; different fake adapters
    reuse the same uids, so a leaked entry would silently cross-contaminate tests."""
    from tinvest_mcp.services import _instrument_cache

    _instrument_cache.clear()
    yield
    _instrument_cache.clear()
