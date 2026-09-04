"""Tests for agents/search_agent.py — sequential provider failover, order, dedup, stats."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agents.search_agent import (
    DEFAULT_SEARCH_PROVIDER_ORDER,
    _classify_provider_error,
    _is_china_region,
    _mark_provider_unhealthy,
    _provider_blocked,
    _reset_provider_health,
    _resolve_provider_order,
    _result_identity_key,
    search_node,
    _search_keyword,
)


def _settings(**overrides):
    base = dict(
        search_concurrency=5,
        search_provider_order="",
        serper_api_key="",
        serpapi_api_key="",
        brave_api_key="",
        exa_api_key="",
        tavily_api_key="",
        parallel_api_key="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _base_state(**overrides):
    base = {
        "website_url": "https://solartech.de",
        "product_keywords": ["solar inverter"],
        "target_regions": ["Europe"],
        "uploaded_files": [],
        "target_lead_count": 200,
        "max_rounds": 10,
        "insight": {"products": ["solar inverter"], "industries": ["Renewable Energy"]},
        "keywords": ["solar inverter distributor", "PV panel wholesale"],
        "used_keywords": ["solar inverter distributor", "PV panel wholesale"],
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


def _web_hit(title="A", link="https://a.com"):
    return {"title": title, "link": link, "snippet": "snippet", "position": 1}


def _http_error(status: int, text: str = ""):
    req = httpx.Request("GET", "https://provider.test")
    resp = httpx.Response(status, request=req, text=text)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


@pytest.fixture(autouse=True)
def _clean_health():
    _reset_provider_health()
    yield
    _reset_provider_health()


class TestProviderOrder:
    def test_default_order(self):
        assert _resolve_provider_order(_settings()) == DEFAULT_SEARCH_PROVIDER_ORDER.split(",")

    def test_env_order_wins(self):
        s = _settings(search_provider_order="exa,brave,tavily")
        assert _resolve_provider_order(s) == ["exa", "brave", "tavily"]

    def test_unknown_names_dropped(self):
        s = _settings(search_provider_order="foo,brave,bar,exa")
        assert _resolve_provider_order(s) == ["brave", "exa"]

    def test_duplicates_dropped(self):
        s = _settings(search_provider_order="brave,brave,exa")
        assert _resolve_provider_order(s) == ["brave", "exa"]


class TestClassifyProviderError:
    def test_http_402_is_quota(self):
        assert _classify_provider_error(_http_error(402)) == "quota"

    def test_http_429_with_quota_wording_is_quota(self):
        assert _classify_provider_error(_http_error(429, text="quota exceeded")) == "quota"

    def test_bare_429_is_rate_limit(self):
        assert _classify_provider_error(_http_error(429)) == "rate_limit"

    def test_401_is_auth(self):
        assert _classify_provider_error(_http_error(401)) == "auth"

    def test_403_with_quota_wording_is_quota(self):
        assert _classify_provider_error(_http_error(403, text="insufficient credit")) == "quota"

    def test_403_plain_is_auth(self):
        assert _classify_provider_error(_http_error(403, text="forbidden")) == "auth"

    def test_500_is_network(self):
        assert _classify_provider_error(_http_error(500)) == "network"

    def test_generic_message_with_quota_word(self):
        assert _classify_provider_error(RuntimeError("Your plan quota is exhausted")) == "quota"


class TestCircuitBreaker:
    def test_mark_unhealthy_blocks_provider(self):
        _mark_provider_unhealthy("brave", "quota")
        assert _provider_blocked("brave") == "quota"

    def test_unmarked_provider_is_not_blocked(self):
        assert _provider_blocked("brave") is None

    def test_reset_clears_block(self):
        _mark_provider_unhealthy("brave", "auth")
        _reset_provider_health()
        assert _provider_blocked("brave") is None


class TestSearchKeywordFailover:
    @pytest.mark.asyncio
    async def test_first_provider_with_results_wins_and_stops(self):
        sem = asyncio.Semaphore(5)
        first = AsyncMock(search=AsyncMock(return_value=[_web_hit()]))
        second = AsyncMock(search=AsyncMock(return_value=[_web_hit(title="B", link="https://b.com")]))
        providers = [("brave", "web", first), ("tavily", "web", second)]

        kw, winning, items, stats = await _search_keyword(
            "kw", providers, sem, gl="de", hl="de", num=10, round_errors={}
        )

        assert kw == "kw"
        assert winning == "brave"
        assert len(items) == 1
        first.search.assert_called_once()
        second.search.assert_not_called()
        assert stats["brave"]["result_count"] == 1

    @pytest.mark.asyncio
    async def test_falls_back_to_next_provider_on_error(self):
        sem = asyncio.Semaphore(5)
        first = AsyncMock(search=AsyncMock(side_effect=_http_error(402)))
        second = AsyncMock(search=AsyncMock(return_value=[_web_hit(link="https://b.com")]))
        providers = [("brave", "web", first), ("serpapi", "web", second)]

        kw, winning, items, stats = await _search_keyword(
            "kw", providers, sem, gl="de", hl="de", num=10, round_errors={}
        )

        assert winning == "serpapi"
        assert len(items) == 1
        # Quota error should have tripped the circuit breaker.
        assert _provider_blocked("brave") == "quota"
        assert stats["brave"]["result_count"] == 0

    @pytest.mark.asyncio
    async def test_empty_results_tries_next_provider(self):
        sem = asyncio.Semaphore(5)
        first = AsyncMock(search=AsyncMock(return_value=[]))
        second = AsyncMock(search=AsyncMock(return_value=[_web_hit(link="https://b.com")]))
        providers = [("brave", "web", first), ("exa", "web", second)]

        kw, winning, items, _ = await _search_keyword(
            "kw", providers, sem, gl="", hl="", num=10, round_errors={}
        )

        assert winning == "exa"
        assert len(items) == 1
        first.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_providers_fail_returns_no_winner(self):
        sem = asyncio.Semaphore(5)
        first = AsyncMock(search=AsyncMock(side_effect=_http_error(402)))
        second = AsyncMock(search=AsyncMock(side_effect=_http_error(500)))
        providers = [("brave", "web", first), ("tavily", "web", second)]

        kw, winning, items, stats = await _search_keyword(
            "kw", providers, sem, gl="", hl="", num=10, round_errors={}
        )

        assert winning is None
        assert items == []
        assert "error" in stats["brave"]
        assert "error" in stats["tavily"]


class TestSearchNode:
    @pytest.mark.asyncio
    async def test_empty_keywords_returns_early(self):
        state = _base_state(keywords=[])
        result = await search_node(state)
        assert result["current_stage"] == "search"

    @pytest.mark.asyncio
    async def test_no_provider_configured_reports_failure(self):
        state = _base_state(keywords=["k1"])
        with patch("agents.search_agent.get_settings", return_value=_settings()):
            result = await search_node(state)
        assert result["search_failed"] is True
        assert "No search provider" in result["search_error"]

    @pytest.mark.asyncio
    async def test_single_provider_serves_results(self):
        state = _base_state(keywords=["solar berlin"])
        brave_inst = AsyncMock()
        brave_inst.search = AsyncMock(return_value=[_web_hit(link="https://brave.com/x")])
        brave_inst.close = AsyncMock()

        s = _settings(brave_api_key="bk", search_provider_order="brave")
        with patch("agents.search_agent.get_settings", return_value=s), \
             patch("agents.search_agent.BraveSearchTool", return_value=brave_inst) as MockBrave:
            result = await search_node(state)

        assert result["search_failed"] is False
        assert len(result["search_results"]) == 1
        assert result["search_results"][0]["source"] == "brave"
        assert result["search_provider_status"]["brave"] == "ok"
        MockBrave.assert_called_once()
        brave_inst.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_quota_failure_then_next_provider(self):
        state = _base_state(keywords=["k1"])
        brave_inst = AsyncMock()
        brave_inst.search = AsyncMock(side_effect=_http_error(402))
        brave_inst.close = AsyncMock()
        serpapi_inst = AsyncMock()
        serpapi_inst.search = AsyncMock(return_value=[_web_hit(link="https://s.com/x")])
        serpapi_inst.close = AsyncMock()

        s = _settings(brave_api_key="bk", serpapi_api_key="sk", search_provider_order="brave,serpapi")
        with patch("agents.search_agent.get_settings", return_value=s), \
             patch("agents.search_agent.BraveSearchTool", return_value=brave_inst), \
             patch("agents.search_agent.SerpApiSearchTool", return_value=serpapi_inst):
            result = await search_node(state)

        assert result["search_failed"] is False
        assert len(result["search_results"]) == 1
        assert result["search_results"][0]["source"] == "serpapi"
        assert result["search_provider_status"]["brave"].startswith("error")
        assert result["search_provider_status"]["serpapi"] == "ok"

    @pytest.mark.asyncio
    async def test_maps_provider_normalizes_places(self):
        state = _base_state(keywords=["k1"])
        maps_inst = AsyncMock()
        maps_inst.search = AsyncMock(return_value=[
            {"title": "Solar Co", "website": "https://solar.com", "address": "Berlin",
             "phone_number": "+49 123", "place_id": "abc", "type": "Solar", "types": ["Solar"]}
        ])
        maps_inst.close = AsyncMock()

        s = _settings(serper_api_key="sk", search_provider_order="google_maps")
        with patch("agents.search_agent.get_settings", return_value=s), \
             patch("agents.search_agent.GoogleMapsSearchTool", return_value=maps_inst):
            result = await search_node(state)

        assert result["search_failed"] is False
        assert len(result["search_results"]) == 1
        assert result["search_results"][0]["source"] == "google_maps"
        assert result["search_results"][0]["maps_data"]["phoneNumber"] == "+49 123"
        assert result["seen_urls"] == ["url:https://solar.com"]

    @pytest.mark.asyncio
    async def test_all_providers_fail_sets_search_failed(self):
        state = _base_state(keywords=["k1"])
        brave_inst = AsyncMock()
        brave_inst.search = AsyncMock(side_effect=_http_error(500))
        brave_inst.close = AsyncMock()

        s = _settings(brave_api_key="bk", search_provider_order="brave")
        with patch("agents.search_agent.get_settings", return_value=s), \
             patch("agents.search_agent.BraveSearchTool", return_value=brave_inst):
            result = await search_node(state)

        assert result["search_failed"] is True
        assert "search_error" in result


class TestResultIdentityKey:
    def test_uses_url_when_present(self):
        assert _result_identity_key({"link": "https://example.com/a"}) == "url:https://example.com/a"

    def test_uses_place_id_without_url(self):
        key = _result_identity_key({"link": "", "maps_data": {"place_id": "PID-1"}})
        assert key == "place:pid-1"

    def test_falls_back_to_title_and_address(self):
        key = _result_identity_key({"title": "ACME", "maps_data": {"address": "Berlin"}})
        assert key == "maps:acme|berlin"


class TestIsChinaRegion:
    def test_china_english(self):
        assert _is_china_region(["China"]) is True

    def test_china_chinese(self):
        assert _is_china_region(["中国"]) is True

    def test_non_china(self):
        assert _is_china_region(["Germany", "Poland"]) is False
