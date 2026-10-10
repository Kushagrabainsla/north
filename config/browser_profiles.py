"""Browser configuration, not a new registry, routing engine, or permission policy."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from config.browser_connection import cdp_http_base


class BrowserProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    purpose: str = Field(min_length=1, max_length=1000)
    context: Literal["isolated", "existing"] = "isolated"
    # Existing browser identity comes from chrome://version, not this label.
    data_directory: str = ""
    profile_directory: str = Field(default="Default", pattern=r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,100}$")
    connect: str = ""
    headed: bool = True
    enabled: bool = True

    @model_validator(mode="after")
    def validate_connection(self):
        if self.context == "isolated":
            if self.connect or self.data_directory:
                raise ValueError("A North-managed profile cannot attach to or copy an existing profile.")
        else:
            path = Path(self.data_directory)
            if not self.data_directory or not path.is_absolute() or path == Path(path.anchor):
                raise ValueError("Existing profiles need their browser's absolute data directory.")
            if self.connect:
                cdp_http_base(self.connect)
        return self

    @property
    def browser_name(self) -> str:
        return f"north-{self.id}"

    @property
    def managed_directory(self) -> Path:
        # chrome-agent owns launch/session storage; North never copies its data.
        return Path.home() / ".chrome-agent" / "browsers" / self.browser_name / "chromium-profile"

    @property
    def expected_path(self) -> Path:
        return (Path(self.data_directory) / self.profile_directory).resolve()

    def catalog_entry(self) -> dict:
        return {key: getattr(self, key) for key in ("id", "name", "purpose", "context", "enabled")}


class SetupProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["not_started", "in_progress", "completed", "skipped"] = "not_started"
    step: int = Field(default=0, ge=0, le=4)


def discover_browser_profiles(home: Path | None = None) -> list[dict]:
    """Names/directories only, on explicit setup discovery. No credentials/history."""
    home = home or Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
        roots = {
            "Chrome": base / "Google/Chrome",
            "Chromium": base / "Chromium",
            "Brave": base / "BraveSoftware/Brave-Browser",
        }
    elif sys.platform == "win32":
        base = home / "AppData/Local"
        roots = {"Chrome": base / "Google/Chrome/User Data", "Chromium": base / "Chromium/User Data"}
    else:
        base = home / ".config"
        roots = {
            "Chrome": base / "google-chrome",
            "Chromium": base / "chromium",
            "Brave": base / "BraveSoftware/Brave-Browser",
        }
    result = []
    for browser, root in roots.items():
        try:
            metadata = json.loads((root / "Local State").read_text(encoding="utf-8"))
            profiles = metadata.get("profile", {}).get("info_cache", {})
            if not isinstance(profiles, dict):
                continue
            for directory, info in profiles.items():
                if not isinstance(info, dict) or not isinstance(directory, str):
                    continue
                try:
                    profile = BrowserProfile(
                        id=hashlib.sha256(str(root / directory).encode()).hexdigest()[:16],
                        name=str(info.get("name") or directory),
                        purpose="Choose what this profile is for",
                        context="existing",
                        data_directory=str(root),
                        profile_directory=directory,
                    )
                except ValueError:
                    continue
                if not (root / directory).is_dir():
                    continue
                result.append({**profile.model_dump(), "browser": browser})
        except (OSError, ValueError, AttributeError):
            continue
    return result[:32]
