"""Search query service functions."""

from __future__ import annotations

import logging
import random
from typing import Any

from icv_search.backends import get_search_backend
from icv_search.models import SearchIndex
from icv_search.services._utils import resolve_index, resolve_tenant_id
from icv_search.types import SearchResult, TaskResult

logger = logging.getLogger(__name__)


def _get_cache():
    """Return an :class:`~icv_search.cache.ICVSearchCache` instance if caching is enabled.

    Reads ``ICV_SEARCH_CACHE_ENABLED`` at call time so that pytest
    ``settings`` fixture overrides take effect.

    Returns:
        An ``ICVSearchCache`` instance, or ``None`` when caching is disabled.
    """
    from django.conf import settings

    if not getattr(settings, "ICV_SEARCH_CACHE_ENABLED", False):
        return None
    from icv_search.cache import ICVSearchCache

    return ICVSearchCache()


def search(
    name_or_index: str | SearchIndex,
    query: str,
    tenant_id: str = "",
    *,
    user: Any = None,
    metadata: dict[str, Any] | None = None,
    log_query: bool = True,
    **params: Any,
) -> SearchResult:
    """Execute a search query against a search index.

    The ``tenant_id`` is resolved using the following precedence:

    1. The explicit ``tenant_id`` argument (when non-empty).
    2. The request-scoped tenant set by
       :class:`~icv_search.middleware.ICVSearchTenantMiddleware`.
    3. An empty string (single-tenant / no-tenant mode).

    Results are served from the cache (when ``ICV_SEARCH_CACHE_ENABLED=True``)
    unless ``user`` is present — analytics-aware searches may vary per user
    and must always hit the backend.

    When ``ICV_SEARCH_LOG_QUERIES`` is ``True`` a
    :class:`~icv_search.models.analytics.SearchQueryLog` record is created
    after each call.  Logging failures never interrupt the search result.

    Args:
        name_or_index: Index name or SearchIndex instance.
        query: The search query string.
        tenant_id: Tenant identifier (only needed if passing a name).
        user: Optional authenticated user instance for analytics logging.
        metadata: Optional dict of extra context stored on the query log
            (e.g. ``{"page": "homepage", "session": "abc"}``).
        log_query: Whether to create a ``SearchQueryLog`` entry.  Pass
            ``False`` for system-generated searches (category browse,
            trending products, dynamic filter pages) that should not
            pollute search analytics.  Defaults to ``True``.
        **params: Additional search parameters (limit, offset, filter, sort, etc.).

    Returns:
        Normalised SearchResult instance.
    """
    effective_tenant_id = resolve_tenant_id(tenant_id)
    index = resolve_index(name_or_index, effective_tenant_id)

    # Cache is skipped when a user is provided — per-user searches may differ
    # based on personalisation or entitlements.
    cache_eligible = user is None
    cache = _get_cache() if cache_eligible else None

    if cache is not None:
        cached = cache.get(index.name, query, **params)
        if cached is not None:
            logger.debug(
                "Cache hit for search '%s' in '%s'.",
                query,
                index.name,
            )
            return cached

    backend = get_search_backend()
    raw = backend.search(uid=index.engine_uid, query=query, **params)
    result = SearchResult.from_engine(raw)

    if cache is not None:
        cache.set(index.name, query, result, **params)

    logger.debug(
        "Search '%s' in '%s' returned %d hits.",
        query,
        index.name,
        len(result.hits),
    )

    if log_query:
        _maybe_log_query(
            index=index,
            query=query,
            params=params,
            result=result,
            user=user,
            metadata=metadata or {},
        )

    return result


def _maybe_log_query(
    *,
    index: SearchIndex,
    query: str,
    params: dict[str, Any],
    result: SearchResult,
    user: Any,
    metadata: dict[str, Any],
) -> None:
    """Create a SearchQueryLog and/or aggregate record if logging is configured.

    Dispatches based on ``ICV_SEARCH_LOG_MODE`` (default ``"individual"``):

    - ``"individual"``: write one ``SearchQueryLog`` row per query (with optional
      sample-rate throttling via ``ICV_SEARCH_LOG_SAMPLE_RATE``).
    - ``"aggregate"``: upsert daily ``SearchQueryAggregate`` counters only.
    - ``"both"``: write individual log rows *and* aggregate counters.

    Wrapped in a broad try/except so that any failure here never breaks
    the caller's search result.
    """
    from django.conf import settings as django_settings

    log_queries: bool = getattr(django_settings, "ICV_SEARCH_LOG_QUERIES", False)
    if not log_queries:
        return

    is_zero = len(result.hits) == 0 and result.estimated_total_hits == 0

    log_zero_only: bool = getattr(django_settings, "ICV_SEARCH_LOG_ZERO_RESULTS_ONLY", False)
    if log_zero_only and not is_zero:
        return

    log_mode: str = getattr(django_settings, "ICV_SEARCH_LOG_MODE", "individual")

    try:
        if log_mode in ("individual", "both"):
            sample_rate: float = getattr(django_settings, "ICV_SEARCH_LOG_SAMPLE_RATE", 1.0)
            if sample_rate < 1.0 and random.random() > sample_rate:
                pass  # sampled out — skip individual log
            else:
                from icv_search.models.analytics import SearchQueryLog

                filter_val = params.get("filter", [])
                if isinstance(filter_val, str):
                    filter_val = [filter_val]

                sort_val = params.get("sort", [])
                if isinstance(sort_val, str):
                    sort_val = [sort_val]

                SearchQueryLog.objects.create(
                    index_name=index.name,
                    query=query,
                    filters={"expressions": list(filter_val)} if isinstance(filter_val, list) else filter_val,
                    sort=list(sort_val),
                    hit_count=result.estimated_total_hits,
                    processing_time_ms=result.processing_time_ms,
                    user=user if _is_saved_user(user) else None,
                    tenant_id=index.tenant_id,
                    is_zero_result=is_zero,
                    metadata=metadata,
                )

        if log_mode in ("aggregate", "both"):
            _log_aggregate(index=index, query=query, result=result, is_zero=is_zero)

    except Exception:
        logger.exception("Failed to log search query for index '%s'.", index.name)


def _log_aggregate(*, index: SearchIndex, query: str, result: SearchResult, is_zero: bool) -> None:
    """Upsert a daily SearchQueryAggregate row using a compare-and-insert strategy.

    Uses an update-then-create pattern to avoid the overhead of
    ``get_or_create`` while remaining safe under concurrent writes via an
    ``IntegrityError`` retry.
    """
    from django.db import IntegrityError
    from django.db.models import F
    from django.utils import timezone

    from icv_search.models.aggregates import SearchQueryAggregate

    normalised = query.strip().lower()
    today = timezone.now().date()
    processing_ms = result.processing_time_ms

    updated = SearchQueryAggregate.objects.filter(
        index_name=index.name,
        query=normalised,
        date=today,
        tenant_id=index.tenant_id,
    ).update(
        total_count=F("total_count") + 1,
        zero_result_count=F("zero_result_count") + (1 if is_zero else 0),
        total_processing_time_ms=F("total_processing_time_ms") + processing_ms,
    )
    if updated == 0:
        try:
            SearchQueryAggregate.objects.create(
                index_name=index.name,
                query=normalised,
                date=today,
                tenant_id=index.tenant_id,
                total_count=1,
                zero_result_count=1 if is_zero else 0,
                total_processing_time_ms=processing_ms,
            )
        except IntegrityError:
            SearchQueryAggregate.objects.filter(
                index_name=index.name,
                query=normalised,
                date=today,
                tenant_id=index.tenant_id,
            ).update(
                total_count=F("total_count") + 1,
                zero_result_count=F("zero_result_count") + (1 if is_zero else 0),
                total_processing_time_ms=F("total_processing_time_ms") + processing_ms,
            )


def _is_saved_user(user: Any) -> bool:
    """Return True only when ``user`` is a saved Django model instance.

    Guards against anonymous user objects, unsaved instances, or plain
    non-model values being stored as the FK.
    """
    if user is None:
        return False
    pk = getattr(user, "pk", None)
    return pk is not None


def autocomplete(
    name_or_index: str | SearchIndex,
    query: str,
    tenant_id: str = "",
    *,
    fields: list[str] | None = None,
    limit: int = 5,
    **params: Any,
) -> SearchResult:
    """Execute a lightweight prefix-match query for typeahead use cases.

    Uses the same tenant resolution as :func:`search` but never writes a
    :class:`~icv_search.models.analytics.SearchQueryLog` row, regardless of
    the ``ICV_SEARCH_LOG_QUERIES`` setting (BR-009).

    The ``fields`` parameter maps to the ``attributesToRetrieve`` backend
    parameter.  ``id`` is always included in the retrieved attributes even
    when not listed explicitly (BR-010).

    Results are always cache-eligible (autocomplete has no ``user`` param).

    Args:
        name_or_index: Index name or SearchIndex instance.
        query: Partial query string for prefix matching.
        tenant_id: Tenant identifier (only needed if passing a name).
        fields: Attributes to retrieve.  ``id`` is always included.
        limit: Maximum number of hits to return (default 5).
        **params: Additional parameters forwarded to the backend.

    Returns:
        Normalised SearchResult instance.
    """
    effective_tenant_id = resolve_tenant_id(tenant_id)
    index = resolve_index(name_or_index, effective_tenant_id)

    # Resolve attributes_to_retrieve — always include 'id'.
    if fields is not None:
        attributes_to_retrieve = list(dict.fromkeys(["id", *fields]))
        params["attributes_to_retrieve"] = attributes_to_retrieve

    cache = _get_cache()

    if cache is not None:
        cached = cache.get(index.name, query, limit=limit, **params)
        if cached is not None:
            logger.debug(
                "Cache hit for autocomplete '%s' in '%s'.",
                query,
                index.name,
            )
            return cached

    backend = get_search_backend()
    raw = backend.search(uid=index.engine_uid, query=query, limit=limit, **params)
    result = SearchResult.from_engine(raw)

    if cache is not None:
        cache.set(index.name, query, result, limit=limit, **params)

    logger.debug(
        "Autocomplete '%s' in '%s' returned %d hits.",
        query,
        index.name,
        len(result.hits),
    )

    return result


def get_task(task_uid: str) -> TaskResult:
    """Poll the status of an asynchronous engine task.

    Not all backends support task tracking. Backends that process operations
    synchronously (e.g. PostgreSQL, DummyBackend) return a succeeded status
    immediately. Backends that do not implement task tracking at all raise
    ``NotImplementedError``.

    Args:
        task_uid: The engine-side task identifier returned by a previous
            operation (e.g. from a :class:`~icv_search.types.TaskResult`).

    Returns:
        Normalised :class:`~icv_search.types.TaskResult` for the task.
    """
    backend = get_search_backend()
    raw = backend.get_task(task_uid)
    result = TaskResult.from_engine(raw)
    logger.debug("Task '%s' status: %s.", task_uid, result.status)
    return result


def multi_search(
    queries: list[dict[str, Any]],
    *,
    tenant_id: str = "",
) -> list[SearchResult]:
    """Execute multiple search queries in a single request where supported.

    Each query dict must contain ``index_name`` (logical index name) and
    ``query`` (the search string). An optional ``tenant_id`` key overrides the
    function-level ``tenant_id`` for that specific query. Additional keys
    (``filter``, ``sort``, ``limit``, ``offset``, ``facets``,
    ``highlight_fields``) are forwarded to the backend unchanged.

    The function-level ``tenant_id`` is resolved via
    :func:`~icv_search.services._utils.resolve_tenant_id` so that the
    request-scoped tenant set by
    :class:`~icv_search.middleware.ICVSearchTenantMiddleware` is used
    automatically when no explicit value is supplied.

    Backends that natively support multi-search (e.g. Meilisearch) execute all
    queries in a single round trip. Other backends fall back to sequential
    individual searches.

    Args:
        queries: List of query dicts. Each must have ``index_name`` and
            ``query`` keys.
        tenant_id: Default tenant identifier for index resolution. Overridden
            per-query by a ``tenant_id`` key in the query dict.

    Returns:
        List of normalised :class:`~icv_search.types.SearchResult` instances
        in the same order as the input queries.
    """
    effective_default_tenant_id = resolve_tenant_id(tenant_id)
    backend = get_search_backend()

    # Resolve each query's logical index name to its engine UID.
    resolved: list[dict[str, Any]] = []
    indexes: list[SearchIndex] = []

    for query in queries:
        q_tenant_id = resolve_tenant_id(query.get("tenant_id", effective_default_tenant_id))
        index = resolve_index(query["index_name"], q_tenant_id)
        indexes.append(index)

        engine_query: dict[str, Any] = {
            "uid": index.engine_uid,
            "query": query.get("query", ""),
        }
        # Forward all search params, excluding service-layer keys.
        _SERVICE_KEYS = {"index_name", "query", "tenant_id", "uid"}
        for key, value in query.items():
            if key not in _SERVICE_KEYS:
                engine_query[key] = value

        resolved.append(engine_query)

    raw_results = backend.multi_search(resolved)

    results: list[SearchResult] = []
    for raw, index in zip(raw_results, indexes, strict=False):
        result = SearchResult.from_engine(raw)
        logger.debug(
            "multi_search hit '%s' in '%s': %d hits.",
            raw.get("query", ""),
            index.name,
            len(result.hits),
        )
        results.append(result)

    return results
