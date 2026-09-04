"""SearchAgent — sequential priority-based search with failover across providers.

Search backends (any combination may be enabled via settings):
  * google_maps  — Serper Google Maps           (needs SERPER_API_KEY)
  * serpapi      — SerpApi Google organic       (needs SERPAPI_API_KEY)
  * brave        — Brave Web Search             (needs BRAVE_API_KEY)
  * exa          — Exa AI semantic search       (needs EXA_API_KEY)
  * tavily       — Tavily Web Search            (needs TAVILY_API_KEY)
  * parallel     — Parallel Search API          (needs PARALLEL_API_KEY)

Failover model (sequential, quota/credit friendly):
  * Providers are ordered by priority — SEARCH_PROVIDER_ORDER env var, or the
    built-in DEFAULT_SEARCH_PROVIDER_ORDER below (change the env var, not the
    code, to re-order for your network / quota situation).
  * For EVERY keyword, providers are tried one at a time in that order.
      - provider returns results  → stop, use its results (saves quota)
      - provider returns 0 results → try the next provider
      - provider hits quota / network / auth error → mark it temporarily
        unavailable (circuit breaker with cooldown), try the next provider
  * If every configured provider fails/returns nothing for a keyword, that
    keyword reports no results (not a hard pipeline failure).
  * The node returns `search_failed=True` only when NO provider produced ANY
    result across the whole round, so the pipeline / user sees the real cause
    instead of a silent 0-lead "completed" run.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

import httpx

from config.settings import Settings, get_settings
from graph.state import HuntState
from tools.blacklist_store import load_blocked_domains
from tools.brave_search import BraveSearchTool
from tools.crawl_registry import normalize_domain
from tools.exa_search import ExaSearchTool
from tools.google_maps_search import GoogleMapsSearchTool
from tools.parallel_search import ParallelSearchTool
from tools.serpapi_search import SerpApiSearchTool
from tools.web_search import WebSearchTool  # Tavily-backed web search

logger = logging.getLogger(__name__)

# ── Region → geo params mapping ─────────────────────────────────────────
# Kept here because KeywordGenAgent imports _REGION_GEO for language hints.
_REGION_GEO: dict[str, dict[str, str]] = {
    # English — Countries
    "germany": {"gl": "de", "hl": "de"},
    "france": {"gl": "fr", "hl": "fr"},
    "uk": {"gl": "uk", "hl": "en"},
    "united kingdom": {"gl": "uk", "hl": "en"},
    "italy": {"gl": "it", "hl": "it"},
    "spain": {"gl": "es", "hl": "es"},
    "netherlands": {"gl": "nl", "hl": "nl"},
    "poland": {"gl": "pl", "hl": "pl"},
    "czech republic": {"gl": "cz", "hl": "cs"},
    "czechia": {"gl": "cz", "hl": "cs"},
    "romania": {"gl": "ro", "hl": "ro"},
    "hungary": {"gl": "hu", "hl": "hu"},
    "turkey": {"gl": "tr", "hl": "tr"},
    "russia": {"gl": "ru", "hl": "ru"},
    "ukraine": {"gl": "ua", "hl": "uk"},
    "sweden": {"gl": "se", "hl": "sv"},
    "norway": {"gl": "no", "hl": "no"},
    "denmark": {"gl": "dk", "hl": "da"},
    "finland": {"gl": "fi", "hl": "fi"},
    "portugal": {"gl": "pt", "hl": "pt"},
    "austria": {"gl": "at", "hl": "de"},
    "switzerland": {"gl": "ch", "hl": "de"},
    "belgium": {"gl": "be", "hl": "nl"},
    "greece": {"gl": "gr", "hl": "el"},
    "usa": {"gl": "us", "hl": "en"},
    "united states": {"gl": "us", "hl": "en"},
    "canada": {"gl": "ca", "hl": "en"},
    "australia": {"gl": "au", "hl": "en"},
    "new zealand": {"gl": "nz", "hl": "en"},
    "japan": {"gl": "jp", "hl": "ja"},
    "south korea": {"gl": "kr", "hl": "ko"},
    "india": {"gl": "in", "hl": "en"},
    "brazil": {"gl": "br", "hl": "pt"},
    "mexico": {"gl": "mx", "hl": "es"},
    "china": {"gl": "cn", "hl": "zh-cn"},
    "singapore": {"gl": "sg", "hl": "en"},
    "thailand": {"gl": "th", "hl": "th"},
    "vietnam": {"gl": "vn", "hl": "vi"},
    "indonesia": {"gl": "id", "hl": "id"},
    "malaysia": {"gl": "my", "hl": "en"},
    "philippines": {"gl": "ph", "hl": "en"},
    "south africa": {"gl": "za", "hl": "en"},
    "uae": {"gl": "ae", "hl": "en"},
    "saudi arabia": {"gl": "sa", "hl": "ar"},
    # English — Composite regions
    "europe": {"gl": "de", "hl": "en"},
    "western europe": {"gl": "de", "hl": "en"},
    "eastern europe": {"gl": "pl", "hl": "en"},
    "central europe": {"gl": "de", "hl": "en"},
    "northern europe": {"gl": "se", "hl": "en"},
    "southern europe": {"gl": "it", "hl": "en"},
    "nordic": {"gl": "se", "hl": "en"},
    "scandinavia": {"gl": "se", "hl": "en"},
    "north america": {"gl": "us", "hl": "en"},
    "south america": {"gl": "br", "hl": "pt"},
    "latin america": {"gl": "br", "hl": "es"},
    "southeast asia": {"gl": "sg", "hl": "en"},
    "east asia": {"gl": "jp", "hl": "en"},
    "south asia": {"gl": "in", "hl": "en"},
    "middle east": {"gl": "ae", "hl": "en"},
    "africa": {"gl": "za", "hl": "en"},
    "oceania": {"gl": "au", "hl": "en"},
    # Chinese — Countries
    "德国": {"gl": "de", "hl": "de"},
    "法国": {"gl": "fr", "hl": "fr"},
    "英国": {"gl": "uk", "hl": "en"},
    "意大利": {"gl": "it", "hl": "it"},
    "西班牙": {"gl": "es", "hl": "es"},
    "荷兰": {"gl": "nl", "hl": "nl"},
    "波兰": {"gl": "pl", "hl": "pl"},
    "捷克": {"gl": "cz", "hl": "cs"},
    "罗马尼亚": {"gl": "ro", "hl": "ro"},
    "匈牙利": {"gl": "hu", "hl": "hu"},
    "土耳其": {"gl": "tr", "hl": "tr"},
    "俄罗斯": {"gl": "ru", "hl": "ru"},
    "乌克兰": {"gl": "ua", "hl": "uk"},
    "瑞典": {"gl": "se", "hl": "sv"},
    "挪威": {"gl": "no", "hl": "no"},
    "丹麦": {"gl": "dk", "hl": "da"},
    "芬兰": {"gl": "fi", "hl": "fi"},
    "葡萄牙": {"gl": "pt", "hl": "pt"},
    "奥地利": {"gl": "at", "hl": "de"},
    "瑞士": {"gl": "ch", "hl": "de"},
    "比利时": {"gl": "be", "hl": "nl"},
    "希腊": {"gl": "gr", "hl": "el"},
    "美国": {"gl": "us", "hl": "en"},
    "加拿大": {"gl": "ca", "hl": "en"},
    "澳大利亚": {"gl": "au", "hl": "en"},
    "新西兰": {"gl": "nz", "hl": "en"},
    "日本": {"gl": "jp", "hl": "ja"},
    "韩国": {"gl": "kr", "hl": "ko"},
    "印度": {"gl": "in", "hl": "en"},
    "巴西": {"gl": "br", "hl": "pt"},
    "墨西哥": {"gl": "mx", "hl": "es"},
    "中国": {"gl": "cn", "hl": "zh-cn"},
    "新加坡": {"gl": "sg", "hl": "en"},
    "泰国": {"gl": "th", "hl": "th"},
    "越南": {"gl": "vn", "hl": "vi"},
    "印尼": {"gl": "id", "hl": "id"},
    "印度尼西亚": {"gl": "id", "hl": "id"},
    "马来西亚": {"gl": "my", "hl": "en"},
    "菲律宾": {"gl": "ph", "hl": "en"},
    "南非": {"gl": "za", "hl": "en"},
    "阿联酋": {"gl": "ae", "hl": "en"},
    "沙特": {"gl": "sa", "hl": "ar"},
    "沙特阿拉伯": {"gl": "sa", "hl": "ar"},
    # Chinese — Composite regions
    "欧洲": {"gl": "de", "hl": "en"},
    "西欧": {"gl": "de", "hl": "en"},
    "东欧": {"gl": "pl", "hl": "en"},
    "中欧": {"gl": "de", "hl": "en"},
    "北欧": {"gl": "se", "hl": "en"},
    "南欧": {"gl": "it", "hl": "en"},
    "北美": {"gl": "us", "hl": "en"},
    "南美": {"gl": "br", "hl": "pt"},
    "拉丁美洲": {"gl": "br", "hl": "es"},
    "东南亚": {"gl": "sg", "hl": "en"},
    "东亚": {"gl": "jp", "hl": "en"},
    "南亚": {"gl": "in", "hl": "en"},
    "中东": {"gl": "ae", "hl": "en"},
    "非洲": {"gl": "za", "hl": "en"},
    "大洋洲": {"gl": "au", "hl": "en"},
}


def _resolve_geo_params(target_regions: list[str]) -> dict[str, str]:
    """Convert target_regions list to Serper gl/hl params using first match."""
    for region in target_regions:
        key = region.strip().lower()
        if key in _REGION_GEO:
            return _REGION_GEO[key]
    return {}


_CHINA_KEYWORDS = {"china", "中国", "cn", "大陆", "mainland china"}


def _is_china_region(target_regions: list[str]) -> bool:
    """Backward-compatible helper kept for tests and external imports."""
    for region in target_regions:
        if region.strip().lower() in _CHINA_KEYWORDS:
            return True
    return False


def _result_identity_key(item: dict) -> str:
    """Stable dedupe key for a search result, including Maps-only rows without website."""
    link = (item.get("link") or "").strip().lower()
    if link:
        return f"url:{link}"

    maps_data = item.get("maps_data") or {}
    place_id = (maps_data.get("place_id") or maps_data.get("cid") or "").strip().lower()
    if place_id:
        return f"place:{place_id}"

    title = (item.get("title") or "").strip().lower()
    address = (maps_data.get("address") or "").strip().lower()
    if title or address:
        return f"maps:{title}|{address}"

    return ""


def _place_to_result(place: dict, keyword: str) -> dict:
    """Normalize a Google Maps place into the common search-result schema."""
    maps_data = {
        "title": place.get("title", ""),
        "address": place.get("address", ""),
        "type": place.get("type", ""),
        "types": place.get("types", []),
        "website": place.get("website", ""),
        "phone_number": place.get("phone_number", ""),
        "phoneNumber": place.get("phone_number", ""),
        "description": place.get("description", ""),
        "email": place.get("email", ""),
        "rating": place.get("rating", 0),
        "rating_count": place.get("rating_count", 0),
        "latitude": place.get("latitude", 0),
        "longitude": place.get("longitude", 0),
        "cid": place.get("cid", ""),
        "place_id": place.get("place_id", ""),
    }
    parts = []
    if maps_data["type"]:
        parts.append(maps_data["type"])
    if maps_data["address"]:
        parts.append(maps_data["address"])
    if maps_data["phone_number"]:
        parts.append(maps_data["phone_number"])
    if maps_data["rating"]:
        parts.append(f"Rating: {maps_data['rating']}/5 ({maps_data['rating_count']} reviews)")
    return {
        "title": place.get("title", ""),
        "link": place.get("website", ""),
        "snippet": " | ".join(parts),
        "position": 0,
        "source": "google_maps",
        "maps_data": maps_data,
        "source_keyword": keyword,
    }


def _web_to_result(item: dict, keyword: str, provider: str) -> dict:
    """Normalize a web-search (Brave/Tavily/SerpApi/Exa/Parallel) hit into the common schema."""
    return {
        "title": item.get("title", ""),
        "link": item.get("link", ""),
        "snippet": item.get("snippet", ""),
        "position": item.get("position", 0),
        "source": provider,
        "maps_data": {},
        "source_keyword": keyword,
    }


# ── Provider priority order ────────────────────────────────────────────────
# "墙内可访问/免费额度稳定"优先. Override at runtime via SEARCH_PROVIDER_ORDER
# (comma separated) in .env — no code change needed to re-prioritize.
DEFAULT_SEARCH_PROVIDER_ORDER = "brave,serpapi,exa,google_maps,tavily,parallel"

# Provider name -> settings field holding its API key.
_PROVIDER_KEY_FIELD: dict[str, str] = {
    "google_maps": "serper_api_key",
    "serpapi": "serpapi_api_key",
    "brave": "brave_api_key",
    "exa": "exa_api_key",
    "tavily": "tavily_api_key",
    "parallel": "parallel_api_key",
}

_PROVIDER_NAMES = set(_PROVIDER_KEY_FIELD)


def _resolve_provider_order(settings: Settings) -> list[str]:
    """Return the ordered provider list: env override wins, else default."""
    raw = (settings.search_provider_order or "").strip()
    if not raw:
        raw = DEFAULT_SEARCH_PROVIDER_ORDER
    order: list[str] = []
    for name in (p.strip().lower() for p in raw.split(",") if p.strip()):
        if name in _PROVIDER_NAMES and name not in order:
            order.append(name)
    return order


# ── Provider circuit breaker (process-wide, thread-safe) ────────────────────
# Remember quota-exhausted / broken providers for a short cooldown so repeated
# keywords do not keep hammering a dead backend within a hunt or across hunts.
_PROVIDER_HEALTH: dict[str, dict[str, Any]] = {}
_HEALTH_LOCK = threading.Lock()

_COOLDOWN_SECONDS = {
    "quota": 1800,       # out of credits — retry in 30 min
    "rate_limit": 300,   # 429 without explicit quota wording — 5 min
    "auth": 3600,        # bad key — 1 h
    "network": 120,      # connectivity/timeout — 2 min
    "other": 60,
}

_QUOTA_HINTS = (
    "quota", "credit", "insufficient", "payment", "plan limit",
    "usage limit", "billing", "no balance", "limit exceeded",
    "exceeded your", "余额不足", "无余额",
)


def _classify_provider_error(exc: Exception) -> str:
    """Return an error category for a provider exception."""
    msg = str(exc).lower()
    status = None
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        try:
            msg = (msg + " " + exc.response.text).lower()
        except Exception:
            pass
    elif isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout,
                          httpx.ReadTimeout, httpx.RequestError,
                          asyncio.TimeoutError)):
        return "network"

    if status == 401:
        return "auth"
    if status == 402:
        return "quota"
    if status == 403:
        return "quota" if any(h in msg for h in _QUOTA_HINTS) else "auth"
    if status == 429:
        # Bare 429 = transient rate limit (short cooldown). 429 with quota
        # wording (e.g. Tavily free-tier cap) is treated as quota exhausted.
        return "quota" if any(h in msg for h in _QUOTA_HINTS) else "rate_limit"
    if any(h in msg for h in _QUOTA_HINTS):
        return "quota"
    if status is not None and status >= 500:
        return "network"
    return "other"


def _mark_provider_unhealthy(name: str, category: str) -> None:
    with _HEALTH_LOCK:
        _PROVIDER_HEALTH[name] = {
            "category": category,
            "blocked_until": time.monotonic() + _COOLDOWN_SECONDS.get(category, 60),
        }
    logger.warning("[SearchAgent] %s marked unhealthy (%s) — will skip for a while", name, category)


def _provider_blocked(name: str) -> str | None:
    """Return the reason a provider is temporarily blocked, or None if usable."""
    with _HEALTH_LOCK:
        info = _PROVIDER_HEALTH.get(name)
        if not info:
            return None
        if time.monotonic() >= info["blocked_until"]:
            _PROVIDER_HEALTH.pop(name, None)
            return None
        return info.get("category", "unknown")


def _reset_provider_health() -> None:
    """Clear circuit-breaker state (used by tests / manual recovery)."""
    with _HEALTH_LOCK:
        _PROVIDER_HEALTH.clear()


# ── Per-keyword failover executor ───────────────────────────────────────────

async def _search_keyword(
    keyword: str,
    providers: list[tuple[str, str, Any]],  # (name, kind: "web"|"maps", tool)
    semaphore: asyncio.Semaphore,
    *,
    gl: str,
    hl: str,
    num: int,
    round_errors: dict[str, list[str]],
) -> tuple[str, str | None, list[dict], dict[str, dict[str, Any]]]:
    """Try providers in priority order until one returns results.

    Returns (keyword, winning_provider|None, items, per_provider_stats).
    """
    per_provider: dict[str, dict[str, Any]] = {}
    async with semaphore:
        for name, kind, tool in providers:
            if _provider_blocked(name):
                per_provider[name] = {"result_count": 0, "error": f"skipped ({_provider_blocked(name)})"}
                continue
            try:
                if kind == "maps":
                    places = await tool.search(keyword, gl=gl, hl=hl)
                    items = [_place_to_result(p, keyword) for p in places]
                else:
                    raw = await tool.search(keyword, num=num, gl=gl, hl=hl)
                    items = [_web_to_result(r, keyword, name) for r in raw]
                per_provider[name] = {"result_count": len(items), "error": None}
                if items:
                    logger.info("[SearchAgent] keyword=%r served by %s (%d results)", keyword, name, len(items))
                    return keyword, name, items, per_provider
                logger.info("[SearchAgent] %s returned 0 results for %r — trying next", name, keyword)
            except Exception as e:  # noqa: BLE001 — any provider failure should fail over
                category = _classify_provider_error(e)
                _mark_provider_unhealthy(name, category)
                round_errors.setdefault(name, []).append(str(e)[:160])
                per_provider[name] = {"result_count": 0, "error": f"{category}: {e}"[:160]}
                logger.warning("[SearchAgent] %s FAILED keyword=%r (%s): %s", name, keyword, category, e)
        return keyword, None, [], per_provider


async def search_node(state: HuntState) -> dict:
    """LangGraph node: sequentially search every keyword with failover."""
    from observability.cost_tracker import get_tracker

    settings = get_settings()
    keywords = state.get("keywords", [])
    target_regions = state.get("target_regions", [])

    logger.info("[SearchAgent] Starting — %d keywords, regions=%s", len(keywords), target_regions)

    if not keywords:
        logger.info("[SearchAgent] No keywords to search, skipping")
        return {"current_stage": "search"}

    geo = _resolve_geo_params(target_regions)
    gl = geo.get("gl", "")
    hl = geo.get("hl", "")
    if geo:
        logger.info("[SearchAgent] Resolved regions %s → gl=%s, hl=%s", target_regions, gl, hl)
    else:
        logger.info("[SearchAgent] No geo params resolved from regions=%s, searching globally", target_regions)

    # ── Build ordered provider list from settings keys + priority order ──
    order = _resolve_provider_order(settings)
    tools: list[Any] = []
    providers: list[tuple[str, str, Any]] = []
    skipped: list[str] = []

    factories = {
        "google_maps": lambda: GoogleMapsSearchTool(settings),
        "brave": lambda: BraveSearchTool(settings=settings),
        "tavily": lambda: WebSearchTool(settings),
        "serpapi": lambda: SerpApiSearchTool(settings),
        "exa": lambda: ExaSearchTool(settings),
        "parallel": lambda: ParallelSearchTool(settings),
    }

    for name in order:
        key_value = getattr(settings, _PROVIDER_KEY_FIELD[name], "")
        if not key_value:
            continue
        kind = "maps" if name == "google_maps" else "web"
        try:
            tool = factories[name]()
        except Exception as e:  # e.g. WebSearchTool raises when key pool empty
            logger.warning("[SearchAgent] %s unavailable: %s", name, e)
            skipped.append(name)
            continue
        tools.append(tool)
        providers.append((name, kind, tool))

    if not providers:
        msg = (
            "No search provider configured. Set any of: SERPER_API_KEY, "
            "SERPAPI_API_KEY, BRAVE_API_KEY, EXA_API_KEY, TAVILY_API_KEY, "
            "PARALLEL_API_KEY."
        )
        logger.error("[SearchAgent] %s", msg)
        return {
            "search_results": list(state.get("search_results", [])),
            "seen_urls": list(state.get("seen_urls", [])),
            "matched_platforms": [],
            "keyword_search_stats": dict(state.get("keyword_search_stats", {})),
            "current_stage": "search",
            "search_failed": True,
            "search_error": msg,
            "search_provider_status": {},
        }

    logger.info("[SearchAgent] Provider chain (priority order): %s", [p[0] for p in providers])

    # Layer 0 blacklist filter — drop blacklisted domains as early as possible,
    # before they consume lead-extraction (LLM/scrape) budget downstream.
    blocked_domains = load_blocked_domains(settings)

    semaphore = asyncio.Semaphore(settings.search_concurrency)
    num = 10
    round_errors: dict[str, list[str]] = {}

    results = await asyncio.gather(*[
        _search_keyword(kw, providers, semaphore, gl=gl, hl=hl, num=num, round_errors=round_errors)
        for kw in keywords
    ])

    # ── Merge winning results, keep global URL de-dup ────────────────────
    state_seen = state.get("seen_urls") or []
    seen_keys: set[str] = set(state_seen)
    if not seen_keys:
        for r in state.get("search_results", []):
            key = _result_identity_key(r)
            if key:
                seen_keys.add(key)

    keyword_stats: dict[str, Any] = dict(state.get("keyword_search_stats", {}))
    initial_new_results: list[dict] = []
    providers_ok: set[str] = set()

    for kw, winning, items, per_provider in results:
        if kw not in keyword_stats:
            keyword_stats[kw] = {"result_count": 0, "leads_found": 0, "sources": {}}
        kwstat = keyword_stats[kw]
        kwstat.setdefault("sources", {})
        for pname, pstat in per_provider.items():
            kwstat["sources"][pname] = pstat
        if items:
            for item in items:
                item["source_keyword"] = kw
                # Layer 0: skip blacklisted domains before dedup/accumulation.
                if blocked_domains and normalize_domain(item.get("link", "")) in blocked_domains:
                    logger.info("[SearchAgent] blacklist skip %s (keyword %r)", item.get("link", ""), kw)
                    continue
                item_key = _result_identity_key(item)
                if not item_key or item_key in seen_keys:
                    continue
                seen_keys.add(item_key)
                initial_new_results.append(item)
                kwstat["result_count"] = kwstat.get("result_count", 0) + 1
            providers_ok.add(winning)

    # ── Per-provider health for status reporting ─────────────────────────
    search_provider_status: dict[str, str] = {}
    for pname, _kind, _tool in providers:
        if pname in providers_ok:
            search_provider_status[pname] = "ok"
        else:
            errs = round_errors.get(pname)
            if errs:
                search_provider_status[pname] = f"error: {errs[0]}"
            else:
                search_provider_status[pname] = "no results"

    search_failed = len(providers_ok) == 0
    if search_failed:
        detail = "; ".join(
            f"{p}: {errs[0]}" for p, errs in round_errors.items()
        )
        search_error = f"All search providers failed. {detail}" if detail else \
            "No search provider returned results."
        logger.error("[SearchAgent] %s", search_error)
    else:
        search_error = ""
        logger.info(
            "[SearchAgent] Completed — %d new results (serving providers: %s)",
            len(initial_new_results), sorted(providers_ok),
        )

    # ── Cost tracking ────────────────────────────────────────────────────
    hunt_id = state.get("hunt_id", "")
    if hunt_id:
        try:
            tracker = get_tracker(hunt_id)
            for kw, winning, items, per_provider in results:
                if winning:
                    tracker.record_search_call(provider=winning, result_count=len(items))
        except Exception:
            pass

    # ── Close provider tools ─────────────────────────────────────────────
    for tool in tools:
        try:
            await tool.close()
        except Exception as e:
            logger.warning("[SearchAgent] Error closing tool: %s", e)

    return {
        "search_results": list(state.get("search_results", [])) + initial_new_results,
        "seen_urls": list(seen_keys),
        "matched_platforms": [],
        "keyword_search_stats": keyword_stats,
        "current_stage": "search",
        "search_failed": search_failed,
        "search_error": search_error,
        "search_provider_status": search_provider_status,
    }
