# request_secret: before and after

Step 3 of the plan to let north rebuild the Kasa tool from one prompt: north must be able to get a
password or key from the user and keep it as a secret, by name.

## How north got a secret before

The guides told the model to call `ask_user` with `requires_user_action=true` and send the user to
Settings, Secrets, to type the secret's exact name and value, then come back and answer **Done**.

## The check

`verify_before_after.py` runs one checkout's own agent, approval store, policy and orchestrator, with
a throwaway north home, on three cases:

- **dashboard:** the user does what the card asks, on the dashboard.
- **reply:** the user types the password as the answer, as in Telegram, the terminal or the chat.
- **yolo:** the approval mode is yolo, and the user is away.

Run it once on each checkout:

```bash
uv run --project <main checkout> python experiments/request_secret/verify_before_after.py <main checkout> before
uv run --project . python experiments/request_secret/verify_before_after.py . after
```

Results: `results/verify_before.json` (main 442a3b8) and `results/verify_after.json` (this branch).
They hold booleans and counts only.

| Case | | Before (main) | After |
|---|---|---|---|
| dashboard | Steps for the user | 7 | **2** |
| dashboard | Secret saved | yes | yes |
| reply | Password reached the model | **yes** | no |
| reply | Password in the card store | **yes** | no |
| reply | Password in the ledger | **yes** | no |
| reply | Task told the secret is saved | **yes** (it was not) | no |
| yolo | Task went on with no secret | **yes** | no: the card waits for the user |

Not checked here: whether a live model calls `request_secret` when a tool needs a key. Step 7 of the
plan (rebuild Kasa from one prompt) measures that.
