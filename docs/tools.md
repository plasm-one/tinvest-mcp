# Tool reference

35 tools. Column **W** marks the ones that can change something: 🟢 read-only,
🟡 writes local state only (no broker mutation), 🔴 reaches the broker with a
mutation.

Only **five** tools are 🔴, and none of them can act unless real trading is
armed *and* an execution token is configured. See
[security.md](security.md#defence-layers).

Arguments are listed as `name` (required) or `name?` (optional).

---

## Health

| Tool | W | Args | What it returns |
|---|---|---|---|
| `status` | 🟢 | — | Liveness plus the active `mode`, `real_trading_enabled`, and `sdk_package`. The first call an agent should make: it tells the agent whether it is in sandbox or prod. |

---

## Accounts and portfolio

| Tool | W | Args | What it returns |
|---|---|---|---|
| `get_accounts` | 🟢 | — | Accounts visible to the token, with `research_access_level`, `planning_available`, `execution_available` and `execution_access_level`. The last two are verified independently through the *trade* token — read-only research access does not imply the account is read-only. |
| `get_portfolio_summary` | 🟢 | — | `total_value`, `cash`, `asset_allocation` (shares/bonds/funds/cash as fractions), `positions[]`, `concentration.largest_position_weight`. |
| `get_portfolio_analytics` | 🟢 | `include_bond_metrics?`, `top_n?` | Allocation by class, sector, currency and issuer; top-position concentration; weighted yield; bond-portfolio duration. With a saved profile it also returns **drift**: per asset class current vs target %, `amount_to_trade`, `action`, plus `mandate_violations` with the excess to shed. |
| `get_operations` | 🟢 | `from_date?`, `to_date?`, `operation_types?`, `instrument_uid?`, `cursor?`, `limit?`, `include_canceled?` | Operation history: trades, commissions, coupons, dividends, withheld tax. Cursor-paginated. |

## Catalogue screeners

One screener per asset class, so rows carry only fields that mean something for
that class — a bond row has no empty dividend column.

All three return `last_price` for free. Computed analytics
(`historical_return_pct`, `volatility_annual_pct`, `max_drawdown_pct`, and the
class-specific yields) are **`null` unless you ask**: pass
`include_analytics=true`, or sort by an analytic key, which enables it
automatically up to `analytics_limit`. `null` here means "not computed for this
call", never "no market data".

| Tool | W | Args | Notes |
|---|---|---|---|
| `list_bonds` | 🟢 | `currency?`, `denomination_currency?`, `api_trade_available?`, `qualified_only?`, `risk_level?`, `max_maturity_years?`, `max_duration_years?`, `min_avg_daily_turnover_rub?`, `apply_mandate?`, `sort_by?`, `descending?`, `include_analytics?`, `analytics_limit?`, `limit?` | Nominal, maturity, `risk_level`, coupons/year, structure flags (floating / amortising / perpetual / subordinated), accrued interest, and with analytics `current_yield_pct`, `ytm_pct`, `duration_years`. A yield sort across mixed denomination currencies is **refused** — a CNY yield and a RUB yield are not comparable numbers. |
| `list_shares` | 🟢 | `currency?`, `api_trade_available?`, `qualified_only?`, `sector?`, `pays_dividends?`, `min_avg_daily_turnover_rub?`, `apply_mandate?`, `sort_by?`, `descending?`, `include_analytics?`, `include_forecast?`, `include_fundamentals?`, `analytics_limit?`, `limit?` | Sector, share type, dividend flag; with analytics `dividend_yield_pct`. Shares have no broker `risk_level` — risk is read from volatility and drawdown. |
| `list_etfs` | 🟢 | `currency?`, `api_trade_available?`, `qualified_only?`, `sector?`, `focus_type?`, `min_avg_daily_turnover_rub?`, `apply_mandate?`, `sort_by?`, `descending?`, `include_analytics?`, `include_fees?`, `analytics_limit?`, `limit?` | `focus_type` (equity / fixed_income / mixed_allocation / alternative_investment), rebalancing frequency, share count, commission; `include_fees` adds TER. |
| `search_instruments` | 🟢 | `query`, `instrument_types?`, `api_trade_available?`, `qualified_only?`, `limit?` | Text search by ticker, name or ISIN when you already know what you want. |

`apply_mandate=true` applies the saved profile's mandate as a filter: excluded
sectors dropped, bond risk capped, duration bounded by the horizon.
`min_avg_daily_turnover_rub` is the liquidity floor — a high-yield bond you
cannot exit without paying several percent on the spread is a trap, not a yield.

## Per-instrument detail

| Tool | W | Args | What it returns |
|---|---|---|---|
| `get_instrument_details` | 🟢 | `instrument_uid` | `lot`, `min_price_increment`, currency, tradability flags (`api_trade_available`, `buy_available`, `sell_available`, `short_enabled`, `qualified_investor_only`), exchange, class code, nominal and maturity for bonds, ISIN, sector, country of risk, trading status. **Required for a correct order** — lot size and price step come from here. |
| `get_instrument_analytics` | 🟢 | `instrument_uid` | Return / risk: `historical_return_pct`, `volatility_annual_pct`, `max_drawdown_pct` (daily candles, ~1 year). Bonds add `risk_level`, `current_yield_pct`, next coupon, coupons/year, nominal, maturity. Shares add `dividend_yield_pct` and last dividend. `notes[]` carries the caveats. |
| `get_instrument_forecast` | 🟢 | `instrument_uid` | Analyst consensus: rating enum, consensus target with min/max band, upside %, per-analyst targets. Structured fields, not prose. |
| `get_instrument_fundamentals` | 🟢 | `instrument_uid` | Market cap, P/E, P/B, P/S, EV/EBITDA, ROE/ROA/ROIC, margins, debt, dividend yield, growth. |
| `get_etf_details` | 🟢 | `instrument_uid` | TER and fees, benchmark index, strategy, tracking error, management style. |
| `get_bond_schedule` | 🟢 | `instrument_uid` | Full upcoming coupon schedule plus lifecycle events: call, offer, maturity. |
| `get_market_snapshot` | 🟢 | `instrument_uid` | **Required before any order.** `price_quote_unit` (bonds `pct_of_nominal`, shares/ETFs `currency`), last/bid/ask, `liquidity.spread_pct`, `is_fresh` / `age_seconds`, and `buy_price_hints`: `patient` (bid), `balanced` (mid), `fast` (ask). Refused outright if the quote is older than `market_data_max_age_seconds`. |

> [!IMPORTANT]
> **Bond prices are percent of nominal.** `last_price = 99.09` means 99.09% of
> face value, not 99 rubles. Passing a ruble money price as `limit_price` is the
> single most expensive mistake available here; `PRICE_DEVIATION` is the check
> that catches it. Prefer `urgency` over a hand-written price.

## Investment profile and target allocation

| Tool | W | Args | What it does |
|---|---|---|---|
| `propose_target_allocation` | 🟢 | `risk_profile`, `horizon` | Deterministic bonds/equity/cash split from a **rule table in code** — `conservative\|moderate\|aggressive` × `short\|medium\|long`. Computes nothing with a model, so the numbers are reproducible and auditable. Places and saves nothing. |
| `save_investment_profile` | 🟡 | `risk_profile`, `horizon`, `custom_allocation?`, `excluded_sectors?`, `max_bond_risk_level?`, `min_cash_pct?`, `max_issuer_weight_pct?`, `max_sector_weight_pct?`, `allow_fx_linked?`, `max_fx_exposure_pct?`, `notes?` | Persists the **user-confirmed** profile and mandate to the state directory. Overwrites the previous one. Nothing reaches the broker. |
| `get_investment_profile` | 🟢 | — | The saved profile, or `null` if the allocation stage was never completed. |

The mandate saved here is what `apply_mandate` filters by and what the
plan-level checks (`PLAN_ISSUER_LIMIT`, `PLAN_ALLOCATION_MANDATE`, …) test
against. `allow_fx_linked` is the explicit opt-in for foreign-currency
denomination: without it, any FX-denominated buy raises a warning whatever its
size.

## Order proposals — previews that place nothing

| Tool | W | Args | What it does |
|---|---|---|---|
| `create_order_proposal` | 🟢 | `instrument_uid`, `direction`, `order_type`, `quantity_lots`, `limit_price?`, `urgency?`, `rationale?`, `user_request_id?` | Risk-checked **preview**. Returns `status` (`READY_FOR_CONFIRMATION` / `RISK_REJECTED`), every `risk_checks[]` entry with its code and verdict, `limit_price`, `estimated_total`, `portfolio_impact`, `price_selection`, and for sells `tax_impact` (НДФЛ estimate) with `LDV_WARNING` / `CORPORATE_ACTION_SOON`. Expires after `confirmation_ttl_seconds`. **Places nothing.** |
| `validate_trade_plan` | 🟢 | `steps` | Simulates a multi-order sequence and checks the **resulting portfolio** against the saved mandate: allocation band, cash floor, issuer and sector caps, per-step cash and position feasibility. No costs or taxes; needs a saved profile. |
| `create_trade_plan` | 🟢 | `items`, `user_request_id?` | Builds the whole rebalance basket from buy/sell intents (lots **or** money amounts, converted to whole lots at current prices with accrued interest). Sequences sells before buys; prices every leg with commission, НКД and a FIFO/ЛДВ tax estimate; shows allocation before and after against target; returns a `WORTH_IT` / `NOT_WORTH_IT` cost-benefit verdict that will tell you to do nothing when drift is under threshold or costs eat the benefit. **Places nothing.** |
| `preview_plan_step` | 🟢 | `plan_id`, `urgency?` | Fresh execution card for the next immutable leg. Takes only `plan_id` — no instrument, quantity or price. Re-runs remaining-plan and per-order checks. |

Note that `preview_plan_step` and the execution tools take **only an id**. The
order's terms were fixed when the plan was built and cannot be edited at
execution time, so a model that constructs different terms has nowhere to put
them.

## Plan lifecycle

| Tool | W | Args | What it does |
|---|---|---|---|
| `confirm_trade_plan` | 🟡 | `plan_id`, `acknowledge_warnings?` | **Gate 1.** Marks a still-valid plan `CONFIRMED` after the user approved the whole basket. Places no order. |
| `execute_plan_step` | 🔴 | `plan_id` | **Gate 2.** Submits exactly the next leg, after that leg's execution card was confirmed. Idempotent, sell-first, never auto-continues. |
| `get_plan_state` | 🟢 | `plan_id`, `refresh?` | Per-leg state: `FILLED` / `SUBMITTED` / `PENDING` / `SKIPPED` / … An in-flight or failed step pauses the plan and blocks every later leg. |
| `cancel_trade_plan` | 🔴 | `plan_id` | Cancels the active plan order and marks the remainder `SKIPPED`. |
| `verify_trade_plan` | 🟢 | `plan_id` | Post-execution snapshot: fresh portfolio, allocation, drift and a cost report for a completed or cancelled plan. |
| `log_recommendation` | 🟡 | `plan_id`, `rationale`, `alternatives_considered?` | Appends why these instruments were preferred to the recommendation journal. Audit trail, not a broker call. |

## Order execution

| Tool | W | Args | What it does |
|---|---|---|---|
| `post_order` | 🔴 | `proposal_id` | Submits a still-valid proposal. Re-validates the **entire** risk battery against a fresh quote, refuses plan-linked proposals, persists an idempotency key before the RPC, sends a LIMIT order. |
| `get_order_state` | 🟢 | `proposal_id` | Order status. `SUBMITTED` with `lots_executed=0` = accepted and resting in the book (normal outside session hours, or with a limit away from the market). `FILLED` = done. |
| `list_executing_orders` | 🟢 | `refresh?` | Every proposal at the execution stage, refreshing broker state by default. Use instead of polling ids one by one. |
| `cancel_order` | 🔴 | `proposal_id` | Cancels an unfilled order. |

> [!WARNING]
> The tool descriptions tell the model that `post_order` and `execute_plan_step`
> belong to a trusted UI and that the agent must never call them. **MCP has no
> caller identity, so that is a prompt-level policy, not a technical gate.** The
> controls that hold regardless of who calls are the real-trading flags, the
> absence of an execution token, and the submit-time re-validation. See
> [security.md § caller identity](security.md#the-caller-identity-problem).

## Sandbox only

| Tool | W | Args | What it does |
|---|---|---|---|
| `open_sandbox_account` | 🔴 | `name?` | Creates a sandbox brokerage account. Refused outside `sandbox` mode. |
| `sandbox_pay_in` | 🔴 | `account_id`, `amount`, `currency?` | Funds a sandbox account with virtual money. Refused outside `sandbox` mode. |

These are 🔴 by mutation, not by risk: sandbox money is not money.

---

## The intended sequence

The server's own instructions push the model through this order, and the risk
engine makes most of it mandatory in practice.

```
status
  │
  ├─ get_investment_profile ──► propose_target_allocation ──► save_investment_profile
  │                                              (user confirms the numbers)
  ▼
get_portfolio_summary            cash and positions, before anything
  ▼
get_portfolio_analytics          drift vs target, mandate violations
  ▼
list_bonds / list_shares / list_etfs / search_instruments
  ▼
get_instrument_analytics + get_instrument_details        yield, risk, lot, tick
  ▼
get_market_snapshot              REQUIRED: unit, freshness, price hints
  ▼
create_order_proposal            or create_trade_plan for a basket
  ▼
      ┌──────── user reads the preview and decides ────────┐
      ▼                                                    ▼
post_order                              confirm_trade_plan → preview_plan_step
  ▼                                                    → execute_plan_step (per leg)
get_order_state ──► FILLED                                 ▼
  ▼                                                  get_plan_state
get_portfolio_summary                                      ▼
                                                     verify_trade_plan
```

## Restricting the surface

There is no config flag to unregister tools. If you want the execution tools to
be unreachable, the effective approach is to remove the credential they need:

```dotenv
# .env — production, research only
TINVEST_READONLY_TOKEN=t.xxx
# TINVEST_FULLACCESS_TOKEN deliberately absent
```

`post_order` and `execute_plan_step` then fail with a configuration error no
matter who calls them, because there is nothing to authenticate the order with.
This is stronger than hiding the tools, and it is the recommended production
configuration.
