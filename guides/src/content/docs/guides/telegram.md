---
title: Use north from Telegram
description: Connect a Telegram bot to north, so you can send tasks and answer cards from your phone.
---

This guide connects a Telegram bot to north.
Then you can send tasks and answer cards from your phone.

## Why use Telegram

- You can give north a task when you are not at your computer.
- You can send a voice note. north changes it to text and runs it as a task.
- north sends its cards to the bot, so you can answer them when you are away from the computer.

## Before you start

- Complete the [Quickstart](/north/guides/quickstart/).
- You need the Telegram app and a Telegram account.
- The bot answers only while north runs on your computer. The bot is part of the north server.
  If north stops or the computer sleeps, the bot does not answer.

## 1. Make a bot

1. In Telegram, send a message to **@BotFather**.
2. Send `/newbot`. Follow the steps to give the bot a name.
3. Copy the token that @BotFather gives you. It looks like `123456789:AA…`.
4. Send a message to **@userinfobot**. Copy your numeric user id.

Use the numeric user id, not your @name. north does not accept an @name.

## 2. Connect the bot in the dashboard

1. Open the dashboard with `north web`.
2. Go to **Settings**. Find the **Telegram** panel under **Connections**.
   You can also use step **4. Telegram** in Setup.
3. Paste the token in **Bot token**.
4. Type your user id in **Allowed user ids**.
   To let more than one account use the bot, separate the ids with commas.
5. Click **Connect**.

north checks the token with Telegram before it saves it.
If Telegram accepts the token, north shows a message such as "Connected as @my_north_bot".
The running server starts to use the bot in a few seconds. You do not have to restart north.

To change the bot or the ids later, use the same panel and click **Save**.
If you leave **Bot token** empty, north keeps the saved token.
To disconnect the bot, click **Remove token**.

## 3. Or connect the bot in the terminal

1. Run `north setup`.
2. The wizard first asks for AI provider keys. Press Enter to keep or skip each one.
3. At "Configure Telegram Bot integration?", type `y`.
4. Paste the token at "Telegram Bot Token (from @BotFather)".
5. Type your user id at "Allowed Telegram Chat/User ID".

`north setup` saves the values but does not check the token with Telegram.
A north server that already runs does not read the new values.
If north runs, stop it with `north stop`. Then start it again with `north web`.

## 4. Send a task

1. Open the chat with your bot in Telegram.
2. Type a task in plain words, or record a voice note.
3. Send it.

The bot shows "typing" while north works. When the task ends, the bot replies with the answer.
If the answer is very long, the bot cuts it. Read the full answer in north.

If you send a message while your task still runs, north adds it to that task.
The bot replies "Added to the task I'm working on" and the next answer covers it.

The bot reads text messages and voice notes only. It ignores photos and files.

## 5. Answer cards

north sends its cards to each allowed id. A card has buttons:

- An approval card has **✅ Approve** and **❌ Reject**.
- A question card can have one button for each option.

Tap a button to answer. The bot then removes the buttons and adds your decision to the message.
To answer a question in your own words, reply to the question message.
If your task waits on a question, a plain message also answers it.

A card in Telegram shows its values, but you cannot change them there.
To change a value before you approve, open the card in the dashboard.
The same cards are also in the **Inbox**.

## Bot commands

Send these commands in the chat with your bot:

| Command | What it does |
| --- | --- |
| `/help` or `/start` | Shows the list of commands. |
| `/status` | Shows the tasks that run now. |
| `/cancel` or `/stop` | Cancels the task that this chat waits on. Add a task id to cancel a different task. |
| `/autonomy` | Shows the approval mode. Send `/autonomy <mode>` to change it: `ask`, `safe`, `autonomous` or `yolo`. |
| `/decisions` | Shows the most recent decisions: who decided, why, and the memory that north used. |
| `/limits` | Shows the rate-limit and cooldown status of your AI providers and models. |
| `/workspace` | Shows the folder that tasks from Telegram work in. |
| `/workspace <path>` | Sets that folder. For coding tasks, the folder must be in a git repository. |
| `/workspace reset` | Sets each task back to its own folder. |

If you send a command that north does not know, the bot tells you that nothing ran.

## Security

- Only the ids in **Allowed user ids** can use the bot.
  A message from a different account gets an "Unauthorized" reply, and north does not run it.
  A button tap from a different account also does not count.
- If **Allowed user ids** is empty, the bot does not start. This stops other people from using your bot.
- Keep the bot token secret. Do not share it or put it in a public file.
  The dashboard shows only the first and last four characters of the saved token.
- If other people learn the token, get a new token from @BotFather. Then save it in the **Telegram** panel.
