# Bucket 6: sandbox and isolation (#64, #40, #37)

Each script runs the product code of a checkout given on the command line, so `main` and the branch are
measured with their own code on the same data. The results hold counts, probe names and exit codes only:
no paths, no command output and no credentials.

```bash
.venv/bin/python experiments/codex_sandbox/e64_sandbox_probes.py before <main checkout>
.venv/bin/python experiments/codex_sandbox/e64_sandbox_probes.py after .
.venv/bin/python experiments/codex_sandbox/e64_replay.py before <main checkout>
.venv/bin/python experiments/codex_sandbox/e64_replay.py after .
.venv/bin/python experiments/codex_sandbox/e40_secret_reach.py before <main checkout>
.venv/bin/python experiments/codex_sandbox/e40_secret_reach.py after .
```

## #64: Codex refusals

**What real runs hit.** Codex keeps a log of each session. 116 sessions since 2026-10-03 were north's
real runs in a copy (tests and evals excluded). They ran 585 commands.

| Refusal | Where | Effect |
|---|---|---|
| `~/.config/git/ignore`: Operation not permitted | 279 lines, 89 of 116 runs | a warning in every git command; the global ignore was not used |
| `lstat ~/.cache/north` / realpath | 6 commands | `npm test`, `npm run build`, `require.resolve`, a virtualenv's python failed |
| heredoc temp file | 5 commands, plan runs | `python3 - <<EOF` failed in reviews |
| `ls ..`, `rm .git`, `ps` | 6 commands | intended: other copies, the copy's git link, other processes |
| a toolchain in an unlisted folder | 0 | the issue's guess; not seen |
| a write to the home folder | 0 | not seen |

**Causes.** A deny on the whole copies folder also hid the folders above the run's own copy, which node and
python look at. A plan run had no writable temp folder. `~/.config` was closed as a whole.

**`e64_sandbox_probes.py`.** A copy-like folder in the real copies folder, next to a second "copy" with a
canary file. Each command runs under `codex sandbox` with the profile the checkout's `CodexBackend` builds,
in the environment the backend starts the agent with. No model call.

| | main | branch |
|---|---|---|
| Dev commands that work (13: npm test, npm run build, virtualenv python, python3, git status, heredoc, a write in the workspace) | 10 | **13** |
| Refusal lines printed by `git status` (plan + edit) | 6 | **0** |
| Safety checks refused (13: other copy read/write, `~/.north/secret.key`, `~/.ssh`, another `~/.config` file, the copies folder, the home folder, writes in plan mode) | 13 | 13 |

**`e64_replay.py`.** Every command of the 116 real runs goes through the checkout's own `_Run`, in the
shape the app-server sends.

| | main | branch |
|---|---|---|
| Commands that failed with a refusal | 17 (14 runs) | 17 (14 runs) |
| Runs with that refusal on north's record | 0 | **14** |

The live tests (`tests/live/test_codex_live.py`, real app-server) pass on the branch: see the PR.

**Not built.** Reading the toolchain list from settings, and an approval path for writes to the home
folder: 0 cases in 116 runs. The listing still happens when a run starts, so a copy that another run makes
during this run is readable from it, not writable.

## #40: one shared secret

**Who holds it today.** The CLI and TUI (the owner, from `secret.key`), the Telegram gateway (part of
north), and the web dashboard (a one-time pairing, then a browser key). Coding agents get their own
per-run tokens for the gate and `ask_north`. No less-trusted client holds the secret yet: the north MCP
server and plugin (#51) are not built.

**`e40_secret_reach.py`.** Canaries go into the environment under north's credential names, as the
dashboard does when you save a provider key or the Telegram token, and as Docker sets `NORTH_SECRET`.

| | main | branch |
|---|---|---|
| Credentials in `env` of a command that needs no card | 3 of 3 | **0 of 3** |
| Credentials in `env` of an approved command | 3 of 3 | **0 of 3** |
| `~/.north/secret.key` readable by a command | no | no |
| Credentials in a coding agent's environment | 0 | 0 |

An approved command may connect to localhost. With `NORTH_SECRET` in its environment, it could call north's
API and answer its own cards.

**Not built.** Per-client tokens with scopes. Today every holder is the owner or north itself, so scopes
would change nothing yet. They belong with the first client that is not: the MCP server (#51).

## #37: OS sandbox

On macOS 26.5.2, north's sandbox tests run with the real `sandbox-exec` and pass (90 tests:
`test_bash_safety`, `test_contained_writes`, `test_egress`, `test_repository_read_only`,
`test_sandbox_cd`, `test_sandbox_refusals`). What is left is the Linux backend (bubblewrap). North runs on
macOS only today (macOS first), so nothing is built here.
