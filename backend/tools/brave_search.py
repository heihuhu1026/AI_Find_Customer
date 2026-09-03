"""Brave Search tool — web search via Brave Search API."""

from __future__ import annotations

from typing import Optional

import httpx

from tools.http_client_mixin import AsyncHTTPClientMixin
from tools.search_retry import retry_search


class BraveSearchTool(AsyncHTTPClientMixin):
    """Search the web via Brave Search API and return structured results.

    Each result contains: title, link, snippet, position.
    Free tier: 2,000 queries/month.
    """

    BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str = "", settings=None) -> None:
        if api_key:
            self._api_key = api_key
        elif settings is not None and hasattr(settings, "brave_api_key"):
            self._api_key = settings.brave_api_key
        else:
            self._api_key = ""
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
        """Execute a web search via Brave Search API.

        Args:
            query: Search query string.
            num: Number of results to return (max 20).
            gl: Country code for geolocation (e.g. "us", "de").
            hl: Language code (e.g. "en", "de").

        Returns:
            List of result dicts with keys: title, link, snippet, position.
        """
        client = await self._get_client()

        params: dict = {"q": query, "count": min(num, 20)}
        if gl:
            params["country"] = gl
        if hl:
            params["search_lang"] = hl

        resp = await client.get(
            self.BRAVE_URL,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": self._api_key,
            },
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for i, item in enumerate(data.get("web", {}).get("results", []), start=1):
            results.append({
                "title": item.get("title", ""),
                "link": item.get("url", ""),
                "snippet": item.get("description", ""),
                "position": i,
            })
        return results

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
