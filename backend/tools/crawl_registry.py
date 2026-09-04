"""Unified cross-hunt URL / domain crawl registry.

Previously, de-duplication was scattered and per-hunt only:

* ``search_agent`` kept an in-memory ``seen_urls`` set for one hunt's search
  results.
* ``lead_extract_agent`` de-duplicated leads by official-website domain,
  but only within a single hunt's in-memory state.

Neither could see what *other* hunts had already crawled, so the same
company website was frequently re-fetched by multiple tasks. This module
is the single, persistent source of truth for "has this URL / domain
already been crawled, and by which hunt?" — shared across all hunts and
processes via a small SQLite database.

Hook points:

* :func:`check_target_site_duplicate` — called by the scheduling endpoints
  *before* a task is enqueued, to refuse re-crawling a target website that
  another (or the same) hunt already crawled.
* :class:`~tools.jina_reader.JinaReaderTool` — consults the registry on every
  fetch and skips URLs/domains already crawled by a *different* hunt, then
  records successful fetches so future tasks benefit.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS crawl_records (
  url TEXT PRIMARY KEY,
  domain TEXT NOT NULL,
  hunt_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ok',
  crawled_at TEXT NOT NULL,
  leads INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_crawl_records_domain ON crawl_records(domain);
CREATE INDEX IF NOT EXISTS idx_crawl_records_hunt ON crawl_records(hunt_id);
"""

# Serializes all writes so concurrent hunts / threads can't corrupt the db.
_lock = threading.Lock()


def normalize_domain(url: str) -> str:
    """Return the bare domain (no scheme, no www., no port).

    Accepts either a full URL or an already-normalized bare domain, so it is
    safe to call repeatedly (idempotent).
    """
    if not url:
        return ""
    netloc = urlparse(url).netloc
    if not netloc:
        # No scheme present (e.g. an already-normalized bare domain).
        netloc = url
    domain = netloc.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    if ":" in domain:
        domain = domain.split(":", 1)[0]
    return domain


def normalize_url(url: str) -> str:
    """Return a stable crawl key for a URL (scheme-less, trailing-slash/fragment stripped)."""
    if not url:
        return ""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    path = parsed.path.rstrip("/")
    if not path:
        path = "/"
    key = f"{domain}{path}"
    if parsed.query:
        key += "?" + parsed.query
    return key


class CrawlRegistry:
    """Persistent, cross-hunt registry of crawled URLs and domains."""

    def __init__(self, db_path: str, *, ttl_seconds: int = 0) -> None:
        self.db_path = db_path
        self.ttl_seconds = ttl_seconds
        self._local = threading.local()

    # ── connection management (one connection per thread) ───────────────
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def init_db(self) -> None:
        with _lock:
            conn = self._conn()
            conn.executescript(_DDL)
            conn.commit()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    # ── writes ──────────────────────────────────────────────────────────
    def record(self, url: str, hunt_id: str, *, status: str = "ok", leads: int = 0, crawled_at: str | None = None) -> None:
        """Record (or refresh) a crawled URL. Failures never break a crawl."""
        if not url:
            return
        try:
            domain = normalize_domain(url)
            key = normalize_url(url)
            ts = crawled_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with _lock:
                conn = self._conn()
                conn.execute(
                    """
                    INSERT INTO crawl_records (url, domain, hunt_id, status, crawled_at, leads)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                      domain=excluded.domain,
                      hunt_id=excluded.hunt_id,
                      status=excluded.status,
                      crawled_at=excluded.crawled_at,
                      leads=excluded.leads
                    """,
                    (key, domain, hunt_id or "", status, ts, int(leads or 0)),
                )
                conn.commit()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[CrawlRegistry] record failed: %s", exc)

    def record_many(self, urls: list[str], hunt_id: str, *, status: str = "ok") -> None:
        for u in urls:
            self.record(u, hunt_id, status=status)

    # ── reads ───────────────────────────────────────────────────────────
    def _is_fresh(self, crawled_at: str) -> bool:
        if self.ttl_seconds <= 0:
            return True
        try:
            struct = time.strptime(crawled_at, "%Y-%m-%dT%H:%M:%SZ")
            return (time.time() - time.mktime(struct)) <= self.ttl_seconds
        except Exception:
            return True

    def is_url_crawled(self, url: str, *, exclude_hunt_id: str = "") -> bool:
        """True if this exact URL was crawled (by another hunt if excluded) and still fresh."""
        key = normalize_url(url)
        if not key:
            return False
        try:
            with _lock:
                row = self._conn().execute(
                    "SELECT hunt_id, status, crawled_at FROM crawl_records WHERE url = ?", (key,)
                ).fetchone()
            if not row:
                return False
            if exclude_hunt_id and row["hunt_id"] == exclude_hunt_id:
                return False
            return row["status"] == "ok" and self._is_fresh(row["crawled_at"])
        except Exception:
            return False

    def domain_crawled_by(self, domain: str, *, exclude_hunt_id: str = "") -> str:
        """Return the hunt_id that crawled ``domain`` (fresh), or '' if none / excluded / stale."""
        domain = normalize_domain(domain)
        if not domain:
            return ""
        try:
            with _lock:
                rows = self._conn().execute(
                    "SELECT hunt_id, crawled_at FROM crawl_records WHERE domain = ? AND status='ok'",
                    (domain,),
                ).fetchall()
            for row in rows:
                if exclude_hunt_id and row["hunt_id"] == exclude_hunt_id:
                    continue
                if self._is_fresh(row["crawled_at"]):
                    return str(row["hunt_id"] or "")
        except Exception:
            return ""
        return ""

    def domain_crawl_info(self, domain: str, *, exclude_hunt_id: str = "") -> dict | None:
        """Return {'hunt_id', 'crawled_at'} for a freshly crawled domain, or None."""
        domain = normalize_domain(domain)
        if not domain:
            return None
        try:
            with _lock:
                rows = self._conn().execute(
                    "SELECT hunt_id, crawled_at FROM crawl_records WHERE domain = ? AND status='ok'",
                    (domain,),
                ).fetchall()
            for row in rows:
                if exclude_hunt_id and row["hunt_id"] == exclude_hunt_id:
                    continue
                if self._is_fresh(row["crawled_at"]):
                    return {"hunt_id": str(row["hunt_id"] or ""), "crawled_at": str(row["crawled_at"] or "")}
        except Exception:
            return None
        return None

    def count(self) -> int:
        try:
            with _lock:
                row = self._conn().execute("SELECT COUNT(*) FROM crawl_records").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    # ── bootstrap: seed historical hunt target sites ─────────────────────
    def seed_from_hunts(self, hunts_dir: str) -> int:
        """Seed the registry from existing hunt JSON files (target website_url).

        Returns the number of target sites seeded. Idempotent (upsert by URL).
        """
        hunt_files: list[Path] = []
        root = Path(hunts_dir)
        if root.is_dir():
            hunt_files = sorted(root.glob("*.json"))
        seeded = 0
        for hf in hunt_files:
            try:
                import json

                data = json.loads(hf.read_text(encoding="utf-8"))
                payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
                website_url = str((payload or {}).get("website_url", "") or "").strip()
                hunt_id = str(data.get("hunt_id", "") or hf.stem)
                if website_url and normalize_domain(website_url):
                    self.record(website_url, hunt_id, status="ok")
                    seeded += 1
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("[CrawlRegistry] seed skip %s: %s", hf.name, exc)
        return seeded


def check_target_site_duplicate(
    website_url: str,
    *,
    exclude_hunt_id: str = "",
    settings=None,
) -> dict:
    """Pre-scheduling check: has this target website already been crawled?

    Returns a dict with keys: ``website_url``, ``domain``, ``duplicate``,
    ``previous_hunt_id``, ``crawled_at``. Safe: never raises; on any error
    ``duplicate`` is False.
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    result: dict = {
        "website_url": website_url,
        "domain": "",
        "duplicate": False,
        "previous_hunt_id": "",
        "crawled_at": "",
    }
    if not website_url:
        return result
    domain = normalize_domain(website_url)
    if not domain:
        return result
    result["domain"] = domain

    if not getattr(settings, "crawl_dedup_enabled", False):
        return result

    try:
        reg = CrawlRegistry(settings.crawl_registry_db_path, ttl_seconds=settings.crawl_dedup_ttl_seconds)
        reg.init_db()
        info = reg.domain_crawl_info(domain, exclude_hunt_id=exclude_hunt_id)
        if info:
            result["duplicate"] = True
            result["previous_hunt_id"] = info["hunt_id"]
            result["crawled_at"] = info["crawled_at"]
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[CrawlRegistry] check_target_site_duplicate failed: %s", exc)
    return result
