"""Tests for api/blacklist_routes.py — CRUD, batch, import/export, check."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient, ASGITransport

from api.app import create_app


@pytest.fixture
def blacklist_db(tmp_path):
    return str(tmp_path / "blacklist.db")


@pytest.fixture
def app(blacklist_db):
    settings = SimpleNamespace(
        blacklist_db_path=blacklist_db,
        blacklist_filter_enabled=True,
        api_access_token="",
        settings_api_enabled=True,
        cors_origins=["http://localhost:3000"],
    )
    with patch("api.blacklist_routes.get_settings", return_value=settings), \
         patch("api.security.get_settings", return_value=settings), \
         patch("config.settings.get_settings", return_value=settings):
        yield create_app()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_create_and_list(client):
    resp = await client.post("/api/v1/blacklist", json={"customer_name": "Acme GmbH", "domain": "acme.com"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "created"
    assert body["domain"] == "acme.com"

    resp = await client.get("/api/v1/blacklist")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["customer_name"] == "Acme GmbH"


@pytest.mark.asyncio
async def test_create_duplicate_domain_updates(client):
    await client.post("/api/v1/blacklist", json={"customer_name": "Acme", "domain": "acme.com"})
    resp = await client.post("/api/v1/blacklist", json={"customer_name": "Acme 2", "domain": "www.acme.com"})
    assert resp.json()["status"] == "updated"

    data = (await client.get("/api/v1/blacklist")).json()
    assert data["total"] == 1
    assert data["items"][0]["customer_name"] == "Acme 2"


@pytest.mark.asyncio
async def test_create_invalid_domain(client):
    resp = await client.post("/api/v1/blacklist", json={"customer_name": "X", "domain": "nope"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_check(client):
    await client.post("/api/v1/blacklist", json={"customer_name": "Acme", "domain": "acme.com"})
    resp = await client.get("/api/v1/blacklist/check", params={"domain": "www.acme.com", "name": "Acme"})
    data = resp.json()
    assert data["valid"] is True
    assert data["exists_domain"] is True


@pytest.mark.asyncio
async def test_update_and_delete(client):
    r = await client.post("/api/v1/blacklist", json={"customer_name": "A", "domain": "a.com"})
    eid = r.json()["id"]

    resp = await client.put(f"/api/v1/blacklist/{eid}", json={"note": "已合作"})
    assert resp.json()["note"] == "已合作"

    resp = await client.delete(f"/api/v1/blacklist/{eid}")
    assert resp.status_code == 204
    assert (await client.get("/api/v1/blacklist")).json()["total"] == 0


@pytest.mark.asyncio
async def test_batch_delete(client):
    ids = []
    for name in ["A", "B", "C"]:
        r = await client.post("/api/v1/blacklist", json={"customer_name": name, "domain": f"{name.lower()}.com"})
        ids.append(r.json()["id"])
    resp = await client.post("/api/v1/blacklist/batch", json={"action": "delete", "ids": ids[:2]})
    assert resp.json()["affected"] == 2
    assert (await client.get("/api/v1/blacklist")).json()["total"] == 1


@pytest.mark.asyncio
async def test_batch_tag(client):
    r = await client.post("/api/v1/blacklist", json={"customer_name": "A", "domain": "a.com", "tags": "x"})
    eid = r.json()["id"]
    resp = await client.post("/api/v1/blacklist/batch", json={"action": "tag", "ids": [eid], "tag": "competitor"})
    assert resp.json()["affected"] == 1
    data = (await client.get("/api/v1/blacklist")).json()
    assert "competitor" in data["items"][0]["tags"]


@pytest.mark.asyncio
async def test_import_csv(client):
    csv_content = "customer_name,domain,note,tags\nAcme GmbH,acme.com,已合作,competitor\nBadDomain,bad,,\n,beta.com,,\n"
    resp = await client.post(
        "/api/v1/blacklist/import",
        files={"file": ("blacklist.csv", csv_content.encode("utf-8"), "text/csv")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["imported"] == 2  # acme + beta (empty name -> domain fallback)
    assert data["skipped"] == 1   # bad domain
    assert len(data["errors"]) == 1

    listed = (await client.get("/api/v1/blacklist")).json()
    assert listed["total"] == 2


@pytest.mark.asyncio
async def test_export_csv(client):
    await client.post("/api/v1/blacklist", json={"customer_name": "Acme", "domain": "acme.com", "note": "n", "tags": "t"})
    resp = await client.get("/api/v1/blacklist/export")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "acme.com" in resp.text


@pytest.mark.asyncio
async def test_template_csv(client):
    resp = await client.get("/api/v1/blacklist/template")
    assert resp.status_code == 200
    assert "customer_name" in resp.text
