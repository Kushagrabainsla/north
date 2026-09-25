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

# Daily news briefing

1. Search live news with web_search for each section: Tech & AI, world events, science & health, and business & markets.
2. Keep the most significant, recent, non-duplicate stories from each, with their sources and dates.
3. Follow the news_briefing agent's own section structure and format exactly.
4. Save the completed briefing with write_file at the path the agent contract requires for today's date. Never write it from memory.
5. If a section returned nothing, say so under that section rather than skipping it or inventing a story.
