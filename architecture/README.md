# architecture

The `architecture` package holds north's module contracts and the code that checks them. `modules.yaml` names each module, the paths it owns, its layer and the layers it may import. The tests in `tests/unit/architecture/` load these contracts and fail when the code breaks them. The package is the protected module `architecture` in its own manifest: no agent may edit it without the user's approval (`docs/MODULES.md`).

## Main parts

| File | Key names | What it does |
|---|---|---|
| `modules.yaml` | `layers`, `modules` | The manifest (`version: 1`). Six layers with their `may_import` lists, and one entry per module with `paths`, `excludes`, `layer`, `owner`, `editable_by`, `protected`, `public_contracts` and `validation`. |
| `contracts.py` | `load_module_manifest`, `ModuleManifest`, `ModuleContract`, `ModuleManifestError` | Loads and validates the manifest. `ModuleManifest.module_for(path)` returns the one module that owns a repository path, or raises. |
| `imports.py` | `local_import_edges`, `forbidden_import_edges`, `load_import_baseline`, `ImportEdge` | Parses the Python files with `ast` and finds each import between two modules. An import is forbidden when the target layer is not in the source layer's `may_import` list. |
| `import-baseline.txt` | one `source -> target` pair per line | The forbidden imports that exist today. A new forbidden pair fails the tests. |
| `scopes.py` | `TaskEditScope`, `ScopeGuard`, `build_guard` | Decides if a task may edit a path: the path must be in a permitted module or path, and a protected path needs an explicit `authorized_paths` match. |

The layers, from the lowest: `platform`, `intelligence`, `application` and `integrations`, `interfaces`, `composition`. A module may import only the layers that its own layer lists in `may_import`.

## How north uses it

- `tests/unit/architecture/test_contracts.py` checks that each production file has exactly one owner, and that the `architecture` and `safety_policy` modules are not agent-editable.
- `tests/unit/architecture/test_imports.py` checks that the forbidden imports are exactly the pairs in `import-baseline.txt`.
- `tests/unit/architecture/test_scopes.py` and `tests/unit/tools/test_edit_scope_enforcement.py` test `TaskEditScope` and `ScopeGuard`.
- `tests/unit/architecture/test_approval_layer.py` checks that only `approval/` decides approvals (CODING_STYLE §7.3). `test_packaging.py` checks the `north` console script and the package data. These two tests do not use the manifest.
- `utils/edit_scope.py` defines the `EditAuthorizer` protocol that `ScopeGuard` implements. `ToolInput`, `AgentPayload` and `TaskRequest` carry it as `edit_scope`, and `tools/_path.py` calls it in `scope_refusal`. These modules do not import `architecture`.
- No production module imports `architecture` today. Only tests call `build_guard`.
- `docs/MODULES.md` explains the catalog for people. `docs/adr/` holds the decisions behind it.

Run the checks with `uv run pytest tests/unit/architecture -q`.

## Rules

- Do not change this package, `docs/adr/`, `docs/refactor/` or `docs/MODULES.md` without the user's approval. The module is `protected` and its `editable_by` list is empty.
- Add each new production path to a module in `modules.yaml` in the same change. A path with no owner, or with two owners, fails `test_contracts.py`.
- Do not add a pair to `import-baseline.txt` to make a test pass. Remove a pair only after you replace the import and test the change. The header of the file states this rule.
- A protected path needs an explicit match in `authorized_paths`, even when its module is in `modules`.
- Build a `ScopeGuard` from a trusted `TaskEditScope`, never from parameters that a model supplied (`scopes.py`).
- `imports.py` and `test_contracts.py` each keep a hard-coded list of top-level package names. If you add a top-level package, update both lists.
- Module boundaries are hard. A module calls only another module's public interface (CODING_STYLE §7.1, §7.2).
