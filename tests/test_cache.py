"""Tests for ICVSearchCache and cache integration with search()."""

from __future__ import annotations

import pytest
from django.core.cache import cache as django_cache

from icv_search.cache import ICVSearchCache
from icv_search.types import SearchResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DUMMY_RESULT = SearchResult(
    hits=[{"id": "1", "title": "Django Testing"}],
    query="Django",
    processing_time_ms=5,
    estimated_total_hits=1,
    offset=0,
    limit=20,
    raw={
        "hits": [{"id": "1", "title": "Django Testing"}],
        "query": "Django",
        "processingTimeMs": 5,
        "estimatedTotalHits": 1,
        "offset": 0,
        "limit": 20,
    },
)


@pytest.fixture(autouse=True)
def _use_locmem_cache(settings):
    """Use LocMemCache for all cache tests and clear between tests."""
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
    django_cache.clear()
    yield
    django_cache.clear()


# ---------------------------------------------------------------------------
# ICVSearchCache.make_cache_key() tests
# ---------------------------------------------------------------------------


class TestICVSearchCacheMakeKey:
    """make_cache_key() generates stable, deterministic keys."""

    def test_same_params_same_key(self):
        cache = ICVSearchCache()
        k1 = cache.make_cache_key("products", "shoes", limit=10)
        k2 = cache.make_cache_key("products", "shoes", limit=10)
        assert k1 == k2

    def test_different_query_different_key(self):
        cache = ICVSearchCache()
        k1 = cache.make_cache_key("products", "shoes")
        k2 = cache.make_cache_key("products", "boots")
        assert k1 != k2

    def test_different_index_different_key(self):
        cache = ICVSearchCache()
        k1 = cache.make_cache_key("products", "shoes")
        k2 = cache.make_cache_key("articles", "shoes")
        assert k1 != k2

    def test_same_logical_index_with_different_engine_uids_has_different_keys(self):
        """Tenant-prefixed indexes with a shared name must not share cached hits."""
        cache = ICVSearchCache()
        first = cache.make_cache_key("products", "shoes", engine_uid="tenant_a_products")
        second = cache.make_cache_key("products", "shoes", engine_uid="tenant_b_products")
        assert first != second

    def test_different_params_different_key(self):
        cache = ICVSearchCache()
        k1 = cache.make_cache_key("products", "shoes", limit=5)
        k2 = cache.make_cache_key("products", "shoes", limit=10)
        assert k1 != k2

    def test_param_order_does_not_affect_key(self):
        """Keys must be deterministic regardless of **kwargs iteration order."""
        cache = ICVSearchCache()
        k1 = cache.make_cache_key("products", "shoes", limit=10, offset=0)
        k2 = cache.make_cache_key("products", "shoes", offset=0, limit=10)
        assert k1 == k2

    def test_key_prefixed_with_custom_prefix(self):
        cache = ICVSearchCache(prefix="myapp_search")
        key = cache.make_cache_key("products", "shoes")
        assert key.startswith("myapp_search:")

    def test_key_is_string(self):
        cache = ICVSearchCache()
        key = cache.make_cache_key("products", "shoes")
        assert isinstance(key, str)

    def test_default_prefix_is_icv_search(self):
        cache = ICVSearchCache()
        key = cache.make_cache_key("products", "shoes")
        assert key.startswith("icv_search:")


# ---------------------------------------------------------------------------
# ICVSearchCache get / set round-trip
# ---------------------------------------------------------------------------


class TestICVSearchCacheGetSet:
    """get() / set() round-trip behaviour."""

    def test_cache_miss_returns_none(self):
        cache = ICVSearchCache()
        result = cache.get("products", "shoes")
        assert result is None

    def test_set_then_get_returns_result(self):
        cache = ICVSearchCache()
        cache.set("products", "Django", _DUMMY_RESULT)
        retrieved = cache.get("products", "Django")
        assert retrieved is not None
        assert isinstance(retrieved, SearchResult)

    def test_retrieved_result_has_correct_hits(self):
        cache = ICVSearchCache()
        cache.set("products", "Django", _DUMMY_RESULT)
        retrieved = cache.get("products", "Django")
        assert retrieved.hits == _DUMMY_RESULT.hits

    def test_retrieved_result_has_correct_query(self):
        cache = ICVSearchCache()
        cache.set("products", "Django", _DUMMY_RESULT)
        retrieved = cache.get("products", "Django")
        assert retrieved.query == "Django"

    def test_params_are_part_of_cache_key(self):
        cache = ICVSearchCache()
        cache.set("products", "shoes", _DUMMY_RESULT, limit=5)
        # Different params → different key → cache miss
        assert cache.get("products", "shoes", limit=10) is None

    def test_different_queries_stored_separately(self):
        cache = ICVSearchCache()
        result_a = SearchResult(
            hits=[{"id": "1"}],
            query="a",
            processing_time_ms=1,
            estimated_total_hits=1,
            offset=0,
            limit=20,
            raw={
                "hits": [{"id": "1"}],
                "query": "a",
                "processingTimeMs": 1,
                "estimatedTotalHits": 1,
                "offset": 0,
                "limit": 20,
            },
        )
        result_b = SearchResult(
            hits=[{"id": "2"}],
            query="b",
            processing_time_ms=1,
            estimated_total_hits=1,
            offset=0,
            limit=20,
            raw={
                "hits": [{"id": "2"}],
                "query": "b",
                "processingTimeMs": 1,
                "estimatedTotalHits": 1,
                "offset": 0,
                "limit": 20,
            },
        )
        cache.set("articles", "a", result_a)
        cache.set("articles", "b", result_b)
        assert cache.get("articles", "a").hits == [{"id": "1"}]
        assert cache.get("articles", "b").hits == [{"id": "2"}]


# ---------------------------------------------------------------------------
# ICVSearchCache.invalidate() tests
# ---------------------------------------------------------------------------


class TestICVSearchCacheInvalidate:
    """invalidate() clears all entries for an index."""

    def test_invalidate_clears_stored_entry(self):
        cache = ICVSearchCache()
        cache.set("products", "shoes", _DUMMY_RESULT)
        cache.invalidate("products")
        assert cache.get("products", "shoes") is None

    def test_invalidate_clears_multiple_entries(self):
        cache = ICVSearchCache()
        cache.set("products", "shoes", _DUMMY_RESULT)
        cache.set("products", "boots", _DUMMY_RESULT, limit=5)
        cache.invalidate("products")
        assert cache.get("products", "shoes") is None
        assert cache.get("products", "boots", limit=5) is None

    def test_invalidate_does_not_affect_other_indexes(self):
        cache = ICVSearchCache()
        cache.set("products", "shoes", _DUMMY_RESULT)
        cache.set("articles", "Python", _DUMMY_RESULT)
        cache.invalidate("products")
        assert cache.get("articles", "Python") is not None

    def test_invalidate_on_empty_index_is_safe(self):
        """Calling invalidate for an index with no cached entries must not raise."""
        cache = ICVSearchCache()
        cache.invalidate("nonexistent_index")  # must not raise


# ---------------------------------------------------------------------------
# Settings-driven behaviour
# ---------------------------------------------------------------------------


class TestICVSearchCacheSettings:
    """Cache reads settings at call time (pytest settings fixture compatible)."""

    def test_timeout_from_settings(self, settings):
        settings.ICV_SEARCH_CACHE_TIMEOUT = 120
        cache = ICVSearchCache()
        assert cache.timeout == 120

    def test_cache_alias_from_settings(self, settings):
        settings.ICV_SEARCH_CACHE_ALIAS = "default"
        cache = ICVSearchCache()
        assert cache.cache_alias == "default"

    def test_explicit_timeout_overrides_settings(self, settings):
        settings.ICV_SEARCH_CACHE_TIMEOUT = 120
        cache = ICVSearchCache(timeout=300)
        assert cache.timeout == 300

    def test_explicit_alias_overrides_settings(self, settings):
        settings.ICV_SEARCH_CACHE_ALIAS = "search"
        cache = ICVSearchCache(cache_alias="default")
        assert cache.cache_alias == "default"

    def test_cache_disabled_by_default(self, settings):
        """ICV_SEARCH_CACHE_ENABLED defaults to False."""
        if hasattr(settings, "ICV_SEARCH_CACHE_ENABLED"):
            del settings.ICV_SEARCH_CACHE_ENABLED
        from icv_search.services.search import _get_cache

        assert _get_cache() is None

    def test_cache_enabled_returns_instance(self, settings):
        settings.ICV_SEARCH_CACHE_ENABLED = True
        from icv_search.services.search import _get_cache

        assert isinstance(_get_cache(), ICVSearchCache)


# ---------------------------------------------------------------------------
# Integration with search() service function
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCacheServiceIntegration:
    """search() service uses the cache when ICV_SEARCH_CACHE_ENABLED=True."""

    @pytest.fixture(autouse=True)
    def _setup(self, settings):
        from icv_search.backends import reset_search_backend
        from icv_search.backends.dummy import DummyBackend

        settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
        settings.ICV_SEARCH_AUTO_SYNC = False
        settings.ICV_SEARCH_CACHE_ENABLED = False
        reset_search_backend()
        DummyBackend.reset()
        django_cache.clear()
        yield
        DummyBackend.reset()
        reset_search_backend()
        django_cache.clear()

    def test_cache_disabled_does_not_cache(self, settings):
        """When cache is disabled, search always hits the backend."""
        from icv_search.backends import get_search_backend
        from icv_search.services import create_index, search

        settings.ICV_SEARCH_CACHE_ENABLED = False

        index = create_index("articles")
        backend = get_search_backend()
        original_search = backend.search
        call_count = [0]

        def counting_search(uid, query, **params):
            call_count[0] += 1
            return original_search(uid, query, **params)

        backend.search = counting_search

        search(index, "test")
        search(index, "test")
        # Both calls should have hit the backend
        assert call_count[0] == 2

        backend.search = original_search

    def test_second_call_is_served_from_cache(self, settings):
        """After the first search, the result is cached and returned on the second call."""
        from icv_search.backends import get_search_backend
        from icv_search.services import create_index, index_documents, search

        settings.ICV_SEARCH_CACHE_ENABLED = True

        index = create_index("articles")
        index_documents(index, [{"id": "1", "title": "Django Guide"}])

        # First call populates the cache
        result1 = search(index, "Django")
        assert len(result1.hits) == 1

        # Intercept backend to verify the second call does NOT hit it
        backend = get_search_backend()
        original_search = backend.search
        call_count = [0]

        def counting_search(uid, query, **params):
            call_count[0] += 1
            return original_search(uid, query, **params)

        backend.search = counting_search

        result2 = search(index, "Django")
        assert result2 is not None
        assert call_count[0] == 0  # served from cache

        backend.search = original_search

    def test_user_kwarg_skips_cache(self, settings):
        """Searches with a ``user`` keyword arg always bypass the cache."""
        from icv_search.backends import get_search_backend
        from icv_search.services import create_index, index_documents, search

        settings.ICV_SEARCH_CACHE_ENABLED = True

        index = create_index("articles")
        index_documents(index, [{"id": "1", "title": "Python"}])

        backend = get_search_backend()
        original_search = backend.search
        call_count = [0]

        def counting_search(uid, query, **params):
            call_count[0] += 1
            return original_search(uid, query, **params)

        backend.search = counting_search

        # Two searches with a non-None user — both must hit the backend
        search(index, "Python", user=object())
        search(index, "Python", user=object())
        assert call_count[0] == 2

        backend.search = original_search

    def test_cache_invalidated_after_index_documents(self, settings):
        """Indexing documents invalidates cached results for that index."""
        from icv_search.services import create_index, index_documents, search

        settings.ICV_SEARCH_CACHE_ENABLED = True

        index = create_index("articles")
        # Initial search — caches an empty result
        search(index, "Django")

        # Add a document — triggers documents_indexed signal → cache invalidation
        index_documents(index, [{"id": "1", "title": "Django Guide"}])

        # Next search should go to backend and see the new document
        result = search(index, "Django")
        assert len(result.hits) == 1

    def test_cache_invalidated_after_remove_documents(self, settings):
        """Removing documents invalidates cached results for that index."""
        from icv_search.services import create_index, index_documents, remove_documents, search

        settings.ICV_SEARCH_CACHE_ENABLED = True

        index = create_index("articles")
        index_documents(index, [{"id": "1", "title": "Django Guide"}])

        # Cache the result with a hit
        result1 = search(index, "Django")
        assert len(result1.hits) == 1

        # Remove the document — triggers documents_removed signal → cache invalidation
        remove_documents(index, ["1"])

        # Next search should reflect the removal
        result2 = search(index, "Django")
        assert len(result2.hits) == 0

    def test_same_name_tenant_indexes_do_not_share_a_cached_result(self, settings):
        """A cache hit for one tenant cannot disclose another tenant's documents."""
        from icv_search.services import create_index, index_documents, search

        settings.ICV_SEARCH_CACHE_ENABLED = True

        first = create_index("products", tenant_id="tenant_a")
        second = create_index("products", tenant_id="tenant_b")
        index_documents(first, [{"id": "a", "title": "Tenant A shoes"}])
        index_documents(second, [{"id": "b", "title": "Tenant B shoes"}])

        first_result = search(first, "shoes")
        second_result = search(second, "shoes")

        assert first_result.hits == [{"id": "a", "title": "Tenant A shoes"}]
        assert second_result.hits == [{"id": "b", "title": "Tenant B shoes"}]
