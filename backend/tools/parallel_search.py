"""Parallel AI Search tool — LLM-optimized web search via Parallel Search API.

API docs: https://docs.parallel.ai
Endpoint:  POST https://api.parallel.ai/v1/search  (header: x-api-key)
Notes:
  * Takes a natural-language "objective" plus one or more "search_queries"
    (3–6 words each) instead of a plain q + gl/hl pair.
  * Free/plan billing requires credit; HTTP 402 (Insufficient credit) is
    classified upstream as "quota" so the failover chain auto-skips it.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class ParallelSearchTool:
    """Search the web via Parallel and return normalized organic-style results.

    Each result contains: title, link, snippet, position.
    """

    PARALLEL_URL = "https://api.parallel.ai/v1/search"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=45.0)
        return self._client

    async def search(
        self,
        query: str,
        *,
        num: int = 10,
        gl: str = "",
        hl: str = "",
    ) -> list[dict]:
        """Execute a Parallel search for the given query.

        Args:
            query: Search query / objective text.
            num: Maximum number of results to return.
            gl: Ignored (no geo param; fold region words into query if needed).
            hl: Ignored (no language param).

        Returns:
            List of result dicts with keys: title, link, snippet, position.
        """
        if not self._settings.parallel_api_key:
            raise RuntimeError("PARALLEL_API_KEY is required for Parallel search.")

        region_hint = f" in {gl.upper()}" if gl and len(gl) == 2 else ""
        body = {
            # Objective helps Parallel understand intent; queries must be 3-6 words.
            "objective": f"Find {query}{region_hint}",
            "search_queries": [query],
            "mode": "fast",
            # Keep response small / cheap; cap by estimated chars per result.
            "max_chars_total": min(max(int(num) * 500, 1000), 8000),
        }

        client = await self._get_client()
        resp = await client.post(
            self.PARALLEL_URL,
            headers={
                "x-api-key": self._settings.parallel_api_key,
                "Content-Type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for i, item in enumerate(data.get("results", []), start=1):
            if i > int(num):
                break
            excerpts = item.get("excerpts") or []
            results.append({
                "title": item.get("title", ""),
                "link": item.get("url", ""),
                "snippet": (excerpts[0] if excerpts else "")[:400],
                "position": i,
            })
        return results

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
