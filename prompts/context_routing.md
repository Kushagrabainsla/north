You are routing user-supplied context into the correct document for a personal AI system.

The three documents are:
- user: facts about the user (schedule, preferences, career, background, contacts)
- judgement_rules: decision patterns and learned preferences
- north_stars: goals across time horizons (lifetime, 5-year, 1-year, 3-month, this week)

The user has provided this content:

{content}

Decide which document this belongs to and write a concise delta to append (1–3 sentences maximum).
The delta should capture only the essential new facts - do not copy the original content verbatim.
Preserve key specifics: names, numbers, dates, deadlines, and thresholds.
A vague summary is less useful than a precise one.
Reply with JSON only:
{{"document": "<user|judgement_rules|north_stars>", "delta": "<1-3 sentences>"}}
