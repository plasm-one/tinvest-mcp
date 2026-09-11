"""File-backed persistence for the confirmed investment profile.

Single-profile store (matches the single-account MVP): one JSON document at
``settings.investment_profile_path``. Writes are atomic (temp file +
``os.replace``) and guarded by a process-wide lock, mirroring the JSONL
journal approach in :mod:`tinvest_mcp.journal` — no DB needed.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path

from .config.sources import resolve_state_path
from .schemas import InvestmentProfile

_lock = threading.Lock()


def _resolve(path_str: str) -> Path:
    """A relative path is anchored to the state directory, never to the cwd:
    an MCP client picks the working directory arbitrarily, so anchoring there
    would scatter one profile across several files."""
    return resolve_state_path(path_str)


def load_profile(path_str: str) -> InvestmentProfile | None:
    """Return the saved profile, or ``None`` if absent or unreadable."""
    path = _resolve(path_str)
    with _lock:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return InvestmentProfile.model_validate(data)
        except Exception:
            # Corrupt/stale file: treat as "no profile" rather than breaking reads.
            return None


def save_profile(path_str: str, profile: InvestmentProfile) -> None:
    """Atomically overwrite the stored profile."""
    path = _resolve(path_str)
    payload = profile.model_dump_json(indent=2)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_name, path)
        except BaseException:
            # Best-effort cleanup: the temp file may already be gone.
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
