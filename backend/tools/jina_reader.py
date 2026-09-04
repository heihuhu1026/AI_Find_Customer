"""Jina Reader tool backed directly by the public r.jina.ai endpoint."""

from __future__ import annotations

import logging
from typing import Optional

import httpx
from tenacity import before_sleep_log, retry, retry_if_exception, stop_after_attempt, wait_exponential

from config.settings import Settings, get_settings
from tools.blacklist_store import load_blocked_domains
from tools.crawl_registry import CrawlRegistry, normalize_domain
from tools.url_filter import classify_url

logger = logging.getLogger(__name__)


def _is_retryable_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    if isinstance(exc, (httpx.RequestError, httpx.TimeoutException)):
        return True
    return False


class JinaReaderTool:
    """Fetch clean Markdown from r.jina.ai for a target URL."""

    JINA_BASE = "https://r.jina.ai/"

    def __init__(self, settings: Settings | None = None, *, hunt_id: str = "", crawl_registry=None, own_domains: set[str] | None = None) -> None:
        self._settings = settings or get_settings()
        self._hunt_id = hunt_id or ""
        self._crawl_registry = crawl_registry
        self._owns_registry = crawl_registry is None
        # Domains that belong to the user's OWN company homepage. These are
        # exempt from cross-hunt de-duplication: always re-fetchable and never
        # recorded, so re-running analysis on the same company site is never
        # blocked or skipped.
        self._own_domains = {d for d in (own_domains or set()) if d}
        self._blocked_domains: set[str] | None = None  # lazy-loaded once
        self._client: Optional[httpx.AsyncClient] = None

    def _registry(self) -> CrawlRegistry | None:
        """Lazily build (or return injected) the cross-hunt crawl registry."""
        if self._crawl_registry is not None:
            return self._crawl_registry
        if not getattr(self._settings, "crawl_dedup_enabled", False):
            return None
        try:
            reg = CrawlRegistry(
                self._settings.crawl_registry_db_path,
                ttl_seconds=self._settings.crawl_dedup_ttl_seconds,
            )
            reg.init_db()
            self._crawl_registry = reg
            return reg
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[JinaReader] crawl registry unavailable: %s", exc)
            return None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    @retry(
        retry=retry_if_exception(_is_retryable_error),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def read(self, url: str) -> str:
        """Read a URL through the Jina Reader service.

        Cross-hunt de-duplication: if the URL (or, for company sites, its
        domain) was already crawled by a *different* hunt within the freshness
        window, the network fetch is skipped to avoid duplicate crawling.
        Successful fetches are recorded so other hunts can skip them later.

        The owning company's OWN homepage (``own_domains``) is exempt: it is
        always re-fetchable and is never written into the registry, so
        re-running analysis on the same company site is never blocked.
        """
        if not url:
            return ""

        domain = normalize_domain(url)
        own = bool(domain and domain in self._own_domains)

        # ── De-dup guard: skip URLs/domains already crawled by other hunts ──
        # (Own company homepage is exempt.)
        if not own and getattr(self._settings, "crawl_dedup_enabled", False):
            reg = self._registry()
            if reg is not None:
                try:
                    if classify_url(url) == "company_site":
                        owner = reg.domain_crawled_by(domain, exclude_hunt_id=self._hunt_id)
                        if owner:
                            logger.info(
                                "[JinaReader] skip already-crawled domain %s (owner hunt %s)",
                                domain, owner,
                            )
                            return ""
                    elif reg.is_url_crawled(url, exclude_hunt_id=self._hunt_id):
                        logger.info("[JinaReader] skip already-crawled url %s", url)
                        return ""
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("[JinaReader] dedup check skipped: %s", exc)

        # ── Layer 2 blacklist filter: never fetch blacklisted domains ──────
        # (Own company homepage is exempt, same as the de-dup exemption.)
        if not own:
            if self._blocked_domains is None:
                try:
                    self._blocked_domains = load_blocked_domains(self._settings)
                except Exception:  # pragma: no cover - defensive
                    self._blocked_domains = set()
            if domain and domain in self._blocked_domains:
                logger.info("[JinaReader] blacklist skip %s", domain)
                return ""

        headers = {"Accept": "text/markdown"}
        if self._settings.jina_api_key:
            headers["Authorization"] = f"Bearer {self._settings.jina_api_key}"

        client = await self._get_client()
        resp = await client.get(f"{self.JINA_BASE}{url}", headers=headers)
        resp.raise_for_status()
        content = resp.text

        # ── Record successful crawl for future cross-hunt de-dup ───────────
        # (Own company homepage is never recorded.)
        if content and not own:
            reg = self._registry()
            if reg is not None:
                try:
                    reg.record(url, self._hunt_id, status="ok")
                except Exception:  # pragma: no cover - defensive
                    pass
        return content

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._owns_registry and self._crawl_registry is not None:
            self._crawl_registry.close()
            self._crawl_registry = None
