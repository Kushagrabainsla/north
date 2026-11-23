---
title: Choose the models north uses
description: Set a model for each kind of work, pick an Auto mode or pin one model, and see which model ran a task and what it cost.
---

This guide shows how north chooses a model for each kind of work.
It also shows how to see which model ran a task and what it cost.

## Before you start

- Connect at least one AI provider. The [Quickstart](/north/guides/quickstart/) shows how.
- Open the dashboard with `north web`.

## Open the Models panel

1. In the sidebar, click **Settings**.
2. Find the **Models** panel.

The panel has a **Default** row and one row for each kind of work.
Each row has one dropdown. The dropdown has these choices:

| Choice | What it does |
| --- | --- |
| **Same as default (...)** | The row uses the choice of the **Default** row. The text in brackets shows that choice. |
| **Auto · eco**, **Auto · cruise**, **Auto · sport** | north chooses the model. These choices are under **North chooses**. |
| A model name | north calls only this model. The models are in groups, one group for each provider. |

The **Default** row does not have **Same as default**.
On a new install, the **Default** row is **Auto · cruise**, and each other row is **Same as default**.

north saves a change when you select it.
north reads the setting at each model call, so you do not restart north.

## Kinds of work

| Row | What north uses it for |
| --- | --- |
| **Your tasks** | The work you ask north to do. A part of north that is not in another row also uses this row. |
| **Checks** | north checks an answer against the task. north also compares a task with your goals. |
| **Approvals** | north decides for you if an action is safe. north uses this row only when the approval mode is **autonomous**. |
| **Background** | Memory upkeep, summaries and other small jobs. |

Background work includes these jobs:

- north saves what it learns from your work to its memory.
- north makes a skill from a kind of task that succeeds many times.
- north makes a summary of the history of a long task.

## Choose an Auto mode

If a row is Auto, north puts the models it can reach in order.
north tries the first model. If that model fails, north tries the next model.

| Mode | What north does | When to use it |
| --- | --- | --- |
| **eco** | Tries the cheapest model first. The cost is low, but the quality can be lower on hard tasks. | Cost is more important than quality, for example for **Background**. |
| **cruise** | Tries the model that fits each part best. It balances cost and quality. | Most work. |
| **sport** | Tries the most capable model first. The quality is best, and the cost is higher. | Hard tasks, where quality is more important than cost. |

In each mode, north uses only models that can do what the work needs, for example tool calls.
In **eco**, north also keeps a minimum quality for each part.

You can also set the mode of the **Default** row in the terminal interface:

1. Run `north` to start the terminal interface.
2. Type `/power` to show the current mode.
3. Type `/power eco`, `/power cruise` or `/power sport` to change it.

`/power` changes only the **Default** row.
If the **Default** row has a pinned model, the new mode applies only after you set the row to Auto again.

## Pin a model

To pin a model is to select one model name for a row.
Pin a model when the answer must come from that one model.

A pinned model is the only model that north calls for that row.
If that model fails, the work stops. north does not try a different model.
For example, the work stops if the provider is down or limits your requests.

If north cannot reach a pinned model now, the dropdown shows it under **Not reachable now**.
To stop pinning, select an **Auto** choice or **Same as default**.

To see which providers and models are limited now, run `north limits`.
The command shows when each one becomes usable again.

## Add providers to get more models

The dropdowns show only the models of the providers that you connected.
To get more models, connect more providers:

1. In **Settings**, go to **Connections** and find the **Providers** panel.
2. If the provider uses a key, click **Add key**.
3. Paste the key in **API key**, then click **Save key**.
4. For **OpenAI Codex**, click **Sign in** and complete the login in the browser window.
5. Reload the **Settings** page. The new models are in each dropdown, in the group of their provider.

north keeps a key on this computer and checks it with the provider.
These providers supply models: **Groq**, **Gemini**, **Anthropic**, **OpenCode Zen**, **Mistral**, **OpenAI Codex** and **OpenRouter**.
**Local embeddings** needs no key, but it supplies no models for the dropdowns.

To list the models north can reach, run `north models`.

## See which model ran a task

1. In the sidebar, click **Tasks**.
2. Click a task in the list.
3. Read these parts of the task page:

| Part | What it shows |
| --- | --- |
| **Cost** | The total cost of the task. |
| **Agent runs** | For each run: the models, the providers, the tokens and the cost. |
| **Model calls** | The calls, the tokens and the cost, separated by purpose. |
| **Models tried** | For each part of the task: the model that answered, and the endpoints north called or skipped, with the reason. |

The task ID is under the task title. You can also use it in the terminal:

```bash
north routing --task <id>
```

For each part of the task, this command shows the chosen model and the result.
It also shows how many models north considered, and why it passed over the others.

## See costs and speed

Run this command to see the cost of the past week:

```bash
north inference costs
```

The command shows the total cost, the cost by component and the cost by model.
To change the period, add `--period day`, `--period week` or `--period month`.

Run `north speed` to see how fast each model was on this computer.
In Auto, north puts a model that is too slow at the end of the order. north does not remove it.
