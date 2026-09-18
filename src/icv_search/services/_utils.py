"""Shared utilities for icv-search services."""

from __future__ import annotations

import logging
from typing import Any

from icv_search.models import SearchIndex

logger = logging.getLogger(__name__)


def _get_model_class_for_index(index_name: str) -> type | None:
    """Look up the model class for an index name from ICV_SEARCH_AUTO_INDEX config.

    Returns ``None`` if the index is not configured or the model cannot be resolved.
    """
    from django.apps import apps
    from django.conf import settings as django_settings

    auto_config: dict[str, dict[str, Any]] = getattr(django_settings, "ICV_SEARCH_AUTO_INDEX", {})
    index_config = auto_config.get(index_name)
    if not index_config:
        return None

    model_path = index_config.get("model", "")
    if not model_path:
        return None

    try:
        app_label, model_name = model_path.rsplit(".", 1)
        return apps.get_model(app_label, model_name)
    except (LookupError, ValueError):
        logger.warning(
            "Could not resolve model path '%s' for index '%s'.",
            model_path,
            index_name,
            exc_info=True,
        )
        return None


def resolve_index(name_or_index: str | SearchIndex, tenant_id: str = "") -> SearchIndex:
    """Resolve an index name or SearchIndex instance to a SearchIndex.

    If no ``SearchIndex`` record exists for the given name, one is
    automatically created (and provisioned in the search engine) using
    model-class metadata from ``ICV_SEARCH_AUTO_INDEX`` when available.
    This prevents cryptic ``DoesNotExist`` errors when calling
    ``search()`` or ``index_documents()`` before manually creating the
    index record.

    Args:
        name_or_index: Index name string or SearchIndex instance.
        tenant_id: Tenant identifier (only needed when passing a name).

    Returns:
        The resolved SearchIndex instance.
    """
    if isinstance(name_or_index, SearchIndex):
        return name_or_index

    try:
        return SearchIndex.objects.get(name=name_or_index, tenant_id=tenant_id)
    except SearchIndex.DoesNotExist:
        pass

    # Auto-create the SearchIndex record.
    model_class = _get_model_class_for_index(name_or_index)

    logger.info(
        "SearchIndex '%s' not found — auto-creating.",
        name_or_index,
    )

    from icv_search.services.indexing import create_index

    return create_index(
        name=name_or_index,
        tenant_id=tenant_id,
        model_class=model_class,
    )


def resolve_tenant_id(explicit_tenant_id: str) -> str:
    """Return the effective tenant identifier for a service call.

    Resolution order:

    1. ``explicit_tenant_id`` — if non-empty, returned as-is (explicit wins).
    2. The request-scoped tenant set by
       :class:`~icv_search.middleware.ICVSearchTenantMiddleware` via
       :func:`~icv_search.middleware.get_current_tenant_id`.
    3. An empty string (single-tenant / no-tenant mode).

    Args:
        explicit_tenant_id: The tenant identifier supplied directly by the caller.

    Returns:
        The resolved tenant identifier string (may be empty).
    """
    if explicit_tenant_id:
        return explicit_tenant_id
    from icv_search.middleware import get_current_tenant_id

    return get_current_tenant_id()
