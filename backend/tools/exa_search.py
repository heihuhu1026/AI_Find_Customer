"""Exa Search tool — semantic web search via the Exa AI API.

API docs: https://exa.ai/docs
Endpoint:  POST https://api.exa.ai/search  (header: x-api-key)
Billing:   usage-based (neural search ≈ $0.007/query at probe time).
Errors:    HTTP 402 (no credit) / 429 (rate limit or quota) surface as
           httpx.HTTPStatusError and are classified upstream as "quota"/"rate".
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class ExaSearchTool:
    """Search the web via Exa and return normalized organic-style results.

    Each result contains: title, link, snippet, position.
    """

    EXA_URL = "https://api.exa.ai/search"

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
        """Execute a semantic web search via Exa.

        Args:
            query: Search query string.
            num: Number of results (Exa caps at 10 per request).
            gl: Country hint — Exa has no direct gl; optionally appended to the
                query as a location qualifier when a valid 2-letter code is given.
            hl: Ignored (no language param on Exa search).

        Returns:
            List of result dicts with keys: title, link, snippet, position.
        """
        if not self._settings.exa_api_key:
            raise RuntimeError("EXA_API_KEY is required for Exa search.")

        q = query
        if gl and len(gl) == 2:
            q = f"{query} in {gl.upper()}"

        body = {
            "query": q,
            "numResults": min(int(num), 10),
            "type": "auto",
        }

        client = await self._get_client()
        resp = await client.post(
            self.EXA_URL,
            headers={
                "x-api-key": self._settings.exa_api_key,
                "Content-Type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for i, item in enumerate(data.get("results", []), start=1):
            results.append({
                "title": item.get("title", ""),
                "link": item.get("url", item.get("id", "")),
                "snippet": item.get("text", "")[:400],
                "position": i,
            })
        return results

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
