"""Single source of truth for North's inference-provider integrations."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from inference.codex_auth import CodexCredentialProvider
from inference.provider import Provider
from inference.providers.gemini import GeminiRouter
from inference.providers.groq import GroqRouter
from inference.providers.local_embeddings import LocalEmbeddingProvider
from inference.providers.openai_codex import OpenAICodexProvider
from inference.providers.opencode_zen import OpenCodeZenRouter
from inference.providers.openrouter import OpenRouterRouter


class AuthKind(StrEnum):
    API_KEY = "api_key"
    OAUTH_PKCE = "oauth_pkce"
    # Runs in this process. Nothing to authenticate, so it is always configured.
    LOCAL = "local"


@dataclass(frozen=True)
class ProviderDefinition:
    id: str
    display_name: str
    description: str
    auth_kind: AuthKind
    settings_field: str | None = None
    env_key: str | None = None
    setup_url: str | None = None
    fallback_order: int = 0
    factory: Callable[[str], Provider] | None = None

    def build(self, credential: str = "") -> Provider:
        if self.factory is None:
            raise ValueError(f"No provider factory registered for {self.id!r}")
        return self.factory(credential)

    def is_configured(self, settings: object | None = None) -> bool:
        if self.auth_kind is AuthKind.LOCAL:
            return True
        if self.auth_kind is AuthKind.API_KEY:
            return bool(self.resolve_credential(settings))
        if self.id == "openai_codex":
            return CodexCredentialProvider().status().configured
        return False

    def resolve_credential(self, settings: object | None = None) -> str:
        """Resolve a key from settings, the process environment, or North's .env.

        Reading the registry's ``env_key`` directly lets future providers work
        without adding another named argument to ``build_router``.
        """
        if self.auth_kind is not AuthKind.API_KEY or not self.env_key:
            return ""
        if self.settings_field and settings is not None:
            value = getattr(settings, self.settings_field, "")
            if value:
                return str(value)
        value = os.environ.get(self.env_key, "").strip()
        if value:
            return value
        north_home = getattr(settings, "north_home", None) if settings is not None else None
        env_path = (
            Path(north_home) / ".env"
            if north_home
            else Path(os.environ.get("NORTH_HOME", "~/.north")).expanduser() / ".env"
        )
        with contextlib.suppress(OSError):
            for line in env_path.read_text(encoding="utf-8").splitlines():
                key, separator, candidate = line.partition("=")
                if separator and key.strip() == self.env_key:
                    return candidate.strip()
        return ""


PROVIDER_DEFINITIONS: tuple[ProviderDefinition, ...] = (
    ProviderDefinition(
        id="local",
        display_name="Local embeddings",
        description="On-device embeddings for tool, code and memory search - no key required",
        auth_kind=AuthKind.LOCAL,
        # First in the fallback order: embeddings must come from one model
        # consistently (see utils/vector_space.py), and the local one is the only
        # provider guaranteed to be there. It serves no chat models, so this does
        # not affect completion routing at all.
        fallback_order=1,
        factory=lambda _="": LocalEmbeddingProvider(),
    ),
    ProviderDefinition(
        id="groq",
        display_name="Groq",
        description="Ultra-fast open-source models",
        auth_kind=AuthKind.API_KEY,
        settings_field="groq_api_key",
        env_key="NORTH_GROQ_API_KEY",
        setup_url="https://console.groq.com/keys",
        fallback_order=10,
        factory=GroqRouter,
    ),
    ProviderDefinition(
        id="gemini",
        display_name="Gemini",
        description="Google Gemini models",
        auth_kind=AuthKind.API_KEY,
        settings_field="gemini_api_key",
        env_key="NORTH_GEMINI_API_KEY",
        setup_url="https://aistudio.google.com/apikey",
        fallback_order=20,
        factory=GeminiRouter,
    ),
    ProviderDefinition(
        id="opencode_zen",
        display_name="OpenCode Zen",
        description="OpenCode free and paid models",
        auth_kind=AuthKind.API_KEY,
        settings_field="opencode_zen_api_key",
        env_key="NORTH_OPENCODE_ZEN_API_KEY",
        setup_url="https://opencode.ai/auth",
        fallback_order=30,
        factory=OpenCodeZenRouter,
    ),
    ProviderDefinition(
        id="openai_codex",
        display_name="OpenAI Codex",
        description="Use a ChatGPT Codex subscription via browser login",
        auth_kind=AuthKind.OAUTH_PKCE,
        fallback_order=40,
        factory=lambda _: OpenAICodexProvider(CodexCredentialProvider()),
    ),
    ProviderDefinition(
        id="openrouter",
        display_name="OpenRouter",
        description="Broad multi-vendor model coverage",
        auth_kind=AuthKind.API_KEY,
        settings_field="openrouter_api_key",
        env_key="NORTH_OPENROUTER_API_KEY",
        setup_url="https://openrouter.ai/keys",
        fallback_order=100,
        factory=OpenRouterRouter,
    ),
)


def get_provider_definition(provider_id: str) -> ProviderDefinition:
    normalized = provider_id.strip().lower().replace("-", "_")
    for definition in PROVIDER_DEFINITIONS:
        if definition.id == normalized:
            return definition
    available = ", ".join(item.id.replace("_", "-") for item in PROVIDER_DEFINITIONS)
    raise ValueError(f"Unknown provider {provider_id!r}. Available: {available}")
