"""Regression tests for #6 (delete fan-out) and #7 (unbounded debounce buffer).

#6: the delete path had no debounce/coalescing at all, so a bulk delete
firing per-row post_delete signals dispatched one Celery task per row.
#7: the debounce buffer had no size cap and the flush sent the entire
buffered list in a single call, with no chunking.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.cache import cache

from icv_search.auto_index import (
    _debounce_document,
    _debounce_removal,
    connect_auto_index_signals,
    disconnect_auto_index_signals,
)
from icv_search.backends import reset_search_backend
from icv_search.backends.dummy import DummyBackend
from icv_search.services import create_index
from icv_search.tasks import flush_debounce_buffer, flush_debounce_removal_buffer

_DEBOUNCED_CONFIG = {
    "articles": {
        "model": "search_testapp.Article",
        "on_save": True,
        "on_delete": True,
        "async": False,
        "auto_create": True,
    },
}


@pytest.fixture(autouse=True)
def _reset_state(settings):
    """Use DummyBackend, clear the cache, and disconnect signals after each test."""
    settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
    settings.ICV_SEARCH_AUTO_SYNC = False
    reset_search_backend()
    DummyBackend.reset()
    cache.clear()
    yield
    DummyBackend.reset()
    reset_search_backend()
    cache.clear()
    disconnect_auto_index_signals(["articles"])


# ===========================================================================
# #6: bulk delete must coalesce into one flush, not one task per row
# ===========================================================================


class TestDeleteDebouncing:
    """Deletes must be debounced the same way saves are, not fanned out."""

    @pytest.mark.django_db
    def test_multiple_deletes_within_window_buffer_into_one_task(self, settings):
        """N deletes inside the debounce window schedule exactly one flush task."""
        settings.ICV_SEARCH_DEBOUNCE_SECONDS = 30
        index = create_index("articles")

        with patch("icv_search.tasks.flush_debounce_removal_buffer.apply_async") as mock_apply_async:
            _debounce_removal("articles", "1", 30)
            _debounce_removal("articles", "2", 30)
            _debounce_removal("articles", "3", 30)

        # Exactly one flush task scheduled for three buffered deletes, not three.
        mock_apply_async.assert_called_once()
        args, kwargs = mock_apply_async.call_args
        assert kwargs["args"] == [str(index.pk)]

        buffered = cache.get(f"icv_search:debounce_removal:{index.pk}")
        assert buffered == ["1", "2", "3"]

    @pytest.mark.django_db
    def test_flush_removes_all_buffered_ids_in_one_backend_call_shape(self, settings):
        """flush_debounce_removal_buffer removes every buffered ID."""
        settings.ICV_SEARCH_DEBOUNCE_SECONDS = 30
        index = create_index("articles")

        for doc_id in ("1", "2", "3"):
            _debounce_removal("articles", doc_id, 30)

        with patch("icv_search.services.documents.remove_documents") as mock_remove:
            result = flush_debounce_removal_buffer(str(index.pk))

        assert result == 3
        removed_ids = set()
        for call in mock_remove.call_args_list:
            removed_ids.update(call.args[1])
        assert removed_ids == {"1", "2", "3"}

    @pytest.mark.django_db
    def test_bulk_delete_via_signals_schedules_one_flush_not_one_per_row(self, settings):
        """The exact #6 shape: N post_delete signals -> one scheduled flush."""
        settings.ICV_SEARCH_AUTO_INDEX = _DEBOUNCED_CONFIG
        settings.ICV_SEARCH_DEBOUNCE_SECONDS = 30
        connect_auto_index_signals()

        from search_testapp.models import Article

        articles = [Article.objects.create(title=f"Row {i}", body="x", author="A") for i in range(5)]

        with patch("icv_search.tasks.flush_debounce_removal_buffer.apply_async") as mock_apply_async:
            for article in articles:
                article.delete()

        # Five post_delete signals must not fan out into five scheduled tasks.
        assert mock_apply_async.call_count == 1


# ===========================================================================
# #7: buffer must be bounded and the flush must chunk
# ===========================================================================


class TestDebounceBufferSizeCap:
    """The debounce buffer must flush early once it grows large enough."""

    @pytest.mark.django_db
    def test_save_buffer_flushes_immediately_once_max_size_reached(self, settings):
        """Reaching ICV_SEARCH_DEBOUNCE_MAX_BUFFER_SIZE triggers countdown=0."""
        settings.ICV_SEARCH_DEBOUNCE_SECONDS = 300
        settings.ICV_SEARCH_DEBOUNCE_MAX_BUFFER_SIZE = 3
        index = create_index("articles")

        with patch("icv_search.tasks.flush_debounce_buffer.apply_async") as mock_apply_async:
            _debounce_document("articles", {"id": "1"}, 300)
            _debounce_document("articles", {"id": "2"}, 300)
            # Third item reaches the cap: must schedule with countdown=0,
            # not wait out the remaining 300 second window.
            _debounce_document("articles", {"id": "3"}, 300)

        assert mock_apply_async.call_count == 2  # first schedule (time) + cap-triggered (immediate)
        first_call_kwargs = mock_apply_async.call_args_list[0].kwargs
        cap_call_kwargs = mock_apply_async.call_args_list[-1].kwargs
        assert first_call_kwargs["countdown"] == 300
        assert cap_call_kwargs["countdown"] == 0
        assert cap_call_kwargs["args"] == [str(index.pk)]

    @pytest.mark.django_db
    def test_removal_buffer_flushes_immediately_once_max_size_reached(self, settings):
        """Same cap behaviour on the delete side."""
        settings.ICV_SEARCH_DEBOUNCE_SECONDS = 300
        settings.ICV_SEARCH_DEBOUNCE_MAX_BUFFER_SIZE = 2
        create_index("articles")

        with patch("icv_search.tasks.flush_debounce_removal_buffer.apply_async") as mock_apply_async:
            _debounce_removal("articles", "1", 300)
            _debounce_removal("articles", "2", 300)

        assert mock_apply_async.call_count == 2
        assert mock_apply_async.call_args_list[-1].kwargs["countdown"] == 0

    @pytest.mark.django_db
    def test_zero_max_buffer_size_disables_early_flush(self, settings):
        """ICV_SEARCH_DEBOUNCE_MAX_BUFFER_SIZE=0 keeps the pre-#7 time-only behaviour."""
        settings.ICV_SEARCH_DEBOUNCE_SECONDS = 300
        settings.ICV_SEARCH_DEBOUNCE_MAX_BUFFER_SIZE = 0
        create_index("articles")

        with patch("icv_search.tasks.flush_debounce_buffer.apply_async") as mock_apply_async:
            for i in range(50):
                _debounce_document("articles", {"id": str(i)}, 300)

        # Only the very first append schedules a flush; nothing forces an early one.
        mock_apply_async.assert_called_once()
        assert mock_apply_async.call_args.kwargs["countdown"] == 300


class TestDebounceFlushChunking:
    """The flush task must send the buffer to the backend in bounded chunks."""

    @pytest.mark.django_db
    def test_index_flush_sends_documents_in_chunks(self, settings):
        settings.ICV_SEARCH_DEBOUNCE_FLUSH_CHUNK_SIZE = 10
        index = create_index("articles")

        documents = [{"id": str(i)} for i in range(25)]
        cache.set(f"icv_search:debounce:{index.pk}", documents, timeout=60)

        with patch("icv_search.services.documents.index_documents") as mock_index:
            result = flush_debounce_buffer(str(index.pk))

        assert result == 25
        # 25 documents at a chunk size of 10 must be sent as three calls, not one.
        assert mock_index.call_count == 3
        chunk_lengths = sorted(len(call.args[1]) for call in mock_index.call_args_list)
        assert chunk_lengths == [5, 10, 10]

    @pytest.mark.django_db
    def test_removal_flush_sends_ids_in_chunks(self, settings):
        settings.ICV_SEARCH_DEBOUNCE_FLUSH_CHUNK_SIZE = 10
        index = create_index("articles")

        document_ids = [str(i) for i in range(25)]
        cache.set(f"icv_search:debounce_removal:{index.pk}", document_ids, timeout=60)

        with patch("icv_search.services.documents.remove_documents") as mock_remove:
            result = flush_debounce_removal_buffer(str(index.pk))

        assert result == 25
        assert mock_remove.call_count == 3
        chunk_lengths = sorted(len(call.args[1]) for call in mock_remove.call_args_list)
        assert chunk_lengths == [5, 10, 10]

    @pytest.mark.django_db
    def test_chunk_size_zero_sends_a_single_unbounded_request(self, settings):
        """An explicit opt-out (chunk size 0) preserves the pre-#7 single-call shape."""
        settings.ICV_SEARCH_DEBOUNCE_FLUSH_CHUNK_SIZE = 0
        index = create_index("articles")

        documents = [{"id": str(i)} for i in range(25)]
        cache.set(f"icv_search:debounce:{index.pk}", documents, timeout=60)

        with patch("icv_search.services.documents.index_documents") as mock_index:
            flush_debounce_buffer(str(index.pk))

        mock_index.assert_called_once()
        assert len(mock_index.call_args.args[1]) == 25

    @pytest.mark.django_db
    def test_empty_buffer_flushes_zero_documents(self, settings):
        index = create_index("articles")

        with patch("icv_search.services.documents.index_documents") as mock_index:
            result = flush_debounce_buffer(str(index.pk))

        assert result == 0
        mock_index.assert_not_called()
