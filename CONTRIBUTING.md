# Contributing

Thanks for considering it. This is software that can move real money, so the
bar for some parts of it is higher than for a typical library — this document
says which parts and why.

## Before you start

* **Security issues do not go in public issues.** See [SECURITY.md](SECURITY.md).
* For anything larger than a bug fix, open an issue first. A design discussion
  is cheaper than a rejected pull request.
* By contributing you agree your work is licensed under the [MIT License](LICENSE).

## Setup

```bash
git clone https://github.com/plasm-one/tinvest-mcp.git
cd tinvest-mcp
uv sync --all-extras --group dev
uv run pytest          # 213 tests, ~2s, no network, no token
uv run ruff check .
```

The suite is fully offline: every broker call goes through a fake adapter and
the audit journal is redirected to a temp file, so it is safe to run on a
machine that has production tokens in its environment. **Keep it that way** — a
test that needs a token or a network call will not be merged.

For manual exploration against the real sandbox:

```bash
uv run python -m tinvest_mcp.scripts.sandbox_smoke     # needs TINVEST_SANDBOX_TOKEN
uv run python -m tinvest_mcp.debug_api                 # Swagger on 127.0.0.1:8099
```

## The rules that are not negotiable

These exist because breaking them costs somebody money.

1. **Never log, return, or persist a token.** Not in an error message, not in
   a debug print, not in the audit journal, not in a test fixture. Account ids
   are masked with `mask()` at every boundary.
2. **`Decimal`, never `float`, for money, prices and quantities.** A price must
   be an exact multiple of the instrument's tick; binary floats are not.
3. **Every mutating broker call needs all four:** a risk path, an audit record
   written *before* the call, an idempotency key, and a mode gate.
4. **Do not widen a default.** If a change makes the shipped configuration
   permit more than it did — a higher cap, a newly allowed order type, a
   mutation reachable in sandbox that was not — say so explicitly in the PR
   description. It may still be right; it needs to be visible.
5. **`adapter.py` is the only module that talks to the SDK.** Keeping every RPC
   in one place is what makes "which token does this call use?" answerable.
6. **Do not add tools that return untrusted free text.** No news, no sentiment
   prose, no web fetch. Their absence is a security property of this server —
   see [docs/security.md § Prompt injection](docs/security.md#prompt-injection).
   Structured numeric fields from the broker are fine.
7. **Logs go to stderr.** On the stdio transport stdout is the protocol
   channel, and a stray `print()` there corrupts the MCP stream.

## Tests

Required for changes to `risk_engine.py`, `proposals.py`, `trade_plan.py`,
`services.py` and `money.py`/`converters.py` — the parts that stop money from
moving by accident. Cover the failing side, not just the passing one: a risk
check that never rejects is not a risk check.

Elsewhere, tests are strongly preferred. Bug fixes should come with the test
that would have caught the bug.

## Style

* `ruff check` and `ruff format` must pass; line length 120.
* Type hints on public functions. `from __future__ import annotations` at the
  top of every module.
* **Comments explain why, not what.** The existing code does this — a comment
  saying a limit exists because a thin book fills a market order far from the
  last print is worth keeping; one saying "set the limit" is not. Match the
  surrounding density.
* Docstrings on modules and public functions, stating the non-obvious
  constraint if there is one.
* Code and comments in English. User-facing docs may be bilingual (EN + RU) —
  if you change a fact in `README.md` or `docs/security.md`, update the `.ru`
  counterpart, or say in the PR that it needs translating.

## Tool descriptions are part of the interface

The strings in `server.py` and the docstrings in `tools.py` are what the model
reads, and they are the only instructions it gets. Treat them as code:

* State units explicitly. `avg_daily_turnover_rub`, not `turnover`.
* Say what a tool does **not** do. "Places nothing" prevents a whole class of
  agent mistake.
* Do not promise an enforcement that does not exist. "The agent must never call
  this" is a policy; if you word it as a guarantee, somebody will rely on it.

## Pull requests

* One logical change per PR.
* Describe what breaks if the change is wrong. For a risk or execution change,
  describe the scenario you are protecting against.
* Update the docs in the same PR: [configuration.md](docs/configuration.md) for
  a new key, [tools.md](docs/tools.md) for a new tool,
  [security.md](docs/security.md) if the security properties change at all.
* Add a `CHANGELOG.md` entry under *Unreleased*.
* CI must be green.

## Good first contributions

* Client setup instructions for an MCP client not yet covered in
  [installation.md](docs/installation.md).
* Translating a doc, or fixing a drifted translation.
* A test for an uncovered risk-check branch.
* A screener filter that exists in the broker API and is not yet exposed.
* Better error messages — anything that tells the model *what to change*
  instead of only what failed.

## What will not be merged

* Automated trading loops, signal generation, or anything that decides to
  trade without a human in the loop. The architecture deliberately has no such
  path, and adding one changes what this project is.
* Tools that return news, sentiment, or fetched web content (rule 6).
* A "convenience" flag that skips a confirmation gate or a risk check.
* Storing tokens anywhere other than `.env` / the process environment.
* Telemetry, analytics, or any outbound call to a host other than the broker.

## Questions

Open a discussion or an issue. If you are unsure whether something counts as a
security issue, treat it as one and email <alex@plasm.one>.

---

<sub>Maintained by the [Plasm](https://plasm.one) team.</sub>
