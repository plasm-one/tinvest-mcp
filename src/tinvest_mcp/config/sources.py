"""Configuration sources: ``config.toml`` for settings, ``.env`` for secrets.

Two separate stores on purpose (see ``docs/security.md``):

* **``config.toml``** — non-secret operational settings (mode, risk limits,
  transport). Safe to commit, safe to share in a bug report.
* **``.env``** — API tokens only. Never committed, never logged.

Both are *discovered* rather than assumed, because an MCP server started by a
client (Claude Code, Cursor, VS Code) inherits whatever working directory that
client happened to have. Relying on ``./config.toml`` alone is the single most
common reason a stdio MCP server silently comes up with default settings, so the
search walks up from the working directory and then falls back to the per-user
config directory:

1. ``$TINVEST_MCP_CONFIG`` / ``$TINVEST_MCP_ENV_FILE`` — explicit path, wins.
2. ``./config.toml`` / ``./.env`` in the working directory.
3. the nearest such file in any parent directory.
4. ``~/.config/tinvest-mcp/config.toml`` / ``~/.config/tinvest-mcp/.env``.

Precedence for an individual value is always: environment variable, then the
file, then the in-code default.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

APP_NAME = "tinvest-mcp"

CONFIG_FILENAME = "config.toml"
ENV_FILENAME = ".env"

CONFIG_PATH_ENV = "TINVEST_MCP_CONFIG"
ENV_FILE_ENV = "TINVEST_MCP_ENV_FILE"
STATE_DIR_ENV = "TINVEST_MCP_STATE_DIR"

# Bounded walk: deep enough to escape a src/ or tests/ nesting, shallow enough
# that a stray config.toml near the filesystem root is never picked up.
_MAX_PARENT_LEVELS = 6


def user_config_dir() -> Path:
    """Per-user configuration directory (XDG on Linux, honoured on macOS too)."""

    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / APP_NAME


def user_state_dir() -> Path:
    """Per-user directory for the audit journal and the investment profile."""

    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / APP_NAME


def _walk_up(filename: str) -> Path | None:
    here = Path.cwd().resolve()
    for parent in [here, *list(here.parents)[:_MAX_PARENT_LEVELS]]:
        candidate = parent / filename
        if candidate.is_file():
            return candidate
    return None


def _discover(filename: str, explicit_env_key: str) -> Path | None:
    explicit = (os.environ.get(explicit_env_key) or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        # An explicit path that does not exist is a configuration mistake worth
        # surfacing, not something to silently paper over with a fallback.
        if not path.is_file():
            raise FileNotFoundError(f"{explicit_env_key}={explicit} does not point at a readable file")
        return path

    found = _walk_up(filename)
    if found:
        return found

    fallback = user_config_dir() / filename
    return fallback if fallback.is_file() else None


@lru_cache(maxsize=1)
def config_path() -> Path | None:
    """Path of the active ``config.toml``, or ``None`` when running on defaults."""

    return _discover(CONFIG_FILENAME, CONFIG_PATH_ENV)


@lru_cache(maxsize=1)
def env_path() -> Path | None:
    """Path of the active ``.env``, or ``None`` when tokens come from the real environment."""

    return _discover(ENV_FILENAME, ENV_FILE_ENV)


def state_dir() -> Path:
    """Directory for files the server writes: audit journal, investment profile.

    Resolved once per call (no caching) so tests can redirect it. A relative
    ``audit_log`` / ``investment_profile_path`` is interpreted against this, not
    against the working directory, which an MCP client chooses arbitrarily.
    """

    explicit = (os.environ.get(STATE_DIR_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()

    cfg = config_path()
    if cfg is not None:
        return cfg.parent

    return user_state_dir()


def resolve_state_path(path_str: str) -> Path:
    """Absolute path for a configured state file (see :func:`state_dir`)."""

    path = Path(path_str).expanduser()
    return path if path.is_absolute() else state_dir() / path


# --- config.toml ----------------------------------------------------------


@lru_cache(maxsize=1)
def _load_toml() -> dict[str, Any]:
    path = config_path()
    if path is None:
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def reload_config() -> None:
    """Drop every cached lookup. For tests and for config-reload tooling."""

    _load_toml.cache_clear()
    config_path.cache_clear()
    env_path.cache_clear()
    _reset_env_loaded()


def _resolve_section(section: str) -> dict[str, Any]:
    node: Any = _load_toml()
    for part in section.split("."):
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def _lookup(section: str, key: str) -> Any:
    return _resolve_section(section).get(key)


def _env_override(env_key: str | None) -> str | None:
    if not env_key:
        return None
    value = os.getenv(env_key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def read_config_str(
    section: str,
    key: str,
    default: str | None = None,
    *,
    env_key: str | None = None,
) -> str | None:
    """Read a string: env override, then TOML, then default."""

    env_val = _env_override(env_key)
    if env_val is not None:
        return env_val

    raw = _lookup(section, key)
    if raw is None:
        return default
    if isinstance(raw, str):
        stripped = raw.strip()
        return stripped if stripped else default
    return str(raw)


def read_config_bool(
    section: str,
    key: str,
    default: bool = False,
    *,
    env_key: str | None = None,
) -> bool:
    env_val = _env_override(env_key)
    if env_val is not None:
        return env_val.lower() in {"1", "true", "yes", "on"}

    raw = _lookup(section, key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return default


def read_config_int(
    section: str,
    key: str,
    default: int,
    *,
    env_key: str | None = None,
) -> int:
    env_val = _env_override(env_key)
    if env_val is not None:
        try:
            return int(env_val)
        except ValueError:
            return default

    raw = _lookup(section, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def read_config_float(
    section: str,
    key: str,
    default: float,
    *,
    env_key: str | None = None,
) -> float:
    env_val = _env_override(env_key)
    if env_val is not None:
        try:
            return float(env_val)
        except ValueError:
            return default

    raw = _lookup(section, key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def read_config_list(
    section: str,
    key: str,
    default: list[str] | None = None,
    *,
    env_key: str | None = None,
) -> list[str]:
    env_val = _env_override(env_key)
    if env_val is not None:
        return [item.strip() for item in env_val.split(",") if item.strip()]

    raw = _lookup(section, key)
    if raw is None:
        return list(default or [])
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return list(default or [])


# --- .env (secrets) -------------------------------------------------------

_ENV_LOADED = False


def _reset_env_loaded() -> None:
    global _ENV_LOADED
    _ENV_LOADED = False


def load_secrets_env() -> None:
    """Load the ``.env`` file once, without overriding the real environment.

    ``override=False`` is deliberate: a token exported by the operator (or
    injected by a secret manager) must win over a stale file on disk.
    """

    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True

    path = env_path()
    if path is not None:
        load_dotenv(path, override=False)


def read_env_str(name: str, default: str | None = None) -> str | None:
    """Read one secret from the environment / ``.env``, trimmed."""

    load_secrets_env()
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


__all__ = [
    "APP_NAME",
    "CONFIG_PATH_ENV",
    "ENV_FILE_ENV",
    "STATE_DIR_ENV",
    "config_path",
    "env_path",
    "load_secrets_env",
    "read_config_bool",
    "read_config_float",
    "read_config_int",
    "read_config_list",
    "read_config_str",
    "read_env_str",
    "reload_config",
    "resolve_state_path",
    "state_dir",
    "user_config_dir",
    "user_state_dir",
]
