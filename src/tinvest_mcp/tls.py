"""gRPC TLS root configuration for T-Invest.

T-Bank's API endpoints present a certificate chain issued by the Russian national
CA ("Russian Trusted Root/Sub CA", Минцифры), which is NOT in certifi nor in
grpc's own bundled roots. gRPC therefore rejects the handshake with
``CERTIFICATE_VERIFY_FAILED: self signed certificate in certificate chain``.

We fix this by pointing gRPC at a combined CA bundle (public roots + the Russian
roots) via ``GRPC_DEFAULT_SSL_ROOTS_FILE_PATH``. grpc reads that env var once, at
the first secure-channel creation, so :func:`ensure_grpc_roots` must run before
any ``Client`` is constructed (it is called at import time of the adapter).

Overrides (respected, in order):
  - ``GRPC_DEFAULT_SSL_ROOTS_FILE_PATH`` already set → left untouched.
  - ``config.toml`` → ``[tinvest].ca_bundle`` (must already include the needed roots).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .config.sources import read_config_str

_GRPC_ROOTS_ENV = "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"
_RUSSIAN_CA = Path(__file__).resolve().parent / "certs" / "russian_trusted_ca.pem"
_CACHE_FILE = Path(tempfile.gettempdir()) / "tinvest_grpc_roots.pem"

_configured = False


def _build_combined_bundle() -> Path:
    """Write (once) a bundle = public roots (certifi, if available) + Russian roots."""
    parts: list[str] = []
    try:
        import certifi  # type: ignore

        parts.append(Path(certifi.where()).read_text(encoding="utf-8"))
    except Exception:
        pass  # Russian roots alone are enough for the T-Bank chain.
    if _RUSSIAN_CA.exists():
        parts.append(_RUSSIAN_CA.read_text(encoding="utf-8"))
    combined = "\n".join(parts)
    try:
        # Only rewrite when content changed to avoid needless disk churn.
        if not _CACHE_FILE.exists() or _CACHE_FILE.read_text(encoding="utf-8") != combined:
            _CACHE_FILE.write_text(combined, encoding="utf-8")
    except Exception:
        # If the temp dir is read-only, fall back to the packaged file directly.
        return _RUSSIAN_CA
    return _CACHE_FILE


def ensure_grpc_roots() -> None:
    """Make grpc trust the Russian CA. Idempotent; safe to call repeatedly."""
    global _configured
    if _configured:
        return
    _configured = True

    if os.environ.get(_GRPC_ROOTS_ENV):
        return  # operator/deployment already configured roots explicitly

    override = read_config_str("tinvest", "ca_bundle")
    if override and Path(override).exists():
        os.environ[_GRPC_ROOTS_ENV] = override
        return

    bundle = _build_combined_bundle()
    os.environ[_GRPC_ROOTS_ENV] = str(bundle)
