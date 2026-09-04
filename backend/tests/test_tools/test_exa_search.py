"""Tests for tools/exa_search.py — mock Exa API, verify parsing & request shape."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from tools.exa_search import ExaSearchTool

_FAKE_REQUEST = httpx.Request("POST", "https://api.exa.ai/search")


def _make_settings(exa_api_key="exa-key-123"):
    return SimpleNamespace(exa_api_key=exa_api_key)


EXA_RESPONSE = {
    "results": [
        {"id": "https://solartech.de", "title": "SolarTech GmbH", "url": "https://solartech.de", "text": "Leading solar manufacturer"},
        {"id": "https://pvdist.com", "title": "PV Distributor", "url": "https://pvdist.com"},
    ]
}


class TestExaSearchTool:
    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        tool = ExaSearchTool(settings=_make_settings())
        mock_resp = httpx.Response(200, json=EXA_RESPONSE, request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            results = await tool.search("solar inverter distributor")

        assert len(results) == 2
        assert results[0]["title"] == "SolarTech GmbH"
        assert results[0]["link"] == "https://solartech.de"
        assert results[0]["snippet"] == "Leading solar manufacturer"
        assert results[0]["position"] == 1
        await tool.close()

    @pytest.mark.asyncio
    async def test_search_sends_expected_body_and_header(self):
        tool = ExaSearchTool(settings=_make_settings("exa-key-abc"))
        captured = {}

        async def mock_post(url, **kwargs):
            captured.update(headers=kwargs.get("headers", {}))
            captured.update(json=kwargs.get("json", {}))
            return httpx.Response(200, json={"results": []}, request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "post", side_effect=mock_post):
            await tool.search("test query", num=5)

        assert captured["headers"]["x-api-key"] == "exa-key-abc"
        assert captured["json"]["query"] == "test query"
        assert captured["json"]["numResults"] == 5
        await tool.close()

    @pytest.mark.asyncio
    async def test_num_capped_at_10(self):
        tool = ExaSearchTool(settings=_make_settings())
        captured = {}

        async def mock_post(url, **kwargs):
            captured.update(kwargs.get("json", {}))
            return httpx.Response(200, json={"results": []}, request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "post", side_effect=mock_post):
            await tool.search("q", num=50)

        assert captured["numResults"] == 10
        await tool.close()

    @pytest.mark.asyncio
    async def test_empty_results(self):
        tool = ExaSearchTool(settings=_make_settings())
        mock_resp = httpx.Response(200, json={"results": []}, request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            results = await tool.search("obscure")

        assert results == []
        await tool.close()

    @pytest.mark.asyncio
    async def test_missing_key_raises(self):
        tool = ExaSearchTool(settings=_make_settings(exa_api_key=""))
        with pytest.raises(RuntimeError):
            await tool.search("q")
        await tool.close()

    @pytest.mark.asyncio
    async def test_http_error_raises(self):
        tool = ExaSearchTool(settings=_make_settings())
        mock_resp = httpx.Response(429, json={"error": "rate limited"}, request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(httpx.HTTPStatusError):
                await tool.search("q")
        await tool.close()
