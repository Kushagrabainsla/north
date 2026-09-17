You are summarising intermediate steps of an ongoing AI agent task.
Condense the following tool calls and results into a concise bullet-point summary.
Preserve: what was accomplished, the exact list of files created or modified, files read when relevant, key facts discovered, file paths, function names, the most recent error or failing test, important data values, and what still remains to be done.
File context extracted directly from tool calls is authoritative: include it as `Files read` and `Files modified` in the summary; do not invent paths.
Omit: raw file contents, verbose outputs, redundant retries.
Max {max_words} words.

<file_context>
{file_context}
</file_context>

<history>
{history_text}
</history>
