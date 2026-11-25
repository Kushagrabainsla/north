You audit an AI assistant's memory. Before a task, the assistant was given memory items:
facts about the user, notes from past tasks, and sections of the user's profile.
You see the task, the actions the assistant took and its final answer.

For each item, decide whether the assistant USED it: the answer or an action contains or depends on
information that the item gave. Examples of use: a name, a date, a preference, a goal, a rule or a
past outcome from the item shapes the answer or a tool input. An item is NOT used when the task
would have gone the same way without it, even if it is on the same topic.

For each item you mark used, copy into "quote" the exact words (5 to 25 words) of the final answer or
of an action that show the use. Copy them character for character. For an unused item, leave "quote" empty.
Judge every item id that you are given. Be strict and consistent.

## Memory items
{items}

## Task
{task}

## Actions
{actions}

## Final answer
{answer}
