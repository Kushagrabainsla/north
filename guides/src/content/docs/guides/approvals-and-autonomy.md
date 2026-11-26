---
title: Approve actions and set autonomy
description: Answer north's cards, make your answers rules and choose an approval mode, so north decides more without asking you.
---

This guide explains the cards that north sends you and how to answer them.
It also explains the approval mode, which sets how much north decides without asking you.

## What a card is

A card is a request from north that waits for you. There are two types:

- An **approval card** shows a change before north makes it, for example a command, a file edit or a message. The dashboard labels it **Needs your OK**.
- A **question card** asks you something. The dashboard labels it **North asks**.

Cards do not expire. Each day at 09:00 by default, north reminds you of the cards that still wait.

In every mode, some work runs without a card.
Work that only reads runs, and north writes its own notes.
A command that writes only to its task's own folders, with no network, also runs.

## Where cards appear

- **Dashboard:** in the session where the task runs, and in the **Inbox** in the sidebar.
- **Terminal:** in the conversation, when you run `north`.
- **Telegram:** if you connected a bot, north sends each card to each allowed user id. This is also true while the dashboard or the terminal is open.

The **Inbox** has three tabs:

| Tab | What it holds |
| --- | --- |
| **Needs you** | Cards and stopped flow runs that wait for your decision. |
| **For your info** | Reminders and results. You decide nothing here. |
| **Done** | Cards that you or north decided. |

**Needs you** has two sections.
**Stopped until you answer** holds the cards that a task or a flow waits on.
**When you have a minute** holds work that north finished and left for you. Nothing is blocked.
Each card shows how long it waits. The Inbox has no "approve all" button, so you decide each card.

## Answer a card

In the dashboard:

1. Open the **Inbox**.
2. Read the card. If a field shows **You can edit this.**, you can change its value.
3. Click **Approve** or **Reject**. If the card shows its own options, click one of them.

If the card holds work that north prepared for you, **Reject** opens **Why reject it?**.
Pick one of the **Common reasons**, or type a reason under **In your own words**. Then click **Reject**.
north uses your reason to make the next batch better.

To answer a question card, click an option.
To give a different answer, click **Something else…**, type your answer and click **Answer**.
If the card asks several questions, answer each one, then click **Send answers**.

In the terminal, type `y` or `1` to approve. Type `n` or `2` to reject.
To reject with a reason, type `n`, a space and the reason.
For a question, type the number of an option or your own answer.

In Telegram, tap **Approve** or **Reject** under the card.
For a question, send your answer as a message.
To see recent decisions, send `/decisions`.

### A card that asks for a password or key

When a tool needs a password, an API key or a login, north shows a card labelled **North needs a secret**.

1. Type each value in its password field.
2. Click **Save**. north keeps the values in **Settings**, **Secrets**, under the names on the card.

north's model sees only the names, never the values.
Click **Not now** to skip. north then tells you which secret is still missing.

Type a secret only on the dashboard.
In Telegram and in the terminal, the card says where to type it. If you send a secret there as an answer, north does not keep it and does not show it to its model.
No approval mode answers this card for you, not even `yolo`.

## Make an answer a rule

After you approve or reject a card, north can ask one time: **Make this a rule?**
The card shows the rule, for example "allow" or "refuse" an action of one kind in one place.
It has two buttons:

- **Yes, always allow it** (after you approve) or **Yes, never allow it** (after you reject). north then follows the rule in every task, flow and session.
- **No, ask me each time**. north keeps this answer too, and does not offer a rule for that kind of action again.

A rule applies only in the safe and autonomous modes.
north does not offer a rule for a send, a spend or a change to north's own settings.
The offer goes to the **Inbox**, under **When you have a minute**.

## The four approval modes

| Mode | What north does without asking | When north asks you |
| --- | --- | --- |
| `ask` (default) | Runs work that only reads. | For every change. |
| `safe` | Runs work that only reads, the actions on the **Safe actions** list, exact repeats of your past answers, and your rules. | For everything else. No model decides for you. |
| `autonomous` | Runs what `safe` runs. For everything else, decides for you from what north knows about you, your past answers and your rules. | When it cannot decide, for example a step outside the workspace, a message to a person or a payment that nothing you said covers. In a chat, also when it would say no. |
| `yolo` | Says yes to every approval and gives the yes answer to every question. | Only for the few actions that always ask, for example a request to forget facts. |

Below `yolo`, these limits apply:

- In `autonomous`, a step outside the workspace, a message to a person, a payment or a delete runs only when one of these covers it: your message in the chat, a past answer of yours, or your **Judgement rules**. Your facts and profile never cover it, because a model wrote them from your chat and your files, and a fact can be wrong. To allow such an action every time, add a line to your **Judgement rules**.
- A change to north's own settings always asks you.
- In `autonomous`, a "no" in a run that is not a chat is final. After north says no 3 times in one task, it asks you about the next refusal.
- In `ask` and `safe`, work that north prepared for your review always asks you.

To see what north may do without asking in safe and autonomous mode, open **Memory** and click the **Safe actions** tab.
Add an action with **Add a safe action**, or click **Disable** on a row.
A send to another person and a spend are never on this list.

## Choose a mode

Start in `ask`. When most of your answers repeat, move to `safe`.
When north has your rules and facts, move to `autonomous`.
Use `yolo` only when you accept every action north takes.

To set the mode, do one of these steps:

- In the dashboard, open **Settings**. In the **Autonomy** panel, click a mode.
- In the terminal or in Telegram, send `/autonomy <mode>`, for example `/autonomy safe`. Send `/autonomy` alone to see the current mode and all the modes.
- To switch to yolo when you start north, run `north --yolo`.

The mode applies to all of north: the dashboard, the terminal and Telegram.
It stays until you set a different mode.
To see the current mode, run `north status`.

## The morning rules card

Each day at 08:00 by default, north can put a card named **Rules to confirm** in the **Inbox**.
The built-in flow `rules-to-confirm` makes it.
The card lists kinds of action that north's approval model decided for you at least 2 times in the last day, in the same way, with no rule.
It lists at most 5 kinds, with the same buttons as **Make this a rule?**.

Only the `autonomous` mode lets the approval model decide, so the card comes only after autonomous runs.
If there is nothing to ask, north sends no card. It sends at most one card each day.

## Undo a decision

To change a decision that north made for you:

1. Open the **Inbox** and click the **Done** tab.
2. Find the card. Click **Overrule**.
3. Optionally, type a reason under **Why (optional)**.
4. Click **Should have approved** or **Should have rejected**. For a question, type **Your answer** and click **Answer instead**.

north then decides your way the next time.
What north already did, or refused, stays done.

To remove a rule or a past answer:

1. Open **Memory** and click the **Learned decisions** tab.
2. Find the row. A rule starts with "Standing rule:".
3. Click **Forget**, then confirm.

north then asks you again the next time this action comes up.
