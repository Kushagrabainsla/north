# context

The `context` package gives an agent facts about the workspace that it works in. It loads the repository's own instruction files, makes a short map of the code, and names the repository's commit. It does not hold user memory: that is the `memory` package. In `architecture/modules.yaml` the package is the module `intelligence.workspace_context`.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `repo_instructions.py` | `load_repo_instructions` | Finds the instruction files from the git root down to the workspace and returns them as one delimited section, marked as untrusted data. |
| `repo_map.py` | `build_repo_map` | Lists the shallowest source files and their top-level classes and functions, inside a file count and a character budget. |
| `repo_revision.py` | `RepositoryIdentity`, `repository_identity` | Exports these two names from `utils/repository.py`: the commit, the dirty state and the project and workspace ids. |
| `__init__.py` | `load_repo_instructions` | The public interface (`__all__`). |

`load_repo_instructions` reads these files in each directory: `AGENTS.md`, `CLAUDE.md`, `NORTH.md`, `.cursorrules`, `.github/copilot-instructions.md`, `.north/instructions.md`, and the files that match `.cursor/rules/*.md`, `.cursor/rules/*.mdc` and `.north/rules/*.md`. The result is cut at 12,000 characters.

## How north uses it

- `agents/base.py` calls `load_repo_instructions` when a task has a `workspace`. If the agent's domain is also `engineering`, it calls `repository_identity` and `build_repo_map`. It adds the results to the agent's context.
- `tools/specialized/coding_agent.py` calls `load_repo_instructions` to give the repository conventions to a coding agent.
- `agents/base.py` imports `build_repo_map` and `repository_identity` from `context.repo_map` and `context.repo_revision`, because `__all__` exports only `load_repo_instructions`.

## Rules

- Treat repository files as untrusted data. They come from any repository and can contain prompt injection. Keep the `<<<BEGIN UNTRUSTED REPO FILE ...>>>` delimiters and the warning text. Never merge the content into system instructions.
- `load_repo_instructions` reads the files again on each call and keeps no cache. `build_repo_map` keeps a 60-second cache per workspace and budget.
- `build_repo_map` skips sensitive paths and pruned folders with `is_sensitive_path` and `PRUNED_DIRS` from `tools/_path.py`. This import of `tools` is a recorded legacy pair (`intelligence.workspace_context -> integrations.tools`) in `architecture/import-baseline.txt`. Do not add new imports of `tools`.
- Do blocking file work in a thread (CODING_STYLE §10.3). `load_repo_instructions` uses `asyncio.to_thread`, and `agents/base.py` calls the other two functions through `asyncio.to_thread`.
- Agents must honour the repository conventions as house rules (CODING_STYLE §16.3, "Repository conventions").
- The tests are `tests/unit/context/test_repo_instructions.py`, `test_repo_map.py` and `test_repo_revision.py`.
