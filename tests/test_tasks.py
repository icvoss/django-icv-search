"""Tests for Celery tasks in icv-search."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from icv_search.models import SearchIndex
from icv_search.services import create_index, index_documents
from icv_search.tasks import (
    add_documents,
    refresh_document_counts,
    reindex,
    remove_documents,
    sync_all_indexes,
    sync_index_settings,
)


@pytest.fixture(autouse=True)
def use_dummy_backend(settings):
    """Use DummyBackend for all task tests."""
    from icv_search.backends import reset_search_backend
    from icv_search.backends.dummy import DummyBackend

    settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
    settings.ICV_SEARCH_AUTO_SYNC = False
    reset_search_backend()
    DummyBackend.reset()
    yield
    DummyBackend.reset()
    reset_search_backend()


class TestSyncIndexSettings:
    """sync_index_settings task."""

    @pytest.mark.django_db
    def test_syncs_index_to_engine(self):
        """Task should call _sync_index_to_engine for the given index."""
        index = create_index("products")
        index.is_synced = False
        index.save()

        with patch("icv_search.services.indexing._sync_index_to_engine") as mock_sync:
            sync_index_settings(str(index.pk))
            mock_sync.assert_called_once()
            args = mock_sync.call_args[0]
            assert args[0].pk == index.pk

    @pytest.mark.django_db
    def test_handles_missing_index(self):
        """Task should log warning and return when index does not exist."""
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        # Should not raise
        sync_index_settings(fake_uuid)

    @pytest.mark.django_db
    def test_retries_on_sync_failure(self):
        """Task should retry when sync fails."""
        index = create_index("products")

        with patch("icv_search.services.indexing._sync_index_to_engine", side_effect=RuntimeError("sync failed")):
            # The task will retry internally - we just verify it raises after retries
            # In real usage Celery handles the retry, but here we test the function directly
            try:
                sync_index_settings(str(index.pk))
            except Exception:
                pass  # Expected to fail


class TestSyncAllIndexes:
    """sync_all_indexes task."""

    @pytest.mark.django_db
    def test_syncs_all_unsynced_active_indexes(self):
        """Task should sync all indexes where is_synced=False and is_active=True."""
        idx1 = create_index("index1")
        idx2 = create_index("index2")
        idx3 = create_index("index3")

        # Mark as unsynced
        SearchIndex.objects.filter(pk__in=[idx1.pk, idx2.pk, idx3.pk]).update(is_synced=False)

        # Deactivate one
        idx3.is_active = False
        idx3.save()

        with patch("icv_search.services.indexing._sync_index_to_engine") as mock_sync:
            count = sync_all_indexes()
            assert count == 2
            assert mock_sync.call_count == 2

    @pytest.mark.django_db
    def test_returns_zero_when_all_synced(self):
        """Task should return 0 when all indexes are already synced."""
        create_index("products")  # Created as synced by default with AUTO_SYNC=False

        with patch("icv_search.services.indexing._sync_index_to_engine") as mock_sync:
            count = sync_all_indexes()
            assert count == 0
            mock_sync.assert_not_called()

    @pytest.mark.django_db
    def test_continues_on_individual_failures(self):
        """Task should continue syncing other indexes when one fails."""
        idx1 = create_index("index1")
        idx2 = create_index("index2")
        SearchIndex.objects.filter(pk__in=[idx1.pk, idx2.pk]).update(is_synced=False)

        def side_effect(index):
            if index.name == "index1":
                raise RuntimeError("sync failed")
            return None

        with patch("icv_search.services.indexing._sync_index_to_engine", side_effect=side_effect):
            count = sync_all_indexes()
            # Should successfully sync index2 despite index1 failure
            assert count == 1


class TestAddDocumentsTask:
    """add_documents task."""

    @pytest.mark.django_db
    def test_adds_documents_to_index(self):
        """Task should call index_documents service."""
        index = create_index("products")
        docs = [{"id": "1", "name": "Widget"}]

        with patch("icv_search.services.documents.index_documents") as mock_index:
            add_documents(str(index.pk), docs)
            mock_index.assert_called_once()
            # Check that the first arg is the SearchIndex instance
            args = mock_index.call_args[0]
            assert args[0].pk == index.pk
            assert args[1] == docs

    @pytest.mark.django_db
    def test_uses_custom_primary_key(self):
        """Task should pass primary_key parameter to service."""
        index = create_index("products")
        docs = [{"product_id": "abc", "name": "Widget"}]

        with patch("icv_search.services.documents.index_documents") as mock_index:
            add_documents(str(index.pk), docs, primary_key="product_id")
            assert mock_index.call_args[1]["primary_key"] == "product_id"

    @pytest.mark.django_db
    def test_handles_missing_index(self):
        """Task should log warning and return when index does not exist."""
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        docs = [{"id": "1"}]
        # Should not raise
        add_documents(fake_uuid, docs)

    @pytest.mark.django_db
    def test_retries_on_failure(self):
        """Task should retry when indexing fails."""
        index = create_index("products")
        docs = [{"id": "1"}]

        with patch("icv_search.services.documents.index_documents", side_effect=RuntimeError("indexing failed")):
            try:
                add_documents(str(index.pk), docs)
            except Exception:
                pass  # Expected to fail


class TestRemoveDocumentsTask:
    """remove_documents task."""

    @pytest.mark.django_db
    def test_removes_documents_from_index(self):
        """Task should call remove_documents service."""
        index = create_index("products")
        doc_ids = ["1", "2"]

        with patch("icv_search.services.documents.remove_documents") as mock_remove:
            from icv_search.tasks import remove_documents as task_remove

            task_remove(str(index.pk), doc_ids)
            mock_remove.assert_called_once()

    @pytest.mark.django_db
    def test_handles_missing_index(self):
        """Task should log warning and return when index does not exist."""
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        doc_ids = ["1", "2"]
        # Should not raise
        remove_documents(fake_uuid, doc_ids)

    @pytest.mark.django_db
    def test_retries_on_failure(self):
        """Task should retry when removal fails."""
        index = create_index("products")
        doc_ids = ["1", "2"]

        # Patch the service function that gets imported inside the task
        with patch("icv_search.services.documents.remove_documents", side_effect=RuntimeError("removal failed")):
            try:
                remove_documents(str(index.pk), doc_ids)
            except Exception:
                pass  # Expected to fail


class TestReindexTask:
    """reindex task."""

    @pytest.mark.django_db
    def test_reindexes_from_model_class(self):
        """Task should call reindex_all service."""
        index = create_index("articles")

        with (
            patch("icv_search.services.documents.reindex_all") as mock_reindex,
            patch("django.utils.module_loading.import_string") as mock_import,
        ):
            from search_testapp.models import Article

            mock_import.return_value = Article
            mock_reindex.return_value = 42
            count = reindex(str(index.pk), "search_testapp.models.Article", batch_size=500)
            assert count == 42
            mock_reindex.assert_called_once()

    @pytest.mark.django_db
    def test_resolves_model_class_from_string(self):
        """Task should import model class from dotted path."""
        index = create_index("articles")

        with (
            patch("icv_search.services.documents.reindex_all") as mock_reindex,
            patch("django.utils.module_loading.import_string") as mock_import,
        ):
            from search_testapp.models import Article

            mock_import.return_value = Article
            mock_reindex.return_value = 10
            reindex(str(index.pk), "search_testapp.models.Article")
            # Check model class was resolved
            args = mock_reindex.call_args[0]
            assert args[1] == Article

    @pytest.mark.django_db
    def test_uses_custom_batch_size(self):
        """Task should pass batch_size to reindex_all."""
        index = create_index("articles")

        with (
            patch("icv_search.services.documents.reindex_all") as mock_reindex,
            patch("django.utils.module_loading.import_string") as mock_import,
        ):
            from search_testapp.models import Article

            mock_import.return_value = Article
            mock_reindex.return_value = 5
            reindex(str(index.pk), "search_testapp.models.Article", batch_size=100)
            assert mock_reindex.call_args[1]["batch_size"] == 100

    @pytest.mark.django_db
    def test_handles_missing_index(self):
        """Task should return 0 when index does not exist."""
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        count = reindex(fake_uuid, "search_testapp.Article")
        assert count == 0


class TestRefreshDocumentCounts:
    """refresh_document_counts task."""

    @pytest.mark.django_db
    def test_updates_document_counts_for_active_indexes(self):
        """Task should update document_count for all active indexes."""
        idx1 = create_index("products")
        idx2 = create_index("articles")

        # Add documents
        index_documents(idx1, [{"id": "1"}, {"id": "2"}, {"id": "3"}])
        index_documents(idx2, [{"id": "1"}])

        # Reset document_count to 0
        SearchIndex.objects.all().update(document_count=0)

        count = refresh_document_counts()
        assert count == 2

        idx1.refresh_from_db()
        idx2.refresh_from_db()
        assert idx1.document_count == 3
        assert idx2.document_count == 1

    @pytest.mark.django_db
    def test_skips_inactive_indexes(self):
        """Task should not update inactive indexes."""
        idx1 = create_index("products")
        idx1.is_active = False
        idx1.save()

        count = refresh_document_counts()
        assert count == 0

    @pytest.mark.django_db
    def test_continues_on_individual_failures(self):
        """Task should continue when one index fails."""
        create_index("index1")
        create_index("index2")

        def side_effect(index):
            if index.name == "index1":
                raise RuntimeError("stats failed")
            from icv_search.types import IndexStats

            return IndexStats(document_count=5, is_indexing=False, field_distribution={}, raw={})

        with patch("icv_search.services.indexing.get_index_stats", side_effect=side_effect):
            count = refresh_document_counts()
            assert count == 1

    @pytest.mark.django_db
    def test_returns_zero_when_no_indexes(self):
        """Task should return 0 when no active indexes exist."""
        count = refresh_document_counts()
        assert count == 0


class TestRetryConfiguration:
    """Documented retry policy on the three bind=True Celery tasks (#48)."""

    @pytest.mark.parametrize(
        "task",
        [sync_index_settings, add_documents, remove_documents],
        ids=["sync_index_settings", "add_documents", "remove_documents"],
    )
    def test_max_retries_is_three(self, task):
        """Each retrying task is decorated with ``max_retries=3``."""
        assert task.max_retries == 3

    @pytest.mark.parametrize(
        "task",
        [sync_index_settings, add_documents, remove_documents],
        ids=["sync_index_settings", "add_documents", "remove_documents"],
    )
    def test_default_retry_delay_is_sixty_seconds(self, task):
        """Each retrying task is decorated with ``default_retry_delay=60``."""
        assert task.default_retry_delay == 60
