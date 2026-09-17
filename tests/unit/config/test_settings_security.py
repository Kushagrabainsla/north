"""Tests for settings security: key-file permissions and trusted .env sources
(review findings R2#14, R3#15)."""

from __future__ import annotations

from pathlib import Path

from config.settings import Settings, read_secret_file


class TestSecretFilePermissions:
    def test_world_readable_key_is_tightened_to_0600(self, tmp_path: Path) -> None:
        key = tmp_path / "secret.key"
        key.write_text("s3cret\n", encoding="utf-8")
        key.chmod(0o644)

        assert read_secret_file(key) == "s3cret"
        assert (key.stat().st_mode & 0o777) == 0o600

    def test_owner_only_key_reads_normally(self, tmp_path: Path) -> None:
        key = tmp_path / "secret.key"
        key.write_text("s3cret\n", encoding="utf-8")
        key.chmod(0o600)

        assert read_secret_file(key) == "s3cret"
        assert (key.stat().st_mode & 0o777) == 0o600


class TestTrustedEnvSources:
    def test_cwd_dotenv_is_not_a_config_source(self) -> None:
        """A .env in an arbitrary cloned repo must never override NORTH_SECRET et al."""
        env_file = Settings.model_config["env_file"]
        files = [env_file] if isinstance(env_file, str) else list(env_file)
        assert ".env" not in files  # the bare CWD-relative entry
        assert all(str(Path.home()) in f for f in files)


class TestRuntimeEnvironment:
    def test_canonical_north_env_is_recognized_and_wins_legacy_alias(self, monkeypatch) -> None:
        monkeypatch.setenv("NORTH_ENV", "test")
        monkeypatch.setenv("NORTH_NORTH_ENV", "production")

        assert Settings(_env_file=None).north_env == "test"

    def test_legacy_north_env_alias_remains_supported(self, monkeypatch) -> None:
        monkeypatch.delenv("NORTH_ENV", raising=False)
        monkeypatch.setenv("NORTH_NORTH_ENV", "test")

        assert Settings(_env_file=None).north_env == "test"

    def test_runtime_environment_can_still_be_constructed_by_field_name(self) -> None:
        assert Settings(_env_file=None, north_env="test").north_env == "test"

    def test_test_mode_is_inert_unless_explicitly_enabled(self, monkeypatch) -> None:
        monkeypatch.setenv("NORTH_ENV", "test")
        monkeypatch.delenv("NORTH_AUTONOMOUS_BACKGROUND_TASKS_ENABLED", raising=False)
        monkeypatch.delenv("NORTH_ONBOARDING_ENABLED", raising=False)

        defaults = Settings(_env_file=None)
        assert defaults.autonomous_background_tasks_active is False
        assert defaults.onboarding_active is False

        monkeypatch.setenv("NORTH_AUTONOMOUS_BACKGROUND_TASKS_ENABLED", "true")
        monkeypatch.setenv("NORTH_ONBOARDING_ENABLED", "true")
        opted_in = Settings(_env_file=None)
        assert opted_in.autonomous_background_tasks_active is True
        assert opted_in.onboarding_active is True

    def test_normal_modes_keep_runtime_services_enabled(self, monkeypatch) -> None:
        monkeypatch.setenv("NORTH_ENV", "production")
        monkeypatch.delenv("NORTH_AUTONOMOUS_BACKGROUND_TASKS_ENABLED", raising=False)
        monkeypatch.delenv("NORTH_ONBOARDING_ENABLED", raising=False)

        configured = Settings(_env_file=None)
        assert configured.autonomous_background_tasks_active is True
        assert configured.onboarding_active is True
