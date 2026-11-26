# Bucket 7: coding agents (#67, #63, #65)

The scripts run the product code of a checkout given on the command line. "Before" is the code without
this change (the Claude backend there is the same as on `main`). The results hold probe names and yes/no
only: no tokens, paths or output.

```bash
.venv/bin/python experiments/coding_probes/e65_claude_reach.py before <checkout without the change>
.venv/bin/python experiments/coding_probes/e65_claude_reach.py after .
.venv/bin/python experiments/coding_probes/e65_claude_tools.py before <checkout without the change>
.venv/bin/python experiments/coding_probes/e65_claude_tools.py after .
NORTH_LIVE_CLAUDE=1 NORTH_LIVE_CODEX=1 .venv/bin/python -m pytest tests/live/test_coding_probes_live.py -q
```

## How much the coding path is used

North's own task records hold 2 `coding_agent` calls from the user's tasks (2026-10-24 and 2026-11-14).
The first worked. The second failed because neither agent could run: `claude --version` did not answer,
and Codex was logged out. Codex's own logs show 116 runs on north's coding path since 2026-10-03, but
these are the harness benchmark, live tests, smoke runs and the nightly check.

## #65: probes as live tests

`tests/live/test_coding_probes_live.py` runs four probes on each agent: a planted secret in a home
folder no list names, in a plan run and in an edit run (the gate rejects every card); and the internet
(`curl https://example.com`), in both modes.

First run, on the code of `main`: Codex blocked all four probes, and Claude blocked both edit probes. A
Claude plan run read the planted secret.

**`e65_claude_reach.py`.** One Claude Code plan run per probe, under north's settings, with a token
nobody could guess.

| A plan run reaches | before | after |
|---|---|---|
| A file in an unlisted home folder, Read tool | yes | **no** |
| A file in an unlisted home folder, `cat` | yes | **no** |
| A file in an unlisted home folder, Grep tool | yes | **no** |
| A file inside `~/.north`, Read tool | **yes** | **no** |
| A file inside `~/.north`, `cat` | no | no |
| A file inside `~/.north`, Grep tool | no | no |
| The internet (example.com) | no | no |
| This machine's own network address | yes | yes |

**Cause.** North gave Claude's sandbox only its short secret list (`~/.north`, `~/.ssh`, ...). The
sandbox's `denyRead` covers commands only; Claude Code's docs say so ("A `denyRead` entry doesn't stop
the Read tool"). In a plan run, the Read tool reads anywhere, and no gate is asked. Codex already had a
closed home folder (#61).

**Fix.** `closed_paths` gives Claude the same closed home as Codex. Each path goes to the sandbox's
`denyRead` (commands) and to `permissions.deny` as `Read(//path)` and `Read(//path/**)` (the Read,
Grep and Glob tools).

**`e65_claude_tools.py`.** On this Mac, node and npm live in a home folder no list names. A plan run
runs node, npm, git and python3: 4 of 4 before, 4 of 4 after.

**This machine's network address.** It is open because of `allowLocalBinding`, which north turned on so
that a check can start a local server. With it off (`--local-binding off`), the address is closed too
(`results/e65_claude_reach_binding-off.json`). The docs describe this setting as localhost only. It is
not changed here: that choice is the user's.

## #67 and #63

Not built. 2 calls from the user's own tasks, no pause at a usage limit, and the one failure had both
agents down, so a fallback could not have helped. In 144 Codex reviews, the median review took 12 s; 34
ran tests, which took seconds, so giving the reviewer north's test result would save little.
