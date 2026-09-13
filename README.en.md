<h1 align="center">tinvest-mcp — MCP server for T-Invest</h1>

<p align="center">
  <strong>A local MCP server for the T-Bank Invest (T-Invest, formerly Tinkoff Investments) brokerage API.</strong><br>
  Research, portfolio analytics, risk-checked order previews, gated execution.<br>
  <sub>Infrastructure for <strong>AI wealth management</strong>: the model reasons, the limits are code.</sub>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-blue.svg">
  <img alt="MCP" src="https://img.shields.io/badge/protocol-MCP-8A2BE2.svg">
  <img alt="Status: beta" src="https://img.shields.io/badge/status-beta-orange.svg">
  <br>
  <em>Built by the <a href="https://plasm.one">Plasm</a> team — we build
  <a href="https://plasm.one/future-of-finance">autonomous finance</a></em>
</p>

<p align="center">
  <a href="README.md">🇷🇺 Русская версия</a> ·
  <a href="docs/installation.md">Installation</a> ·
  <a href="docs/security.md">Security model</a> ·
  <a href="docs/configuration.md">Configuration</a> ·
  <a href="docs/tools.md">Tool reference</a>
</p>

---

> [!WARNING]
> **Unofficial software that can move real money.** Not affiliated with,
> endorsed by, or supported by T-Bank / Т-Банк. Not investment advice. An MCP
> server that holds brokerage tools carries risks that no amount of code can
> fully remove — prompt injection and model error among them.
> **Read [DISCLAIMER.md](DISCLAIMER.md) and [docs/security.md](docs/security.md)
> before you connect a production token.** Default mode is `sandbox`, and real
> trading stays off until you turn on two separate switches. Please leave it
> that way until you have read both documents.

---

## What this is

An MCP ([Model Context Protocol](https://modelcontextprotocol.io)) server that
runs **on your own machine** and gives an AI assistant 35 tools for working with
a T-Invest brokerage account: reading the portfolio, screening the instrument
catalogue, computing yield and risk, building a rebalance plan, and — behind
explicit gates — submitting limit orders.

The design premise is that **the model is a research assistant, not a trader**.
Reads are open; everything that touches money goes through a preview that the
model cannot skip, priced against a fresh quote, bounded by numeric limits that
live in your config file rather than in a prompt.

<p align="center">
  <img src="https://raw.githubusercontent.com/plasm-one/tinvest-mcp/main/docs/assets/architecture.png"
       alt="Claude Code / Cursor / VS Code talk to tinvest-mcp over stdio; tinvest-mcp talks to the T-Bank Invest API over gRPC+TLS and reads .env, config.toml and audit.jsonl from your machine"
       width="900">
</p>

**The token never goes anywhere except to the broker that issued it.** It is not
stored in your editor's config, not synced between machines, and not sent to any
third party — including us. See [docs/security.md](docs/security.md) for why
that matters and for the risks this design does *not* remove.

## Why an MCP server for a brokerage at all

The hard part of managing your own money is not order entry — brokers have
perfectly good apps for that. It is the reasoning around it: *what do I actually
hold, what is it costing me, what should change now that my situation has
changed, and what must never happen whatever the argument for it.*

A language model is genuinely good at the first three. It is unreliable at the
fourth — and the fourth is the one that loses money. So the split this server
makes is deliberate: the model gets deep read access and the job of explaining,
while every constraint that must hold lives in code and in your config file,
where a persuasive argument cannot reach it.

That is what makes MCP the right shape here. Not "chat with your broker", but: a
model that can read your whole position, reason about it in the terms you care
about, propose one concrete change with the arithmetic already done — and hand
you a decision you can check, while the boundaries it cannot cross are enforced
somewhere it has no access to.

This is what **AI wealth management** has to look like before it can be trusted
with a real account: not an agent that trades, but an agent that reasons, inside
limits that are not up for negotiation.

## Why this and not T-Bank's own MCP server

T-Bank publishes a first-party hosted MCP server. It is a good product and if
you want vendor support, use it. The two make different trade-offs:

| | **tinvest-mcp** (this) | **T-Bank hosted MCP** |
|---|---|---|
| Runs | on your machine | on T-Bank's servers |
| Token lives in | `.env`, read by one local process | your editor's MCP config, sent with every request |
| Transport | stdio (no open port) | streamable HTTP |
| Execution gate | preview → fresh-quote re-check → explicit submit | tool call places the order |
| Risk limits | yours, in `config.toml` | broker-side, not user-configurable |
| Sandbox | yes | no |
| Claude Desktop | works (stdio) | not supported |
| Untrusted content in context | no news/sentiment tools by design | news + sentiment + analyst ideas |
| Support | community, best-effort | first-party |
| Setup effort | Python install, ~10 minutes | one command |

Honest summary: theirs is faster to set up and vendor-supported; this one keeps
the credential local, lets you set your own hard caps, and does not put
untrusted text in the same context as trading tools.
Full write-up: [docs/comparison.md](docs/comparison.md).

## What it can do

**Research (read-only, always available)**

* Portfolio: cash, positions, allocation by class/sector/currency/issuer,
  concentration, weighted yield, bond duration, drift against a saved target.
* Screeners with a separate tool per asset class, so rows carry only the fields
  that mean something for that class: `list_bonds`, `list_shares`, `list_etfs`.
  Filter by risk, maturity, duration, sector, dividends, liquidity floor.
* Per-instrument: historical return, annualised volatility, max drawdown, bond
  YTM / current yield / coupon schedule, share dividend yield, fundamentals
  (P/E, EV/EBITDA, ROE, margins, debt), analyst consensus, ETF fees and index.
* Market snapshot: bid/ask/last, spread, quote age, and three suggested limit
  prices (patient / balanced / fast) so a price is chosen from the book rather
  than guessed by the model.
* Operation history: trades, commissions, coupons, dividends, withheld tax.

**Planning (read-only)**

* A deterministic target allocation from `risk_profile × horizon` — a rule
  table in code, not a model opinion.
* Whole-basket rebalance plans: sells sequenced before buys, every leg priced
  with commission and accrued interest, FIFO tax lots with the 3-year ЛДВ
  exemption applied, plus a `WORTH_IT` / `NOT_WORTH_IT` verdict that will tell
  you to do nothing when costs exceed the benefit.

**Execution (off by default, gated)**

* `create_order_proposal` → risk-checked preview, places nothing.
* `post_order` → submits a still-valid proposal by id, re-checking risk against
  a fresh quote. LIMIT only. Idempotent.
* Trade plans add two more gates: confirm the whole plan, then confirm each
  individual leg's execution card.

Full list with arguments and return shapes: [docs/tools.md](docs/tools.md).

## Requirements

* **Python 3.12+**
* An MCP-capable client: Claude Code, Claude Desktop, Cursor, VS Code (agent
  mode), Gemini CLI, Qwen Code, or anything else that speaks MCP over stdio.
* A T-Invest API token (free, issued in the broker's web interface — next section).
* [`uv`](https://docs.astral.sh/uv/) recommended; plain `pip` works fine.

## Installation

### 1. Get the code and install

```bash
git clone https://github.com/plasm-one/tinvest-mcp.git
cd tinvest-mcp
uv sync                      # or: python3 -m venv .venv && .venv/bin/pip install -e .
```

<details>
<summary>If the T-Invest SDK will not install</summary>

The gRPC SDK (`t-tech-investments`) is served from an index that has been known
to return truncated wheels, which breaks `uv sync` with a hash or archive error.
Two fallbacks, both supported out of the box:

1. **Legacy package.** `tinvest_mcp.sdk` imports `t_tech.invest` and falls back
   to `tinkoff.invest` automatically, so installing the older
   `tinkoff-investments` package instead works:
   ```bash
   uv pip install tinkoff-investments
   ```
2. **Vendored wheel.** Put a verified wheel in `vendor/` and uncomment the
   `[tool.uv.sources]` block at the bottom of `pyproject.toml`.

`tinvest-mcp` prints which one it loaded in the `status` tool's `sdk_package`
field. Details: [docs/installation.md](docs/installation.md#if-the-sdk-will-not-install).
</details>

### 2. Create your API token

In T-Bank Invest: **Settings → API tokens → Create token**
(web: <https://www.tbank.ru/invest/settings/api/> · docs:
<https://developer.tbank.ru/invest/intro/intro/token>).

You choose a **scope** at creation, and the choice is the single most important
security decision in this whole setup:

| Scope | Use it for | Verdict |
|---|---|---|
| **Read-only** | everything this server does except placing orders | ✅ **start here, and stay here** |
| **Full access** | placing real orders | ⚠️ only if you truly want the agent to trade |
| **+ money transfers** | moving cash between accounts | ❌ **never** — this server does not use it, and it turns a leaked file into a withdrawal |

> [!IMPORTANT]
> The token is displayed **once** and cannot be recovered. Save it immediately.
> If you ever lose track of a token, revoke it in the same screen and issue a
> new one — revocation is instant and free.

For the safest production setup, issue **two separate tokens**: a read-only one
for research and a full-access one you leave out of the config entirely until
the day you actually want to execute. The server checks at startup that they
are not the same string.

### 3. Give the server the token

Tokens go in a `.env` file — **never** in your editor's MCP config, which tends
to get committed to git or synced between machines.

```bash
cp .env.example .env
cp config.toml.example config.toml
chmod 600 .env                # owner-only; do this
```

Then edit `.env`. To start in sandbox (recommended):

```dotenv
TINVEST_SANDBOX_TOKEN=t.your_token_here
```

That is the whole secret configuration. Everything else — mode, risk limits,
transport — is non-secret and lives in `config.toml`.

<details>
<summary>Recommended: keep config outside the repo</summary>

An MCP client starts the server with an arbitrary working directory, so the
server searches for `config.toml` and `.env` upward from the cwd and then in
`~/.config/tinvest-mcp/`. Putting them there works from anywhere and keeps
secrets out of any repo:

```bash
mkdir -p ~/.config/tinvest-mcp
cp .env.example ~/.config/tinvest-mcp/.env
cp config.toml.example ~/.config/tinvest-mcp/config.toml
chmod 600 ~/.config/tinvest-mcp/.env
```
</details>

### 4. Verify before wiring up a client

```bash
uv run tinvest-mcp-doctor       # or: .venv/bin/tinvest-mcp-doctor
```

This resolves your configuration, reports which files it actually found, which
tokens are present (masked), and runs the startup security checks — without
starting a server, opening a port, or placing anything. Expected on a fresh
sandbox setup:

```
Resolved files
------------------------------------------------------------
config.toml:          /Users/you/.config/tinvest-mcp/config.toml
.env:                 /Users/you/.config/tinvest-mcp/.env
state directory:      /Users/you/.config/tinvest-mcp
audit journal:        /Users/you/.config/tinvest-mcp/audit.jsonl

Mode
------------------------------------------------------------
mode:                 sandbox
real trading:         disabled
transport:            stdio
SDK package:          t_tech.invest

Tokens (masked)
------------------------------------------------------------
sandbox:              present (t.a***xyz)
read-only (prod):     not set
full access (prod):   not set

Startup security checks
------------------------------------------------------------
[ok]   real_trading_default_off: real_trading_enabled=False mode=sandbox
[ok]   token_present: sandbox token configured
[ok]   limits_configured: max_order_rub=500000 max_daily=5000000
```

The "Resolved files" block is the one to read twice: a `config.toml` the server
did not find is a `config.toml` whose limits are not in effect.

### 5. Connect your client

All clients need the same thing: the **absolute path** to the `tinvest-mcp`
executable inside your virtualenv. Find it with `readlink -f .venv/bin/tinvest-mcp`
(or `uv run which tinvest-mcp`).

<details open>
<summary><strong>Claude Code</strong></summary>

```bash
claude mcp add tinvest -- /absolute/path/to/tinvest-mcp/.venv/bin/tinvest-mcp
```

Or by hand in `.mcp.json` (project) / `~/.claude.json` (user):

```json
{
  "mcpServers": {
    "tinvest": {
      "command": "/absolute/path/to/tinvest-mcp/.venv/bin/tinvest-mcp"
    }
  }
}
```
</details>

<details>
<summary><strong>Claude Desktop</strong></summary>

Edit `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/`,
Windows: `%APPDATA%\Claude\`), then restart the app:

```json
{
  "mcpServers": {
    "tinvest": {
      "command": "/absolute/path/to/tinvest-mcp/.venv/bin/tinvest-mcp"
    }
  }
}
```

Claude Desktop speaks stdio, which is what this server uses by default — so
unlike hosted streamable-HTTP MCP servers, this one works there.
</details>

<details>
<summary><strong>Cursor</strong></summary>

`.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global):

```json
{
  "mcpServers": {
    "tinvest": {
      "command": "/absolute/path/to/tinvest-mcp/.venv/bin/tinvest-mcp"
    }
  }
}
```
</details>

<details>
<summary><strong>VS Code (agent mode)</strong></summary>

`.vscode/mcp.json` — note VS Code uses `servers`, not `mcpServers`:

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

Do not add a token here: `.vscode/` is commonly committed. This server reads it
from `.env` precisely so your editor config stays free of secrets.
</details>

<details>
<summary><strong>Config in a non-standard location</strong></summary>

If your files are not discoverable from the working directory, point at them
explicitly:

```json
{
  "mcpServers": {
    "tinvest": {
      "command": "/absolute/path/to/.venv/bin/tinvest-mcp",
      "env": {
        "TINVEST_MCP_CONFIG": "/Users/you/.config/tinvest-mcp/config.toml",
        "TINVEST_MCP_ENV_FILE": "/Users/you/.config/tinvest-mcp/.env"
      }
    }
  }
}
```

These are *paths*, not secrets — safe to commit.
</details>

### 6. First run, in sandbox

Ask your assistant:

> Check the T-Invest server status, open a sandbox account, fund it with
> 100 000 ₽ of virtual money, then show me the portfolio.

Then try a full loop end to end — screen some bonds, look at one in detail, get
a market snapshot, create an order proposal, read the preview, and only then
submit it. Sandbox money is fake; this is the place to learn what each gate
does and what the previews look like before any of it is real.

There is also a scripted version of the same loop:

```bash
uv run python -m tinvest_mcp.scripts.sandbox_smoke
```

## Going to production

Do this deliberately, in this order, and read
[docs/security.md](docs/security.md) first.

**Research only — the recommended production setup.** Put a read-only token in
`.env`, leave `TINVEST_FULLACCESS_TOKEN` empty, and set:

```toml
[tinvest]
mode = "prod"
enable_real_trading = false
```

The agent now sees your real portfolio and real prices and **cannot place an
order** — not by policy but structurally: the execution path has no credential
to authenticate with. Most people should stop here.

**Real trading.** Only if you have decided you want it. All four must be true:

1. `mode = "prod"` in `config.toml`
2. `enable_real_trading = true` in `config.toml`
3. `TINVEST_FULLACCESS_TOKEN` set in `.env` (a *different* token from the read-only one)
4. Risk caps in `config.toml` reviewed and set to numbers you are comfortable
   losing — **delete any values you relaxed for sandbox.** The prod defaults are
   1 500 ₽ per order and 3 000 ₽ daily turnover, deliberately small enough that
   a first mistake is a cheap lesson.

## Security

Short version:

* **The token stays local.** Read from `.env` into one process; sent only to
  T-Bank's own API over TLS. No third party, us included, ever sees it.
* **Never logged.** Errors, audit records and the settings `repr` are all
  scrubbed; account ids are masked; keys that look token-shaped are dropped
  before writing.
* **Two independent switches** stand between a fresh install and a real order,
  and the credential for real orders is absent by default.
* **Hard numeric limits** live in `config.toml`, not in a prompt — per-order
  value, daily turnover, position weight, price deviation, quote age, LIMIT
  only, no margin, no shorts, optional strict instrument allowlist.
* **Re-validation at submit time.** A preview that has aged past its TTL, or
  whose price no longer matches the book, cannot be executed.
* **No untrusted content tools.** No news, no sentiment, no web fetch — the
  usual prompt-injection carriers are simply not in the tool surface.
* **Append-only audit journal** of every action that left the process.
* **Loopback and stdio by default.** Nothing listens on a port unless you ask.

Equally important, the things this design does **not** fix — MCP has no caller
identity, so "the agent must never call `post_order`" is a prompt instruction
rather than a technical gate; a compromised machine is a compromised account;
and TLS trust is extended to the Russian national CA for this process's gRPC
channels. Each is explained, with the mitigation that actually works, in
**[docs/security.md](docs/security.md)**.

Found a vulnerability? See [SECURITY.md](SECURITY.md) — please do not open a
public issue.

## Documentation

| | |
|---|---|
| [docs/installation.md](docs/installation.md) | Full install, token creation, every client, troubleshooting |
| [docs/security.md](docs/security.md) | Threat model, what is enforced where, hardening checklist |
| [docs/configuration.md](docs/configuration.md) | Every `config.toml` key and environment variable |
| [docs/tools.md](docs/tools.md) | All 35 tools: arguments, returns, which are read-only |
| [docs/architecture.md](docs/architecture.md) | Layers, the execution gates, how risk checks run |
| [docs/comparison.md](docs/comparison.md) | Honest comparison with T-Bank's hosted MCP server |
| [DISCLAIMER.md](DISCLAIMER.md) | Unofficial status, MCP risks, not investment advice |

## Development

```bash
uv sync --all-extras          # dev dependencies
uv run pytest                 # 213 tests, no network, no token needed
uv run ruff check .
uv run ruff format .
```

The suite is fully offline: every broker call goes through a fake adapter, and
the audit journal is redirected to a temp file, so `pytest` is safe to run on a
machine that has production tokens in its environment.

```
src/tinvest_mcp/
  server.py          MCP tool registry and the agent playbook prompt
  tools.py           tool signatures and validation (the MCP boundary)
  services.py        orchestration: research, proposals, plans, execution
  adapter.py         the only module that talks to the broker SDK
  risk_engine.py     every hard check, one function per rule
  proposals.py       proposal store with TTL and idempotency keys
  trade_plan.py      multi-leg plans and their two confirmation gates
  allocation.py      deterministic target-allocation rule table
  schemas.py         pydantic models — the contract the model sees
  journal.py         append-only audit journal
  config/            config.toml + .env discovery and resolved Settings
  tls.py             gRPC trust roots for the T-Bank certificate chain
```

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Changes to
`risk_engine.py`, `proposals.py` or `trade_plan.py` need tests; they are the
parts that stop money from moving by accident.

## About Plasm

We build **autonomous finance** at [Plasm](https://plasm.one) — the idea that
your financial life should think ahead, adapt, and act, instead of being a pile
of disconnected products you reconcile by hand. Our vision, in full:
**[The Future of Finance →](https://plasm.one/future-of-finance)**

That vision describes four cooperating agents. This server is a working,
auditable slice of exactly that model, pointed at one real broker:

| Plasm agent | Its counterpart here |
|---|---|
| **Planning** — find the options for your capital | [`allocation.py`](src/tinvest_mcp/allocation.py) target allocation from a rule table; [`trade_plan.py`](src/tinvest_mcp/trade_plan.py) the priced basket with a worth-it verdict |
| **Risk** — protect the reserves | [`risk_engine.py`](src/tinvest_mcp/risk_engine.py) per-order caps, price, liquidity and session checks |
| **Checking** — verify the constraints | the personal mandate: issuer, sector, cash floor, FX exposure, tested against the *resulting* portfolio |
| **Action** — execute, with your approval | proposal → fresh-quote re-check → explicit submit; nothing moves without you |

*"You set the rules. Agents follow them."* — here the rules are literally
[`config.toml`](config.toml.example), and they are the one thing the model
cannot edit.

And because *intelligence should not depend on the size of your balance*: this
is MIT-licensed, runs on your own laptop, and needs nothing but a free broker
API token.

Building in this space? We would like to hear from you — <alex@plasm.one>.

## License

[MIT](LICENSE) © 2026 [Plasm](https://plasm.one)

---

<p align="center">
  Built with care by the <a href="https://plasm.one"><strong>Plasm</strong></a> team.<br>
  <sub>Unofficial. Not affiliated with T-Bank. Not investment advice.<br>
  Read <a href="DISCLAIMER.md">DISCLAIMER.md</a>.</sub>
</p>
