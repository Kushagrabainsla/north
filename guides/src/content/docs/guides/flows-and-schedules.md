---
title: Run tasks on a schedule with flows
description: Make a flow, test it, activate it and run it on a schedule from the dashboard or the terminal.
---

This guide shows how to make a flow and run it on a schedule.
It also shows where to find the results.
Before you start, complete the [Quickstart](/north/guides/quickstart/).

## What a flow is

A flow is a list of steps. north runs the steps in order, from top to bottom.
Each step uses a skill or instructions that you write.
A schedule tells north when to run the whole flow.

A flow has one of these statuses:

| Status | What it means |
| --- | --- |
| **Draft / candidate** | The flow is new or changed. You can test it. It cannot run on a schedule. |
| **Active (tested)** | The flow passed a test run, and you activated it. It can run on a schedule and by hand. |
| **Retired** | The flow does not run. |

## 1. Make a flow

1. In the sidebar, click **Flows**.
2. Click **+ New flow**.
3. In **Flow name**, type a name. Use lowercase letters, numbers and hyphens.
4. In **Purpose**, write what the flow must do.
5. Type a **Step name**, then choose the **Kind** of the step:
   - **Existing skill**: choose the skill in **Skill**. If the skill needs inputs, fill in **Skill inputs**.
   - **Instructions**: write the procedure in **Step instructions**.
6. To add a step, click **+ Add step**. To remove a step, click **Remove step**.
7. Click **Create flow**.

north saves the new flow as a candidate.
You can also ask north in a session to make a flow for you.

## 2. Test and activate the flow

A flow must pass a test run before it can run on a schedule.
A test run does the real work one time, on a small case.

1. On the **Flows** page, click the flow to open it.
2. Click **Test run**. north starts the run and shows the **Runs** tab.
3. If a step needs your approval, answer its card in the **Inbox**.
4. When the test run is complete, click **Activate**.
5. In the **Activate flow?** dialog, click **Activate**.

**Activate** shows only after a test run of the current version of the flow is complete.
To run an active flow at once, click **Run now**, then click **Run now** in the dialog.

If you change an active flow and click **Save flow**, the flow goes back to candidate.
If a skill that the flow uses changes, the flow shows a warning.
In both cases, its schedules do not run it. Test the flow and activate it again.

## 3. Schedule the flow on the dashboard

1. Open the flow and click the **Schedule** tab.
2. Click **+ New schedule**.
3. Optional: in **Name**, type a name for the schedule.
4. In **Repeats**, choose how often the flow runs. The table below shows the choices.
5. Set the **Time**.
6. Click **Add schedule**. For **Does not repeat**, click **Schedule it**.

| Repeats | When the flow runs |
| --- | --- |
| **Does not repeat** | One time, on the **Date** and at the **Time** that you set. |
| **Every few minutes** | Again and again, at the interval in **Every (minutes)**. This choice has no **Time**. |
| **Every day** | Each day, at the **Time**. |
| **Every weekday (Mon to Fri)** | Monday to Friday, at the **Time**. |
| **Every weekend (Sat and Sun)** | Saturday and Sunday, at the **Time**. |
| **Weekly on selected days…** | On the days that you select in **On these days**, at the **Time**. |

**+ New schedule** stays off until the flow is active.
A flow can have more than one schedule. Each schedule runs the whole flow.
Each schedule has the buttons **Pause** (or **Resume**), **Edit** and **Delete**.
When you delete a schedule, the flow stays.

## 4. Schedule the flow in the terminal

To run an active flow each day at 07:00, run this command. Use the flow's name in place of `<flow>`:

```bash
north cron add <flow> --hour 7
```

| Option | What it does |
| --- | --- |
| `--hour` | The hour, from 0 to 23. You must give it. |
| `--minute` | The minute, from 0 to 59. If you do not give it, north uses 0. |
| `--days` | The days: `mon` to `sun`, with commas between them, or `weekdays`, `weekends` or `daily`. If you do not give it, the flow runs each day. |
| `--name` | The name of the schedule. If you do not give it, north makes a name from the label or the flow. |
| `--label` | A short title for lists. |

For example, this command runs a flow at 07:30 on Monday, Wednesday and Friday:

```bash
north cron add <flow> --hour 7 --minute 30 --days mon,wed,fri
```

To manage schedules, use these commands:

- `north cron list` shows all schedules, soonest first. Built-in schedules show "(built-in)".
- `north cron set <name> --hour 8` changes a schedule. It changes only the options that you give.
  The other options are `--minute`, `--days`, `--flow`, `--pause` and `--resume`.
- `north cron rm <name>` removes a schedule.
  For a built-in schedule, it puts back the default settings. To stop a built-in schedule, use `--pause`.

The terminal makes only schedules at a time of day.
For a one-time run or a run every few minutes, use the dashboard.

## Time zone

Schedule times use north's time zone.
To change it, go to **Settings**, open **Time zone**, and choose a zone in **North uses**.
If you change the zone, a flow schedule keeps its clock time in the new zone.
`north cron list` shows the zone under the table.

## When the computer sleeps or north is off

north runs schedules only while the computer is awake and north runs.

- When the computer wakes, north runs each schedule whose time passed during the sleep.
- When north starts, north runs each schedule whose time passed while north was off.
- north runs only the most recent missed time of each schedule, and only from the last 24 hours.
- If the previous run of a schedule has not finished, north does not start a second run.

A run that starts more than 2 minutes after its time is late.
north shows the delay and the reason beside the run, for example: "ran 08:58, 1 h 58 min late: the computer was asleep".
The other reason is "north was not running".

## See runs and results

- On the **Flows** page, **Recent runs** shows the latest runs of all flows.
- Open a flow and click the **Runs** tab to see all runs of that flow.
- Each run shows how it started ("scheduled", "run by hand" or "test run"), when it started, any late note and its status.
- Click **Details** to see what each step did, the files it wrote and the tools it used.
  To see the full task, click **Open its timeline**.
- If a run stopped, click **Resume** to continue from the step where it stopped. Click **Discard** to end it.
- If the last run of a flow failed or was cancelled, **Flows** in the sidebar shows a red dot.

In the terminal, `north jobs` shows scheduled jobs with their status, their time and any late note.
Use `--limit` to show more or fewer jobs, and `--status` to show only one status.

## Built-in flows

north comes with these flows. You can run them and see their runs, but you cannot change them.

| Flow | What it does | When it runs |
| --- | --- | --- |
| `waiting-cards-reminder` | Tells you on Telegram and your other channels which cards still wait for your answer. It sends nothing when nothing waits. | Each day at 09:00 |
| `rules-to-confirm` | Asks you about actions that the approval model decided for you more than once, in the same way. Your answer makes a rule. | Each day at 08:00 |
| `reminder` | Gives you the text of a reminder at its time. Each reminder is a schedule of this flow. To make one, ask north in a session. | At each reminder's time |
| `fact-upkeep` | Stops recalling facts whose last day has passed, and writes your profile again from the facts. | Each day at 00:05 |
| `episode-memory` | Writes a short note about each finished task. north recalls these notes in later tasks. | Every 60 minutes |
| `skill-learning` | Turns tasks that worked into learned skills that north can use again. | Every 60 minutes |
| `nightly-cleanup` | Closes tasks left half-finished, deletes the data of old runs, and retires learned skills whose tasks keep failing. | Each day at 03:00 |

To stop a built-in flow, pause its schedule. You can also change its time:

- On the **Flows** page, open the flow and click the **Schedule** tab. Click **Pause** or **Edit**.
  After you change a built-in schedule, click **Restore default** to put back its default settings.
- In the terminal, run `north cron set <name> --pause`. Find the schedule's name with `north cron list`.
