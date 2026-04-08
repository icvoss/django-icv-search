"""Typesense backend using the typesense Python SDK."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

try:
    import typesense
    import typesense.exceptions

    _HAS_TYPESENSE = True
except ImportError:  # pragma: no cover
    _HAS_TYPESENSE = False

from icv_search.backends.base import BaseSearchBackend
from icv_search.exceptions import IndexNotFoundError, SearchBackendError, SearchTimeoutError

logger = logging.getLogger(__name__)

# Django-style range lookup suffixes.
_RANGE_OPERATORS: dict[str, str] = {
    "__gte": ">=",
    "__gt": ">",
    "__lte": "<=",
    "__lt": "<",
}

# Default Typesense field type mapping for common Python types.
_DEFAULT_FIELD_TYPES: dict[str, str] = {
    "str": "string",
    "int": "int64",
    "float": "float",
    "bool": "bool",
    "list": "string[]",
}


def translate_filter_to_typesense(filters: dict[str, Any] | str) -> str:
    """Convert a Django-native filter dict to a Typesense filter_by string.

    Passes through a pre-built string unchanged.

    Supported lookups:
    - ``{"field": value}`` → ``field:=value``
    - ``{"field": [v1, v2]}`` → ``field:=[v1,v2]``
    - ``{"field__gte": v}`` → ``field:>=v``
    - ``{"field__gt": v}`` → ``field:>v``
    - ``{"field__lte": v}`` → ``field:<=v``
    - ``{"field__lt": v}`` → ``field:<v``
    - ``{"field": True}`` → ``field:=true``
    - ``{"field": False}`` → ``field:=false``

    Multiple conditions are joined with `` && ``.

    Args:
        filters: A dict of field/lookup conditions, or a pre-built Typesense
            filter_by string.

    Returns:
        A Typesense ``filter_by`` string, or an empty string for empty input.
    """
    if isinstance(filters, str):
        return filters

    if not isinstance(filters, dict) or not filters:
        return ""

    clauses: list[str] = []

    for field, value in filters.items():
        # Check for range lookup suffixes.
        range_op: str | None = None
        real_field = field
        for suffix, op in _RANGE_OPERATORS.items():
            if field.endswith(suffix):
                real_field = field[: -len(suffix)]
                range_op = op
                break

        if range_op is not None:
            clauses.append(f"{real_field}:{range_op}{value}")

        elif isinstance(value, list):
            joined = ",".join(_format_value(v) for v in value)
            clauses.append(f"{real_field}:=[{joined}]")

        elif isinstance(value, bool):
            # Must check bool before int — bool is a subclass of int.
            clauses.append(f"{real_field}:={str(value).lower()}")

        else:
            clauses.append(f"{real_field}:={_format_value(value)}")

    return " && ".join(clauses)


def _format_value(value: Any) -> str:
    """Format a scalar value for a Typesense filter expression.

    Always backtick-wraps strings to prevent operator injection.
    """
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    # Always backtick-wrap strings to prevent operator injection.
    escaped = str(value).replace("`", "\\`")
    return f"`{escaped}`"


def translate_sort_to_typesense(sort: list[str] | str) -> str:
    """Convert a Django-native sort list to a Typesense sort_by string.

    Fields already in Typesense ``field:dir`` format pass through unchanged.

    Args:
        sort: Either a list of field names (with optional ``-`` prefix for
            descending) or a single field name string.

    Returns:
        A Typesense ``sort_by`` string (comma-separated), or an empty string.
    """
    if isinstance(sort, str):
        sort = [sort] if sort else []

    if not isinstance(sort, list) or not sort:
        return ""

    parts: list[str] = []
    for field in sort:
        if ":" in field:
            # Already in Typesense format (e.g. "price:desc").
            parts.append(field)
        elif field.startswith("-"):
            parts.append(f"{field[1:]}:desc")
        else:
            parts.append(f"{field}:asc")

    return ",".join(parts)


def _build_schema_fields(
    settings: dict[str, Any],
    field_types: dict[str, str],
) -> list[dict[str, Any]]:
    """Build a Typesense collection schema fields list from icv-search settings.

    Args:
        settings: icv-search canonical settings dict with keys
            ``searchableAttributes``, ``filterableAttributes``,
            ``sortableAttributes``.
        field_types: Mapping of field name → Typesense type string.
            Falls back to ``"string"`` for unmapped fields.

    Returns:
        A list of Typesense field dicts suitable for ``collections.create()``.
    """
    searchable: set[str] = set(settings.get("searchableAttributes", []))
    filterable: set[str] = set(settings.get("filterableAttributes", []))
    sortable: set[str] = set(settings.get("sortableAttributes", []))

    all_fields = searchable | filterable | sortable
    fields: list[dict[str, Any]] = []

    for field_name in sorted(all_fields):
        field_type = field_types.get(field_name, "string")
        field_def: dict[str, Any] = {
            "name": field_name,
            "type": field_type,
        }

        if field_name in searchable:
            field_def["index"] = True

        if field_name in filterable:
            field_def["facet"] = True

        if field_name in sortable:
            field_def["sort"] = True

        # Fields that are only sortable (not searchable or filterable)
        # should not be indexed for full-text.
        if field_name not in searchable:
            field_def.setdefault("index", False)

        fields.append(field_def)

    return fields


class TypesenseBackend(BaseSearchBackend):
    """Search backend for Typesense using the typesense Python SDK.

    Supports self-managed Typesense servers and Typesense Cloud.

    Install the optional extra before use::

        pip install django-icv-search[typesense]

    Configure via settings::

        ICV_SEARCH_BACKEND = "icv_search.backends.typesense.TypesenseBackend"
        ICV_SEARCH_URL     = "http://localhost:8108"
        ICV_SEARCH_API_KEY = "your-api-key"

    For HA clusters, pass ``nodes`` as a list of node dicts via
    ``ICV_SEARCH_BACKEND_OPTIONS``::

        ICV_SEARCH_BACKEND_OPTIONS = {
            "nodes": [
                {"host": "node1.example.com", "port": 443, "protocol": "https"},
                {"host": "node2.example.com", "port": 443, "protocol": "https"},
            ]
        }
    """

    def __init__(self, url: str, api_key: str, timeout: int = 30, **kwargs: Any) -> None:
        if not _HAS_TYPESENSE:
            from django.core.exceptions import ImproperlyConfigured

            raise ImproperlyConfigured(
                "typesense is required to use TypesenseBackend. "
                "Install it with: pip install django-icv-search[typesense]"
            )

        super().__init__(url=url, api_key=api_key, timeout=timeout, **kwargs)

        # Allow caller to supply a pre-built nodes list for HA clusters.
        nodes: list[dict[str, Any]] | None = kwargs.get("nodes")

        if nodes is None:
            parsed = urlparse(url)
            host = parsed.hostname or "localhost"
            port = parsed.port or (443 if parsed.scheme == "https" else 8108)
            protocol = parsed.scheme or "http"
            nodes = [{"host": host, "port": str(port), "protocol": protocol}]

        connection_timeout_seconds: int = kwargs.get("connection_timeout", timeout)

        self._client = typesense.Client(
            {
                "nodes": nodes,
                "api_key": api_key,
                "connection_timeout_seconds": connection_timeout_seconds,
            }
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Wrap a typesense SDK call with icv-search exception mapping."""
        try:
            return fn(*args, **kwargs)
        except typesense.exceptions.RequestUnauthorized as exc:
            raise SearchBackendError(f"Typesense unauthorised: {exc}") from exc
        except typesense.exceptions.ObjectNotFound as exc:
            raise IndexNotFoundError(str(exc)) from exc
        except typesense.exceptions.ServiceUnavailable as exc:
            raise SearchTimeoutError(f"Typesense service unavailable: {exc}") from exc
        except typesense.exceptions.TypesenseClientError as exc:
            raise SearchBackendError(f"Typesense error: {exc}") from exc
        except Exception as exc:
            # Catch connection-level errors (e.g. requests.exceptions.ConnectionError)
            # that may escape the SDK wrapper.
            raise SearchBackendError(f"Typesense request failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Abstract methods
    # ------------------------------------------------------------------

    def create_index(self, uid: str, primary_key: str = "id") -> dict[str, Any]:
        """Create a Typesense collection.

        The minimal schema has only the primary key field. Call
        ``update_settings()`` to add fields after creation.
        """
        schema: dict[str, Any] = {
            "name": uid,
            "fields": [{"name": primary_key, "type": "string"}],
            "default_sorting_field": "",
        }
        return self._call(self._client.collections.create, schema)

    def delete_index(self, uid: str) -> None:
        """Delete a Typesense collection and all its documents."""
        self._call(self._client.collections[uid].delete)

    def update_settings(self, uid: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Translate icv-search canonical settings and push to Typesense.

        Typesense does not support live schema alteration of existing fields.
        This method rebuilds the collection schema by dropping and recreating
        the collection, then re-indexing any retained documents.

        For index-time settings (synonyms, stop words), the appropriate
        Typesense overrides/synonyms APIs are used directly.

        Note: Because Typesense field additions require a collection update
        via ``PATCH /collections/{name}``, this method uses the update endpoint
        when the collection already exists rather than a full drop/recreate.
        """
        from icv_search.conf import ICV_SEARCH_TYPESENSE_FIELD_TYPES

        field_types: dict[str, str] = ICV_SEARCH_TYPESENSE_FIELD_TYPES
        fields = _build_schema_fields(settings, field_types)

        if not fields:
            return {}

        update_body: dict[str, Any] = {"fields": fields}
        return self._call(self._client.collections[uid].update, update_body)

    def get_settings(self, uid: str) -> dict[str, Any]:
        """Retrieve the collection schema and translate to icv-search format."""
        response = self._call(self._client.collections[uid].retrieve)
        fields: list[dict[str, Any]] = response.get("fields", [])

        searchable: list[str] = []
        filterable: list[str] = []
        sortable: list[str] = []

        for field in fields:
            name = field.get("name", "")
            if not name or name == ".*":
                continue
            if field.get("index", True):
                searchable.append(name)
            if field.get("facet", False):
                filterable.append(name)
            if field.get("sort", False):
                sortable.append(name)

        return {
            "searchableAttributes": searchable,
            "filterableAttributes": filterable,
            "sortableAttributes": sortable,
        }

    def add_documents(
        self,
        uid: str,
        documents: list[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Add or update documents using Typesense's import endpoint (upsert).

        Uses ``action=upsert`` so existing documents are updated in place.
        """
        results = self._call(
            self._client.collections[uid].documents.import_,
            documents,
            {"action": "upsert"},
        )
        succeeded = sum(1 for r in results if r.get("success", False))
        failed = len(results) - succeeded
        return {"succeeded": succeeded, "failed": failed, "results": results}

    def delete_documents(self, uid: str, document_ids: list[str]) -> dict[str, Any]:
        """Remove documents by ID.

        Typesense does not have a native batch-delete-by-IDs endpoint, so this
        method iterates and deletes individually. For bulk deletion by filter,
        use the Typesense delete-by-query endpoint directly.
        """
        succeeded = 0
        errors: list[str] = []

        for doc_id in document_ids:
            try:
                self._call(self._client.collections[uid].documents[doc_id].delete)
                succeeded += 1
            except (IndexNotFoundError, SearchBackendError) as exc:
                errors.append(str(exc))

        return {"succeeded": succeeded, "failed": len(errors), "errors": errors}

    def clear_documents(self, uid: str) -> dict[str, Any]:
        """Remove all documents from a collection without deleting the collection.

        Uses the Typesense ``DELETE /collections/{uid}/documents`` endpoint
        with a ``filter_by`` that matches all documents.
        """
        # Typesense requires a filter_by to bulk-delete; matching on id != "" is
        # a reliable way to match all documents regardless of schema.
        return self._call(
            self._client.collections[uid].documents.delete,
            {"filter_by": "id:!=__nonexistent__", "batch_size": 100},
        )

    def search(self, uid: str, query: str, **params: Any) -> dict[str, Any]:
        """Execute a search query against a Typesense collection.

        Recognised params:

        - ``filter`` (dict | str): Django-native filter dict or Typesense
          filter_by string.
        - ``sort`` (list[str] | str): Sort fields with optional ``-`` prefix,
          or a Typesense sort_by string.
        - ``limit`` (int): Maximum hits to return. Default 20.
        - ``offset`` (int): Number of hits to skip. Default 0.
        - ``facets`` (list[str]): Fields to aggregate for facet counts.
        - ``highlight_fields`` (list[str]): Fields to highlight.
        - ``highlight_pre_tag`` (str): Opening highlight tag. Default ``<mark>``.
        - ``highlight_post_tag`` (str): Closing highlight tag. Default ``</mark>``.
        - ``attributesToRetrieve`` (list[str]): Fields to include.
        - ``show_ranking_score`` (bool): Include ``_score`` in hits.
        - ``geo_point`` (tuple[float, float]): ``(lat, lng)`` origin.
        - ``geo_radius`` (int): Radius in metres for geo filter.
        - ``geo_sort`` (str): ``"asc"`` or ``"desc"`` for geo sort.
        """
        from icv_search.conf import ICV_SEARCH_TYPESENSE_GEO_FIELD

        limit: int = params.pop("limit", 20)
        offset: int = params.pop("offset", 0)

        # Filter translation.
        filter_param = params.pop("filter", None)
        filter_by = ""
        if filter_param is not None:
            filter_by = translate_filter_to_typesense(filter_param)

        # Geo search.
        geo_point: tuple[float, float] | None = params.pop("geo_point", None)
        geo_radius: int | None = params.pop("geo_radius", None)
        geo_sort: str | None = params.pop("geo_sort", None)
        geo_field: str = params.pop("geo_field", ICV_SEARCH_TYPESENSE_GEO_FIELD)

        if geo_point is not None and geo_radius is not None:
            lat, lng = geo_point
            geo_filter = f"{geo_field}:({lat}, {lng}, {geo_radius} m)"
            filter_by = f"{filter_by} && {geo_filter}" if filter_by else geo_filter

        # Sort translation.
        sort_param = params.pop("sort", None)
        sort_by = ""
        if sort_param is not None:
            sort_by = translate_sort_to_typesense(sort_param)

        if geo_point is not None and geo_sort in ("asc", "desc"):
            lat, lng = geo_point
            geo_sort_expr = f"{geo_field}({lat},{lng}):asc" if geo_sort == "asc" else f"{geo_field}({lat},{lng}):desc"
            sort_by = f"{geo_sort_expr},{sort_by}" if sort_by else geo_sort_expr

        # Facets.
        facets: list[str] | None = params.pop("facets", None)

        # Highlighting.
        highlight_fields: list[str] | None = params.pop("highlight_fields", None)
        pre_tag: str = params.pop("highlight_pre_tag", "<mark>")
        post_tag: str = params.pop("highlight_post_tag", "</mark>")

        # Source filtering.
        attributes_to_retrieve: list[str] | None = params.pop(
            "attributes_to_retrieve", params.pop("attributesToRetrieve", None)
        )

        # Ranking score.
        _show_ranking_score: bool = params.pop("show_ranking_score", False)

        search_params: dict[str, Any] = {
            "q": query or "*",
            "query_by": params.pop("query_by", "*"),
            "per_page": limit,
            "page": (offset // limit) + 1 if limit > 0 else 1,
        }

        if filter_by:
            search_params["filter_by"] = filter_by
        if sort_by:
            search_params["sort_by"] = sort_by
        if facets:
            search_params["facet_by"] = ",".join(facets)
        if highlight_fields:
            search_params["highlight_fields"] = ",".join(highlight_fields)
            search_params["highlight_start_tag"] = pre_tag
            search_params["highlight_end_tag"] = post_tag
        if attributes_to_retrieve:
            include_fields = list(attributes_to_retrieve)
            if "id" not in include_fields:
                include_fields.append("id")
            search_params["include_fields"] = ",".join(include_fields)

        response = self._call(
            self._client.collections[uid].documents.search,
            search_params,
        )
        return self._normalise_search_response(response, query, limit, offset, highlight_fields)

    def get_stats(self, uid: str) -> dict[str, Any]:
        """Get collection stats from Typesense."""
        response = self._call(self._client.collections[uid].retrieve)
        return {
            "num_documents": response.get("num_documents", 0),
            "name": response.get("name", uid),
        }

    def health(self) -> bool:
        """Check Typesense health endpoint."""
        try:
            result = self._call(self._client.operations.perform, "health", {})
            return result.get("ok", False) is True
        except (SearchBackendError, SearchTimeoutError, IndexNotFoundError):
            return False

    # ------------------------------------------------------------------
    # Optional method overrides
    # ------------------------------------------------------------------

    def get_task(self, task_uid: str) -> dict[str, Any]:
        """Return a stub task dict.

        Typesense operations are synchronous; there is no async task queue.
        This method satisfies the interface contract by returning a dict that
        looks like a completed task.
        """
        return {"taskUid": task_uid, "status": "succeeded"}

    def multi_search(self, queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Execute multiple search queries via Typesense's ``/multi_search`` endpoint.

        Each query dict must contain ``uid`` and ``query``.

        Returns:
            A list of icv-search normalised result dicts in query order.
        """
        searches: list[dict[str, Any]] = []
        query_meta: list[dict[str, Any]] = []

        for query_dict in queries:
            uid = query_dict["uid"]
            q = query_dict.get("query", "")
            params = {k: v for k, v in query_dict.items() if k not in ("uid", "query")}
            limit = params.pop("limit", 20)
            offset = params.pop("offset", 0)

            filter_param = params.pop("filter", None)
            filter_by = translate_filter_to_typesense(filter_param) if filter_param else ""

            sort_param = params.pop("sort", None)
            sort_by = translate_sort_to_typesense(sort_param) if sort_param else ""

            facets: list[str] | None = params.pop("facets", None)
            highlight_fields: list[str] | None = params.pop("highlight_fields", None)
            pre_tag = params.pop("highlight_pre_tag", "<mark>")
            post_tag = params.pop("highlight_post_tag", "</mark>")

            search_entry: dict[str, Any] = {
                "collection": uid,
                "q": q or "*",
                "query_by": params.pop("query_by", "*"),
                "per_page": limit,
                "page": (offset // limit) + 1 if limit > 0 else 1,
            }

            if filter_by:
                search_entry["filter_by"] = filter_by
            if sort_by:
                search_entry["sort_by"] = sort_by
            if facets:
                search_entry["facet_by"] = ",".join(facets)
            if highlight_fields:
                search_entry["highlight_fields"] = ",".join(highlight_fields)
                search_entry["highlight_start_tag"] = pre_tag
                search_entry["highlight_end_tag"] = post_tag

            searches.append(search_entry)
            query_meta.append({"q": q, "limit": limit, "offset": offset, "highlight_fields": highlight_fields})

        raw_response = self._call(
            self._client.multi_search.perform,
            {"searches": searches},
            {},
        )
        raw_results = raw_response.get("results", [])

        results: list[dict[str, Any]] = []
        for i, resp in enumerate(raw_results):
            meta = query_meta[i]
            results.append(
                self._normalise_search_response(
                    resp,
                    meta["q"],
                    meta["limit"],
                    meta["offset"],
                    meta["highlight_fields"],
                )
            )

        return results

    def get_document(self, uid: str, document_id: str) -> dict[str, Any]:
        """Fetch a single document by primary key (BR-012).

        Returns the document dict with ``id`` always present.
        """
        response = self._call(self._client.collections[uid].documents[document_id].retrieve)
        doc = dict(response)
        if "id" not in doc:
            doc["id"] = document_id
        return doc

    def get_documents(
        self,
        uid: str,
        document_ids: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch multiple documents, optionally by IDs (BR-010).

        When ``document_ids`` is provided, fetches each document individually.
        When ``document_ids`` is ``None``, uses the export endpoint for browse.
        ``id`` is always included in returned fields.
        """
        effective_fields: set[str] | None = None
        if fields is not None:
            effective_fields = set(fields) | {"id"}

        if document_ids is not None:
            results: list[dict[str, Any]] = []
            for doc_id in document_ids:
                try:
                    doc = self._call(self._client.collections[uid].documents[doc_id].retrieve)
                    doc = dict(doc)
                    if "id" not in doc:
                        doc["id"] = doc_id
                    if effective_fields is not None:
                        doc = {k: v for k, v in doc.items() if k in effective_fields}
                    results.append(doc)
                except (IndexNotFoundError, SearchBackendError):
                    pass
            return results

        # Browse mode via search with q=*.
        search_params: dict[str, Any] = {
            "q": "*",
            "query_by": "id",
            "per_page": limit,
            "page": (offset // limit) + 1 if limit > 0 else 1,
        }
        if effective_fields is not None:
            search_params["include_fields"] = ",".join(effective_fields)

        response = self._call(
            self._client.collections[uid].documents.search,
            search_params,
        )
        hits = response.get("hits", [])
        docs: list[dict[str, Any]] = []
        for hit in hits:
            doc = dict(hit.get("document", {}))
            if "id" not in doc:
                doc["id"] = hit.get("document", {}).get("id", "")
            if effective_fields is not None:
                doc = {k: v for k, v in doc.items() if k in effective_fields}
            docs.append(doc)
        return docs

    def facet_search(
        self,
        uid: str,
        facet_name: str,
        facet_query: str = "",
        **params: Any,
    ) -> list[dict[str, Any]]:
        """Search within facet values for typeahead filter UIs.

        Uses Typesense's facet search via a ``q=*`` search with ``facet_query``.
        Returns ``[{"value": str, "count": int}]`` sorted by count descending
        (BR-014). The facet field must be declared with ``facet: true``.
        """
        filter_param = params.pop("filter", None)
        filter_by = translate_filter_to_typesense(filter_param) if filter_param else ""

        search_params: dict[str, Any] = {
            "q": facet_query or "*",
            "query_by": facet_name,
            "facet_by": facet_name,
            "per_page": 0,
        }

        if facet_query:
            search_params["facet_query"] = f"{facet_name}:{facet_query}"

        if filter_by:
            search_params["filter_by"] = filter_by

        response = self._call(
            self._client.collections[uid].documents.search,
            search_params,
        )

        facet_counts: list[dict[str, Any]] = response.get("facet_counts", [])
        results: list[dict[str, Any]] = []

        for facet in facet_counts:
            if facet.get("field_name") == facet_name:
                for item in facet.get("counts", []):
                    results.append(
                        {
                            "value": str(item["value"]),
                            "count": int(item["count"]),
                        }
                    )
                break

        return sorted(results, key=lambda x: x["count"], reverse=True)

    def similar_documents(
        self,
        uid: str,
        document_id: str,
        **params: Any,
    ) -> dict[str, Any]:
        """Not supported by Typesense.

        Typesense does not have a native similarity/more-like-this feature.
        Raise ``NotImplementedError`` per BR-013.
        """
        raise NotImplementedError(
            "TypesenseBackend does not support similar_documents(). "
            "Typesense does not have a native similarity/more-like-this feature."
        )

    def compact(self, uid: str) -> dict[str, Any]:
        """No-op for Typesense — compaction is managed internally (BR-016).

        Returns ``{}`` to satisfy the interface contract.
        """
        return {}

    def swap_indexes(self, pairs: list[tuple[str, str]]) -> dict[str, Any]:
        """Swap collection names using the Typesense aliases API.

        For each ``(index_a, index_b)`` pair, the alias ``index_a`` is
        updated to point to ``index_b``. If ``index_a`` is not currently
        an alias, a new alias ``index_a`` is created pointing to ``index_b``.

        Returns:
            A dict summarising the alias operations performed.
        """
        operations: list[dict[str, Any]] = []

        for alias_name, target_collection in pairs:
            result = self._call(
                self._client.aliases.upsert,
                alias_name,
                {"collection_name": target_collection},
            )
            operations.append(result)

        return {"aliases": operations}

    def update_documents(
        self,
        uid: str,
        documents: list[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Partial update of document fields using Typesense's ``emplace`` action.

        Only the supplied fields are modified; existing fields are preserved
        (BR-015). Uses ``action=emplace`` which does a partial update when the
        document already exists, or a full insert when it does not.
        """
        results = self._call(
            self._client.collections[uid].documents.import_,
            documents,
            {"action": "emplace"},
        )
        succeeded = sum(1 for r in results if r.get("success", False))
        failed = len(results) - succeeded
        return {"succeeded": succeeded, "failed": failed, "results": results}

    # ------------------------------------------------------------------
    # Private normalisation helpers
    # ------------------------------------------------------------------

    def _normalise_search_response(
        self,
        response: dict[str, Any],
        query: str,
        limit: int,
        offset: int,
        highlight_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Translate a raw Typesense search response to icv-search canonical format.

        Typesense returns::

            {
                "found": N,
                "search_time_ms": T,
                "hits": [
                    {"document": {...}, "highlights": [...], ...},
                    ...
                ],
                "facet_counts": [{"field_name": "...", "counts": [...]}],
            }

        This normalises it to the same shape used by all other backends::

            {
                "hits": [{"id": ..., ...fields}],
                "query": str,
                "processingTimeMs": int,
                "estimatedTotalHits": int,
                "limit": int,
                "offset": int,
                "facetDistribution": {"field": {"value": count}},
                "formatted_hits": [...],  # only when highlight_fields given
            }
        """
        raw_hits: list[dict[str, Any]] = response.get("hits", [])
        total: int = response.get("found", 0)
        took: int = response.get("search_time_ms", 0)

        hits: list[dict[str, Any]] = []
        formatted_hits: list[dict[str, Any]] = []

        for hit in raw_hits:
            doc = dict(hit.get("document", {}))
            if "id" not in doc:
                doc["id"] = hit.get("document", {}).get("id", "")
            hits.append(doc)

            if highlight_fields is not None:
                highlight_snippets = hit.get("highlights", [])
                formatted: dict[str, Any] = {}
                for hl in highlight_snippets:
                    field = hl.get("field", "")
                    snippet = hl.get("snippet", "")
                    if field:
                        formatted[field] = snippet
                formatted_hits.append(formatted)

        # Build facet distribution from facet_counts.
        facet_counts_raw: list[dict[str, Any]] = response.get("facet_counts", [])
        facet_distribution: dict[str, dict[str, int]] = {}
        for facet in facet_counts_raw:
            field_name = facet.get("field_name", "")
            if not field_name:
                continue
            facet_distribution[field_name] = {
                str(item["value"]): int(item["count"]) for item in facet.get("counts", [])
            }

        result: dict[str, Any] = {
            "hits": hits,
            "query": query,
            "processingTimeMs": took,
            "estimatedTotalHits": total,
            "limit": limit,
            "offset": offset,
            "facetDistribution": facet_distribution,
        }

        if highlight_fields is not None:
            result["formatted_hits"] = formatted_hits

        return result
