"""FastMCP server exposing Tinkoff / T-Invest brokerage operations as LLM tools."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastmcp import FastMCP

from . import __version__, tools
from .config.env import get_settings
from .proposals import get_store
from .runtime import register_health_tool, run_mcp_server
from .sdk import SDK_PACKAGE
from .startup_checks import run_startup_checks
from .trade_plan import get_plan_store

mcp = FastMCP(
    "tinvest-mcp",
    # Without this, a client reports FastMCP's own version as the server's.
    version=__version__,
    instructions=(
        "Tinkoff / T-Invest BROKERAGE (stocks, bonds, ETFs). Follow the purchase playbook below.\n\n"
        "=== TOKEN SCOPE (never conflate these capabilities) ===\n"
        "Production reads intentionally use a least-privileged research token; production does NOT mean "
        "that every API call should carry the full-access token. get_accounts.research_access_level therefore "
        "describes only that research token. Trade-plan creation is read-only and is available when "
        "planning_available=true. Only execution_available and execution_access_level describe whether "
        "the trusted UI/controller can submit confirmed orders through the separate trade token. Never "
        "tell the user the account is read-only, or that a plan cannot be created, merely because "
        "research_access_level=ACCOUNT_ACCESS_LEVEL_READ_ONLY.\n\n"
        "=== PURCHASE PLAYBOOK (mandatory order) ===\n"
        "PHASE 1 — RESEARCH (read-only, always allowed):\n"
        "  1. get_portfolio_summary — cash and positions BEFORE any trade.\n"
        "  2. list_bonds / list_shares / list_etfs OR search_instruments — pick instrument_uid.\n"
        "  3. get_instrument_analytics + get_instrument_details — yield/risk, lot, price step, nominal.\n"
        "  4. get_market_snapshot(uid) — REQUIRED before any order:\n"
        "     • price_quote_unit: bonds=pct_of_nominal (% of face, NOT rubles); shares/ETFs=currency.\n"
        "     • buy_price_hints: patient (bid), balanced (mid, DEFAULT), fast (ask, quickest fill).\n"
        "     • is_fresh / age_seconds: quote age (informational).\n\n"
        "PHASE 2 — PROPOSE (read-only preview, does NOT place an order):\n"
        "  5. create_order_proposal(instrument_uid, BUY|SELL, LIMIT, quantity_lots, urgency=balanced|fast|patient).\n"
        "     • PREFER urgency over guessing limit_price (BUY uses buy_price_hints; SELL mirrors them:\n"
        "       patient=join ask, balanced=mid, fast=cross bid).\n"
        "     • Only pass limit_price if the user explicitly named a price.\n"
        "     • Check all_passed=true and status=READY_FOR_CONFIRMATION.\n"
        "     • If RISK_REJECTED: read risk_checks where passed=false (see table below).\n"
        "     • Show user: instrument, lots, limit_price, estimated_total, portfolio_impact, price_selection.warning.\n"
        "     • SELL previews also return tax_impact (НДФЛ estimate) and may include severity=warning\n"
        "       checks (LDV_WARNING = selling forfeits the 3-year tax exemption soon;\n"
        "       CORPORATE_ACTION_SOON = offer/maturity close). They pass but MUST be shown to the user.\n\n"
        "PHASE 3 — EXECUTE (UI/execution-controller owned; the AI NEVER submits an order):\n"
        "  6. After explicit UI confirmation, the trusted UI/controller invokes post_order(proposal_id).\n"
        "     The agent must not invoke post_order itself under any circumstances.\n"
        "  7. get_order_state(proposal_id) — poll until FILLED, or explain SUBMITTED.\n"
        "  8. cancel_order(proposal_id) — if user wants to abort an unfilled order.\n"
        "  9. get_portfolio_summary — verify position after FILLED.\n\n"
        "ORDER STATUS MEANINGS:\n"
        "  SUBMITTED + lots_executed=0 → accepted, waiting in book (common on weekends or limit below ask).\n"
        "  FILLED → bought; cash decreased, position appears in portfolio.\n"
        "  CANCELLED / REJECTED → no trade.\n\n"
        "BOND PRICING: last_price 99.09 = 99.09% of nominal. NEVER pass ruble money price as limit_price.\n"
        "URGENCY: fast=cross ask (buy quickly in session); balanced=default; patient=join bid (cheaper, slower).\n\n"
        "=== CURRENCY: A YIELD IS ONLY A YIELD IN ITS OWN CURRENCY ===\n"
        "Some MOEX bonds SETTLE in rubles but are DENOMINATED in a foreign currency — yuan issues\n"
        "are the usual case: currency='rub' while nominal_currency='cny'. Consequences:\n"
        "  • ytm_pct / current_yield_pct are yields in nominal_currency (check yield_currency /\n"
        "    fx_linked). A CNY 8.7% next to a RUB 15.7% is NOT '7 points worse' — the gap is the\n"
        "    market's CNY/RUB expectation. Holding it is a currency bet on top of the credit bet,\n"
        "    and the quoted volatility does not include the exchange-rate swing.\n"
        "  • Compare and rank yields ONLY inside one yield_currency (list_bonds rejects a\n"
        "    yield sort that would mix them — pass denomination_currency).\n"
        "  • Use avg_daily_turnover_rub, not avg_daily_turnover, to judge liquidity.\n"
        "  • Never present such a bond as plain 'corporate diversification'. Ask whether the\n"
        "    currency exposure is wanted; only then save_investment_profile(allow_fx_linked=true).\n\n"
        "RISK_REJECTED — common fixes:\n"
        "  PRICE_DEVIATION → use urgency or hints; bonds use % not rubles.\n"
        "  SUFFICIENT_CASH / MAX_ORDER_VALUE → reduce quantity_lots.\n"
        "  POSITION_EXISTS (SELL) → not enough sellable lots; re-check get_portfolio_summary.\n"
        "  CURRENCY_EXPOSURE / PLAN_CURRENCY_EXPOSURE (warnings, non-blocking) → the trade adds\n"
        "    foreign-currency exposure; show it to the user and get an explicit decision.\n\n"
        "RULES: LIMIT orders only (BUY or SELL, no shorts); no account id or token from user; "
        "The trusted execution call carries only proposal_id (or plan_id for a plan), never raw order fields.\n"
        'Real trading needs [tinvest].mode="prod" AND enable_real_trading=true in config.toml; default is sandbox.\n'
        "Sandbox setup: open_sandbox_account → sandbox_pay_in if no cash.\n\n"
        "=== CATALOGUE SCREENERS (list_bonds / list_shares / list_etfs) ===\n"
        "Each screener returns catalogue rows with last_price (always) plus an optional analytics tier.\n"
        "Analytics fields — historical_return_pct, volatility_annual_pct, max_drawdown_pct; "
        "bonds also current_yield_pct/ytm_pct; shares dividend_yield_pct — are NULL unless you "
        "explicitly request them:\n"
        "  • pass include_analytics=true when comparing candidates (recommended), OR\n"
        "  • sort_by return / volatility / drawdown (auto-enables analytics, capped by analytics_limit).\n"
        "NULL does NOT mean missing market data — it means analytics was not computed for that call.\n"
        "Set analytics_limit to your result limit when using include_analytics. "
        "For one instrument, use get_instrument_analytics(uid).\n\n"
        "Other research tools: get_instrument_forecast, get_instrument_fundamentals, get_bond_schedule, "
        "get_operations (trade/coupon/dividend history), get_portfolio_analytics (allocation & concentration) — "
        "use when comparing candidates or reviewing portfolio, not required for every purchase.\n\n"
        "=== TARGET ALLOCATION (advisory stage — BEFORE picking instruments) ===\n"
        "1. get_investment_profile — check for a saved profile first; null = stage not done.\n"
        "2. propose_target_allocation(risk_profile, horizon) — deterministic rule table "
        "(conservative|moderate|aggressive × short|medium|long → bonds/equity/cash %). "
        "The code produces the numbers; you only explain them (bonds=anchor, equity=growth, cash=buffer). "
        "NEVER invent or adjust percentages yourself.\n"
        "3. After the user explicitly confirms → save_investment_profile(...). "
        "Pass custom_allocation ONLY if the user demanded different percentages (must sum to 100).\n\n"
        "=== GAP ANALYSIS (after a profile is saved) ===\n"
        "get_portfolio_analytics.drift compares the live portfolio against the saved allocation: "
        "per asset class — current %, target %, deviation, amount_to_trade (positive=buy, negative=sell) "
        "and action (hold when |deviation| < rebalance_threshold_pct). mandate_violations lists "
        "issuer/sector weights above mandate limits with the excess value to shed. "
        "Translate these numbers for the user and highlight the biggest gaps; NEVER recompute them yourself. "
        "drift=null means the allocation stage was not completed. Amounts ignore commissions and lot rounding.\n\n"
        "=== REBALANCE PLAN (stage 6 — the basket) ===\n"
        "Turn the chosen candidates + gap analysis into ONE plan with create_trade_plan(items):\n"
        "each item = instrument_uid, action BUY|SELL, and quantity_lots OR amount (money;\n"
        "the code converts amounts to whole lots at current prices, НКД included).\n"
        "The code sequences SELL legs first (proceeds fund the buys) and returns per leg:\n"
        "commission, НКД, estimated НДФЛ for sells (FIFO tax lots from the operation history,\n"
        "3-year ЛДВ exemption), running cash_after; plus allocation before/after vs target and\n"
        "a cost_benefit verdict — WORTH_IT or NOT_WORTH_IT (drift under threshold or costs eat\n"
        "the benefit → honestly recommend doing nothing). Show the user the WHOLE plan, not\n"
        "isolated orders; NEVER recompute the numbers. status=READY_FOR_CONFIRMATION → ask the\n"
        "user. Two mandatory gates follow: (1) the UI calls confirm_trade_plan(plan_id) only\n"
        "after the user approves the WHOLE plan; (2) preview_plan_step(plan_id) returns a fresh\n"
        "execution card, then the UI calls execute_plan_step(plan_id) only after that card is\n"
        "confirmed. The agent NEVER confirms or executes on the user's behalf. Plan-linked\n"
        "proposals cannot bypass this flow through post_order.\n"
        "Execution is sequential and SELL-first. Poll get_plan_state(plan_id); SUBMITTED, partial,\n"
        "cancelled or rejected pauses the plan and blocks all later legs. Never auto-continue.\n"
        "After COMPLETED, call verify_trade_plan(plan_id) and present allocation/drift/costs;\n"
        "log_recommendation(plan_id, ...) records why these instruments were preferred.\n"
        "For a quick mandate-only re-check of an edited sequence use validate_trade_plan(steps):\n"
        "it simulates the RESULTING portfolio against the personal mandate (allocation band,\n"
        "cash floor, issuer/sector caps, per-step cash/position feasibility) without costs/taxes.\n"
        "If a check fails: rework the plan (fewer lots, another instrument, reorder) — do NOT\n"
        "override the numbers.\n\n"
        "=== CANDIDATE SCREENING (fill each underweight slot) ===\n"
        "For every drift item with action=buy, screen 2-3 candidates and present them with a short rationale:\n"
        "1. Screen with apply_mandate=true (profile mandate: excluded sectors, bond risk cap, "
        "duration ≤ horizon) + min_avg_daily_turnover_rub (liquidity floor; e.g. 1000000).\n"
        "2. LIQUIDITY IS CRITICAL: a high-yield bond you can buy but cannot exit without losing "
        "several % on the spread is a trap. Check avg_daily_turnover on screener rows and "
        "liquidity.spread_pct in get_market_snapshot before proposing anything.\n"
        "3. Bonds: prefer duration_years ≤ horizon; compare survivors by ytm_pct. "
        "ETFs: compare TER (include_fees) and focus_type against the target class.\n"
        "4. Compare finalists via get_instrument_analytics / get_etf_details; let the user pick, "
        "or pick yourself WITH the reasoning stated if the user delegated the choice."
    ),
)


def _health_extra() -> Mapping[str, Any]:
    settings = get_settings()
    return {
        "mode": settings.mode,
        "real_trading_enabled": settings.real_trading_enabled,
        "sdk_package": SDK_PACKAGE,
    }


# -- read / research --------------------------------------------------------
mcp.tool(
    name="get_accounts",
    description=(
        "List brokerage accounts with token-scoped capabilities. research_access_level is "
        "RESEARCH-TOKEN "
        "only; planning_available says whether a read-only trade plan can be built; "
        "execution_available and execution_access_level are independently verified through the "
        "trade token. Never infer global read-only access from research_access_level."
    ),
    tags={"tinvest", "accounts", "read"},
)(tools.get_accounts)
mcp.tool(
    name="get_portfolio_summary",
    description="STEP 1 (before trade) and STEP 9 (after FILLED): cash, positions, concentration.",
    tags={"tinvest", "portfolio", "read"},
)(tools.get_portfolio_summary)
mcp.tool(
    name="get_operations",
    description=(
        "Brokerage operation history: trades, commissions, coupons, dividends, withheld taxes. "
        "Paginated via cursor. No tax-lot accounting."
    ),
    tags={"tinvest", "operations", "read"},
)(tools.get_operations)
mcp.tool(
    name="get_portfolio_analytics",
    description=(
        "Portfolio aggregates: allocation by class/sector/currency/issuer, top-position concentration, "
        "weighted yield, bond-portfolio duration. When an investment profile is saved, also returns "
        "drift (GAP ANALYSIS): per asset class current vs target %, amount to buy/sell, plus "
        "issuer/sector mandate violations — all computed by code."
    ),
    tags={"tinvest", "portfolio", "analytics", "read"},
)(tools.get_portfolio_analytics)
mcp.tool(
    name="search_instruments",
    description="Search the T-Invest catalogue by text (ticker/name/ISIN).",
    tags={"tinvest", "instruments", "read"},
)(tools.search_instruments)
_SCREEN_ANALYTICS_NOTE = (
    " Analytics fields (historical_return_pct, volatility_annual_pct, max_drawdown_pct) are null "
    "unless include_analytics=true or sort_by is return/volatility/drawdown."
)

mcp.tool(
    name="list_bonds",
    description=(
        "Screen bonds: nominal/maturity/risk_level/coupons + last_price; filter by risk/maturity, "
        "max_duration_years (ladder: duration ≤ horizon), min_avg_daily_turnover_rub (liquidity), "
        "apply_mandate=true (profile mandate: excluded sectors + risk cap + duration default); "
        "sort by risk/maturity/ytm/yield."
        + _SCREEN_ANALYTICS_NOTE
        + " Bonds: current_yield_pct/ytm_pct/duration_years also need include_analytics=true."
    ),
    tags={"tinvest", "instruments", "bonds", "read"},
)(tools.list_bonds)
mcp.tool(
    name="list_shares",
    description=(
        "Screen shares: sector/share_type/pays_dividends + last_price; filter by sector/dividends, "
        "min_avg_daily_turnover_rub (liquidity), apply_mandate=true (drop excluded sectors); "
        "sort by dividend_yield/volatility/return/price."
        + _SCREEN_ANALYTICS_NOTE
        + " dividend_yield_pct also needs include_analytics=true. "
        "include_forecast adds analyst consensus (rating + target + upside)."
    ),
    tags={"tinvest", "instruments", "shares", "read"},
)(tools.list_shares)
mcp.tool(
    name="list_etfs",
    description=(
        "Screen ETFs/funds: focus_type/commission + last_price; filter by focus_type "
        "(equity|fixed_income|mixed_allocation|alternative_investment) / sector, "
        "min_avg_daily_turnover_rub (liquidity), apply_mandate=true (drop excluded sectors); "
        "sort by commission/focus/released/return/volatility/price."
        + _SCREEN_ANALYTICS_NOTE
        + " Pass include_analytics=true when comparing fund candidates."
    ),
    tags={"tinvest", "instruments", "etfs", "read"},
)(tools.list_etfs)
mcp.tool(
    name="get_instrument_details",
    description="Full normalized parameters for one instrument.",
    tags={"tinvest", "instruments", "read"},
)(tools.get_instrument_details)
mcp.tool(
    name="get_instrument_analytics",
    description="Yield & risk signals: historical return/volatility/drawdown, bond risk_level & yield, share dividend yield.",
    tags={"tinvest", "instruments", "analytics", "read"},
)(tools.get_instrument_analytics)
mcp.tool(
    name="get_instrument_forecast",
    description="Analyst consensus rating (buy/hold/sell), consensus target price + min/max band, upside %, and per-analyst targets. Best for shares.",
    tags={"tinvest", "instruments", "forecast", "read"},
)(tools.get_instrument_forecast)
mcp.tool(
    name="get_instrument_fundamentals",
    description="Company financials & ratios: market cap, P/E, P/B, P/S, EV/EBITDA, ROE/ROA/ROIC, margins, debt, dividend yield, growth. Best for shares.",
    tags={"tinvest", "instruments", "fundamentals", "read"},
)(tools.get_instrument_fundamentals)
mcp.tool(
    name="get_etf_details",
    description="Extended ETF metadata: TER/fees, benchmark/index, strategy, tracking error, management style.",
    tags={"tinvest", "instruments", "etfs", "read"},
)(tools.get_etf_details)
mcp.tool(
    name="get_bond_schedule",
    description="Full upcoming coupon schedule + lifecycle events (call/offer, maturity) for one bond.",
    tags={"tinvest", "instruments", "bonds", "read"},
)(tools.get_bond_schedule)
mcp.tool(
    name="get_market_snapshot",
    description=(
        "STEP 4 before buying: market data + buy_price_hints. "
        "Returns price_quote_unit, last/bid/ask, is_fresh, and patient/balanced/fast limit prices. "
        "Bonds: prices are % of nominal, NOT rubles."
    ),
    tags={"tinvest", "market-data", "read"},
)(tools.get_market_snapshot)

# -- investment profile / target allocation ---------------------------------
mcp.tool(
    name="propose_target_allocation",
    description=(
        "ALLOCATION STAGE (before picking instruments): deterministic bonds/equity/cash % "
        "for risk_profile × horizon from a rule table. Code decides the numbers; "
        "explain them to the user and get confirmation, then save_investment_profile."
    ),
    tags={"tinvest", "profile", "allocation", "read"},
)(tools.propose_target_allocation)
mcp.tool(
    name="save_investment_profile",
    description=(
        "Persist the USER-CONFIRMED risk profile, horizon and target allocation. "
        "Call only after the user agreed to the proposed allocation. Overwrites the previous profile."
    ),
    tags={"tinvest", "profile", "allocation"},
)(tools.save_investment_profile)
mcp.tool(
    name="get_investment_profile",
    description=(
        "Read the saved investment profile + target allocation (null if the allocation stage "
        "was never completed). Check this before advising on instruments."
    ),
    tags={"tinvest", "profile", "allocation", "read"},
)(tools.get_investment_profile)

# -- propose ----------------------------------------------------------------
mcp.tool(
    name="create_order_proposal",
    description=(
        "STEP 5: risk-checked PREVIEW only (no order placed). BUY or SELL. "
        "Use urgency=balanced|fast|patient (preferred) or explicit limit_price. "
        "Requires all_passed=true before showing preview to user. "
        "SELL adds tax_impact (НДФЛ estimate) and LDV/corporate-action warnings. "
        "Bonds: limit in % of nominal."
    ),
    tags={"tinvest", "proposal", "read"},
)(tools.create_order_proposal)
mcp.tool(
    name="validate_trade_plan",
    description=(
        "PLAN-LEVEL preview: simulate a multi-order rebalance sequence and check the "
        "RESULTING portfolio against the saved personal mandate (allocation band, cash "
        "floor, issuer/sector caps, per-step cash/position feasibility). Places nothing. "
        "Requires a saved investment profile. Run BEFORE proposing a multi-order plan."
    ),
    tags={"tinvest", "proposal", "plan", "read"},
)(tools.validate_trade_plan)
mcp.tool(
    name="create_trade_plan",
    description=(
        "STAGE 6: build the full rebalance BASKET from buy/sell intents (lots or money "
        "amounts → whole lots). Sequences SELL before BUY, prices every leg with "
        "commission, НКД and FIFO/ЛДВ tax estimates, previews allocation before/after "
        "vs target, runs plan-level risk checks and returns a WORTH_IT / NOT_WORTH_IT "
        "cost-benefit verdict. Places nothing; requires a saved investment profile."
    ),
    tags={"tinvest", "proposal", "plan", "read"},
)(tools.create_trade_plan)

mcp.tool(
    name="confirm_trade_plan",
    description=(
        "GATE 1, UI-owned: confirm the WHOLE still-valid plan by plan_id. Places no order. "
        "The agent must never infer or perform this confirmation."
    ),
    tags={"tinvest", "plan", "confirmation"},
)(tools.confirm_trade_plan)
mcp.tool(
    name="preview_plan_step",
    description=(
        "GATE-2 read-only preview for the next immutable plan leg. Input is plan_id plus "
        "optional urgency; no instrument/quantity/price. Re-checks remaining-plan and order risk."
    ),
    tags={"tinvest", "proposal", "plan", "read"},
)(tools.preview_plan_step)
mcp.tool(
    name="execute_plan_step",
    description=(
        "GATE 2, UI-owned: after the current execution card is explicitly confirmed, submit "
        "exactly the next leg by plan_id. Idempotent; SELL-first; never auto-continues."
    ),
    tags={"tinvest", "orders", "plan", "execute"},
)(tools.execute_plan_step)
mcp.tool(
    name="get_plan_state",
    description=(
        "Poll plan execution state: FILLED/SUBMITTED/PENDING/SKIPPED/etc per leg. "
        "In-flight or failed steps pause the plan and block later orders."
    ),
    tags={"tinvest", "orders", "plan", "read"},
)(tools.get_plan_state)
mcp.tool(
    name="cancel_trade_plan",
    description="User-confirmed cancellation: cancel active plan order and SKIP the remainder.",
    tags={"tinvest", "orders", "plan", "execute"},
)(tools.cancel_trade_plan)
mcp.tool(
    name="verify_trade_plan",
    description=(
        "STAGE 10: fresh portfolio/analytics snapshot plus allocation, drift and cost report "
        "for a completed or cancelled plan."
    ),
    tags={"tinvest", "plan", "analytics", "read"},
)(tools.verify_trade_plan)
mcp.tool(
    name="log_recommendation",
    description="Append why the plan's instruments were preferred to the recommendation audit journal.",
    tags={"tinvest", "plan", "audit"},
)(tools.log_recommendation)

# -- execute (gated) --------------------------------------------------------
mcp.tool(
    name="post_order",
    description=(
        "UI/EXECUTION-CONTROLLER ONLY after explicit preview confirmation; the AI must never call it. "
        "Input: proposal_id from create_order_proposal. Idempotent."
    ),
    tags={"tinvest", "orders", "execute"},
)(tools.post_order)
mcp.tool(
    name="get_order_state",
    description=(
        "STEP 7: poll order status after post_order. "
        "SUBMITTED=queued (lots_executed=0); FILLED=bought. Repeat until terminal."
    ),
    tags={"tinvest", "orders", "read"},
)(tools.get_order_state)
mcp.tool(
    name="list_executing_orders",
    description=(
        "List all proposals at the execution stage (SUBMITTED/PARTIALLY_FILLED/…). "
        "Refreshes broker state by default. Use instead of polling each proposal_id."
    ),
    tags={"tinvest", "orders", "read"},
)(tools.list_executing_orders)
mcp.tool(
    name="cancel_order",
    description="Cancel unfilled order for a proposal (SUBMITTED, not FILLED).",
    tags={"tinvest", "orders", "execute"},
)(tools.cancel_order)

# -- sandbox-only -----------------------------------------------------------
mcp.tool(
    name="open_sandbox_account",
    description="Create a sandbox brokerage account (sandbox mode only).",
    tags={"tinvest", "sandbox"},
)(tools.open_sandbox_account)
mcp.tool(
    name="sandbox_pay_in",
    description="Fund a sandbox account with virtual money (sandbox mode only).",
    tags={"tinvest", "sandbox"},
)(tools.sandbox_pay_in)

register_health_tool(
    mcp,
    name="status",
    description="Check if the T-Invest MCP server is running and responsive.",
    message="tinvest-mcp is up",
    extra=_health_extra,
)


def main() -> None:
    settings = get_settings()
    get_store(settings.confirmation_ttl_seconds)  # init proposal store with configured TTL
    get_plan_store(settings.trade_plan_ttl_seconds)  # init whole-plan TTL/execution store
    # Log startup security findings (advisory; execution-time gates are the hard stop).
    for result in run_startup_checks(settings):
        flag = "ok" if result.ok else "WARN"
        print(f"[tinvest-mcp][startup][{flag}] {result.name}: {result.message}")
    run_mcp_server(mcp)


if __name__ == "__main__":
    main()
