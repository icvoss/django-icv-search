"""Automatic indexing of Django models via ICV_SEARCH_AUTO_INDEX configuration."""

from __future__ import annotations

import logging
import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from django.apps import apps
from django.db.models.signals import post_delete, post_save
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)

# Thread-local storage for skip_index_update context manager
_skip_state = threading.local()

# Registry of currently-connected signal handlers.
# Maps dispatch_uid → model class so we can pass the correct sender on disconnect.
# Format: {"icv_search_auto_index_save_articles": <class Article>, ...}
_connected_signals: dict[str, type] = {}


# Sentinel used to distinguish "field absent" from "field present but None".
_SENTINEL = object()


def _is_skipped() -> bool:
    """Check if auto-indexing is currently skipped (via context manager)."""
    return getattr(_skip_state, "skip", False)


def _is_soft_deleted(instance: Any) -> bool:
    """Return ``True`` if the instance has been soft-deleted.

    Checks two common soft-delete field conventions:

    - ``is_deleted`` (boolean) — returns ``True`` when the field value is
      truthy.
    - ``deleted_at`` (nullable datetime) — returns ``True`` when the field
      value is not ``None``.

    Returns ``False`` for instances that have neither field (i.e. models that
    do not use soft deletion).
    """
    is_deleted = getattr(instance, "is_deleted", None)
    if is_deleted is not None and is_deleted:
        return True

    deleted_at = getattr(instance, "deleted_at", _SENTINEL)
    return deleted_at is not _SENTINEL and deleted_at is not None


@contextmanager
def skip_index_update() -> Generator[None, None, None]:
    """Context manager to temporarily disable auto-indexing.

    Useful for bulk imports, data migrations, and test factories where
    you don't want every individual save to trigger an index update.

    The context manager is nestable — the outer skip remains active until
    the outermost ``with`` block exits.

    Example::

        from icv_search.auto_index import skip_index_update

        with skip_index_update():
            Article.objects.bulk_create(articles)
            # No search index updates during this block

        # Auto-indexing resumes here
    """
    old = getattr(_skip_state, "skip", False)
    _skip_state.skip = True
    try:
        yield
    finally:
        _skip_state.skip = old


def _get_auto_index_config() -> dict[str, dict[str, Any]]:
    """Read ICV_SEARCH_AUTO_INDEX from Django settings."""
    from django.conf import settings

    return getattr(settings, "ICV_SEARCH_AUTO_INDEX", {})


def _handle_post_save(
    sender: type,
    instance: Any,
    created: bool,
    index_name: str,
    config: dict[str, Any],
    **kwargs: Any,
) -> None:
    """Handle post_save signal for an auto-indexed model."""
    if _is_skipped():
        return

    if not config.get("on_save", True):
        return

    # Check should_update callable
    should_update_path = config.get("should_update")
    if should_update_path:
        func = import_string(should_update_path)
        if not func(instance):
            logger.debug(
                "Skipping auto-index for %s (pk=%s) — should_update returned False.",
                sender.__name__,
                instance.pk,
            )
            return

    # Soft-delete awareness: if the instance has been soft-deleted, remove it
    # from the index rather than indexing it.  This handles the common pattern
    # where soft_delete() sets is_deleted=True (or deleted_at) and calls
    # save(), which fires post_save.
    if _is_soft_deleted(instance):
        logger.debug(
            "Auto-removing soft-deleted %s (pk=%s) from index '%s'.",
            sender.__name__,
            instance.pk,
            index_name,
        )
        _remove_instance(instance, index_name, config)
        return

    _index_instance(instance, index_name, config)


def _handle_post_delete(
    sender: type,
    instance: Any,
    index_name: str,
    config: dict[str, Any],
    **kwargs: Any,
) -> None:
    """Handle post_delete signal for an auto-indexed model."""
    if _is_skipped():
        return

    if not config.get("on_delete", True):
        return

    _remove_instance(instance, index_name, config)


def _get_debounce_seconds() -> int:
    """Read ICV_SEARCH_DEBOUNCE_SECONDS from Django settings."""
    from django.conf import settings

    return getattr(settings, "ICV_SEARCH_DEBOUNCE_SECONDS", 0)


def _get_debounce_max_buffer_size() -> int:
    """Read ICV_SEARCH_DEBOUNCE_MAX_BUFFER_SIZE from Django settings."""
    from django.conf import settings

    return getattr(settings, "ICV_SEARCH_DEBOUNCE_MAX_BUFFER_SIZE", 500)


def _debounce_buffer_append(
    *,
    index_pk: str,
    cache_key: str,
    task_key: str,
    item: Any,
    debounce_seconds: int,
    flush_task_import: str,
    synchronous_fallback: Any,
) -> None:
    """Append *item* to a per-index cache buffer and schedule its flush task.

    Shared by the save-side (document) and delete-side (document ID) debounce
    paths. The flush is scheduled after ``debounce_seconds`` on the first
    item added to an empty buffer, or immediately (``countdown=0``) once the
    buffer reaches ``ICV_SEARCH_DEBOUNCE_MAX_BUFFER_SIZE`` (#7). Without a
    size-triggered early flush, a high-rate save/delete loop accumulates the
    entire buffer in memory and only sends it, as a single unbounded request,
    once the time window finally elapses.

    If scheduling the flush task fails (for example, no Celery broker is
    configured), the buffered item is removed from the buffer and processed
    via ``synchronous_fallback`` immediately, matching the pre-existing
    behaviour: buffered items must never sit unflushed indefinitely because
    no flush could ever be scheduled for them.
    """
    from django.core.cache import cache

    buffer = cache.get(cache_key, [])
    buffer.append(item)
    # Set with a TTL longer than the debounce window to avoid premature expiry.
    cache.set(cache_key, buffer, timeout=debounce_seconds * 3)

    max_buffer_size = _get_debounce_max_buffer_size()
    flush_immediately = max_buffer_size > 0 and len(buffer) >= max_buffer_size

    # Schedule the flush task if not already scheduled. The task_key guard
    # prevents scheduling a second flush for the same buffer while one is
    # already pending, whether that pending flush is time-triggered or was
    # itself size-triggered.
    already_scheduled = cache.get(task_key)
    if already_scheduled and not flush_immediately:
        return

    try:
        flush_task = import_string(flush_task_import)
        countdown = 0 if flush_immediately else debounce_seconds
        flush_task.apply_async(args=[index_pk], countdown=countdown)
        # Mark that a task is scheduled. TTL matches the countdown so a new
        # flush can be scheduled once this one is due to have run.
        cache.set(task_key, True, timeout=max(countdown, 1))
    except Exception:
        logger.warning(
            "Could not schedule debounce flush for buffer '%s'; falling back to synchronous processing for this item.",
            cache_key,
        )
        cache.delete(cache_key)
        try:
            synchronous_fallback()
        except Exception:
            logger.exception("Synchronous fallback failed for buffer '%s'.", cache_key)


def _debounce_document(index_name: str, document: dict[str, Any], debounce_seconds: int) -> None:
    """Buffer a document for debounced indexing.

    Appends the document to a per-index cache buffer and schedules (or
    re-schedules) a Celery task to flush the buffer after the debounce
    window expires (or immediately, once the buffer is large enough; see
    ``_debounce_buffer_append``).
    """
    from icv_search.models import SearchIndex

    try:
        index = SearchIndex.objects.get(name=index_name)
    except SearchIndex.DoesNotExist:
        logger.warning(
            "SearchIndex '%s' not found for debouncing; indexing synchronously.",
            index_name,
        )
        from icv_search.services.documents import index_documents

        try:
            index_documents(index_name, [document])
        except Exception:
            logger.exception("Failed to index document for '%s'.", index_name)
        return

    def _synchronous_fallback() -> None:
        from icv_search.services.documents import index_documents

        index_documents(index, [document])

    _debounce_buffer_append(
        index_pk=str(index.pk),
        cache_key=f"icv_search:debounce:{index.pk}",
        task_key=f"icv_search:debounce_task:{index.pk}",
        item=document,
        debounce_seconds=debounce_seconds,
        flush_task_import="icv_search.tasks.flush_debounce_buffer",
        synchronous_fallback=_synchronous_fallback,
    )


def _debounce_removal(index_name: str, document_id: str, debounce_seconds: int) -> None:
    """Buffer a document ID for debounced removal.

    Mirrors :func:`_debounce_document`, giving the delete path the same
    batching the save path has (#6). Without this, a bulk delete that fires
    per-row ``post_delete`` signals dispatches one Celery task per row.
    """
    from icv_search.models import SearchIndex

    try:
        index = SearchIndex.objects.get(name=index_name)
    except SearchIndex.DoesNotExist:
        logger.warning(
            "SearchIndex '%s' not found for debouncing; removing synchronously.",
            index_name,
        )
        from icv_search.services.documents import remove_documents

        try:
            remove_documents(index_name, [document_id])
        except Exception:
            logger.exception("Failed to remove document for '%s'.", index_name)
        return

    def _synchronous_fallback() -> None:
        from icv_search.services.documents import remove_documents

        remove_documents(index, [document_id])

    _debounce_buffer_append(
        index_pk=str(index.pk),
        cache_key=f"icv_search:debounce_removal:{index.pk}",
        task_key=f"icv_search:debounce_removal_task:{index.pk}",
        item=document_id,
        debounce_seconds=debounce_seconds,
        flush_task_import="icv_search.tasks.flush_debounce_removal_buffer",
        synchronous_fallback=_synchronous_fallback,
    )


def _index_instance(instance: Any, index_name: str, config: dict[str, Any]) -> None:
    """Index a single model instance."""
    from icv_search.models import SearchIndex

    if not hasattr(instance, "to_search_document"):
        logger.warning(
            "Model %s does not implement to_search_document() — skipping auto-index.",
            type(instance).__name__,
        )
        return

    # Auto-create the SearchIndex record if configured
    if config.get("auto_create", True):
        _ensure_index_exists(instance, index_name, config)
    else:
        # auto_create=False: skip indexing if no SearchIndex record exists
        if not SearchIndex.objects.filter(name=index_name).exists():
            logger.debug(
                "SearchIndex '%s' does not exist and auto_create=False — skipping.",
                index_name,
            )
            return

    document = instance.to_search_document()

    # Debounce: buffer documents and dispatch a single batch after the window
    debounce_seconds = _get_debounce_seconds()
    if debounce_seconds > 0:
        _debounce_document(index_name, document, debounce_seconds)
        return

    use_async = config.get("async")
    if use_async is None:
        from django.conf import settings as django_settings

        use_async = getattr(django_settings, "ICV_SEARCH_ASYNC_INDEXING", True)

    if use_async:
        try:
            from icv_search.tasks import add_documents

            # Resolve the SearchIndex pk for the task
            try:
                index = SearchIndex.objects.get(name=index_name)
                add_documents.delay(str(index.pk), [document])
                return
            except SearchIndex.DoesNotExist:
                logger.warning(
                    "SearchIndex '%s' not found for async indexing — falling back to sync.",
                    index_name,
                )
        except ImportError:
            logger.debug("Celery not available — falling back to synchronous indexing.")

    # Synchronous fallback
    from icv_search.services.documents import index_documents

    try:
        index_documents(index_name, [document])
    except Exception:
        logger.exception(
            "Failed to auto-index %s (pk=%s) in '%s'.",
            type(instance).__name__,
            instance.pk,
            index_name,
        )


def _remove_instance(instance: Any, index_name: str, config: dict[str, Any]) -> None:
    """Remove a single model instance from the search index.

    The document ID is resolved synchronously before any async dispatch so
    the value is available even after the database row is deleted.
    """
    # Resolve document ID synchronously BEFORE the DB row is gone.
    doc_id = str(instance.pk)

    # Debounce: buffer document IDs and dispatch a single batched removal
    # after the window (#6). Mirrors _index_instance's debounce check below,
    # so a bulk delete that fires per-row post_delete signals coalesces into
    # one removal task per index instead of one task per row.
    debounce_seconds = _get_debounce_seconds()
    if debounce_seconds > 0:
        _debounce_removal(index_name, doc_id, debounce_seconds)
        return

    use_async = config.get("async")
    if use_async is None:
        from django.conf import settings as django_settings

        use_async = getattr(django_settings, "ICV_SEARCH_ASYNC_INDEXING", True)

    if use_async:
        try:
            from icv_search.models import SearchIndex
            from icv_search.tasks import remove_documents

            try:
                index = SearchIndex.objects.get(name=index_name)
                remove_documents.delay(str(index.pk), [doc_id])
                return
            except SearchIndex.DoesNotExist:
                logger.warning(
                    "SearchIndex '%s' not found for async removal — falling back to sync.",
                    index_name,
                )
        except ImportError:
            logger.debug("Celery not available — falling back to synchronous removal.")

    # Synchronous fallback
    from icv_search.services.documents import remove_documents

    try:
        remove_documents(index_name, [doc_id])
    except Exception:
        logger.exception(
            "Failed to auto-remove %s (pk=%s) from '%s'.",
            type(instance).__name__,
            instance.pk,
            index_name,
        )


def _ensure_index_exists(instance: Any, index_name: str, config: dict[str, Any]) -> None:
    """Create the SearchIndex record and engine index if they do not already exist."""
    from icv_search.models import SearchIndex

    if SearchIndex.objects.filter(name=index_name).exists():
        return

    logger.info(
        "Auto-creating SearchIndex '%s' for %s.",
        index_name,
        type(instance).__name__,
    )

    from icv_search.services.indexing import create_index

    try:
        model_class = type(instance)
        create_index(name=index_name, model_class=model_class)
    except Exception:
        logger.exception("Failed to auto-create SearchIndex '%s'.", index_name)


def _disconnect_signal(signal, dispatch_uid: str) -> bool:
    """Disconnect a signal receiver by dispatch_uid, looking up the sender from the registry.

    Django requires the ``sender`` to be passed when the signal was connected with a
    specific sender.  We keep a registry of connected ``dispatch_uid → model_class``
    mappings so we can supply the correct sender here.

    Args:
        signal: The Django signal instance (post_save or post_delete).
        dispatch_uid: The dispatch UID used when connecting the receiver.

    Returns:
        True if the receiver was disconnected, False if it was not found.
    """
    sender = _connected_signals.get(dispatch_uid)
    disconnected = signal.disconnect(sender=sender, dispatch_uid=dispatch_uid)
    if disconnected:
        _connected_signals.pop(dispatch_uid, None)
    return disconnected


def disconnect_auto_index_signals(index_names: list[str] | None = None) -> None:
    """Disconnect auto-index signals for the given index names (or all registered).

    Useful in tests when ``override_settings`` changes the config and you need to
    clear any previously-connected handlers before connecting fresh ones.

    Args:
        index_names: Specific index names to disconnect.  When ``None``, disconnects
            all entries currently tracked in the internal registry.
    """
    if index_names is None:
        # Disconnect everything in the registry
        uids = list(_connected_signals.keys())
        for uid in uids:
            signal = post_save if uid.startswith("icv_search_auto_index_save_") else post_delete
            _disconnect_signal(signal, uid)
        return

    for index_name in index_names:
        save_uid = f"icv_search_auto_index_save_{index_name}"
        delete_uid = f"icv_search_auto_index_delete_{index_name}"
        _disconnect_signal(post_save, save_uid)
        _disconnect_signal(post_delete, delete_uid)


def connect_auto_index_signals() -> None:
    """Connect post_save/post_delete signals for all models in ICV_SEARCH_AUTO_INDEX.

    Called from ``IcvSearchConfig.ready()``. Only connects signals for models
    that are explicitly configured — no global signal hookup.

    Uses ``dispatch_uid`` on every connection so this function is idempotent;
    calling it multiple times (e.g. during test runs with ``override_settings``)
    will replace existing handlers rather than duplicate them.

    When ``on_save`` or ``on_delete`` is ``False`` for an index, any previously
    registered handler for that signal/index combination is explicitly disconnected
    to prevent stale handlers from prior configurations.
    """
    config = _get_auto_index_config()
    if not config:
        return

    for index_name, index_config in config.items():
        model_path = index_config.get("model")
        if not model_path:
            logger.warning(
                "ICV_SEARCH_AUTO_INDEX['%s'] missing 'model' key — skipping.",
                index_name,
            )
            continue

        try:
            # Resolve dotted path to model class — expects "app_label.ModelName"
            app_label, model_name = model_path.rsplit(".", 1)
            model_class = apps.get_model(app_label, model_name)
        except (LookupError, ValueError):
            logger.warning(
                "ICV_SEARCH_AUTO_INDEX['%s']: could not resolve model '%s' — skipping.",
                index_name,
                model_path,
            )
            continue

        # Validate the model uses SearchableMixin
        if not hasattr(model_class, "to_search_document"):
            logger.warning(
                "ICV_SEARCH_AUTO_INDEX['%s']: model %s does not use SearchableMixin — skipping.",
                index_name,
                model_path,
            )
            continue

        save_uid = f"icv_search_auto_index_save_{index_name}"
        delete_uid = f"icv_search_auto_index_delete_{index_name}"

        # Connect post_save with bound config via closure.
        # When on_save is False, disconnect any stale handler from a prior config.
        if index_config.get("on_save", True):

            def make_save_handler(idx_name: str, cfg: dict[str, Any]):
                def handler(sender: type, instance: Any, created: bool, **kwargs: Any) -> None:
                    _handle_post_save(sender, instance, created, index_name=idx_name, config=cfg, **kwargs)

                return handler

            post_save.connect(
                make_save_handler(index_name, index_config),
                sender=model_class,
                weak=False,
                dispatch_uid=save_uid,
            )
            _connected_signals[save_uid] = model_class
        else:
            # Explicitly remove any previously-registered save handler (with correct sender)
            _disconnect_signal(post_save, save_uid)

        # Connect post_delete with bound config via closure.
        # When on_delete is False, disconnect any stale handler.
        if index_config.get("on_delete", True):

            def make_delete_handler(idx_name: str, cfg: dict[str, Any]):
                def handler(sender: type, instance: Any, **kwargs: Any) -> None:
                    _handle_post_delete(sender, instance, index_name=idx_name, config=cfg, **kwargs)

                return handler

            post_delete.connect(
                make_delete_handler(index_name, index_config),
                sender=model_class,
                weak=False,
                dispatch_uid=delete_uid,
            )
            _connected_signals[delete_uid] = model_class
        else:
            # Explicitly remove any previously-registered delete handler
            _disconnect_signal(post_delete, delete_uid)

        logger.info(
            "Auto-index connected for '%s' → %s (save=%s, delete=%s).",
            index_name,
            model_path,
            index_config.get("on_save", True),
            index_config.get("on_delete", True),
        )
