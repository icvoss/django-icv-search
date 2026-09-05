"""Rule loading, caching, and match evaluation for merchandising rules."""

from __future__ import annotations

import logging
from typing import Any

from django.db.models import F

logger = logging.getLogger(__name__)


def normalise_query(query: str) -> str:
    """Strip, collapse whitespace, and lowercase a query string."""
    return " ".join(query.split()).lower()


def _cache_key(rule_type: str, index_name: str, tenant_id: str) -> str:
    """Build a cache key for a set of merchandising rules."""
    return f"icv_search:merch:{rule_type}:{index_name}:{tenant_id}"


def _get_cache_timeout() -> int:
    """Return the configured merchandising cache timeout (reads at call time)."""
    from django.conf import settings as django_settings

    return getattr(django_settings, "ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT", 300)


def _get_cache() -> Any:
    """Return the Django cache backend used for merchandising rules.

    Resolved through ``ICV_CACHES_ALIAS`` (ADR-037), never the bare
    default-alias ``cache`` object, so a consumer routing icv packages
    through a non-default cache also gets merchandising rules cached there.
    """
    from django.core.cache import caches

    from icv_search.conf import ICV_CACHES_ALIAS

    return caches[ICV_CACHES_ALIAS]


def load_rules(
    model_class: type,
    index_name: str,
    tenant_id: str = "",
) -> list[Any]:
    """Load active rules from the database, using the Django cache when available.

    Rules are filtered to ``is_active=True`` and ordered by ``-priority, -created_at``
    (the model default ordering). The cache TTL is controlled by
    ``ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT``.
    """
    timeout = _get_cache_timeout()
    rule_type = model_class.__name__

    if timeout > 0:
        cache = _get_cache()
        key = _cache_key(rule_type, index_name, tenant_id)
        cached = cache.get(key)
        if cached is not None:
            return cached

    qs = model_class.objects.filter(
        index_name=index_name,
        is_active=True,
    )
    # Include both tenant-specific and global (empty tenant_id) rules
    qs = qs.filter(tenant_id__in=[tenant_id, ""]) if tenant_id else qs.filter(tenant_id="")

    rules = list(qs)

    if timeout > 0:
        cache = _get_cache()
        key = _cache_key(rule_type, index_name, tenant_id)
        cache.set(key, rules, timeout)

    return rules


def invalidate_rules(rule_type: str, index_name: str, tenant_id: str = "") -> None:
    """Delete cached rules for a given type, index, and tenant.

    Also invalidates the empty-tenant key since global rules may have changed.
    """
    cache = _get_cache()
    keys = [_cache_key(rule_type, index_name, tenant_id)]
    if tenant_id:
        keys.append(_cache_key(rule_type, index_name, ""))
    for key in keys:
        cache.delete(key)


def get_matching_rules(
    model_class: type,
    rule_type: str,
    index_name: str,
    query: str,
    tenant_id: str = "",
    *,
    single_winner: bool = False,
) -> list[Any]:
    """Load rules, filter by schedule and query match, increment hit counts.

    Args:
        model_class: The Django model class for the rule type.
        rule_type: String name used for cache keys (e.g. "QueryRedirect").
        index_name: Logical search index name.
        query: The user's search query (will be normalised).
        tenant_id: Tenant identifier.
        single_winner: If True, return only the highest-priority matching rule.

    Returns:
        List of matching rule instances (or a single-element list if ``single_winner``).
    """
    rules = load_rules(model_class, index_name, tenant_id)
    normalised = normalise_query(query)

    matched: list[Any] = []
    for rule in rules:
        if not rule.is_within_schedule():
            continue
        if not rule.matches_query(normalised):
            continue
        matched.append(rule)
        if single_winner:
            break

    # Increment hit_count for matched rules using F-expression (no race condition)
    if matched:
        pks = [r.pk for r in matched]
        model_class.objects.filter(pk__in=pks).update(hit_count=F("hit_count") + 1)

    return matched
