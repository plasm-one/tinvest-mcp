# Security policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** This software
holds brokerage credentials and can place orders; a public report is a working
exploit notice for everyone running it.

Report privately, whichever is easier:

1. **GitHub Security Advisories** — the *Security* tab → *Report a
   vulnerability*. Preferred: it keeps the report, the fix and the disclosure in
   one place.
2. **Email** — <alex@plasm.one>

### What to include

* What an attacker can achieve — read the portfolio, place an order, exfiltrate
  a token, bypass a risk check.
* Steps to reproduce, or a proof of concept.
* Affected version or commit.
* Your configuration **without secrets** — `config.toml` is designed to be
  secret-free, so it is safe to attach. **Never send a token**, not even a
  revoked one, and never send `.env`.

### What to expect

| | |
|---|---|
| Acknowledgement | within 3 working days |
| Initial assessment | within 7 working days |
| Fix for a credential-exposure or unauthorised-order issue | prioritised over everything else |
| Credit | your name in the advisory and `CHANGELOG.md`, unless you prefer otherwise |

We will tell you honestly if we think a report is not a vulnerability, and why.
We would rather receive ten reports that turn out to be by-design than miss the
one that is not.

## In scope

* Token or credential exposure — in logs, errors, the audit journal, tool
  output, tracebacks, or anywhere else.
* Bypassing an execution gate: placing an order without a valid non-expired
  proposal, without real trading armed, or around a plan's confirmation gates.
* Bypassing a risk check: exceeding `max_order_rub`, the daily turnover cap,
  the position-weight cap, the price-deviation check, or the instrument
  allowlist.
* Executing a proposal past its TTL, or replaying one.
* Order placement against an account the token should not reach.
* Anything that lets an attacker reaching the HTTP transport do more than the
  documented (already significant) "call any tool" — that limitation is known
  and documented, not a vulnerability.
* Injection through tool inputs — path traversal on a config or state path,
  code execution, unsafe deserialisation.
* Dependency vulnerabilities with a plausible exploitation path in this
  software's use of them.

## Out of scope

These are documented properties, not bugs. Please read
[docs/security.md](docs/security.md) before reporting one — but if you believe
a case is worse than documented, report it.

* **MCP has no caller identity.** The server cannot distinguish a call from the
  model from a call from a human. That `post_order` can be called by the model
  is a documented limitation; the mitigation is to run without an execution
  token. See [§ caller identity](docs/security.md#the-caller-identity-problem).
* **The HTTP transport has no authentication.** Documented, warned about at
  startup, and the reason stdio is the default.
* **A compromised host exposes the token.** `.env` is readable by your user.
* **A malicious MCP client** is trusted by construction — it holds the pipe.
* **Trusting the Russian CA root** for this process's gRPC channels is a
  deliberate, documented, process-scoped decision.
* Prompt-injection *susceptibility of the model itself*. Concrete injection
  vectors introduced **by this server's own tool output** are in scope and we
  want to hear about them.
* Market losses, bad trades, broker outages, rate limits.
* Findings from an automated scanner with no demonstrated impact.

## Vulnerabilities in T-Bank's API or platform

Out of scope for this project — report those through the broker's own channels.
We can neither fix nor coordinate disclosure for someone else's service.

## Supported versions

This is a beta project. Security fixes go to the latest release on `main`.
There are no backports to older tags; upgrade.

## Disclosure

Coordinated. We will agree a timeline with you, aiming to publish an advisory
within 90 days of the report or on the day a fix ships, whichever is sooner.
If a vulnerability is being exploited, we will publish a mitigation
immediately, before a full fix.

---

<sub>Maintained by the [Plasm](https://plasm.one) team. This is an
unofficial client, not affiliated with T-Bank — see
[DISCLAIMER.md](DISCLAIMER.md).</sub>
