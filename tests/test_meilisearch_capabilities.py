"""Tests for Meilisearch capabilities gap closure.

Covers: new search parameters, index settings management, delete-by-filter,
geo bounding box/polygon, SearchQuery builder extensions, and SearchResult
new fields.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from icv_search.backends import reset_search_backend
from icv_search.backends.dummy import DummyBackend
from icv_search.backends.meilisearch import MeilisearchBackend
from icv_search.query import SearchQuery
from icv_search.services import create_index, index_documents
from icv_search.types import MerchandisedSearchResult, SearchResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def use_dummy_backend(settings):
    """Use DummyBackend and reset state between tests."""
    settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
    settings.ICV_SEARCH_AUTO_SYNC = False
    settings.ICV_SEARCH_LOG_QUERIES = False
    reset_search_backend()
    DummyBackend.reset()
    yield
    DummyBackend.reset()
    reset_search_backend()


@pytest.fixture
def products_index(db):
    """Create a products search index with sample documents."""
    index = create_index("products", primary_key="id")
    index_documents(
        "products",
        [
            {"id": "1", "name": "Running Shoes", "brand": "Nike", "price": 120},
            {"id": "2", "name": "Walking Shoes", "brand": "Adidas", "price": 90},
            {"id": "3", "name": "Running Socks", "brand": "Nike", "price": 15},
        ],
    )
    return index


@pytest.fixture
def meilisearch_backend():
    """Create a MeilisearchBackend instance for testing."""
    return MeilisearchBackend(url="http://localhost:7700", api_key="test-key", timeout=30)


# ===========================================================================
# SearchResult — new fields
# ===========================================================================


class TestSearchResultNewFields:
    """SearchResult.from_engine handles new response fields."""

    def test_ranking_score_details_extracted(self):
        data = {
            "hits": [
                {
                    "id": "1",
                    "_rankingScoreDetails": {"words": {"order": 0, "score": 1.0}},
                }
            ],
            "processingTimeMs": 5,
        }
        result = SearchResult.from_engine(data)
        assert len(result.ranking_score_details) == 1
        assert result.ranking_score_details[0]["words"]["score"] == 1.0

    def test_ranking_score_details_stripped_from_hits(self):
        data = {
            "hits": [{"id": "1", "_rankingScoreDetails": {"words": {}}}],
        }
        result = SearchResult.from_engine(data)
        assert "_rankingScoreDetails" not in result.hits[0]

    def test_matches_position_extracted(self):
        data = {
            "hits": [
                {
                    "id": "1",
                    "_matchesPosition": {"name": [{"start": 0, "length": 5}]},
                }
            ],
        }
        result = SearchResult.from_engine(data)
        assert len(result.matches_position) == 1
        assert result.matches_position[0]["name"][0]["start"] == 0

    def test_matches_position_stripped_from_hits(self):
        data = {
            "hits": [{"id": "1", "_matchesPosition": {"name": []}}],
        }
        result = SearchResult.from_engine(data)
        assert "_matchesPosition" not in result.hits[0]

    def test_vectors_stripped_from_hits(self):
        data = {
            "hits": [{"id": "1", "_vectors": {"default": [0.1, 0.2]}}],
        }
        result = SearchResult.from_engine(data)
        assert "_vectors" not in result.hits[0]

    def test_page_based_pagination_fields(self):
        data = {
            "hits": [{"id": "1"}],
            "page": 2,
            "hitsPerPage": 10,
            "totalHits": 42,
            "totalPages": 5,
            "processingTimeMs": 3,
        }
        result = SearchResult.from_engine(data)
        assert result.page == 2
        assert result.hits_per_page == 10
        assert result.total_hits == 42
        assert result.total_pages == 5
        assert result.estimated_total_hits == 42

    def test_offset_based_response_has_no_page_fields(self):
        data = {
            "hits": [],
            "estimatedTotalHits": 100,
            "limit": 20,
            "offset": 0,
        }
        result = SearchResult.from_engine(data)
        assert result.page is None
        assert result.total_pages is None
        assert result.estimated_total_hits == 100

    def test_geo_distance_preserved_in_hits(self):
        data = {
            "hits": [{"id": "1", "_geoDistance": 1234}],
        }
        result = SearchResult.from_engine(data)
        assert result.hits[0]["_geoDistance"] == 1234


class TestMerchandisedSearchResultNewFields:
    """MerchandisedSearchResult carries over new SearchResult fields."""

    def test_from_search_result_carries_new_fields(self):
        sr = SearchResult(
            hits=[{"id": "1"}],
            ranking_score_details=[{"words": {}}],
            matches_position=[{"name": []}],
            page=1,
            hits_per_page=10,
            total_hits=5,
            total_pages=1,
        )
        mr = MerchandisedSearchResult.from_search_result(sr)
        assert mr.ranking_score_details == [{"words": {}}]
        assert mr.matches_position == [{"name": []}]
        assert mr.page == 1
        assert mr.hits_per_page == 10
        assert mr.total_hits == 5
        assert mr.total_pages == 1


# ===========================================================================
# Meilisearch backend — search parameter translation
# ===========================================================================


class TestMeilisearchSearchParams:
    """Meilisearch backend translates new search params to API body."""

    def _capture_body(self, backend, **params):
        """Execute search with mocked HTTP and return the JSON body sent."""
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"hits": [], "processingTimeMs": 1}
        with patch.object(backend._client, "request", return_value=response) as mock_req:
            backend.search("products", "test", **params)
            _, call_kwargs = mock_req.call_args
            return call_kwargs["json"]

    def test_attributes_to_retrieve(self, meilisearch_backend):
        body = self._capture_body(meilisearch_backend, attributes_to_retrieve=["name", "price"])
        assert body["attributesToRetrieve"] == ["name", "price"]

    def test_attributes_to_search_on(self, meilisearch_backend):
        body = self._capture_body(meilisearch_backend, attributes_to_search_on=["name"])
        assert body["attributesToSearchOn"] == ["name"]

    def test_crop_fields(self, meilisearch_backend):
        body = self._capture_body(
            meilisearch_backend,
            crop_fields=["description"],
            crop_length=20,
            crop_marker="...",
        )
        assert body["attributesToCrop"] == ["description"]
        assert body["cropLength"] == 20
        assert body["cropMarker"] == "..."

    def test_crop_fields_without_options(self, meilisearch_backend):
        body = self._capture_body(meilisearch_backend, crop_fields=["description"])
        assert body["attributesToCrop"] == ["description"]
        assert "cropLength" not in body
        assert "cropMarker" not in body

    def test_show_ranking_score_details(self, meilisearch_backend):
        body = self._capture_body(meilisearch_backend, show_ranking_score_details=True)
        assert body["showRankingScoreDetails"] is True

    def test_show_matches_position(self, meilisearch_backend):
        body = self._capture_body(meilisearch_backend, show_matches_position=True)
        assert body["showMatchesPosition"] is True

    def test_ranking_score_threshold(self, meilisearch_backend):
        body = self._capture_body(meilisearch_backend, ranking_score_threshold=0.8)
        assert body["rankingScoreThreshold"] == 0.8

    def test_distinct(self, meilisearch_backend):
        body = self._capture_body(meilisearch_backend, distinct="brand")
        assert body["distinct"] == "brand"

    def test_hybrid(self, meilisearch_backend):
        body = self._capture_body(
            meilisearch_backend,
            hybrid={"semanticRatio": 0.7, "embedder": "default"},
        )
        assert body["hybrid"]["semanticRatio"] == 0.7
        assert body["hybrid"]["embedder"] == "default"

    def test_vector(self, meilisearch_backend):
        vec = [0.1, 0.2, 0.3]
        body = self._capture_body(meilisearch_backend, vector=vec)
        assert body["vector"] == vec

    def test_retrieve_vectors(self, meilisearch_backend):
        body = self._capture_body(meilisearch_backend, retrieve_vectors=True)
        assert body["retrieveVectors"] is True

    def test_page_and_hits_per_page(self, meilisearch_backend):
        body = self._capture_body(meilisearch_backend, page=2, hits_per_page=10)
        assert body["page"] == 2
        assert body["hitsPerPage"] == 10

    def test_locales(self, meilisearch_backend):
        body = self._capture_body(meilisearch_backend, locales=["eng", "jpn"])
        assert body["locales"] == ["eng", "jpn"]

    def test_geo_bounding_box(self, meilisearch_backend):
        body = self._capture_body(
            meilisearch_backend,
            geo_bbox=((52.0, 0.5), (51.0, -0.5)),
        )
        assert "_geoBoundingBox" in body["filter"]
        assert "52.0" in body["filter"]

    def test_geo_polygon(self, meilisearch_backend):
        body = self._capture_body(
            meilisearch_backend,
            geo_polygon=[(51.5, -0.12), (52.0, 0.5), (51.0, 0.5)],
        )
        assert "_geoPolygon" in body["filter"]

    def test_geo_bbox_combined_with_existing_filter(self, meilisearch_backend):
        body = self._capture_body(
            meilisearch_backend,
            filter="brand = 'Nike'",
            geo_bbox=((52.0, 0.5), (51.0, -0.5)),
        )
        assert body["filter"].startswith("brand = 'Nike' AND")
        assert "_geoBoundingBox" in body["filter"]

    def test_geo_radius_and_bbox_combined(self, meilisearch_backend):
        body = self._capture_body(
            meilisearch_backend,
            geo_point=(51.5, -0.12),
            geo_radius=5000,
            geo_bbox=((52.0, 0.5), (51.0, -0.5)),
        )
        assert "_geoRadius" in body["filter"]
        assert "_geoBoundingBox" in body["filter"]

    def test_false_bools_not_sent(self, meilisearch_backend):
        """Parameters set to False/None should not appear in the body."""
        body = self._capture_body(
            meilisearch_backend,
            show_ranking_score=False,
            show_ranking_score_details=False,
            show_matches_position=False,
            retrieve_vectors=False,
        )
        assert "showRankingScore" not in body
        assert "showRankingScoreDetails" not in body
        assert "showMatchesPosition" not in body
        assert "retrieveVectors" not in body


# ===========================================================================
# Meilisearch backend — delete by filter
# ===========================================================================


class TestMeilisearchDeleteByFilter:
    """Meilisearch backend delete_documents_by_filter."""

    def test_sends_filter_in_body(self, meilisearch_backend):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"taskUid": 42, "status": "enqueued"}

        with patch.object(meilisearch_backend._client, "request", return_value=response) as mock_req:
            meilisearch_backend.delete_documents_by_filter("products", "brand = 'Nike'")
            _, call_kwargs = mock_req.call_args
            assert call_kwargs["json"] == {"filter": "brand = 'Nike'"}
            assert "/documents/delete" in mock_req.call_args[0][1]


class TestBaseBackendDeleteByFilter:
    """BaseSearchBackend default raises NotImplementedError."""

    def test_raises_not_implemented(self):
        backend = DummyBackend()
        with pytest.raises(NotImplementedError):
            backend.delete_documents_by_filter("products", "brand = 'Nike'")


# ===========================================================================
# SearchQuery builder — new methods
# ===========================================================================


class TestSearchQueryNewMethods:
    """SearchQuery builder exposes all new Meilisearch params."""

    def test_crop(self):
        params = SearchQuery("products").crop("description", length=20, marker="...")._build_params()
        assert params["crop_fields"] == ["description"]
        assert params["crop_length"] == 20
        assert params["crop_marker"] == "..."

    def test_crop_without_options(self):
        params = SearchQuery("products").crop("name")._build_params()
        assert params["crop_fields"] == ["name"]
        assert "crop_length" not in params
        assert "crop_marker" not in params

    def test_attributes_to_retrieve(self):
        params = SearchQuery("products").attributes_to_retrieve("name", "price")._build_params()
        assert params["attributes_to_retrieve"] == ["name", "price"]

    def test_attributes_to_search_on(self):
        params = SearchQuery("products").attributes_to_search_on("name")._build_params()
        assert params["attributes_to_search_on"] == ["name"]

    def test_distinct(self):
        params = SearchQuery("products").distinct("brand")._build_params()
        assert params["distinct"] == "brand"

    def test_hybrid(self):
        params = SearchQuery("products").hybrid(semantic_ratio=0.7, embedder="my-embedder")._build_params()
        assert params["hybrid"]["semanticRatio"] == 0.7
        assert params["hybrid"]["embedder"] == "my-embedder"

    def test_hybrid_defaults(self):
        params = SearchQuery("products").hybrid()._build_params()
        assert params["hybrid"]["semanticRatio"] == 0.5
        assert params["hybrid"]["embedder"] == "default"

    def test_vector(self):
        vec = [0.1, 0.2, 0.3]
        params = SearchQuery("products").vector(vec)._build_params()
        assert params["vector"] == vec

    def test_retrieve_vectors(self):
        params = SearchQuery("products").retrieve_vectors()._build_params()
        assert params["retrieve_vectors"] is True

    def test_ranking_score_threshold(self):
        params = SearchQuery("products").ranking_score_threshold(0.5)._build_params()
        assert params["ranking_score_threshold"] == 0.5

    def test_show_matches_position(self):
        params = SearchQuery("products").show_matches_position()._build_params()
        assert params["show_matches_position"] is True

    def test_show_ranking_score_details(self):
        params = SearchQuery("products").show_ranking_score_details()._build_params()
        assert params["show_ranking_score_details"] is True

    def test_locales(self):
        params = SearchQuery("products").locales("eng", "jpn")._build_params()
        assert params["locales"] == ["eng", "jpn"]

    def test_page_based_pagination(self):
        params = SearchQuery("products").page(3, per_page=25)._build_params()
        assert params["page"] == 3
        assert params["hits_per_page"] == 25

    def test_page_default_per_page(self):
        params = SearchQuery("products").page(1)._build_params()
        assert params["page"] == 1
        assert params["hits_per_page"] == 20

    def test_chaining_all_new_methods(self):
        """All new methods are chainable and produce correct params."""
        params = (
            SearchQuery("products")
            .text("shoes")
            .attributes_to_retrieve("name", "price")
            .attributes_to_search_on("name")
            .crop("description", length=15)
            .distinct("brand")
            .hybrid(semantic_ratio=0.6)
            .ranking_score_threshold(0.3)
            .show_matches_position()
            .show_ranking_score_details()
            .with_ranking_scores()
            .locales("eng")
            .page(2, per_page=10)
            ._build_params()
        )
        assert params["attributes_to_retrieve"] == ["name", "price"]
        assert params["attributes_to_search_on"] == ["name"]
        assert params["crop_fields"] == ["description"]
        assert params["distinct"] == "brand"
        assert params["hybrid"]["semanticRatio"] == 0.6
        assert params["ranking_score_threshold"] == 0.3
        assert params["show_matches_position"] is True
        assert params["show_ranking_score_details"] is True
        assert params["show_ranking_score"] is True
        assert params["locales"] == ["eng"]
        assert params["page"] == 2
        assert params["hits_per_page"] == 10


# ===========================================================================
# Index settings — service functions
# ===========================================================================


class TestIndexSettingsServices:
    """Service functions for new index settings."""

    def test_displayed_attributes_round_trip(self, db):
        from icv_search.services import (
            get_displayed_attributes,
            reset_displayed_attributes,
            update_displayed_attributes,
        )

        create_index("products")
        update_displayed_attributes("products", ["name", "price"])
        # Settings are pushed via update_index_settings which calls
        # backend.update_settings; DummyBackend stores them.
        # get_displayed_attributes reads from the backend directly.
        result = get_displayed_attributes("products")
        assert "name" in result

        reset_displayed_attributes("products")
        result = get_displayed_attributes("products")
        assert result == ["*"]

    def test_distinct_attribute_round_trip(self, db):
        from icv_search.services import (
            get_distinct_attribute,
            update_distinct_attribute,
        )

        create_index("products")
        update_distinct_attribute("products", "brand")
        result = get_distinct_attribute("products")
        assert result == "brand"

        update_distinct_attribute("products", None)
        result = get_distinct_attribute("products")
        assert result is None

    def test_pagination_settings_round_trip(self, db):
        from icv_search.services import (
            get_pagination_settings,
            update_pagination_settings,
        )

        create_index("products")
        update_pagination_settings("products", max_total_hits=5000)
        result = get_pagination_settings("products")
        assert result["maxTotalHits"] == 5000

    def test_faceting_settings_round_trip(self, db):
        from icv_search.services import (
            get_faceting_settings,
            update_faceting_settings,
        )

        create_index("products")
        update_faceting_settings(
            "products",
            {
                "maxValuesPerFacet": 200,
                "sortFacetValuesBy": {"brand": "count"},
            },
        )
        result = get_faceting_settings("products")
        assert result["maxValuesPerFacet"] == 200

    def test_proximity_precision_round_trip(self, db):
        from icv_search.services import (
            get_proximity_precision,
            update_proximity_precision,
        )

        create_index("products")
        update_proximity_precision("products", "byAttribute")
        result = get_proximity_precision("products")
        assert result == "byAttribute"

    def test_search_cutoff_round_trip(self, db):
        from icv_search.services import (
            get_search_cutoff,
            update_search_cutoff,
        )

        create_index("products")
        update_search_cutoff("products", 500)
        result = get_search_cutoff("products")
        assert result == 500

        update_search_cutoff("products", None)
        result = get_search_cutoff("products")
        assert result is None

    def test_dictionary_round_trip(self, db):
        from icv_search.services import (
            get_dictionary,
            reset_dictionary,
            update_dictionary,
        )

        create_index("products")
        update_dictionary("products", ["J. K. Rowling", "C++"])
        result = get_dictionary("products")
        assert "C++" in result

        reset_dictionary("products")
        result = get_dictionary("products")
        assert result == []

    def test_separator_tokens_round_trip(self, db):
        from icv_search.services import (
            get_separator_tokens,
            reset_separator_tokens,
            update_separator_tokens,
        )

        create_index("products")
        update_separator_tokens("products", ["@", "#"])
        result = get_separator_tokens("products")
        assert "@" in result

        reset_separator_tokens("products")
        assert get_separator_tokens("products") == []

    def test_non_separator_tokens_round_trip(self, db):
        from icv_search.services import (
            get_non_separator_tokens,
            reset_non_separator_tokens,
            update_non_separator_tokens,
        )

        create_index("products")
        update_non_separator_tokens("products", ["@", "#"])
        result = get_non_separator_tokens("products")
        assert "@" in result

        reset_non_separator_tokens("products")
        assert get_non_separator_tokens("products") == []

    def test_prefix_search_round_trip(self, db):
        from icv_search.services import (
            get_prefix_search,
            update_prefix_search,
        )

        create_index("products")
        update_prefix_search("products", "disabled")
        result = get_prefix_search("products")
        assert result == "disabled"

    def test_embedders_round_trip(self, db):
        from icv_search.services import (
            get_embedders,
            reset_embedders,
            update_embedders,
        )

        create_index("products")
        update_embedders(
            "products",
            {
                "default": {
                    "source": "userProvided",
                    "dimensions": 384,
                }
            },
        )
        result = get_embedders("products")
        assert "default" in result
        assert result["default"]["dimensions"] == 384

        reset_embedders("products")
        result = get_embedders("products")
        # After reset, embedders should be None or empty
        assert result is None or result == {}

    def test_localized_attributes_round_trip(self, db):
        from icv_search.services import (
            get_localized_attributes,
            reset_localized_attributes,
            update_localized_attributes,
        )

        create_index("products")
        rules = [{"attributePatterns": ["name_ja"], "locales": ["jpn"]}]
        update_localized_attributes("products", rules)
        result = get_localized_attributes("products")
        assert len(result) == 1
        assert result[0]["locales"] == ["jpn"]

        reset_localized_attributes("products")
        assert get_localized_attributes("products") == []

    def test_ranking_rules_round_trip(self, db):
        from icv_search.services import (
            get_ranking_rules,
            update_ranking_rules,
        )

        create_index("products")
        rules = ["words", "typo", "proximity", "attribute", "sort", "exactness"]
        update_ranking_rules("products", rules)
        result = get_ranking_rules("products")
        assert result == rules


# ===========================================================================
# Delete documents by filter — service layer
# ===========================================================================


class TestDeleteDocumentsByFilterService:
    """delete_documents_by_filter service function."""

    def test_with_meilisearch_backend(self):
        """Verify the Meilisearch backend method is called correctly."""
        backend = MeilisearchBackend(url="http://localhost:7700", api_key="test-key")
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"taskUid": 99, "status": "enqueued"}

        with patch.object(backend._client, "request", return_value=response):
            result = backend.delete_documents_by_filter("products", "brand = 'Nike'")
            assert result["taskUid"] == 99

    def test_service_translates_dict_filter(self, db):
        """When a dict filter is passed, it's translated to engine format."""
        from icv_search.services.documents import delete_documents_by_filter

        create_index("products")
        index_documents(
            "products",
            [
                {"id": "1", "brand": "Nike"},
                {"id": "2", "brand": "Adidas"},
            ],
        )

        # DummyBackend raises NotImplementedError for delete_documents_by_filter
        with pytest.raises(NotImplementedError):
            delete_documents_by_filter("products", {"brand": "Nike"})


# ===========================================================================
# SearchableMixin — search_displayed_fields
# ===========================================================================


class TestSearchableMixinDisplayedFields:
    """SearchableMixin supports search_displayed_fields."""

    def test_get_model_search_settings_includes_displayed(self):
        from icv_search.services.indexing import get_model_search_settings

        class FakeModel:
            search_fields = ["name"]
            search_filterable_fields = ["brand"]
            search_sortable_fields = ["price"]
            search_displayed_fields = ["name", "price"]

        settings = get_model_search_settings(FakeModel)
        assert settings["displayedAttributes"] == ["name", "price"]

    def test_empty_displayed_fields_omitted(self):
        from icv_search.services.indexing import get_model_search_settings

        class FakeModel:
            search_fields = ["name"]
            search_filterable_fields = []
            search_sortable_fields = []
            search_displayed_fields = []

        settings = get_model_search_settings(FakeModel)
        assert "displayedAttributes" not in settings


# ===========================================================================
# Cross-backend: multi_search param forwarding
# ===========================================================================


class TestMultiSearchParamForwarding:
    """multi_search() forwards all params, not just a hardcoded whitelist."""

    def test_forwards_highlight_fields(self, db):
        from icv_search.services import multi_search

        create_index("products")
        index_documents(
            "products",
            [{"id": "1", "name": "Running Shoes"}],
        )
        results = multi_search(
            [
                {
                    "index_name": "products",
                    "query": "Running",
                    "highlight_fields": ["name"],
                }
            ]
        )
        # DummyBackend generates formatted_hits when highlight_fields is provided
        assert len(results) == 1

    def test_forwards_filter_param(self, db):
        from icv_search.services import multi_search

        create_index("products")
        index_documents(
            "products",
            [
                {"id": "1", "name": "Shoes", "brand": "Nike"},
                {"id": "2", "name": "Socks", "brand": "Adidas"},
            ],
        )
        results = multi_search(
            [
                {
                    "index_name": "products",
                    "query": "",
                    "filter": {"brand": "Nike"},
                }
            ]
        )
        assert len(results[0].hits) == 1
        assert results[0].hits[0]["brand"] == "Nike"

    def test_forwards_limit_and_offset(self, db):
        from icv_search.services import multi_search

        create_index("products")
        index_documents(
            "products",
            [{"id": str(i), "name": f"Item {i}"} for i in range(10)],
        )
        results = multi_search(
            [
                {
                    "index_name": "products",
                    "query": "",
                    "limit": 3,
                    "offset": 2,
                }
            ]
        )
        assert len(results[0].hits) == 3


# ===========================================================================
# Cross-backend: attributes_to_retrieve snake_case
# ===========================================================================


class TestAttributesToRetrieveSnakeCase:
    """Backends accept snake_case attributes_to_retrieve from SearchQuery."""

    def test_dummy_backend_filters_fields_snake_case(self, db):
        create_index("products")
        index_documents(
            "products",
            [{"id": "1", "name": "Shoes", "brand": "Nike", "price": 120}],
        )
        from icv_search.services import search

        result = search("products", "", attributes_to_retrieve=["name"])
        # Only id and name should be returned
        assert "name" in result.hits[0]
        assert "id" in result.hits[0]
        assert "brand" not in result.hits[0]
        assert "price" not in result.hits[0]

    def test_autocomplete_uses_snake_case(self, db):
        create_index("products")
        index_documents(
            "products",
            [{"id": "1", "name": "Shoes", "brand": "Nike"}],
        )
        from icv_search.services import autocomplete

        result = autocomplete("products", "Shoes", fields=["name"])
        assert "id" in result.hits[0]
        assert "name" in result.hits[0]
        assert "brand" not in result.hits[0]


# ===========================================================================
# Cross-backend: SearchQuery executes through DummyBackend
# ===========================================================================


class TestSearchQueryDummyExecution:
    """SearchQuery builder methods execute correctly through DummyBackend."""

    def test_geo_near_executes(self, db):
        create_index("venues")
        index_documents(
            "venues",
            [
                {"id": "1", "name": "Pub", "_geo": {"lat": 51.5, "lng": -0.12}},
                {"id": "2", "name": "Cafe", "_geo": {"lat": 52.0, "lng": 0.1}},
            ],
        )
        result = SearchQuery("venues").text("").geo_near(lat=51.5, lng=-0.12, radius=10000).execute()
        assert len(result.hits) >= 1

    def test_facets_execute(self, db):
        create_index("products")
        index_documents(
            "products",
            [
                {"id": "1", "name": "Shoes", "brand": "Nike"},
                {"id": "2", "name": "Shirt", "brand": "Nike"},
                {"id": "3", "name": "Socks", "brand": "Adidas"},
            ],
        )
        result = SearchQuery("products").text("").facets("brand").execute()
        assert "brand" in result.facet_distribution
        assert result.facet_distribution["brand"]["Nike"] == 2

    def test_chained_sort_and_limit(self, db):
        create_index("products")
        index_documents(
            "products",
            [
                {"id": "1", "name": "A Shoes", "brand": "Nike", "price": 120},
                {"id": "2", "name": "B Shirt", "brand": "Nike", "price": 80},
                {"id": "3", "name": "C Socks", "brand": "Adidas", "price": 15},
            ],
        )
        result = SearchQuery("products").text("").sort("-price").limit(2).execute()
        assert len(result.hits) == 2
        assert result.hits[0]["price"] == 120

    def test_attributes_to_retrieve_via_query_builder(self, db):
        create_index("products")
        index_documents(
            "products",
            [{"id": "1", "name": "Shoes", "brand": "Nike", "price": 120}],
        )
        result = SearchQuery("products").text("").attributes_to_retrieve("name").execute()
        assert "name" in result.hits[0]
        assert "id" in result.hits[0]
        assert "brand" not in result.hits[0]
