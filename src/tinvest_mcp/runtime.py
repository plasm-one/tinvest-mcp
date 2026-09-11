"""Server runtime: logging, transport selection, health tool.

Transport comes from ``[server]`` in ``config.toml`` with environment overrides.
The default is **stdio** — the client spawns the server as a child process and
talks to it over pipes, so nothing is listening on a port and no local process
can reach the broker through this server. Choose ``http`` only when you actually
need a network-reachable server, and read the HTTP section of
``docs/security.md`` first: FastMCP's HTTP transport has no authentication of
its own, so an open port is an unauthenticated route to the broker token.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP

from .config.sources import read_config_int, read_config_str

HTTP_TRANSPORTS: set[str] = {"http", "streamable-http", "sse"}

DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8003

_SECTION = "server"

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def setup_logging(level: str | None = None) -> Any:
    """Configure logging to **stderr**.

    stdout is the MCP message channel on the stdio transport — a stray log line
    written there corrupts the protocol stream, so every sink here is stderr.
    """

    resolved = (
        level or read_config_str(_SECTION, "log_level", "INFO", env_key="TINVEST_MCP_LOG_LEVEL") or "INFO"
    ).upper()

    try:
        from loguru import logger as loguru_logger
    except ImportError:
        loguru_logger = None

    if loguru_logger is not None:
        loguru_logger.remove()
        loguru_logger.add(
            sys.stderr,
            level=resolved,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
        )
        return loguru_logger

    logging.basicConfig(
        level=getattr(logging, resolved, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stderr,
    )
    return logging.getLogger("tinvest-mcp")


@dataclass(frozen=True)
class RuntimeConfig:
    transport: str
    host: str
    port: int
    log_level: str | None = None

    @property
    def is_http_like(self) -> bool:
        return self.transport in HTTP_TRANSPORTS

    @property
    def is_loopback_only(self) -> bool:
        return self.host in LOOPBACK_HOSTS


def get_runtime_config() -> RuntimeConfig:
    """Resolve transport settings from ``[server]`` plus env overrides."""

    transport = (
        (
            read_config_str(_SECTION, "transport", DEFAULT_TRANSPORT, env_key="TINVEST_MCP_TRANSPORT")
            or DEFAULT_TRANSPORT
        )
        .strip()
        .lower()
    )

    host = (read_config_str(_SECTION, "host", DEFAULT_HOST, env_key="TINVEST_MCP_HOST") or DEFAULT_HOST).strip()

    port = read_config_int(_SECTION, "port", DEFAULT_PORT, env_key="TINVEST_MCP_PORT")
    log_level = read_config_str(_SECTION, "log_level", env_key="TINVEST_MCP_LOG_LEVEL")

    return RuntimeConfig(transport=transport, host=host, port=port, log_level=log_level)


def register_health_tool(
    server: FastMCP,
    *,
    name: str = "status",
    description: str = "Health check for the MCP server.",
    message: str = "ok",
    tags: Iterable[str] | None = None,
    extra: Callable[[], Mapping[str, Any]] | None = None,
) -> Callable[[], Mapping[str, Any]]:
    """Register a liveness tool that also reports the active mode."""

    tool_tags = set(tags) if tags else {"utilities", "health"}

    @server.tool(name=name, description=description, tags=tool_tags)
    def _health() -> Mapping[str, Any]:
        payload: dict[str, Any] = {"status": "ok", "message": message}
        if extra:
            try:
                payload.update(extra())
            except Exception as exc:  # noqa: BLE001 - health must never raise
                payload["error"] = f"health extra callback failed: {type(exc).__name__}"
        return payload

    return _health


def run_mcp_server(server: FastMCP, *, show_banner: bool = True) -> None:
    """Start the MCP server on the configured transport."""

    config = get_runtime_config()
    setup_logging(config.log_level)

    print(
        f"[{server.name}] transport={config.transport} host={config.host} "
        f"port={config.port} log_level={config.log_level or 'INFO'}",
        file=sys.stderr,
    )

    if config.is_http_like:
        if not config.is_loopback_only:
            print(
                f"[{server.name}] WARNING: listening on {config.host} — the HTTP transport has no "
                "authentication. Anyone who can reach this port can trade through your broker "
                "token. Bind 127.0.0.1 and use a reverse proxy with auth, or switch to stdio. "
                "See docs/security.md.",
                file=sys.stderr,
            )
        server.run(
            transport=config.transport,
            host=config.host,
            port=config.port,
            log_level=config.log_level,
            show_banner=show_banner,
        )
        return

    if hasattr(server, "run_stdio"):
        server.run_stdio(show_banner=show_banner, log_level=config.log_level)
    else:  # pragma: no cover - depends on the installed FastMCP version
        server.run(transport=config.transport, show_banner=show_banner, log_level=config.log_level)


def _unused_env_note() -> str:  # pragma: no cover - documentation helper
    """Names accepted for compatibility with the multi-agent parent repo."""

    return ", ".join(sorted({"MCP_TRANSPORT", "MCP_HTTP_HOST", "MCP_HTTP_PORT", "MCP_LOG_LEVEL"}))


# Accept the parent repo's generic MCP_* names as aliases so an existing
# deployment keeps working after switching to this standalone package.
for _alias, _canonical in (
    ("MCP_TRANSPORT", "TINVEST_MCP_TRANSPORT"),
    ("MCP_HTTP_HOST", "TINVEST_MCP_HOST"),
    ("MCP_HTTP_PORT", "TINVEST_MCP_PORT"),
    ("MCP_LOG_LEVEL", "TINVEST_MCP_LOG_LEVEL"),
):
    if os.environ.get(_alias) and not os.environ.get(_canonical):
        os.environ[_canonical] = os.environ[_alias]


__all__ = [
    "RuntimeConfig",
    "get_runtime_config",
    "register_health_tool",
    "run_mcp_server",
    "setup_logging",
]
