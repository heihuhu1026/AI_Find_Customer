"""Shared lazy ``httpx.AsyncClient`` mixin to remove per-tool boilerplate.

Brave / Google / Google Maps / Jina tools each duplicated the identical
``_get_client()`` lazy-init pattern.  Tavily is intentionally excluded — its
``_get_client`` returns a ``TavilyClient`` SDK object, not an ``httpx`` client.

Subclasses must set ``self._client = None`` (and may override
``self._http_timeout`` in seconds, default 30.0) inside ``__init__``.
"""

from __future__ import annotations

import httpx


class AsyncHTTPClientMixin:
    """Provides a lazily-created, process-lifetime httpx.AsyncClient."""

    _http_timeout: float = 30.0

    async def _get_client(self) -> httpx.AsyncClient:
        client = getattr(self, "_client", None)
        if client is None:
            client = httpx.AsyncClient(timeout=self._http_timeout)
            self._client = client
        return client

    async def aclose_http_client(self) -> None:
        """Close the shared client if it was created (best-effort)."""
        client = getattr(self, "_client", None)
        if client is not None:
            await client.aclose()
            self._client = None
