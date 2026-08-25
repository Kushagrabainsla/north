"""Authentication contracts shared by inference providers.

Credentials are deliberately independent from wire protocols.  API-key
providers and rotating OAuth providers can therefore use the same HTTP and
dispatcher lifecycle without capturing a token in an AsyncClient forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AuthContext:
    """Headers and non-secret account metadata for one authenticated request."""

    headers: dict[str, str]
    account_id: str | None = None
    expires_at: datetime | None = None
    source: str = ""


@dataclass(frozen=True)
class AuthStatus:
    """Safe-to-display credential state."""

    configured: bool
    provider_id: str
    account_id: str | None = None
    expires_at: datetime | None = None
    needs_login: bool = False
    detail: str = ""


@runtime_checkable
class CredentialProvider(Protocol):
    """Supplies request credentials and optionally manages an interactive login."""

    provider_id: str

    async def get_auth(self) -> AuthContext: ...

    def status(self) -> AuthStatus: ...

    async def login(self, *, open_browser: bool = True) -> AuthStatus: ...

    async def logout(self) -> None: ...


@dataclass
class ApiKeyCredentialProvider:
    """Static bearer-token credentials used by OpenAI-compatible providers."""

    provider_id: str
    api_key: str = field(repr=False)
    header_name: str = "Authorization"
    prefix: str = "Bearer "

    async def get_auth(self) -> AuthContext:
        return AuthContext(
            headers={self.header_name: f"{self.prefix}{self.api_key}"},
            source="api_key",
        )

    def status(self) -> AuthStatus:
        configured = bool(self.api_key)
        return AuthStatus(
            configured=configured,
            provider_id=self.provider_id,
            needs_login=not configured,
            detail="API key configured" if configured else "API key missing",
        )

    async def login(self, *, open_browser: bool = True) -> AuthStatus:
        del open_browser
        return self.status()

    async def logout(self) -> None:
        self.api_key = ""
