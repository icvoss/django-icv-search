"""Additional tests for document service functions to improve coverage."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from icv_search.backends import reset_search_backend
from icv_search.backends.dummy import DummyBackend
from icv_search.exceptions import SearchBackendError
from icv_search.models import IndexSyncLog
from icv_search.services import create_index, index_documents, remove_documents
from icv_search.services.documents import index_model_instances, reindex_all, reindex_zero_downtime


@pytest.fixture(autouse=True)
def use_dummy_backend(settings):
    """Use DummyBackend for all tests."""
    settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
    settings.ICV_SEARCH_AUTO_SYNC = False
    reset_search_backend()
    DummyBackend.reset()
    yield
    DummyBackend.reset()
    reset_search_backend()


class TestIndexDocumentsErrorHandling:
    """Error handling in index_documents service."""

    @pytest.mark.django_db
    def test_creates_failed_log_on_backend_error(self):
        """index_documents should create failed log when backend raises."""
        index = create_index("products")

        with patch(
            "icv_search.backends.dummy.DummyBackend.add_documents", side_effect=SearchBackendError("connection failed")
        ):
            with pytest.raises(SearchBackendError):
                index_documents(index, [{"id": "1"}])

            log = IndexSyncLog.objects.filter(index=index, action="documents_added", status="failed").first()
            assert log is not None
            assert "connection failed" in log.detail

    @pytest.mark.django_db
    def test_logs_exception_on_backend_error(self, caplog):
        """index_documents should log exception when backend fails."""
        index = create_index("products")

        with patch("icv_search.backends.dummy.DummyBackend.add_documents", side_effect=SearchBackendError("error")):
            with pytest.raises(SearchBackendError):
                index_documents(index, [{"id": "1"}])

            assert any("Failed to index documents" in record.message for record in caplog.records)


class TestRemoveDocumentsErrorHandling:
    """Error handling in remove_documents service."""

    @pytest.mark.django_db
    def test_creates_failed_log_on_backend_error(self):
        """remove_documents should create failed log when backend raises."""
        index = create_index("products")

        with patch(
            "icv_search.backends.dummy.DummyBackend.delete_documents",
            side_effect=SearchBackendError("connection failed"),
        ):
            with pytest.raises(SearchBackendError):
                remove_documents(index, ["1", "2"])

            log = IndexSyncLog.objects.filter(index=index, action="documents_deleted", status="failed").first()
            assert log is not None
            assert "connection failed" in log.detail

    @pytest.mark.django_db
    def test_logs_exception_on_backend_error(self, caplog):
        """remove_documents should log exception when backend fails."""
        index = create_index("products")

        with patch("icv_search.backends.dummy.DummyBackend.delete_documents", side_effect=SearchBackendError("error")):
            with pytest.raises(SearchBackendError):
                remove_documents(index, ["1"])

            assert any("Failed to remove documents" in record.message for record in caplog.records)


class TestIndexModelInstances:
    """index_model_instances service function."""

    @pytest.mark.django_db
    def test_raises_when_model_has_no_search_index_name(self):
        """index_model_instances should raise ValueError when model lacks search_index_name."""

        class NoSearchMixin:
            pass

        with pytest.raises(ValueError, match="does not define search_index_name"):
            index_model_instances(NoSearchMixin)

    @pytest.mark.django_db
    def test_raises_when_search_index_name_is_empty(self):
        """index_model_instances should raise ValueError when search_index_name is empty."""

        class EmptyName:
            search_index_name = ""

        with pytest.raises(ValueError, match="does not define search_index_name"):
            index_model_instances(EmptyName)

    @pytest.mark.django_db
    def test_uses_custom_queryset(self):
        """index_model_instances should use provided queryset instead of get_search_queryset."""
        from search_testapp.models import Article

        create_index("articles")

        # Create articles
        Article.objects.create(title="Article 1", body="Content 1", author="Author 1")
        Article.objects.create(title="Article 2", body="Content 2", author="Author 2")

        # Pass custom queryset with filter
        custom_qs = Article.objects.filter(title="Article 1")
        count = index_model_instances(Article, queryset=custom_qs)
        assert count == 1

    @pytest.mark.django_db
    def test_uses_get_search_queryset_by_default(self):
        """index_model_instances should use model's get_search_queryset when no queryset provided."""
        from search_testapp.models import Article

        create_index("articles")

        Article.objects.create(title="Article 1", body="Content 1", author="Author 1")
        Article.objects.create(title="Article 2", body="Content 2", author="Author 2")

        count = index_model_instances(Article)
        assert count == 2

    @pytest.mark.django_db
    def test_batches_documents(self):
        """index_model_instances should batch documents according to batch_size."""
        from search_testapp.models import Article

        create_index("articles")

        # Create 5 articles
        for i in range(5):
            Article.objects.create(title=f"Article {i}", body=f"Content {i}", author=f"Author {i}")

        with patch("icv_search.services.documents.index_documents") as mock_index:
            # Use batch size of 2
            index_model_instances(Article, batch_size=2)
            # Should be called 3 times: 2 + 2 + 1
            assert mock_index.call_count == 3

    @pytest.mark.django_db
    def test_logs_completion(self):
        """index_model_instances should log completion with count."""
        from search_testapp.models import Article

        create_index("articles")
        Article.objects.create(title="Article 1", body="Content 1", author="Author 1")

        # Just verify it completes without error
        count = index_model_instances(Article)
        assert count == 1


class TestReindexAll:
    """reindex_all service function."""

    @pytest.mark.django_db
    def test_clears_documents_before_reindexing(self):
        """reindex_all should clear existing documents before indexing."""
        from search_testapp.models import Article

        index = create_index("articles")

        # Add initial documents
        index_documents(index, [{"id": "old1"}, {"id": "old2"}])

        # Create article
        Article.objects.create(title="New Article", body="Content", author="Author")

        with patch("icv_search.backends.dummy.DummyBackend.clear_documents") as mock_clear:
            reindex_all(index, Article)
            mock_clear.assert_called_once()

    @pytest.mark.django_db
    def test_continues_when_clear_fails(self, caplog):
        """reindex_all should continue indexing even if clear fails."""
        from search_testapp.models import Article

        index = create_index("articles")
        Article.objects.create(title="Article", body="Content", author="Author")

        with patch(
            "icv_search.backends.dummy.DummyBackend.clear_documents", side_effect=SearchBackendError("clear failed")
        ):
            count = reindex_all(index, Article)
            assert count == 1
            assert any("Could not clear documents" in record.message for record in caplog.records)

    @pytest.mark.django_db
    def test_continues_when_clear_not_implemented(self, caplog):
        """reindex_all should continue when backend does not support clear_documents."""
        from search_testapp.models import Article

        index = create_index("articles")
        Article.objects.create(title="Article", body="Content", author="Author")

        with patch("icv_search.backends.dummy.DummyBackend.clear_documents", side_effect=NotImplementedError()):
            count = reindex_all(index, Article)
            assert count == 1
            assert any("Could not clear documents" in record.message for record in caplog.records)

    @pytest.mark.django_db
    def test_creates_reindexed_log(self):
        """reindex_all should create IndexSyncLog with action='reindexed'."""
        from search_testapp.models import Article

        index = create_index("articles")
        Article.objects.create(title="Article", body="Content", author="Author")

        reindex_all(index, Article)

        log = IndexSyncLog.objects.filter(index=index, action="reindexed", status="success").first()
        assert log is not None
        assert "Reindexed 1 documents" in log.detail

    @pytest.mark.django_db
    def test_creates_failed_log_on_error(self):
        """reindex_all should create failed log when indexing fails."""
        from search_testapp.models import Article

        index = create_index("articles")
        Article.objects.create(title="Article", body="Content", author="Author")

        with patch(
            "icv_search.services.documents.index_model_instances", side_effect=SearchBackendError("indexing failed")
        ):
            with pytest.raises(SearchBackendError):
                reindex_all(index, Article)

            log = IndexSyncLog.objects.filter(index=index, action="reindexed", status="failed").first()
            assert log is not None
            assert "indexing failed" in log.detail

    @pytest.mark.django_db
    def test_uses_custom_batch_size(self):
        """reindex_all should pass batch_size to index_model_instances."""
        from search_testapp.models import Article

        index = create_index("articles")

        with patch("icv_search.services.documents.index_model_instances") as mock_index:
            mock_index.return_value = 0
            reindex_all(index, Article, batch_size=500)
            assert mock_index.call_args[1]["batch_size"] == 500

    @pytest.mark.django_db
    def test_resolves_index_by_name(self):
        """reindex_all should resolve SearchIndex from name string."""
        from search_testapp.models import Article

        create_index("articles")
        Article.objects.create(title="Article", body="Content", author="Author")

        count = reindex_all("articles", Article)
        assert count == 1


class TestReindexZeroDowntimeCleanupLogging:
    """reindex_zero_downtime() logs temp-index cleanup failures (ADR-101 / #54)."""

    @pytest.mark.django_db
    def test_swap_fallback_cleanup_failure_logs_warning(self, caplog):
        from search_testapp.models import Article

        index = create_index("articles")
        Article.objects.create(title="A", body="B", author="Author")

        with (
            patch(
                "icv_search.backends.dummy.DummyBackend.swap_indexes",
                side_effect=NotImplementedError("no swap"),
            ),
            patch(
                "icv_search.backends.dummy.DummyBackend.delete_index",
                side_effect=RuntimeError("cleanup failed"),
            ),
            caplog.at_level(logging.WARNING, logger="icv_search.services.documents"),
        ):
            total = reindex_zero_downtime(index, Article)

        assert total == 1
        assert any(
            "Failed to delete temporary index" in r.message and "during swap fallback" in r.message
            for r in caplog.records
        )

    @pytest.mark.django_db
    def test_post_swap_cleanup_failure_logs_warning(self, caplog):
        from search_testapp.models import Article

        index = create_index("articles")
        Article.objects.create(title="A", body="B", author="Author")
        temp_uid = f"{index.engine_uid}_reindex_tmp"

        original_delete = DummyBackend.delete_index

        def delete_index(self, uid, *args, **kwargs):
            if uid == temp_uid:
                raise RuntimeError("post-swap cleanup failed")
            return original_delete(self, uid, *args, **kwargs)

        with (
            patch.object(DummyBackend, "delete_index", delete_index),
            caplog.at_level(logging.WARNING, logger="icv_search.services.documents"),
        ):
            total = reindex_zero_downtime(index, Article)

        assert total == 1
        assert any(
            "Failed to delete temporary index" in r.message and "after swap" in r.message for r in caplog.records
        )

    @pytest.mark.django_db
    def test_failure_cleanup_logs_warning(self, caplog):
        from search_testapp.models import Article

        index = create_index("articles")
        Article.objects.create(title="A", body="B", author="Author")
        temp_uid = f"{index.engine_uid}_reindex_tmp"

        original_delete = DummyBackend.delete_index

        def delete_index(self, uid, *args, **kwargs):
            if uid == temp_uid:
                raise RuntimeError("failure cleanup failed")
            return original_delete(self, uid, *args, **kwargs)

        with (
            patch(
                "icv_search.backends.dummy.DummyBackend.swap_indexes",
                side_effect=SearchBackendError("swap failed"),
            ),
            patch.object(DummyBackend, "delete_index", delete_index),
            caplog.at_level(logging.WARNING, logger="icv_search.services.documents"),
            pytest.raises(SearchBackendError),
        ):
            reindex_zero_downtime(index, Article)

        assert any(
            "Failed to delete temporary index" in r.message and "after reindex failure" in r.message
            for r in caplog.records
        )
