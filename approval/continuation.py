"""What happens after a card is decided.

With a non-blocking card (#12) nobody is awaiting the decision, so resolving one
has to *produce the next step* rather than unblock a suspended coroutine. Until
this existed, `ApprovalStore.resolve` wrote a status and stopped: whatever you
decided, the same thing happened next, which was nothing.

The obvious shape - "approve enqueues a job" - is too narrow in three ways:

* **Reject is a decision too.** It has a next step: recording the reason, which
  is what #17 learns from, and in a flow, moving to the next candidate.
* **The edits are inputs, not a record.** A card comes back with *your* values,
  so the next step runs on what you decided rather than what north proposed.
* **It is not a boolean.** `Card.options` and `chosen_option` already allow
  "Approve / Reject / Approve without the cover letter" - three next steps.

So: resolving a card emits the decision, and whatever created the card decides
what follows. The approval layer stays the consent boundary and learns nothing
about jobs, flows or applications - the same reasoning that rejected a separate
`proposals/` module.

`Card.source` is the hook. A card with no registered source behaves exactly as
it did before, which is what keeps the ordinary "may I run this command?"
guard-rail from acquiring a continuation it has no use for.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from approval.models import Card

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CardOutcome:
    """A decided card, as the thing that created it needs to read it."""

    card: Card
    decision: str
    chosen_option: str
    values: dict[str, Any]

    @property
    def source(self) -> str:
        return self.card.source

    @property
    def approved(self) -> bool:
        return self.decision == "approved"

    @property
    def rejected(self) -> bool:
        """True for an explicit refusal only.

        A card that expired is not a refusal - nobody was there - and the two
        must not collapse, or a flow treats "you were asleep" as "you said no"
        and learns from it.
        """
        return self.decision == "rejected"

    @property
    def unanswered(self) -> bool:
        return self.decision == "timeout_rejected"


# Returns nothing: the handler decides what the next step is and starts it. The
# registry deliberately cannot see a Job, so nothing here depends on jobs/.
NextStep = Callable[[CardOutcome], Awaitable[None]]


class CardContinuations:
    """Which next step belongs to which card source.

    Registration is by `Card.source`, the field a card already carries to say
    what produced it. One handler per source; registering the same source twice
    replaces it, so a hot-reloaded flow does not accumulate handlers.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, NextStep] = {}
        # Kept so tasks are not garbage-collected mid-flight, and so tests can
        # await them rather than sleeping.
        self._running: set[asyncio.Task] = set()

    def register(self, source: str, handler: NextStep) -> None:
        if not source:
            raise ValueError("a continuation needs a source to be registered against")
        self._handlers[source] = handler

    def unregister(self, source: str) -> None:
        self._handlers.pop(source, None)

    def registered_for(self, source: str) -> bool:
        return source in self._handlers

    def dispatch(self, outcome: CardOutcome) -> asyncio.Task | None:
        """Start the next step for *outcome*, if its source registered one.

        Called from `ApprovalStore.resolve`, which is synchronous and must stay
        that way: a decision is recorded whether or not anything follows it, and
        the recording must not be able to fail because the follow-up did. So the
        handler runs as its own task and its failures are logged, never raised
        back into the resolve that triggered it.
        """
        handler = self._handlers.get(outcome.source)
        if handler is None:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Resolved from a thread with no event loop - a CLI, a test. The
            # decision still stands; only the follow-up cannot start here.
            logger.warning(
                "Card %s resolved outside an event loop - the next step for %r did not start",
                outcome.card.id,
                outcome.source,
            )
            return None
        task = loop.create_task(self._run(handler, outcome), name=f"continuation:{outcome.source}")
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        return task

    @staticmethod
    async def _run(handler: NextStep, outcome: CardOutcome) -> None:
        try:
            await handler(outcome)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("The next step for card %s (source %r) failed", outcome.card.id, outcome.source)

    async def drain(self) -> None:
        """Wait for in-flight next steps. For shutdown and for tests."""
        while self._running:
            await asyncio.gather(*list(self._running), return_exceptions=True)
