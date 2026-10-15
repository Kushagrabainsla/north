Below are facts about one person, numbered. Each starts with the day it was learned, in brackets.

For each fact, give the days it states it is true from and until, as YYYY-MM-DD. Use only dates the fact's own text states. Resolve relative words ("next Monday", "this semester", "last year") against the day the fact was learned, and only when the text makes the day clear.

- "from": the first day the fact holds, such as a start date. Leave it null if the text does not say.
- "until": the last day the fact holds, ONLY for a fact written as true now or planned - present or future tense - whose text says when it stops: a job or internship that "is scheduled through", an offer that expires, a stay somewhere, a plan for a period. Leave it null if the text does not say.
- A fact written in the past tense is history, and history stays true forever: it gets no "until", even when it names the years it happened. "Earned a degree (2018-2022)", "worked at X from 2020 to 2022" and "signed the offer on January 24, 2026" all get null for both.
- A month counts as a stated day: "through July 2026" ends 2026-07-31, "from June 2026" starts 2026-06-01, and "mid-June" or "late May" mean the last or first day of that month the same way. A season or term with no month ("fall semester") gets null.
- A fact about the same specific thing as another fact in this list that states its dates - the same internship, job, offer, course, trip or plan - shares those dates: "the internship is full-time" ends when "the internship runs May 26 through August 14, 2026" does. Only when it is clearly the same thing, never a similar one. The tense rule still comes first: "received an offer for the internship" is past tense, so it gets null even though the internship has dates.
- A fact with no stated dates gets null for both. Never guess, never use the day it was learned as a date of its own, and never add a date the text does not contain. Null is the right answer whenever you are unsure.

{numbered}

Reply with JSON only, one entry per fact that states at least one date: {{"dates": [{{"index": <number>, "from": "<YYYY-MM-DD or null>", "until": "<YYYY-MM-DD or null>"}}]}}
