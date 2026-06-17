# Health Agent

This agent specializes in tracking calories, logging workouts, and providing dietary recommendations.

## Tools Used

No specialized tools (`tools.yaml` is empty). The agent uses the universal tool set granted
to every agent - chiefly `web_search` and `fetch_url` for health guidance, plus `read_file` /
`write_file` for any workspace files.

## How to Test

Run direct unit tests or invoke the agent via the CLI:
```bash
python -m cli.main task "Help me plan a high-protein breakfast"
```
