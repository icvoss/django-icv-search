"""Index management service functions."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.utils import timezone

from icv_search.backends import get_search_backend
from icv_search.exceptions import SearchBackendError
from icv_search.models import IndexSyncLog, SearchIndex
from icv_search.services._utils import resolve_index
from icv_search.signals import search_index_created, search_index_deleted, search_index_synced
from icv_search.types import IndexStats, TaskResult

logger = logging.getLogger(__name__)


def get_model_search_settings(model_class: type | None = None) -> dict[str, Any]:
    """Extract search engine settings from a SearchableMixin model class.

    Reads ``search_filterable_fields``, ``search_sortable_fields``, and
    ``search_fields`` from the model class (as declared on
    :class:`~icv_search.mixins.SearchableMixin`) and returns a settings dict
    suitable for passing to :func:`update_index_settings`.

    Args:
        model_class: A Django model class that uses ``SearchableMixin``, or
            ``None`` (returns an empty dict).

    Returns:
        A dict with ``filterableAttributes``, ``sortableAttributes``, and/or
        ``searchableAttributes`` keys, populated only when the corresponding
        class attribute is non-empty.
    """
    if model_class is None:
        return {}

    result: dict[str, Any] = {}

    filterable = getattr(model_class, "search_filterable_fields", [])
    if filterable:
        result["filterableAttributes"] = list(filterable)

    sortable = getattr(model_class, "search_sortable_fields", [])
    if sortable:
        result["sortableAttributes"] = list(sortable)

    searchable = getattr(model_class, "search_fields", [])
    if searchable:
        result["searchableAttributes"] = list(searchable)

    displayed = getattr(model_class, "search_displayed_fields", [])
    if displayed:
        result["displayedAttributes"] = list(displayed)

    return result


def create_index(
    name: str,
    tenant_id: str = "",
    settings: dict[str, Any] | None = None,
    primary_key: str = "id",
    model_class: type | None = None,
) -> SearchIndex:
    """Create a new SearchIndex and provision it in the search engine.

    If ``model_class`` is supplied and it declares any of the
    ``SearchableMixin`` field lists (``search_fields``,
    ``search_filterable_fields``, ``search_sortable_fields``), those values
    are merged into the index settings before the index is created.
    Explicitly passed ``settings`` take precedence over values derived from
    the model class.

    Args:
        name: Logical index name (e.g. "products").
        tenant_id: Tenant identifier for multi-tenant setups.
        settings: Optional index settings (searchable/filterable/sortable attrs, etc.).
        primary_key: Document field used as primary key.
        model_class: Optional ``SearchableMixin`` model class whose field
            declarations should seed the index settings.

    Returns:
        The created SearchIndex instance.
    """
    # Merge mixin settings (model_class) with any explicitly-passed settings.
    # Explicit settings win on key collisions.
    merged_settings: dict[str, Any] = get_model_search_settings(model_class)
    if settings:
        merged_settings.update(settings)

    index = SearchIndex(
        name=name,
        tenant_id=tenant_id,
        primary_key_field=primary_key,
        settings=merged_settings,
    )
    index.save()

    backend = get_search_backend()
    log = IndexSyncLog.objects.create(index=index, action="created", status="pending")

    try:
        raw_result = backend.create_index(uid=index.engine_uid, primary_key=primary_key)
        task_result = TaskResult.from_engine(raw_result)
        log.task_uid = task_result.task_uid

        if merged_settings:
            backend.update_settings(uid=index.engine_uid, settings=merged_settings)

        index.mark_synced()
        log.mark_complete(status="success")

        search_index_created.send(sender=SearchIndex, instance=index)
        logger.info("Created search index '%s' (engine_uid: %s).", name, index.engine_uid)

    except SearchBackendError as exc:
        log.mark_complete(status="failed", detail=str(exc))
        logger.exception("Failed to create search index '%s' in engine.", name)
        raise

    return index


def delete_index(name_or_index: str | SearchIndex, tenant_id: str = "") -> None:
    """Delete a SearchIndex and remove it from the search engine.

    Args:
        name_or_index: Index name or SearchIndex instance.
        tenant_id: Tenant identifier (only needed if passing a name).
    """
    index = resolve_index(name_or_index, tenant_id)
    backend = get_search_backend()
    log = IndexSyncLog.objects.create(index=index, action="deleted", status="pending")

    try:
        backend.delete_index(uid=index.engine_uid)
        log.mark_complete(status="success")
        search_index_deleted.send(sender=SearchIndex, instance=index)
        logger.info("Deleted search index '%s'.", index.name)
    except SearchBackendError as exc:
        log.mark_complete(status="failed", detail=str(exc))
        logger.exception("Failed to delete search index '%s' from engine.", index.name)
        raise

    index.delete()


def update_index_settings(
    name_or_index: str | SearchIndex,
    settings: dict[str, Any],
    tenant_id: str = "",
) -> SearchIndex:
    """Update a SearchIndex's settings and sync to the engine.

    Args:
        name_or_index: Index name or SearchIndex instance.
        settings: New settings to merge with existing.
        tenant_id: Tenant identifier (only needed if passing a name).

    Returns:
        The updated SearchIndex instance.
    """
    index = resolve_index(name_or_index, tenant_id)
    index.settings.update(settings)
    index.save()

    _sync_index_to_engine(index)
    return index


def get_index_stats(name_or_index: str | SearchIndex, tenant_id: str = "") -> IndexStats:
    """Get stats for an index from the search engine.

    Args:
        name_or_index: Index name or SearchIndex instance.
        tenant_id: Tenant identifier (only needed if passing a name).

    Returns:
        Normalised IndexStats instance.
    """
    index = resolve_index(name_or_index, tenant_id)
    backend = get_search_backend()
    raw = backend.get_stats(uid=index.engine_uid)
    return IndexStats.from_engine(raw)


def get_index_settings(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> dict[str, Any]:
    """Retrieve current engine-side settings for an index.

    Fetches the live settings directly from the search engine rather than
    returning the locally-cached settings stored in the ``SearchIndex`` model.
    Useful for verifying what the engine currently holds, or for reading
    settings that were applied outside of Django (e.g. via the engine's own
    API).

    Args:
        name_or_index: Index name or SearchIndex instance.
        tenant_id: Tenant identifier (only needed if passing a name).

    Returns:
        Raw settings dict as returned by the search engine.
    """
    index = resolve_index(name_or_index, tenant_id)
    backend = get_search_backend()
    return backend.get_settings(uid=index.engine_uid)


def get_synonyms(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> dict[str, list[str]]:
    """Get the current synonyms configured for an index.

    Fetches live settings from the engine and extracts the ``synonyms`` entry.

    Args:
        name_or_index: Index name or SearchIndex instance.
        tenant_id: Tenant identifier (only needed if passing a name).

    Returns:
        Dict mapping each word to its list of synonyms.  Empty dict when no
        synonyms are configured.
    """
    settings = get_index_settings(name_or_index, tenant_id=tenant_id)
    return settings.get("synonyms", {})


def update_synonyms(
    name_or_index: str | SearchIndex,
    synonyms: dict[str, list[str]],
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Set synonyms for an index, merging with any existing synonyms.

    Fetches the current synonyms from the engine, merges the provided
    ``synonyms`` on top (caller-supplied values win on key collision), then
    pushes the merged result back to the engine via
    :func:`update_index_settings`.

    Args:
        name_or_index: Index name or SearchIndex instance.
        synonyms: Synonyms to add or update.  Keys are words; values are lists
            of synonymous words.
        tenant_id: Tenant identifier (only needed if passing a name).

    Returns:
        The updated SearchIndex instance.
    """
    current = get_synonyms(name_or_index, tenant_id=tenant_id)
    merged = {**current, **synonyms}
    return update_index_settings(name_or_index, {"synonyms": merged}, tenant_id)


def reset_synonyms(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Remove all synonyms from an index.

    Pushes an empty synonyms dict to the engine via
    :func:`update_index_settings`.

    Args:
        name_or_index: Index name or SearchIndex instance.
        tenant_id: Tenant identifier (only needed if passing a name).

    Returns:
        The updated SearchIndex instance.
    """
    return update_index_settings(name_or_index, {"synonyms": {}}, tenant_id)


def get_stop_words(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> list[str]:
    """Get the current stop words configured for an index.

    Fetches live settings from the engine and extracts the ``stopWords`` entry.

    Args:
        name_or_index: Index name or SearchIndex instance.
        tenant_id: Tenant identifier (only needed if passing a name).

    Returns:
        List of stop words.  Empty list when none are configured.
    """
    settings = get_index_settings(name_or_index, tenant_id=tenant_id)
    return settings.get("stopWords", [])


def update_stop_words(
    name_or_index: str | SearchIndex,
    stop_words: list[str],
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Set stop words for an index.

    Replaces the current stop-word list entirely with the provided
    ``stop_words`` list via :func:`update_index_settings`.

    Args:
        name_or_index: Index name or SearchIndex instance.
        stop_words: Complete list of stop words to apply.
        tenant_id: Tenant identifier (only needed if passing a name).

    Returns:
        The updated SearchIndex instance.
    """
    return update_index_settings(name_or_index, {"stopWords": stop_words}, tenant_id)


def reset_stop_words(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Remove all stop words from an index.

    Pushes an empty stop-words list to the engine via
    :func:`update_index_settings`.

    Args:
        name_or_index: Index name or SearchIndex instance.
        tenant_id: Tenant identifier (only needed if passing a name).

    Returns:
        The updated SearchIndex instance.
    """
    return update_index_settings(name_or_index, {"stopWords": []}, tenant_id)


def _sync_index_to_engine(index: SearchIndex) -> None:
    """Push index settings to the engine. Internal helper."""
    backend = get_search_backend()
    log = IndexSyncLog.objects.create(index=index, action="settings_updated", status="pending")

    try:
        raw_result = backend.update_settings(uid=index.engine_uid, settings=index.settings)
        task_result = TaskResult.from_engine(raw_result)
        log.task_uid = task_result.task_uid
        log.mark_complete(status="success")
        index.mark_synced()
        search_index_synced.send(sender=SearchIndex, instance=index)
        logger.info("Synced settings for index '%s'.", index.name)
    except SearchBackendError as exc:
        log.mark_complete(status="failed", detail=str(exc))
        logger.exception("Failed to sync settings for index '%s'.", index.name)
        raise


def get_typo_tolerance(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> dict:
    """Get typo tolerance settings for an index.

    Returns the ``typoTolerance`` portion of the index settings dict.
    When no typo tolerance settings have been configured, an empty dict is
    returned.

    Args:
        name_or_index: Index name or SearchIndex instance.
        tenant_id: Tenant identifier (only needed if passing a name).

    Returns:
        The ``typoTolerance`` settings dict (may be empty).
    """
    index = resolve_index(name_or_index, tenant_id)
    backend = get_search_backend()
    settings = backend.get_settings(uid=index.engine_uid)
    return settings.get("typoTolerance", {})


def update_typo_tolerance(
    name_or_index: str | SearchIndex,
    settings: dict,
    *,
    tenant_id: str = "",
) -> None:
    """Update typo tolerance settings for an index.

    Merges the provided ``settings`` into the ``typoTolerance`` key of the
    index settings and pushes the change to the search engine.

    Args:
        name_or_index: Index name or SearchIndex instance.
        settings: Typo tolerance settings dict (e.g.
            ``{"enabled": True, "minWordSizeForTypos": {"oneTypo": 5}}``).
        tenant_id: Tenant identifier (only needed if passing a name).
    """
    update_index_settings(name_or_index, {"typoTolerance": settings}, tenant_id=tenant_id)


def get_displayed_attributes(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> list[str]:
    """Get the displayed attributes for an index.

    Returns the ``displayedAttributes`` portion of the index settings.
    When ``["*"]`` (the default), all fields are returned in search results.
    """
    settings = get_index_settings(name_or_index, tenant_id=tenant_id)
    return settings.get("displayedAttributes", ["*"])


def update_displayed_attributes(
    name_or_index: str | SearchIndex,
    attributes: list[str],
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Set the displayed attributes for an index.

    Controls which fields are included in search results. Pass ``["*"]`` to
    return all fields (the Meilisearch default).
    """
    return update_index_settings(name_or_index, {"displayedAttributes": attributes}, tenant_id)


def reset_displayed_attributes(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Reset displayed attributes to the default (all fields)."""
    return update_index_settings(name_or_index, {"displayedAttributes": ["*"]}, tenant_id)


def get_distinct_attribute(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> str | None:
    """Get the distinct attribute for an index.

    Returns the ``distinctAttribute`` value, or ``None`` when not set.
    """
    settings = get_index_settings(name_or_index, tenant_id=tenant_id)
    return settings.get("distinctAttribute")


def update_distinct_attribute(
    name_or_index: str | SearchIndex,
    attribute: str | None,
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Set or clear the distinct attribute for an index.

    When set, only one document per distinct field value appears in results.
    Pass ``None`` to disable deduplication.
    """
    return update_index_settings(name_or_index, {"distinctAttribute": attribute}, tenant_id)


def get_pagination_settings(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> dict:
    """Get pagination settings for an index.

    Returns the ``pagination`` portion of the index settings, including
    ``maxTotalHits`` (default 1000).
    """
    settings = get_index_settings(name_or_index, tenant_id=tenant_id)
    return settings.get("pagination", {})


def update_pagination_settings(
    name_or_index: str | SearchIndex,
    max_total_hits: int,
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Set the maximum total hits for pagination.

    Controls the hard cap on the result window. Higher values allow deeper
    pagination but increase memory usage.
    """
    return update_index_settings(name_or_index, {"pagination": {"maxTotalHits": max_total_hits}}, tenant_id)


def get_faceting_settings(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> dict:
    """Get faceting settings for an index.

    Returns the ``faceting`` portion of the index settings, including
    ``maxValuesPerFacet`` and ``sortFacetValuesBy``.
    """
    settings = get_index_settings(name_or_index, tenant_id=tenant_id)
    return settings.get("faceting", {})


def update_faceting_settings(
    name_or_index: str | SearchIndex,
    settings: dict,
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Update faceting settings for an index.

    Accepts a dict with any of: ``maxValuesPerFacet`` (int),
    ``sortFacetValuesBy`` (dict mapping facet name to ``"alpha"`` or
    ``"count"``).
    """
    return update_index_settings(name_or_index, {"faceting": settings}, tenant_id=tenant_id)


def get_proximity_precision(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> str:
    """Get the proximity precision setting for an index.

    Returns ``"byWord"`` (default) or ``"byAttribute"``.
    """
    index_settings = get_index_settings(name_or_index, tenant_id=tenant_id)
    return index_settings.get("proximityPrecision", "byWord")


def update_proximity_precision(
    name_or_index: str | SearchIndex,
    precision: str,
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Set the proximity precision for an index.

    Args:
        precision: ``"byWord"`` or ``"byAttribute"``.
    """
    return update_index_settings(name_or_index, {"proximityPrecision": precision}, tenant_id)


def get_search_cutoff(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> int | None:
    """Get the search cutoff time in milliseconds for an index.

    Returns the ``searchCutoffMs`` value, or ``None`` when using the default
    (1500ms).
    """
    settings = get_index_settings(name_or_index, tenant_id=tenant_id)
    return settings.get("searchCutoffMs")


def update_search_cutoff(
    name_or_index: str | SearchIndex,
    cutoff_ms: int | None,
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Set the search timeout in milliseconds for an index.

    Pass ``None`` to reset to the default (1500ms). Searches exceeding this
    duration are aborted and return partial results.
    """
    return update_index_settings(name_or_index, {"searchCutoffMs": cutoff_ms}, tenant_id)


def get_dictionary(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> list[str]:
    """Get the custom dictionary for an index.

    Returns multi-word strings that should be treated as single tokens
    during indexing and search.
    """
    settings = get_index_settings(name_or_index, tenant_id=tenant_id)
    return settings.get("dictionary", [])


def update_dictionary(
    name_or_index: str | SearchIndex,
    words: list[str],
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Set the custom dictionary for an index.

    Multi-word strings in the dictionary are treated as single tokens.
    Useful for technical terms like ``"J. K. Rowling"`` or ``"C++"``.
    """
    return update_index_settings(name_or_index, {"dictionary": words}, tenant_id)


def reset_dictionary(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Reset the custom dictionary to empty."""
    return update_index_settings(name_or_index, {"dictionary": []}, tenant_id)


def get_separator_tokens(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> list[str]:
    """Get custom separator tokens for an index."""
    settings = get_index_settings(name_or_index, tenant_id=tenant_id)
    return settings.get("separatorTokens", [])


def update_separator_tokens(
    name_or_index: str | SearchIndex,
    tokens: list[str],
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Set characters that should act as word separators."""
    return update_index_settings(name_or_index, {"separatorTokens": tokens}, tenant_id)


def reset_separator_tokens(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Reset separator tokens to the default set."""
    return update_index_settings(name_or_index, {"separatorTokens": []}, tenant_id)


def get_non_separator_tokens(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> list[str]:
    """Get custom non-separator tokens for an index."""
    settings = get_index_settings(name_or_index, tenant_id=tenant_id)
    return settings.get("nonSeparatorTokens", [])


def update_non_separator_tokens(
    name_or_index: str | SearchIndex,
    tokens: list[str],
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Set characters that should NOT act as word separators."""
    return update_index_settings(name_or_index, {"nonSeparatorTokens": tokens}, tenant_id)


def reset_non_separator_tokens(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Reset non-separator tokens to the default set."""
    return update_index_settings(name_or_index, {"nonSeparatorTokens": []}, tenant_id)


def get_prefix_search(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> str:
    """Get the prefix search setting for an index.

    Returns ``"indexingTime"`` (default) or ``"disabled"``.
    """
    settings = get_index_settings(name_or_index, tenant_id=tenant_id)
    return settings.get("prefixSearch", "indexingTime")


def update_prefix_search(
    name_or_index: str | SearchIndex,
    mode: str,
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Set the prefix search mode for an index.

    Args:
        mode: ``"indexingTime"`` (enable prefix matching, default) or
            ``"disabled"`` (only match exact words).
    """
    return update_index_settings(name_or_index, {"prefixSearch": mode}, tenant_id)


def get_embedders(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> dict[str, Any]:
    """Get the embedder configurations for an index.

    Returns a dict mapping embedder names to their configuration.  Empty
    dict when no embedders are configured.
    """
    settings = get_index_settings(name_or_index, tenant_id=tenant_id)
    return settings.get("embedders", {})


def update_embedders(
    name_or_index: str | SearchIndex,
    embedders: dict[str, Any],
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Set or update embedder configurations for an index.

    Each embedder is a named config with at minimum a ``source`` key.
    Supported sources: ``openAi``, ``huggingFace``, ``ollama``, ``rest``,
    ``userProvided``, ``composite``.

    Example::

        update_embedders("products", {
            "default": {
                "source": "openAi",
                "apiKey": "sk-...",
                "model": "text-embedding-3-small",
                "dimensions": 1536,
            }
        })
    """
    return update_index_settings(name_or_index, {"embedders": embedders}, tenant_id)


def reset_embedders(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Remove all embedder configurations from an index."""
    return update_index_settings(name_or_index, {"embedders": None}, tenant_id)


def get_localized_attributes(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Get localised attribute rules for an index.

    Each rule maps attribute patterns to locale codes for language-specific
    tokenisation.
    """
    settings = get_index_settings(name_or_index, tenant_id=tenant_id)
    return settings.get("localizedAttributes", [])


def update_localized_attributes(
    name_or_index: str | SearchIndex,
    rules: list[dict[str, Any]],
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Set localised attribute rules for an index.

    Example::

        update_localized_attributes("products", [
            {"attributePatterns": ["name_ja", "desc_ja"], "locales": ["jpn"]},
            {"attributePatterns": ["name_*"], "locales": ["eng"]},
        ])
    """
    return update_index_settings(name_or_index, {"localizedAttributes": rules}, tenant_id)


def reset_localized_attributes(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Remove all localised attribute rules from an index."""
    return update_index_settings(name_or_index, {"localizedAttributes": []}, tenant_id)


def get_ranking_rules(
    name_or_index: str | SearchIndex,
    *,
    tenant_id: str = "",
) -> list[str]:
    """Get the ranking rules for an index.

    Returns the ordered list of ranking rules.  Default Meilisearch order:
    ``["words", "typo", "proximity", "attribute", "sort", "exactness"]``.
    """
    settings = get_index_settings(name_or_index, tenant_id=tenant_id)
    return settings.get("rankingRules", [])


def update_ranking_rules(
    name_or_index: str | SearchIndex,
    rules: list[str],
    *,
    tenant_id: str = "",
) -> SearchIndex:
    """Set the ranking rules for an index.

    Order matters — each rule is a tiebreaker for the previous one.
    """
    return update_index_settings(name_or_index, {"rankingRules": rules}, tenant_id)


def compact_index(
    name_or_index: str | SearchIndex,
    tenant_id: str = "",
) -> dict[str, Any]:
    """Reclaim storage space and optimise the index.

    No-op on engines that manage compaction automatically.

    Args:
        name_or_index: Index name or SearchIndex instance.
        tenant_id: Tenant identifier (only needed if passing a name).

    Returns:
        Engine response dict (empty dict for no-op engines).
    """
    index = resolve_index(name_or_index, tenant_id)
    backend = get_search_backend()
    return backend.compact(uid=index.engine_uid)


def clear_sync_logs(*, days_older_than: int = 90) -> int:
    """Delete IndexSyncLog rows older than the given number of days.

    Args:
        days_older_than: Records created more than this many days ago are
            deleted.

    Returns:
        Number of records deleted.
    """
    cutoff = timezone.now() - timedelta(days=days_older_than)
    deleted_count, _ = IndexSyncLog.objects.filter(created_at__lt=cutoff).delete()
    logger.info("Deleted %d old index sync logs (older than %d days).", deleted_count, days_older_than)
    return deleted_count
