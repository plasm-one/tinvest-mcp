# Security model

*[Русская версия](security.ru.md)*

This document describes what `tinvest-mcp` protects, how, and — the part most
security docs skip — **what it does not protect**, with the mitigation that
actually works in each case.

Read it before connecting a production token. If you read only one section,
read [Hardening checklist](#hardening-checklist) and
[What this does not protect against](#what-this-does-not-protect-against).

---

## Contents

1. [Hardening checklist](#hardening-checklist)
2. [Threat model](#threat-model)
3. [The credential: where the token lives and where it goes](#the-credential)
4. [Token scopes](#token-scopes)
5. [Defence layers: what is enforced, and where](#defence-layers)
6. [The execution path, step by step](#the-execution-path)
7. [Prompt injection](#prompt-injection)
8. [Transport security](#transport-security)
9. [TLS and the Russian CA](#tls-and-the-russian-ca)
10. [Logging, masking and the audit journal](#logging-masking-and-the-audit-journal)
11. [What this does not protect against](#what-this-does-not-protect-against)
12. [Incident response](#incident-response)
13. [Reporting a vulnerability](#reporting-a-vulnerability)

---

## Hardening checklist

In descending order of how much each one buys you.

- [ ] **Run production with a read-only token and no execution token.**
      Leave `TINVEST_FULLACCESS_TOKEN` empty. An order then cannot be placed —
      not by policy, structurally: `Settings.active_trade_token()` returns
      `None` and `TInvestAdapter` raises before any RPC. This is the single
      most effective control available to you, and it costs nothing.
- [ ] **Never grant the money-transfer scope.** This server never calls a
      transfer endpoint. A token that can move cash converts a leaked file into
      a withdrawal.
- [ ] **Learn the flow in `sandbox` first.** Real orders are impossible there
      regardless of what the model does.
- [ ] **`chmod 600 .env`.** It is a bearer credential in a plain text file.
- [ ] **Keep `transport = "stdio"`.** No port, no listener, no local attacker.
- [ ] **Set the risk caps yourself** in `config.toml`, and re-check them after
      switching `mode` — see the sandbox-values-in-prod trap
      [below](#the-sandbox-values-in-prod-trap).
- [ ] **Use `instrument_allowlist`** if the agent only needs a handful of
      instruments. It is the tightest control in the file.
- [ ] **Do not put the token in your editor's MCP config.** `.vscode/mcp.json`
      and `.cursor/mcp.json` get committed and synced. This server reads from
      `.env` specifically so they stay clean.
- [ ] **Keep untrusted-content MCP servers out of the same session** as this
      one — see [Prompt injection](#prompt-injection).
- [ ] **Read `audit.jsonl` occasionally.** It is the only record of what the
      agent did that does not depend on the agent's own account of it.
- [ ] **Run `tinvest-mcp-doctor`** after any configuration change. It prints
      which files are actually in effect and what is armed.

---

## Threat model

### In scope — defended

| Threat | Defence |
|---|---|
| Model places an order the user never approved | Preview → explicit submit, re-validated against a fresh quote; real trading disarmed by default |
| Model miscalculates size or price | Hard numeric caps in `config.toml`, checked twice; price-deviation and quote-age checks |
| Bond quoted in % of nominal read as rubles | `PRICE_DEVIATION` + `PRICE_INCREMENT` checks; unit is stated in every snapshot |
| Token leaked through logs or an error message | Tokens never logged; `Settings.__repr__` redacts; error types carry no credentials; token-shaped keys stripped before journalling |
| Token leaked through a committed config | Tokens live only in `.env`, git-ignored, separate from `config.toml` |
| Retried call double-submits an order | Idempotency key persisted *before* the RPC (`services.py`) |
| Stale approval executed later | Proposal TTL (`confirmation_ttl_seconds`), plan TTL, `EXPIRED` is terminal |
| Order lands in the wrong account | `require_single_account_token`; account isolation probe at startup; the model never supplies an account id |
| Plan leg submitted out of order, or the plan bypassed | Plan-linked proposals are refused by `post_order`; execution is sequential and sell-first; a stalled leg pauses the plan |
| Runaway loop of orders | Daily turnover cap; per-order cap; position-weight cap |
| Prompt injection via market content | No news / sentiment / web-fetch tools exist in this server |
| Unauthenticated local access to the server | stdio by default: no listener at all |
| MITM on the broker connection | TLS with an explicit trust bundle, scoped to this process |

### Out of scope — not defended

| Threat | Why | What to do instead |
|---|---|---|
| Compromised host | The token is on that disk, readable by your user | OS-level hygiene, disk encryption, no execution token |
| Malicious MCP client | The client is the trusted party by construction | Use clients you trust |
| A caller impersonating "the UI" | MCP carries no caller identity — [see below](#the-caller-identity-problem) | Run without an execution token |
| Broker-side outage, rejection, rate limit | Not ours to control | Errors are surfaced, never silently swallowed |
| Market risk | Not a security property | Read [DISCLAIMER.md](../DISCLAIMER.md) |
| Supply-chain compromise of a dependency | Standard ecosystem risk | Review dependencies; `uv lock` in your own deployment |

---

## The credential

### Where it lives

```
.env  (chmod 600, git-ignored)
  └─► os.environ, once, at first settings read
        └─► Settings.sandbox_token / readonly_token / fullaccess_token
              └─► TInvestAdapter._client(trade=…)  ──gRPC+TLS──►  invest-public-api.tbank.ru
```

Four properties worth stating explicitly:

1. **One recipient.** The token is sent to exactly one host — T-Bank's own API,
   the entity that issued it. No proxy, no relay, no telemetry, no plasm.one
   endpoint. There is no code path in this repository that transmits a token
   anywhere else; `grep -r "token" src/` is a short read if you want to check.
2. **One file.** Secrets are in `.env` and nowhere else. `config.toml` is
   deliberately secret-free so it can be committed and pasted into bug reports.
3. **Split by privilege.** Reads use `active_read_token()`, execution uses
   `active_trade_token()`, and they resolve to different strings when you
   configure them that way. `startup_checks.py` warns if they are identical.
4. **Never rendered.** `Settings.__repr__` prints `tokens=<redacted>`. Account
   ids are masked to `200***456` by `mask()`. `audit.py` drops any metadata key
   containing `token` or `authorization` before writing.

### Risk limits are not readable from the environment

Every `[tinvest]` value — mode, caps, flags — is read from `config.toml` only,
with **no environment-variable override**. This is a deliberate asymmetry with
the transport settings (which do accept env overrides):

> A risk limit that a stray environment variable can raise is not a risk limit.

To change a cap you edit a file on disk and restart. An MCP client that sets
environment variables for the child process it spawns cannot widen your limits,
and neither can anything that merely influences the environment.

### The sandbox-values-in-prod trap

Limit *defaults* differ by mode, so switching to prod tightens them:

| | `max_order_rub` | `max_daily_turnover_rub` | `max_position_weight` |
|---|---|---|---|
| `sandbox` default | 500 000 | 5 000 000 | 1.0 |
| `prod` default | **1 500** | **3 000** | **0.40** |

But an **explicit** value in `config.toml` always wins over the default. So the
failure mode is: you relax the caps to experiment in sandbox, later flip
`mode = "prod"`, and carry 500 000 ₽ per-order into production with a comment
above it still saying "relaxed for sandbox".

Delete relaxed values rather than editing them when you switch, and run
`tinvest-mcp-doctor` — it prints the caps actually in effect and warns when a
large cap is armed alongside real trading.

---

## Token scopes

Chosen when you create the token in the broker's web interface, and not
changeable afterwards — issue a new token instead.

| Scope | What this server does with it | Recommendation |
|---|---|---|
| **Read-only** | everything except placing orders | ✅ default choice |
| **Full access** | places limit orders | ⚠️ only with real trading deliberately armed |
| **Transfers / money movement** | **nothing — never used** | ❌ do not grant |

A T-Invest token is a **bearer credential**: no proof of possession, no client
binding. Whoever holds the string is you, from any IP, until you revoke it.
That is precisely why scope is the control that matters — a read-only token in
the wrong hands is an information disclosure; a transfer-capable one is a loss
of funds.

Revocation is immediate and free, in the same screen that issued it. If you are
ever unsure whether a token leaked, revoke it. The cost is re-issuing a token;
the alternative is unbounded.

---

## Defence layers

Enforcement lives in [`risk_engine.py`](../src/tinvest_mcp/risk_engine.py) —
one function per rule, each returning a named check. **Hard** means the order
cannot proceed. **Advisory** means it is surfaced for a human to decide.

### Structural — cannot be reached at all

| Control | Config | Default |
|---|---|---|
| Real trading disarmed | `mode` + `enable_real_trading` | `sandbox`, `false` |
| No execution credential | absence of `TINVEST_FULLACCESS_TOKEN` | not set |
| LIMIT orders only | `allow_market_orders` | `false` |
| No margin | `allow_margin` | `false` |
| No short selling | `allow_shorts` | `false` |
| Instrument classes | `allowed_instrument_types` | `share, bond, etf` |
| Strict instrument allowlist | `instrument_allowlist` | empty (off) |
| Single-account isolation | `require_single_account_token` | `true` |

### Hard numeric checks — per order

| Check code | Rule |
|---|---|
| `MAX_ORDER_VALUE` | order total ≤ `max_order_rub` |
| `DAILY_TURNOVER` | today's total ≤ `max_daily_turnover_rub` |
| `POSITION_WEIGHT` | resulting position ≤ `max_position_weight` of the portfolio |
| `SUFFICIENT_CASH` | cash covers total including commission and accrued interest |
| `POSITION_EXISTS` | selling only what is actually held and sellable |
| `PRICE_DEVIATION` | limit within `max_price_deviation_pct` of the live quote |
| `PRICE_INCREMENT` | price is a multiple of the instrument's tick |
| `PRICE_POSITIVE` | no zero or negative price |
| `MAX_LOTS` | within the broker's own `GetMaxLots` |
| `LIMIT_ORDERS_ONLY`, `NO_MARKET_ORDER` | order type allowed |
| `DIRECTION_ALLOWED` | no shorting when disabled |
| `INSTRUMENT_TYPE_ALLOWED`, `INSTRUMENT_ALLOWLIST` | instrument permitted |
| `API_TRADE_AVAILABLE`, `TRADING_STATUS` | broker actually allows API trading now |
| `NOT_QUALIFIED_ONLY` | not a qualified-investor-only instrument |
| `MARKET_SESSION` | not during a clearing pause or outside session hours |

Market data older than `market_data_max_age_seconds` is refused outright, so no
check is ever evaluated against a stale quote.

### Plan-level checks — the resulting portfolio

| Check code | Rule |
|---|---|
| `PLAN_STEP_CASH`, `PLAN_STEP_POSITION` | each leg feasible in sequence |
| `PLAN_MIN_CASH` | cash floor preserved |
| `PLAN_ISSUER_LIMIT`, `PLAN_SECTOR_LIMIT` | concentration caps, scaled by portfolio size |
| `PLAN_ALLOCATION_MANDATE` | result stays inside the saved allocation band |
| `PLAN_CURRENCY_EXPOSURE` | FX exposure cap (advisory without an opt-in) |

### Advisory — surfaced, not blocked

`LDV_WARNING` (selling forfeits the 3-year tax exemption soon),
`CORPORATE_ACTION_SOON` (offer or maturity near), `CURRENCY_EXPOSURE`
(the trade adds foreign-currency risk), `TAX_IMPACT` (НДФЛ estimate).

These pass on purpose. They are judgement calls that belong to you, and the
tool descriptions instruct the model to show them rather than bury them.

### Startup checks

`startup_checks.py` runs at boot, logs each finding, and exposes them through
the `status` tool: real trading disarmed, token present for the mode, read and
trade tokens distinct, limits configured, single-account isolation, and — in
prod with real trading armed — a non-mutating `GetAccounts` probe through the
*trade* token to confirm it genuinely has full access to the same account set.
Presence of a secret is not proof it can trade; the probe checks.

These are **advisory**: the server still starts so research works. The hard
stops are at execution time.

---

## The execution path

What `post_order(proposal_id)` does, in order
([`services.py`](../src/tinvest_mcp/services.py)):

1. **Plan-bypass check.** A proposal belonging to a trade plan is refused
   outright — it must go through `execute_plan_step`, which has its own two
   gates. This closes the obvious route around plan confirmation.
2. **TTL check.** Expired → `EXPIRED`, terminal, no execution. The price it was
   approved at is no longer real.
3. **Status check.** Only `READY_FOR_CONFIRMATION` (or a reconciliation state)
   proceeds; terminal states are refused.
4. **Real-trading gate.** In prod without `enable_real_trading`,
   `TInvestRealTradingDisabledError`. Independent of the startup check — this
   one is at the point of no return.
5. **Full re-validation.** Fresh instrument data, fresh market snapshot, fresh
   portfolio, then the *entire* risk battery again. A preview that passed
   against a quote from two minutes ago does not get to execute now. Failure →
   `RISK_REJECTED` + an audit record naming the failed check codes.
6. **Idempotency key persisted before the RPC.** A retry after a timeout cannot
   produce a second order.
7. **Audit record written before the call**, then the order goes out as a LIMIT
   order at a money price derived from the validated quote.

Trade plans add: `confirm_trade_plan(plan_id)` for the whole basket, then
`preview_plan_step(plan_id)` → `execute_plan_step(plan_id)` per leg, sequential
and sell-first, where any non-terminal or failed leg pauses the plan and blocks
everything after it. Nothing auto-continues.

Note what the execution calls carry: **only an id**. Not an instrument, not a
quantity, not a price. The order's terms were fixed when the preview was
created and cannot be edited at submit time — so a model that constructs a
different order between preview and execution has nowhere to put it.

---

## Prompt injection

The hard problem in agentic trading, and the one no code in this repository can
fully solve.

**The mechanism.** Untrusted text reaching the model's context can attempt to
instruct it. A model holding trading tools is a model that can be argued into
using them. The text does not have to come from this server — a web page in
another tab of the same session, a document, a README, the output of a
different MCP server, an issuer description field.

**What this server does about it.** It exposes **no news, no sentiment scoring,
no analyst commentary as free text, no web fetch**. This is a deliberate
omission, not a missing feature: those tools are the standard carriers, and a
brokerage server is the worst possible place to host them. Everything returned
here is either a number computed from the broker's structured data or a
short enumerated field.

Analyst *forecasts* (`get_instrument_forecast`) are the one place broker-relayed
third-party opinion enters, and it arrives as structured numeric fields —
rating enum, target price, upside percent — not prose.

**What it cannot do.** It cannot see the rest of your session. If you load a
web-browsing or news MCP server alongside this one, you have reassembled the
injection-to-trade chain inside your own client, and this server's tools are
the payload's actuators.

**The mitigation that works:** no execution token. With research-only
credentials, a successful injection gets an attacker your portfolio contents —
bad, but bounded, and not a loss of funds. Every prompt-level defence degrades
under a novel phrasing; an absent credential does not.

A secondary one: `instrument_allowlist`. Even fully armed, an agent restricted
to instruments you vetted cannot be steered into an attacker's illiquid
micro-cap.

---

## Transport security

### stdio — the default

The client spawns the server as a child process and talks over pipes. There is
**no listening socket**, so there is nothing for a local process, a browser, or
the network to connect to. The security boundary is the OS process boundary.

Keep this unless you have a specific reason not to.

### HTTP — opt in, and understand what you are doing

`transport = "http"` starts a FastMCP HTTP server. **It has no authentication
of its own.** Whatever can reach the port can call every registered tool,
including execution tools, using your token. No password, no API key, no
allowlist.

That means an open port here is not "an HTTP API" — it is an unauthenticated
remote-trading endpoint.

Two specific local risks even on loopback:

* **Any local process** running as any user that can reach `127.0.0.1:8003`
  can trade. On a shared or multi-user machine, loopback is not a boundary.
* **DNS rebinding.** A web page you visit can be made to resolve a hostname to
  `127.0.0.1` and issue requests to your local port from the browser. The token
  is never exposed to the page, but the *capability* is — the page does not need
  to steal your credential if it can borrow your server.

If you must use HTTP:

1. Bind `127.0.0.1` only — never `0.0.0.0`. The server prints a loud warning to
   stderr if you bind anything else.
2. Put a reverse proxy in front that does real authentication (mTLS, or a proxy
   that injects and verifies a bearer token).
3. Run research-only. Do not arm real trading on an HTTP deployment.
4. Prefer a container with no published port over a host-bound one.

### On "the token could be intercepted in transit"

A common and reasonable worry, worth answering precisely: **both a local server
and a remote hosted one send the token over TLS, and in both cases the
credential is encrypted on the wire.** A network observer sees the destination
host (SNI), the IP, and traffic sizes — not the token. Wire interception is not
where the meaningful difference between architectures lies.

The differences that do matter are about *who else stores the credential* and
*what the blast radius is*:

* **Local (this server):** the token is at rest in one file on your disk and in
  one process's memory. The realistic leak vectors are your machine and your
  own backups.
* **Hosted MCP (any vendor):** the token is additionally at rest in your
  editor's MCP configuration — which is routinely committed to git, synced
  between machines by settings sync, and shared in team configs. It also
  traverses an additional TLS termination point. When the host *is* the broker,
  no new trusted party is introduced; when it is a third party, one is.

In practice, a token committed to a repository in `.vscode/mcp.json` is a far
more likely incident than any wire attack. That is the reason this server reads
from `.env` and not from client config, and the reason
[docs/comparison.md](comparison.md) frames the choice the way it does.

---

## TLS and the Russian CA

T-Bank's API presents a certificate chain from the Russian national CA
("Russian Trusted Root / Sub CA", Минцифры), which is in neither `certifi` nor
gRPC's bundled roots. Without it the handshake fails with
`CERTIFICATE_VERIFY_FAILED`.

[`tls.py`](../src/tinvest_mcp/tls.py) resolves this by building a bundle of
public roots (`certifi`) plus the Russian roots, and pointing gRPC at it via
`GRPC_DEFAULT_SSL_ROOTS_FILE_PATH`.

**Be clear about what that means.** Trusting an additional root CA means that CA
could, in principle, issue a certificate for *any* host that your client would
accept. It is a real expansion of MITM surface, accepted deliberately because
the API is unreachable otherwise.

What limits it here:

* The variable is set **inside this process only**, at import time. Your system
  trust store is untouched; your browser and other applications are unaffected.
* It applies to **gRPC channels in this process** — which, in this server, go
  to exactly one host.
* An operator-provided `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` is respected and never
  overwritten, and `[tinvest].ca_bundle` lets you supply your own bundle.

**Do not install the Russian root into your operating system or browser trust
store** to make some other client work. That converts a narrow, process-scoped
decision into a machine-wide one. Process-scoped trust is the whole point of
how this is implemented.

---

## Logging, masking and the audit journal

**Never written anywhere:** API tokens, authorization headers, unmasked account
ids.

Mechanisms:

* `Settings.__repr__` → `tokens=<redacted>`. A settings object in a traceback
  or a debug print cannot leak.
* `mask()` → `200***456`. Account ids are masked at every boundary.
* `errors.py` — typed exceptions whose messages are constructed from safe
  fields; broker errors are reduced to a type name when unsure.
* `audit.py` deletes any metadata key containing `token` or `authorization`
  before writing, regardless of what a caller passed in.
* All logs go to **stderr**. On stdio, stdout is the protocol channel — a stray
  log line there corrupts the MCP stream.

**The audit journal** ([`journal.py`](../src/tinvest_mcp/journal.py)) is an
append-only JSONL file, one object per line: timestamp, action, order id,
status, and masked context. It records `submit_order`, `submit_rejected`,
`revalidation_failed`, `account_isolation_violation`, plan step execution, and
cancellations — written *before* the broker call, so an action that timed out
mid-flight still leaves a trace.

Location: `[tinvest].audit_log`, resolved against the state directory
(`$TINVEST_MCP_STATE_DIR`, else the config file's directory, else
`~/.local/state/tinvest-mcp`) — never the working directory, which an MCP
client chooses arbitrarily and which would otherwise scatter your audit trail
across several files.

Writing to it never raises into the caller's flow. By the time the record is
written the order has already reached the broker; losing a log line is strictly
better than raising over a completed trade. The trade-off is stated here so it
is not a surprise: a full disk costs you audit lines, not orders.

---

## What this does not protect against

### The caller-identity problem

**The most important honest limitation in this document.**

Several tool descriptions say the AI agent must never call `post_order` and
that execution belongs to "the trusted UI or execution controller". **MCP has
no notion of caller identity.** The server cannot distinguish a call made by
the model from a call made by a human clicking Confirm — both arrive as
identical JSON-RPC over the same channel.

So that instruction is a **prompt-level policy, not a technical gate.** A model
that ignores it, or an injection that overrides it, can call `post_order`
directly. Do not treat the wording as an enforcement boundary.

What *is* enforced, and stands regardless of who calls:

* real trading disarmed unless two config flags are set;
* **no execution credential unless you configured one** — the decisive control;
* full risk re-validation against a fresh quote at submit time;
* proposal TTL; plan-linked proposals refused; per-order and daily caps;
* execution calls accept only an id, never order terms.

The mitigation, restated because it is the whole answer: **run production
without `TINVEST_FULLACCESS_TOKEN`.** Then `post_order` fails with a
configuration error no matter who calls it, and the prompt-level policy stops
mattering.

### Everything else

* **A compromised machine is a compromised account.** `.env` is readable by
  your user. `chmod 600` stops other users, not malware running as you.
* **A malicious or buggy MCP client** is trusted by construction. It holds the
  pipe.
* **Dependencies.** `fastmcp`, `pydantic`, the T-Invest SDK and their
  transitive deps run in the same process as your token. Standard supply-chain
  exposure. This repository does not ship a lockfile — run `uv lock` in your
  own deployment if you want reproducible builds and a dependency change that
  shows up in a diff.
* **The broker side.** Rate limits, outages, order rejections, API changes and
  account restrictions are outside this server's control. Errors are surfaced
  rather than swallowed, which is all it can do.
* **Market risk is not a security property.** Every check here is arithmetic
  about order mechanics. None of it makes a trade a good idea.

---

## Incident response

**If you think a token leaked:** revoke it at
<https://www.tbank.ru/invest/settings/api/> first — before investigating.
Revocation is instant, free, and reversible only in the sense that you can
issue a new one. Then read `audit.jsonl` for unexpected `submit_order` records
and check the broker's own operation history, which is authoritative.

**If an unexpected order appeared:**

1. `get_order_state` / `list_executing_orders`, then `cancel_order` if it is
   still unfilled.
2. `audit.jsonl` — every submission is there with its masked context and
   timestamp, written before the call went out.
3. Disarm: `enable_real_trading = false`, and remove
   `TINVEST_FULLACCESS_TOKEN` from `.env`. Restart the server.
4. Then work out how it happened. The journal plus the broker's history is
   enough to reconstruct the sequence.

**If you are unsure what is armed right now:** `tinvest-mcp-doctor`.

---

## Reporting a vulnerability

Please **do not open a public issue** for a security problem in this software.
See [SECURITY.md](../SECURITY.md) for the reporting process and what to expect.

Vulnerabilities in T-Bank's own API or platform belong to T-Bank, not to this
project — report those through the broker's channels.

---

<sub>Maintained by the [Plasm](https://plasm.one) team. This document
describes the software's behaviour, not a guarantee of security. Read
[DISCLAIMER.md](../DISCLAIMER.md).</sub>
