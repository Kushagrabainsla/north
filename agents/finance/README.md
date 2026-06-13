# Finance Agent

This agent specializes in personal budgeting, tracking expenses, and looking up market prices for financial assets.

## Tools Used

No specialized tools (`tools.yaml` is empty). The agent uses the universal tool set granted
to every agent — chiefly `web_search` and `fetch_url` for financial information, plus
`read_file` / `write_file` for any workspace files.

## How to Test

Run direct unit tests or invoke the agent via the CLI:
```bash
python -m cli.main task "How much did I spend on groceries?"
```
