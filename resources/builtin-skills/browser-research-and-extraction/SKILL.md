---
name: browser-research-and-extraction
description: "Use when navigating live web pages, scraping tabular or list data, reading articles/documentation, or asserting web UI states with browser."
---
# Browser Research and Structured Web Extraction

> **Use the `browser` tool to interact with dynamic web pages, harvest structured records, extract clean documentation, and verify UI states over Chrome CDP.**

## Use this when
- Scraping tables, cards, news feeds, search results, or API listings from web pages.
- Reading dense documentation or articles without navigation sidebars, ads, or headers.
- Interacting with dynamic SPAs or multi-step forms using stable numeric Accessibility Tree UIDs.
- Verifying web application UI states deterministically (`assert value`, `assert text`, `assert exists`).

## Key Action Guidelines

### 1. Structured Data Harvesting (`action="extract"`)
Use `extract` instead of reading the raw DOM. It uses structural heuristic pattern recognition (MDR/DEPTA) to parse repeating lists/tables directly into structured JSON records:
```json
{
  "action": "goto",
  "url": "https://news.ycombinator.com",
  "stealth": true
}
```
followed by:
```json
{
  "action": "extract",
  "limit": 25
}
```

### 2. Documentation & Article Reading (`action="read"`)
Use `read` to extract clean readability text/markdown from documentation or blog posts:
```json
{
  "action": "read",
  "url": "https://docs.rs/tokio/latest/tokio/"
}
```

### 3. Element Inspection and Interaction (`action="inspect"`, `click`, `fill`)
1. Run `action="inspect"` to get the Accessibility Tree with stable numeric UIDs (`n12`, `n20`).
2. Click or fill using the UID directly:
```json
{
  "action": "click",
  "uid": "n12"
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
  "value": "Welcome back"
}
```

## Done when
- The requested web data is retrieved with minimal token overhead, or UI actions/assertions complete successfully.
