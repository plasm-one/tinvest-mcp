# syntax=docker/dockerfile:1.7
#
# Container image for tinvest-mcp.
#
# NOTE: a container implies the HTTP transport, and FastMCP's HTTP transport has
# NO authentication — anyone who can reach the port can trade with your token.
# The bundled docker-compose.yml therefore publishes to 127.0.0.1 only. Read
# docs/security.md § Transport security before changing that.
#
# For desktop use, prefer running the server directly over stdio: no port, no
# listener, nothing to reach.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=300

# Dependency layer: cached until pyproject/lockfile change.
COPY pyproject.toml uv.lock* README.md ./
COPY vendor ./vendor
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project

# Application layer.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev


FROM python:3.13-slim-bookworm

# Run as a non-root user: the process needs no privileges, and the token it
# holds is reason enough not to hand it root.
RUN useradd --create-home --uid 10001 tinvest

WORKDIR /app

COPY --from=builder --chown=tinvest:tinvest /app/.venv /app/.venv
COPY --from=builder --chown=tinvest:tinvest /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TINVEST_MCP_TRANSPORT=http \
    TINVEST_MCP_HOST=0.0.0.0 \
    TINVEST_MCP_PORT=8003 \
    TINVEST_MCP_CONFIG=/config/config.toml \
    TINVEST_MCP_STATE_DIR=/state

# 0.0.0.0 inside the container is correct — the container network namespace is
# the boundary. What matters is that the host publishes to loopback only, which
# docker-compose.yml does.

RUN mkdir -p /state /config && chown tinvest:tinvest /state /config

USER tinvest

EXPOSE 8003

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import socket,sys; s=socket.socket(); s.settimeout(3); s.connect(('127.0.0.1',8003)); s.close()" || exit 1

CMD ["tinvest-mcp"]
