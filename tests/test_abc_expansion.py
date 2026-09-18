"""Tests for the 6 new optional methods added to BaseSearchBackend.

Covers:
- DummyBackend implementations of get_document, get_documents, facet_search,
  similar_documents, compact, and update_documents
- Service layer wrappers for each of the above
- BaseSearchBackend default/fallback behaviour
"""

from __future__ import annotations

import logging

import pytest

from icv_search.backends import reset_search_backend
from icv_search.backends.dummy import DummyBackend, _documents
from icv_search.exceptions import IndexNotFoundError, SearchBackendError
from icv_search.services import (
    create_index,
    facet_search,
    get_document,
    get_documents,
    index_documents,
    similar_documents,
    update_documents,
)
from icv_search.services.indexing import compact_index

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def use_dummy_backend(settings):
    """Configure DummyBackend for all tests in this module."""
    settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
    settings.ICV_SEARCH_AUTO_SYNC = False
    reset_search_backend()
    DummyBackend.reset()
    yield
    DummyBackend.reset()
    reset_search_backend()


# ===========================================================================
# DummyBackend — get_document
# ===========================================================================


class TestDummyBackendGetDocument:
    """DummyBackend.get_document()"""

    def setup_method(self):
        DummyBackend.reset()
        self.backend = DummyBackend()
        self.backend.create_index("products")
        self.backend.add_documents(
            "products",
            [{"id": "1", "name": "Widget", "price": 9.99}],
        )

    def test_returns_correct_document_dict(self):
        """Fetching an existing document returns the full dict (BR-012)."""
        doc = self.backend.get_document("products", "1")
        assert doc["id"] == "1"
        assert doc["name"] == "Widget"
        assert doc["price"] == 9.99

    def test_returned_document_always_includes_id(self):
        """id field is always present in the returned dict (BR-012)."""
        doc = self.backend.get_document("products", "1")
        assert "id" in doc

    def test_non_existent_index_raises_index_not_found_error(self):
        """Fetching from a non-existent index raises IndexNotFoundError."""
        with pytest.raises(IndexNotFoundError):
            self.backend.get_document("no-such-index", "1")

    def test_non_existent_document_raises_search_backend_error(self):
        """Fetching a document that does not exist raises SearchBackendError."""
        with pytest.raises(SearchBackendError):
            self.backend.get_document("products", "does-not-exist")

    def test_returns_deep_copy_not_reference(self):
        """Mutating the returned dict must not affect the stored document."""
        doc = self.backend.get_document("products", "1")
        doc["name"] = "MUTATED"
        assert _documents["products"]["1"]["name"] == "Widget"


# ===========================================================================
# DummyBackend — get_documents
# ===========================================================================


class TestDummyBackendGetDocuments:
    """DummyBackend.get_documents()"""

    def setup_method(self):
        DummyBackend.reset()
        self.backend = DummyBackend()
        self.backend.create_index("articles")
        self.backend.add_documents(
            "articles",
            [
                {"id": "1", "title": "Alpha", "author": "Alice"},
                {"id": "2", "title": "Beta", "author": "Bob"},
                {"id": "3", "title": "Gamma", "author": "Carol"},
                {"id": "4", "title": "Delta", "author": "Dave"},
                {"id": "5", "title": "Epsilon", "author": "Eve"},
            ],
        )

    # Browse mode (document_ids=None)

    def test_browse_returns_up_to_limit_documents(self):
        """Browse mode returns at most `limit` documents."""
        docs = self.backend.get_documents("articles", limit=3)
        assert len(docs) == 3

    def test_browse_respects_offset(self):
        """Browse mode starts from `offset`."""
        all_docs = self.backend.get_documents("articles", limit=5)
        offset_docs = self.backend.get_documents("articles", offset=2, limit=5)
        assert len(offset_docs) == 3
        assert offset_docs[0]["id"] == all_docs[2]["id"]

    def test_browse_default_limit_is_twenty(self):
        """Default limit for browse mode is 20 (returns all 5 when fewer exist)."""
        docs = self.backend.get_documents("articles")
        assert len(docs) == 5

    # ID-based fetch

    def test_id_fetch_returns_only_requested_documents(self):
        """Passing document_ids returns only those documents."""
        docs = self.backend.get_documents("articles", document_ids=["1", "3"])
        ids = [d["id"] for d in docs]
        assert sorted(ids) == ["1", "3"]

    def test_id_fetch_preserves_request_order(self):
        """Documents are returned in the order of document_ids."""
        docs = self.backend.get_documents("articles", document_ids=["3", "1"])
        assert docs[0]["id"] == "3"
        assert docs[1]["id"] == "1"

    def test_id_fetch_silently_skips_missing_ids(self):
        """IDs that are not found are skipped without raising."""
        docs = self.backend.get_documents("articles", document_ids=["1", "missing"])
        assert len(docs) == 1
        assert docs[0]["id"] == "1"

    # fields parameter

    def test_fields_filters_returned_keys(self):
        """fields parameter restricts keys in each returned document (BR-010)."""
        docs = self.backend.get_documents("articles", fields=["title"])
        for doc in docs:
            assert "author" not in doc
            assert "title" in doc

    def test_fields_always_includes_id(self):
        """id is always present even when not listed in fields (BR-010)."""
        docs = self.backend.get_documents("articles", fields=["title"])
        for doc in docs:
            assert "id" in doc

    def test_fields_none_returns_all_keys(self):
        """When fields is None, all stored keys are returned."""
        docs = self.backend.get_documents("articles", document_ids=["1"])
        assert set(docs[0].keys()) == {"id", "title", "author"}

    # Error handling

    def test_non_existent_index_raises_index_not_found_error(self):
        """Non-existent index raises IndexNotFoundError."""
        with pytest.raises(IndexNotFoundError):
            self.backend.get_documents("no-such-index")


# ===========================================================================
# DummyBackend — facet_search
# ===========================================================================


class TestDummyBackendFacetSearch:
    """DummyBackend.facet_search()"""

    def setup_method(self):
        DummyBackend.reset()
        self.backend = DummyBackend()
        self.backend.create_index("products")
        self.backend.add_documents(
            "products",
            [
                {"id": "1", "category": "electronics"},
                {"id": "2", "category": "electronics"},
                {"id": "3", "category": "electronics"},
                {"id": "4", "category": "clothing"},
                {"id": "5", "category": "clothing"},
                {"id": "6", "category": "books"},
            ],
        )

    def test_returns_value_count_dicts(self):
        """Each result is a dict with value and count keys (BR-014)."""
        results = self.backend.facet_search("products", "category")
        for item in results:
            assert "value" in item
            assert "count" in item

    def test_sorted_by_count_descending(self):
        """Results are sorted by count descending (BR-014)."""
        results = self.backend.facet_search("products", "category")
        counts = [item["count"] for item in results]
        assert counts == sorted(counts, reverse=True)

    def test_highest_count_first(self):
        """The most frequent facet value appears first."""
        results = self.backend.facet_search("products", "category")
        assert results[0]["value"] == "electronics"
        assert results[0]["count"] == 3

    def test_facet_query_filters_values_case_insensitively(self):
        """facet_query performs a case-insensitive substring match."""
        results = self.backend.facet_search("products", "category", facet_query="ELECT")
        assert len(results) == 1
        assert results[0]["value"] == "electronics"

    def test_empty_facet_query_returns_all_values(self):
        """An empty facet_query returns all distinct values."""
        results = self.backend.facet_search("products", "category", facet_query="")
        values = {item["value"] for item in results}
        assert values == {"electronics", "clothing", "books"}

    def test_facet_query_no_match_returns_empty_list(self):
        """A facet_query with no matching values returns an empty list."""
        results = self.backend.facet_search("products", "category", facet_query="xyz-no-match")
        assert results == []

    def test_non_existent_index_raises_index_not_found_error(self):
        """Non-existent index raises IndexNotFoundError."""
        with pytest.raises(IndexNotFoundError):
            self.backend.facet_search("no-such-index", "category")

    def test_field_not_present_in_documents_returns_empty_list(self):
        """A facet field absent from all documents returns an empty list."""
        results = self.backend.facet_search("products", "nonexistent_field")
        assert results == []


# ===========================================================================
# DummyBackend — similar_documents
# ===========================================================================


class TestDummyBackendSimilarDocuments:
    """DummyBackend.similar_documents()"""

    def setup_method(self):
        DummyBackend.reset()
        self.backend = DummyBackend()
        self.backend.create_index("articles")
        self.backend.add_documents(
            "articles",
            [
                {"id": "1", "title": "Django Guide"},
                {"id": "2", "title": "Python Cookbook"},
                {"id": "3", "title": "REST API Design"},
            ],
        )

    def test_excludes_source_document(self):
        """The source document is not included in the results."""
        result = self.backend.similar_documents("articles", "1")
        returned_ids = [d["id"] for d in result["hits"]]
        assert "1" not in returned_ids

    def test_includes_other_documents(self):
        """All documents except the source are returned."""
        result = self.backend.similar_documents("articles", "1")
        returned_ids = sorted(d["id"] for d in result["hits"])
        assert returned_ids == ["2", "3"]

    def test_returns_search_result_shaped_dict(self):
        """Return value has the same shape as search() — hits and estimatedTotalHits."""
        result = self.backend.similar_documents("articles", "1")
        assert "hits" in result
        assert "estimatedTotalHits" in result
        assert result["estimatedTotalHits"] == 2

    def test_non_existent_index_raises_index_not_found_error(self):
        """Non-existent index raises IndexNotFoundError."""
        with pytest.raises(IndexNotFoundError):
            self.backend.similar_documents("no-such-index", "1")

    def test_non_existent_source_document_returns_all(self):
        """A non-existent source document ID simply returns all documents."""
        result = self.backend.similar_documents("articles", "does-not-exist")
        assert len(result["hits"]) == 3


# ===========================================================================
# DummyBackend — compact
# ===========================================================================


class TestDummyBackendCompact:
    """DummyBackend.compact()"""

    def setup_method(self):
        DummyBackend.reset()
        self.backend = DummyBackend()
        self.backend.create_index("products")
        self.backend.add_documents("products", [{"id": "1", "name": "Widget"}])

    def test_returns_empty_dict(self):
        """compact() returns an empty dict (no-op, BR-016)."""
        result = self.backend.compact("products")
        assert result == {}

    def test_never_raises_on_existing_index(self):
        """compact() must not raise for a known index (BR-016)."""
        # Should not raise
        self.backend.compact("products")

    def test_never_raises_on_non_existent_index(self):
        """compact() must not raise even for a non-existent index (BR-016)."""
        # Should not raise
        self.backend.compact("does-not-exist")

    def test_does_not_modify_documents(self):
        """compact() is a true no-op — document store is unchanged."""
        before = dict(_documents.get("products", {}))
        self.backend.compact("products")
        after = dict(_documents.get("products", {}))
        assert before == after


# ===========================================================================
# DummyBackend — update_documents
# ===========================================================================


class TestDummyBackendUpdateDocuments:
    """DummyBackend.update_documents()"""

    def setup_method(self):
        DummyBackend.reset()
        self.backend = DummyBackend()
        self.backend.create_index("products")
        self.backend.add_documents(
            "products",
            [{"id": "1", "name": "Widget", "price": 9.99, "stock": 100}],
        )

    def test_partial_update_preserves_unchanged_fields(self):
        """Fields absent from the update dict are preserved (BR-015)."""
        self.backend.update_documents("products", [{"id": "1", "price": 7.99}])
        doc = _documents["products"]["1"]
        assert doc["name"] == "Widget"
        assert doc["stock"] == 100

    def test_partial_update_modifies_supplied_fields(self):
        """Fields present in the update dict are overwritten."""
        self.backend.update_documents("products", [{"id": "1", "price": 7.99}])
        assert _documents["products"]["1"]["price"] == 7.99

    def test_updating_non_existent_document_inserts_it(self):
        """update_documents() inserts the document when the ID does not exist."""
        self.backend.update_documents("products", [{"id": "99", "name": "New"}])
        assert "99" in _documents["products"]
        assert _documents["products"]["99"]["name"] == "New"

    def test_multiple_documents_updated_in_one_call(self):
        """Multiple documents can be updated in a single call."""
        self.backend.add_documents("products", [{"id": "2", "name": "Gadget", "price": 19.99}])
        self.backend.update_documents(
            "products",
            [{"id": "1", "price": 1.00}, {"id": "2", "price": 2.00}],
        )
        assert _documents["products"]["1"]["price"] == 1.00
        assert _documents["products"]["2"]["price"] == 2.00

    def test_returns_task_result_dict(self):
        """update_documents() returns a task-compatible dict."""
        result = self.backend.update_documents("products", [{"id": "1", "price": 5.00}])
        assert "taskUid" in result

    def test_non_existent_index_raises_index_not_found_error(self):
        """Non-existent index raises IndexNotFoundError."""
        with pytest.raises(IndexNotFoundError):
            self.backend.update_documents("no-such-index", [{"id": "1"}])


# ===========================================================================
# Service layer wrappers
# ===========================================================================


class TestServiceGetDocument:
    """get_document() service function."""

    @pytest.mark.django_db
    def test_returns_document_dict(self):
        """get_document() returns the correct document from the engine."""
        index = create_index("products")
        index_documents(index, [{"id": "1", "name": "Widget"}])
        doc = get_document(index, "1")
        assert doc["id"] == "1"
        assert doc["name"] == "Widget"

    @pytest.mark.django_db
    def test_accepts_index_by_name(self):
        """get_document() resolves the index when passed a string name."""
        create_index("products")
        index_documents("products", [{"id": "42", "name": "Anchor"}])
        doc = get_document("products", "42")
        assert doc["id"] == "42"

    @pytest.mark.django_db
    def test_raises_search_backend_error_for_missing_document(self):
        """get_document() propagates SearchBackendError for missing document."""
        index = create_index("products")
        with pytest.raises(SearchBackendError):
            get_document(index, "does-not-exist")

    @pytest.mark.django_db
    def test_calls_backend_with_correct_engine_uid(self):
        """get_document() passes the engine_uid, not the index name, to the backend."""
        index = create_index("products", tenant_id="acme")
        index_documents(index, [{"id": "1", "name": "Widget"}])
        doc = get_document(index, "1")
        assert doc["id"] == "1"


class TestServiceGetDocuments:
    """get_documents() service function."""

    @pytest.mark.django_db
    def test_returns_list_of_documents(self):
        """get_documents() returns a list."""
        index = create_index("articles")
        index_documents(index, [{"id": "1"}, {"id": "2"}])
        docs = get_documents(index)
        assert isinstance(docs, list)

    @pytest.mark.django_db
    def test_browse_mode_returns_up_to_limit(self):
        """get_documents() in browse mode honours the limit parameter."""
        index = create_index("articles")
        index_documents(index, [{"id": str(i)} for i in range(10)])
        docs = get_documents(index, limit=3)
        assert len(docs) == 3

    @pytest.mark.django_db
    def test_id_based_fetch_returns_only_requested_documents(self):
        """get_documents() with document_ids fetches only those IDs."""
        index = create_index("articles")
        index_documents(index, [{"id": "1", "t": "A"}, {"id": "2", "t": "B"}, {"id": "3", "t": "C"}])
        docs = get_documents(index, document_ids=["1", "3"])
        assert sorted(d["id"] for d in docs) == ["1", "3"]

    @pytest.mark.django_db
    def test_fields_parameter_filters_keys(self):
        """get_documents() passes fields through to the backend."""
        index = create_index("articles")
        index_documents(index, [{"id": "1", "title": "Hello", "body": "World"}])
        docs = get_documents(index, fields=["title"])
        assert "body" not in docs[0]
        assert "title" in docs[0]
        assert "id" in docs[0]

    @pytest.mark.django_db
    def test_accepts_index_by_name(self):
        """get_documents() resolves the index when passed a string name."""
        create_index("articles")
        index_documents("articles", [{"id": "1"}])
        docs = get_documents("articles")
        assert len(docs) == 1


class TestServiceFacetSearch:
    """facet_search() service function."""

    @pytest.mark.django_db
    def test_returns_list_of_value_count_dicts(self):
        """facet_search() returns the facet list from the backend."""
        index = create_index("products")
        index_documents(
            index,
            [
                {"id": "1", "category": "books"},
                {"id": "2", "category": "books"},
                {"id": "3", "category": "dvds"},
            ],
        )
        results = facet_search(index, "category")
        assert isinstance(results, list)
        assert results[0]["value"] == "books"
        assert results[0]["count"] == 2

    @pytest.mark.django_db
    def test_facet_query_filters_results(self):
        """facet_search() passes facet_query through to the backend."""
        index = create_index("products")
        index_documents(
            index,
            [
                {"id": "1", "category": "electronics"},
                {"id": "2", "category": "clothing"},
            ],
        )
        results = facet_search(index, "category", facet_query="elec")
        assert len(results) == 1
        assert results[0]["value"] == "electronics"

    @pytest.mark.django_db
    def test_accepts_index_by_name(self):
        """facet_search() resolves the index when passed a string name."""
        create_index("products")
        index_documents("products", [{"id": "1", "category": "books"}])
        results = facet_search("products", "category")
        assert len(results) == 1


class TestServiceSimilarDocuments:
    """similar_documents() service function."""

    @pytest.mark.django_db
    def test_returns_search_result_instance(self):
        """similar_documents() returns a SearchResult."""
        from icv_search.types import SearchResult

        index = create_index("articles")
        index_documents(index, [{"id": "1"}, {"id": "2"}, {"id": "3"}])
        result = similar_documents(index, "1")
        assert isinstance(result, SearchResult)

    @pytest.mark.django_db
    def test_source_document_excluded_from_results(self):
        """similar_documents() does not include the source document in hits."""
        index = create_index("articles")
        index_documents(index, [{"id": "1"}, {"id": "2"}, {"id": "3"}])
        result = similar_documents(index, "1")
        returned_ids = [d["id"] for d in result.hits]
        assert "1" not in returned_ids

    @pytest.mark.django_db
    def test_accepts_index_by_name(self):
        """similar_documents() resolves the index when passed a string name."""
        create_index("articles")
        index_documents("articles", [{"id": "1"}, {"id": "2"}])
        result = similar_documents("articles", "1")
        assert len(result.hits) == 1

    @pytest.mark.django_db
    def test_result_has_estimated_total_hits(self):
        """similar_documents() result exposes estimated_total_hits."""
        index = create_index("articles")
        index_documents(index, [{"id": "1"}, {"id": "2"}, {"id": "3"}])
        result = similar_documents(index, "1")
        assert result.estimated_total_hits == 2


class TestServiceCompactIndex:
    """compact_index() service function."""

    @pytest.mark.django_db
    def test_returns_empty_dict_for_dummy_backend(self):
        """compact_index() returns {} for the DummyBackend (no-op, BR-016)."""
        index = create_index("products")
        result = compact_index(index)
        assert result == {}

    @pytest.mark.django_db
    def test_never_raises(self):
        """compact_index() must never raise an error (BR-016)."""
        index = create_index("products")
        # Should not raise
        compact_index(index)

    @pytest.mark.django_db
    def test_accepts_index_by_name(self):
        """compact_index() resolves the index when passed a string name."""
        create_index("products")
        result = compact_index("products")
        assert result == {}


class TestServiceUpdateDocuments:
    """update_documents() service function."""

    @pytest.mark.django_db
    def test_returns_task_result(self):
        """update_documents() returns a TaskResult."""
        from icv_search.types import TaskResult

        index = create_index("products")
        index_documents(index, [{"id": "1", "name": "Widget", "price": 9.99}])
        result = update_documents(index, [{"id": "1", "price": 7.99}])
        assert isinstance(result, TaskResult)

    @pytest.mark.django_db
    def test_partial_update_preserves_existing_fields(self):
        """update_documents() preserves fields not in the update dict (BR-015)."""
        index = create_index("products")
        index_documents(index, [{"id": "1", "name": "Widget", "price": 9.99}])
        update_documents(index, [{"id": "1", "price": 4.99}])
        doc = _documents[index.engine_uid]["1"]
        assert doc["name"] == "Widget"
        assert doc["price"] == 4.99

    @pytest.mark.django_db
    def test_inserting_new_document_via_update(self):
        """update_documents() inserts a document that does not yet exist."""
        index = create_index("products")
        update_documents(index, [{"id": "new-1", "name": "Novelty"}])
        assert "new-1" in _documents[index.engine_uid]

    @pytest.mark.django_db
    def test_accepts_index_by_name(self):
        """update_documents() resolves the index when passed a string name."""
        create_index("products")
        index_documents("products", [{"id": "1", "name": "Widget"}])
        update_documents("products", [{"id": "1", "name": "Updated Widget"}])
        from icv_search.models import SearchIndex

        index = SearchIndex.objects.get(name="products")
        assert _documents[index.engine_uid]["1"]["name"] == "Updated Widget"


# ===========================================================================
# BaseSearchBackend defaults
# ===========================================================================


class TestBaseSearchBackendDefaults:
    """Verify the default/fallback behaviour defined on BaseSearchBackend."""

    def _make_concrete(self):
        """Return a minimal concrete subclass that only implements abstractmethods."""
        from icv_search.backends.base import BaseSearchBackend

        class _MinimalBackend(BaseSearchBackend):
            def create_index(self, uid, primary_key="id"):
                return {}

            def delete_index(self, uid):
                pass

            def update_settings(self, uid, settings):
                return {}

            def get_settings(self, uid):
                return {}

            def add_documents(self, uid, documents, primary_key="id"):
                # Store in a simple dict so get_document fallback can be tested
                if not hasattr(self, "_store"):
                    self._store = {}
                if uid not in self._store:
                    self._store[uid] = {}
                for doc in documents:
                    self._store[uid][str(doc.get(primary_key, ""))] = doc
                return {"taskUid": "t1"}

            def delete_documents(self, uid, document_ids):
                return {}

            def clear_documents(self, uid):
                return {}

            def search(self, uid, query, **params):
                return {"hits": [], "query": query, "estimatedTotalHits": 0}

            def get_stats(self, uid):
                return {"numberOfDocuments": 0}

            def health(self):
                return True

        return _MinimalBackend(url="", api_key="")

    def test_get_document_raises_not_implemented_error(self):
        """BaseSearchBackend.get_document() raises NotImplementedError by default."""
        backend = self._make_concrete()
        with pytest.raises(NotImplementedError):
            backend.get_document("products", "1")

    def test_facet_search_raises_not_implemented_error(self):
        """BaseSearchBackend.facet_search() raises NotImplementedError by default."""
        backend = self._make_concrete()
        with pytest.raises(NotImplementedError):
            backend.facet_search("products", "category")

    def test_similar_documents_raises_not_implemented_error(self):
        """BaseSearchBackend.similar_documents() raises NotImplementedError by default."""
        backend = self._make_concrete()
        with pytest.raises(NotImplementedError):
            backend.similar_documents("products", "1")

    def test_compact_returns_empty_dict(self):
        """BaseSearchBackend.compact() returns {} by default (BR-016)."""
        backend = self._make_concrete()
        result = backend.compact("products")
        assert result == {}

    def test_compact_never_raises(self):
        """BaseSearchBackend.compact() must never raise."""
        backend = self._make_concrete()
        # Should not raise on any uid
        backend.compact("any-index")
        backend.compact("")

    def test_update_documents_falls_back_to_add_documents(self):
        """BaseSearchBackend.update_documents() falls back to add_documents() (BR-015)."""
        backend = self._make_concrete()
        docs = [{"id": "1", "name": "Widget"}]
        result = backend.update_documents("products", docs)
        # The fallback delegates to add_documents, which returns {"taskUid": "t1"}
        assert result == {"taskUid": "t1"}

    def test_get_documents_with_ids_calls_get_document_in_loop(self):
        """BaseSearchBackend.get_documents() with IDs calls get_document() for each ID.

        The default implementation on BaseSearchBackend raises NotImplementedError
        for get_document, so we override it on a subclass to verify the loop.
        """
        from icv_search.backends.base import BaseSearchBackend

        class _LoopCheckBackend(BaseSearchBackend):
            def create_index(self, uid, primary_key="id"):
                return {}

            def delete_index(self, uid):
                pass

            def update_settings(self, uid, settings):
                return {}

            def get_settings(self, uid):
                return {}

            def add_documents(self, uid, documents, primary_key="id"):
                return {"taskUid": "t1"}

            def delete_documents(self, uid, document_ids):
                return {}

            def clear_documents(self, uid):
                return {}

            def search(self, uid, query, **params):
                return {"hits": [], "query": query, "estimatedTotalHits": 0}

            def get_stats(self, uid):
                return {}

            def health(self):
                return True

            def get_document(self, uid, document_id):
                return {"id": document_id, "title": f"doc-{document_id}"}

        backend = _LoopCheckBackend(url="", api_key="")
        docs = backend.get_documents("any-index", document_ids=["A", "B", "C"])
        assert len(docs) == 3
        assert {d["id"] for d in docs} == {"A", "B", "C"}

    def test_get_documents_browse_mode_raises_not_implemented_error(self):
        """BaseSearchBackend.get_documents() in browse mode (no IDs) raises NotImplementedError."""
        backend = self._make_concrete()
        with pytest.raises(NotImplementedError):
            backend.get_documents("products")

    def test_get_documents_with_ids_and_fields_filters_keys(self):
        """get_documents() default implementation applies fields filtering."""
        from icv_search.backends.base import BaseSearchBackend

        class _FieldBackend(BaseSearchBackend):
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
                return {"hits": [], "query": query, "estimatedTotalHits": 0}

            def get_stats(self, uid):
                return {}

            def health(self):
                return True

            def get_document(self, uid, document_id):
                return {"id": document_id, "name": "Widget", "price": 9.99}

        backend = _FieldBackend(url="", api_key="")
        docs = backend.get_documents("any-index", document_ids=["1"], fields=["name"])
        assert "price" not in docs[0]
        assert "name" in docs[0]
        assert "id" in docs[0]

    def test_get_documents_with_ids_skips_failed_fetches(self, caplog):
        """get_documents() skips IDs whose get_document raises, and logs the failure."""
        from icv_search.backends.base import BaseSearchBackend

        class _PartialBackend(BaseSearchBackend):
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
                return {"hits": [], "query": query, "estimatedTotalHits": 0}

            def get_stats(self, uid):
                return {}

            def health(self):
                return True

            def get_document(self, uid, document_id):
                if document_id == "missing":
                    raise SearchBackendError("not found")
                return {"id": document_id}

        backend = _PartialBackend(url="", api_key="")
        with caplog.at_level(logging.WARNING, logger="icv_search.backends.base"):
            docs = backend.get_documents("any-index", document_ids=["1", "missing", "2"])
        ids = {d["id"] for d in docs}
        assert ids == {"1", "2"}
        assert any("Failed to fetch document" in r.message and "missing" in r.message for r in caplog.records)
