---
name: security-and-hardening
description: "Use when touching a security boundary - authentication/authorization, input parsing, secrets, serialization, or shell/SQL/network/file-path construction."
---
# Security and hardening

> **Never build a command, query, or path from unvalidated input.**

## Use this when
- The change handles auth, untrusted input, secrets, (de)serialization, or builds shell/SQL/URLs/file paths.

## Do NOT use for
- Logic with no external input and no privileged action.

## Procedure
1. Identify the trust boundary and exactly which input is untrusted.
2. Validate / allow-list at the boundary; reject early.
3. Parameterize SQL; pass argv lists (never a shell string); resolve and contain file paths to their root.
4. Never log, echo, or commit secrets; read them from the configured store.
5. Fail closed; check authorization on the server, not the client.
6. Add a test for the rejection/denied path.

## Done when
- Untrusted input cannot reach a command/query/path unvalidated, and the denial path is tested.
