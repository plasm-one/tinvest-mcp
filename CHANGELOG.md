# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries that change **security properties or defaults** are marked 🔒 and listed
first within their section, because those are the ones you must read before
upgrading.

## [Unreleased]

## [0.1.0] — 2026-09-11

First public release. Extracted from an internal multi-agent monorepo into a
standalone, self-contained project.

### Added

- MCP server exposing 35 tools for T-Bank Invest (T-Invest) brokerage
  workflows: portfolio, catalogue screeners, per-instrument analytics, market
  snapshots, target allocation, trade plans, and gated order execution.
- 🔒 Two-flag gate for real trading: `[tinvest].mode = "prod"` **and**
  `enable_real_trading = true`, both required, both off by default.
- 🔒 Hard risk limits enforced at proposal time and re-checked against a fresh
  quote at submit time: per-order value, daily turnover, position weight, price
  deviation, price increment, quote age, broker max lots, trading status,
  market session.
- 🔒 Structural restrictions, all on by default: LIMIT orders only, no margin,
  no shorts, single-account isolation, instrument-class restriction, and an
  optional strict instrument allowlist.
- 🔒 Split read/execute credentials. Reads use a least-privileged research
  token; execution uses a separate full-access token. Startup checks verify
  they differ and probe the trade token's actual access with a non-mutating
  `GetAccounts` call.
- 🔒 Append-only JSONL audit journal written *before* each broker call, with
  account ids masked and token-shaped keys stripped.
- 🔒 Proposal TTL and persisted idempotency keys: a stale approval cannot
  execute, and a retry after a timeout cannot double-submit.
- 🔒 Trade plans with two confirmation gates (whole plan, then each leg),
  sequential sell-first execution, and a hard refusal of plan-linked proposals
  through `post_order`.
- Deterministic target allocation from a `risk_profile × horizon` rule table in
  code, plus a personal mandate (issuer, sector, cash floor, FX exposure) with
  size-scaled concentration caps.
- Cost-benefit verdict on rebalance plans: `NOT_WORTH_IT` when drift is under
  threshold or costs exceed the misallocation removed.
- Sell previews with НДФЛ estimates from FIFO tax lots, the 3-year ЛДВ
  exemption, and warnings for approaching exemption or corporate action.
- Currency-aware bond handling: a bond settling in rubles but denominated in a
  foreign currency has its yield reported in the denomination currency, and a
  yield sort mixing denominations is refused.
- Sandbox mode with virtual money, and a scripted end-to-end smoke test.
- `tinvest-mcp-doctor` — pre-flight diagnostics printing the resolved config
  paths, active mode, masked token presence, risk limits in effect, and the
  startup security checks. Starts no server and places nothing.
- Configuration discovery that does not assume the working directory: explicit
  env path, then cwd, then parents, then `~/.config/tinvest-mcp/`. Files the
  server writes resolve against a state directory instead of the cwd.
- Process-scoped TLS trust for the Russian national CA, applied to this
  process's gRPC channels only and never to the system trust store.
- SDK fallback: `t_tech.invest` with automatic fallback to `tinkoff.invest`.
- Documentation: installation, security model (EN + RU), configuration
  reference, tool reference, architecture, and an honest comparison with
  T-Bank's own hosted MCP server.
- 213 offline tests. No token, no network; the audit journal is redirected to a
  temp file so the suite is safe to run on a machine holding production tokens.

### Changed from the internal version

- 🔒 Risk limits and mode are now read from `config.toml` **only**, with no
  environment-variable override. A limit a stray env var can raise is not a
  limit.
- 🔒 Relative state paths resolve against the state directory rather than the
  working directory. An MCP client picks the cwd arbitrarily, which previously
  could scatter one audit trail across several files.
- 🔒 The default HTTP bind address is `127.0.0.1` (was `0.0.0.0`), and binding
  anything else now logs a loud warning naming the consequence.
- Removed the dependency on the parent monorepo's shared `mcp_common` package;
  the runtime, config sources and audit journal are now part of this project.
- Removed 25 source citations to an internal design document that does not ship
  with this project. The prose they annotated is unchanged; only the dangling
  reference is gone.
- The MCP handshake now reports this package's version. Previously a client
  displayed the FastMCP library's version as the server's.
- Corrected stale references in the agent-facing prompt and the debug API to an
  `ENABLE_TINVEST_REAL_TRADING` environment variable that the code never read —
  the setting is `[tinvest].enable_real_trading` in `config.toml`.
- Packaging moved to a `src/` layout with MIT licensing and Plasm attribution.
- No lockfile is committed: this installs from an index, and a lockfile here
  would pin versions for every consumer. Run `uv lock` in your own deployment.

[Unreleased]: https://github.com/plasm-one/tinvest-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/plasm-one/tinvest-mcp/releases/tag/v0.1.0
