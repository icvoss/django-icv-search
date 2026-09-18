"""Tests for new icv-search features.

Covers:
- SearchResult.facet_distribution and get_facet_values
- Range filter operators (__gte, __gt, __lte, __lt) in translate_filter_to_meilisearch
- In-memory range filter matching via apply_filters_to_documents (DummyBackend)
- ICVSearchPaginator and ICVSearchPage
- icv_search_health view
- DummyBackend.swap_indexes
- BaseSearchBackend.swap_indexes default (NotImplementedError)
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from django.test import RequestFactory, override_settings

from icv_search.backends.dummy import DummyBackend
from icv_search.backends.filters import apply_filters_to_documents, translate_filter_to_meilisearch
from icv_search.pagination import ICVSearchPage, ICVSearchPaginator
from icv_search.types import SearchResult

# ---------------------------------------------------------------------------
# 1. SearchResult facet_distribution
# ---------------------------------------------------------------------------


class TestSearchResultFacets:
    def test_from_engine_extracts_facet_distribution(self):
        """camelCase facetDistribution key is normalised."""
        data = {
            "hits": [],
            "estimatedTotalHits": 0,
            "facetDistribution": {"brand": {"Nike": 42, "Adidas": 31}},
        }
        result = SearchResult.from_engine(data)
        assert result.facet_distribution == {"brand": {"Nike": 42, "Adidas": 31}}

    def test_from_engine_extracts_snake_case_facet_distribution(self):
        """snake_case facet_distribution key is accepted as fallback."""
        data = {
            "hits": [],
            "estimatedTotalHits": 0,
            "facet_distribution": {"category": {"shoes": 10, "bags": 5}},
        }
        result = SearchResult.from_engine(data)
        assert result.facet_distribution == {"category": {"shoes": 10, "bags": 5}}

    def test_from_engine_defaults_to_empty_dict(self):
        """When neither facet key is present, facet_distribution defaults to empty dict."""
        data = {"hits": [], "estimatedTotalHits": 0}
        result = SearchResult.from_engine(data)
        assert result.facet_distribution == {}

    def test_get_facet_values_sorted_by_count_descending(self):
        """get_facet_values returns values sorted by count, highest first."""
        result = SearchResult(
            facet_distribution={
                "brand": {"Adidas": 31, "Nike": 42, "Puma": 15},
            }
        )
        values = result.get_facet_values("brand")
        assert values == [
            {"name": "Nike", "count": 42},
            {"name": "Adidas", "count": 31},
            {"name": "Puma", "count": 15},
        ]

    def test_get_facet_values_missing_facet_returns_empty_list(self):
        """get_facet_values returns an empty list for an unknown facet name."""
        result = SearchResult(facet_distribution={"brand": {"Nike": 10}})
        assert result.get_facet_values("category") == []

    def test_get_facet_values_with_empty_distribution(self):
        """get_facet_values returns an empty list when the facet exists but has no values."""
        result = SearchResult(facet_distribution={"brand": {}})
        assert result.get_facet_values("brand") == []

    def test_camelcase_takes_precedence_over_snake_case(self):
        """When both camelCase and snake_case keys exist, camelCase wins."""
        data = {
            "hits": [],
            "estimatedTotalHits": 0,
            "facetDistribution": {"brand": {"Nike": 5}},
            "facet_distribution": {"brand": {"Adidas": 99}},
        }
        result = SearchResult.from_engine(data)
        assert result.facet_distribution == {"brand": {"Nike": 5}}


# ---------------------------------------------------------------------------
# 2. Range filters — translate_filter_to_meilisearch
# ---------------------------------------------------------------------------


class TestRangeFilters:
    def test_translate_gte_filter(self):
        result = translate_filter_to_meilisearch({"price__gte": 10})
        assert result == "price >= 10"

    def test_translate_gt_filter(self):
        result = translate_filter_to_meilisearch({"price__gt": 10})
        assert result == "price > 10"

    def test_translate_lte_filter(self):
        result = translate_filter_to_meilisearch({"price__lte": 100})
        assert result == "price <= 100"

    def test_translate_lt_filter(self):
        result = translate_filter_to_meilisearch({"price__lt": 100})
        assert result == "price < 100"

    def test_translate_combined_range(self):
        result = translate_filter_to_meilisearch({"price__gte": 10, "price__lte": 100})
        assert "price >= 10" in result
        assert "price <= 100" in result
        assert " AND " in result

    def test_translate_range_with_float(self):
        result = translate_filter_to_meilisearch({"price__gte": 9.99})
        assert result == "price >= 9.99"

    def test_range_ignores_non_numeric_value(self):
        """Non-numeric values with range suffix are silently skipped."""
        result = translate_filter_to_meilisearch({"name__gte": "abc"})
        assert result == ""

    def test_range_ignores_boolean_value(self):
        """Booleans (which are ints in Python) are not treated as numeric for range ops."""
        result = translate_filter_to_meilisearch({"score__gte": True})
        assert result == ""

    def test_range_mixed_with_equality(self):
        result = translate_filter_to_meilisearch({"category": "shoes", "price__gte": 50})
        assert "category = 'shoes'" in result
        assert "price >= 50" in result

    def test_translate_zero_range_value(self):
        """Zero is a valid numeric range value."""
        result = translate_filter_to_meilisearch({"price__gte": 0})
        assert result == "price >= 0"

    def test_translate_negative_range_value(self):
        """Negative numbers are valid for range filters."""
        result = translate_filter_to_meilisearch({"temperature__lt": -10})
        assert result == "temperature < -10"


# ---------------------------------------------------------------------------
# 3. In-memory range filter matching (apply_filters_to_documents)
# ---------------------------------------------------------------------------


class TestRangeFilterMatching:
    def test_matches_gte(self):
        docs = [{"price": 10}, {"price": 20}, {"price": 30}]
        result = apply_filters_to_documents(docs, {"price__gte": 20})
        assert len(result) == 2
        assert all(d["price"] >= 20 for d in result)

    def test_matches_gt(self):
        docs = [{"price": 10}, {"price": 20}, {"price": 30}]
        result = apply_filters_to_documents(docs, {"price__gt": 20})
        assert len(result) == 1
        assert result[0]["price"] == 30

    def test_matches_lte(self):
        docs = [{"price": 10}, {"price": 20}, {"price": 30}]
        result = apply_filters_to_documents(docs, {"price__lte": 20})
        assert len(result) == 2
        assert all(d["price"] <= 20 for d in result)

    def test_matches_lt(self):
        docs = [{"price": 10}, {"price": 20}, {"price": 30}]
        result = apply_filters_to_documents(docs, {"price__lt": 20})
        assert len(result) == 1
        assert result[0]["price"] == 10

    def test_matches_combined_range(self):
        docs = [{"price": 5}, {"price": 15}, {"price": 25}]
        result = apply_filters_to_documents(docs, {"price__gte": 10, "price__lte": 20})
        assert len(result) == 1
        assert result[0]["price"] == 15

    def test_range_boundary_gte_exact_match(self):
        """gte includes the boundary value itself."""
        docs = [{"price": 10}, {"price": 11}]
        result = apply_filters_to_documents(docs, {"price__gte": 10})
        assert len(result) == 2

    def test_range_boundary_gt_excludes_exact_match(self):
        """gt excludes the boundary value itself."""
        docs = [{"price": 10}, {"price": 11}]
        result = apply_filters_to_documents(docs, {"price__gt": 10})
        assert len(result) == 1
        assert result[0]["price"] == 11

    def test_range_with_float_doc_values(self):
        """Range comparisons work when document values are floats."""
        docs = [{"score": 1.5}, {"score": 2.5}, {"score": 3.5}]
        result = apply_filters_to_documents(docs, {"score__gte": 2.5})
        assert len(result) == 2

    def test_range_skips_non_numeric_filter_value(self):
        """A string range value is silently skipped — all docs pass."""
        docs = [{"price": 10}, {"price": 20}]
        result = apply_filters_to_documents(docs, {"price__gte": "abc"})
        assert len(result) == 2

    def test_range_excludes_doc_with_non_numeric_field(self):
        """A document whose field value cannot be cast to float is excluded."""
        docs = [{"price": "not-a-number"}, {"price": 20}]
        result = apply_filters_to_documents(docs, {"price__gte": 10})
        assert len(result) == 1
        assert result[0]["price"] == 20

    def test_range_filter_combined_with_equality(self):
        """Range and equality filters work together correctly."""
        docs = [
            {"category": "shoes", "price": 30},
            {"category": "shoes", "price": 80},
            {"category": "bags", "price": 60},
        ]
        result = apply_filters_to_documents(docs, {"category": "shoes", "price__lte": 50})
        assert len(result) == 1
        assert result[0]["price"] == 30


# ---------------------------------------------------------------------------
# 4. ICVSearchPaginator and ICVSearchPage
# ---------------------------------------------------------------------------


class TestICVSearchPaginator:
    def test_count_uses_estimated_total_hits(self):
        result = SearchResult(hits=[{"id": "1"}], estimated_total_hits=100, limit=10)
        paginator = ICVSearchPaginator(result)
        assert paginator.count == 100

    def test_is_estimated_always_true(self):
        result = SearchResult(hits=[], estimated_total_hits=0, limit=10)
        paginator = ICVSearchPaginator(result)
        assert paginator.is_estimated is True

    def test_num_pages(self):
        result = SearchResult(hits=[{"id": "1"}], estimated_total_hits=100, limit=10)
        paginator = ICVSearchPaginator(result)
        assert paginator.num_pages == 10

    def test_num_pages_rounds_up(self):
        """101 results / 10 per page = 11 pages."""
        result = SearchResult(hits=[], estimated_total_hits=101, limit=10)
        paginator = ICVSearchPaginator(result)
        assert paginator.num_pages == 11

    def test_per_page_defaults_to_search_result_limit(self):
        result = SearchResult(hits=[], estimated_total_hits=50, limit=25)
        paginator = ICVSearchPaginator(result)
        assert paginator.per_page == 25

    def test_per_page_override(self):
        result = SearchResult(hits=[], estimated_total_hits=50, limit=25)
        paginator = ICVSearchPaginator(result, per_page=10)
        assert paginator.per_page == 10

    def test_page_returns_search_page_instance(self):
        result = SearchResult(hits=[{"id": "1"}, {"id": "2"}], estimated_total_hits=100, limit=10)
        paginator = ICVSearchPaginator(result)
        page = paginator.page(1)
        assert isinstance(page, ICVSearchPage)

    def test_page_contains_hits(self):
        result = SearchResult(hits=[{"id": "1"}, {"id": "2"}], estimated_total_hits=100, limit=10)
        paginator = ICVSearchPaginator(result)
        page = paginator.page(1)
        assert list(page) == [{"id": "1"}, {"id": "2"}]

    def test_page_is_estimated(self):
        result = SearchResult(hits=[], estimated_total_hits=100, limit=10)
        paginator = ICVSearchPaginator(result)
        page = paginator.page(1)
        assert page.is_estimated is True

    def test_display_count_prefixes_tilde(self):
        result = SearchResult(hits=[], estimated_total_hits=1200, limit=10)
        paginator = ICVSearchPaginator(result)
        page = paginator.page(1)
        assert page.display_count() == "~1,200"

    def test_display_count_formats_with_commas(self):
        result = SearchResult(hits=[], estimated_total_hits=1_000_000, limit=10)
        paginator = ICVSearchPaginator(result)
        page = paginator.page(1)
        assert page.display_count() == "~1,000,000"

    def test_display_count_custom_prefix(self):
        result = SearchResult(hits=[], estimated_total_hits=500, limit=10)
        paginator = ICVSearchPaginator(result)
        page = paginator.page(1)
        assert page.display_count(prefix="approx. ") == "approx. 500"

    def test_high_page_number_does_not_raise(self):
        """Estimates can shift; a page number beyond num_pages should not raise EmptyPage."""
        result = SearchResult(hits=[], estimated_total_hits=10, limit=10)
        paginator = ICVSearchPaginator(result)
        page = paginator.page(999)
        assert list(page) == []

    def test_zero_count_allows_empty_first_page(self):
        result = SearchResult(hits=[], estimated_total_hits=0, limit=10)
        paginator = ICVSearchPaginator(result)
        assert paginator.num_pages == 1

    def test_zero_count_no_allow_empty_first_page(self):
        result = SearchResult(hits=[], estimated_total_hits=0, limit=10)
        paginator = ICVSearchPaginator(result, allow_empty_first_page=False)
        assert paginator.num_pages == 0

    def test_page_one_with_data(self):
        hits = [{"id": str(i)} for i in range(5)]
        result = SearchResult(hits=hits, estimated_total_hits=50, limit=5)
        paginator = ICVSearchPaginator(result)
        page = paginator.page(1)
        assert len(list(page)) == 5

    def test_paginator_stores_search_result(self):
        result = SearchResult(hits=[], estimated_total_hits=42, limit=10)
        paginator = ICVSearchPaginator(result)
        assert paginator.search_result is result


# ---------------------------------------------------------------------------
# 5. Health check view
# ---------------------------------------------------------------------------

# A minimal URL conf for health view tests only.
_health_urlconf = "icv_search.testing.urls"


class TestHealthView:
    @pytest.mark.django_db
    @override_settings(ROOT_URLCONF=_health_urlconf)
    def test_health_ok(self, client):
        """DummyBackend.health() returns True — view returns 200 with status ok."""
        response = client.get("/health/")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    @pytest.mark.django_db
    @override_settings(ROOT_URLCONF=_health_urlconf)
    def test_health_unavailable(self, client):
        """When health() returns False, view returns 503 with status unavailable."""
        with patch("icv_search.backends.dummy.DummyBackend.health", return_value=False):
            response = client.get("/health/")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}

    @pytest.mark.django_db
    @override_settings(ROOT_URLCONF=_health_urlconf)
    def test_health_unavailable_on_exception(self, client, caplog):
        """When health() raises an exception, view returns 503 and logs a warning."""
        with caplog.at_level(logging.WARNING, logger="icv_search.views"), patch(
            "icv_search.backends.dummy.DummyBackend.health",
            side_effect=Exception("connection refused"),
        ):
            response = client.get("/health/")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
        assert any("Search backend health check failed" in r.message for r in caplog.records)

    @pytest.mark.django_db
    @override_settings(ROOT_URLCONF=_health_urlconf)
    def test_health_only_accepts_get(self, client):
        """POST to the health endpoint returns 405 Method Not Allowed."""
        response = client.post("/health/")
        assert response.status_code == 405

    @pytest.mark.django_db
    @override_settings(ROOT_URLCONF=_health_urlconf)
    def test_health_view_directly_via_request_factory(self):
        """Direct view invocation via RequestFactory — no URL routing needed."""
        from icv_search.views import icv_search_health

        factory = RequestFactory()
        request = factory.get("/health/")
        response = icv_search_health(request)
        assert response.status_code == 200

    @pytest.mark.django_db
    @override_settings(ROOT_URLCONF=_health_urlconf)
    def test_health_response_is_json(self, client):
        """Response Content-Type is application/json."""
        response = client.get("/health/")
        assert "application/json" in response.get("Content-Type", "")


# ---------------------------------------------------------------------------
# 6. DummyBackend.swap_indexes
# ---------------------------------------------------------------------------


class TestDummyBackendSwap:
    def test_swap_indexes_swaps_documents(self):
        backend = DummyBackend()
        backend.create_index("a")
        backend.create_index("b")
        backend.add_documents("a", [{"id": "1", "name": "doc_a"}])
        backend.add_documents("b", [{"id": "2", "name": "doc_b"}])

        backend.swap_indexes([("a", "b")])

        result_a = backend.search("a", "")
        result_b = backend.search("b", "")
        assert result_a["hits"][0]["name"] == "doc_b"
        assert result_b["hits"][0]["name"] == "doc_a"

    def test_swap_indexes_returns_task_uid(self):
        backend = DummyBackend()
        backend.create_index("a")
        backend.create_index("b")

        result = backend.swap_indexes([("a", "b")])

        assert "taskUid" in result
        assert result["status"] == "succeeded"

    def test_swap_indexes_swaps_settings(self):
        backend = DummyBackend()
        backend.create_index("a")
        backend.create_index("b")
        backend.update_settings("a", {"searchableAttributes": ["title"]})
        backend.update_settings("b", {"searchableAttributes": ["body"]})

        backend.swap_indexes([("a", "b")])

        assert backend.get_settings("a") == {"searchableAttributes": ["body"]}
        assert backend.get_settings("b") == {"searchableAttributes": ["title"]}

    def test_swap_multiple_pairs(self):
        backend = DummyBackend()
        for uid in ("x", "y", "p", "q"):
            backend.create_index(uid)
        backend.add_documents("x", [{"id": "1", "name": "from_x"}])
        backend.add_documents("y", [{"id": "2", "name": "from_y"}])
        backend.add_documents("p", [{"id": "3", "name": "from_p"}])
        backend.add_documents("q", [{"id": "4", "name": "from_q"}])

        backend.swap_indexes([("x", "y"), ("p", "q")])

        assert backend.search("x", "")["hits"][0]["name"] == "from_y"
        assert backend.search("y", "")["hits"][0]["name"] == "from_x"
        assert backend.search("p", "")["hits"][0]["name"] == "from_q"
        assert backend.search("q", "")["hits"][0]["name"] == "from_p"

    def test_swap_indexes_with_empty_index(self):
        """Swapping a populated index with an empty one clears the populated one."""
        backend = DummyBackend()
        backend.create_index("a")
        backend.create_index("b")
        backend.add_documents("a", [{"id": "1", "name": "doc_a"}])

        backend.swap_indexes([("a", "b")])

        assert backend.search("a", "")["hits"] == []
        assert backend.search("b", "")["hits"][0]["name"] == "doc_a"


# ---------------------------------------------------------------------------
# 7. BaseSearchBackend.swap_indexes default raises NotImplementedError
# ---------------------------------------------------------------------------


class TestBaseBackendSwap:
    def test_swap_indexes_raises_not_implemented(self):
        """A concrete backend that does not override swap_indexes raises NotImplementedError."""
        from icv_search.backends.base import BaseSearchBackend

        class MinimalBackend(BaseSearchBackend):
            def create_index(self, uid, primary_key="id"):
                return {}

            def delete_index(self, uid):
                pass

            def update_settings(self, uid, settings):
                return {}

            def get_settings(self, uid):
                return {}

            def add_documents(self, uid, documents, primary_key="id"):
                return {}

            def delete_documents(self, uid, document_ids):
                return {}

            def clear_documents(self, uid):
                return {}

            def search(self, uid, query, **params):
                return {}

            def get_stats(self, uid):
                return {}

            def health(self):
                return True

        backend = MinimalBackend(url="", api_key="")
        with pytest.raises(NotImplementedError):
            backend.swap_indexes([("a", "b")])

    def test_swap_indexes_error_message_includes_class_name(self):
        """NotImplementedError message names the backend class."""
        from icv_search.backends.base import BaseSearchBackend

        class MyCustomBackend(BaseSearchBackend):
            def create_index(self, uid, primary_key="id"):
                return {}

            def delete_index(self, uid):
                pass

            def update_settings(self, uid, settings):
                return {}

            def get_settings(self, uid):
                return {}

            def add_documents(self, uid, documents, primary_key="id"):
                return {}

            def delete_documents(self, uid, document_ids):
                return {}

            def clear_documents(self, uid):
                return {}

            def search(self, uid, query, **params):
                return {}

            def get_stats(self, uid):
                return {}

            def health(self):
                return True

        backend = MyCustomBackend(url="", api_key="")
        with pytest.raises(NotImplementedError, match="MyCustomBackend"):
            backend.swap_indexes([("a", "b")])


# ---------------------------------------------------------------------------
# 8. SearchResult highlighting
# ---------------------------------------------------------------------------


class TestSearchResultHighlighting:
    def test_formatted_hits_defaults_to_empty_list(self):
        """SearchResult.formatted_hits is an empty list by default."""
        result = SearchResult()
        assert result.formatted_hits == []

    def test_get_highlighted_hits_returns_formatted_when_present(self):
        """get_highlighted_hits() returns formatted_hits when populated."""
        plain = [{"id": "1", "title": "Hello world"}]
        highlighted = [{"id": "1", "title": "Hello <mark>world</mark>"}]
        result = SearchResult(hits=plain, formatted_hits=highlighted)
        assert result.get_highlighted_hits() == highlighted

    def test_get_highlighted_hits_falls_back_to_hits(self):
        """get_highlighted_hits() returns plain hits when formatted_hits is empty."""
        plain = [{"id": "1", "title": "Hello world"}]
        result = SearchResult(hits=plain)
        assert result.get_highlighted_hits() == plain

    def test_from_engine_extracts_formatted_from_meilisearch_hits(self):
        """from_engine() extracts _formatted from each Meilisearch hit."""
        data = {
            "hits": [
                {
                    "id": "1",
                    "title": "Hello world",
                    "_formatted": {"id": "1", "title": "Hello <mark>world</mark>"},
                },
                {
                    "id": "2",
                    "title": "Goodbye world",
                    "_formatted": {"id": "2", "title": "Goodbye <mark>world</mark>"},
                },
            ],
            "query": "world",
            "estimatedTotalHits": 2,
        }
        result = SearchResult.from_engine(data)
        assert result.formatted_hits == [
            {"id": "1", "title": "Hello <mark>world</mark>"},
            {"id": "2", "title": "Goodbye <mark>world</mark>"},
        ]

    def test_from_engine_strips_formatted_key_from_plain_hits(self):
        """from_engine() removes _formatted from each hit in the hits list."""
        data = {
            "hits": [
                {
                    "id": "1",
                    "title": "Hello world",
                    "_formatted": {"id": "1", "title": "Hello <mark>world</mark>"},
                }
            ],
            "query": "world",
            "estimatedTotalHits": 1,
        }
        result = SearchResult.from_engine(data)
        assert "_formatted" not in result.hits[0]

    def test_from_engine_partial_formatted_hits(self):
        """Only hits that contain _formatted contribute to formatted_hits."""
        data = {
            "hits": [
                {"id": "1", "title": "Hello", "_formatted": {"id": "1", "title": "<mark>Hello</mark>"}},
                {"id": "2", "title": "World"},
            ],
            "query": "hello",
            "estimatedTotalHits": 2,
        }
        result = SearchResult.from_engine(data)
        # Only the first hit has _formatted
        assert len(result.formatted_hits) == 1
        assert result.formatted_hits[0]["title"] == "<mark>Hello</mark>"

    def test_from_engine_accepts_top_level_formatted_hits(self):
        """from_engine() accepts a top-level formatted_hits key (Postgres/Dummy)."""
        data = {
            "hits": [{"id": "1", "title": "Hello world"}],
            "formatted_hits": [{"id": "1", "title": "Hello <mark>world</mark>"}],
            "query": "world",
            "estimatedTotalHits": 1,
        }
        result = SearchResult.from_engine(data)
        assert result.formatted_hits == [{"id": "1", "title": "Hello <mark>world</mark>"}]

    def test_from_engine_meilisearch_formatted_takes_precedence_over_top_level(self):
        """Meilisearch _formatted keys take precedence over a top-level formatted_hits key."""
        data = {
            "hits": [
                {
                    "id": "1",
                    "title": "Hello",
                    "_formatted": {"id": "1", "title": "<em>Hello</em>"},
                }
            ],
            "formatted_hits": [{"id": "1", "title": "ignored"}],
            "query": "hello",
            "estimatedTotalHits": 1,
        }
        result = SearchResult.from_engine(data)
        assert result.formatted_hits[0]["title"] == "<em>Hello</em>"

    def test_from_engine_no_highlighting_data(self):
        """from_engine() leaves formatted_hits empty when no highlight data is present."""
        data = {
            "hits": [{"id": "1", "title": "Hello"}],
            "query": "hello",
            "estimatedTotalHits": 1,
        }
        result = SearchResult.from_engine(data)
        assert result.formatted_hits == []


# ---------------------------------------------------------------------------
# 9. DummyBackend highlighting
# ---------------------------------------------------------------------------


class TestDummyBackendHighlighting:
    def setup_method(self):
        DummyBackend.reset()

    def teardown_method(self):
        DummyBackend.reset()

    def test_highlight_wraps_matching_term(self):
        """DummyBackend wraps matched substrings with <mark> tags by default."""
        backend = DummyBackend()
        backend.create_index("idx")
        backend.add_documents("idx", [{"id": "1", "title": "Hello world", "body": "A world of text"}])

        raw = backend.search("idx", "world", highlight_fields=["title", "body"])

        assert "formatted_hits" in raw
        assert raw["formatted_hits"][0]["title"] == "Hello <mark>world</mark>"
        assert raw["formatted_hits"][0]["body"] == "A <mark>world</mark> of text"

    def test_highlight_custom_tags(self):
        """Custom pre/post tags are used when supplied."""
        backend = DummyBackend()
        backend.create_index("idx")
        backend.add_documents("idx", [{"id": "1", "title": "Hello world"}])

        raw = backend.search(
            "idx",
            "world",
            highlight_fields=["title"],
            highlight_pre_tag="<b>",
            highlight_post_tag="</b>",
        )

        assert raw["formatted_hits"][0]["title"] == "Hello <b>world</b>"

    def test_highlight_case_insensitive(self):
        """Highlighting is case-insensitive — matched casing is preserved."""
        backend = DummyBackend()
        backend.create_index("idx")
        backend.add_documents("idx", [{"id": "1", "title": "Hello World"}])

        raw = backend.search("idx", "world", highlight_fields=["title"])

        assert raw["formatted_hits"][0]["title"] == "Hello <mark>World</mark>"

    def test_highlight_non_string_field_unchanged(self):
        """Non-string fields are carried through to formatted_hits unchanged."""
        backend = DummyBackend()
        backend.create_index("idx")
        backend.add_documents("idx", [{"id": "1", "title": "Hello world", "count": 42}])

        raw = backend.search("idx", "world", highlight_fields=["title", "count"])

        assert raw["formatted_hits"][0]["count"] == 42

    def test_no_highlight_when_no_highlight_fields(self):
        """formatted_hits is absent from the response when highlight_fields is not passed."""
        backend = DummyBackend()
        backend.create_index("idx")
        backend.add_documents("idx", [{"id": "1", "title": "Hello world"}])

        raw = backend.search("idx", "world")

        assert "formatted_hits" not in raw

    def test_no_highlight_when_empty_query(self):
        """formatted_hits is absent when the query is empty (nothing to highlight)."""
        backend = DummyBackend()
        backend.create_index("idx")
        backend.add_documents("idx", [{"id": "1", "title": "Hello world"}])

        raw = backend.search("idx", "", highlight_fields=["title"])

        assert "formatted_hits" not in raw

    def test_highlight_preserves_non_highlighted_fields(self):
        """Fields not in highlight_fields are still present in formatted_hits."""
        backend = DummyBackend()
        backend.create_index("idx")
        backend.add_documents("idx", [{"id": "1", "title": "Hello world", "author": "Alice"}])

        raw = backend.search("idx", "world", highlight_fields=["title"])

        assert raw["formatted_hits"][0]["author"] == "Alice"

    def test_plain_hits_are_unmodified(self):
        """The plain hits list is not mutated by the highlighting logic."""
        backend = DummyBackend()
        backend.create_index("idx")
        backend.add_documents("idx", [{"id": "1", "title": "Hello world"}])

        raw = backend.search("idx", "world", highlight_fields=["title"])

        assert raw["hits"][0]["title"] == "Hello world"

    def test_search_result_from_dummy_highlighting(self):
        """SearchResult.from_engine correctly wires formatted_hits from a DummyBackend response."""
        from icv_search.types import SearchResult

        backend = DummyBackend()
        backend.create_index("idx")
        backend.add_documents("idx", [{"id": "1", "title": "Hello world"}])

        raw = backend.search("idx", "world", highlight_fields=["title"])
        result = SearchResult.from_engine(raw)

        assert result.get_highlighted_hits()[0]["title"] == "Hello <mark>world</mark>"
        assert result.hits[0]["title"] == "Hello world"
