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
