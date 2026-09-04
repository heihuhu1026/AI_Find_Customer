"""SerpApi Search tool — Google organic search via serpapi.com.

API docs: https://serpapi.com/search-api
Endpoint:  GET https://serpapi.com/search.json?engine=google&q=...&api_key=...
Free tier: 100 searches/month. Quota exhaustion usually comes back as an
"error" JSON field or HTTP 429 — both are handled here so the caller can
classify it as "quota" and auto-switch to the next search backend.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class SerpApiSearchTool:
    """Search Google through SerpApi and return normalized organic results.

    Each result contains: title, link, snippet, position.
    """

    SERPAPI_URL = "https://serpapi.com/search.json"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def search(
        self,
        query: str,
        *,
        num: int = 10,
        gl: str = "",
        hl: str = "",
    ) -> list[dict]:
        """Execute a Google search via SerpApi."""
        if not self._settings.serpapi_api_key:
            raise RuntimeError("SERPAPI_API_KEY is required for SerpApi search.")

        params: dict[str, object] = {
            "engine": "google",
            "q": query,
            "api_key": self._settings.serpapi_api_key,
            "num": min(int(num), 20),
        }
        if gl:
            params["gl"] = gl
        if hl:
            params["hl"] = hl

        client = await self._get_client()
        resp = await client.get(self.SERPAPI_URL, params=params)
        # SerpApi returns 200 with an "error" field on quota / account issues.
        if resp.status_code == 200:
            payload = resp.json()
            if payload.get("error"):
                raise RuntimeError(f"SerpApi error: {payload['error']}")
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("organic_results", []):
            results.append({
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "snippet": item.get("snippet", ""),
                "position": item.get("position", 0),
            })
        return results

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
