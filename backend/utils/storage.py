"""JSON storage adapter over the per-hunt file layout.

Purpose
-------
Hunts live one-file-per-hunt in ``data/hunts/<hunt_id>.json``. This module
exposes stable helpers for reading/updating hunts, their embedded leads
(addressed by a synthetic ``lead_key``), plus cross-hunt collections
(blacklists / portraits) stored in sibling JSON files under ``data/``.

All writes reuse ``api.hunt_store``'s atomic-write + lock semantics so a crash
never leaves a truncated JSON file. Reads are best-effort and never raise.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

from api.hunt_store import load_all_hunts, load_hunt, save_hunt, now_iso
from config.settings import get_settings

logger = logging.getLogger(__name__)

_write_lock = threading.Lock()


# ── Paths ────────────────────────────────────────────────────────────────
def data_dir() -> Path:
    """Directory that holds hunts/ plus the sibling blacklists/portraits files."""
    return Path(get_settings().hunts_dir).parent


def _blacklists_path() -> Path:
    return data_dir() / "blacklists.json"


def _portraits_path() -> Path:
    return data_dir() / "portraits.json"


def _read_json(path: Path, default: Any) -> Any:
    """Read+parse a JSON file; return ``default`` on any error (never raises)."""
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[storage] failed to read %s: %s", path.name, e)
        return default


def _write_json(path: Path, data: Any) -> bool:
    """Atomically write ``data`` to ``path`` (temp file + os.replace)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with _write_lock:
            tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(tmp, path)
        return True
    except Exception as e:
        logger.warning("[storage] failed to write %s: %s", path.name, e)
        try:
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.unlink(missing_ok=True)
        except Exception:  # pragma: no cover
            pass
        return False


# ── Hunts (pass-through to hunt_store) ──────────────────────────────────
def list_hunts() -> list[dict[str, Any]]:
    return list(load_all_hunts().values())


def get_hunt(hunt_id: str) -> dict[str, Any] | None:
    return load_hunt(hunt_id)


def upsert_hunt(hunt_id: str, hunt_data: dict[str, Any]) -> bool:
    return save_hunt(hunt_id, hunt_data)


# ── Leads (no id; synthetic lead_key) ───────────────────────────────────
def _lead_key_domain(website: str) -> str:
    """Lowercase, scheme/www-stripped host used to build a lead key.

    Deliberately a local normalizer (not the project-wide domain normalizer) —
    it only needs to be stable for a given input. Handles scheme-less hosts
    like ``www.example.com`` that urlparse would not parse.
    """
    raw = (website or "").strip().lower()
    raw = re.sub(r"^https?://", "", raw)
    raw = raw.split("/")[0].split("?")[0].split("#")[0]
    if raw.startswith("www."):
        raw = raw[4:]
    return raw.strip()


def make_lead_key(hunt_id: str, lead: dict[str, Any]) -> str:
    """Stable synthetic primary key for a lead (leads have no ``id`` field)."""
    company = re.sub(r"\s+", " ", str(lead.get("company_name") or "")).strip().lower()
    domain = _lead_key_domain(str(lead.get("website") or ""))
    basis = f"{hunt_id}|{domain}|{company}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def get_leads(hunt_id: str) -> list[dict[str, Any]]:
    """Return the leads embedded in a hunt's result (empty list if none)."""
    hunt = load_hunt(hunt_id)
    if not hunt:
        return []
    result = hunt.get("result") or {}
    leads = result.get("leads") or []
    return [lead for lead in leads if isinstance(lead, dict)]


def find_lead(hunt_id: str, lead_key: str) -> dict[str, Any] | None:
    """Find a lead by its synthetic key, or None."""
    for lead in get_leads(hunt_id):
        if make_lead_key(hunt_id, lead) == lead_key:
            return lead
    return None


def update_lead(hunt_id: str, lead_key: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    """Apply ``patch`` to one lead and persist the hunt atomically."""
    hunt = load_hunt(hunt_id)
    if not hunt:
        return None
    result = hunt.setdefault("result", {})
    leads = result.setdefault("leads", [])
    for index, lead in enumerate(leads):
        if not isinstance(lead, dict):
            continue
        if make_lead_key(hunt_id, lead) == lead_key:
            lead.update(patch)
            leads[index] = lead
            if save_hunt(hunt_id, hunt):
                return lead
            return None
    return None


def has_contacted_before(domain: str) -> bool:
    """Best-effort: whether ``domain`` already has generated email sequences.

    The authoritative "already emailed" state lives in the SQLite EmailStore;
    this helper scans persisted hunt results instead so it stays dependency-free.
    """
    needle = _lead_key_domain(domain)
    if not needle:
        return False
    for hunt in load_all_hunts().values():
        result = hunt.get("result") or {}
        sequences = result.get("email_sequences") or []
        for sequence in sequences:
            if not isinstance(sequence, dict):
                continue
            lead = sequence.get("lead") or {}
            if _lead_key_domain(str(lead.get("website") or "")) == needle:
                return True
    return False


# ── Blacklists ──────────────────────────────────────────────────────────
def get_blacklists() -> list[dict[str, Any]]:
    data = _read_json(_blacklists_path(), [])
    return data if isinstance(data, list) else []


def save_blacklist(item: dict[str, Any]) -> bool:
    items = get_blacklists()
    items = [i for i in items if not (isinstance(i, dict) and i.get("id") == item.get("id"))]
    items.append(item)
    return _write_json(_blacklists_path(), items)


def delete_blacklist(item_id: str) -> bool:
    items = get_blacklists()
    remaining = [i for i in items if not (isinstance(i, dict) and i.get("id") == item_id)]
    if len(remaining) == len(items):
        return False
    return _write_json(_blacklists_path(), remaining)


def find_blacklist(item_id: str) -> dict[str, Any] | None:
    for item in get_blacklists():
        if isinstance(item, dict) and str(item.get("id")) == item_id:
            return item
    return None


# ── Portraits ───────────────────────────────────────────────────────────
def get_portraits() -> list[dict[str, Any]]:
    data = _read_json(_portraits_path(), [])
    return data if isinstance(data, list) else []


def save_portrait(item: dict[str, Any]) -> bool:
    items = get_portraits()
    items = [i for i in items if not (isinstance(i, dict) and i.get("id") == item.get("id"))]
    items.append(item)
    return _write_json(_portraits_path(), items)


def get_portrait_by_id(portrait_id: str) -> dict[str, Any] | None:
    for item in get_portraits():
        if isinstance(item, dict) and str(item.get("id")) == portrait_id:
            return item
    return None


def delete_portrait(portrait_id: str) -> bool:
    items = get_portraits()
    remaining = [i for i in items if not (isinstance(i, dict) and i.get("id") == portrait_id)]
    if len(remaining) == len(items):
        return False
    return _write_json(_portraits_path(), remaining)
