"""tinvest-mcp — an unofficial MCP server for the T-Bank Invest (T-Invest) API.

AI wealth management infrastructure: the model reasons over the portfolio, and
every limit that must hold is enforced in code rather than in a prompt.

Not affiliated with, endorsed by, or supported by T-Bank / Т-Банк.
See DISCLAIMER.md. Built by the Plasm team — https://plasm.one
Vision: https://plasm.one/future-of-finance

The public entry point is :func:`tinvest_mcp.server.main`, exposed as the
``tinvest-mcp`` console script.
"""

from __future__ import annotations

__version__ = "0.1.0"
__author__ = "Plasm"
__license__ = "MIT"
__url__ = "https://plasm.one"

__all__ = ["__author__", "__license__", "__url__", "__version__"]
