"""In-memory search backend for testing.

Modelled after Django's locmem email backend. Stores documents in a
module-level dict so tests can verify indexing behaviour without
running a real search engine.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from icv_search.backends.base import BaseSearchBackend
from icv_search.backends.filters import _haversine_distance, apply_filters_to_documents, apply_sort_to_documents
from icv_search.exceptions import IndexNotFoundError, SearchBackendError

# Module-level storage — persists for the process lifetime.
# Call DummyBackend.reset() between tests.
_indexes: dict[str, dict[str, Any]] = {}
_documents: dict[str, dict[str, dict[str, Any]]] = {}
_settings: dict[str, dict[str, Any]] = {}


class DummyBackend(BaseSearchBackend):
    """In-memory search backend for testing."""

    def __init__(self, url: str = "", api_key: str = "", timeout: int = 30, **kwargs: Any) -> None:
        super().__init__(url=url, api_key=api_key, timeout=timeout, **kwargs)

    def create_index(self, uid: str, primary_key: str = "id") -> dict[str, Any]:
        _indexes[uid] = {"uid": uid, "primaryKey": primary_key}
        _documents[uid] = {}
        _settings[uid] = {}
        return {"uid": uid, "primaryKey": primary_key}

    def delete_index(self, uid: str) -> None:
        _indexes.pop(uid, None)
        _documents.pop(uid, None)
        _settings.pop(uid, None)

    def update_settings(self, uid: str, settings: dict[str, Any]) -> dict[str, Any]:
        _settings[uid] = deepcopy(settings)
        return {"taskUid": f"dummy-settings-{uid}"}

    def get_settings(self, uid: str) -> dict[str, Any]:
        return deepcopy(_settings.get(uid, {}))

    def add_documents(self, uid: str, documents: list[dict[str, Any]], primary_key: str = "id") -> dict[str, Any]:
        if uid not in _documents:
            _documents[uid] = {}
        for doc in documents:
            doc_id = str(doc.get(primary_key, ""))
            _documents[uid][doc_id] = deepcopy(doc)
        return {"taskUid": f"dummy-add-{uid}", "indexUid": uid}

    def add_documents_ndjson(
        self,
        uid: str,
        documents: Iterable[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Accept NDJSON-style iterable — stores documents identically to add_documents."""
        if uid not in _documents:
            _documents[uid] = {}
        for doc in documents:
            doc_id = str(doc.get(primary_key, ""))
            _documents[uid][doc_id] = deepcopy(doc)
        return {"taskUid": f"dummy-ndjson-{uid}", "indexUid": uid}

    def delete_documents(self, uid: str, document_ids: list[str]) -> dict[str, Any]:
        if uid in _documents:
            for doc_id in document_ids:
                _documents[uid].pop(str(doc_id), None)
        return {"taskUid": f"dummy-delete-{uid}", "indexUid": uid}

    def clear_documents(self, uid: str) -> dict[str, Any]:
        """Remove all documents from an index."""
        if uid in _documents:
            _documents[uid].clear()
        return {"taskUid": f"dummy-clear-{uid}", "indexUid": uid}

    def search(self, uid: str, query: str, **params: Any) -> dict[str, Any]:
        """Execute an in-memory search with optional geo filtering and sorting.

        Geo parameters:

        - ``geo_point`` (tuple[float, float]): ``(lat, lng)`` origin used for
          radius filtering and/or distance sorting.
        - ``geo_radius`` (int | None): When combined with ``geo_point``,
          documents without a ``_geo`` field or whose ``_geo`` position lies
          outside this radius (in metres) are excluded.
        - ``geo_sort`` (str): ``"asc"`` or ``"desc"``.  When combined with
          ``geo_point``, results are ordered by Haversine distance from the
          point.  Each hit gains a ``_geoDistance`` key (metres) matching
          the behaviour of the Meilisearch backend.
        """
        docs = list(_documents.get(uid, {}).values())

        # Extract geo params.
        geo_point: tuple[float, float] | None = params.get("geo_point")
        geo_radius: int | None = params.get("geo_radius")
        geo_sort: str | None = params.get("geo_sort")

        # Apply text matching and compute simple relevance scores.
        pattern: re.Pattern[str] | None = None
        scores: list[float | None] = []

        if query:
            terms = query.lower().split()
            pattern = re.compile(re.escape(query), re.IGNORECASE)
            matched = []
            for doc in docs:
                for value in doc.values():
                    if isinstance(value, str) and pattern.search(value):
                        matched.append(doc)
                        break
            docs = matched

            # Simple term-frequency score: fraction of query terms found in
            # the concatenated string values of the document, capped at 1.0.
            for doc in docs:
                all_text = " ".join(str(v) for v in doc.values() if isinstance(v, str)).lower()
                total_words = max(len(all_text.split()), 1)
                term_hits = sum(all_text.count(term) for term in terms)
                scores.append(round(min(term_hits / total_words, 1.0), 4))

        # Apply standard field filters.
        filters = params.get("filter")
        if filters:
            docs = apply_filters_to_documents(docs, filters)

        # Apply geo radius filter — exclude documents without a valid ``_geo``
        # field or those further than ``geo_radius`` metres from ``geo_point``.
        if geo_point is not None and geo_radius is not None:
            origin_lat, origin_lng = geo_point
            geo_filtered = []
            for doc in docs:
                geo = doc.get("_geo")
                if not isinstance(geo, dict):
                    continue
                try:
                    doc_lat = float(geo["lat"])
                    doc_lng = float(geo["lng"])
                except (KeyError, TypeError, ValueError):
                    continue
                dist = _haversine_distance(origin_lat, origin_lng, doc_lat, doc_lng)
                if dist <= geo_radius:
                    geo_filtered.append(doc)
            docs = geo_filtered

        # Annotate each document with its distance from geo_point so that
        # callers can use the value regardless of whether geo_sort is set.
        # This mirrors the ``_geoDistance`` field that Meilisearch adds
        # automatically when geo is used.
        if geo_point is not None:
            origin_lat, origin_lng = geo_point
            annotated: list[dict[str, Any]] = []
            for doc in docs:
                doc = dict(doc)  # shallow copy — do not mutate stored docs
                geo = doc.get("_geo")
                if isinstance(geo, dict):
                    try:
                        doc_lat = float(geo["lat"])
                        doc_lng = float(geo["lng"])
                        doc["_geoDistance"] = round(_haversine_distance(origin_lat, origin_lng, doc_lat, doc_lng))
                    except (KeyError, TypeError, ValueError):
                        pass
                annotated.append(doc)
            docs = annotated

        # Apply geo distance sort before regular sort so that regular sort
        # fields can break ties if desired.
        if geo_point is not None and geo_sort in ("asc", "desc"):
            reverse = geo_sort == "desc"
            docs.sort(
                key=lambda d: d.get("_geoDistance", float("inf")),
                reverse=reverse,
            )
        else:
            # Apply regular sort.
            sort = params.get("sort")
            if sort:
                docs = apply_sort_to_documents(docs, sort)

        # Compute facet distribution before pagination
        facet_distribution: dict[str, dict[str, int]] = {}
        facets: list[str] | None = params.get("facets")
        if facets:
            for facet_field in facets:
                counts: dict[str, int] = {}
                for doc in docs:
                    value = doc.get(facet_field)
                    if value is None:
                        continue
                    key = str(value)
                    counts[key] = counts.get(key, 0) + 1
                facet_distribution[facet_field] = counts

        # Apply limit and offset
        limit = params.get("limit", 20)
        offset = params.get("offset", 0)
        results = docs[offset : offset + limit]
        page_scores = scores[offset : offset + limit] if scores else []

        # Build highlighted versions when requested
        highlight_fields: list[str] = params.get("highlight_fields") or []
        pre_tag: str = params.get("highlight_pre_tag", "<mark>")
        post_tag: str = params.get("highlight_post_tag", "</mark>")

        formatted_hits: list[dict[str, Any]] = []
        if highlight_fields and pattern is not None:
            for doc in results:
                formatted: dict[str, Any] = dict(doc)
                for field_name in highlight_fields:
                    field_value = doc.get(field_name)
                    if isinstance(field_value, str):
                        formatted[field_name] = pattern.sub(
                            lambda m: f"{pre_tag}{m.group()}{post_tag}",
                            field_value,
                        )
                formatted_hits.append(formatted)

        # Filter returned fields when attributesToRetrieve is specified.
        # Accept both camelCase (Meilisearch native) and snake_case (SearchQuery builder).
        # The primary key ("id") is always included regardless of the list.
        attributes_to_retrieve: list[str] | None = params.get(
            "attributes_to_retrieve", params.get("attributesToRetrieve")
        )
        if attributes_to_retrieve is not None:
            allowed = set(attributes_to_retrieve) | {"id"}
            results = [{k: v for k, v in doc.items() if k in allowed} for doc in results]
            if formatted_hits:
                formatted_hits = [{k: v for k, v in doc.items() if k in allowed} for doc in formatted_hits]

        response: dict[str, Any] = {
            "hits": deepcopy(results),
            "query": query,
            "processingTimeMs": 0,
            "estimatedTotalHits": len(docs),
        }
        if page_scores:
            response["ranking_scores"] = page_scores
        if formatted_hits:
            response["formatted_hits"] = formatted_hits
        if facets:
            response["facetDistribution"] = facet_distribution
        return response

    def get_stats(self, uid: str) -> dict[str, Any]:
        return {
            "numberOfDocuments": len(_documents.get(uid, {})),
            "isIndexing": False,
        }

    def health(self) -> bool:
        return True

    def get_task(self, task_uid: str) -> dict[str, Any]:
        return {"uid": task_uid, "status": "succeeded"}

    def swap_indexes(self, pairs: list[tuple[str, str]]) -> dict[str, Any]:
        """Swap indexes in memory."""
        for a, b in pairs:
            _indexes[a], _indexes[b] = _indexes.get(b, {}), _indexes.get(a, {})
            _documents[a], _documents[b] = _documents.get(b, {}), _documents.get(a, {})
            _settings[a], _settings[b] = _settings.get(b, {}), _settings.get(a, {})
        return {"taskUid": "dummy-swap", "status": "succeeded"}

    def get_document(self, uid: str, document_id: str) -> dict[str, Any]:
        """Return a single document from the in-memory store by its primary key.

        Raises:
            IndexNotFoundError: if ``uid`` is not a known index.
            SearchBackendError: if the document is not found in the index.
        """
        if uid not in _indexes:
            raise IndexNotFoundError(f"Index '{uid}' not found.")
        doc = _documents.get(uid, {}).get(str(document_id))
        if doc is None:
            raise SearchBackendError(f"Document '{document_id}' not found in index '{uid}'.")
        return deepcopy(doc)

    def get_documents(
        self,
        uid: str,
        document_ids: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return multiple documents from the in-memory store.

        When ``document_ids`` is provided, returns those specific documents
        (order matches ``document_ids``).  When ``None``, browses all documents
        with ``limit``/``offset`` pagination.

        ``fields`` restricts the returned keys; ``id`` is always included.

        Raises:
            IndexNotFoundError: if ``uid`` is not a known index.
        """
        if uid not in _indexes:
            raise IndexNotFoundError(f"Index '{uid}' not found.")

        store = _documents.get(uid, {})

        if document_ids is not None:
            docs = [deepcopy(store[str(doc_id)]) for doc_id in document_ids if str(doc_id) in store]
        else:
            all_docs = list(store.values())
            docs = [deepcopy(d) for d in all_docs[offset : offset + limit]]

        if fields is not None:
            allowed = set(fields) | {"id"}
            docs = [{k: v for k, v in doc.items() if k in allowed} for doc in docs]

        return docs

    def facet_search(self, uid: str, facet_name: str, facet_query: str = "", **params: Any) -> list[dict[str, Any]]:
        """Return facet value counts for ``facet_name``, optionally filtered by ``facet_query``.

        Iterates all documents in the index, collects values for the given field,
        applies case-insensitive substring filtering when ``facet_query`` is
        non-empty, and returns counts sorted by frequency descending.

        Raises:
            IndexNotFoundError: if ``uid`` is not a known index.
        """
        if uid not in _indexes:
            raise IndexNotFoundError(f"Index '{uid}' not found.")

        counts: dict[str, int] = {}
        for doc in _documents.get(uid, {}).values():
            value = doc.get(facet_name)
            if value is None:
                continue
            str_value = str(value)
            if facet_query and facet_query.lower() not in str_value.lower():
                continue
            counts[str_value] = counts.get(str_value, 0) + 1

        return sorted(
            [{"value": v, "count": c} for v, c in counts.items()],
            key=lambda item: item["count"],
            reverse=True,
        )

    def similar_documents(self, uid: str, document_id: str, **params: Any) -> dict[str, Any]:
        """Return all documents except the source document.

        The DummyBackend cannot perform real similarity ranking; returning every
        other document is sufficient to verify that the service layer wires the
        call correctly.

        Raises:
            IndexNotFoundError: if ``uid`` is not a known index.
        """
        if uid not in _indexes:
            raise IndexNotFoundError(f"Index '{uid}' not found.")

        results = [deepcopy(doc) for key, doc in _documents.get(uid, {}).items() if key != str(document_id)]
        return {
            "hits": results,
            "estimatedTotalHits": len(results),
            "processingTimeMs": 0,
        }

    def compact(self, uid: str) -> dict[str, Any]:
        """No-op for the in-memory backend — returns an empty dict."""
        return {}

    def update_documents(
        self,
        uid: str,
        documents: list[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Partially update documents in the in-memory store.

        For each document in ``documents``, existing fields are merged with the
        provided fields (fields absent from the update dict are preserved).
        Documents whose primary key does not exist in the index are appended.

        Raises:
            IndexNotFoundError: if ``uid`` is not a known index.
        """
        if uid not in _indexes:
            raise IndexNotFoundError(f"Index '{uid}' not found.")

        store = _documents.setdefault(uid, {})
        for doc in documents:
            doc_id = str(doc.get(primary_key, ""))
            if doc_id in store:
                store[doc_id] = {**store[doc_id], **deepcopy(doc)}
            else:
                store[doc_id] = deepcopy(doc)

        return {"taskUid": "dummy-update-0", "status": "succeeded"}

    @classmethod
    def reset(cls) -> None:
        """Clear all in-memory data. Call in test teardown."""
        _indexes.clear()
        _documents.clear()
        _settings.clear()
