"""OpenAI Codex OAuth credentials with secure, North-owned persistence.

This module intentionally does not read ``~/.codex/auth.json``.  North owns
its credential lifecycle and can change transports without depending on the
private storage format of another CLI.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx

from inference.auth import AuthContext, AuthStatus
from inference.exceptions import ProviderAuthError

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
CODEX_SCOPE = "openid profile email offline_access"
_REFRESH_MARGIN_SECONDS = 60


# Where every authenticated provider's credentials live, relative to north's home.
# Named here rather than spelled inline so `north reset` can preserve the whole
# directory without having to know which providers are in it.
CREDENTIALS_DIR_NAME = "credentials"


def default_codex_token_path() -> Path:
    north_home = Path(os.environ.get("NORTH_HOME", "~/.north")).expanduser()
    return north_home / CREDENTIALS_DIR_NAME / "openai_codex.json"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _jwt_claim(token: str, namespace: str, key: str) -> str | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        payload = json.loads(raw)
        nested = payload.get(namespace, {})
        value = nested.get(key) if isinstance(nested, dict) else None
        return str(value) if value else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


@dataclass(frozen=True)
class CodexToken:
    access_token: str
    refresh_token: str
    expires_at: float
    account_id: str | None = None

    @property
    def is_fresh(self) -> bool:
        return self.expires_at > time.time() + _REFRESH_MARGIN_SECONDS


class CodexTokenStore:
    """Atomic token storage with restrictive directory and file permissions."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_codex_token_path()

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> CodexToken | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return CodexToken(
                access_token=str(data["access_token"]),
                refresh_token=str(data["refresh_token"]),
                expires_at=float(data["expires_at"]),
                account_id=str(data["account_id"]) if data.get("account_id") else None,
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ProviderAuthError(f"OpenAI Codex credential store is invalid: {exc}") from exc

    def save(self, token: CodexToken) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        payload = {
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expires_at": token.expires_at,
            "account_id": token.account_id,
        }
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temporary_path = Path(temporary)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


class _CallbackServer(HTTPServer):
    expected_state: str
    future: asyncio.Future[str]
    loop: asyncio.AbstractEventLoop


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer  # type: ignore[assignment]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        code = query.get("code", [None])[0]
        state = query.get("state", [None])[0]
        error = query.get("error_description", query.get("error", [None]))[0]
        valid = parsed.path == "/auth/callback" and state == self.server.expected_state and bool(code)
        body = (
            b"Authentication complete. You can close this tab and return to North."
            if valid
            else f"Authentication failed: {error or 'invalid callback'}".encode()
        )
        self.send_response(200 if valid else 400)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if valid and not self.server.future.done():
            self.server.loop.call_soon_threadsafe(self.server.future.set_result, str(code))

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


class CodexCredentialProvider:
    """PKCE login and automatic access-token refresh for OpenAI Codex."""

    provider_id = "openai_codex"

    def __init__(
        self,
        store: CodexTokenStore | None = None,
        *,
        authorization_callback: Callable[[str], None] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.store = store or CodexTokenStore()
        self.authorization_callback = authorization_callback
        self._client = client
        self._refresh_lock = asyncio.Lock()

    def status(self) -> AuthStatus:
        try:
            token = self.store.load()
        except ProviderAuthError as exc:
            return AuthStatus(False, self.provider_id, needs_login=True, detail=str(exc))
        if token is None:
            return AuthStatus(False, self.provider_id, needs_login=True, detail="Not logged in")
        expires = datetime.fromtimestamp(token.expires_at, tz=UTC)
        return AuthStatus(
            True,
            self.provider_id,
            account_id=token.account_id,
            expires_at=expires,
            needs_login=not bool(token.refresh_token),
            detail="Logged in",
        )

    async def get_auth(self) -> AuthContext:
        token = self.store.load()
        if token is None:
            raise ProviderAuthError("OpenAI Codex is not logged in. Run `north auth login openai-codex`.")
        if not token.is_fresh:
            async with self._refresh_lock:
                token = self.store.load() or token
                if not token.is_fresh:
                    token = await self._refresh(token)
                    self.store.save(token)
        headers = {
            "Authorization": f"Bearer {token.access_token}",
            "originator": "north",
        }
        if token.account_id:
            headers["chatgpt-account-id"] = token.account_id
        return AuthContext(
            headers=headers,
            account_id=token.account_id,
            expires_at=datetime.fromtimestamp(token.expires_at, tz=UTC),
            source="oauth_pkce",
        )

    async def login(self, *, open_browser: bool = True) -> AuthStatus:
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        state = _b64url(secrets.token_bytes(24))
        params = {
            "response_type": "code",
            "client_id": CODEX_CLIENT_ID,
            "redirect_uri": CODEX_REDIRECT_URI,
            "scope": CODEX_SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        authorization_url = f"{CODEX_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        try:
            server = _CallbackServer(("127.0.0.1", 1455), _CallbackHandler)
        except OSError as exc:
            raise ProviderAuthError("Cannot start OAuth callback on 127.0.0.1:1455; is the port in use?") from exc
        server.expected_state = state
        server.future = future
        server.loop = loop
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        if self.authorization_callback:
            self.authorization_callback(authorization_url)
        if open_browser:
            await asyncio.to_thread(webbrowser.open, authorization_url)
        try:
            code = await asyncio.wait_for(future, timeout=180)
        except TimeoutError as exc:
            raise ProviderAuthError("OpenAI Codex login timed out after 3 minutes") from exc
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        token = await self._exchange_code(code, verifier)
        self.store.save(token)
        return self.status()

    async def logout(self) -> None:
        self.store.delete()

    async def _request_token(self, data: dict[str, str]) -> CodexToken:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=30)
        try:
            response = await client.post(CODEX_TOKEN_URL, data=data)
        except httpx.RequestError as exc:
            raise ProviderAuthError(f"OpenAI Codex token request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code != 200:
            raise ProviderAuthError(
                f"OpenAI Codex token request returned {response.status_code}: {response.text[:200]}"
            )
        try:
            payload = response.json()
            access = str(payload["access_token"])
            refresh = str(payload.get("refresh_token") or data.get("refresh_token") or "")
            expires_in = max(1, int(payload.get("expires_in", 3600)))
        except (ValueError, TypeError, KeyError) as exc:
            raise ProviderAuthError("OpenAI Codex returned an invalid token response") from exc
        account_id = _jwt_claim(access, "https://api.openai.com/auth", "chatgpt_account_id")
        return CodexToken(access, refresh, time.time() + expires_in, account_id)

    async def _exchange_code(self, code: str, verifier: str) -> CodexToken:
        return await self._request_token({
            "grant_type": "authorization_code",
            "client_id": CODEX_CLIENT_ID,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": CODEX_REDIRECT_URI,
        })

    async def _refresh(self, token: CodexToken) -> CodexToken:
        if not token.refresh_token:
            raise ProviderAuthError("OpenAI Codex refresh token is missing; log in again")
        return await self._request_token({
            "grant_type": "refresh_token",
            "client_id": CODEX_CLIENT_ID,
            "refresh_token": token.refresh_token,
        })
