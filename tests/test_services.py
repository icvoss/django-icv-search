"""Tests for icv-search service functions."""

from __future__ import annotations

import pytest

from icv_search.backends import get_search_backend, reset_search_backend
from icv_search.backends.base import BaseSearchBackend
from icv_search.backends.dummy import DummyBackend, _documents, _indexes
from icv_search.exceptions import IndexNotFoundError, SearchBackendError
from icv_search.models import IndexSyncLog, SearchIndex
from icv_search.services import (
    IndexStats,
    SearchResult,
    TaskResult,
    create_index,
    delete_index,
    get_index_stats,
    index_documents,
    remove_documents,
    search,
    update_index_settings,
)


@pytest.fixture(autouse=True)
def use_dummy_backend(settings):
    """Use DummyBackend for all service tests."""
    settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
    settings.ICV_SEARCH_AUTO_SYNC = False
    reset_search_backend()
    DummyBackend.reset()
    yield
    DummyBackend.reset()
    reset_search_backend()


class TestCreateIndex:
    """create_index() service function."""

    @pytest.mark.django_db
    def test_creates_search_index_record(self):
        create_index("products")
        assert SearchIndex.objects.filter(name="products").exists()

    @pytest.mark.django_db
    def test_provisions_index_in_engine(self):
        index = create_index("products")
        assert index.engine_uid in _indexes

    @pytest.mark.django_db
    def test_marks_index_as_synced(self):
        index = create_index("products")
        index.refresh_from_db()
        assert index.is_synced is True

    @pytest.mark.django_db
    def test_creates_sync_log_entry(self):
        index = create_index("products")
        log = IndexSyncLog.objects.filter(index=index, action="created").first()
        assert log is not None
        assert log.status == "success"

    @pytest.mark.django_db
    def test_applies_initial_settings(self):
        settings_data = {"searchableAttributes": ["name", "description"]}
        index = create_index("products", settings=settings_data)
        stored = DummyBackend().get_settings(index.engine_uid)
        assert stored.get("searchableAttributes") == ["name", "description"]

    @pytest.mark.django_db
    def test_creates_index_with_tenant(self):
        index = create_index("products", tenant_id="acme")
        assert index.tenant_id == "acme"
        assert index.engine_uid == "acme_products"

    @pytest.mark.django_db
    def test_creates_log_with_failed_status_on_engine_error(self):
        backend = get_search_backend()
        original = backend.create_index
        backend.create_index = lambda *a, **kw: (_ for _ in ()).throw(SearchBackendError("connection refused"))

        with pytest.raises(SearchBackendError):
            create_index("broken")

        log = IndexSyncLog.objects.filter(action="created").first()
        assert log is not None
        assert log.status == "failed"

        backend.create_index = original


class _PostgresLikeBackend(BaseSearchBackend):
    """Reproduces PostgresBackend's real ordering contract without a DB.

    ``update_settings()`` raises ``IndexNotFoundError`` unless
    ``create_index()`` has already been called for that uid, exactly like
    ``icv_search.backends.postgres.PostgresBackend.update_settings()``
    (``UPDATE ... WHERE index_uid = %s`` with ``rowcount == 0``).
    """

    _created_uids: set[str] = set()

    def __init__(self, url: str = "", api_key: str = "", timeout: int = 30, **kwargs):
        super().__init__(url=url, api_key=api_key, timeout=timeout)

    @classmethod
    def reset(cls) -> None:
        cls._created_uids = set()

    def create_index(self, uid, primary_key="id"):
        self._created_uids.add(uid)
        return {"taskUid": f"fake-create-{uid}", "indexUid": uid, "status": "succeeded"}

    def update_settings(self, uid, settings):
        if uid not in self._created_uids:
            raise IndexNotFoundError(f"Index '{uid}' not found.")
        return {"taskUid": f"fake-settings-{uid}"}

    def delete_index(self, uid):
        self._created_uids.discard(uid)

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

    def health(self):
        return True

    def get_stats(self, uid):
        return {}


class TestCreateIndexEngineProvisioningOrder:
    """Regression tests for #10: create_index() must provision the engine
    index before the Django row's post_save signal tries to sync settings
    to it, or backends whose update_settings() requires the index to
    already exist (PostgresBackend) raise IndexNotFoundError on every
    first-ever create_index() call for a name.
    """

    @pytest.fixture(autouse=True)
    def _use_postgres_like_backend(self, settings):
        settings.ICV_SEARCH_BACKEND = "tests.test_services._PostgresLikeBackend"
        # The exact conditions from #10: auto-sync on, synchronous (no Celery).
        settings.ICV_SEARCH_AUTO_SYNC = True
        settings.ICV_SEARCH_ASYNC_INDEXING = False
        reset_search_backend()
        _PostgresLikeBackend.reset()
        yield
        _PostgresLikeBackend.reset()
        reset_search_backend()

    @pytest.mark.django_db
    def test_first_create_index_call_does_not_raise_index_not_found(self):
        """The exact #10 repro: create_index() must not raise IndexNotFoundError
        on the very first call for a name, when AUTO_SYNC fires update_settings()
        synchronously via post_save before this fix's reordering."""
        index = create_index("cms_pages", settings={"searchableAttributes": ["title"]})

        assert index.is_synced is True

    @pytest.mark.django_db
    def test_first_create_index_call_marks_success_log(self):
        index = create_index("cms_pages", settings={"searchableAttributes": ["title"]})

        log = IndexSyncLog.objects.filter(index=index, action="created").first()
        assert log is not None
        assert log.status == "success"

    @pytest.mark.django_db
    def test_engine_index_exists_before_settings_sync_is_attempted(self):
        """backend.create_index() must run, and complete, before any
        update_settings() call for the same uid (whether from the signal
        or from create_index()'s own explicit sync)."""
        index = create_index("cms_pages", settings={"searchableAttributes": ["title"]})

        assert index.engine_uid in _PostgresLikeBackend._created_uids

    @pytest.mark.django_db
    def test_create_index_engine_failure_still_saves_index_and_failed_log(self):
        """BR-007: every backend operation is logged, even when
        backend.create_index() itself fails outright (not just
        update_settings racing it)."""
        reset_search_backend()
        backend = get_search_backend()
        backend.create_index = lambda *a, **kw: (_ for _ in ()).throw(SearchBackendError("connection refused"))

        with pytest.raises(SearchBackendError):
            create_index("broken")

        log = IndexSyncLog.objects.filter(action="created").first()
        assert log is not None
        assert log.status == "failed"
        assert SearchIndex.objects.filter(name="broken").exists()


class TestDeleteIndex:
    """delete_index() service function."""

    @pytest.mark.django_db
    def test_removes_index_from_database(self):
        index = create_index("products")
        delete_index(index)
        assert not SearchIndex.objects.filter(pk=index.pk).exists()

    @pytest.mark.django_db
    def test_removes_index_from_engine(self):
        index = create_index("products")
        uid = index.engine_uid
        delete_index(index)
        assert uid not in _indexes

    @pytest.mark.django_db
    def test_creates_deleted_log_entry(self):
        index = create_index("products")

        # Track log creation before delete cascades it
        received = []
        from icv_search.signals import search_index_deleted

        def handler(sender, instance, **kwargs):
            # Capture the log at the moment of signal emission (before cascade delete)
            log = IndexSyncLog.objects.filter(index=instance, action="deleted").first()
            if log:
                received.append(log.status)

        search_index_deleted.connect(handler)
        try:
            delete_index(index)
        finally:
            search_index_deleted.disconnect(handler)

        # Verify signal fired and log was in success state
        assert len(received) == 1
        assert received[0] == "success"

    @pytest.mark.django_db
    def test_resolves_index_by_name(self):
        create_index("articles")
        delete_index("articles")
        assert not SearchIndex.objects.filter(name="articles").exists()


class TestUpdateIndexSettings:
    """update_index_settings() service function."""

    @pytest.mark.django_db
    def test_updates_settings_in_database(self):
        index = create_index("products")
        update_index_settings(index, {"searchableAttributes": ["name"]})
        index.refresh_from_db()
        assert index.settings.get("searchableAttributes") == ["name"]

    @pytest.mark.django_db
    def test_pushes_settings_to_engine(self):
        index = create_index("products")
        update_index_settings(index, {"searchableAttributes": ["name"]})
        stored = DummyBackend().get_settings(index.engine_uid)
        assert stored.get("searchableAttributes") == ["name"]

    @pytest.mark.django_db
    def test_merges_with_existing_settings(self):
        index = create_index("products", settings={"searchableAttributes": ["name"]})
        update_index_settings(index, {"filterableAttributes": ["category"]})
        index.refresh_from_db()
        assert "searchableAttributes" in index.settings
        assert "filterableAttributes" in index.settings

    @pytest.mark.django_db
    def test_creates_settings_updated_log(self):
        index = create_index("products")
        update_index_settings(index, {"searchableAttributes": ["name"]})
        log = IndexSyncLog.objects.filter(action="settings_updated").first()
        assert log is not None
        assert log.status == "success"


class TestGetIndexStats:
    """get_index_stats() service function."""

    @pytest.mark.django_db
    def test_returns_index_stats_instance(self):
        index = create_index("products")
        stats = get_index_stats(index)
        assert isinstance(stats, IndexStats)

    @pytest.mark.django_db
    def test_returns_document_count(self):
        index = create_index("products")
        backend = get_search_backend()
        backend.add_documents(index.engine_uid, [{"id": "1"}, {"id": "2"}])
        stats = get_index_stats(index)
        assert stats.document_count == 2

    @pytest.mark.django_db
    def test_raw_field_preserves_engine_response(self):
        index = create_index("products")
        stats = get_index_stats(index)
        assert "numberOfDocuments" in stats.raw

    @pytest.mark.django_db
    def test_resolves_index_by_name(self):
        create_index("articles")
        stats = get_index_stats("articles")
        assert isinstance(stats, IndexStats)


class TestIndexDocuments:
    """index_documents() service function."""

    @pytest.mark.django_db
    def test_adds_documents_to_engine(self):
        index = create_index("products")
        docs = [{"id": "1", "name": "Widget"}, {"id": "2", "name": "Gadget"}]
        index_documents(index, docs)
        assert "1" in _documents[index.engine_uid]
        assert "2" in _documents[index.engine_uid]

    @pytest.mark.django_db
    def test_creates_documents_added_log(self):
        index = create_index("products")
        index_documents(index, [{"id": "1"}])
        log = IndexSyncLog.objects.filter(action="documents_added").first()
        assert log is not None
        assert log.status == "success"

    @pytest.mark.django_db
    def test_log_detail_includes_document_count(self):
        index = create_index("products")
        index_documents(index, [{"id": "1"}, {"id": "2"}, {"id": "3"}])
        log = IndexSyncLog.objects.filter(action="documents_added").first()
        assert "3" in log.detail

    @pytest.mark.django_db
    def test_returns_task_result_instance(self):
        index = create_index("products")
        result = index_documents(index, [{"id": "1"}])
        assert isinstance(result, TaskResult)

    @pytest.mark.django_db
    def test_returns_task_uid(self):
        index = create_index("products")
        result = index_documents(index, [{"id": "1"}])
        assert result.task_uid != ""

    @pytest.mark.django_db
    def test_raw_field_preserves_engine_response(self):
        index = create_index("products")
        result = index_documents(index, [{"id": "1"}])
        assert "taskUid" in result.raw

    @pytest.mark.django_db
    def test_resolves_index_by_name(self):
        create_index("articles")
        index_documents("articles", [{"id": "1", "title": "Hello"}])
        index = SearchIndex.objects.get(name="articles")
        assert "1" in _documents[index.engine_uid]


class TestRemoveDocuments:
    """remove_documents() service function."""

    @pytest.mark.django_db
    def test_removes_documents_from_engine(self):
        index = create_index("products")
        index_documents(index, [{"id": "1"}, {"id": "2"}])
        remove_documents(index, ["1"])
        assert "1" not in _documents[index.engine_uid]
        assert "2" in _documents[index.engine_uid]

    @pytest.mark.django_db
    def test_creates_documents_deleted_log(self):
        index = create_index("products")
        remove_documents(index, ["1", "2"])
        log = IndexSyncLog.objects.filter(action="documents_deleted").first()
        assert log is not None
        assert log.status == "success"

    @pytest.mark.django_db
    def test_returns_task_result_instance(self):
        index = create_index("products")
        result = remove_documents(index, ["1"])
        assert isinstance(result, TaskResult)

    @pytest.mark.django_db
    def test_returns_task_uid(self):
        index = create_index("products")
        result = remove_documents(index, ["1"])
        assert result.task_uid != ""

    @pytest.mark.django_db
    def test_raw_field_preserves_engine_response(self):
        index = create_index("products")
        result = remove_documents(index, ["1"])
        assert "taskUid" in result.raw


class TestSearchService:
    """search() service function."""

    @pytest.mark.django_db
    def test_returns_search_result_instance(self):
        index = create_index("articles")
        result = search(index, "")
        assert isinstance(result, SearchResult)

    @pytest.mark.django_db
    def test_returns_matching_hits(self):
        index = create_index("articles")
        index_documents(
            index,
            [
                {"id": "1", "title": "Django for Beginners"},
                {"id": "2", "title": "Python Data Science"},
            ],
        )
        result = search(index, "Django")
        assert len(result.hits) == 1
        assert result.hits[0]["id"] == "1"

    @pytest.mark.django_db
    def test_accepts_index_by_name(self):
        create_index("articles")
        index_documents("articles", [{"id": "1", "title": "Test"}])
        result = search("articles", "Test")
        assert len(result.hits) == 1

    @pytest.mark.django_db
    def test_passes_params_to_backend(self):
        index = create_index("articles")
        index_documents(index, [{"id": str(i), "title": f"Article {i}"} for i in range(10)])
        result = search(index, "", limit=3)
        assert len(result.hits) == 3

    @pytest.mark.django_db
    def test_empty_query_returns_all(self):
        index = create_index("articles")
        index_documents(index, [{"id": "1"}, {"id": "2"}])
        result = search(index, "")
        assert result.estimated_total_hits == 2

    @pytest.mark.django_db
    def test_query_field_is_normalised(self):
        index = create_index("articles")
        result = search(index, "hello")
        assert result.query == "hello"

    @pytest.mark.django_db
    def test_raw_field_preserves_engine_response(self):
        index = create_index("articles")
        result = search(index, "")
        assert "hits" in result.raw
        assert "estimatedTotalHits" in result.raw


class TestGetModelSearchSettings:
    """get_model_search_settings() helper."""

    def test_returns_empty_dict_for_none(self):
        from icv_search.services import get_model_search_settings

        assert get_model_search_settings(None) == {}

    def test_returns_empty_dict_for_plain_class(self):
        from icv_search.services import get_model_search_settings

        class Plain:
            pass

        assert get_model_search_settings(Plain) == {}

    def test_extracts_filterable_fields(self):
        from icv_search.services import get_model_search_settings

        class MyModel:
            search_filterable_fields = ["category", "is_active"]
            search_sortable_fields = []
            search_fields = []

        result = get_model_search_settings(MyModel)
        assert result["filterableAttributes"] == ["category", "is_active"]
        assert "sortableAttributes" not in result

    def test_extracts_sortable_fields(self):
        from icv_search.services import get_model_search_settings

        class MyModel:
            search_filterable_fields = []
            search_sortable_fields = ["price", "created_at"]
            search_fields = []

        result = get_model_search_settings(MyModel)
        assert result["sortableAttributes"] == ["price", "created_at"]
        assert "filterableAttributes" not in result

    def test_extracts_searchable_fields(self):
        from icv_search.services import get_model_search_settings

        class MyModel:
            search_filterable_fields = []
            search_sortable_fields = []
            search_fields = ["title", "body"]

        result = get_model_search_settings(MyModel)
        assert result["searchableAttributes"] == ["title", "body"]

    def test_extracts_all_field_types_together(self):
        from search_testapp.models import Article

        from icv_search.services import get_model_search_settings

        result = get_model_search_settings(Article)
        assert result["filterableAttributes"] == ["author", "is_published"]
        assert result["sortableAttributes"] == ["created_at"]
        assert result["searchableAttributes"] == ["title", "body", "author"]

    def test_returns_list_copies_not_references(self):
        """Mutating the returned lists must not affect the class attribute."""
        from icv_search.services import get_model_search_settings

        class MyModel:
            search_filterable_fields = ["a"]
            search_sortable_fields = []
            search_fields = []

        result = get_model_search_settings(MyModel)
        result["filterableAttributes"].append("injected")
        assert MyModel.search_filterable_fields == ["a"]


class TestCreateIndexWithModelClass:
    """create_index() model_class parameter."""

    @pytest.mark.django_db
    def test_seeds_settings_from_model_class(self):
        from search_testapp.models import Article

        index = create_index("articles", model_class=Article)
        index.refresh_from_db()
        assert index.settings.get("filterableAttributes") == ["author", "is_published"]
        assert index.settings.get("sortableAttributes") == ["created_at"]
        assert index.settings.get("searchableAttributes") == ["title", "body", "author"]

    @pytest.mark.django_db
    def test_explicit_settings_override_model_class(self):
        """Caller-supplied settings must win over mixin-derived values."""
        from search_testapp.models import Article

        override = {"filterableAttributes": ["custom_field"]}
        index = create_index("articles", model_class=Article, settings=override)
        index.refresh_from_db()
        assert index.settings["filterableAttributes"] == ["custom_field"]
        # Other mixin-derived keys are still present
        assert "sortableAttributes" in index.settings

    @pytest.mark.django_db
    def test_model_class_settings_pushed_to_engine(self):
        """Engine must receive the mixin-derived settings."""
        from search_testapp.models import Article

        index = create_index("articles", model_class=Article)
        stored = DummyBackend().get_settings(index.engine_uid)
        assert stored.get("filterableAttributes") == ["author", "is_published"]

    @pytest.mark.django_db
    def test_no_model_class_behaves_as_before(self):
        """Omitting model_class preserves existing behaviour."""
        index = create_index("products", settings={"searchableAttributes": ["name"]})
        index.refresh_from_db()
        assert index.settings == {"searchableAttributes": ["name"]}

    @pytest.mark.django_db
    def test_model_class_none_behaves_as_before(self):
        """Explicitly passing model_class=None preserves existing behaviour."""
        index = create_index("products", model_class=None)
        index.refresh_from_db()
        assert index.settings == {}


class TestResolveIndexAutoCreate:
    """resolve_index() auto-creates a SearchIndex when none exists."""

    @pytest.mark.django_db
    def test_auto_creates_index_on_search(self):
        """Calling search() with a name that has no SearchIndex record auto-creates it."""
        result = search("products", "test")
        assert SearchIndex.objects.filter(name="products").exists()
        assert result.hits == []

    @pytest.mark.django_db
    def test_auto_creates_index_on_index_documents(self):
        """index_documents() auto-creates the SearchIndex record."""
        index_documents("widgets", [{"id": "1", "name": "Widget"}])
        assert SearchIndex.objects.filter(name="widgets").exists()

    @pytest.mark.django_db
    def test_auto_create_uses_model_class_from_config(self, settings):
        """When ICV_SEARCH_AUTO_INDEX is configured, model class settings are applied."""
        settings.ICV_SEARCH_AUTO_INDEX = {
            "articles": {
                "model": "search_testapp.Article",
            },
        }
        search("articles", "test")
        index = SearchIndex.objects.get(name="articles")
        assert "searchableAttributes" in index.settings
        assert "title" in index.settings["searchableAttributes"]

    @pytest.mark.django_db
    def test_auto_create_without_config_creates_bare_index(self, settings):
        """Without ICV_SEARCH_AUTO_INDEX config, a bare index is created."""
        settings.ICV_SEARCH_AUTO_INDEX = {}
        search("bare_index", "test")
        index = SearchIndex.objects.get(name="bare_index")
        assert index.settings == {}

    @pytest.mark.django_db
    def test_existing_index_is_returned_without_recreation(self):
        """resolve_index returns the existing record when it already exists."""
        create_index("products")
        search("products", "test")
        assert SearchIndex.objects.filter(name="products").count() == 1
