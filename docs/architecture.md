# Architecture

## The premise

The model is a **research assistant, not a trader**. Everything follows from
that: reads are open and cheap, and every path that touches money narrows to a
single choke point that re-checks arithmetic against fresh data.

Two rules shape the code:

1. **Code computes, the model explains.** Target allocations, yields, drift,
   tax estimates, lot conversions, price hints — all deterministic functions.
   The tool descriptions tell the model to present these numbers, never to
   recompute or adjust them. A model that invents a percentage is a model whose
   output cannot be audited.
2. **Order terms are immutable after preview.** `post_order` and
   `execute_plan_step` accept **only an id**. The instrument, direction,
   quantity and price were fixed when the preview was built, so there is no
   parameter for a model to change between approval and execution.

## Layers

```
┌────────────────────────────────────────────────────────────────────┐
│ server.py            MCP tool registry + the agent playbook prompt │
├────────────────────────────────────────────────────────────────────┤
│ tools.py             the MCP boundary: signatures, validation,     │
│                      error translation. No business logic.         │
├────────────────────────────────────────────────────────────────────┤
│ services.py          orchestration: research, proposals, plans,    │
│                      execution. Knows the sequence.                │
├──────────────┬──────────────┬──────────────┬───────────────────────┤
│ risk_engine  │ proposals    │ trade_plan   │ allocation            │
│ every check  │ TTL +        │ multi-leg +  │ deterministic rule    │
│ as one fn    │ idempotency  │ two gates    │ table                 │
├──────────────┴──────────────┴──────────────┴───────────────────────┤
│ converters · money · fx · pricing_hints · session_calendar         │
│ Decimal arithmetic, quote units, FX, price hints, MOEX clock       │
├────────────────────────────────────────────────────────────────────┤
│ adapter.py           the ONLY module that talks to the broker SDK  │
├────────────────────────────────────────────────────────────────────┤
│ sdk.py               single import surface (t_tech.invest, with a  │
│ tls.py               tinkoff.invest fallback) + gRPC trust roots   │
└────────────────────────────────────────────────────────────────────┘
   cross-cutting: schemas.py (the contract the model sees) ·
                  config/ (settings + discovery) · journal.py + audit.py ·
                  errors.py (typed, never carries credentials)
```

**Why `adapter.py` is the only SDK caller.** One module holds every RPC, which
means one place decides which token a call uses (`trade=True` or not), one place
to add a retry, and one place to audit when checking whether a credential can
leak. `services.py` never sees a raw SDK object.

**Why `schemas.py` is large.** Its pydantic models *are* the interface the model
sees — field names, enums, docstrings and examples all end up in the tool
schema. Making a unit explicit in a field name (`price_quote_unit`,
`avg_daily_turnover_rub`) is cheaper than hoping the model infers it.

## Money arithmetic

`Decimal` everywhere. Never `float`. Quotations from the SDK arrive as
`units` + `nano` integer pairs and are converted exactly; a binary float would
introduce rounding into a price that must be a multiple of the instrument's
tick.

Three unit traps the code handles explicitly, because each has a real cost:

* **Bond prices are percent of nominal.** `99.09` is 99.09% of face value.
  `price_quote_unit` states the unit on every snapshot, and `PRICE_DEVIATION`
  catches a ruble price passed in its place.
* **`PostOrder` takes money per unit, not the quote.** `_unit_money_price()`
  converts at submit time, using the same price `GetOrderPrice` validated.
* **A bond can settle in rubles while being denominated in a foreign
  currency** — yuan issues are the usual case: `currency = "rub"`,
  `nominal_currency = "cny"`. Its yield is a yield *in that currency*, so
  `list_bonds` refuses a yield sort that would mix denominations, and
  `avg_daily_turnover_rub` exists so liquidity is compared in one unit.

## The execution gates

### Single order

```
create_order_proposal            ── read-only, places nothing
  │  full risk battery
  │  status = READY_FOR_CONFIRMATION | RISK_REJECTED
  │  TTL starts (confirmation_ttl_seconds)
  ▼
[ human reads the preview and decides ]
  ▼
post_order(proposal_id)          ── the only mutation
  ├─ reject if plan-linked           (must go through the plan's own gates)
  ├─ reject if expired               (approved price is no longer real)
  ├─ reject if not READY             (terminal states are terminal)
  ├─ reject if real trading disabled (point-of-no-return check)
  ├─ re-fetch instrument, quote, portfolio
  ├─ re-run the ENTIRE risk battery  → fail = RISK_REJECTED + audit record
  ├─ persist idempotency key         BEFORE the RPC
  ├─ write audit record              BEFORE the RPC
  └─ send LIMIT order
```

The re-validation in the middle is the important part. A preview that passed
against a quote from two minutes ago does not get to execute on the strength of
that; the checks run again against the book as it is now.

### Trade plan

```
create_trade_plan(items)         ── read-only; sells sequenced first
  │  per leg: commission, accrued interest, FIFO/ЛДВ tax estimate
  │  allocation before/after vs target
  │  cost_benefit: WORTH_IT | NOT_WORTH_IT
  ▼
confirm_trade_plan(plan_id)      ── GATE 1: the whole basket, once
  ▼
  ┌──── per leg, sequential, sell-first ────┐
  │  preview_plan_step(plan_id)   fresh card, re-checks risk
  │  execute_plan_step(plan_id)   GATE 2: this leg only
  └─────────────────────────────────────────┘
  ▼
get_plan_state                   a stalled or failed leg pauses everything after it
  ▼
verify_trade_plan                fresh snapshot + cost report
```

Nothing auto-continues. A partially filled, cancelled or rejected leg blocks
the remainder rather than pressing on, because the plan's arithmetic assumed
the earlier legs completed.

Plan-linked proposals are refused by `post_order`, which closes the obvious
route around Gate 1.

## Risk engine

[`risk_engine.py`](../src/tinvest_mcp/risk_engine.py) is a list of small
functions, each returning a named `RiskCheck` with a code, a verdict, a
severity and a human-readable message. `evaluate()` runs them all and
`all_passed()` decides.

The shape matters for two reasons: a failing preview tells the model *which*
code failed so it can fix the order rather than guess, and adding a rule means
adding one function and one test rather than editing a conditional.

Severity separates blocking from advisory. `LDV_WARNING` and
`CORPORATE_ACTION_SOON` pass on purpose — they are judgement calls that belong
to the person, and the tool descriptions require showing them.

Full list of codes: [security.md § Defence layers](security.md#defence-layers).

## State

No database. Three things persist, all as files:

| What | Where | Why a file is enough |
|---|---|---|
| Audit journal | `audit.jsonl` in the state dir | Append-only, one JSON object per line. Sequential writes under a process lock; nothing needs indexed queries. |
| Investment profile | `investment_profile.json` | One document, single-profile MVP. Atomic writes via temp file + `os.replace`. |
| Proposals and plans | in memory, with TTL | They expire in seconds to minutes. Surviving a restart would be a bug, not a feature: an approval from before a restart should not execute after it. |

Relative paths resolve against the **state directory**, never the working
directory — an MCP client picks the cwd arbitrarily, and anchoring there would
scatter one audit trail across several files.

## Configuration

Settings resolve once into a frozen `Settings` dataclass
([`config/env.py`](../src/tinvest_mcp/config/env.py)), cached with
`lru_cache`. Non-secret values come from `config.toml`, tokens from `.env`,
both discovered rather than assumed
([`config/sources.py`](../src/tinvest_mcp/config/sources.py)).

`Settings.__repr__` is overridden to print `tokens=<redacted>`, so the object
is safe in a traceback.

The asymmetry worth knowing: `[server]` keys accept environment overrides,
`[tinvest]` keys do not. Transport is operational; risk limits are a safety
boundary, and a boundary a stray env var can move is not one.

## Error handling

[`errors.py`](../src/tinvest_mcp/errors.py) defines typed exceptions —
configuration, authentication, permission, not-found, rate-limit,
data-unavailable, order-rejected, real-trading-disabled. Two rules:

* **Never carry credentials.** Messages are built from safe fields; an
  unfamiliar broker error degrades to its type name rather than being
  interpolated whole.
* **Never swallow a failure.** A broker rejection surfaces as a rejection. An
  agent that cannot tell "refused" from "succeeded" will retry a filled order.

`tools.py` translates these into MCP errors with messages written to be useful
to a model: what failed, which check, what to change.

## Testing

213 tests, fully offline. Broker calls go through a fake adapter, and the audit
journal is redirected to a temp file by an autouse fixture — so `pytest` is safe
on a machine holding production tokens.

```bash
uv run pytest
```

Coverage is concentrated where mistakes cost money: `test_risk_engine.py` (602
lines), `test_trade_plan.py` (692), `test_services.py` (1605). Changes to
`risk_engine.py`, `proposals.py` or `trade_plan.py` need tests — they are the
parts that stop money from moving by accident.

## Extending it

**A new read tool.** Add the RPC to `adapter.py`, the shape to `schemas.py`,
the orchestration to `services.py`, the signature to `tools.py`, and register it
in `server.py`. Keep the field names unit-explicit.

**A new risk check.** One function in `risk_engine.py` returning a `RiskCheck`,
wired into `evaluate()`, plus a test for the passing and failing sides. Pick
`severity="warning"` only if a human should decide rather than be blocked.

**A new config knob.** Add the field to `Settings`, read it in `get_settings()`,
document it in [configuration.md](configuration.md), and add it to
`config.toml.example` with a comment saying *why* the default is what it is.

**Anything that reaches the broker with a mutation** needs: a risk path, an
audit record written before the call, an idempotency key, and a mode gate. If a
change adds a fifth 🔴 tool, it belongs in the security review, not just the
code review.
