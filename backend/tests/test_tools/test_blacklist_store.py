"""Tests for tools/blacklist_store.py — uniqueness rules, upsert, filtering."""

import pytest

from tools.blacklist_store import (
    BlacklistStore,
    is_valid_domain,
    load_blocked_domains,
    normalize_customer_name,
)


@pytest.fixture
def store(tmp_path):
    s = BlacklistStore(str(tmp_path / "blacklist.db"))
    s.init_db()
    yield s
    s.close()


class TestNormalizeCustomerName:
    def test_lowercase_and_collapse(self):
        assert normalize_customer_name("  Acme   GmbH ") == "acme"

    def test_strips_common_suffixes(self):
        assert normalize_customer_name("Beta Ltd") == "beta"
        assert normalize_customer_name("Gamma Co., Ltd") == "gamma"
        assert normalize_customer_name("Delta 有限公司") == "delta"

    def test_punctuation_stripped(self):
        assert normalize_customer_name("Acme (Europe), Inc.") == "acme europe"


class TestIsValidDomain:
    def test_accepts_bare_and_url(self):
        assert is_valid_domain("acme.com") is True
        assert is_valid_domain("www.acme.com") is True
        assert is_valid_domain("https://acme.com/") is True
        assert is_valid_domain("sub.domain.co.uk") is True

    def test_rejects_invalid(self):
        assert is_valid_domain("") is False
        assert is_valid_domain("not a domain") is False
        assert is_valid_domain("acme") is False  # no TLD
        assert is_valid_domain("acme_com") is False


class TestUpsertUniqueness:
    def test_upsert_creates(self, store):
        res = store.upsert("Acme GmbH", "https://www.acme.com/")
        assert res["status"] == "created"
        assert res["entry"]["domain"] == "acme.com"
        assert res["entry"]["customer_name"] == "Acme GmbH"

    def test_upsert_same_domain_updates(self, store):
        store.upsert("Acme GmbH", "acme.com")
        res = store.upsert("Acme New Name", "https://acme.com/")
        assert res["status"] == "updated"
        assert res["entry"]["customer_name"] == "Acme New Name"
        assert store.count() == 1

    def test_upsert_rejects_invalid_domain(self, store):
        with pytest.raises(ValueError):
            store.upsert("Bad", "not a domain")


class TestCheck:
    def test_check_valid_free(self, store):
        res = store.check(domain="acme.com", customer_name="Acme GmbH")
        assert res["valid"] is True
        assert res["exists_domain"] is False
        assert res["suspected_name"] is False

    def test_check_existing_domain(self, store):
        store.upsert("Acme GmbH", "acme.com")
        res = store.check(domain="www.acme.com", customer_name="Acme GmbH")
        assert res["exists_domain"] is True
        assert res["exists_id"] is not None

    def test_check_suspected_name(self, store):
        store.upsert("Acme GmbH", "acme.com")
        res = store.check(domain="acme-deutschland.de", customer_name="Acme GmbH")
        assert res["suspected_name"] is True
        assert res["exists_domain"] is False

    def test_check_invalid(self, store):
        res = store.check(domain="nope", customer_name="")
        assert res["valid"] is False
        assert "invalid domain" in res["error"]

    def test_check_missing_domain(self, store):
        res = store.check(domain="", customer_name="Acme")
        assert res["valid"] is False
        assert res["error"] == "domain is required"


class TestIsBlocked:
    def test_is_blocked(self, store):
        store.upsert("Acme", "acme.com")
        assert store.is_blocked("https://www.acme.com/page") is True
        assert store.is_blocked("acme.com") is True
        assert store.is_blocked("other.com") is False

    def test_blocked_domains(self, store):
        store.upsert("A", "a.com")
        store.upsert("B", "b.com")
        assert store.blocked_domains() == {"a.com", "b.com"}


class TestListAndBatch:
    def test_list_filters_and_pagination(self, store):
        store.upsert("Alpha", "alpha.com", tags="competitor", source="import")
        store.upsert("Beta", "beta.com", tags="partner", source="manual")
        store.upsert("Gamma", "gamma.com", tags="competitor,big")

        assert store.list()["total"] == 3
        assert store.list(q="alp")["total"] == 1
        assert store.list(tag="competitor")["total"] == 2
        assert store.list(source="manual")["total"] == 2  # Beta + Gamma(default)
        assert store.list(source="import")["total"] == 1
        assert store.list(page=1, page_size=2)["items"].__len__() == 2

    def test_delete_and_delete_many(self, store):
        store.upsert("A", "a.com")
        store.upsert("B", "b.com")
        store.upsert("C", "c.com")
        ids = [e["id"] for e in store.list(page_size=100)["items"]]
        assert store.delete_many(ids[:2]) == 2
        assert store.count() == 1
        assert store.delete(ids[2]) is True
        assert store.count() == 0

    def test_add_tags_dedup(self, store):
        e = store.upsert("A", "a.com", tags="x")
        store.add_tags([e["id"]], "y")
        store.add_tags([e["id"]], "y")
        entry = store.get(e["id"])
        assert entry["tags"] == "x,y"


class TestLoadBlockedDomains:
    def test_returns_empty_when_disabled(self, tmp_path):
        from types import SimpleNamespace

        s = SimpleNamespace(blacklist_filter_enabled=False, blacklist_db_path=str(tmp_path / "b.db"))
        assert load_blocked_domains(s) == set()

    def test_returns_domains_when_enabled(self, tmp_path):
        from types import SimpleNamespace

        s = SimpleNamespace(blacklist_filter_enabled=True, blacklist_db_path=str(tmp_path / "b.db"))
        st = BlacklistStore(s.blacklist_db_path)
        st.init_db()
        st.upsert("A", "a.com")
        st.close()
        assert load_blocked_domains(s) == {"a.com"}
