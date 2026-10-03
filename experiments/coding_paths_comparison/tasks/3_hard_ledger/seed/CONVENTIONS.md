# Conventions

- Money is integer cents. Never use floats for money.
- A transaction's day is its UTC date. Statements carry timestamps with an offset; the ledger buckets by UTC.
- A statement row's `id` identifies it. The same id is the same transaction, however many times it is imported.
- Do not add dependencies.
- Existing tests describe behaviour that must keep working. Do not edit them; add new tests in a new file.
