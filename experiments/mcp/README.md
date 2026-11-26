# Bucket 10: MCP (#75, #51)

Measured 2026-11-25 on the owner's Mac, against `origin/main` at 442a3b8.

## #75: use the official MCP SDK

**Why it matters now.** The MCP spec of 2026-07-28 removed the `initialize` handshake. The spec's own table says
a client that only speaks the handshake "fails" against a server that only speaks 2026-07-28. north's client
announced 2024-11-05 and spoke only that handshake, only over stdio.

**Probe** (`e75_reach.py`): each client starts or reaches 9 servers, lists the tools, and calls `echo` and `big`
(256 KB) or, on DeepWiki, `read_wiki_structure` on a public repository. The owner's configured server (Gmail) gets
start and tool list only, no calls.

| Server | main (`mcp/`) | branch (`mcp_client/`, SDK) |
|---|---|---|
| Python SDK 2.3 server, default (serves both eras) | yes | yes |
| Python SDK 2.3 server, 2026-07-28 only | **no**: "the initialize handshake is not accepted" | yes |
| TypeScript SDK 1.32 server | yes | yes |
| TypeScript server 2.4 | yes | yes |
| Hand-written 2024-11-05 server that answers unknown methods | yes | yes |
| Hand-written 2024-11-05 server, silent on unknown methods | yes | yes, **10 s later** (the SDK's `server/discover` probe waits 10 s) |
| DeepWiki (hosted) through `mcp-remote` as a stdio command | yes | yes |
| DeepWiki (hosted) by `url` | **no**: `mcp.json` has no field for it | yes |
| Owner's Gmail server (`workspace-mcp` 2.0.1) | yes | yes |
| **Reached** | **7 / 9** | **9 / 9** |

Results: `results/e75_reach_north_main.json`, `results/e75_reach_north_branch.json`.

Other findings:
- Every official SDK server today still accepts the old handshake, and so do DeepWiki and Gmail (both also answer
  2026-07-28). `mcp-remote`'s own notes (0.16.7) say "every server in the wild today is a legacy one". So the
  break is not here yet; it comes with the first server that drops the handshake.
- The SDK refuses a server whose `serverInfo` has no `version` (the spec requires it). north's two test servers
  had none; they were fixed.
- Real use: in 2,178 runs from 2026-09-05 to 2026-11-25, MCP tools were called 5 times (all Gmail; the 2 that
  failed were the `workspace` argument bug, fixed since).
- The SDK adds 5 packages to north's environment (`mcp`, `mcp-types`, `opentelemetry-api`, `pyjwt`,
  `sse-starlette`), not 9: north already had the rest.

Not tested: OAuth sign-in to a hosted server (it needs a person at a browser). `mcp-remote` documents it.

## #51: a north MCP server and plugin for the owner's own `claude` sessions

**Question.** Would north's recall, put into a session at its start, help the owner's own sessions?

**Replay** (`e51_recall_for_sessions.py`): the 183 sessions the owner started from the desktop app (entrypoint
`claude-desktop`, 108 project folders; the 955 `sdk-cli` sessions are north's own runs). For each, north's
production recall (15 facts with its cutoff, 3 episodes) ran on the first message the owner typed, on a copy of
north's stores. Haiku 5.5 (medium effort) judged each recalled item: did the session need it (with a quote from
the message), and did the session's own memory (`~/.claude/CLAUDE.md` and the project's memory folder) already
hold it.

| | Result |
|---|---|
| Items recalled | 1,904 |
| Items the session needed | **0** |
| Sessions with at least one needed item | **0 / 183** |
| Recall time in the process (median / p95 / max) | 42 / 163 / 226 ms |

**Control** (`--control`): the same judge, given each session's own project notes as the "recalled" items (80
sessions with a memory folder, 2,207 notes), marked 81 notes needed, in 42 of 80 sessions. So the judge does say
"needed" when an item is needed; north's recall gave it nothing to say yes to. (The notes favour the control: they
were written for those projects, some after the session.)

The first message is the best case for recall: a SessionStart hook sees no message at all. Results:
`results/e51_recall_for_sessions.json`, `results/e51_control.json`.

## Setup for the probes

Private text (messages, facts, verdicts) stays in `~/.north/experiments/mcp`. Only counts go to `results/`.

```bash
uv venv ~/.north/experiments/mcp/sdkvenv --python 3.12
uv pip install --python ~/.north/experiments/mcp/sdkvenv/bin/python mcp==2.3.0
npm install --prefix ~/.north/experiments/mcp/node @modelcontextprotocol/sdk@1.32.2 zod mcp-remote@0.16.7
npm install --prefix ~/.north/experiments/mcp/node2 @modelcontextprotocol/server@2.4.0 zod
```
