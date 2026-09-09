"""User-configurable inference strategy. Controls model selection order."""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from approval.mode import ApprovalMode

logger = logging.getLogger(__name__)


class StrategyMode(StrEnum):
    ECO = "eco"  # cheapest model first - maximise cost savings
    CRUISE = "cruise"  # role-aware best fit (default)
    SPORT = "sport"  # most capable model first - maximise quality


class RoutingMode(StrEnum):
    """Who picks the model: north, or the user.

    ``AUTO`` is the whole of what north does today - rank every model it can
    reach against what this part of the task needs, and walk that chain. ``MANUAL``
    names one model and calls only that, which is what makes an experiment
    repeatable: hold the model still, change one thing, compare. Everything the
    ranking would have decided is skipped, so the power dial means nothing here.
    """

    AUTO = "auto"
    MANUAL = "manual"


# `power` is the user-facing name for the model-selection dial.
PowerMode = StrategyMode


_DESCRIPTIONS = {
    StrategyMode.ECO: "Cheapest model first. Saves cost; quality may vary on hard tasks.",
    StrategyMode.CRUISE: "Best fit per task. Balances cost and quality automatically.",
    StrategyMode.SPORT: "Most capable model first. Best quality; higher cost.",
}

_ROUTING_DESCRIPTIONS = {
    RoutingMode.AUTO: "North picks the model for each part of a task.",
    RoutingMode.MANUAL: "One model answers everything. Power has no effect.",
}


def describe_routing(mode: RoutingMode) -> str:
    return _ROUTING_DESCRIPTIONS[mode]


def describe(mode: StrategyMode) -> str:
    return _DESCRIPTIONS[mode]


class NorthSettings:
    """Persistent user settings stored at ~/.north/settings.json."""

    _DEFAULT_POWER = StrategyMode.CRUISE
    _DEFAULT_ROUTING = RoutingMode.AUTO
    # How long a *blocking* card waits for an answer. Five minutes was tuned
    # for a card raised while you watched; the real ceiling is the stuck-task
    # watchdog at 24h, so this was needlessly tight - a question asked while
    # you were making coffee expired unanswered and the agent was told the
    # action had been refused. Prepared work does not use this at all: a
    # non-blocking card has no timeout, because nothing is waiting on it.
    _DEFAULT_APPROVAL_TIMEOUT = 1800.0
    # The previous default, kept only to recognise a settings file that never
    # made a choice. Remove once no install can still be carrying it.
    _SUPERSEDED_APPROVAL_TIMEOUT = 300.0

    def __init__(self, path: Path, default_approval_mode: ApprovalMode | None = None) -> None:
        from approval.mode import ApprovalMode

        self._path = path
        self._power: StrategyMode = self._DEFAULT_POWER
        self._approval_timeout_seconds: float = self._DEFAULT_APPROVAL_TIMEOUT
        # Startup default (e.g. from NORTH_APPROVAL_MODE); settings.json overrides it.
        self._autonomy: ApprovalMode = default_approval_mode or ApprovalMode.INTERACTIVE
        # Per-part routing overrides (see inference/routing/parts.py). Profiles are
        # data, so an install can retune which part gets which model without a code
        # change. Persisted only when deliberately set, like scoring above.
        self._routing_parts: dict[str, object] = {}
        # Who picks the model, and - in manual mode - which one. The model is kept
        # even while the mode is auto, so switching back to manual does not make
        # the user find their model again.
        self._routing_mode: RoutingMode = self._DEFAULT_ROUTING
        self._routing_model: str = ""
        self._load()

    def _load(self) -> None:
        from approval.mode import parse_approval_mode

        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            raw_power = data.get("power")
            self._power = StrategyMode(raw_power or self._DEFAULT_POWER.value)
            stored_timeout = float(data.get("approval_timeout_seconds", self._DEFAULT_APPROVAL_TIMEOUT))
            # An install that never chose a timeout has the *old* default written
            # into its settings file, so raising the default alone would not reach
            # the people it was raised for. A value equal to the superseded default
            # is read as "never chosen" and moves up; anything else is a choice and
            # is left exactly as it is.
            self._approval_timeout_seconds = (
                self._DEFAULT_APPROVAL_TIMEOUT
                if stored_timeout == self._SUPERSEDED_APPROVAL_TIMEOUT
                else stored_timeout
            )
            raw_autonomy = data.get("autonomy")
            self._autonomy = parse_approval_mode(raw_autonomy) or self._autonomy
            routing = data.get("routing")
            if isinstance(routing, dict):
                if isinstance(routing.get("parts"), dict):
                    self._routing_parts = routing["parts"]
                # An unreadable mode falls back to auto rather than failing the
                # load: north picking a model is always a working answer, where
                # a half-understood manual setting is not.
                try:
                    self._routing_mode = RoutingMode(str(routing.get("mode") or self._DEFAULT_ROUTING.value))
                except ValueError:
                    logger.warning("Unknown routing mode %r in settings.json - using auto", routing.get("mode"))
                self._routing_model = str(routing.get("model") or "")
        except Exception as exc:
            logger.warning(
                "settings.json is unreadable - resetting to defaults (%s): %s",
                self._path,
                exc,
            )

    @property
    def power(self) -> StrategyMode:
        return self._power

    @property
    def approval_timeout_seconds(self) -> float:
        return self._approval_timeout_seconds

    @property
    def autonomy(self) -> ApprovalMode:
        return self._autonomy

    @property
    def routing_mode(self) -> RoutingMode:
        """Whether north picks the model, or the user has pinned one."""
        return self._routing_mode

    @property
    def routing_model(self) -> str:
        """The pinned model as ``provider:model_id``. Only meaningful in manual mode."""
        return self._routing_model

    @property
    def pinned_model(self) -> str:
        """The model to route to, or "" when north is choosing.

        The one question routing actually asks. Reading the mode and the model
        separately is how a pin left behind by a switch back to auto ends up
        silently applied.
        """
        return self._routing_model if self._routing_mode is RoutingMode.MANUAL else ""

    @property
    def routing_parts(self) -> dict[str, object]:
        """Raw per-part routing overrides; parsed by inference.routing.parts."""
        return self._routing_parts

    def set_routing_parts(self, parts: dict[str, object]) -> None:
        self._routing_parts = parts if isinstance(parts, dict) else {}
        self._save()

    def set_routing(self, mode: RoutingMode | None = None, model: str | None = None) -> None:
        """Change who picks the model, the model itself, or both."""
        if mode is not None:
            self._routing_mode = mode
        if model is not None:
            self._routing_model = model.strip()
        self._save()

    def set_power(self, mode: StrategyMode) -> None:
        self._power = mode
        self._save()

    def set_approval_timeout(self, seconds: float) -> None:
        self._approval_timeout_seconds = max(10.0, seconds)
        self._save()

    def set_autonomy(self, mode: ApprovalMode) -> None:
        self._autonomy = mode
        self._save()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data: dict[str, object] = {
                "power": self._power.value,
                "approval_timeout_seconds": self._approval_timeout_seconds,
                "autonomy": self._autonomy.value,
            }
            routing: dict[str, object] = {"mode": self._routing_mode.value}
            if self._routing_model:
                routing["model"] = self._routing_model
            if self._routing_parts:
                routing["parts"] = self._routing_parts
            data["routing"] = routing
            self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to persist settings to %s: %s", self._path, exc)
