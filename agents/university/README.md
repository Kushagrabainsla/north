# University Agent

This agent specializes in managing school deadlines, syncing assignments from Canvas, and planning study blocks.

## Tools Used

No specialized tools (`tools.yaml` is empty). The agent uses the universal tool set granted
to every agent — chiefly `web_search` and `fetch_url` for academic research, plus `read_file` /
`write_file` for any workspace files.

## How to Test

Run direct unit tests or invoke the agent via the CLI:
```bash
python -m cli.main task "What assignments are due in CS 162?"
```
