# tinvest-mcp vs T-Bank's hosted MCP server

T-Bank publishes a first-party MCP server at
<https://developer.tbank.ru/invest/mcp>. This document compares the two
honestly, including where theirs is the better choice.

We are not neutral — we wrote one of them. So this is written to be checkable:
facts about their product come from their public documentation, facts about
ours point at the file that implements them.

## At a glance

| | **tinvest-mcp** (this) | **T-Bank hosted MCP** |
|---|---|---|
| Where it runs | your machine | T-Bank's servers |
| Transport | stdio (no open port) | streamable HTTP |
| Endpoint | — | `https://invest-public-api.tbank.ru/mcp` |
| Token at rest | `.env`, one local file | your MCP client's config file |
| Token in transit | to the broker's API only | `Authorization: Bearer` on every request to their gateway |
| Who else holds it | nobody | T-Bank (who issued it) |
| Risk limits | yours, in `config.toml` | broker-side, not user-configurable |
| Order flow | preview → fresh-quote re-check → submit by id | tool call places the order |
| Sandbox | yes | no |
| Untrusted content in the tool surface | none by design | news, sentiment, analyst ideas |
| Money transfers | never called | available as a token scope and a tool |
| Claude Desktop | works | not supported (no streamable HTTP) |
| Setup | Python install, ~10 minutes | one command |
| Maintenance | you update it | they update it |
| Support | community, best-effort | first-party |
| Audit trail | local append-only JSONL | broker-side |
| Source | MIT, auditable | closed |

## Use theirs if…

* You want something working in one command, with no Python toolchain.
* You want a vendor-supported product with someone accountable for uptime.
* You want tools this server deliberately does not have: news with sentiment
  scoring, investment ideas, analyst-house anomaly detection, tax reporting,
  account top-ups.
* You are not going to maintain a self-hosted service, and an unmaintained
  self-hosted service is worse than a maintained hosted one. This is a real
  argument and we will not pretend otherwise.

## Use this if…

* You want the credential to stay on your machine and out of your editor's
  config.
* You want to set your own hard caps — per-order value, daily turnover,
  position weight, an instrument allowlist.
* You want a sandbox to learn in before anything is real.
* You want untrusted text (news, ideas) **absent** from the same context as
  trading tools.
* You use Claude Desktop, which cannot connect to a streamable-HTTP server.
* You want to read the code that places your orders.

## On the security difference

### The wire is not the difference

The intuitive worry about a hosted MCP server is "my token is sent over the
network and could be intercepted". Stated precisely: **both designs send the
token over TLS, and in both the credential is encrypted on the wire.** An
observer sees the destination host (SNI), the IP, and packet sizes — not the
token. This server's gRPC channel is no more and no less protected than their
HTTPS request.

So wire interception is not where the difference lies. Two things are.

### 1. Where the token comes to rest

Their setup puts the token in your MCP client's configuration — `.vscode/mcp.json`,
`~/.qwen/settings.json`, a `claude mcp add --header` invocation. Those files:

* live inside projects and get **committed to git**;
* are **synced between machines** by editor settings sync;
* are **shared in teams** as part of a project setup;
* show up in screenshots and bug reports.

A token committed to a repository is a far more likely incident than any attack
on a TLS connection. This server reads from `.env` — git-ignored, `chmod 600`,
and placeable outside any repo at `~/.config/tinvest-mcp/.env` — precisely so
your client config stays free of secrets.

**Credit where due:** the recipient of their token is T-Bank, the entity that
issued it. No new trusted party is introduced. If a *third party* hosted an MCP
server that took your broker token, that would be a categorically different and
much worse situation. Theirs is not that.

### 2. What sits next to the trading tools

Their server exposes news with sentiment scoring, investment ideas, and
analyst-house anomaly detection — **untrusted text from the internet** — in the
same session as tools that place orders and top up accounts. That is the
canonical prompt-injection chain: text the model reads attempts to instruct it,
and the model holds the actuators.

This server has **no news, no sentiment, no web fetch**. That is a deliberate
omission, not a missing feature. The one place third-party opinion enters
(`get_instrument_forecast`) returns structured numeric fields — rating enum,
target price, upside percent — rather than prose.

The caveat: we control our tool surface, not your session. Load a
web-browsing MCP server alongside this one and you have reassembled the chain
inside your own client.

### 3. Scopes and limits

Their token scopes are coarse — read-only, full access, or full access plus
transfers — and chosen once at issuance. There are no user-configurable
order-size or velocity limits documented.

Here, the scope choice is the same (it is the broker's token either way), but
the server adds caps you control: per-order value, daily turnover, position
weight, price deviation, quote age, LIMIT-only, no margin, no shorts, and an
optional strict instrument allowlist. Prod defaults are deliberately tiny —
1 500 ₽ per order — so a first mistake is a cheap lesson.

And the control that matters most is available in both architectures but only
practical in this one: **run with a read-only token and no execution token at
all.** Here that makes orders structurally impossible while research keeps
working. On a hosted server whose tool list is fixed, the equivalent is issuing
a read-only token and accepting that the trading tools will simply error.

### Where this server is weaker

Symmetry demands these:

* **Two things, not one, to maintain.** Python environment plus the server. A
  stale self-hosted server with an unpatched dependency is worse than a managed
  one.
* **No caller identity, same as theirs.** MCP does not tell the server whether
  a call came from the model or from a human pressing Confirm. Our "the agent
  must never call `post_order`" is a prompt instruction, not a gate — see
  [security.md](security.md#the-caller-identity-problem). Their server has the
  same structural property.
* **Your machine is the weak point.** The token is on your disk. Their hosted
  design at least does not put a long-lived credential file on a laptop that
  might be stolen — though it does put one in your editor config, which is
  usually worse.
* **We extend TLS trust** to the Russian national CA for this process's gRPC
  channels, because the API is otherwise unreachable. It is scoped to one
  process rather than your system store
  ([details](security.md#tls-and-the-russian-ca)), but it is still an
  expansion of trust that a hosted client does not ask of you locally.
* **No first-party support.** If this breaks at 9:55 before the open, you are
  reading our source.

## Running both

Nothing stops you. A sensible split, if you want their research breadth and our
execution discipline:

* Theirs with a **read-only** token, for news and ideas.
* This one with a read-only token for portfolio work, and execution armed only
  when you are deliberately trading.

One caution: if both are loaded in the same client session, their news tools
and our order tools are in the same context, which recreates the injection
chain this server avoids. If you care about that, keep them in separate
sessions.

---

<sub>Facts about T-Bank's MCP server come from
<https://developer.tbank.ru/invest/mcp> and were accurate as of
September 2026; their product may have changed. Corrections welcome as a pull
request. We are not affiliated with T-Bank — see
[DISCLAIMER.md](../DISCLAIMER.md).</sub>
