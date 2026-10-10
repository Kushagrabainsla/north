from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.browser_connection import active_cdp_endpoint
from config.browser_profiles import BrowserProfile, discover_browser_profiles
from config.strategy import NorthSettings


def profile(**overrides):
    return BrowserProfile(id="university", name="University", purpose="University work and applications", **overrides)


def test_profiles_and_setup_roundtrip_preserve_existing_settings(tmp_path):
    settings = NorthSettings(tmp_path / "settings.json")
    settings.set_timezone("America/Los_Angeles")
    settings.set_routing(model="provider:model")
    settings.set_browser_profiles([profile()])
    settings.set_setup_progress({"status": "in_progress", "step": 2})
    reloaded = NorthSettings(tmp_path / "settings.json")
    assert reloaded.browser_profiles == [profile()]
    assert reloaded.setup_progress == {"status": "in_progress", "step": 2}
    assert reloaded.timezone_configured
    assert reloaded.timezone == "America/Los_Angeles"
    assert reloaded.routing_model == "provider:model"


def test_duplicate_ids_are_rejected_atomically(tmp_path):
    settings = NorthSettings(tmp_path / "settings.json")
    settings.set_browser_profiles([profile()])
    with pytest.raises(ValueError):
        settings.set_browser_profiles([profile(), profile()])
    assert settings.browser_profiles == [profile()]


def test_saving_optional_setup_does_not_mark_timezone_configured(tmp_path):
    path = tmp_path / "settings.json"
    settings = NorthSettings(path)
    settings.set_browser_profiles([profile()])
    assert not NorthSettings(path).timezone_configured


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "../private"},
        {"name": ""},
        {"purpose": ""},
        {"connect": "9222"},
        {"data_directory": "/tmp/browser"},
        {"context": "existing", "data_directory": "relative"},
        {"context": "existing", "data_directory": "/"},
        {"context": "existing", "data_directory": "/tmp/browser", "profile_directory": "../private"},
        {"context": "existing", "data_directory": "/tmp/browser", "connect": "https://remote.example:9222"},
        {"context": "existing", "data_directory": "/tmp/browser", "connect": "http://user:pass@localhost:9222"},
    ],
)
def test_unsafe_profile_configuration_is_refused(overrides):
    with pytest.raises(ValueError):
        BrowserProfile.model_validate({"id": "test", "name": "Test", "purpose": "Test work", **overrides})


def test_malformed_optional_settings_do_not_break_legacy_dials(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"timezone": "Asia/Kolkata", "browser_profiles": None, "setup": {"step": 99}}))
    settings = NorthSettings(path)
    assert settings.timezone == "Asia/Kolkata"
    assert not settings.browser_profiles
    assert settings.setup_progress["status"] == "not_started"


def test_discovery_returns_names_and_directories_not_private_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr("config.browser_profiles.sys.platform", "linux")
    root = tmp_path / ".config/google-chrome"
    (root / "Profile 1").mkdir(parents=True)
    (root / "Local State").write_text(
        json.dumps(
            {
                "profile": {
                    "info_cache": {
                        "Profile 1": {"name": "University", "user_name": "private@example.com", "secret": "password"},
                        "../private": {"name": "Bad"},
                    }
                }
            }
        )
    )
    [found] = discover_browser_profiles(tmp_path)
    assert found["name"] == "University"
    assert "private@example.com" not in json.dumps(found)
    assert "password" not in json.dumps(found)
    assert found["profile_directory"] == "Profile 1"


def test_endpoint_is_only_read_from_selected_directory(tmp_path):
    (tmp_path / "DevToolsActivePort").write_text("9222\n/devtools/browser/selected\n")
    assert active_cdp_endpoint(tmp_path) == "ws://127.0.0.1:9222/devtools/browser/selected"
    with pytest.raises(ValueError, match="not restart Chrome"):
        active_cdp_endpoint(tmp_path / "other")


def test_profile_catalog_does_not_expose_connection_details():
    item = profile(context="existing", data_directory="/tmp/private", connect="9222")
    assert set(item.catalog_entry()) == {"id", "name", "purpose", "context", "enabled"}
    assert Path(item.expected_path).name == "Default"
