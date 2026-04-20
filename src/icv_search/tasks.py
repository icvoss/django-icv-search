"""Celery tasks for async search operations."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from celery import shared_task
except ImportError:
    import functools

    # Celery not installed — define a no-op decorator so imports don't fail.
    # When bind=True, strip the leading `self` parameter so the function
    # can be called directly without a Celery task instance.
    def shared_task(func=None, **kwargs):  # type: ignore[misc]
        bind = kwargs.get("bind", False)

        class _FakeTask:
            """Minimal stand-in for a Celery task instance when Celery is absent."""

            @staticmethod
            def retry(exc=None, **kw):
                if exc:
                    raise exc

        _fake = _FakeTask()

        def decorator(f):
            if bind:

                @functools.wraps(f)
                def wrapper(*args, **kw):
                    return f(_fake, *args, **kw)

                return wrapper
            return f

        if func is not None:
            return decorator(func)
        return decorator


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_index_settings(self, index_pk: str) -> None:
    """Push index settings to the search engine."""
    from icv_search.models import SearchIndex
    from icv_search.services.indexing import _sync_index_to_engine

    try:
        index = SearchIndex.objects.get(pk=index_pk)
    except SearchIndex.DoesNotExist:
        logger.warning("SearchIndex %s not found — skipping sync.", index_pk)
        return

    try:
        _sync_index_to_engine(index)
    except Exception as exc:
        logger.warning("Sync failed for index '%s', retrying...", index.name)
        raise self.retry(exc=exc)  # noqa: B904 — self.retry() is the Celery retry mechanism


@shared_task
def sync_all_indexes() -> int:
    """Sync all unsynced indexes. Intended as a periodic task."""
    from icv_search.models import SearchIndex
    from icv_search.services.indexing import _sync_index_to_engine

    unsynced = SearchIndex.objects.filter(is_synced=False, is_active=True)
    count = 0
    total = unsynced.count()

    for index in unsynced:
        try:
            _sync_index_to_engine(index)
            count += 1
        except Exception:
            logger.exception("Failed to sync index '%s'.", index.name)

    logger.info("Synced %d/%d unsynced indexes.", count, total)
    return count


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def add_documents(self, index_pk: str, documents: list[dict[str, Any]], primary_key: str = "id") -> None:
    """Add documents to a search index asynchronously."""
    from icv_search.models import SearchIndex
    from icv_search.services.documents import index_documents

    try:
        index = SearchIndex.objects.get(pk=index_pk)
    except SearchIndex.DoesNotExist:
        logger.warning("SearchIndex %s not found — skipping document add.", index_pk)
        return

    try:
        index_documents(index, documents, primary_key=primary_key)
    except Exception as exc:
        logger.warning("Document add failed for index '%s', retrying...", index.name)
        raise self.retry(exc=exc)  # noqa: B904 — self.retry() is the Celery retry mechanism


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def remove_documents(self, index_pk: str, document_ids: list[str]) -> None:
    """Remove documents from a search index asynchronously."""
    from icv_search.models import SearchIndex
    from icv_search.services.documents import remove_documents as _remove

    try:
        index = SearchIndex.objects.get(pk=index_pk)
    except SearchIndex.DoesNotExist:
        logger.warning("SearchIndex %s not found — skipping document remove.", index_pk)
        return

    try:
        _remove(index, document_ids)
    except Exception as exc:
        logger.warning("Document remove failed for index '%s', retrying...", index.name)
        raise self.retry(exc=exc)  # noqa: B904 — self.retry() is the Celery retry mechanism


@shared_task
def reindex(index_pk: str, model_path: str, batch_size: int = 1000) -> int:
    """Full reindex from Django model queryset."""
    from django.utils.module_loading import import_string

    from icv_search.models import SearchIndex
    from icv_search.services.documents import reindex_all

    try:
        index = SearchIndex.objects.get(pk=index_pk)
    except SearchIndex.DoesNotExist:
        logger.warning("SearchIndex %s not found — skipping reindex.", index_pk)
        return 0

    model_class = import_string(model_path)
    return reindex_all(index, model_class, batch_size=batch_size)


@shared_task
def reindex_zero_downtime_task(index_pk: str, model_path: str, batch_size: int = 1000) -> int:
    """Zero-downtime reindex using index swap."""
    from django.utils.module_loading import import_string

    from icv_search.models import SearchIndex
    from icv_search.services.documents import reindex_zero_downtime

    try:
        index = SearchIndex.objects.get(pk=index_pk)
    except SearchIndex.DoesNotExist:
        logger.warning("SearchIndex %s not found — skipping reindex.", index_pk)
        return 0

    model_class = import_string(model_path)
    return reindex_zero_downtime(index, model_class, batch_size=batch_size)


@shared_task
def flush_debounce_buffer(index_pk: str) -> int:
    """Drain the debounce buffer for an index and index all buffered documents.

    Called after the debounce window expires. Reads buffered document dicts
    from the Django cache, clears the buffer, and indexes them in a single
    batch call.
    """
    from django.core.cache import cache

    from icv_search.models import SearchIndex
    from icv_search.services.documents import index_documents

    cache_key = f"icv_search:debounce:{index_pk}"

    try:
        index = SearchIndex.objects.get(pk=index_pk)
    except SearchIndex.DoesNotExist:
        logger.warning("SearchIndex %s not found — clearing debounce buffer.", index_pk)
        cache.delete(cache_key)
        return 0

    # Atomically read and clear the buffer
    documents = cache.get(cache_key, [])
    if not documents:
        return 0

    cache.delete(cache_key)

    # Deduplicate by primary key (keep last version)
    pk_field = index.primary_key_field
    seen: dict[str, dict] = {}
    for doc in documents:
        doc_id = str(doc.get(pk_field, doc.get("id", "")))
        seen[doc_id] = doc

    unique_docs = list(seen.values())

    try:
        index_documents(index, unique_docs, primary_key=pk_field)
        logger.info(
            "Flushed debounce buffer for '%s': %d documents (%d deduplicated).",
            index.name,
            len(unique_docs),
            len(documents) - len(unique_docs),
        )
        return len(unique_docs)
    except Exception:
        logger.exception("Failed to flush debounce buffer for '%s'.", index.name)
        raise


@shared_task
def cleanup_sync_logs(days_older_than: int = 90) -> int:
    """Delete old IndexSyncLog rows.

    Intended as a periodic Celery beat task.  Records created more than
    ``days_older_than`` days ago are permanently deleted.

    Args:
        days_older_than: Retention period in days.  Defaults to 90.

    Returns:
        Number of records deleted.
    """
    from icv_search.services.indexing import clear_sync_logs

    return clear_sync_logs(days_older_than=days_older_than)


@shared_task
def cleanup_search_query_aggregates(days_older_than: int = 90) -> int:
    """Delete old search query aggregate rows.

    Intended as a periodic Celery beat task.  Aggregate rows with a ``date``
    more than ``days_older_than`` days in the past are permanently deleted.

    Args:
        days_older_than: Retention period in days.  Defaults to 90.

    Returns:
        Number of rows deleted.
    """
    from icv_search.services.analytics import clear_query_aggregates

    return clear_query_aggregates(days_older_than=days_older_than)


@shared_task
def cleanup_search_query_logs(days_older_than: int = 30) -> int:
    """Delete old search query logs.

    Intended as a periodic Celery beat task.  Records created more than
    ``days_older_than`` days ago are permanently deleted.

    Args:
        days_older_than: Retention period in days.  Defaults to 30.

    Returns:
        Number of records deleted.
    """
    from icv_search.services.analytics import clear_query_logs

    return clear_query_logs(days_older_than=days_older_than)


@shared_task
def refresh_document_counts() -> int:
    """Update document_count for all active indexes from engine stats."""
    from icv_search.models import SearchIndex
    from icv_search.services.indexing import get_index_stats

    indexes = SearchIndex.objects.filter(is_active=True)
    count = 0

    for index in indexes:
        try:
            stats = get_index_stats(index)
            doc_count = stats.document_count
            SearchIndex.objects.filter(pk=index.pk).update(document_count=doc_count)
            count += 1
        except Exception:
            logger.exception("Failed to refresh document count for '%s'.", index.name)

    logger.info("Refreshed document counts for %d indexes.", count)
    return count
