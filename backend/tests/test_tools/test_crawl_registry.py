"""Tests for the unified cross-hunt crawl de-duplication registry."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

from config.settings import Settings
from tools.crawl_registry import (
    CrawlRegistry,
    check_target_site_duplicate,
    normalize_domain,
    normalize_url,
)
from tools.jina_reader import JinaReaderTool


def test_normalize_helpers():
    assert normalize_domain("https://www.Example.com:8080/path") == "example.com"
    assert normalize_url("https://www.example.com/path/") == "example.com/path"
    assert normalize_url("https://example.com/") == "example.com/"
    # query is part of the key, fragments are not
    assert normalize_url("https://example.com/p?x=1#frag") == "example.com/p?x=1"


def test_record_and_is_url_crawled(tmp_path):
    reg = CrawlRegistry(str(tmp_path / "r.db"))
    reg.init_db()
    assert not reg.is_url_crawled("https://example.com/a")

    reg.record("https://example.com/a", "hunt1")
    # normalization makes www./trailing-slash variants match
    assert reg.is_url_crawled("https://www.example.com/a/")
    # crawled by another hunt -> still considered crawled
    assert reg.is_url_crawled("https://example.com/a", exclude_hunt_id="other")
    # crawled by the SAME hunt -> not a "duplicate by another task"
    assert not reg.is_url_crawled("https://example.com/a", exclude_hunt_id="hunt1")


def test_domain_crawled_by_cross_hunt(tmp_path):
    reg = CrawlRegistry(str(tmp_path / "r.db"))
    reg.init_db()
    reg.record("https://example.com/about", "hunt1")

    assert reg.domain_crawled_by("example.com") == "hunt1"
    # same hunt excluded
    assert reg.domain_crawled_by("example.com", exclude_hunt_id="hunt1") == ""
    assert reg.domain_crawled_by("other.com") == ""


def test_ttl_expiry(tmp_path):
    old = "2000-01-01T00:00:00Z"
    # with a 1s TTL, the old crawl is expired
    reg = CrawlRegistry(str(tmp_path / "r1.db"), ttl_seconds=1)
    reg.init_db()
    reg.record("https://example.com/a", "h1", crawled_at=old)
    assert not reg.is_url_crawled("https://example.com/a")

    # with TTL disabled (0), the old crawl is still fresh
    reg2 = CrawlRegistry(str(tmp_path / "r2.db"), ttl_seconds=0)
    reg2.init_db()
    reg2.record("https://example.com/a", "h1", crawled_at=old)
    assert reg2.is_url_crawled("https://example.com/a")


def test_seed_from_hunts(tmp_path):
    hunts_dir = tmp_path / "hunts"
    hunts_dir.mkdir()
    (hunts_dir / "h1.json").write_text(
        json.dumps({"hunt_id": "h1", "payload": {"website_url": "https://seed.example.com"}})
    )
    reg = CrawlRegistry(str(tmp_path / "r.db"))
    reg.init_db()
    n = reg.seed_from_hunts(str(hunts_dir))
    assert n == 1
    assert reg.domain_crawled_by("seed.example.com") == "h1"


def test_check_target_site_duplicate(tmp_path):
    settings = Settings(
        crawl_dedup_enabled=True,
        crawl_registry_db_path=str(tmp_path / "r.db"),
        crawl_dedup_ttl_seconds=999999,
        hunts_dir=str(tmp_path / "empty"),
    )
    reg = CrawlRegistry(settings.crawl_registry_db_path, ttl_seconds=settings.crawl_dedup_ttl_seconds)
    reg.init_db()
    reg.record("https://target.example.com", "hX")

    res = check_target_site_duplicate("https://target.example.com", settings=settings)
    assert res["duplicate"] is True
    assert res["previous_hunt_id"] == "hX"
    assert res["domain"] == "target.example.com"

    res2 = check_target_site_duplicate("https://fresh.example.com", settings=settings)
    assert res2["duplicate"] is False

    # disabled -> never flagged
    settings_off = Settings(
        crawl_dedup_enabled=False,
        crawl_registry_db_path=str(tmp_path / "r2.db"),
        hunts_dir=str(tmp_path / "empty"),
    )
    res3 = check_target_site_duplicate("https://target.example.com", settings=settings_off)
    assert res3["duplicate"] is False


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


def test_jina_reader_skips_already_crawled_domain(tmp_path):
    reg = CrawlRegistry(str(tmp_path / "r.db"))
    reg.init_db()
    # Pre-record the domain as crawled by a *different* hunt.
    reg.record("https://company.example.com/", "huntA")

    fake = AsyncMock()
    fake.get = AsyncMock(return_value=_FakeResp("## markdown content"))

    tool = JinaReaderTool(hunt_id="huntB", crawl_registry=reg)
    tool._client = fake  # bypass real httpx client

    # Already-crawled company site -> skipped, no network call.
    out = asyncio.run(tool.read("https://company.example.com/about"))
    assert out == ""
    fake.get.assert_not_called()

    # Fresh domain -> fetched and recorded.
    out2 = asyncio.run(tool.read("https://fresh.example.com/"))
    assert "markdown" in out2
    fake.get.assert_called_once()
    # Recorded by huntB (no exclusion) and skipped if another hunt tries it.
    assert reg.domain_crawled_by("fresh.example.com") == "huntB"
    assert reg.domain_crawled_by("fresh.example.com", exclude_hunt_id="huntC") == "huntB"
