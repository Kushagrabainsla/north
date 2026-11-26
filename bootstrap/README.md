# bootstrap

The `bootstrap` package seeds north's memory from the user's own files on first setup. It finds personal documents on this computer and reads the useful ones. A model gives facts and a short profile for each file, and north stores the facts in `FactStore`. It runs only when the user starts it from dashboard onboarding, or when a paused run must finish. In `architecture/modules.yaml` the package is part of the module `application.jobs`, together with `jobs/`.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `onboarding.py` | `run_bootstrap_if_needed`, `bootstrap_paused` | Finds candidate files, surveys them, calls the model once per file, stores the facts and writes `user.md`. Saves a checkpoint after each file. |
| `survey.py` | `survey_files`, `SurveyedFile`, `SurveyReport`, `family_of` | Reads each candidate locally before any model call. Drops empty files, copies, files about other people and surplus files of one kind. Scores the rest on their content. |
| `schema.py` | `FactTopic`, `FactSubject`, `FactItem`, `UserProfile`, `UNIFIED_EXTRACTION_JSON_SCHEMA` | The Pydantic models for the model's answer, and the strict JSON schema sent with the request. |

`onboarding.py` searches `Documents`, `Desktop`, `Downloads` and the notes folders (`Notes`, `Documents/Notes`, `Obsidian`), each to a fixed depth. It also adds loose files in the home folder and some project READMEs. It reads `.pdf`, `.docx`, `.md`, `.txt`, `.csv`, `.json`, `.yaml` and `.yml` files. It ranks files on their names, size, age and depth, and keeps at most 25 files for extraction.

## How north uses it

- `web/api.py` route `POST /bootstrap` calls `run_bootstrap_if_needed` with the files that the user selected. The same file lists the candidates with `_discover_files` and `_load_progress`.
- `orchestrator/app.py` calls `bootstrap_paused` at startup. If a run is paused and `settings.onboarding_active` is true, it starts `run_bootstrap_if_needed` as a background task.
- The prompt is `prompts/bootstrap_extraction.md`, loaded with `load_prompt`. The request uses the `bootstrap` component name.
- `__init__.py` has only a docstring and no `__all__`. Callers import from `bootstrap.onboarding`.

## Data on disk

Both files are in north's home (`~/.north` by default).

- `.bootstrapped`: the completion marker, with the bootstrap version. North writes it only after a clean run.
- `.bootstrap_progress.json`: the checkpoint of the files already done. North deletes it when the run completes.

## Rules

- Do not start a run without the user. Startup may only finish a run that the user started and that paused (`bootstrap_paused`).
- If no model is available for a file, pause: save the checkpoint and do not write the marker. A cancelled run keeps the facts that it stored.
- Test mode does not read personal files. `settings.onboarding_active` is false when `NORTH_ENV` is `test`, unless `onboarding_enabled` is set.
- Store each fact as one claim, with its source path, hash, time and evidence (`add_fact_with_provenance`). Drop facts that contain secrets, card numbers or government ids (`_is_usable_fact`).
- Survey files before any model call. A model call on an empty or copied file only costs money.
- Do blocking file work in a thread (CODING_STYLE §10.3). `survey_files` runs through `asyncio.to_thread`.
- Callers outside the package must use only public names (CODING_STYLE §7.1). Today `web/api.py` imports the private `_discover_files` and `_load_progress`. Do not add more such imports.
- The tests are in `tests/unit/bootstrap/`.
