---
name: adding-a-north-tool
description: "Use when North must create or update an atomic executable capability because no registered tool can perform a required action."
intents: [create-tool]
domains: [general, engineering]
---
# Adding a tool to north

> A tool is one executable action. Reuse first, create only for a real capability gap, and never report success until the tool is loaded and tested.

## Use this when
- north needs a new capability, or you must extend/create a `Tool` under `tools/`.

## Do NOT use for
- Something an existing tool already does, or a one-off that `write_file`/`bash` handles directly.

## Procedure
1. Search the live tool catalog and existing MCP capabilities. Extend an existing tool when it already owns the action. A different prompt or workflow is not a reason for another tool.
2. State the capability gap in one sentence, then define concrete inputs, outputs, dependencies, failure modes, and whether each action reads or changes local or external state.
3. Ask only for missing authority or choices that change the contract. Never infer credentials, account scope, browser profile, external recipients, or permission to make consequential changes.
4. For browser work, ask whether to attach to the user's existing browser through CDP or use an isolated browser. Explain that the existing browser can expose its sessions, cookies, tabs, and extensions, then verify the chosen environment before building around it.
5. Create a `Tool` subclass with a unique snake_case `name`, retrieval-focused `description`, strict `parameters_schema`, and recoverable `ToolOutput` failures. Mutation classification must reflect the requested action, not merely the tool class.
6. Keep final actions such as submit, send, purchase, publish, delete, or deploy behind an enforceable approval boundary. A sentence in a description is not a safety control.
7. Add tests for success, invalid input, unavailable dependencies, refused authority, and the consequential-action boundary. A representative `create_tool` test call executes real code, so choose the smallest safe input and keep external mutation behind approval.
8. Use `create_tool` to create or update a candidate only after the contract and code are complete. Call `validate`, then `test` with representative inputs. Activate only when both apply to the current source and the user explicitly confirms activation.

## Done when
- The active tool is discovered in the live registry, its schema is valid, its permissions are truthful, representative success and failure tests pass, and the original user outcome can now use it.
- Say "tool candidate created" when verification is incomplete. Creating the file is never the user's completed outcome.
