---
name: security-and-hardening
description: "Use when touching a security boundary - authentication/authorization, input parsing, secrets, serialization, or shell/SQL/network/file-path construction."
---
# Security and Hardening

> **Never build a command, query, or path from unvalidated input.**

## Use this when
- The change handles auth, untrusted input, secrets, (de)serialization, or builds shell/SQL/URLs/file paths.

## Do NOT use for
- Logic with no external input and no privileged action.

## Threat Model First

Controls bolted on without a threat model are guesses. Before hardening, spend five minutes thinking like an attacker:

1. **Map the trust boundaries.** Where does untrusted data cross into your system? HTTP requests, form fields, file uploads, webhooks, third-party APIs, message queues.
2. **Name the assets.** What's worth stealing or breaking? Credentials, PII, payment data, admin actions.
3. **Run STRIDE over each boundary:**

| Threat | Ask | Typical mitigation |
|--------|-----|-------------------|
| **S**poofing | Can someone impersonate a user/service? | Authentication, signature verification |
| **T**ampering | Can data be altered in transit or at rest? | Integrity checks, parameterized queries, HTTPS |
| **R**epudiation | Can an action be denied later? | Audit logging of security events |
| **I**nformation disclosure | Can data leak? | Encryption, field allowlists, generic errors |
| **D**enial of service | Can it be overwhelmed? | Rate limiting, input size caps, timeouts |
| **E**levation of privilege | Can a user gain rights they shouldn't? | Authorization checks, least privilege |

4. **Write abuse cases next to use cases.** For each feature, ask "how would I misuse this?" — then make that your first test.

## The Three-Tier Boundary System

### Always Do (No Exceptions)
- **Validate all external input** at the boundary
- **Parameterize all database queries** — never concatenate user input into SQL
- **Encode output** to prevent XSS/injection
- **Use HTTPS** for all external communication
- **Hash passwords** with bcrypt/scrypt/argon2 (never store plaintext)
- **Read secrets from env/config** — never commit them
- **Fail closed** — check authorization on the server, not the client
- **Run dependency audit** before every release

### Ask First (Requires Human Approval)
- Adding new authentication flows or changing auth logic
- Storing new categories of sensitive data (PII, payment info)
- Adding new external service integrations
- Changing CORS or rate limiting configuration

### Never Do
- **Never commit secrets** to version control (API keys, passwords, tokens)
- **Never log sensitive data** (passwords, tokens, full credit card numbers)
- **Never trust client-side validation** as a security boundary
- **Never use `eval()` or `exec()`** with user-provided data
- **Never expose stack traces** or internal error details to users
- **Never build shell commands from string concatenation**

## Procedure

1. **Identify the trust boundary** and exactly which input is untrusted.
2. **Validate / allow-list at the boundary; reject early.**
3. **Parameterize SQL; pass argv lists (never a shell string); resolve and contain file paths to their root.**
4. **Never log, echo, or commit secrets;** read them from the configured store.
5. **Fail closed; check authorization on the server, not the client.**
6. **Add a test for the rejection/denied path.**

## Python-Specific Patterns

```python
# BAD: SQL injection via string concatenation
cursor.execute(f"SELECT * FROM users WHERE id = '{user_id}'")

# GOOD: Parameterized query
cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))

# BAD: Shell injection
os.system(f"cat {filename}")

# GOOD: Subprocess with argv list
subprocess.run(["cat", filename], check=True)

# GOOD: Path containment
from pathlib import Path
safe = Path(base_dir) / user_input
safe = safe.resolve()
if not str(safe).startswith(str(Path(base_dir).resolve())):
    raise SecurityError("Path traversal detected")
```

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "This input comes from our own frontend" | Frontend can be tampered with. Validate server-side. |
| "We're a small app, nobody will attack us" | Bots scan everything. Automated attacks don't care about your size. |
| "I'll add security later" | Later means after the breach. Build it in from the start. |
| "eval() is the easiest way to do this" | eval() is the easiest way to get hacked. Find an alternative. |
| "We need to trust this third-party API" | Even trusted APIs get compromised. Validate their responses. |

## Red Flags
- Building SQL queries with f-strings or string concatenation
- Passing user input directly to `os.system()` or shell commands
- Committing secrets or API keys to version control
- Catching exceptions and returning generic "error" messages without logging
- No rate limiting on authentication endpoints
- Storing passwords with MD5 or SHA-256 (use bcrypt/scrypt/argon2)
- Trusting client-side validation as the only security boundary

## Done when
- Untrusted input cannot reach a command/query/path unvalidated, and the denial path is tested.

## Verification
- [ ] No SQL concatenation with user input
- [ ] No shell command construction from user input
- [ ] Secrets read from env/config, not hardcoded
- [ ] Authorization checked server-side
- [ ] At least one rejection/denial path is tested
- [ ] `ruff check .` passes
