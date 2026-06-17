# Job Agent

This agent specializes in career coaching, networking outreach drafting, and interviewing guidance.

## Tools Used

No specialized tools (`tools.yaml` is empty). The agent uses the universal tool set granted
to every agent - chiefly `web_search` and `fetch_url` for company research, plus `read_file` /
`write_file` for drafting documents in the workspace.

## How to Test

Run direct unit tests or invoke the agent via the CLI:
```bash
python -m cli.main task "Help me prep for a software engineering interview at LinkedIn"
```
