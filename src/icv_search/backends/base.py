"""Abstract base class for search engine backends."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)


class BaseSearchBackend(ABC):
    """Abstract interface for search engine backends.

    Modelled after Django's email backend pattern. Consuming projects
    swap backends via the ICV_SEARCH_BACKEND setting.

    All methods raise SearchBackendError on failure.
    """

    def __init__(self, url: str, api_key: str, timeout: int = 30, **kwargs: Any) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout = timeout

    # Known Meilisearch-specific params that other backends may not support.
    _MEILI_SPECIFIC: frozenset[str] = frozenset(
        {
            "crop_fields",
            "crop_length",
            "crop_marker",
            "show_ranking_score_details",
            "show_matches_position",
            "ranking_score_threshold",
            "distinct",
            "hybrid",
            "vector",
            "retrieve_vectors",
            "page",
            "hits_per_page",
            "locales",
            "geo_bbox",
            "geo_polygon",
            "attributes_to_search_on",
        }
    )

    def _warn_unsupported_params(self, params: dict[str, Any], supported: set[str]) -> None:
        """Log a warning for any Meilisearch-specific params that this backend ignores.

        Args:
            params: The full search params dict as received by ``search()``.
            supported: Set of Meilisearch-specific param names that this
                particular backend does in fact support, and should therefore
                not be warned about.
        """
        unsupported = set(params.keys()) & (self._MEILI_SPECIFIC - supported)
        if unsupported:
            logger.debug(
                "%s does not support search params: %s — they will be ignored.",
                self.__class__.__name__,
                ", ".join(sorted(unsupported)),
            )

    @abstractmethod
    def create_index(self, uid: str, primary_key: str = "id") -> dict[str, Any]:
        """Provision a new index in the engine."""

    @abstractmethod
    def delete_index(self, uid: str) -> None:
        """Remove an index and all its documents."""

    @abstractmethod
    def update_settings(self, uid: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Push index settings (searchable, filterable, sortable attrs, etc.)."""

    @abstractmethod
    def get_settings(self, uid: str) -> dict[str, Any]:
        """Retrieve current settings from the engine."""

    @abstractmethod
    def add_documents(self, uid: str, documents: list[dict[str, Any]], primary_key: str = "id") -> dict[str, Any]:
        """Add or update documents. Returns engine task info."""

    @abstractmethod
    def delete_documents(self, uid: str, document_ids: list[str]) -> dict[str, Any]:
        """Remove documents by ID. Returns engine task info."""

    # Upper bound on documents the fallback delete-by-filter will collect in a
    # single pass. Engines with native filter-deletion (e.g. Meilisearch)
    # override delete_documents_by_filter and ignore this.
    _FILTER_DELETE_SCAN_LIMIT = 1000

    def delete_documents_by_filter(self, uid: str, filter_expr: Any) -> dict[str, Any]:
        """Remove documents matching a filter expression.

        The default implementation composes the two primitives every backend
        already implements: it searches the index with ``filter_expr`` to
        collect the matching primary keys, then calls :meth:`delete_documents`.
        This keeps the method substitutable across all backends rather than
        raising ``NotImplementedError`` on engines without a native
        filter-delete endpoint.

        Backends with a native single-request filter delete (e.g. Meilisearch)
        should override this for efficiency and to avoid the scan limit.

        Args:
            uid: Index UID.
            filter_expr: Filter in the form this backend's :meth:`search`
                accepts (a Django-native dict, or an engine-native string).

        Returns:
            Engine task info dict.
        """
        ids: list[str] = []
        offset = 0
        while True:
            response = self.search(
                uid,
                "",
                filter=filter_expr,
                limit=self._FILTER_DELETE_SCAN_LIMIT,
                offset=offset,
            )
            hits = response.get("hits", [])
            if not hits:
                break
            for hit in hits:
                doc_id = hit.get("id")
                if doc_id is not None:
                    ids.append(str(doc_id))
            if len(hits) < self._FILTER_DELETE_SCAN_LIMIT:
                break
            offset += self._FILTER_DELETE_SCAN_LIMIT

        if not ids:
            return {"deleted": 0, "document_ids": []}
        return self.delete_documents(uid, ids)

    @abstractmethod
    def clear_documents(self, uid: str) -> dict[str, Any]:
        """Remove ALL documents from an index without deleting the index itself."""

    @abstractmethod
    def search(self, uid: str, query: str, **params: Any) -> dict[str, Any]:
        """Execute a search query. Returns engine response."""

    @abstractmethod
    def get_stats(self, uid: str) -> dict[str, Any]:
        """Get index stats (document count, size, etc.)."""

    @abstractmethod
    def health(self) -> bool:
        """Check engine connectivity. Returns True if healthy."""

    def get_task(self, task_uid: str) -> dict[str, Any]:
        """Check status of an async engine task. Optional — not all engines support this."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support task tracking.")

    def multi_search(self, queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Execute multiple search queries, returning one result dict per query.

        Each query dict must contain ``uid`` (the engine index UID) and ``query``
        (the search string). All other keys are forwarded to :meth:`search`
        as keyword arguments.

        The default implementation calls :meth:`search` in a loop. Backends
        that natively support multi-search (e.g. Meilisearch) should override
        this method.

        Returns:
            A list of raw engine response dicts in the same order as ``queries``.
        """
        results: list[dict[str, Any]] = []
        for query in queries:
            uid = query["uid"]
            q = query.get("query", "")
            params = {k: v for k, v in query.items() if k not in ("uid", "query")}
            results.append(self.search(uid=uid, query=q, **params))
        return results

    def add_documents_ndjson(
        self,
        uid: str,
        documents: Iterable[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Add or update documents using NDJSON (newline-delimited JSON).

        Backends that support NDJSON ingestion (e.g. Meilisearch 1.x+) should
        override this for better streaming performance.  The default falls back
        to :meth:`add_documents` after materialising the iterable.

        Args:
            uid: Index UID.
            documents: Iterable of document dicts.
            primary_key: Document field used as primary key.

        Returns:
            Engine task info dict.
        """
        return self.add_documents(uid, list(documents), primary_key=primary_key)

    def swap_indexes(self, pairs: list[tuple[str, str]]) -> dict[str, Any]:
        """Atomically swap index names. Not all engines support this.

        Args:
            pairs: List of (index_a, index_b) tuples to swap.

        Returns:
            Engine task info.
        """
        raise NotImplementedError(f"{self.__class__.__name__} does not support index swaps.")

    def get_document(self, uid: str, document_id: str) -> dict[str, Any]:
        """Fetch a single document by its primary key.

        Returns the document as a dict. The ``id`` key is always present
        in the returned dict (BR-012).
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support get_document(). Override this method to fetch documents by ID."
        )

    def get_documents(
        self,
        uid: str,
        document_ids: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch multiple documents, optionally by IDs.

        When ``document_ids`` is ``None``, returns up to ``limit`` documents
        starting from ``offset`` (browse mode).  When ``document_ids`` is
        provided, fetches those specific documents.

        ``fields`` filters the returned keys; ``id`` is always included
        (BR-010).

        The default implementation calls :meth:`get_document` in a loop.
        Backends with native multi-get support should override for efficiency.
        """
        if document_ids is not None:
            results = []
            for doc_id in document_ids:
                try:
                    results.append(self.get_document(uid, doc_id))
                except Exception:
                    pass  # Skip documents that cannot be fetched
            if fields is not None:
                keep = set(fields) | {"id"}
                results = [{k: v for k, v in doc.items() if k in keep} for doc in results]
            return results
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support browsing documents. "
            "Override get_documents() to support browse mode."
        )

    def facet_search(self, uid: str, facet_name: str, facet_query: str = "", **params: Any) -> list[dict[str, Any]]:
        """Search within facet values for typeahead in filter UIs.

        Returns a list of ``{"value": str, "count": int}`` dicts sorted by
        count descending (BR-014).  The facet field must be in
        ``filterableAttributes``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support facet_search(). "
            "Override this method to enable facet value typeahead."
        )

    def similar_documents(self, uid: str, document_id: str, **params: Any) -> dict[str, Any]:
        """Find documents similar to the given document.

        Uses the engine's native similarity/MLT feature.  Returns a search
        result dict in the same format as :meth:`search`.

        Backends that do not support similarity search raise
        ``NotImplementedError`` with a message naming the feature and
        engine (BR-013).
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support similar_documents(). "
            "This engine does not have a native similarity/more-like-this feature."
        )

    def compact(self, uid: str) -> dict[str, Any]:
        """Reclaim storage space and optimise the index.

        No-op on engines that manage compaction automatically (BR-016).
        Must never raise an error.
        """
        return {}

    def update_documents(
        self,
        uid: str,
        documents: list[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Partial/atomic update of document fields.

        Fields not included in the update dicts are preserved when the
        engine supports partial updates (BR-015).  The default falls back
        to :meth:`add_documents` (full replacement).
        """
        return self.add_documents(uid, documents, primary_key=primary_key)
