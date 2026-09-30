"""Authentication strategies applied to connector requests.

A strategy mutates a *request dict* - ``{"url", "headers", "params"}`` - which
is what every HTTP-shaped connector builds before sending. Keeping the shape
plain (instead of an httpx object) lets the WebSocket connector reuse the same
strategies for its handshake headers.
"""

from __future__ import annotations

import abc
import base64
import logging
import time
from typing import Any

import httpx

from tradinglib.data.ingest.source import AuthConfig, AuthType

logger = logging.getLogger(__name__)

#: Refresh an OAuth token this many seconds before it actually expires.
TOKEN_REFRESH_MARGIN: float = 30.0


class AuthError(RuntimeError):
    """Raised when credentials are missing or a token exchange fails."""


class AuthStrategy(abc.ABC):
    """Applies credentials to an outgoing request."""

    #: Strategy identifier, matching :class:`~tradinglib.data.ingest.source.AuthType`.
    type: str = "none"

    @abc.abstractmethod
    async def apply(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return ``request`` (headers/params/url) with credentials applied."""
        raise NotImplementedError

    async def refresh(self) -> None:
        """Refresh short-lived credentials. Default: nothing to do."""
        return None

    async def close(self) -> None:
        """Release any client the strategy owns. Default: nothing to do."""
        return None

    def describe(self) -> dict[str, Any]:
        """Return a masked description for API responses."""
        return {"type": self.type}

    @staticmethod
    def _headers(request: dict[str, Any]) -> dict[str, str]:
        """Return (creating if needed) the request's header mapping."""
        headers = request.get("headers")
        if not isinstance(headers, dict):
            headers = {}
            request["headers"] = headers
        return headers

    @staticmethod
    def _params(request: dict[str, Any]) -> dict[str, Any]:
        """Return (creating if needed) the request's query-parameter mapping."""
        params = request.get("params")
        if not isinstance(params, dict):
            params = {}
            request["params"] = params
        return params


class NoAuth(AuthStrategy):
    """Pass-through strategy for keyless sources."""

    type = "none"

    async def apply(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return the request unchanged."""
        return request

    def describe(self) -> dict[str, Any]:
        """Describe this strategy."""
        return {"type": "none"}


class ApiKeyAuth(AuthStrategy):
    """API-key auth, injected as a header or a query parameter."""

    type = "api_key"

    def __init__(self, key_name: str, key_value: str, *, in_header: bool = True) -> None:
        """Create the strategy."""
        if not key_name:
            raise AuthError("API-key authentication needs a key name")
        if not key_value:
            raise AuthError(f"API-key authentication needs a value for {key_name!r}")
        self.key_name = key_name
        self.key_value = key_value
        self.in_header = in_header

    async def apply(self, request: dict[str, Any]) -> dict[str, Any]:
        """Inject the API key into headers or query parameters."""
        if self.in_header:
            self._headers(request)[self.key_name] = self.key_value
        else:
            self._params(request)[self.key_name] = self.key_value
        return request

    def describe(self) -> dict[str, Any]:
        """Describe this strategy with the key masked."""
        from tradinglib.config import mask_secret

        return {
            "type": self.type,
            "key_name": self.key_name,
            "key_value": mask_secret(self.key_value),
            "in_header": self.in_header,
        }


class BasicAuth(AuthStrategy):
    """HTTP basic authentication."""

    type = "basic"

    def __init__(self, username: str, password: str) -> None:
        """Create the strategy."""
        if not username:
            raise AuthError("basic authentication needs a username")
        self.username = username
        self.password = password or ""

    @property
    def header_value(self) -> str:
        """The ``Authorization`` header value this strategy sends."""
        raw = f"{self.username}:{self.password}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")

    async def apply(self, request: dict[str, Any]) -> dict[str, Any]:
        """Add the ``Authorization: Basic ...`` header."""
        self._headers(request)["Authorization"] = self.header_value
        return request

    def describe(self) -> dict[str, Any]:
        """Describe this strategy with the password masked."""
        from tradinglib.config import mask_secret

        return {
            "type": self.type,
            "username": self.username,
            "password": mask_secret(self.password),
        }


class ConnectionSecretAuth(AuthStrategy):
    """A single opaque secret (database password, connection string fragment).

    HTTP requests are left untouched: the secret belongs in the connection URI,
    where :class:`~tradinglib.data.ingest.connectors.database.DatabaseConnector`
    substitutes it for the ``{secret}`` placeholder.
    """

    type = "secret"

    def __init__(self, secret: str) -> None:
        """Create the strategy."""
        if not secret:
            raise AuthError("connection-secret authentication needs a secret")
        self.secret = secret

    async def apply(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return the request unchanged - the secret is not an HTTP credential."""
        return request

    def describe(self) -> dict[str, Any]:
        """Describe this strategy with the secret masked."""
        from tradinglib.config import mask_secret

        return {"type": self.type, "secret": mask_secret(self.secret)}


class OAuthAuth(AuthStrategy):
    """OAuth2 client-credentials flow with automatic token refresh."""

    type = "oauth"

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        *,
        scopes: list[str] | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Create the strategy.

        Args:
            token_url: the token endpoint.
            client_id: OAuth client id.
            client_secret: OAuth client secret.
            scopes: requested scopes, sent space-separated.
            timeout: token-request timeout in seconds.
            transport: injected httpx transport (tests use ``MockTransport``).
        """
        if not token_url:
            raise AuthError("OAuth authentication needs a token URL")
        if not client_id or not client_secret:
            raise AuthError("OAuth authentication needs a client id and secret")
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = list(scopes or [])
        self.timeout = timeout
        self._transport = transport
        self._token: str | None = None
        self._expires_at: float = 0.0

    @property
    def token_valid(self) -> bool:
        """Whether a cached token exists and is not about to expire."""
        return self._token is not None and time.monotonic() < self._expires_at

    async def apply(self, request: dict[str, Any]) -> dict[str, Any]:
        """Add a bearer token, fetching or refreshing it when needed."""
        if not self.token_valid:
            await self.refresh()
        self._headers(request)["Authorization"] = f"Bearer {self._token}"
        return request

    async def refresh(self) -> None:
        """Fetch a new access token from ``token_url``.

        Raises:
            AuthError: when the endpoint rejects the credentials or returns a
                body without an ``access_token``.
        """
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if self.scopes:
            data["scope"] = " ".join(self.scopes)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout), transport=self._transport
        ) as client:
            try:
                response = await client.post(self.token_url, data=data)
            except httpx.HTTPError as exc:
                raise AuthError(f"token request to {self.token_url} failed: {exc}") from exc
        if response.status_code >= 400:
            raise AuthError(
                f"token endpoint returned HTTP {response.status_code}: "
                f"{response.text.strip()[:200]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AuthError("token endpoint returned a non-JSON body") from exc
        token = payload.get("access_token")
        if not token:
            raise AuthError("token endpoint returned no access_token")
        self._token = str(token)
        try:
            lifetime = float(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            lifetime = 3600.0
        self._expires_at = time.monotonic() + max(lifetime - TOKEN_REFRESH_MARGIN, 1.0)

    def describe(self) -> dict[str, Any]:
        """Describe this strategy with the client secret masked."""
        from tradinglib.config import mask_secret

        return {
            "type": self.type,
            "token_url": self.token_url,
            "client_id": self.client_id,
            "client_secret": mask_secret(self.client_secret),
            "scopes": list(self.scopes),
            "token_cached": self.token_valid,
        }


def build_auth(config: AuthConfig, *, transport: httpx.BaseTransport | None = None) -> AuthStrategy:
    """Construct the strategy named by ``config.type``.

    Args:
        config: the source's credential configuration.
        transport: injected httpx transport, used by the OAuth token request in
            tests.

    Raises:
        AuthError: when the config is incomplete for the chosen type. The
            caller turns that into an honest "not configured" status instead of
            attempting an unauthenticated request.
    """
    if config.type is AuthType.NONE:
        return NoAuth()
    if config.type is AuthType.API_KEY:
        return ApiKeyAuth(
            config.key_name or "X-API-Key",
            config.key_value or "",
            in_header=config.in_header,
        )
    if config.type is AuthType.BASIC:
        return BasicAuth(config.username or "", config.password or "")
    if config.type is AuthType.SECRET:
        return ConnectionSecretAuth(config.key_value or "")
    if config.type is AuthType.OAUTH:
        return OAuthAuth(
            config.token_url or "",
            config.client_id or "",
            config.client_secret or "",
            scopes=config.scopes,
            transport=transport,
        )
    raise AuthError(f"unsupported authentication type: {config.type}")  # pragma: no cover


__all__ = [
    "ApiKeyAuth",
    "AuthError",
    "AuthStrategy",
    "BasicAuth",
    "ConnectionSecretAuth",
    "NoAuth",
    "OAuthAuth",
    "build_auth",
]
