"""Tests for the three-layer blacklist filter in the crawl pipeline."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agents.search_agent import search_node
from agents.lead_extract_agent import lead_extract_node
from tools.jina_reader import JinaReaderTool


def _base_state(**overrides):
    base = {
        "website_url": "https://solartech.de",
        "product_keywords": ["solar inverter"],
        "target_regions": ["Europe"],
        "uploaded_files": [],
        "target_lead_count": 200,
        "max_rounds": 10,
        "insight": {"products": ["solar inverter"], "industries": ["Renewable Energy"]},
        "keywords": ["solar inverter distributor"],
        "used_keywords": ["solar inverter distributor"],
        "search_results": [],
        "seen_urls": [],
        "matched_platforms": [],
        "keyword_search_stats": {},
        "leads": [],
        "email_sequences": [],
        "hunt_round": 1,
        "prev_round_lead_count": 0,
        "round_feedback": None,
        "current_stage": "keyword_gen",
        "messages": [],
    }
    base.update(overrides)
    return base


class TestLayer0SearchAgent:
    @pytest.mark.asyncio
    async def test_blacklisted_result_dropped(self):
        state = _base_state(keywords=["k1"])
        brave_inst = AsyncMock()
        brave_inst.search = AsyncMock(return_value=[
            {"title": "Blocked Co", "link": "https://blocked.com/x", "snippet": "s", "position": 1},
            {"title": "Good Co", "link": "https://good.com/x", "snippet": "s", "position": 2},
        ])
        brave_inst.close = AsyncMock()
        settings = SimpleNamespace(
            search_concurrency=5,
            search_provider_order="brave",
            brave_api_key="bk",
            serper_api_key="", serpapi_api_key="", exa_api_key="", tavily_api_key="", parallel_api_key="",
        )
        with patch("agents.search_agent.get_settings", return_value=settings), \
             patch("agents.search_agent.BraveSearchTool", return_value=brave_inst), \
             patch("agents.search_agent.load_blocked_domains", return_value={"blocked.com"}):
            result = await search_node(state)

        assert result["search_failed"] is False
        links = [r["link"] for r in result["search_results"]]
        assert links == ["https://good.com/x"]
        assert "https://blocked.com/x" not in links


class TestLayer1LeadExtract:
    @pytest.mark.asyncio
    async def test_blacklisted_candidate_skipped_before_gate(self):
        state = _base_state(
            search_results=[{"title": "Blocked", "link": "https://blocked.com", "source": "brave"}],
            leads=[],
        )
        settings = SimpleNamespace()
        with patch("agents.lead_extract_agent.get_settings", return_value=settings), \
             patch("agents.lead_extract_agent.load_blocked_domains", return_value={"blocked.com"}):
            result = await lead_extract_node(state)

        # All candidates blacklisted => no processable URLs, early return.
        assert result == {"current_stage": "lead_extract"}


class TestLayer2JinaReader:
    @pytest.mark.asyncio
    async def test_blacklisted_domain_not_fetched(self):
        settings = SimpleNamespace(
            crawl_dedup_enabled=False,
            jina_api_key="",
        )
        reader = JinaReaderTool(settings)
        with patch("tools.jina_reader.load_blocked_domains", return_value={"blocked.com"}):
            content = await reader.read("https://blocked.com/page")
        assert content == ""
        await reader.close()

    @pytest.mark.asyncio
    async def test_non_blacklisted_domain_proceeds(self):
        import httpx

        settings = SimpleNamespace(
            crawl_dedup_enabled=False,
            jina_api_key="",
        )
        reader = JinaReaderTool(settings)
        fake_resp = httpx.Response(200, text="# Good Co\ncontent", request=httpx.Request("GET", "https://good.com"))
        with patch("tools.jina_reader.load_blocked_domains", return_value={"blocked.com"}), \
             patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=fake_resp):
            content = await reader.read("https://good.com/page")
        assert "Good Co" in content
        await reader.close()

    @pytest.mark.asyncio
    async def test_own_domain_exempt_from_blacklist(self):
        import httpx

        settings = SimpleNamespace(
            crawl_dedup_enabled=False,
            jina_api_key="",
        )
        reader = JinaReaderTool(settings, own_domains={"solartech.de"})
        fake_resp = httpx.Response(200, text="# Own site\ncontent", request=httpx.Request("GET", "https://solartech.de"))
        with patch("tools.jina_reader.load_blocked_domains", return_value={"solartech.de"}), \
             patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=fake_resp):
            content = await reader.read("https://solartech.de/page")
        assert "Own site" in content
        await reader.close()
