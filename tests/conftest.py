"""Shared pytest fixtures for north.

Fixtures used by more than one test file live here. Fixtures scoped to a
single module live in that module's own conftest.py. See CODING_STYLE.md
Section 18.2.

Intentionally empty: production fixtures (e.g. `deps`, `ledger`,
`context_store`) wire to `config.dependencies`, which does not exist yet.
They are added in the same change as the modules they depend on.
"""

from __future__ import annotations
