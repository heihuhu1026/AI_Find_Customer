"""PortraitBuilder — build customer portraits from source-company domains.

Wraps the existing LangGraph ``insight_node`` for per-company insight, then
aggregates those insights into an ideal-customer-profile (ICP) via the project
``LLMTool`` (so it reuses the configured fallback chain and cost tracking).

Every LLM step degrades gracefully: on any failure it falls back to a
deterministic result instead of raising, so portrait building never blocks.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from services.portrait_service import PortraitService
from tools.llm_client import LLMTool

logger = logging.getLogger(__name__)

_ICP_SYSTEM = (
    "You are a B2B customer-portrait analyst. Given several source-company "
    "insights, distill a single ideal-customer-profile (ICP) as strict JSON with "
    "keys: industries (list), employee_range (list), regions (list), keywords "
    "(list), tech_stack (list). Output ONLY JSON."
)


def _seed_icp(insights: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic fallback ICP derived by unioning insight fields."""
    industries: list[str] = []
    regions: list[str] = []
    keywords: list[str] = []

    def _push(target: list[str], value: Any) -> None:
        if isinstance(value, list):
            for v in value:
                s = str(v).strip()
                if s and s not in target:
                    target.append(s)
        elif value:
            s = str(value).strip()
            if s and s not in target:
                target.append(s)

    for ins in insights:
        if not isinstance(ins, dict):
            continue
        _push(industries, ins.get("industry"))
        _push(industries, ins.get("industries"))
        _push(regions, ins.get("region"))
        _push(regions, ins.get("regions"))
        _push(regions, ins.get("country"))
        _push(keywords, ins.get("keywords"))
        _push(keywords, ins.get("product_keywords"))

    return {
        "industries": industries[:8],
        "employee_range": [],
        "regions": regions[:8],
        "keywords": keywords[:12],
        "tech_stack": [],
    }


async def get_company_insight(domain: str, llm: LLMTool | None = None) -> dict[str, Any]:
    """Fetch a single company's insight by wrapping ``insight_node``.

    Degrades to ``{"domain": domain, "description": domain}`` on any failure.
    """
    from agents.insight_agent import insight_node  # local import to avoid cycles

    state: dict[str, Any] = {
        "website_url": domain,
        "description": "",
        "product_keywords": [],
        "target_customer_profile": "",
        "target_regions": [],
        "uploaded_files": [],
        "target_lead_count": 0,
        "max_rounds": 1,
        "min_new_leads_threshold": 1,
        "enable_email_craft": False,
        "email_template_examples": [],
        "email_template_notes": "",
        "template_seed": None,
        "insight": None,
        "keywords": [],
        "used_keywords": [],
        "search_results": [],
        "seen_urls": [],
        "matched_platforms": [],
        "keyword_search_stats": {},
        "leads": [],
        "email_sequences": [],
        "hunt_round": 1,
        "prev_round_lead_count": 0,
        "round_feedback": None,
        "current_stage": "insight",
        "hunt_id": "",
        "messages": [],
    }
    try:
        result = await insight_node(state)
        insight = result.get("insight") if isinstance(result, dict) else None
        if isinstance(insight, dict) and insight:
            insight.setdefault("domain", domain)
            return insight
        return {"domain": domain, "description": str(domain)}
    except Exception as e:
        logger.warning("[PortraitBuilder] insight failed for %s (degrading): %s", domain, e)
        return {"domain": domain, "description": str(domain)}


async def aggregate_icp(insights: list[dict[str, Any]], llm: LLMTool | None = None) -> dict[str, Any]:
    """Aggregate insights into an ICP via LLM, falling back to ``_seed_icp``."""
    seed = _seed_icp(insights)
    if llm is None:
        return seed
    prompt = json.dumps(insights, ensure_ascii=False, default=str)
    try:
        raw = await llm.generate(
            prompt,
            system=_ICP_SYSTEM,
            response_format={"type": "json_object"},
            max_tokens=800,
        )
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return seed
        for key in ("industries", "employee_range", "regions", "keywords", "tech_stack"):
            if not isinstance(parsed.get(key), list):
                parsed[key] = seed[key]
        return parsed
    except Exception as e:
        logger.warning("[PortraitBuilder] ICP aggregation failed (degrading to seed): %s", e)
        return seed


async def build_portrait(
    name: str,
    source_domains: list[str],
    insight_summary: str = "",
    llm: LLMTool | None = None,
) -> dict[str, Any]:
    """Build + persist a portrait from source domains."""
    domains = [d for d in (source_domains or []) if str(d).strip()][:20]
    insights: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(5)

    async def _one(domain: str) -> None:
        async with sem:
            insights.append(await get_company_insight(domain, llm))

    await asyncio.gather(*[_one(d) for d in domains], return_exceptions=True)

    icp = await aggregate_icp(insights, llm)
    svc = PortraitService()
    return svc.create_portrait(
        name=name,
        source_customers=domains,
        insight_summary=insight_summary or "",
        icp=icp,
    )


async def start_hunt_from_portrait(
    portrait_id: str,
    target_lead_count: int = 1,
    max_rounds: int = 1,
    llm: LLMTool | None = None,
) -> str:
    """Start a real hunt seeded by a portrait's ICP (consumes LLM quota)."""
    from api import routes  # local import to avoid circular import

    svc = PortraitService()
    portrait = svc.get_portrait(portrait_id)
    if not portrait:
        raise ValueError(f"portrait not found: {portrait_id}")

    icp = portrait.get("icp") or {}
    industries = icp.get("industries") or []
    regions = icp.get("regions") or []
    keywords = icp.get("keywords") or []

    request = routes.HuntRequest(
        website_url="",
        description=portrait.get("insight_summary") or portrait.get("name", ""),
        product_keywords=list(keywords),
        target_customer_profile=", ".join(industries),
        uploaded_file_ids=[],
        target_regions=list(regions),
        target_lead_count=int(target_lead_count or 1),
        max_rounds=int(max_rounds or 1),
        min_new_leads_threshold=1,
        enable_email_craft=False,
        email_template_examples=[],
        email_template_notes="",
    )
    resp = await routes.create_hunt_internal(request)
    return resp.hunt_id
