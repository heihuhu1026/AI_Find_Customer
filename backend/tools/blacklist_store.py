"""Persistent blacklist store — customers excluded from crawling across all hunts.

The blacklist is the single source of truth for "this company/domain must never
be crawled or included as a lead again", shared across hunts and processes via
a small SQLite database (mirroring ``tools/crawl_registry.py``).

Uniqueness rules
----------------
* ``domain`` is the strong, unique key — normalized with ``normalize_domain``
  (lowercase, strip ``www.``/scheme/port/trailing slash). ``example.com``,
  ``www.example.com`` and ``http://example.com/`` are the SAME customer.
* ``customer_name`` is a soft key — normalized (lowercase, collapsed
  whitespace, common company suffixes stripped) and matched exactly. A hit is
  reported as a "suspected duplicate" (same company, different domain) but is
  not auto-blocked, so multi-domain companies can still be listed explicitly.

Filter hook
-----------
:func:`is_blocked` is called by the crawl pipeline (search_agent →
lead_extract_agent → jina_reader) so a blacklisted domain is excluded at the
cheapest possible stage.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from tools.crawl_registry import normalize_domain

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS blacklist (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_name TEXT NOT NULL,
  domain TEXT NOT NULL UNIQUE,
  note TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blacklist_domain ON blacklist(domain);
CREATE INDEX IF NOT EXISTS idx_blacklist_name ON blacklist(customer_name);
"""

_lock = threading.Lock()

# A bare domain must be dot-separated labels of [a-z0-9-], no leading/trailing
# hyphens, and at least one dot (a real TLD).
_DOMAIN_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)

# Common company-designation suffixes, stripped before soft name matching.
_NAME_SUFFIXES = (
    " inc", " incorporated", " corp", " corporation", " co", " company",
    " ltd", " limited", " llc", " plc", " gmbh", " ag", " kg", " sarl", " sas",
    " bv", " nv", " oy", " ab", " spa", " srls", " srl", " pty ltd", " 有限公司",
    " 股份", " 集团", " 有限公司", " co ltd", " co., ltd",
)


def normalize_customer_name(name: str) -> str:
    """Return a soft-match key for a company name (lowercase, collapsed, suffix-stripped)."""
    if not name:
        return ""
    key = " ".join(str(name).lower().split())
    key = re.sub(r"[.,()]", " ", key)
    key = " ".join(key.split())
    # Strip common suffixes from the END (longest first so "co., ltd" beats "co").
    changed = True
    while changed:
        changed = False
        for suffix in sorted(_NAME_SUFFIXES, key=len, reverse=True):
            if key.endswith(suffix):
                key = key[: -len(suffix)].strip()
                changed = True
                break
    return key


def is_valid_domain(domain: str) -> bool:
    """Return True when ``domain`` looks like a real bare domain.

    Accepts a full URL or a bare domain; normalizes first, then validates.
    """
    if not domain:
        return False
    d = normalize_domain(domain)
    return bool(d and _DOMAIN_RE.match(d))


class BlacklistStore:
    """Persistent blacklist with domain-unique upsert and soft name matching."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._local = threading.local()

    # ── connection management ────────────────────────────────────────────
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

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "customer_name": str(row["customer_name"]),
            "domain": str(row["domain"]),
            "note": str(row["note"] or ""),
            "tags": str(row["tags"] or ""),
            "source": str(row["source"] or ""),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    # ── reads ────────────────────────────────────────────────────────────
    def get(self, entry_id: int) -> dict | None:
        try:
            with _lock:
                row = self._conn().execute(
                    "SELECT * FROM blacklist WHERE id = ?", (int(entry_id),)
                ).fetchone()
            return self._row_to_dict(row) if row else None
        except Exception:
            return None

    def find_by_domain(self, domain: str) -> dict | None:
        d = normalize_domain(domain)
        if not d:
            return None
        try:
            with _lock:
                row = self._conn().execute(
                    "SELECT * FROM blacklist WHERE domain = ?", (d,)
                ).fetchone()
            return self._row_to_dict(row) if row else None
        except Exception:
            return None

    def find_by_name(self, customer_name: str) -> dict | None:
        key = normalize_customer_name(customer_name)
        if not key:
            return None
        try:
            with _lock:
                rows = self._conn().execute("SELECT * FROM blacklist").fetchall()
            for row in rows:
                if normalize_customer_name(str(row["customer_name"])) == key:
                    return self._row_to_dict(row)
            return None
        except Exception:
            return None

    def is_blocked(self, domain: str) -> bool:
        """True when the domain (or any URL's domain) is blacklisted."""
        return self.find_by_domain(domain) is not None

    def blocked_domains(self) -> set[str]:
        """Return the full set of blocked domains (for cheap in-memory filtering)."""
        try:
            with _lock:
                rows = self._conn().execute("SELECT domain FROM blacklist").fetchall()
            return {str(r["domain"]) for r in rows}
        except Exception:
            return set()

    def check(self, domain: str = "", customer_name: str = "") -> dict[str, Any]:
        """Validation + duplicate probe for a single entry.

        Returns ``{valid, domain, name_key, exists_domain, exists_id,
        suspected_name, suspected_id, error}``.
        """
        result: dict[str, Any] = {
            "valid": False,
            "domain": "",
            "name_key": "",
            "exists_domain": False,
            "exists_id": None,
            "suspected_name": False,
            "suspected_id": None,
            "error": "",
        }
        d = normalize_domain(domain) if domain else ""
        if not d:
            result["error"] = "domain is required"
            return result
        if not is_valid_domain(domain):
            result["error"] = f"invalid domain: {domain!r}"
            return result
        result["domain"] = d
        result["name_key"] = normalize_customer_name(customer_name)
        result["valid"] = True

        existing = self.find_by_domain(d)
        if existing:
            result["exists_domain"] = True
            result["exists_id"] = existing["id"]
        elif customer_name:
            by_name = self.find_by_name(customer_name)
            if by_name:
                result["suspected_name"] = True
                result["suspected_id"] = by_name["id"]
        return result

    # ── writes ───────────────────────────────────────────────────────────
    def upsert(
        self,
        customer_name: str,
        domain: str,
        *,
        note: str = "",
        tags: str = "",
        source: str = "manual",
    ) -> dict[str, Any]:
        """Insert or update by domain. Returns ``{status, id, entry}``.

        ``status`` is ``created`` (new) or ``updated`` (existing domain).
        Raises ValueError on an invalid/empty domain.
        """
        d = normalize_domain(domain)
        if not is_valid_domain(d):
            raise ValueError(f"invalid domain: {domain!r}")
        name = (customer_name or "").strip() or d
        now = self._now()
        try:
            with _lock:
                conn = self._conn()
                row = conn.execute(
                    "SELECT id FROM blacklist WHERE domain = ?", (d,)
                ).fetchone()
                if row:
                    conn.execute(
                        """
                        UPDATE blacklist
                        SET customer_name=?, note=?, tags=?, source=?, updated_at=?
                        WHERE id=?
                        """,
                        (name, note or "", tags or "", source or "manual", now, int(row["id"])),
                    )
                    conn.commit()
                    entry_id = int(row["id"])
                    status = "updated"
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO blacklist (customer_name, domain, note, tags, source, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (name, d, note or "", tags or "", source or "manual", now, now),
                    )
                    conn.commit()
                    entry_id = int(cur.lastrowid)
                    status = "created"
            return {"status": status, "id": entry_id, "entry": self.get(entry_id)}
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[BlacklistStore] upsert failed: %s", exc)
            raise

    def update(self, entry_id: int, *, customer_name: str | None = None,
               domain: str | None = None, note: str | None = None,
               tags: str | None = None) -> dict | None:
        """Update fields of an existing entry. Returns the updated entry or None."""
        fields: list[str] = []
        params: list[Any] = []
        if customer_name is not None:
            fields.append("customer_name=?")
            params.append((customer_name or "").strip())
        if domain is not None:
            d = normalize_domain(domain)
            if not is_valid_domain(d):
                raise ValueError(f"invalid domain: {domain!r}")
            fields.append("domain=?")
            params.append(d)
        if note is not None:
            fields.append("note=?")
            params.append(note or "")
        if tags is not None:
            fields.append("tags=?")
            params.append(tags or "")
        if not fields:
            return self.get(entry_id)
        fields.append("updated_at=?")
        params.append(self._now())
        params.append(int(entry_id))
        try:
            with _lock:
                self._conn().execute(
                    f"UPDATE blacklist SET {', '.join(fields)} WHERE id=?", params
                )
                self._conn().commit()
            return self.get(entry_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[BlacklistStore] update failed: %s", exc)
            raise

    def delete(self, entry_id: int) -> bool:
        try:
            with _lock:
                cur = self._conn().execute("DELETE FROM blacklist WHERE id=?", (int(entry_id),))
                self._conn().commit()
            return cur.rowcount > 0
        except Exception:
            return False

    def delete_many(self, ids: list[int]) -> int:
        if not ids:
            return 0
        try:
            with _lock:
                cur = self._conn().executemany(
                    "DELETE FROM blacklist WHERE id=?", [(int(i),) for i in ids]
                )
                self._conn().commit()
            return cur.rowcount
        except Exception:
            return 0

    def add_tags(self, ids: list[int], tag: str) -> int:
        """Append a tag (dedup, comma-separated) to each entry. Returns count."""
        tag = (tag or "").strip()
        if not tag or not ids:
            return 0
        updated = 0
        for i in ids:
            entry = self.get(int(i))
            if not entry:
                continue
            existing = [t.strip() for t in str(entry["tags"]).split(",") if t.strip()]
            if tag in existing:
                continue
            existing.append(tag)
            self.update(int(i), tags=",".join(existing))
            updated += 1
        return updated

    # ── listing ──────────────────────────────────────────────────────────
    def list(
        self,
        *,
        q: str = "",
        tag: str = "",
        source: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Return ``{items, total}`` with optional filters and pagination."""
        where: list[str] = []
        params: list[Any] = []
        if q:
            like = f"%{q.strip()}%"
            where.append("(customer_name LIKE ? OR domain LIKE ?)")
            params.extend([like, like])
        if tag:
            where.append("(',' || tags || ',') LIKE ?")
            params.append(f"%,{tag.strip()},%")
        if source:
            where.append("source = ?")
            params.append(source.strip())
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        try:
            with _lock:
                conn = self._conn()
                total = int(conn.execute(f"SELECT COUNT(*) FROM blacklist {clause}", params).fetchone()[0])
                rows = conn.execute(
                    f"SELECT * FROM blacklist {clause} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    params + [int(page_size), max(0, (int(page) - 1) * int(page_size))],
                ).fetchall()
            return {"items": [self._row_to_dict(r) for r in rows], "total": total}
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[BlacklistStore] list failed: %s", exc)
            return {"items": [], "total": 0}

    def count(self) -> int:
        try:
            with _lock:
                row = self._conn().execute("SELECT COUNT(*) FROM blacklist").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0


def load_blocked_domains(settings=None) -> set[str]:
    """Return the current set of blacklisted domains, honoring the feature flag.

    Returns an empty set when filtering is disabled or the store is unavailable,
    so the crawl pipeline degrades to "no blacklist filtering" instead of failing.
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    if not getattr(settings, "blacklist_filter_enabled", True):
        return set()
    try:
        store = BlacklistStore(settings.blacklist_db_path)
        store.init_db()
        try:
            return store.blocked_domains()
        finally:
            store.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[BlacklistStore] load_blocked_domains failed: %s", exc)
        return set()
