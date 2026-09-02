"""Web search tool.

Backend selection (minimal & free-first):
    * If BRAVE_API_KEY is configured  -> use Brave Search API (free tier).
    * Else if SERPER_API_KEY is set   -> fall back to Serper (Google).
    * Otherwise raise, telling the user which key to set.

The agent-facing tool name stays `google_search`; only the underlying
backend changes, so no agent prompt or wiring needs to be edited.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx
from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


# --- Circuit breaker for search backends ---------------------------------
# Without this, a dead or rate-limited backend is retried (with tenacity
# backoff) on *every* single query — roughly +45s per search when the first
# backend in the preference list is unreachable. After `_BREAKER_THRESHOLD`
# consecutive failures the backend is skipped for `_BREAKER_COOLDOWN_SECONDS`;
# any success resets it immediately. State is module-level so it is shared
# across tool instances (a new tool is built per search).
_BREAKER_THRESHOLD = 2
_BREAKER_COOLDOWN_SECONDS = 300
_backend_failures: dict[str, int] = {}
_backend_blocked_until: dict[str, float] = {}


def _backend_available(backend: str) -> bool:
    """Return False while the backend is in circuit-breaker cooldown."""
    until = _backend_blocked_until.get(backend, 0.0)
    if not until:
        return True
    if time.monotonic() >= until:
        # Cooldown expired — clear state and allow a fresh probe attempt.
        _backend_blocked_until.pop(backend, None)
        _backend_failures.pop(backend, None)
        return True
    return False


def _record_backend_failure(backend: str) -> None:
    count = _backend_failures.get(backend, 0) + 1
    _backend_failures[backend] = count
    if count >= _BREAKER_THRESHOLD:
        _backend_blocked_until[backend] = time.monotonic() + _BREAKER_COOLDOWN_SECONDS
        logger.warning(
            "[WebSearch] backend '%s' failed %d times in a row; skipping it for %ds",
            backend,
            count,
            _BREAKER_COOLDOWN_SECONDS,
        )


def _record_backend_success(backend: str) -> None:
    if backend in _backend_failures or backend in _backend_blocked_until:
        _backend_failures.pop(backend, None)
        _backend_blocked_until.pop(backend, None)


def _is_retryable_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    if isinstance(exc, (httpx.RequestError, httpx.TimeoutException)):
        return True
    return False


class GoogleSearchTool:
    """General web search used during lead extraction / insight.

    Backend selection (minimal & free-first), tried in this order and
    auto-falling back to the next available one on failure:
        * Brave Search API (free tier)  — when BRAVE_API_KEY is set
        * Tavily Search API             — when TAVILY_API_KEY is set
        * Serper (Google)               — when SERPER_API_KEY is set

    The agent-facing tool name stays `google_search`; only the underlying
    backend changes, so no agent prompt or wiring needs to be edited.
    """

    SERPER_URL = "https://google.serper.dev/search"
    BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: Optional[httpx.AsyncClient] = None
        # Ordered preference list is taken from `search_backend_order`
        # (default: brave,tavily,serper). Only backends with a configured key
        # are kept, preserving the configured order. Putting a reachable
        # provider first (e.g. tavily) skips retries against blocked ones.
        order = [b.strip().lower() for b in self._settings.search_backend_order.split(",") if b.strip()]
        key_map = {
            "brave": self._settings.brave_api_key,
            "tavily": self._settings.tavily_api_key,
            "serper": self._settings.serper_api_key,
        }
        self._backends = [b for b in order if b in key_map and key_map[b]]
        # Never skip a configured backend entirely; fall back to the legacy
        # brave->tavily->serper order if the configured order lists nothing.
        if not self._backends:
            for b in ("brave", "tavily", "serper"):
                if key_map.get(b):
                    self._backends.append(b)
        if not self._backends:
            raise RuntimeError(
                "A web search API key is required: set BRAVE_API_KEY (free), "
                "TAVILY_API_KEY, or SERPER_API_KEY."
            )
        logger.info("[WebSearch] Backends (ordered): %s", ", ".join(self._backends))

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.search_backend_timeout)
        return self._client

    async def _retry_call(self, coro_fn) -> list[dict]:
        """Retry a single backend call using configured attempt budget.

        Keeps retries short so an unreachable backend fails fast (the circuit
        breaker then skips it for the rest of the run).
        """
        attempts = max(1, int(self._settings.search_backend_retry_attempts))
        async for attempt in AsyncRetrying(
            retry=retry_if_exception(_is_retryable_error),
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=1, min=1, max=4),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        ):
            with attempt:
                return await coro_fn()
        return []  # unreachable, reraise=True ensures exception propagates

    async def search(
        self,
        query: str,
        *,
        num: int = 10,
        gl: str = "",
        hl: str = "",
    ) -> list[dict]:
        """Execute a web search, trying each configured backend in order.

        Falls back to the next backend automatically when one fails, so the
        search layer is resilient to a single provider being unavailable.
        """
        last_err: BaseException | None = None
        attempted = False

        for backend in self._backends:
            if not _backend_available(backend):
                logger.info(
                    "[WebSearch] skipping backend '%s' (circuit breaker cooldown)", backend
                )
                continue
            attempted = True
            try:
                if backend == "brave":
                    results = await self._retry_call(lambda: self._brave_search(query, num=num, gl=gl, hl=hl))
                elif backend == "tavily":
                    results = await self._retry_call(lambda: self._tavily_search(query, num=num, gl=gl, hl=hl))
                else:
                    results = await self._retry_call(lambda: self._serper_search(query, num=num, gl=gl, hl=hl))
                _record_backend_success(backend)
                return results
            except Exception as exc:  # noqa: BLE001 — try next backend
                logger.warning(
                    "[WebSearch] backend '%s' failed (%s); trying next", backend, exc
                )
                last_err = exc
                _record_backend_failure(backend)

        if not attempted:
            # Every backend is in cooldown. Never fail purely because of the
            # breaker — make one forced pass over all of them.
            logger.warning("[WebSearch] all backends in cooldown; forcing a retry pass")
            for backend in self._backends:
                try:
                    if backend == "brave":
                        results = await self._retry_call(lambda: self._brave_search(query, num=num, gl=gl, hl=hl))
                    elif backend == "tavily":
                        results = await self._retry_call(lambda: self._tavily_search(query, num=num, gl=gl, hl=hl))
                    else:
                        results = await self._retry_call(lambda: self._serper_search(query, num=num, gl=gl, hl=hl))
                    _record_backend_success(backend)
                    return results
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    _record_backend_failure(backend)

        raise RuntimeError(f"All web search backends failed: {last_err}")

    async def _serper_search(
        self,
        query: str,
        *,
        num: int = 10,
        gl: str = "",
        hl: str = "",
    ) -> list[dict]:
        """Execute a Google search via Serper."""
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

    async def _brave_search(
        self,
        query: str,
        *,
        num: int = 10,
        gl: str = "",
        hl: str = "",
    ) -> list[dict]:
        """Execute a web search via Brave Search API."""
        params: dict[str, object] = {"q": query, "count": min(num, 20)}
        if gl:
            params["country"] = gl
        if hl:
            params["search_lang"] = hl

        client = await self._get_client()
        resp = await client.get(
            self.BRAVE_URL,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": self._settings.brave_api_key,
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

    async def _tavily_search(
        self,
        query: str,
        *,
        num: int = 10,
        gl: str = "",
        hl: str = "",
    ) -> list[dict]:
        """Execute a web search via the Tavily Search API (official SDK).

        Tavily's ``country`` param expects a full country *name*, not a
        2-letter ISO code, so we deliberately omit the geo hint here (geo
        targeting is best-effort). The SDK is synchronous, so we run it in a
        worker thread. Parsing mirrors ``tools/web_search.py``.
        """
        import asyncio

        from tavily import TavilyClient

        client = TavilyClient(self._settings.tavily_api_key)
        max_results = min(num, 20)

        def _sync() -> dict:
            return client.search(
                query=query,
                search_depth="basic",
                max_results=max_results,
            )

        resp = await asyncio.to_thread(_sync)
        results = []
        for i, item in enumerate(resp.get("results", []), start=1):
            content = item.get("content", "")
            snippet = content[:400] + "..." if len(content) > 400 else content
            results.append({
                "title": item.get("title", ""),
                "link": item.get("url", ""),
                "snippet": snippet,
                "position": i,
            })
        return results

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
