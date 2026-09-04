"""ParseDescriptionAgent — extracts structured hunt parameters from a free-form description.

When a user types something like:
  "我想找东南亚的旅行社公司"
  "Looking for US importers of industrial LED lighting"
  "帮我找巴西的太阳能经销商"

This agent extracts:
  - target_regions: ["东南亚"] / ["US"] / ["Brazil"]
  - target_customer_profile: "旅行社" / "importers of industrial LED" / "solar energy distributors"
  - product_keywords: [] / ["industrial LED lighting"] / ["solar panels"]
  - description_insight: short summary of what was understood (for prompt enrichment)

The extracted values MERGE with (not overwrite) any user-supplied fields —
e.g. if the user also typed a website URL, both the URL and description are used.

This node runs ONLY when state.description is non-empty.
It runs BEFORE insight_node so that InsightAgent receives enriched state.
"""

from __future__ import annotations

import logging

from config.settings import get_settings
from graph.state import HuntState
from tools.llm_client import LLMTool

logger = logging.getLogger(__name__)

from tools.prompts import PARSE_DESCRIPTION_SYSTEM as _SYSTEM_PROMPT


async def parse_description_node(state: HuntState) -> dict:
    """LangGraph node: parse free-form description into structured hunt parameters.

    Runs only when state.description is non-empty.
    Merges extracted values into state — does NOT overwrite user-provided fields.
    """
    description = (state.get("description") or "").strip()
    if not description:
        return {"current_stage": "parse_description"}

    logger.info("[ParseDescription] Parsing: %r", description[:100])
    updates: dict = {"current_stage": "parse_description"}

    llm = LLMTool(
        model_type="cheap",
        hunt_id=state.get("hunt_id", ""),
        agent="parse_description",
        hunt_round=0,
    )

    try:
        raw = await llm.generate(
            description,
            system=_SYSTEM_PROMPT,
            temperature=0.1,
            max_tokens=600,
            response_format={"type": "json_object"},
        )

        from tools.llm_output import parse_json
        parsed = parse_json(raw, context="ParseDescriptionAgent")

        if not parsed or not isinstance(parsed, dict):
            logger.warning("[ParseDescription] Failed to parse LLM output, using description as-is")
            return {"current_stage": "parse_description"}

        # ── Merge target_regions (description supplements user input) ────
        extracted_regions = parsed.get("target_regions") or []
        existing_regions = state.get("target_regions") or []
        if extracted_regions and not existing_regions:
            updates["target_regions"] = extracted_regions
            logger.info("[ParseDescription] Set regions: %s", extracted_regions)
        elif extracted_regions and existing_regions:
            # Merge: user-specified first, then description-extracted
            merged = list(existing_regions)
            for r in extracted_regions:
                if r not in merged:
                    merged.append(r)
            updates["target_regions"] = merged

        # ── Merge target_customer_profile ─────────────────────────────────
        extracted_profile = (parsed.get("target_customer_profile") or "").strip()
        existing_profile = (state.get("target_customer_profile") or "").strip()
        if extracted_profile and not existing_profile:
            updates["target_customer_profile"] = extracted_profile
            logger.info("[ParseDescription] Set customer profile: %r", extracted_profile)

        # ── Merge product_keywords ────────────────────────────────────────
        extracted_kw = [k for k in (parsed.get("product_keywords") or []) if isinstance(k, str) and k.strip()]
        existing_kw = state.get("product_keywords") or []
        if extracted_kw and not existing_kw:
            updates["product_keywords"] = extracted_kw
            logger.info("[ParseDescription] Set product keywords: %s", extracted_kw)
        elif extracted_kw and existing_kw:
            merged_kw = list(existing_kw)
            for k in extracted_kw:
                if k not in merged_kw:
                    merged_kw.append(k)
            updates["product_keywords"] = merged_kw

        # ── Inject description_insight into insight prompt via website_url hint ──
        # We use a special field so InsightAgent can include it in its prompt
        description_insight = parsed.get("description_insight", "")
        if description_insight:
            updates["description_insight"] = description_insight

        logger.info(
            "[ParseDescription] Done — regions=%s, profile=%r, keywords=%s",
            updates.get("target_regions", existing_regions),
            updates.get("target_customer_profile", existing_profile),
            updates.get("product_keywords", existing_kw),
        )

    except Exception as e:
        logger.error("[ParseDescription] Failed: %s", e)
    finally:
        await llm.close()

    return updates
