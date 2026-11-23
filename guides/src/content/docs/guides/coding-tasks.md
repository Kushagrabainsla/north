---
title: Code with Claude Code or Codex
description: Let north send coding tasks to Claude Code or Codex, test each change in a copy of your repository and apply it after you approve.
---

north does not write code itself. It sends coding tasks to Claude Code or Codex on your computer.
This guide shows how to set up the agents, choose a repository, ask for a change and approve it.

## What north does and what the agent does

The coding agent reads the code, makes the change and reports what it did.
north does the work around the agent:

- It makes an isolated copy of your repository on a new branch. The agent works only in this copy.
- It checks the actions of the agent against your approval settings.
- It runs the tests of your project on the copy itself. It does not trust the report of the agent.
- It shows you the change on a card, with the diff. Your folder changes only when the change is applied.

## Before you start

- Complete the [Quickstart](/north/guides/quickstart/).
- Install Claude Code, Codex or both. north finds the `claude` and `codex` commands on your `PATH`.
- Use a recent version of each agent. If a version is too old, the **Coding agents** panel tells you.

## 1. Set up the agents

Each agent uses its own login. north does not give them an AI provider key.

1. Log in to each agent in a terminal:

   ```bash
   claude auth login
   codex login
   ```

2. In the dashboard, open **Settings**. Under **Connections**, find the **Coding agents** panel. Setup shows the same panel.
3. Click **Check again**.

Each agent shows one of these states:

| State | Meaning |
| --- | --- |
| **Ready** | The agent is installed, recent enough, logged in and able to run here. |
| **Needs attention** | The agent is installed, but a check failed. The row tells you what to do. |
| **Not installed** | north cannot find the command on your `PATH`. |

The row can show these fixes:

- If Claude Code is too old, run `claude update`. If Codex is too old, update it.
- If an agent is not logged in, run `claude auth login` or `codex login`.
- On Linux, the Claude Code sandbox needs `bwrap` and `socat`. On Debian or Ubuntu, run `sudo apt-get install bubblewrap socat`.

The check does not know your usage limits. north finds a usage limit only when a run starts.

## 2. Choose the repository

Each task works in one folder. For a code change, the folder must be in a git repository with at least one commit.
If you choose a subfolder, north copies the whole repository and tells the agent to work in your subfolder.

In the dashboard:

1. Open a session.
2. Click the **Workspace** picker under the message box.
3. Go to the folder, then click **Choose this folder**.

In Telegram:

- Send `/workspace <path>` to choose a folder. If the folder is not a git repository, the bot tells you that coding tasks cannot run there.
- Send `/workspace` to see the current folder.
- Send `/workspace reset` to go back to the default folder.

In the terminal, use the `-w` option (or `--workspace`):

```bash
north start -w ~/code/my-app
north task "Fix the failing date test and add a test for it" -w ~/code/my-app
```

`north start -w` sets the folder of the terminal chat and where the dashboard picker opens.
`north web -w` sets where the dashboard picker opens.
Without `-w`, `north start` and `north task` use the git root of the current folder.

## 3. Ask for a change

Type the task in plain words, for example: "Fix the bug that shows invoice dates in UTC, and add a test."

north selects one of two run types:

- A plan run reads the repository and answers. It cannot change anything.
- An edit run makes the change in the isolated copy.

To control the process, say it in your task. For example, write "Plan it first", "Use Codex" or "Do not change anything".
If you name an agent, north uses that agent. If you do not, north uses the first ready agent: Claude Code, then Codex.

The copy starts from what your folder holds now, including uncommitted changes and new files.

## 4. Review and approve the change

An edit run shows these cards:

1. Before the run starts, north shows **Coding Agent - Approval Required**. The card names the agent, the folder and the task.
2. While the agent works, edits inside the copy need no card. Other actions, such as shell commands, can show a card.
3. When the agent stops, north runs your tests on the copy. The test command can also show a card.
4. north shows **Apply Coding Agent Changes - Approval Required**. The card shows the changed files, the diff and the test result.

Click **Approve** or **Reject**. Cards that wait for you are also in the **Inbox**.

- If you approve, north applies the change to your folder as uncommitted changes. Your git history does not change. north removes the copy and its branch.
- If you reject, your folder does not change. The change stays on a branch in your repository. The branch name starts with `north/wt-`.
- If the same lines changed in your folder during the run, north does not apply the change. It overwrites nothing and keeps the branch.

To delete a kept branch that you do not want, run `git branch -D <branch>`.
north does not commit or push unless you ask.

The **Autonomy** setting in **Settings** decides who answers these cards. In the **ask** mode, you answer every card for a change. In the **yolo** mode, north says yes to every card.

After north applies a change, a different agent reads the finished change one time. If no other agent is ready, the same agent reads it in a new session.
north tells you what the reviewer said. This review is an opinion, not a test. To skip it, say so in your task.

## If the tests fail

north finds the test command from the files in your repository:

| File in the repository | Command |
| --- | --- |
| `pytest.ini`, or pytest settings in `pyproject.toml` or `setup.cfg` | `pytest -q` |
| `go.mod` | `go test ./...` |
| `Cargo.toml` | `cargo test` |
| `package.json` with a `"test"` script | `npm test --silent` |

For pytest, north uses the Python in the `.venv` or `venv` folder of the project, if it exists. north stops the tests after 600 seconds.

The result controls what north does next:

- If some tests fail only with the change, north does not offer the change. The change stays on its branch. north can start a new edit with the failures. If it cannot, it tells you the branch and the reason.
- If the same tests also fail without the change, north offers the change. The card tells you that those tests say nothing about the change.
- If north finds no test command, the test runner is not installed, or you reject the test command, north offers the change. The card says that the tests are not verified.

## If the agent stops

If the agent reaches its usage limit or is not logged in, north pauses the run. It keeps the progress and the copy.
Log in again, or wait for the limit to reset. Then ask north to continue the same task.
