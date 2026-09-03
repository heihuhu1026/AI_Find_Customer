"""PortraitService — CRUD over persisted customer portraits (no LLM).

Portraits are built from source customers' domains; the ICP aggregation that
involves an LLM lives in ``services/portrait_builder.py``. This service is the
thin persistence layer on top of ``utils.storage``.
"""

from __future__ import annotations

import uuid
from typing import Any

from models import ICPProfile
from utils.storage import (
    get_portrait_by_id,
    get_portraits,
    save_portrait,
    delete_portrait as storage_delete_portrait,
)


class PortraitService:
    """Create / read / update / delete customer portraits."""

    def create_portrait(
        self,
        name: str,
        source_customers: list[str] | None = None,
        insight_summary: str = "",
        icp: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a portrait and persist it. Returns the stored dict."""
        icp_model = ICPProfile(**(icp or {})) if icp else ICPProfile()
        item = {
            "id": uuid.uuid4().hex,
            "name": name or "untitled portrait",
            "source_customers": list(source_customers or []),
            "icp": icp_model.model_dump(),
            "insight_summary": insight_summary or "",
            "created_at": "",
            "hunt_count": 0,
            "total_leads": 0,
        }
        save_portrait(item)
        return item

    def get_portrait(self, portrait_id: str) -> dict[str, Any] | None:
        return get_portrait_by_id(portrait_id)

    def list_portraits(self) -> list[dict[str, Any]]:
        return get_portraits()

    def bump_stats(self, portrait_id: str, hunt_count_delta: int = 0, total_leads_delta: int = 0) -> dict[str, Any] | None:
        """Increment a portrait's hunt/lead counters and persist."""
        item = get_portrait_by_id(portrait_id)
        if not item:
            return None
        item["hunt_count"] = int(item.get("hunt_count", 0)) + int(hunt_count_delta)
        item["total_leads"] = int(item.get("total_leads", 0)) + int(total_leads_delta)
        save_portrait(item)
        return item

    def delete_portrait(self, portrait_id: str) -> bool:
        return storage_delete_portrait(portrait_id)
