"""Social / contact enrichment via an external provider (currently Apollo).

The enricher NEVER raises and returns an empty dict when it cannot produce
data (provider not configured / no API key / no resolvable domain / timeout /
HTTP error / decode error). This keeps callers (routes, portrait service)
simple and robust.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

from config.settings import get_settings

logger = logging.getLogger(__name__)

_APOLLO_PEOPLE_SEARCH_URL = "https://api.apollo.io/v1/people/search"


class SocialEnricher:
    """Fetch enriched contact/social data for a company.

    ``enrich(domain=None, linkedin_url=None)`` returns a ``SocialData``-shaped
    dict (or ``{}``). ``domain`` wins when both are provided.
    """

    def __init__(self, provider: str | None = None, api_key: str | None = None, timeout: int | None = None) -> None:
        settings = get_settings()
        self.provider = (provider or settings.social_api_provider or "").strip().lower()
        self.api_key = (api_key if api_key is not None else settings.social_api_key or "").strip()
        self.timeout = int(timeout or settings.social_api_timeout or 15)

    def enrich(self, domain: str | None = None, linkedin_url: str | None = None) -> dict[str, Any]:
        """Return enriched data as a dict, or ``{}`` when nothing was produced."""
        if not self.provider or self.provider == "none":
            return {}
        if not self.api_key:
            logger.info("[SocialEnricher] no API key configured; skipping enrichment")
            return {}
        if not domain and not linkedin_url:
            return {}
        try:
            if self.provider == "apollo":
                return self._fetch_apollo(domain=domain, linkedin_url=linkedin_url)
        except Exception:  # pragma: no cover — defensive
            # NOTE: `{}` and `%s` must not be mixed — logging only interpolates
            # `%`-style args, so the previous form raised a formatting error
            # (and, worse, hid the real failure) whenever this branch ran.
            logger.warning("[SocialEnricher] enrichment failed (degrading to {}): %s", exc_info=True)
        return {}

    # ── Apollo ──────────────────────────────────────────────────────────
    def _fetch_apollo(self, domain: str | None = None, linkedin_url: str | None = None) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "X-Api-Key": self.api_key,
        }
        payload: dict[str, Any] = {"per_page": 1}
        if domain:
            payload["q_keywords"] = domain
        elif linkedin_url:
            payload["q_keywords"] = linkedin_url
        try:
            resp = requests.post(
                _APOLLO_PEOPLE_SEARCH_URL,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            logger.warning("[SocialEnricher] Apollo request error: %s", e)
            return {}
        if resp.status_code != 200:
            logger.warning("[SocialEnricher] Apollo returned HTTP %s", resp.status_code)
            return {}
        try:
            data = resp.json()
        except ValueError:
            logger.warning("[SocialEnricher] Apollo returned invalid JSON")
            return {}
        people = data.get("people") or []
        if not people:
            return {}
        person = people[0]
        if not isinstance(person, dict):
            return {}
        return {
            "linkedin_url": person.get("linkedin_url") or "",
            "job_title": person.get("title") or "",
            "department": person.get("department") or "",
            "summary": person.get("headline") or person.get("summary") or "",
            "source": "apollo",
            "enriched_at": datetime.now(timezone.utc).isoformat(),
        }
