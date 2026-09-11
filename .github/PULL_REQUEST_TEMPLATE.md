# What this changes

<!-- One paragraph. What breaks if this change is wrong? -->

## Type

- [ ] Bug fix
- [ ] New read-only tool or filter
- [ ] Change to risk checks, proposals, plans or execution
- [ ] Documentation
- [ ] Build / CI / packaging

## Checklist

- [ ] `make check` passes (`ruff check` + `pytest`)
- [ ] Tests added for the change, covering the **failing** side as well as the passing one
- [ ] No token, account id, or other secret appears in the diff — including in a test fixture
- [ ] `Decimal` used for every money, price and quantity value (never `float`)
- [ ] Logs go to stderr; no `print()` to stdout
- [ ] Docs updated in this PR: `docs/configuration.md` for a new key, `docs/tools.md` for a new tool, `docs/security.md` if security properties changed
- [ ] `CHANGELOG.md` entry added under *Unreleased*

## If this touches the broker or the risk engine

- [ ] The mutating path has: a risk check, an audit record written **before** the call, an idempotency key, and a mode gate
- [ ] Every new RPC goes through `adapter.py` — nothing else imports the SDK
- [ ] Tool descriptions state the units explicitly and say what the tool does **not** do

## Does this widen a default?

<!-- A higher cap, a newly permitted order type, a mutation newly reachable,
     a tool that returns text from an external source. It may well be right —
     it needs to be visible. Say "no" if not. -->

## Security impact

<!-- "None" is a valid answer, but think before writing it. If this changes
     what an attacker or a misbehaving model could achieve, say how.
     Vulnerabilities in existing code go to alex@plasm.one, not here. -->
