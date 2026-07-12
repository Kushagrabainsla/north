---
name: conducting-a-literature-review
description: "Use when asked to research a topic and synthesise external sources into a written review: a literature review, a survey of the state of the art, or a comparison of the leading approaches/tools/methods in a field. Triggers include 'research X', 'review the literature on X', 'survey the state of the art in X', 'what does the research say about X', 'compare the main approaches to X'. Do NOT use for personal writing (cover letters, notes, posts, emails) or tasks about the user's own life or schedule."
domains: [general]
---
# Conducting a literature review

> **Search the web and ground every claim in a real source you opened - never write a review from memory, and never narrate your process or print these steps.**

## Use this when
- You must research a subject and produce a written synthesis of what external sources say: a literature review, a state-of-the-art survey, or a comparison of the leading approaches/tools in a field.

## Do NOT use for
- Personal or business writing (cover letters, thank-you notes, emails, posts) - just write it.
- Researching a codebase/library so you can build something - that is the engineering researcher.
- A single quick fact - just `web_search` once and answer.

## Non-negotiable
- You MUST actually call `web_search` (several distinct queries) and open the strongest results with `fetch_url` **before writing anything**. A review composed from memory, with no searches and no source URLs, is a failed task - not a shortcut.
- Output **only the finished report** in the format below. Never print your steps, the words "Query Frame / Gather / Screen", or placeholder lines like "gather information here".

## How to work (do this silently, do not describe it)
1. Break the question into 3-6 sub-questions. Turn each into 1-2 plain-language `web_search` queries (no `site:`/`AND`/`OR` operators - they fail on many backends).
2. Run the searches; open the most promising, credible results with `fetch_url`. Cover 8-15+ distinct sources across viewpoints, not one restated. Note when a source is a preprint, vendor post, or opinion.
3. From each useful source, note the concrete facts - names, dates, numbers, findings - with its URL, so every claim can be cited.
4. If sub-questions remain thin or sources disagree, search again to go deeper. Stop when new searches add nothing.

## The report you output
```
# <Title>
*<today's date>*

<1-2 sentence intro framing the question.>

## <Theme / approach 1>
<Findings, each non-obvious claim cited inline like ([source](url)). Attribute conflicting views to their sources.>

## <Theme / approach 2>
...

## Tradeoffs & open questions
<Honest comparison; what is unresolved or debated; gaps in the evidence.>

## Conclusion
<2-3 paragraphs with a concrete, reasoned takeaway - not generic hedging.>

## References
<Deduplicated list of the sources you actually opened, each a hyperlink [title](url).>
```
Unless the user asked for the answer inline, `write_file` this report and tell them where it is.

## Guardrails
- Only assert what your sources support. If evidence is thin, say so; if you must speculate, label it.
- Never invent a citation, title, or URL. Every reference must be a page you actually opened.

## Done when
- A source-cited report answers the question, names the tradeoffs and open gaps, states a concrete view, and every reference resolves to a real source you searched.
