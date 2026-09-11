# Configuration reference

Two stores, on purpose:

| File | Holds | Committable | Env override |
|---|---|---|---|
| `config.toml` | mode, risk limits, transport | ✅ yes, it is secret-free | only `[server]` |
| `.env` | API tokens, nothing else | ❌ never | it *is* the environment |

## Why risk limits ignore the environment

Every `[tinvest]` key is read from `config.toml` **only**. There is no
environment-variable override for any of them, and that is deliberate:

> A risk limit that a stray environment variable can raise is not a risk limit.

To change a cap you edit a file on disk and restart the server. An MCP client
that sets environment variables for the process it spawns cannot widen your
limits. `[server]` keys *do* accept env overrides, because transport is an
operational concern rather than a safety one.

## File discovery

Neither file is assumed to be in the working directory, because an MCP client
starts the server with whatever cwd it happens to have. Search order:

**`config.toml`**
1. `$TINVEST_MCP_CONFIG` — explicit path; raises if the file is missing
2. `./config.toml`
3. nearest `config.toml` in a parent directory (up to 6 levels)
4. `~/.config/tinvest-mcp/config.toml`

**`.env`** — the same, with `$TINVEST_MCP_ENV_FILE` and
`~/.config/tinvest-mcp/.env`.

**State directory** (files the server *writes*):
1. `$TINVEST_MCP_STATE_DIR`
2. the directory containing the discovered `config.toml`
3. `~/.local/state/tinvest-mcp`

`tinvest-mcp-doctor` prints all of these as resolved. A `config.toml` that was
not found is a `config.toml` whose limits are not in effect.

---

## `.env` — secrets

| Variable | Used when | Notes |
|---|---|---|
| `TINVEST_SANDBOX_TOKEN` | `mode = "sandbox"` | Virtual money. Read-only scope suffices. |
| `TINVEST_READONLY_TOKEN` | `mode = "prod"`, all reads | Issue with **read-only** scope. |
| `TINVEST_FULLACCESS_TOKEN` | `mode = "prod"`, execution only | Issue with **full access**. Leave empty to make orders structurally impossible. Must differ from the read-only token. |

Resolution: sandbox mode uses the sandbox token for both reads and writes. Prod
reads use `TINVEST_READONLY_TOKEN` and fall back to `TINVEST_FULLACCESS_TOKEN`
if no read-only token is set; prod execution uses `TINVEST_FULLACCESS_TOKEN`
and nothing else.

---

## `[server]` — transport

| Key | Env override | Default | Meaning |
|---|---|---|---|
| `transport` | `TINVEST_MCP_TRANSPORT` | `"stdio"` | `stdio` (child process over pipes, no listener) or `http` / `streamable-http` / `sse`. **`http` has no authentication** — read [security.md § Transport](security.md#transport-security). |
| `host` | `TINVEST_MCP_HOST` | `"127.0.0.1"` | HTTP only. Anything other than loopback logs a loud warning to stderr. |
| `port` | `TINVEST_MCP_PORT` | `8003` | HTTP only. |
| `log_level` | `TINVEST_MCP_LOG_LEVEL` | `"INFO"` | `DEBUG`/`INFO`/`WARNING`/`ERROR`. Always to stderr — stdout is the protocol channel. |

The generic `MCP_TRANSPORT`, `MCP_HTTP_HOST`, `MCP_HTTP_PORT` and
`MCP_LOG_LEVEL` names are accepted as aliases, for compatibility with
multi-agent deployments that already set them.

---

## `[tinvest]` — mode and account

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"sandbox"` | `sandbox` (T-Bank's virtual-money servers; real orders impossible) or `prod` (your real account). |
| `account_id` | `""` | Leave empty. Resolved automatically via `GetAccounts`; only needed to disambiguate when one token sees several accounts. |
| `enable_real_trading` | `false` | The master switch. `mode = "prod"` alone is **not** enough — this flag *and* a full-access token must both be present before a real order can leave the process. |

## `[tinvest]` — hard risk limits

Checked when a proposal is created **and** again at submit time against a fresh
quote. Defaults depend on mode, so switching to prod tightens them:

| Key | `sandbox` default | `prod` default | Meaning |
|---|---|---|---|
| `max_order_rub` | `"500000"` | `"1500"` | Maximum value of one order. |
| `max_daily_turnover_rub` | `"5000000"` | `"3000"` | Maximum total traded in a day. |
| `max_position_weight` | `"1.0"` | `"0.40"` | Maximum share of the portfolio in one position after the trade. |

> [!WARNING]
> An **explicit** value always beats the mode default. If you relax these for
> sandbox experiments and later set `mode = "prod"`, the relaxed numbers come
> with you. Delete them rather than editing when you switch, and check
> `tinvest-mcp-doctor` afterwards.

| Key | Default | Meaning |
|---|---|---|
| `confirmation_ttl_seconds` | `60` | How long an approved preview stays executable. After this it is `EXPIRED` and terminal — the price it was approved at is no longer real. |
| `max_price_deviation_pct` | `"1.0"` | Reject a limit further than this from the live quote. Catches a ruble price passed where percent-of-nominal was expected. |
| `market_data_max_age_seconds` | `10` | Refuse to price an order against a quote older than this. |

## `[tinvest]` — structural restrictions

| Key | Default | Meaning |
|---|---|---|
| `allow_market_orders` | `false` | MARKET orders have no price ceiling; in a thin book a market buy can fill far from the last print. |
| `allow_margin` | `false` | Leverage can lose more than the account holds. |
| `allow_shorts` | `false` | Short selling has unbounded loss. |
| `require_single_account_token` | `true` | Refuse to operate when the token can see more than one account, so a misrouted order cannot land in an account you did not mean to trade. |
| `allowed_instrument_types` | `["share", "bond", "etf"]` | Instrument classes the agent may trade at all. |
| `instrument_allowlist` | `[]` | Non-empty = strict allowlist of instrument uids; everything else is refused. **The tightest control in the file** — use it to pin the agent to instruments you have vetted. |

## `[tinvest]` — personal mandate

Applied to whole plans rather than single orders, and scaled by portfolio size:
a small portfolio cannot spread across many names without paying more in
commission and lot friction than the diversification is worth.

| Key | Default | Meaning |
|---|---|---|
| `rebalance_threshold_pct` | `"5"` | Deviation below which drift is reported as `hold`. |
| `mandate_max_issuer_weight` | `"0.15"` | Baseline per-issuer cap. |
| `mandate_max_sector_weight` | `"0.30"` | Per-sector cap. |
| `mandate_small_portfolio_rub` | `"100000"` | Below this, the issuer cap becomes `mandate_small_issuer_weight`. |
| `mandate_small_issuer_weight` | `"0.30"` | Issuer cap for a small portfolio. |
| `mandate_mid_portfolio_rub` | `"500000"` | Below this, the cap becomes `mandate_mid_issuer_weight`. |
| `mandate_mid_issuer_weight` | `"0.20"` | Issuer cap for a mid-size portfolio. |
| `mandate_sovereign_issuer_weight` | `"0.35"` | Higher cap for sovereign / quasi-sovereign bonds, which carry lower issuer risk. Funds are internally diversified and excluded from issuer caps entirely. |
| `mandate_max_fx_exposure` | `"0.20"` | Cap on the portfolio share denominated in a foreign currency, applied once the profile opts in via `allow_fx_linked`. Without the opt-in, any FX-denominated buy warns whatever its size. |
| `mandate_min_progress_pp` | `"1"` | A soft breach that shrinks an existing breach by at least this many percentage points counts as progress and passes instead of blocking. |

## `[tinvest]` — trade plans

| Key | Default | Meaning |
|---|---|---|
| `trade_plan_ttl_seconds` | `900` | How long a built plan stays confirmable. |
| `plan_fallback_commission_pct` | `"0.3"` | Commission assumption when `GetOrderPrice` is unavailable. |
| `plan_max_cost_to_benefit_ratio` | `"0.05"` | Above this ratio of costs to misallocation removed, the plan is `NOT_WORTH_IT` and you are told to do nothing. |
| `plan_execution_urgency` | `"fast"` | Default urgency for confirmed plan steps. `balanced` sits mid-spread and can rest unfilled for many minutes; `fast` crosses to the ask/bid and normally fills at once, paying at most the spread. |
| `market_session_check_enabled` | `true` | Block orders during clearing pauses and outside session hours, where a limit would simply rest unmatched. |
| `market_session_close_buffer_seconds` | `120` | Warn when a still-open window closes within this long. |

## `[tinvest]` — tax estimates

Rough arithmetic for a Russian private investor. **Not tax advice** — see
[DISCLAIMER.md](../DISCLAIMER.md).

| Key | Default | Meaning |
|---|---|---|
| `sell_tax_rate` | `"0.13"` | НДФЛ rate used in sell previews. |
| `ldv_warning_months` | `6` | Warn when a position is within this long of qualifying for the 3-year ЛДВ exemption — selling now forfeits it. |
| `corporate_action_warning_days` | `30` | Warn when an offer or maturity falls within this window. |

## `[tinvest]` — performance

| Key | Default | Meaning |
|---|---|---|
| `analytics_concurrency` | `4` | Parallel analytics requests when a screener computes metrics. Raising it risks broker rate limits. |
| `instrument_cache_ttl_seconds` | `300` | Cache TTL for static reference data (lot, nominal, price step). |

## `[tinvest]` — files and TLS

| Key | Env override | Default | Meaning |
|---|---|---|---|
| `audit_log` | `TINVEST_AUDIT_LOG` | `"audit.jsonl"` | Append-only journal of every brokerage action. Relative paths resolve against the state directory. |
| `investment_profile_path` | — | `"investment_profile.json"` | Saved profile and mandate. Same resolution. |
| `ca_bundle` | — | `""` | Override the TLS trust bundle for gRPC with your own. Empty = the packaged Russian roots plus `certifi`, applied to this process only. See [security.md § TLS](security.md#tls-and-the-russian-ca). |

---

## Environment variables, complete list

| Variable | Purpose |
|---|---|
| `TINVEST_SANDBOX_TOKEN` | sandbox API token |
| `TINVEST_READONLY_TOKEN` | prod read-only API token |
| `TINVEST_FULLACCESS_TOKEN` | prod full-access API token (execution) |
| `TINVEST_MCP_CONFIG` | explicit path to `config.toml` |
| `TINVEST_MCP_ENV_FILE` | explicit path to `.env` |
| `TINVEST_MCP_STATE_DIR` | where the server writes journal and profile |
| `TINVEST_AUDIT_LOG` | audit journal path (overrides `[tinvest].audit_log`) |
| `TINVEST_MCP_TRANSPORT` | `stdio` / `http` |
| `TINVEST_MCP_HOST` | HTTP bind address |
| `TINVEST_MCP_PORT` | HTTP port |
| `TINVEST_MCP_LOG_LEVEL` | log level |
| `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` | if already set, respected and never overwritten |
| `XDG_CONFIG_HOME`, `XDG_STATE_HOME` | honoured for the per-user directories |

---

## Worked configurations

### Sandbox — learning the flow

```toml
[server]
transport = "stdio"

[tinvest]
mode = "sandbox"
enable_real_trading = false
```

```dotenv
TINVEST_SANDBOX_TOKEN=t.xxx
```

### Production research — the recommended setup

The agent sees your real portfolio and real prices, and cannot place an order:
the execution path has no credential.

```toml
[server]
transport = "stdio"

[tinvest]
mode = "prod"
enable_real_trading = false
```

```dotenv
TINVEST_READONLY_TOKEN=t.xxx
# TINVEST_FULLACCESS_TOKEN deliberately absent
```

### Production with real trading — deliberately, with small caps

```toml
[server]
transport = "stdio"

[tinvest]
mode = "prod"
enable_real_trading = true

# Prod defaults, written out so they are visible. Start here; raise only
# after you have watched the flow work on money you do not mind losing.
max_order_rub = "1500"
max_daily_turnover_rub = "3000"
max_position_weight = "0.40"

allow_market_orders = false
allow_margin = false
allow_shorts = false
require_single_account_token = true

# Tightest available control: only these instruments, nothing else.
instrument_allowlist = [
  "e6123145-9665-43e0-8413-cd61b8aa9b13",  # SBER
]
```

```dotenv
TINVEST_READONLY_TOKEN=t.read_only_one
TINVEST_FULLACCESS_TOKEN=t.a_different_full_access_one
```
