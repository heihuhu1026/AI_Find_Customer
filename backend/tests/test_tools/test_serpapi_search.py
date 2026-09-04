"""Tests for tools/serpapi_search.py — mock SerpApi, verify parsing & request shape."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from tools.serpapi_search import SerpApiSearchTool

_FAKE_REQUEST = httpx.Request("GET", "https://serpapi.com/search.json")


def _make_settings(serpapi_api_key="serpapi-key-123"):
    return SimpleNamespace(serpapi_api_key=serpapi_api_key)


SERPAPI_RESPONSE = {
    "organic_results": [
        {"position": 1, "title": "SolarTech GmbH", "link": "https://solartech.de", "snippet": "Leading solar manufacturer"},
        {"position": 2, "title": "PV Distributor", "link": "https://pvdist.com", "snippet": "PV panels wholesale"},
    ]
}


class TestSerpApiSearchTool:
    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        tool = SerpApiSearchTool(settings=_make_settings())
        mock_resp = httpx.Response(200, json=SERPAPI_RESPONSE, request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_resp):
            results = await tool.search("solar inverter distributor")

        assert len(results) == 2
        assert results[0]["title"] == "SolarTech GmbH"
        assert results[0]["link"] == "https://solartech.de"
        assert results[0]["snippet"] == "Leading solar manufacturer"
        assert results[0]["position"] == 1
        await tool.close()

    @pytest.mark.asyncio
    async def test_search_sends_expected_params(self):
        tool = SerpApiSearchTool(settings=_make_settings("serpapi-key-abc"))
        captured = {}

        async def mock_get(url, **kwargs):
            captured.update(kwargs.get("params", {}))
            return httpx.Response(200, json={"organic_results": []}, request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "get", side_effect=mock_get):
            await tool.search("test query", num=15, gl="de", hl="de")

        assert captured["engine"] == "google"
        assert captured["q"] == "test query"
        assert captured["api_key"] == "serpapi-key-abc"
        assert captured["gl"] == "de"
        assert captured["hl"] == "de"
        assert captured["num"] == 15
        await tool.close()

    @pytest.mark.asyncio
    async def test_num_capped_at_20(self):
        tool = SerpApiSearchTool(settings=_make_settings())
        captured = {}

        async def mock_get(url, **kwargs):
            captured.update(kwargs.get("params", {}))
            return httpx.Response(200, json={"organic_results": []}, request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "get", side_effect=mock_get):
            await tool.search("q", num=50)

        assert captured["num"] == 20
        await tool.close()

    @pytest.mark.asyncio
    async def test_error_field_raises(self):
        # SerpApi often returns HTTP 200 with an "error" JSON field on quota issues.
        tool = SerpApiSearchTool(settings=_make_settings())
        mock_resp = httpx.Response(200, json={"error": "Your account does not have any searches left."}, request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(RuntimeError, match="searches left"):
                await tool.search("q")
        await tool.close()

    @pytest.mark.asyncio
    async def test_missing_key_raises(self):
        tool = SerpApiSearchTool(settings=_make_settings(serpapi_api_key=""))
        with pytest.raises(RuntimeError):
            await tool.search("q")
        await tool.close()
