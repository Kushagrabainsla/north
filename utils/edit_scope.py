"""Structural contract for server-owned edit-scope enforcement.

The concrete authority is :class:`architecture.scopes.TaskEditScope` bound to a
workspace and the module manifest (see :class:`architecture.scopes.ScopeGuard`).
Runtime layers that must *carry* or *consult* a scope - the task request, the
agent payload, and the tool input - only ever see this structural
:class:`EditAuthorizer` shape, never the ``architecture`` package itself. That
keeps the integrations/application layers from taking a forbidden inward-only
dependency on the composition-layer ``architecture`` module while still letting a
scope, minted at the composition root, ride the request all the way to the
mutation site.

An authorizer answers one question: may this absolute path be edited? It returns
``None`` to permit the edit, or a human-readable refusal reason to deny it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class EditAuthorizer(Protocol):
    """Decides whether one absolute filesystem path may be mutated.

    Implemented by :class:`architecture.scopes.ScopeGuard`. Kept structural so no
    runtime layer imports ``architecture`` merely to pass a scope through.
    """

    def authorize(self, path: Path) -> str | None:
        """Return ``None`` when the edit is allowed, else a refusal reason."""
        ...
