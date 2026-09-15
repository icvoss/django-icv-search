"""Signal handlers for icv-search."""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from icv_search.signals import documents_indexed, documents_removed

logger = logging.getLogger(__name__)


@receiver(post_save, sender="icv_search.SearchIndex")
def on_search_index_save(sender, instance, created, **kwargs):
    """Dispatch settings sync when a SearchIndex is saved and AUTO_SYNC is enabled."""
    from icv_search.conf import ICV_SEARCH_AUTO_SYNC

    if not ICV_SEARCH_AUTO_SYNC:
        return

    if created:
        logger.info("SearchIndex '%s' created — dispatching engine provisioning.", instance.name)
    else:
        logger.info("SearchIndex '%s' updated — dispatching settings sync.", instance.name)

    # Mark as unsynced (will be synced by the task or synchronous call below)
    if instance.is_synced:
        from icv_search.models import SearchIndex

        SearchIndex.objects.filter(pk=instance.pk).update(is_synced=False)

    from icv_search.conf import ICV_SEARCH_ASYNC_INDEXING

    if ICV_SEARCH_ASYNC_INDEXING:
        try:
            from icv_search.tasks import sync_index_settings

            sync_index_settings.delay(str(instance.pk))
        except Exception:
            logger.warning(
                "Celery unavailable — falling back to synchronous sync for index '%s'.",
                instance.name,
            )
            from icv_search.services.indexing import _sync_index_to_engine

            _sync_index_to_engine(instance)
    else:
        from icv_search.services.indexing import _sync_index_to_engine

        _sync_index_to_engine(instance)


@receiver(documents_indexed)
def on_documents_indexed(sender, instance, **kwargs):
    """Invalidate the search result cache when documents are indexed.

    Called after :func:`~icv_search.services.documents.index_documents`
    successfully adds or updates documents in the engine.  Stale cache
    entries for the affected index are cleared so subsequent searches reflect
    the updated content.
    """
    from django.conf import settings

    if not getattr(settings, "ICV_SEARCH_CACHE_ENABLED", False):
        return

    from icv_search.cache import ICVSearchCache

    cache = ICVSearchCache()
    cache.invalidate(instance.name, engine_uid=instance.engine_uid)
    logger.debug(
        "Cache invalidated for index '%s' after documents were indexed.",
        instance.name,
    )


@receiver(documents_removed)
def on_documents_removed(sender, instance, **kwargs):
    """Invalidate the search result cache when documents are removed.

    Called after :func:`~icv_search.services.documents.remove_documents`
    successfully deletes documents from the engine.  Stale cache entries for
    the affected index are cleared so subsequent searches reflect the removal.
    """
    from django.conf import settings

    if not getattr(settings, "ICV_SEARCH_CACHE_ENABLED", False):
        return

    from icv_search.cache import ICVSearchCache

    cache = ICVSearchCache()
    cache.invalidate(instance.name, engine_uid=instance.engine_uid)
    logger.debug(
        "Cache invalidated for index '%s' after documents were removed.",
        instance.name,
    )
