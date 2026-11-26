# gateways

This package connects north to outside message channels. Today the only channel is Telegram. The gateway reads the
messages that you send to your bot and sends them into north through the local API. Then it sends the answer back
to the same chat. It runs as a background task in the north server process.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `telegram.py` | `TelegramGateway` | Long-polls the bot with `aiogram`, checks the allowlist, runs slash commands, starts tasks, answers questions, and replies with the result. |
| `telegram_api.py` | `deliver`, `send_message`, `approval_keyboard`, `approval_callback_data`, `parse_approval_callback`, `card_reference`, `referenced_card`, `within_telegram_limit`, `bot_username` | The raw send layer and the one wire format for approval buttons, shared by the gateway and the notifier. |
| `telegram_outbox.py` | `TelegramOutbox` | Keeps messages that could not go out in SQLite and tries them again with backoff, until Telegram accepts or refuses them. |

`TelegramGateway` talks to north only over HTTP on `127.0.0.1:8000`:

- `POST /orchestrator/webhooks/telegram` starts a task, in the folder that `/workspace` chose.
- `POST /orchestrator/steer` sends a message into the task that this chat already waits on.
- `GET /orchestrator/ledger` is polled until the task ends. The wait grows from 1 s to 15 s and stops after a day.
- `GET /web/api/approvals` finds a question that waits. A reply to a question card answers that card.
- `POST /orchestrator/transcribe` turns a voice note into text.

The slash commands are in `_COMMAND_HANDLERS`: `/start`, `/help`, `/limits`, `/status`, `/cancel`, `/stop`,
`/autonomy`, `/decisions` and `/workspace`. Any other text, and a path such as `/Users/me/notes` that answers a
question, goes to north as a normal message.

## How north uses it

- `orchestrator/app.py` makes one `TelegramGateway()` and starts `TelegramGateway.run` as the background loop
  `telegram_gateway`. It also starts `TelegramOutbox.run` as the loop `telegram_outbox`, with a `still_wanted`
  check that drops a card after the user answers it in another place.
- `approval/telegram.py` (`TelegramNotifier`) sends the cards that nobody asked for, such as a card from a cron
  task. It uses `approval_keyboard`, `card_reference`, `send_message` and `TelegramOutbox` from this package.
- `orchestrator/app.py` gives `bot_username` to the web layer, so the dashboard can show the bot name.
- `__init__.py` has no `__all__`. Callers import from the submodules.

## Data on disk

All files are in north's home (`~/.north` by default):

- `telegram_outbox.db`: messages that wait to be sent again.
- `telegram_workspace.txt`: the folder that `/workspace` chose.
- `telegram_waits.json`: the chats that wait on a task's answer. After a restart, `resume_waits` waits on them again.

## Rules

- Write the approval button format only in `telegram_api.py`. If the gateway and the notifier each had a copy, a
  button could stop working with no error.
- The gateway fails closed. It waits until `settings.telegram_ready` is true: a bot token and a valid allowlist. An
  empty allowlist allows nobody. Set up the bot on the dashboard, not in a README (CODING_STYLE §5.1).
- Telegram never replaces the dashboard. Everything that Telegram shows must also be on the dashboard
  (CODING_STYLE §5.1, §1 "Every Surface Shows the Same State").
- A message sent while this chat's task runs goes into that task, not into a new task (CODING_STYLE §15.4).
- A task with no chosen folder works in its own folder under `<NORTH_HOME>/tasks/` (CODING_STYLE §16.1.1).
- Do not wait on a task with a short timeout. A task can wait on a card as long as the user takes
  (CODING_STYLE §13.5).
- Keep each message under `MAX_MESSAGE_CHARS` (3900). Telegram refuses more than 4096 characters.
