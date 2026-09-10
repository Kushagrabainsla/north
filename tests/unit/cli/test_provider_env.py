from __future__ import annotations

from cli.provider_env import load_env_keys, update_env_file


def test_load_env_keys_parses_and_trims_entries(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("A = one\nINVALID\nB= two \n", encoding="utf-8")

    assert load_env_keys(env_file) == {"A": "one", "B": "two"}
    assert load_env_keys(tmp_path / "missing") == {}


def test_update_env_file_replaces_then_appends_and_exports(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("A=old\n", encoding="utf-8")
    monkeypatch.delenv("A", raising=False)
    monkeypatch.delenv("B", raising=False)

    update_env_file(env_file, "A", "new")
    update_env_file(env_file, "B", "two")

    assert env_file.read_text(encoding="utf-8") == "A=new\nB=two\n"
    assert __import__("os").environ["A"] == "new"
    assert __import__("os").environ["B"] == "two"


def test_parse_provider_selection_keeps_valid_first_seen_indexes() -> None:
    from cli.provider_env import parse_provider_selection

    providers = ["first", "second", "third"]
    assert parse_provider_selection(" 2,1,2,0,4,nope ", providers) == ["second", "first"]


def test_provider_configuration_detection_uses_oauth_environment_and_env_file(monkeypatch) -> None:
    from cli.provider_env import any_provider_configured, provider_is_configured

    api = {"id": "api", "auth_kind": "api_key", "env_key": "NORTH_TEST_KEY"}
    oauth = {"id": "oauth", "auth_kind": "oauth_pkce", "env_key": ""}
    monkeypatch.delenv("NORTH_TEST_KEY", raising=False)

    assert provider_is_configured(api, {"NORTH_TEST_KEY": "from-file"}, lambda _: False)
    monkeypatch.setenv("NORTH_TEST_KEY", "from-process")
    assert provider_is_configured(api, {}, lambda _: False)
    assert provider_is_configured(oauth, {}, lambda provider_id: provider_id == "oauth")
    assert any_provider_configured([api, oauth], {}, lambda _: True)
