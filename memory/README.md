# memory

The `memory` package holds what north knows about the user and about past tasks. It stores facts, task episodes and the context documents, and it learns new facts from the ledger. `LocalMemoryGateway` is the one read path for agents and internal checks. In `architecture/modules.yaml` the package is the module `intelligence.memory`, in the `intelligence` layer.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `base.py` | `ContextStore` (ABC), `MemoryGateway` (ABC) | The two interfaces. `ContextStore` reads and writes the context documents. `MemoryGateway` reads all memory. |
| `models.py` | `ContextDocument`, `MemoryPrincipal`, `MemoryContext` | The document names (`user.md`, `judgement_rules.md`, `north_stars.md`, `soul.md`), the caller identity and the recall result. |
| `gateway.py` | `LocalMemoryGateway` | Recalls facts, episodes and the `user.md` profile for one principal. Scopes episodes by domain and facts by topic. |
| `documents.py` | `SQLiteContextStore`, `FileContextStore` | `SQLiteContextStore` keeps the documents in `memory.db`. `FileContextStore` keeps them as markdown files. |
| `facts.py` | `FactStore` | One SQLite row per fact, each with its own embedding. Adds, searches, supersedes, dates, merges and forgets facts. |
| `episodic.py` | `EpisodicStore` | One summary per ended task, with its outcome. Searched by embedding, or by keywords when there is no embedding. |
| `consolidator.py` | `EpisodeConsolidator` | The single writer of `episodic.db`. Reads ended tasks from the ledger and records one episode for each task. |
| `extraction.py` | `ExtractionPipeline`, `learning_status` | Reads the user's own messages from the ledger after a task ends. Writes facts, rules and goals. |
| `task_end_learning.py` | `TaskEndLearning` | When a task ends, writes the episodes, then starts skill learning. One run at a time. |
| `profile.py` | `rebuild_profile`, `move_rule_and_goal_facts` | Writes `user.md` from the facts and the rule and goal documents. |
| `injection.py` | `ContextInjector` | Adds text, a file or a URL that the user gives to the context layer. |
| `embeddings.py` | `EmbeddingIndex` | Paragraph embeddings of the context documents in SQLite. |
| `backup.py` | `snapshot_memory` | Copies the memory databases into a time-stamped folder with the SQLite backup API. |
| `dedup.py` | `normalize_for_dedup` | Normalizes fact text to find facts that say the same thing. |
| `exceptions.py` | `ContextError`, `ContextReadError`, `ContextWriteError` | The package errors. |

## How north uses it

- `config/dependencies.py` builds `SQLiteContextStore`, `FactStore`, `EpisodicStore` and `LocalMemoryGateway`. It gives each store the shared `embed_fn`.
- `agents/base.py` calls `principal_for` and `recall` before an agent run. `agents/agentic_llm_agent.py` calls `read_persona`.
- `approval/decider.py` recalls memory and reads `judgement_rules.md` to decide a card in autonomous mode.
- `orchestrator/north_star.py` reads `north_stars.md`. `orchestrator/coding_briefing.py` recalls facts for a coding task.
- `orchestrator/app.py` builds `ExtractionPipeline`, `EpisodeConsolidator`, `TaskEndLearning` and `ContextInjector`. At startup it runs the fact upkeep (`backfill_embeddings`, `deduplicate`, `expand_identifiers`, `date_facts`), then `rebuild_profile`.
- The `Orchestrator` calls `ExtractionPipeline.run_once` when a task ends.
- `tools/universal/remember.py` and `tools/universal/forget.py` write to `FactStore` and call `rebuild_profile`.
- `skills/distiller.py` reads successful episodes with `EpisodicStore.list_successful`.
- `web/api.py` calls `learning_status`. `bootstrap/onboarding.py` writes facts with `FactStore.add_fact_with_provenance`.
- The public interface is `__all__` in `__init__.py`. Callers import `FactStore` and `EpisodicStore` from `memory.facts` and `memory.episodic`. The `__init__.py` docstring says that this prevents an import cycle.

## Data on disk

All files are in north's home (`~/.north` by default).

- `memory.db`: the context documents. `facts.db`: the facts. `episodic.db`: the episodes. `embeddings.db`: the document paragraph embeddings.
- `extraction_watermark.txt` and `episode_watermark.txt`: how far each reader got in the ledger.
- `backups/<stamp>/`: daily snapshots of `facts.db`, `episodic.db`, `memory.db`, `ledger.db` and `web.db`. The last 7 are kept.
- `context_archive/`: a copy of a document before the extraction pipeline shortens it.

## Rules

- Agents and internal checks read memory through `MemoryGateway`. Do not read the stores directly in a new agent or check (CODING_STYLE §7.3, "One layer per concern").
- Learn facts about the user only from the user's own words: `PROMPT`, `MIC`, `MANUAL_INJECTION` and `CLARIFICATION` ledger rows. Agent output can be wrong, and north must not learn it as fact.
- Learn from a message only after its task has ended. If the user cancels a task, north learns nothing from it.
- A fact about the user lives in `FactStore`. A judgement rule or a goal lives in its document. `user.md` is a view of both, made by `rebuild_profile`. Do not keep a second copy.
- Keep recall thresholds relative to the best match. Absolute cosine values change when the embedding model changes.
- Each store creates its own schema at construction with `CREATE TABLE IF NOT EXISTS` and guarded `ALTER TABLE` (CODING_STYLE §11.4). Use `utils.db.open_db_connection` (CODING_STYLE §11.1).
- Do blocking file and database work in a thread, with `asyncio.to_thread` (CODING_STYLE §10.3).
- A vector store stamps itself with the embedding model. When the model changes, the store clears or re-embeds its vectors (`utils/vector_space.py`).
- The tests for this package are in `tests/unit/context/` and `tests/unit/memory/`.
