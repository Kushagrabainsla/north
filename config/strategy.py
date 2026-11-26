"""User-configurable inference strategy. Controls model selection order."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from config.approval_mode import ApprovalMode

logger = logging.getLogger(__name__)


class StrategyMode(StrEnum):
    ECO = "eco"  # cheapest model first - maximise cost savings
    CRUISE = "cruise"  # role-aware best fit (default)
    SPORT = "sport"  # most capable model first - maximise quality


class WorkKind(StrEnum):
    """The kinds of work north does, each with its own row on the Settings page (#80).

    Which part of north belongs to which kind is `inference.routing.parts.kind_of`.
    """

    TASKS = "tasks"  # the work you ask for
    CHECKS = "checks"  # north checking its own answers against the task and your goals
    APPROVALS = "approvals"  # deciding for you whether an action is safe
    BACKGROUND = "background"  # memory upkeep, summaries and the other small jobs


_KIND_TEXT = {
    WorkKind.TASKS: ("Your tasks", "The work you ask north to do."),
    WorkKind.CHECKS: ("Checks", "North checks an answer, and compares a task with your goals."),
    WorkKind.APPROVALS: ("Approvals", "North decides for you if an action is safe."),
    WorkKind.BACKGROUND: ("Background", "Memory upkeep, summaries and other small jobs."),
}


def describe_kind(kind: WorkKind) -> tuple[str, str]:
    """The name and one-line description every surface shows for a kind of work."""
    return _KIND_TEXT[kind]


@dataclass(frozen=True)
class ModelChoice:
    """One row of the Models panel: a model you chose, or north choosing with a power.

    ``model`` is "provider:model_id", or "" for Auto: north ranks every model it can
    reach and walks that chain, ordered by ``power``. A chosen model is the only one
    called - a model that fails stops the call rather than falling back, because the
    point of naming it is that the answer came from it. So power means nothing there.
    """

    model: str = ""
    power: StrategyMode = StrategyMode.CRUISE

    @property
    def auto(self) -> bool:
        return not self.model

    def as_dict(self) -> dict[str, str]:
        # Power is kept under a chosen model too, so a row set back to Auto has its power again.
        return {"model": self.model, "power": self.power.value}

    @classmethod
    def parse(cls, raw: object) -> ModelChoice:
        """A row from settings.json or the API; raises ValueError for an unknown power."""
        if not isinstance(raw, dict):
            raise ValueError(f"A model row is an object, not {raw!r}")
        model = str(raw.get("model") or "").strip()
        return cls(model=model, power=StrategyMode(str(raw.get("power") or StrategyMode.CRUISE.value)))


_DESCRIPTIONS = {
    StrategyMode.ECO: "Cheapest model first. Saves cost; quality may vary on hard tasks.",
    StrategyMode.CRUISE: "Best fit per task. Balances cost and quality automatically.",
    StrategyMode.SPORT: "Most capable model first. Best quality; higher cost.",
}


def describe(mode: StrategyMode) -> str:
    return _DESCRIPTIONS[mode]


class NorthSettings:
    """Persistent user settings stored at ~/.north/settings.json."""

    # There is no approval timeout: a card waits until it is answered
    # (CODING_STYLE §13.5). An older settings.json may still carry
    # "approval_timeout_seconds"; it is ignored and dropped on the next save.

    def __init__(
        self,
        path: Path,
        default_approval_mode: ApprovalMode | None = None,
        default_timezone: str = "UTC",
    ) -> None:
        from config.approval_mode import ApprovalMode

        self._path = path
        # The Models panel: the Default row, and a row for each kind of work that does not
        # use it. A kind with no row here is "Same as default".
        self._default = ModelChoice()
        self._kinds: dict[WorkKind, ModelChoice] = {}
        # Startup default (e.g. from NORTH_APPROVAL_MODE); settings.json overrides it.
        self._autonomy: ApprovalMode = default_approval_mode or ApprovalMode.ASK
        # Per-part routing overrides (see inference/routing/parts.py). Profiles are
        # data, so an install can retune which part gets which model without a code
        # change. Persisted only when deliberately set, like scoring above.
        self._routing_parts: dict[str, object] = {}
        # A named zone is persisted rather than an offset so recurring wall
        # clocks continue to mean the same thing through daylight-saving time.
        self._timezone: str = default_timezone if _is_known_timezone(default_timezone) else "UTC"
        self._timezone_configured = False
        self._browser_profiles: list = []
        self._setup_progress: dict = {"status": "not_started", "step": 0}
        self._load()

    def _load(self) -> None:
        from config.approval_mode import parse_approval_mode

        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            raw_autonomy = data.get("autonomy")
            self._autonomy = parse_approval_mode(raw_autonomy) or self._autonomy
            routing = data.get("routing")
            if not isinstance(routing, dict):
                routing = {}
            if isinstance(routing.get("parts"), dict):
                self._routing_parts = routing["parts"]
            self._load_models(routing, data.get("power"))
            raw_timezone = str(data.get("timezone") or "").strip()
            if raw_timezone:
                if _is_known_timezone(raw_timezone):
                    self._timezone = raw_timezone
                    self._timezone_configured = data.get("timezone_configured", True) is True
                else:
                    logger.warning("Unknown timezone %r in settings.json - using %s", raw_timezone, self._timezone)
            from config.browser_profiles import BrowserProfile, SetupProgress

            raw_profiles = data.get("browser_profiles", [])
            for raw in raw_profiles[:24] if isinstance(raw_profiles, list) else []:
                try:
                    profile = BrowserProfile.model_validate(raw)
                    if not any(saved.id == profile.id for saved in self._browser_profiles):
                        self._browser_profiles.append(profile)
                except ValueError:
                    logger.warning("Ignoring invalid saved browser profile")
            try:
                self._setup_progress = SetupProgress.model_validate(data.get("setup", {})).model_dump()
            except ValueError:
                logger.warning("Ignoring invalid saved setup progress")
        except Exception as exc:
            logger.warning(
                "settings.json is unreadable - resetting to defaults (%s): %s",
                self._path,
                exc,
            )

    def _load_models(self, routing: dict, old_power: object) -> None:
        """Read the Models panel; an unreadable row is dropped, never the whole file.

        Before #80 there was one choice for all of north: `routing.mode` and `routing.model`,
        and `power` at the top level. That one choice becomes the Default row, and every
        kind uses it, so nothing changes until a row is changed.
        """
        if "default" in routing:
            self._default = self._read_row(routing["default"], "default") or ModelChoice()
        else:
            model = str(routing.get("model") or "") if routing.get("mode") == "manual" else ""
            self._default = self._read_row({"model": model, "power": old_power}, "default") or ModelChoice()
        kinds = routing.get("kinds")
        for name, raw in (kinds if isinstance(kinds, dict) else {}).items():
            try:
                kind = WorkKind(name)
            except ValueError:
                logger.warning("Unknown kind of work %r in settings.json - ignored", name)
                continue
            if (row := self._read_row(raw, name)) is not None:
                self._kinds[kind] = row

    @staticmethod
    def _read_row(raw: object, name: str) -> ModelChoice | None:
        try:
            return ModelChoice.parse(raw)
        except ValueError:
            logger.warning("Unreadable %s row in settings.json - using its default: %r", name, raw)
            return None

    @property
    def power(self) -> StrategyMode:
        """The Default row's power: what "switch to eco mode" and `/power` change."""
        return self._default.power

    @property
    def autonomy(self) -> ApprovalMode:
        return self._autonomy

    @property
    def default_model(self) -> ModelChoice:
        """The Default row: what every kind of work without its own row uses."""
        return self._default

    def kind_model(self, kind: WorkKind) -> ModelChoice | None:
        """The row a kind of work has, or None for "Same as default"."""
        return self._kinds.get(kind)

    def model_for(self, kind: WorkKind) -> ModelChoice:
        """The choice in force for one kind of work - the one question routing asks."""
        return self._kinds.get(kind, self._default)

    def set_default_model(self, choice: ModelChoice) -> None:
        self._default = choice
        self._save()

    def set_kind_model(self, kind: WorkKind, choice: ModelChoice | None) -> None:
        """Give a kind its own row, or None to make it "Same as default" again."""
        if choice is None:
            self._kinds.pop(kind, None)
        else:
            self._kinds[kind] = choice
        self._save()

    @property
    def routing_parts(self) -> dict[str, object]:
        """Raw per-part routing overrides; parsed by inference.routing.parts."""
        return self._routing_parts

    @property
    def timezone(self) -> str:
        """The IANA zone North uses for local dates, times, and new schedules."""
        return self._timezone

    def set_power(self, mode: StrategyMode) -> None:
        """Set the Default row's power. It is used when the row is Auto."""
        self._default = replace(self._default, power=mode)
        self._save()

    def set_autonomy(self, mode: ApprovalMode) -> None:
        self._autonomy = mode
        self._save()

    def set_timezone(self, name: str) -> None:
        if not _is_known_timezone(name):
            raise ValueError(f"Unknown timezone {name!r}")
        self._timezone = name
        self._timezone_configured = True
        self._save()

    @property
    def timezone_configured(self) -> bool:
        return self._timezone_configured

    @property
    def browser_profiles(self) -> list:
        return list(self._browser_profiles)

    def set_browser_profiles(self, profiles: list) -> None:
        from config.browser_profiles import BrowserProfile

        parsed = [BrowserProfile.model_validate(profile) for profile in profiles]
        if len(parsed) > 24 or len({profile.id for profile in parsed}) != len(parsed):
            raise ValueError("Browser profiles need unique IDs; at most 24 can be saved.")
        self._browser_profiles = parsed
        self._save()

    @property
    def setup_progress(self) -> dict:
        return dict(self._setup_progress)

    def set_setup_progress(self, progress: dict) -> None:
        from config.browser_profiles import SetupProgress

        self._setup_progress = SetupProgress.model_validate(progress).model_dump()
        self._save()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data: dict[str, object] = {
                "autonomy": self._autonomy.value,
                "timezone": self._timezone,
                "timezone_configured": self._timezone_configured,
                "browser_profiles": [profile.model_dump() for profile in self._browser_profiles],
                "setup": self._setup_progress,
            }
            routing: dict[str, object] = {
                "default": self._default.as_dict(),
                "kinds": {kind.value: choice.as_dict() for kind, choice in self._kinds.items()},
            }
            if self._routing_parts:
                routing["parts"] = self._routing_parts
            data["routing"] = routing
            self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to persist settings to %s: %s", self._path, exc)


def _is_known_timezone(name: str) -> bool:
    """Validate a setting inside config without crossing platform module boundaries."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True
