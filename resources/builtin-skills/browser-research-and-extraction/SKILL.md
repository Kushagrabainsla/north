---
name: browser-research-and-extraction
description: "Use when navigating live web pages, scraping tabular or list data, reading articles/documentation, or asserting web UI states with browser."
domains:
  - general
  - engineering
execution:
  agent: general
  tools:
    - browser
  approval: on_mutation
  inputs:
    type: object
    properties:
      request:
        type: string
    additionalProperties: true
  outputs:
    type: object
    properties: {}
    additionalProperties: true
  success_criteria:
    - The browser profile was selected per tool call and verified before use.
    - The requested browser work completed with a deterministic assertion or clearly reported blocker.
---
# Browser Research and Structured Web Extraction

> **Use the `browser` tool to interact with dynamic web pages, harvest structured records, extract clean documentation, and verify UI states over Chrome CDP.**

## Use this when
- Scraping tables, cards, news feeds, search results, or API listings from web pages.
- Reading dense documentation or articles without navigation sidebars, ads, or headers.
- Interacting with dynamic SPAs or multi-step forms using stable numeric Accessibility Tree UIDs.
- Verifying web application UI states deterministically (`assert value`, `assert text`, `assert exists`).

## Key Action Guidelines

### 0. Choose the browser context before opening anything
Call `browser:list_profiles`. Select an enabled `profile_id` for each browser tool call using the current task and saved profile purposes. Profiles belong to tool calls, not flow inputs. If the choice is missing or ambiguous, ask which browser profile to use through `ask_user` and the central approval policy; reuse a choice already supplied for this task. Explain that existing-browser access includes logged-in sessions, cookies, open tabs and extensions. A profile name alone is not proof of attachment. Never silently switch profiles or copy cookies as a fallback.

Before promising the task can run, call `browser` with `action="preflight"` and the selected `profile_id`. Successful preflight must report `profile_verified: true`; a process listing is not proof of attachment, and preflight does not prove login. Then navigate to the site and assert that the required account is logged in. Never describe an untested browser flow as ready.

If already signed in to the expected account, reuse the session without asking the user to log in again. Saved passwords alone do not prove an active login. Ask for sign-in help only when the site requires it (expired session, MFA, or password-manager unlock). Do not read or export passwords or ask for them in chat. Connection failure is a setup blocker, not evidence that another login is needed.

For login, MFA, unlock, or browser takeover, stop browser interaction and call `ask_user` with `requires_user_action=true` as its own tool call. Explain exactly what to complete and offer "Done". The same central mode policy answers this request; no separate human-only mechanism. Pending requests pause the task. After any answer, inspect and assert the expected account or state before continuing in the same profile. An answer alone does not prove completion. If still blocked, ask for remaining help without looping on automatic answers. Never repeat completed external actions; stop on cancellation.

### 1. Structured Data Harvesting (`action="extract"`)
Use `extract` instead of reading the raw DOM. It uses structural heuristic pattern recognition (MDR/DEPTA) to parse repeating lists/tables directly into structured JSON records:
```json
{
  "action": "goto",
  "url": "https://news.ycombinator.com",
  "profile_id": "<selected-id-from-list_profiles>",
  "stealth": true
}
```
followed by:
```json
{
  "action": "extract",
  "profile_id": "<selected-id-from-list_profiles>",
  "limit": 25
}
```

### 2. Documentation & Article Reading (`action="read"`)
Use `read` to extract clean readability text/markdown from documentation or blog posts:
```json
{
  "action": "read",
  "url": "https://docs.rs/tokio/latest/tokio/",
  "profile_id": "<selected-id-from-list_profiles>"
}
```

### 3. Element Inspection and Interaction (`action="inspect"`, `click`, `fill`)
1. Run `action="inspect"` to get the Accessibility Tree with stable numeric UIDs (`n12`, `n20`).
2. Click or fill using the UID directly:
```json
{
  "action": "click",
  "uid": "n12",
  "profile_id": "<selected-id-from-list_profiles>"
}
```
3. Use `diff=True` on `inspect` after an action to see only what changed on the page rather than re-reading the entire tree.

### 4. Deterministic State Verification (`action="assert"`)
Use `assert` in testing or conductor loops:
```json
{
  "action": "assert",
  "assert_type": "text",
  "assert_condition": "contains",
  "value": "Welcome back",
  "profile_id": "<selected-id-from-list_profiles>"
}
```

## Done when
- The browser context was explicitly chosen, its dependencies and login were checked, and the requested web data or UI action completed with a deterministic assertion.
