---
title: Teach north about you
description: See and change what north remembers about you, add dated facts, and let north read your files one time.
---

This guide shows what north remembers about you and how to change it.
north uses this memory to do tasks your way, so you do not have to repeat yourself.

## How north learns about you

north learns from three sources:

- **Your messages.** When a task ends, north reads the messages that you wrote in it. It keeps the facts, rules and goals that it finds.
- **Your files.** If you start it, north reads your documents one time. The section "Let north read your files" tells how.
- **Your edits.** You can add and change facts and documents on the **Memory** page.

north learns only from the words that you write, not from its own answers.
If you cancel a task, north does not learn from the messages in that task.

## Open the Memory page

1. Run `north web` to open the dashboard.
2. In the sidebar, click **Memory**.

The page has one row of tabs:

| Tab | What it holds | When to edit it |
| --- | --- | --- |
| **North stars** | Your goals for your work and your life. | When a goal starts, changes or ends. |
| **Judgement rules** | How you decide: what you approve or reject, and your priorities. | When north decides in a way that you do not like. |
| **Soul** | north's character and voice. | When you want north to talk in a different way. |
| **Facts** | Short facts about you, such as your work and your preferences. | When a fact is wrong, old or missing. |
| **Episodes** | Notes of past tasks. north uses them to recognize a situation that it was in before. | You cannot edit them. |
| **Learned decisions** | Your approve and reject decisions, which north repeats. | When north must ask you again. |
| **Safe actions** | The actions that north can do without asking, in safe and autonomous mode. | When you want to allow or stop an action. |

## Edit a document

**North stars**, **Judgement rules** and **Soul** are documents.
Each one shows the **Markdown source** next to a **Rendered preview**.

1. Click the tab of the document.
2. Change the text in **Markdown source**.
3. Click **Save document**.

To remove all of your changes, click **Reset document**, then confirm.
north puts the document back to the text that it ships with. Anything that you wrote in it is lost.

north also adds lines to **North stars** and **Judgement rules** when it learns a goal or a rule from your messages.
Read these tabs from time to time, and remove lines that are not true.

How north uses the documents:

- **North stars**: Before a task that acts outside north, north checks the task against your goals. If they conflict, north shows the card **North Star Conflict Detected**. Click **Proceed anyway** or **Cancel**.
- **Judgement rules**: When north answers an approval card for you, it reads these rules.
- **Soul**: north reads this document as its character when it works on a task.

## Your profile

north also keeps a profile of you. The profile is not a tab.
north builds the profile from your facts, your **North stars** and your **Judgement rules**. It rebuilds the profile when one of them changes.
You cannot edit the profile. To change it, change a fact or a document.

To see your profile, run `north context show user`.

## Add, correct or delete a fact

A fact is one short statement about you, for example "My manager is Priya."

To add a fact:

1. On the **Memory** page, click **Facts**.
2. In **Fact**, type one fact. Put only one idea in each fact.
3. Optional: set **True from** and **Until**. The next section tells how.
4. Click **Add fact**.

To correct a fact:

1. Find the fact in the list, then click **Edit**.
2. Change the text or the dates.
3. Click **Save fact**. To stop without a change, click **Cancel**.

To delete a fact, click **Delete**, then click **Delete fact**.
north stops using the fact and does not recall it for future tasks.

Each fact shows its topic, its dates and when it changed last. The badge shows north's confidence as a percentage, or **Active**.

You can also correct a fact in a message, for example "I moved to Boston."
When a new fact makes an older fact untrue, north can mark the older fact "replaced by a newer fact". The older fact stays in the list, dimmed.

The **Facts** and **Episodes** tabs show how far north has read your messages.
"Learning from your messages: up to date." means that north has read all of them.

## Give a fact dates

Some facts are true only for a time, for example an internship or a lease. Give these facts dates:

- **True from** is the first day that the fact is true.
- **Until** is the last day that the fact is true.

Both dates are optional. north reads the days in the time zone on the **Settings** page.

When north uses a dated fact, it adds the dates to the text, for example "(true until Aug 14, 2026)".
The model can then tell the present from the past.

After the **Until** day, the fact has ended. It stays on the **Facts** tab and shows "ended" with the date.
north does not put an ended fact into its prompts.

## Let north read your files

north can read documents such as your resume, notes and plans one time. It then keeps facts about your work, goals and preferences.

1. Go to **Settings** and click **Setup guide**.
2. Click the step **5. Your files**.
3. Look at the list of documents that north found.
4. Click **Read my files**.

Nothing starts until you click the button. Each document shows **completed** or **pending**.

north looks in your Documents, Desktop and Downloads folders, and in Notes, Documents/Notes and Obsidian.
It also reads `.csv` and `.txt` files directly in your home folder, and `README.md` files in `~/Desktop/projects`.
It skips code and build folders. It reads at most 25 documents in one run.

If you add or change files after a run, the button shows the number of new files, for example **Read 2 new files**.
The facts that north finds appear on the **Facts** tab. Correct or delete them there.

## Learned decisions and Safe actions

These two tabs come from your approvals:

- **Learned decisions**: Each time you approve or reject an action, north records your decision. When a matching action comes up, north repeats your decision. To make north ask again, click **Forget**, then **Forget decision**.
- **Safe actions**: These rules apply only in safe and autonomous mode. To add one, choose a **Kind**, type the **Action** and click **Add rule**. Two things are never on this list: sending something to another person, and spending money.

## Use the terminal

```bash
north context show north_stars
north context edit judgement_rules
north context add --text "I prefer short replies."
```

- `north context show <doc>` shows a document. Use `user`, `north_stars`, `judgement_rules` or `soul`.
- `north context edit <doc>` opens a document in your `$EDITOR`, or in `nano` if `$EDITOR` is not set. When you close the editor, north saves your changes. You cannot edit `user`.
- `north context add` takes text from `--text`, a file from `--file` or a web page from `--url`. A model decides where it goes. Something about you becomes a fact. A rule or a goal goes into its document.

## Your data and your AI provider

north keeps its memory in `~/.north` on your computer. If you set `NORTH_HOME`, north uses that folder instead.

- `memory.db` holds the documents and your profile.
- `facts.db` holds your facts. `episodic.db` holds the episodes.
- `approval_memory.db` holds learned decisions and safe actions.
- `backups` holds up to 7 snapshots of the memory files. A snapshot is a copy, so a deleted fact can stay in an older snapshot.

Some text goes to your AI provider:

- Your messages, after the task ends, to find facts, rules and goals.
- The text of each document that north reads from your files.
- The text from `north context add`, to choose where it goes.
- A new fact and older facts like it, to find older facts that the new fact makes untrue.
- During a task: the facts that match the task, and often your profile, your **Soul** and notes of past tasks.
- Before a task that acts outside north: the task and your **North stars**, for the goal check.
- When north answers an approval card for you: your **Judgement rules** and the facts that match.
