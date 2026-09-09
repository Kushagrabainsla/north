You are compacting one tool result that an AI agent has already read, so it can be dropped from the agent's working context without losing what the task needs.

The agent's task:
<task>
{task}
</task>

The full result of calling `{tool_name}`:
<output>
{output}
</output>

Write a summary of the output, for the agent, in the context of that task.

Keep: anything in the output that bears on the task - exact values, names, paths, identifiers, counts, versions, error messages, and any part that answers the task directly. Quote exact strings rather than describing them; "the config sets a 30s timeout" is useless if the agent needed `timeout_seconds: 30`.

If the output contains something that directly answers the task, state it in full and first. This is the whole point: a summary that describes what the output was about, without carrying what it said, is worse than useless because it reads like the answer is not there.

Omit: formatting, boilerplate, repeated rows, and anything that only mattered while the tool was running.

Write the summary only - no preamble, no "here is a summary".

Max {max_words} words.
