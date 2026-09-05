"""Tests for the merchandising_cache module.

Covers normalise_query(), load_rules(), invalidate_rules(), and
get_matching_rules() including schedule filtering and hit_count
incrementing.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache as django_cache
from django.utils import timezone

from icv_search.merchandising_cache import (
    _get_cache,
    get_matching_rules,
    invalidate_rules,
    load_rules,
    normalise_query,
)
from icv_search.models.merchandising import QueryRedirect

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BASE = {
    "index_name": "products",
    "tenant_id": "",
    "query_pattern": "shoes",
    "match_type": "exact",
    "is_active": True,
    "priority": 0,
    "destination_url": "https://example.com/shoes/",
}


def _redirect(**kwargs) -> QueryRedirect:
    params = {**_BASE, **kwargs}
    return QueryRedirect.objects.create(**params)


@pytest.fixture(autouse=True)
def _no_cache(settings):
    """Disable merchandising rule caching for most tests.

    Individual tests that exercise caching behaviour override this by
    setting ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT to a positive value.
    """
    settings.ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT = 0


@pytest.fixture(autouse=True)
def _locmem_cache(settings):
    """Use an in-process LocMemCache and clear it between tests."""
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
    django_cache.clear()
    yield
    django_cache.clear()


# ---------------------------------------------------------------------------
# normalise_query()
# ---------------------------------------------------------------------------


class TestNormaliseQuery:
    """normalise_query() produces a canonical, cache-safe string."""

    def test_strips_and_lowers(self):
        assert normalise_query("  SHOES  ") == "shoes"

    def test_collapses_internal_whitespace(self):
        assert normalise_query("red    shoes") == "red shoes"

    def test_empty_string_returns_empty_string(self):
        assert normalise_query("") == ""

    def test_mixed_case_and_whitespace(self):
        assert normalise_query("  Running   SHOES ") == "running shoes"


# ---------------------------------------------------------------------------
# load_rules()
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLoadRules:
    """load_rules() returns the correct subset of active rules."""

    def test_returns_active_rules(self):
        active = _redirect(is_active=True)
        _redirect(is_active=False, query_pattern="boots")
        rules = load_rules(QueryRedirect, "products")
        pks = [r.pk for r in rules]
        assert active.pk in pks

    def test_inactive_rule_excluded(self):
        _redirect(is_active=False)
        rules = load_rules(QueryRedirect, "products")
        assert rules == []

    def test_filters_by_index_name(self):
        rule_a = _redirect(index_name="products")
        QueryRedirect.objects.create(**{**_BASE, "index_name": "articles", "query_pattern": "django"})
        rules = load_rules(QueryRedirect, "products")
        pks = [r.pk for r in rules]
        assert rule_a.pk in pks
        assert all(r.index_name == "products" for r in rules)

    def test_includes_global_and_tenant_specific_rules(self):
        """When tenant_id is given, both global (blank) and tenant rules are returned."""
        global_rule = _redirect(tenant_id="")
        tenant_rule = _redirect(tenant_id="acme", query_pattern="boots")
        rules = load_rules(QueryRedirect, "products", tenant_id="acme")
        pks = [r.pk for r in rules]
        assert global_rule.pk in pks
        assert tenant_rule.pk in pks

    def test_does_not_include_other_tenants_rules(self):
        _redirect(tenant_id="other", query_pattern="boots")
        rules = load_rules(QueryRedirect, "products", tenant_id="acme")
        assert rules == []

    def test_caching_second_call_does_not_re_query_db(self, settings, monkeypatch):
        """With a positive cache timeout, the second load_rules call is served
        from cache without executing a new database query."""
        settings.ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT = 60
        rule = _redirect()

        # First call — populates the cache.
        rules_first = load_rules(QueryRedirect, "products")
        assert any(r.pk == rule.pk for r in rules_first)

        # Patch the queryset filter so any DB call would raise AssertionError.
        original_filter = QueryRedirect.objects.filter

        def no_db_filter(*args, **kwargs):
            raise AssertionError("DB should not be queried on the second call — cache miss?")

        monkeypatch.setattr(QueryRedirect.objects, "filter", no_db_filter)
        try:
            # Second call must be served from cache, not the DB.
            rules_second = load_rules(QueryRedirect, "products")
        finally:
            monkeypatch.setattr(QueryRedirect.objects, "filter", original_filter)

        assert any(r.pk == rule.pk for r in rules_second)


# ---------------------------------------------------------------------------
# invalidate_rules()
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInvalidateRules:
    """invalidate_rules() clears the relevant cache entry."""

    def test_invalidate_clears_cache(self, settings):
        settings.ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT = 60
        rule = _redirect()

        # Populate the cache
        load_rules(QueryRedirect, "products")

        # Delete DB row so a fresh load returns nothing
        QueryRedirect.objects.all().delete()

        # Invalidate — next load must query the DB
        invalidate_rules("QueryRedirect", "products")
        rules = load_rules(QueryRedirect, "products")
        assert rules == []

        # Confirm the original rule is gone from DB
        assert not QueryRedirect.objects.filter(pk=rule.pk).exists()


# ---------------------------------------------------------------------------
# get_matching_rules()
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGetMatchingRules:
    """get_matching_rules() combines loading, schedule checks, query matching, and hit_count."""

    def test_returns_matching_rule(self):
        rule = _redirect(query_pattern="shoes", match_type="exact")
        results = get_matching_rules(QueryRedirect, "QueryRedirect", "products", "shoes")
        assert any(r.pk == rule.pk for r in results)

    def test_non_matching_query_returns_empty_list(self):
        _redirect(query_pattern="boots", match_type="exact")
        results = get_matching_rules(QueryRedirect, "QueryRedirect", "products", "shoes")
        assert results == []

    def test_single_winner_returns_one_result(self):
        _redirect(query_pattern="shoes", priority=5, destination_url="https://example.com/a/")
        _redirect(query_pattern="shoes", priority=1, destination_url="https://example.com/b/")
        results = get_matching_rules(QueryRedirect, "QueryRedirect", "products", "shoes", single_winner=True)
        assert len(results) == 1

    def test_increments_hit_count_on_match(self):
        rule = _redirect(query_pattern="shoes", hit_count=0)
        get_matching_rules(QueryRedirect, "QueryRedirect", "products", "shoes")
        rule.refresh_from_db()
        assert rule.hit_count == 1

    def test_does_not_increment_hit_count_on_no_match(self):
        rule = _redirect(query_pattern="boots", hit_count=0)
        get_matching_rules(QueryRedirect, "QueryRedirect", "products", "shoes")
        rule.refresh_from_db()
        assert rule.hit_count == 0

    def test_skips_rule_outside_schedule(self):
        """A rule whose starts_at is in the future must not be returned."""
        _redirect(
            query_pattern="shoes",
            starts_at=timezone.now() + timedelta(hours=1),
        )
        results = get_matching_rules(QueryRedirect, "QueryRedirect", "products", "shoes")
        assert results == []

    def test_normalises_query_before_matching(self):
        """get_matching_rules normalises the query so 'SHOES' matches pattern 'shoes'."""
        rule = _redirect(query_pattern="shoes", match_type="exact")
        results = get_matching_rules(QueryRedirect, "QueryRedirect", "products", "  SHOES  ")
        assert any(r.pk == rule.pk for r in results)


# ---------------------------------------------------------------------------
# _get_cache() honours ICV_CACHES_ALIAS (ADR-037)
# ---------------------------------------------------------------------------


class TestGetCacheHonoursCachesAlias:
    """_get_cache() resolves through ICV_CACHES_ALIAS, not the default alias."""

    def test_writes_land_in_the_aliased_cache(self, settings):
        """Pointing ICV_CACHES_ALIAS at a second configured cache moves
        merchandising rule caching there, leaving the default alias untouched."""
        settings.CACHES = {
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "default-alias",
            },
            "fleet": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "fleet-alias",
            },
        }
        settings.ICV_CACHES_ALIAS = "fleet"

        cache = _get_cache()
        cache.set("adr037-probe", "value")

        assert cache.get("adr037-probe") == "value"
        assert django_cache.get("adr037-probe") is None

    def test_defaults_to_the_default_alias_when_unset(self, settings):
        """With ICV_CACHES_ALIAS unset, _get_cache() still resolves to the
        default alias (unchanged behaviour)."""
        assert not hasattr(settings, "ICV_CACHES_ALIAS")

        cache = _get_cache()
        cache.set("adr037-probe-default", "value")

        assert django_cache.get("adr037-probe-default") == "value"
