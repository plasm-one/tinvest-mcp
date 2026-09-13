# Installation

The short path is in the [README](../README.en.md#installation). This document is
the long one: every client, the token in detail, and what to do when something
does not work.

## Contents

- [Requirements](#requirements)
- [Install the package](#install-the-package)
- [Creating an API token](#creating-an-api-token)
- [Where to put the token](#where-to-put-the-token)
- [Where to put config.toml](#where-to-put-configtoml)
- [Verify with the doctor](#verify-with-the-doctor)
- [Connecting a client](#connecting-a-client)
- [Docker](#docker)
- [Troubleshooting](#troubleshooting)

---

## Requirements

| | |
|---|---|
| Python | 3.12 or newer |
| Package manager | [`uv`](https://docs.astral.sh/uv/) recommended, `pip` fine |
| Client | any MCP client that speaks **stdio**: Claude Code, Claude Desktop, Cursor, VS Code agent mode, Gemini CLI, Qwen Code, Zed, … |
| Broker | a T-Bank Invest brokerage account, and an API token (free) |
| Network | outbound TLS to `invest-public-api.tbank.ru` (gRPC over 443) |

No database, no Redis, no background worker. State is two files on disk.

## Install the package

### With uv (recommended)

```bash
git clone https://github.com/plasm-one/tinvest-mcp.git
cd tinvest-mcp
uv sync
```

The executables land in `.venv/bin/`. You will need the absolute path to
`.venv/bin/tinvest-mcp` when configuring a client:

```bash
readlink -f .venv/bin/tinvest-mcp
```

### With pip

```bash
git clone https://github.com/plasm-one/tinvest-mcp.git
cd tinvest-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

### Optional extras

```bash
uv sync --extra logging      # loguru: nicer stderr logs (stdlib logging otherwise)
uv sync --extra debug-api     # FastAPI Swagger UI for manual poking — dev only
uv sync --all-extras          # both
uv sync --all-extras --group dev   # + pytest and ruff
```

---

## Creating an API token

### Where

**T-Bank Invest → Settings → API tokens → Create token**

* Web: <https://www.tbank.ru/invest/settings/api/>
* Official docs: <https://developer.tbank.ru/invest/intro/intro/token>

### Which scope

You pick a scope at creation and it cannot be changed afterwards. This is the
most consequential security decision in the setup.

| Scope | What it allows | Use for |
|---|---|---|
| **Read-only** | portfolio, catalogue, market data, operation history | ✅ **everything this server does except placing orders** |
| **Full access** | the above plus placing and cancelling orders | ⚠️ only if you want the agent to trade for real |
| **Full access + transfers** | the above plus moving money between accounts | ❌ **never** — unused here, and it turns a leaked file into a withdrawal |

> [!IMPORTANT]
> The token string is shown **once**, at creation, and cannot be retrieved
> afterwards. Copy it immediately. If you lose it, revoke it in the same screen
> and issue a new one — revocation is instant and free.

### How many

**Two, for a production setup:**

1. A **read-only** token → `TINVEST_READONLY_TOKEN`. Every read goes through it.
2. A **full-access** token → `TINVEST_FULLACCESS_TOKEN`. Only needed if you
   intend to execute. Leave it out of `.env` until that day.

They must be different strings; `tinvest-mcp-doctor` and the startup checks
both flag it when they match. The point of the split is that the privileged
credential is used for exactly one operation instead of for hundreds of reads.

**One, for sandbox:** `TINVEST_SANDBOX_TOKEN`. A read-only scope is enough —
sandbox trading works with it, because sandbox orders are virtual.

### A token is a bearer credential

No password, no second factor, no device binding. Whoever holds the string can
act as you from anywhere until it is revoked. Treat it like a password that
cannot be reset by email:

* `chmod 600` the file it lives in.
* Never paste it into a chat, an issue, a screenshot, or a config that gets
  committed.
* Revoke on any suspicion. Re-issuing costs a minute.

---

## Where to put the token

Tokens go in **`.env`**, and nowhere else.

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Minimum content to start in sandbox:

```dotenv
TINVEST_SANDBOX_TOKEN=t.your_sandbox_token_here
```

For production research-only (recommended):

```dotenv
TINVEST_READONLY_TOKEN=t.your_readonly_token_here
# TINVEST_FULLACCESS_TOKEN intentionally left empty:
# with no execution credential, an order cannot be placed at all.
```

### Why not in the client config

Every MCP client supports an `env` block where you could put the token
directly. Don't:

* `.vscode/mcp.json` and `.cursor/mcp.json` live inside the project and get
  committed. A token in git history is a token you must revoke.
* Editor settings sync copies the file to your other machines and, in a team,
  sometimes to other people's.
* Client configs end up in screenshots and bug reports.

`.env` is git-ignored by default here, can be `chmod 600`, and lives outside
any repo if you follow the next section. Paths in the client config are fine —
paths are not secrets.

---

## Where to put `config.toml`

An MCP client spawns the server with **whatever working directory the client
happens to have** — often your home directory or the client's install
directory, not your project. A `config.toml` sitting next to the repo will
therefore frequently not be found, and the server will silently run on built-in
defaults.

The server handles this by searching, in order:

1. `$TINVEST_MCP_CONFIG` — an explicit path. Wins; errors if the file is absent.
2. `./config.toml` in the working directory.
3. the nearest `config.toml` in a parent directory (up to 6 levels).
4. `~/.config/tinvest-mcp/config.toml`.

`.env` is discovered the same way (`$TINVEST_MCP_ENV_FILE`, cwd, parents,
`~/.config/tinvest-mcp/.env`).

**Recommended: use the per-user directory.** It works from any working
directory and keeps secrets out of every repo:

```bash
mkdir -p ~/.config/tinvest-mcp
cp config.toml.example ~/.config/tinvest-mcp/config.toml
cp .env.example        ~/.config/tinvest-mcp/.env
chmod 600              ~/.config/tinvest-mcp/.env
```

Files the server *writes* — the audit journal and the investment profile — go
to the **state directory**: `$TINVEST_MCP_STATE_DIR`, else the directory holding
`config.toml`, else `~/.local/state/tinvest-mcp`. Never the working directory,
for the same reason.

---

## Verify with the doctor

Before wiring up a client, check what the server actually resolved:

```bash
uv run tinvest-mcp-doctor        # or .venv/bin/tinvest-mcp-doctor
```

It prints the resolved file paths, the mode, whether real trading is armed,
which tokens are present (masked), the risk limits in effect, and the startup
security checks. It starts no server, opens no port, and places nothing.

Exit code `0` = all checks passed, `1` = at least one warning, `2` = it could
not even resolve the configuration.

**Read the "Resolved files" block twice.** A `config.toml` the server did not
find is a `config.toml` whose limits are not in effect — the most common
configuration surprise there is.

---

## Connecting a client

Every stdio client needs the same two things: the **absolute path** to the
executable, and nothing else if your config is discoverable.

### Claude Code

```bash
claude mcp add tinvest -- /absolute/path/to/tinvest-mcp/.venv/bin/tinvest-mcp
```

Or edit `.mcp.json` (project scope) or `~/.claude.json` (user scope):

```json
{
  "mcpServers": {
    "tinvest": {
      "command": "/absolute/path/to/tinvest-mcp/.venv/bin/tinvest-mcp"
    }
  }
}
```

Verify with `/mcp` inside Claude Code — the server should list 35 tools.

### Claude Desktop

Edit `claude_desktop_config.json`:

* macOS — `~/Library/Application Support/Claude/claude_desktop_config.json`
* Windows — `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "tinvest": {
      "command": "/absolute/path/to/tinvest-mcp/.venv/bin/tinvest-mcp"
    }
  }
}
```

Restart Claude Desktop completely (quit, not just close the window).

Claude Desktop supports stdio servers, which is what this one is by default —
so unlike hosted streamable-HTTP MCP servers, it works here.

### Cursor

`.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` globally:

```json
{
  "mcpServers": {
    "tinvest": {
      "command": "/absolute/path/to/tinvest-mcp/.venv/bin/tinvest-mcp"
    }
  }
}
```

### VS Code (agent mode)

`.vscode/mcp.json` — note the key is `servers`, not `mcpServers`:

```json
{
  "servers": {
    "tinvest": {
      "type": "stdio",
      "command": "/absolute/path/to/tinvest-mcp/.venv/bin/tinvest-mcp"
    }
  }
}
```

`.vscode/` is usually committed, so keep the token out of this file — the
server reads it from `.env`.

### Any other stdio client

Run this command, speak MCP over its stdin/stdout:

```bash
/absolute/path/to/.venv/bin/tinvest-mcp
```

Equivalent forms: `uv run tinvest-mcp` (needs the repo as cwd), or
`python -m tinvest_mcp.server`.

### Pointing at a non-standard config

```json
{
  "mcpServers": {
    "tinvest": {
      "command": "/absolute/path/to/.venv/bin/tinvest-mcp",
      "env": {
        "TINVEST_MCP_CONFIG": "/Users/you/.config/tinvest-mcp/config.toml",
        "TINVEST_MCP_ENV_FILE": "/Users/you/.config/tinvest-mcp/.env",
        "TINVEST_MCP_STATE_DIR": "/Users/you/.local/state/tinvest-mcp"
      }
    }
  }
}
```

Paths, not secrets — safe to commit.

---

## Docker

A `Dockerfile` and `docker-compose.yml` ship with the repo. Docker means the
HTTP transport, so read
[docs/security.md § Transport](security.md#transport-security) first: FastMCP's
HTTP transport has **no authentication**, and the compose file therefore binds
to `127.0.0.1` only.

```bash
docker compose up --build
```

The server listens on `127.0.0.1:8003/mcp`. The compose file mounts your
`config.toml` read-only, passes `.env` via `env_file`, and persists the state
directory in a named volume so the audit journal survives a rebuild.

Only run a container with real trading armed if you have deliberately decided
to, and never publish the port beyond loopback without an authenticating proxy
in front.

---

## Troubleshooting

### The client shows no tools, or "server failed to start"

Run the command by hand — the client usually hides the error:

```bash
/absolute/path/to/.venv/bin/tinvest-mcp
```

A healthy stdio server prints a line to stderr and then waits silently for
input. That silence is correct: it is waiting for the client to speak MCP.
`Ctrl-C` to exit. Anything else — a traceback, `command not found` — is the
real error.

Then run `tinvest-mcp-doctor`, which diagnoses configuration rather than
transport.

### My risk limits are being ignored

The server did not find your `config.toml`. Run `tinvest-mcp-doctor` and read
the `config.toml:` line. If it says `NOT FOUND`, either move the file to
`~/.config/tinvest-mcp/config.toml` or set `TINVEST_MCP_CONFIG` in the client's
`env` block.

### If the SDK will not install

`t-tech-investments` is served from an index that has been known to return
truncated wheels; `uv sync` then fails with an archive or hash error. Two
supported fallbacks:

**1. Use the legacy package.** `tinvest_mcp.sdk` imports `t_tech.invest` and
falls back to `tinkoff.invest`, which exposes the same API:

```bash
uv pip install tinkoff-investments
```

The `status` tool reports which one loaded in `sdk_package`.

**2. Use a vendored wheel.** Put a verified wheel in `vendor/` and uncomment
the block at the end of `pyproject.toml`:

```toml
[tool.uv.sources]
t-tech-investments = { path = "vendor/t_tech_investments-1.49.1-py3-none-any.whl" }
```

### `CERTIFICATE_VERIFY_FAILED` on the first broker call

The Russian CA root is not being applied. `tinvest_mcp.tls.ensure_grpc_roots()`
runs at adapter import and normally handles this. Check:

* `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` is not already set to something else in
  your environment — if it is, the server respects it and does not override it.
* `src/tinvest_mcp/certs/russian_trusted_ca.pem` exists in your install.
* The temp directory is writable (the combined bundle is cached there); if it
  is not, set `[tinvest].ca_bundle` to a bundle path you control.

Do **not** fix this by installing the Russian root into your system trust
store — see [security.md § TLS](security.md#tls-and-the-russian-ca).

### `No token configured for mode 'prod'`

`.env` was not found, or the variable for the active mode is empty. The doctor's
`.env:` line tells you which file is in effect. Remember the mode matters:
`sandbox` reads `TINVEST_SANDBOX_TOKEN`, `prod` reads `TINVEST_READONLY_TOKEN`
(and `TINVEST_FULLACCESS_TOKEN` for execution).

### `Real trading is disabled`

Working as intended. Two flags plus a token are required — see
[README § Going to production](../README.en.md#going-to-production). If you did
not mean to trade for real, nothing is wrong.

### Orders sit at `SUBMITTED` with `lots_executed=0`

Not an error. The limit order was accepted and is resting in the book: the
price is away from the market, or the session is closed (evenings, weekends).
Poll `get_order_state`, or `cancel_order` and re-price with `urgency="fast"`.

### A bond order is rejected with `PRICE_DEVIATION`

Almost always the units. Bond prices are **percent of nominal**: `99.09` means
99.09% of face value, not 99 rubles. Pass `urgency` instead of a hand-written
`limit_price` and let the server take the price from the book.

### Still stuck

Open an issue with the output of `tinvest-mcp-doctor` (it is token-free by
construction) and your `config.toml` (secret-free by design). Do **not** attach
`.env` or any token.
