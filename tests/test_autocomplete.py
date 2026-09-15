"""Tests for the autocomplete() service function."""

from __future__ import annotations

import pytest

from icv_search.backends import reset_search_backend
from icv_search.backends.dummy import DummyBackend
from icv_search.models.analytics import SearchQueryLog
from icv_search.services import autocomplete, create_index, index_documents, remove_documents


@pytest.fixture(autouse=True)
def use_dummy_backend(settings):
    settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
    settings.ICV_SEARCH_AUTO_SYNC = False
    settings.ICV_SEARCH_LOG_QUERIES = True
    settings.ICV_SEARCH_CACHE_ENABLED = False
    reset_search_backend()
    DummyBackend.reset()
    yield
    DummyBackend.reset()
    reset_search_backend()


@pytest.fixture()
def products_index():
    index = create_index("products")
    index_documents(
        index,
        [
            {"id": "1", "title": "Running Shoes", "brand": "Nike", "price": 120},
            {"id": "2", "title": "Running Shorts", "brand": "Adidas", "price": 45},
            {"id": "3", "title": "Tennis Racket", "brand": "Wilson", "price": 200},
            {"id": "4", "title": "Running Jacket", "brand": "Nike", "price": 90},
            {"id": "5", "title": "Running Socks", "brand": "Puma", "price": 15},
            {"id": "6", "title": "Running Cap", "brand": "Nike", "price": 25},
        ],
    )
    return index


class TestAutocompleteBasic:
    """Core autocomplete functionality."""

    @pytest.mark.django_db
    def test_returns_search_result(self, products_index):
        result = autocomplete("products", "Running")
        assert hasattr(result, "hits")
        assert hasattr(result, "estimated_total_hits")

    @pytest.mark.django_db
    def test_default_limit_is_five(self, products_index):
        result = autocomplete("products", "Running")
        assert len(result.hits) == 5

    @pytest.mark.django_db
    def test_custom_limit(self, products_index):
        result = autocomplete("products", "Running", limit=2)
        assert len(result.hits) == 2

    @pytest.mark.django_db
    def test_empty_query_returns_results(self, products_index):
        result = autocomplete("products", "", limit=3)
        assert len(result.hits) == 3

    @pytest.mark.django_db
    def test_no_matches_returns_empty(self, products_index):
        result = autocomplete("products", "zzzznotfound")
        assert len(result.hits) == 0

    @pytest.mark.django_db
    def test_accepts_index_instance(self, products_index):
        result = autocomplete(products_index, "Running", limit=2)
        assert len(result.hits) == 2


class TestAutocompleteNoQueryLogging:
    """BR-009: autocomplete must not create SearchQueryLog rows."""

    @pytest.mark.django_db
    def test_does_not_log_queries(self, products_index):
        assert SearchQueryLog.objects.count() == 0
        autocomplete("products", "Running")
        assert SearchQueryLog.objects.count() == 0

    @pytest.mark.django_db
    def test_does_not_log_even_with_logging_enabled(self, products_index, settings):
        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_MODE = "both"
        autocomplete("products", "Running")
        assert SearchQueryLog.objects.count() == 0


class TestAutocompleteFieldFiltering:
    """BR-010: fields param maps to attributesToRetrieve."""

    @pytest.mark.django_db
    def test_fields_filters_returned_keys(self, products_index):
        result = autocomplete("products", "Running", fields=["title"])
        for hit in result.hits:
            assert set(hit.keys()) == {"id", "title"}

    @pytest.mark.django_db
    def test_id_always_included(self, products_index):
        result = autocomplete("products", "Running", fields=["brand"])
        for hit in result.hits:
            assert "id" in hit

    @pytest.mark.django_db
    def test_multiple_fields(self, products_index):
        result = autocomplete("products", "Running", fields=["title", "brand"])
        for hit in result.hits:
            assert set(hit.keys()) == {"id", "title", "brand"}

    @pytest.mark.django_db
    def test_no_fields_returns_all_keys(self, products_index):
        result = autocomplete("products", "Running", limit=1)
        hit = result.hits[0]
        assert "title" in hit
        assert "brand" in hit
        assert "price" in hit

    @pytest.mark.django_db
    def test_fields_with_id_explicit(self, products_index):
        """id listed explicitly should not cause duplication."""
        result = autocomplete("products", "Running", fields=["id", "title"])
        for hit in result.hits:
            assert set(hit.keys()) == {"id", "title"}


class TestAutocompleteTenantResolution:
    """Tenant resolution follows the same pattern as search()."""

    @pytest.mark.django_db
    def test_tenant_id_resolves_correctly(self):
        create_index("products", tenant_id="tenant-a")
        index_documents(
            "products",
            [
                {"id": "1", "title": "Tenant A Product"},
            ],
            tenant_id="tenant-a",
        )

        result = autocomplete("products", "Tenant", tenant_id="tenant-a")
        assert len(result.hits) == 1

    @pytest.mark.django_db
    def test_wrong_tenant_returns_no_results(self):
        create_index("products", tenant_id="tenant-a")
        index_documents(
            "products",
            [
                {"id": "1", "title": "Tenant A Product"},
            ],
            tenant_id="tenant-a",
        )

        create_index("products", tenant_id="tenant-b")
        result = autocomplete("products", "Tenant", tenant_id="tenant-b")
        assert len(result.hits) == 0

    @pytest.mark.django_db
    def test_cached_results_are_tenant_isolated_and_invalidated(self, settings):
        """Autocomplete never returns or retains another tenant's cached hits."""
        settings.ICV_SEARCH_CACHE_ENABLED = True
        first = create_index("products", tenant_id="tenant-a")
        second = create_index("products", tenant_id="tenant-b")
        index_documents(first, [{"id": "a", "title": "Tenant A Product"}])
        index_documents(second, [{"id": "b", "title": "Tenant B Product"}])

        assert autocomplete(first, "Tenant").hits == [{"id": "a", "title": "Tenant A Product"}]
        assert autocomplete(second, "Tenant").hits == [{"id": "b", "title": "Tenant B Product"}]

        remove_documents(second, ["b"])

        assert autocomplete(second, "Tenant").hits == []
        assert autocomplete(first, "Tenant").hits == [{"id": "a", "title": "Tenant A Product"}]


class TestAutocompleteExtraParams:
    """Extra params are forwarded to the backend."""

    @pytest.mark.django_db
    def test_filter_param_forwarded(self, products_index):
        result = autocomplete(
            "products",
            "Running",
            filter={"brand": "Nike"},
            limit=10,
        )
        for hit in result.hits:
            assert hit["brand"] == "Nike"

    @pytest.mark.django_db
    def test_offset_param_forwarded(self, products_index):
        all_results = autocomplete("products", "Running", limit=10)
        offset_results = autocomplete("products", "Running", limit=2, offset=2)
        assert len(offset_results.hits) <= 2
        if len(all_results.hits) > 2:
            assert offset_results.hits[0]["id"] == all_results.hits[2]["id"]


class TestAutocompleteExport:
    """autocomplete is correctly exported."""

    def test_importable_from_services(self):
        from icv_search.services import autocomplete as ac

        assert callable(ac)

    def test_in_services_all(self):
        from icv_search.services import __all__

        assert "autocomplete" in __all__
