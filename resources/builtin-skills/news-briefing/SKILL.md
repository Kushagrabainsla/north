---
name: news-briefing
description: "Use when compiling and saving the scheduled daily news briefing across Tech & AI, world events, science & health, and business & markets."
domains:
  - general
execution:
  agent: news_briefing
  tools:
    - web_search
    - write_file
  inputs:
    type: object
    properties: {}
    additionalProperties: false
  outputs:
    type: object
    properties: {}
  approval: on_mutation
  success_criteria:
    - A markdown briefing file was saved under the news handoff directory for today's date, covering all four sections with sourced, dated stories.
---

Run the daily news briefing: search live news across Tech & AI, world events,
science & health, and business & markets, synthesize the most significant,
recent, non-duplicate stories from each with their sources, and save the
completed briefing with write_file at the path your agent contract requires for
today's date. Follow the agent's own section structure and format exactly - do
not write the file from memory or skip a section that returned no results;
note that instead.
