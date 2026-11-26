# Tools in their own environments: before and after

Step 2 of the plan to let north rebuild the Kasa tool and its page from one prompt: a tool north writes
must be able to use any package, without touching north's own environment, and a panel must be able to
read it.

## Step 1 checks (2026-11-25)

Measured before building, on this Mac:
- The installed north is a uv tool, and `uv` is on its PATH.
- A separate environment with python-kasa and the MCP SDK: 4.6 s the first time (cold cache), 0.06 to
  0.5 s after, 32.5 MB.
- The runner, through north's MCP client: start 0.3 to 1.0 s, a call under 1 ms, about 79 MB per process.
- The microphone from a tool's process failed (PortAudio -9986): music sync uses a Microphone part in the
  browser instead (PR #107).

## The check

`verify_before_after.py` runs one checkout's own `create_tool` on four drafts, with an approval layer that
says yes, in a throwaway north home, then calls each tool once.

```bash
uv run --project <main checkout> python experiments/tool_envs/verify_before_after.py <main checkout> before
uv run --project . python experiments/tool_envs/verify_before_after.py . after
```

| Case | main (37f2893) | this branch |
|---|---|---|
| A tool that needs `tabulate==0.9.0` (north does not have it) | refused: "north does not have" | created; runs 0.9.0 |
| A tool that needs `packaging==23.2` (north has 26.2) | created, but silently runs **26.2** | created; runs **23.2** |
| North's own `packaging` afterwards | 26.2 | 26.2 |
| The tool reads north's secret key file | **read** | refused by the sandbox |
| A view reads the tool's `list` call | refused: every call is a change | allowed |

Results: `results/verify_before.json` and `results/verify_after.json` (booleans and versions only).

## Costs

- The first call of a tool starts its process: about 1 s, and about 80 MB while it runs. It stops after
  10 idle minutes unless the tool keeps a job running.
- A new environment: 2.5 s with packages in uv's cache; longer the first time a package is downloaded.
- A tool can import from north only `tools.base`, `tools.models` and `config.secret_store`.
- The tool north wrote before this change (`record_media`) is not moved: it imports north's own helpers
  and writes into task folders, which the sandbox closes. It runs inside north, as before, until updated.
