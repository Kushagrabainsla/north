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
      browser_context:
        type: string
        enum: [isolated, existing]
      context_confirmed:
        type: boolean
      request:
        type: string
    required: [browser_context, context_confirmed]
    additionalProperties: true
  outputs:
    type: object
    properties: {}
    additionalProperties: true
  success_criteria:
    - The browser context was explicitly selected and confirmed before navigation.
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
Ask the user whether North should use an isolated browser or attach to their existing browser through CDP. Explain that the existing-browser option can expose logged-in sessions, cookies, open tabs, and extensions. Do not infer this choice from the fact that a site requires login. If the user already made the choice in the current request, do not ask again.

Before promising the task can run, call `browser` with `action="preflight"` for an existing CDP browser (include `connect`), or `action="status"` for an isolated browser. A successful existing-browser preflight must report `verified: true`; a generic browser process listing is not proof of attachment. Then navigate to the site and assert that the required account is logged in. Never describe an untested browser flow as ready.

### 1. Structured Data Harvesting (`action="extract"`)
Use `extract` instead of reading the raw DOM. It uses structural heuristic pattern recognition (MDR/DEPTA) to parse repeating lists/tables directly into structured JSON records:
```json
{
  "action": "goto",
  "url": "https://news.ycombinator.com",
  "browser_context": "isolated",
  "context_confirmed": true,
  "stealth": true
}
```
followed by:
```json
{
  "action": "extract",
  "browser_context": "isolated",
  "context_confirmed": true,
  "limit": 25
}
```

### 2. Documentation & Article Reading (`action="read"`)
Use `read` to extract clean readability text/markdown from documentation or blog posts:
```json
{
  "action": "read",
  "url": "https://docs.rs/tokio/latest/tokio/",
  "browser_context": "isolated",
  "context_confirmed": true
}
```

### 3. Element Inspection and Interaction (`action="inspect"`, `click`, `fill`)
1. Run `action="inspect"` to get the Accessibility Tree with stable numeric UIDs (`n12`, `n20`).
2. Click or fill using the UID directly:
```json
{
  "action": "click",
  "uid": "n12",
  "browser_context": "isolated",
  "context_confirmed": true
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
  "browser_context": "isolated",
  "context_confirmed": true
}
```

## Done when
- The browser context was explicitly chosen, its dependencies and login were checked, and the requested web data or UI action completed with a deterministic assertion.
