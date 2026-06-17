# Security Policy

north handles personal user data: context documents, ledger entries, scheduled tasks, and voice audio in transit to OpenRouter. The local data store at `~/.north/` (including `private.md`) never leaves the machine. The shared secret in `~/.north/secret.key` protects the local Orchestrator REST API and notification callbacks.

## Reporting a vulnerability

If you find a security issue, **please do not file a public GitHub issue.** Instead:

1. Open a private security advisory via the GitHub Security tab on the repository, or
2. Email the maintainer directly via the address associated with their GitHub account.

Expected response: acknowledgment within 7 days, triage within 14 days.

## In scope

- Bypassing the `X-North-Secret` header check on the Orchestrator REST API (README Sections 6.8 and 9.1).
- Forging notification callbacks to `localhost:8001/callback`.
- Reading `~/.north/private.md` or `~/.north/secret.key` from outside the local user account.
- LLM prompt injection that causes exfiltration of context documents.
- Tampering with the Ledger such that an entry can be modified, deleted, or read by an unauthorized party.

## Out of scope

- Vulnerabilities in the underlying OS, Python interpreter, or upstream third-party dependencies - report those to their respective maintainers.
- Issues that require root or physical access on the local machine (north trusts the local user account).
- Network-level attacks against `localhost:8000` or `localhost:8001` - these are local-only ports. Do not expose them publicly.
