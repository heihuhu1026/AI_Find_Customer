"""Shared API access control helpers."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Query, Request, status

from config.settings import get_settings

_LOCAL_HOSTS = {"", "127.0.0.1", "::1", "localhost", "testclient", "test"}


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def _client_host(request: Request, trust_proxy_headers: bool) -> str:
    """Resolve the caller's host.

    ``request.client.host`` is the TCP peer. Behind any reverse proxy that value
    is the proxy itself, so trusting it by default lets a remote caller satisfy
    the localhost bypass. X-Forwarded-For is therefore consulted **only** when
    ``trust_proxy_headers`` is explicitly enabled — and even then only the
    left-most entry (the original client) is used.
    """
    if trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        for candidate in forwarded.split(","):
            candidate = candidate.strip().lower()
            if candidate:
                return candidate
    return (request.client.host if request.client else "").strip().lower()


def require_api_access(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    api_key: str | None = Query(default=None),
) -> None:
    """Allow localhost access by default; require token for non-local requests when configured."""
    settings = get_settings()
    expected = settings.api_access_token.strip()
    client_host = _client_host(request, bool(getattr(settings, "trust_proxy_headers", False)))

    if not expected:
        if client_host in _LOCAL_HOSTS:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API access is restricted to localhost unless API_ACCESS_TOKEN is configured.",
        )

    provided = x_api_key or api_key or _extract_bearer_token(authorization)
    # Constant-time comparison: `==` short-circuits on the first differing byte,
    # which leaks the length of the matching prefix through response timing.
    if provided and hmac.compare_digest(provided, expected):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API access token.",
    )
