---
title: Quickstart
description: Install north, connect an AI provider and run your first task in about ten minutes.
---

This guide installs north, connects it to an AI provider and runs your first task.
It takes about ten minutes.

## Before you start

- north is built for macOS. Most of it also runs on Linux.
- You need a key for an AI provider, such as OpenRouter, Groq, Gemini, Anthropic or Mistral.
  If you have a ChatGPT plan that includes Codex, you can sign in to OpenAI Codex instead.

## 1. Install north

Run the installer in a terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/Kushagrabainsla/north/main/scripts/install.sh | bash
```

The installer does these steps:

1. It installs `uv`, if you do not have it.
2. It installs north from GitHub.
3. It asks for an OpenRouter key. Press Enter to skip this step. You can add a key later.

If your terminal cannot find the `north` command, add uv's folder to your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Add this line to your shell profile, for example `~/.zshrc`, to keep it.

## 2. Open the dashboard

```bash
north web
```

This command starts north and opens the dashboard in your browser.
A one-time link signs your browser in.

If you use a different browser, run `north web --no-open`.
The command then shows the link instead. The link works one time, for ten minutes.

## 3. Complete Setup

The first time, the dashboard opens Setup. Setup has six steps:

| Step | What you do | Required |
| --- | --- | --- |
| 1. AI connection | Add a key for a provider, or sign in to OpenAI Codex. | Yes |
| 2. Coding agents | north finds Claude Code and Codex, if you have them. Each one uses its own login. | No |
| 3. Browser profiles | Choose the browser profiles north can use for web tasks. | No |
| 4. Telegram | Connect a Telegram bot, so you can send tasks from your phone. | No |
| 5. Your files | Let north read your documents one time and remember facts about you. | No |
| 6. Finish | Check the summary, then click **Finish setup**. | Yes |

You can skip any optional step and come back later.
Steps 1 to 4 are also in **Settings**, under **Connections**.
To open Setup again, go to **Settings** and click **Setup guide**.

**Finish setup** stays off until you connect an AI provider.
To leave Setup before that, click **Skip setup for now**.

Setup also sets north's time zone from your browser.
To change it, go to **Settings** and open **Time zone**.

## 4. Run your first task

1. In the sidebar, click **Sessions**.
2. Click **+ New session**.
3. Type a task in plain words, for example: "Summarize the top three stories on Hacker News today."
4. Click **Send**.

north shows its work while it runs.
Before it changes anything, it shows you a card with the exact change: the command, the file or the message.
Click **Approve** or **Reject**. Cards that wait for you are also in the **Inbox**.

To work on a folder, such as a git repository, choose it with the **Workspace** picker under the message box.

## 5. Use the terminal instead (optional)

north also has a terminal interface. It is the same north, with the same memory and settings.

```bash
north
```

Type `/help` to see its commands.

## What to do next

- Answer the cards that north sends you. When you answer one, north can make your answer a rule, so it does not ask again.
- Open **Settings** to choose the models north uses and how much it decides without asking you.
- See the [full list of terminal commands](https://kushagrabainsla.github.io/north/#commands) on the landing page.
