"""Tests for the delete_document() convenience service function."""

from __future__ import annotations

import pytest

from icv_search.backends import reset_search_backend
from icv_search.backends.dummy import DummyBackend
from icv_search.services import (
    create_index,
    delete_document,
    delete_documents_by_filter,
    index_documents,
    search,
)
from icv_search.types import TaskResult


@pytest.fixture(autouse=True)
def use_dummy_backend(settings):
    settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
    settings.ICV_SEARCH_AUTO_SYNC = False
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
            {"id": "1", "title": "Running Shoes"},
            {"id": "2", "title": "Tennis Racket"},
            {"id": "3", "title": "Golf Club"},
        ],
    )
    return index


class TestDeleteDocument:
    """delete_document() service function (BR-011)."""

    @pytest.mark.django_db
    def test_returns_task_result(self, products_index):
        result = delete_document("products", "1")
        assert isinstance(result, TaskResult)

    @pytest.mark.django_db
    def test_removes_single_document(self, products_index):
        delete_document("products", "1")
        result = search("products", "", limit=10)
        ids = [h["id"] for h in result.hits]
        assert "1" not in ids
        assert "2" in ids
        assert "3" in ids

    @pytest.mark.django_db
    def test_other_documents_unaffected(self, products_index):
        delete_document("products", "2")
        result = search("products", "", limit=10)
        assert len(result.hits) == 2
        ids = [h["id"] for h in result.hits]
        assert "1" in ids
        assert "3" in ids

    @pytest.mark.django_db
    def test_accepts_index_instance(self, products_index):
        delete_document(products_index, "1")
        result = search("products", "", limit=10)
        ids = [h["id"] for h in result.hits]
        assert "1" not in ids

    @pytest.mark.django_db
    def test_with_tenant_id(self):
        create_index("products", tenant_id="t1")
        index_documents("products", [{"id": "1", "title": "A"}], tenant_id="t1")

        delete_document("products", "1", tenant_id="t1")
        result = search("products", "", tenant_id="t1", limit=10)
        assert len(result.hits) == 0

    @pytest.mark.django_db
    def test_nonexistent_document_does_not_raise(self, products_index):
        """Deleting a document that doesn't exist should not raise."""
        result = delete_document("products", "nonexistent")
        assert isinstance(result, TaskResult)


class TestDeleteDocumentExport:
    """delete_document is correctly exported."""

    def test_importable_from_services(self):
        from icv_search.services import delete_document as dd

        assert callable(dd)

    def test_in_services_all(self):
        from icv_search.services import __all__

        assert "delete_document" in __all__


class TestSearchQueryLogFactoryExport:
    """SearchQueryLogFactory is correctly exported from testing."""

    def test_importable_from_testing(self):
        from icv_search.testing import SearchQueryLogFactory

        assert SearchQueryLogFactory is not None

    def test_in_testing_all(self):
        from icv_search.testing import __all__

        assert "SearchQueryLogFactory" in __all__


class TestDeleteDocumentsByFilterFallback:
    """delete_documents_by_filter works on backends without a native endpoint.

    Regression: the base backend raised NotImplementedError, so DummyBackend
    and PostgresBackend could not satisfy the documented interface (LSP
    violation). The base now composes search() + delete_documents().
    """

    @pytest.mark.django_db
    def test_filter_delete_removes_matching_docs(self, products_index):
        # Dummy backend has no native filter-delete — exercises the fallback.
        result = delete_documents_by_filter(products_index, {"title": "Tennis Racket"})
        assert isinstance(result, TaskResult)

        remaining = {hit["id"] for hit in search(products_index, "").hits}
        assert remaining == {"1", "3"}

    @pytest.mark.django_db
    def test_filter_delete_no_match_is_noop(self, products_index):
        delete_documents_by_filter(products_index, {"title": "Nonexistent"})
        remaining = {hit["id"] for hit in search(products_index, "").hits}
        assert remaining == {"1", "2", "3"}

    def test_backend_method_does_not_raise_not_implemented(self):
        from icv_search.backends import get_search_backend

        backend = get_search_backend()
        backend.create_index("widgets")
        backend.add_documents(
            "widgets",
            [{"id": "a", "kind": "x"}, {"id": "b", "kind": "y"}],
        )
        # Must not raise NotImplementedError.
        backend.delete_documents_by_filter("widgets", {"kind": "x"})
        ids = {d["id"] for d in backend.search("widgets", "")["hits"]}
        assert ids == {"b"}

    def test_fallback_paginates_past_scan_limit(self, monkeypatch):
        from icv_search.backends import get_search_backend

        backend = get_search_backend()
        # Force a tiny scan window so the pagination loop runs more than once.
        monkeypatch.setattr(type(backend), "_FILTER_DELETE_SCAN_LIMIT", 2, raising=False)

        backend.create_index("bulk")
        backend.add_documents(
            "bulk",
            [{"id": str(i), "drop": True} for i in range(5)],
        )
        backend.delete_documents_by_filter("bulk", {"drop": True})
        assert backend.search("bulk", "")["hits"] == []
