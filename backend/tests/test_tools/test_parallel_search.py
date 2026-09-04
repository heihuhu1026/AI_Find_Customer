"""Tests for tools/parallel_search.py — mock Parallel API, verify parsing & request shape."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from tools.parallel_search import ParallelSearchTool

_FAKE_REQUEST = httpx.Request("POST", "https://api.parallel.ai/v1/search")


def _make_settings(parallel_api_key="parallel-key-123"):
    return SimpleNamespace(parallel_api_key=parallel_api_key)


PARALLEL_RESPONSE = {
    "search_id": "search_1",
    "results": [
        {"url": "https://solartech.de", "title": "SolarTech GmbH", "excerpts": ["Leading solar manufacturer"]},
        {"url": "https://pvdist.com", "title": "PV Distributor", "excerpts": []},
    ],
}


class TestParallelSearchTool:
    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        tool = ParallelSearchTool(settings=_make_settings())
        mock_resp = httpx.Response(200, json=PARALLEL_RESPONSE, request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            results = await tool.search("solar inverter distributor")

        assert len(results) == 2
        assert results[0]["title"] == "SolarTech GmbH"
        assert results[0]["link"] == "https://solartech.de"
        assert results[0]["snippet"] == "Leading solar manufacturer"
        assert results[0]["position"] == 1
        assert results[1]["snippet"] == ""  # missing excerpts => empty snippet
        await tool.close()

    @pytest.mark.asyncio
    async def test_search_sends_expected_body_and_header(self):
        tool = ParallelSearchTool(settings=_make_settings("parallel-key-abc"))
        captured = {}

        async def mock_post(url, **kwargs):
            captured.update(headers=kwargs.get("headers", {}))
            captured.update(json=kwargs.get("json", {}))
            return httpx.Response(200, json={"results": []}, request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "post", side_effect=mock_post):
            await tool.search("test query", num=3)

        assert captured["headers"]["x-api-key"] == "parallel-key-abc"
        assert captured["json"]["search_queries"] == ["test query"]
        assert "test query" in captured["json"]["objective"]
        assert captured["json"]["mode"] == "fast"
        await tool.close()

    @pytest.mark.asyncio
    async def test_num_limits_results(self):
        tool = ParallelSearchTool(settings=_make_settings())
        mock_resp = httpx.Response(200, json={"results": PARALLEL_RESPONSE["results"] * 5}, request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            results = await tool.search("q", num=3)

        assert len(results) == 3
        await tool.close()

    @pytest.mark.asyncio
    async def test_missing_key_raises(self):
        tool = ParallelSearchTool(settings=_make_settings(parallel_api_key=""))
        with pytest.raises(RuntimeError):
            await tool.search("q")
        await tool.close()

    @pytest.mark.asyncio
    async def test_http_402_raises(self):
        # 402 = insufficient credit; must surface as HTTPStatusError so upstream
        # classifies it as quota and switches to the next provider.
        tool = ParallelSearchTool(settings=_make_settings())
        mock_resp = httpx.Response(402, json={"error": {"message": "Insufficient credit"}}, request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(httpx.HTTPStatusError):
                await tool.search("q")
        await tool.close()
