"""Optional search result cache layer for icv-search.

Uses Django's cache framework.  Disabled by default; enable via
``ICV_SEARCH_CACHE_ENABLED = True`` in your project settings.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from icv_search.types import SearchResult

logger = logging.getLogger(__name__)


class ICVSearchCache:
    """Optional cache layer for search results.

    Uses Django's cache framework.  Cache keys are derived from the search
    parameters (index, query, filters, sort, limit, offset) so that
    semantically identical queries share a single cache entry.

    Usage::

        cache = ICVSearchCache(timeout=120)
        result = cache.get("products", "shoes", limit=10)
        if result is None:
            result = backend.search(...)
            cache.set("products", "shoes", result, limit=10)

    Cache invalidation::

        cache.invalidate("products")  # clears all entries for the index

    Args:
        timeout: Cache TTL in seconds.  Defaults to ``ICV_SEARCH_CACHE_TIMEOUT``
            (60 s).
        cache_alias: Django cache alias.  Defaults to ``ICV_SEARCH_CACHE_ALIAS``
            ("default").
        prefix: String prepended to every cache key to avoid collisions with
            other cache consumers.
    """

    def __init__(
        self,
        timeout: int | None = None,
        cache_alias: str | None = None,
        prefix: str = "icv_search",
    ) -> None:
        self.prefix = prefix
        self._timeout = timeout
        self._cache_alias = cache_alias

    # ------------------------------------------------------------------
    # Settings helpers — read at call time for pytest override compatibility
    # ------------------------------------------------------------------

    @property
    def timeout(self) -> int:
        if self._timeout is not None:
            return self._timeout
        from django.conf import settings

        return getattr(settings, "ICV_SEARCH_CACHE_TIMEOUT", 60)

    @property
    def cache_alias(self) -> str:
        if self._cache_alias is not None:
            return self._cache_alias
        from django.conf import settings

        return getattr(settings, "ICV_SEARCH_CACHE_ALIAS", "default")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(
        self,
        index_name: str,
        query: str,
        *,
        engine_uid: str = "",
        **params: Any,
    ) -> SearchResult | None:
        """Return a cached :class:`~icv_search.types.SearchResult` or ``None``.

        Args:
            index_name: Logical name of the search index.
            query: The search query string.
            **params: Additional search parameters (limit, offset, filter, sort, etc.).

        Returns:
            A ``SearchResult`` if a matching cache entry exists, otherwise ``None``.
        """
        key = self.make_cache_key(index_name, query, engine_uid=engine_uid, **params)
        raw = self._cache.get(key)
        if raw is None:
            return None
        try:
            return SearchResult.from_engine(raw)
        except Exception:
            logger.exception("Failed to deserialise cached search result for key '%s'.", key)
            return None

    def set(
        self,
        index_name: str,
        query: str,
        result: SearchResult,
        *,
        engine_uid: str = "",
        **params: Any,
    ) -> None:
        """Store a search result in the cache.

        Args:
            index_name: Logical name of the search index.
            query: The search query string.
            result: The ``SearchResult`` to cache.
            **params: Additional search parameters used to derive the cache key.
        """
        key = self.make_cache_key(index_name, query, engine_uid=engine_uid, **params)
        # Store the raw engine dict so it can be deserialised via from_engine()
        self._cache.set(key, result.raw, timeout=self.timeout)
        # Also register this key in the per-index key-set so invalidate() can find it.
        self._register_key(index_name, engine_uid, key)
        logger.debug("Cached search result under key '%s' (ttl=%ds).", key, self.timeout)

    def invalidate(self, index_name: str, *, engine_uid: str = "") -> None:
        """Invalidate all cached search results for the given index.

        Clears every cache key that was registered via :meth:`set` for this
        index, then removes the index's key-set entry.

        Args:
            index_name: Logical name of the search index to invalidate.
        """
        keyset_key = self._keyset_key(index_name, engine_uid)
        known_keys: list[str] = self._cache.get(keyset_key) or []
        if known_keys:
            self._cache.delete_many(known_keys)
        self._cache.delete(keyset_key)
        logger.debug(
            "Invalidated %d cache entries for index '%s'.",
            len(known_keys),
            index_name,
        )

    def make_cache_key(
        self,
        index_name: str,
        query: str,
        *,
        engine_uid: str = "",
        **params: Any,
    ) -> str:
        """Generate a deterministic cache key from the search parameters.

        The key is a SHA-256 digest of the JSON-serialised parameter set, so
        callers do not need to worry about key-length limits imposed by cache
        backends.

        Args:
            index_name: Logical name of the search index.
            query: The search query string.
            **params: Additional search parameters.

        Returns:
            A hex-encoded SHA-256 digest prefixed by ``{self.prefix}:``.
        """
        payload = json.dumps(
            {"index": index_name, "engine_uid": engine_uid, "query": query, **params},
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()
        return f"{self.prefix}:{digest}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _cache(self):
        """Return the Django cache instance for this alias."""
        from django.core.cache import caches

        return caches[self.cache_alias]

    def _keyset_key(self, index_name: str, engine_uid: str) -> str:
        """Return the cache key for the per-index key-set."""
        identity = engine_uid or index_name
        return f"{self.prefix}:keyset:{identity}"

    def _register_key(self, index_name: str, engine_uid: str, key: str) -> None:
        """Add a cache key to the per-index key-set.

        The key-set itself is cached with a generous TTL (10× the result TTL)
        to survive as long as any entry it tracks.
        """
        keyset_key = self._keyset_key(index_name, engine_uid)
        # Use add/get/set to avoid a race — best-effort; correctness is not
        # critical here (a missed key just means incomplete invalidation).
        known: list[str] = self._cache.get(keyset_key) or []
        if key not in known:
            known.append(key)
            self._cache.set(keyset_key, known, timeout=self.timeout * 10)
