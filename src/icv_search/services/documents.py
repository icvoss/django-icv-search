"""Document indexing service functions."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Empty, Queue
from threading import Event
from typing import Any

from icv_search.backends import get_search_backend
from icv_search.exceptions import SearchBackendError
from icv_search.models import IndexSyncLog, SearchIndex
from icv_search.services._utils import resolve_index
from icv_search.signals import documents_indexed, documents_removed
from icv_search.types import TaskResult

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]


def index_documents(
    name_or_index: str | SearchIndex,
    documents: list[dict[str, Any]],
    tenant_id: str = "",
    primary_key: str = "id",
) -> TaskResult:
    """Add or update documents in a search index.

    Args:
        name_or_index: Index name or SearchIndex instance.
        documents: List of document dicts to index.
        tenant_id: Tenant identifier (only needed if passing a name).
        primary_key: Document field used as primary key.

    Returns:
        Normalised TaskResult for the engine operation.
    """
    index = resolve_index(name_or_index, tenant_id)
    backend = get_search_backend()
    log = IndexSyncLog.objects.create(index=index, action="documents_added", status="pending")

    try:
        raw_result = backend.add_documents(
            uid=index.engine_uid,
            documents=documents,
            primary_key=primary_key,
        )
        task_result = TaskResult.from_engine(raw_result)
        log.task_uid = task_result.task_uid
        log.mark_complete(status="success", detail=f"Indexed {len(documents)} documents.")

        document_ids = [str(doc.get(primary_key, "")) for doc in documents]
        documents_indexed.send(
            sender=SearchIndex,
            instance=index,
            count=len(documents),
            document_ids=document_ids,
        )
        logger.info("Indexed %d documents in '%s'.", len(documents), index.name)
        return task_result

    except SearchBackendError as exc:
        log.mark_complete(status="failed", detail=str(exc))
        logger.exception("Failed to index documents in '%s'.", index.name)
        raise


def remove_documents(
    name_or_index: str | SearchIndex,
    document_ids: list[str],
    tenant_id: str = "",
) -> TaskResult:
    """Remove documents from a search index.

    Args:
        name_or_index: Index name or SearchIndex instance.
        document_ids: List of document IDs to remove.
        tenant_id: Tenant identifier (only needed if passing a name).

    Returns:
        Normalised TaskResult for the engine operation.
    """
    index = resolve_index(name_or_index, tenant_id)
    backend = get_search_backend()
    log = IndexSyncLog.objects.create(index=index, action="documents_deleted", status="pending")

    try:
        raw_result = backend.delete_documents(uid=index.engine_uid, document_ids=document_ids)
        task_result = TaskResult.from_engine(raw_result)
        log.task_uid = task_result.task_uid
        log.mark_complete(status="success", detail=f"Removed {len(document_ids)} documents.")

        documents_removed.send(
            sender=SearchIndex,
            instance=index,
            count=len(document_ids),
            document_ids=document_ids,
        )
        logger.info("Removed %d documents from '%s'.", len(document_ids), index.name)
        return task_result

    except SearchBackendError as exc:
        log.mark_complete(status="failed", detail=str(exc))
        logger.exception("Failed to remove documents from '%s'.", index.name)
        raise


def delete_documents_by_filter(
    name_or_index: str | SearchIndex,
    filter_expr: str | dict[str, Any],
    tenant_id: str = "",
) -> TaskResult:
    """Remove documents matching a filter expression.

    Uses the engine's filter-based deletion (e.g. Meilisearch's
    ``POST /indexes/{uid}/documents/delete`` with a filter body).

    Args:
        name_or_index: Index name or SearchIndex instance.
        filter_expr: A filter expression string (engine-native format) or a
            Django-native filter dict that will be translated automatically.
        tenant_id: Tenant identifier (only needed if passing a name).

    Returns:
        Normalised TaskResult for the engine operation.
    """
    index = resolve_index(name_or_index, tenant_id)
    backend = get_search_backend()

    # Pass the filter through unchanged. Each backend accepts its native form:
    # Meilisearch translates a dict to its filter string (and the fallback
    # delete-by-filter in BaseSearchBackend routes dicts through search()),
    # so a Django-native dict works across every backend.

    log = IndexSyncLog.objects.create(index=index, action="documents_deleted", status="pending")

    try:
        raw_result = backend.delete_documents_by_filter(uid=index.engine_uid, filter_expr=filter_expr)
        task_result = TaskResult.from_engine(raw_result)
        log.task_uid = task_result.task_uid
        log.mark_complete(status="success", detail=f"Deleted documents by filter: {filter_expr}")

        documents_removed.send(
            sender=SearchIndex,
            instance=index,
            count=0,
            document_ids=[],
        )
        logger.info("Deleted documents by filter from '%s': %s", index.name, filter_expr)
        return task_result

    except SearchBackendError as exc:
        log.mark_complete(status="failed", detail=str(exc))
        logger.exception("Failed to delete documents by filter from '%s'.", index.name)
        raise


def delete_document(
    name_or_index: str | SearchIndex,
    document_id: str,
    tenant_id: str = "",
) -> TaskResult:
    """Remove a single document from a search index.

    Convenience wrapper around :func:`remove_documents` for the common
    single-document case (BR-011).

    Args:
        name_or_index: Index name or SearchIndex instance.
        document_id: The ID of the document to remove.
        tenant_id: Tenant identifier (only needed if passing a name).

    Returns:
        Normalised TaskResult for the engine operation.
    """
    return remove_documents(name_or_index, [document_id], tenant_id)


def get_document(
    name_or_index: str | SearchIndex,
    document_id: str,
    tenant_id: str = "",
) -> dict[str, Any]:
    """Fetch a single document by ID from the search engine.

    Args:
        name_or_index: Index name or SearchIndex instance.
        document_id: Primary key of the document to fetch.
        tenant_id: Tenant identifier (only needed if passing a name).

    Returns:
        Document dict with ``id`` always present.
    """
    index = resolve_index(name_or_index, tenant_id)
    backend = get_search_backend()
    return backend.get_document(uid=index.engine_uid, document_id=document_id)


def get_documents(
    name_or_index: str | SearchIndex,
    document_ids: list[str] | None = None,
    tenant_id: str = "",
    *,
    limit: int = 20,
    offset: int = 0,
    fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch multiple documents from the search engine.

    When ``document_ids`` is ``None``, browses up to ``limit`` documents
    from ``offset``.  When provided, fetches those specific IDs.

    Args:
        name_or_index: Index name or SearchIndex instance.
        document_ids: Specific document IDs to fetch, or None for browse.
        tenant_id: Tenant identifier (only needed if passing a name).
        limit: Maximum documents to return in browse mode.
        offset: Starting offset in browse mode.
        fields: Restrict returned fields; ``id`` is always included.

    Returns:
        List of document dicts.
    """
    index = resolve_index(name_or_index, tenant_id)
    backend = get_search_backend()
    return backend.get_documents(
        uid=index.engine_uid,
        document_ids=document_ids,
        limit=limit,
        offset=offset,
        fields=fields,
    )


def update_documents(
    name_or_index: str | SearchIndex,
    documents: list[dict[str, Any]],
    tenant_id: str = "",
    primary_key: str = "id",
) -> TaskResult:
    """Partial update of documents in the search engine.

    Fields not included in the update dicts are preserved when the
    engine supports partial updates.  Falls back to full replacement
    on engines without native partial update support.

    Args:
        name_or_index: Index name or SearchIndex instance.
        documents: List of partial document dicts (must include primary key).
        tenant_id: Tenant identifier (only needed if passing a name).
        primary_key: Document field used as primary key.

    Returns:
        TaskResult from the engine.
    """
    index = resolve_index(name_or_index, tenant_id)
    backend = get_search_backend()
    raw = backend.update_documents(uid=index.engine_uid, documents=documents, primary_key=primary_key)
    return TaskResult.from_engine(raw)


# ---------------------------------------------------------------------------
# Bulk indexing
# ---------------------------------------------------------------------------


def _send_batch_ndjson(
    backend: Any,
    uid: str,
    batch: list[dict[str, Any]],
    primary_key: str,
) -> int:
    """Send a single batch via NDJSON and return the document count."""
    backend.add_documents_ndjson(uid=uid, documents=batch, primary_key=primary_key)
    return len(batch)


def _batch_iterator(
    documents: Iterable[dict[str, Any]],
    batch_size: int,
) -> Iterable[list[dict[str, Any]]]:
    """Yield fixed-size batches from an iterable of documents."""
    batch: list[dict[str, Any]] = []
    for doc in documents:
        batch.append(doc)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def bulk_index(
    uid: str,
    documents: Iterable[dict[str, Any]],
    *,
    primary_key: str = "id",
    batch_size: int | None = None,
    concurrency: int | None = None,
    progress_callback: ProgressCallback | None = None,
    total_hint: int | None = None,
) -> int:
    """High-throughput document indexing using NDJSON and concurrent sends.

    Designed for large datasets (100K+ documents). Differences from
    :func:`index_documents`:

    - Uses ``add_documents_ndjson()`` instead of ``add_documents()``
    - Overlaps DB reads with HTTP sends via a thread pool
    - No per-batch ``IndexSyncLog`` or ``documents_indexed`` signal
    - Progress reported via optional callback

    Args:
        uid: Engine index UID (e.g. ``index.engine_uid``).
        documents: Iterable of document dicts. Can be a generator.
        primary_key: Document field used as primary key.
        batch_size: Documents per batch.  Defaults to
            ``ICV_SEARCH_BULK_BATCH_SIZE`` (5000).
        concurrency: Number of concurrent HTTP sender threads.  Defaults to
            ``ICV_SEARCH_BULK_CONCURRENCY`` (2).
        progress_callback: Called with ``(indexed_so_far, total)`` after each
            batch completes.  ``total`` is ``total_hint`` or 0 if unknown.
        total_hint: Expected total document count (used for progress only).

    Returns:
        Total number of documents sent to the engine.
    """
    from icv_search.conf import ICV_SEARCH_BULK_BATCH_SIZE, ICV_SEARCH_BULK_CONCURRENCY

    if batch_size is None:
        batch_size = ICV_SEARCH_BULK_BATCH_SIZE
    if concurrency is None:
        concurrency = ICV_SEARCH_BULK_CONCURRENCY

    backend = get_search_backend()
    total_sent = 0
    total_for_progress = total_hint or 0

    # Use a bounded queue to limit memory: at most `concurrency + 1` batches
    # buffered at any time (one per worker + one being prepared).
    send_queue: Queue[list[dict[str, Any]] | None] = Queue(maxsize=concurrency + 1)
    error_event = Event()
    first_error: list[Exception] = []

    def sender_worker() -> None:
        """Pull batches from the queue and send them."""
        while not error_event.is_set():
            try:
                batch = send_queue.get(timeout=1.0)
            except Empty:
                continue
            if batch is None:
                # Poison pill — producer is done.
                return
            try:
                backend.add_documents_ndjson(
                    uid=uid,
                    documents=batch,
                    primary_key=primary_key,
                )
            except Exception as exc:
                if not first_error:
                    first_error.append(exc)
                error_event.set()
                return

    # Start sender threads.
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(sender_worker) for _ in range(concurrency)]

        try:
            batch_count = 0
            for batch in _batch_iterator(documents, batch_size):
                if error_event.is_set():
                    break
                send_queue.put(batch)
                total_sent += len(batch)
                batch_count += 1
                if progress_callback:
                    progress_callback(total_sent, total_for_progress)
                if batch_count % 10 == 0:
                    logger.info(
                        "bulk_index '%s': sent %d documents (%d batches).",
                        uid,
                        total_sent,
                        batch_count,
                    )
        finally:
            # Send poison pills to shut down workers.
            for _ in range(concurrency):
                send_queue.put(None)

        # Wait for all senders to complete.
        for future in as_completed(futures):
            future.result()  # Raises if worker had an unhandled exception.

    if first_error:
        raise first_error[0]

    logger.info("bulk_index '%s' complete: %d documents in %d batches.", uid, total_sent, batch_count)
    return total_sent


def index_model_instances(
    model_class: type,
    queryset=None,
    batch_size: int = 1000,
    *,
    bulk: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> int:
    """Index model instances using their SearchableMixin configuration.

    Args:
        model_class: A Django model class using SearchableMixin.
        queryset: Optional queryset to use (defaults to get_search_queryset()).
        batch_size: Number of documents per batch.
        bulk: When ``True``, uses :func:`bulk_index` for high-throughput
            indexing with NDJSON and concurrent sends.  Skips per-batch
            ``IndexSyncLog`` and signal dispatch.
        progress_callback: Called with ``(indexed_so_far, total)`` after each
            batch.  Only used when ``bulk=True``.

    Returns:
        Total number of documents indexed.
    """
    if not hasattr(model_class, "search_index_name") or not model_class.search_index_name:
        raise ValueError(f"{model_class.__name__} does not define search_index_name.")

    qs = queryset if queryset is not None else model_class.get_search_queryset()

    if not bulk:
        # Original path — unchanged behaviour.
        total = 0
        batch: list[dict[str, Any]] = []

        for instance in qs.iterator(chunk_size=batch_size):
            batch.append(instance.to_search_document())
            if len(batch) >= batch_size:
                index_documents(model_class.search_index_name, batch)
                total += len(batch)
                batch = []

        if batch:
            index_documents(model_class.search_index_name, batch)
            total += len(batch)

        logger.info("Indexed %d %s instances.", total, model_class.__name__)
        return total

    # Bulk path — resolve the index once, then use bulk_index.
    from icv_search.conf import ICV_SEARCH_BULK_BATCH_SIZE

    index = resolve_index(model_class.search_index_name)
    total_count = qs.count()

    def _doc_generator() -> Iterable[dict[str, Any]]:
        for instance in qs.iterator(chunk_size=batch_size or ICV_SEARCH_BULK_BATCH_SIZE):
            yield instance.to_search_document()

    total = bulk_index(
        uid=index.engine_uid,
        documents=_doc_generator(),
        primary_key=index.primary_key_field,
        batch_size=batch_size if batch_size != 1000 else ICV_SEARCH_BULK_BATCH_SIZE,
        progress_callback=progress_callback,
        total_hint=total_count,
    )

    # Single summary log + signal.
    log = IndexSyncLog.objects.create(
        index=index,
        action="documents_added",
        status="pending",
    )
    log.mark_complete(status="success", detail=f"Bulk indexed {total} documents.")
    documents_indexed.send(
        sender=SearchIndex,
        instance=index,
        count=total,
        document_ids=[],
    )
    logger.info("Bulk indexed %d %s instances.", total, model_class.__name__)
    return total


def reindex_all(
    name_or_index: str | SearchIndex,
    model_class: type,
    tenant_id: str = "",
    batch_size: int = 1000,
) -> int:
    """Full reindex: clear all documents, then re-index from the model queryset.

    Args:
        name_or_index: Index name or SearchIndex instance.
        model_class: A Django model class using SearchableMixin.
        tenant_id: Tenant identifier (only needed if passing a name).
        batch_size: Number of documents per batch.

    Returns:
        Total number of documents indexed.
    """
    index = resolve_index(name_or_index, tenant_id)
    backend = get_search_backend()

    # Clear existing documents
    try:
        backend.clear_documents(uid=index.engine_uid)
    except (SearchBackendError, NotImplementedError):
        logger.warning("Could not clear documents before reindex for '%s'.", index.name)

    log = IndexSyncLog.objects.create(index=index, action="reindexed", status="pending")

    try:
        total = index_model_instances(model_class, batch_size=batch_size)
        log.mark_complete(status="success", detail=f"Reindexed {total} documents.")
        return total
    except SearchBackendError as exc:
        log.mark_complete(status="failed", detail=str(exc))
        raise


def reindex_zero_downtime(
    name_or_index: str | SearchIndex,
    model_class: type,
    tenant_id: str = "",
    batch_size: int = 1000,
    *,
    bulk: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> int:
    """Zero-downtime reindex using index swap.

    Creates a temporary index, populates it fully from the model queryset,
    then atomically swaps it with the live index. The old index is deleted
    after the swap.

    Falls back to ``reindex_all`` (clear + re-index) if the backend does not
    support index swaps.

    Args:
        name_or_index: Index name or SearchIndex instance.
        model_class: A Django model class using SearchableMixin.
        tenant_id: Tenant identifier (only needed if passing a name).
        batch_size: Number of documents per batch.
        bulk: When ``True``, uses :func:`bulk_index` for high-throughput
            population of the temporary index (NDJSON + concurrent sends).
        progress_callback: Called with ``(indexed_so_far, total)`` after each
            batch.  Only used when ``bulk=True``.

    Returns:
        Total number of documents indexed.
    """
    index = resolve_index(name_or_index, tenant_id)
    backend = get_search_backend()

    # Check if backend supports swap
    try:
        backend.swap_indexes  # noqa: B018 — attribute access check
    except AttributeError:
        logger.info("Backend does not support swap — falling back to reindex_all.")
        return reindex_all(index, model_class, tenant_id=tenant_id, batch_size=batch_size)

    temp_uid = f"{index.engine_uid}_reindex_tmp"
    log = IndexSyncLog.objects.create(index=index, action="reindexed", status="pending")

    try:
        # 1. Create temporary index with same settings
        backend.create_index(uid=temp_uid, primary_key=index.primary_key_field)
        if index.settings:
            backend.update_settings(uid=temp_uid, settings=index.settings)

        # 2. Populate temporary index
        if not hasattr(model_class, "search_index_name") or not model_class.search_index_name:
            raise ValueError(f"{model_class.__name__} does not define search_index_name.")

        qs = model_class.get_search_queryset()

        if bulk:
            from icv_search.conf import ICV_SEARCH_BULK_BATCH_SIZE

            effective_batch = batch_size if batch_size != 1000 else ICV_SEARCH_BULK_BATCH_SIZE
            total_count = qs.count()

            def _doc_generator() -> Iterable[dict[str, Any]]:
                for instance in qs.iterator(chunk_size=effective_batch):
                    yield instance.to_search_document()

            total = bulk_index(
                uid=temp_uid,
                documents=_doc_generator(),
                primary_key=index.primary_key_field,
                batch_size=effective_batch,
                progress_callback=progress_callback,
                total_hint=total_count,
            )
        else:
            total = 0
            batch_buf: list[dict[str, Any]] = []

            for instance in qs.iterator(chunk_size=batch_size):
                batch_buf.append(instance.to_search_document())
                if len(batch_buf) >= batch_size:
                    backend.add_documents(
                        uid=temp_uid,
                        documents=batch_buf,
                        primary_key=index.primary_key_field,
                    )
                    total += len(batch_buf)
                    batch_buf = []

            if batch_buf:
                backend.add_documents(
                    uid=temp_uid,
                    documents=batch_buf,
                    primary_key=index.primary_key_field,
                )
                total += len(batch_buf)

        # 3. Swap indexes atomically
        try:
            backend.swap_indexes([(index.engine_uid, temp_uid)])
        except NotImplementedError:
            # Backend claimed to have swap but doesn't — clean up and fall back
            logger.warning("swap_indexes raised NotImplementedError — falling back.")
            try:
                backend.delete_index(uid=temp_uid)
            except Exception:
                pass
            return reindex_all(index, model_class, tenant_id=tenant_id, batch_size=batch_size)

        # 4. Delete the old index (now under the temp name after swap)
        try:
            backend.delete_index(uid=temp_uid)
        except Exception:
            logger.warning("Failed to delete temporary index '%s' after swap.", temp_uid)

        log.mark_complete(status="success", detail=f"Zero-downtime reindex: {total} documents.")
        logger.info("Zero-downtime reindex of '%s' complete: %d documents.", index.name, total)
        return total

    except Exception as exc:
        # Clean up temporary index on failure
        try:
            backend.delete_index(uid=temp_uid)
        except Exception:
            pass
        log.mark_complete(status="failed", detail=str(exc))
        logger.exception("Zero-downtime reindex failed for '%s'.", index.name)
        raise
