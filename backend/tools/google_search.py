"""Google Search tool backed directly by the Serper API."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from config.settings import Settings, get_settings
from tools.http_client_mixin import AsyncHTTPClientMixin
from tools.search_retry import retry_search

logger = logging.getLogger(__name__)


class GoogleSearchTool(AsyncHTTPClientMixin):
    """Search Google through Serper and return normalized organic results."""

    SERPER_URL = "https://google.serper.dev/search"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._http_timeout = 30.0
        self._client: Optional[httpx.AsyncClient] = None

    @retry_search
    async def search(
        self,
        query: str,
        *,
        num: int = 10,
        gl: str = "",
        hl: str = "",
    ) -> list[dict]:
        """Execute a Google search via Serper."""
        if not self._settings.serper_api_key:
            raise RuntimeError("SERPER_API_KEY is required for Google search.")

        body: dict[str, object] = {"q": query, "num": num}
        if gl:
            body["gl"] = gl
        if hl:
            body["hl"] = hl

        client = await self._get_client()
        resp = await client.post(
            self.SERPER_URL,
            headers={
                "X-API-KEY": self._settings.serper_api_key,
                "Content-Type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("organic", []):
            results.append(
                {
                    "title": item.get("title", ""),
                    "link": item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                    "position": item.get("position", 0),
                }
            )
        return results

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
