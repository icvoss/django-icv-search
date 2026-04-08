"""Vespa search backend using the pyvespa SDK.

Install the optional dependency before using this backend::

    pip install django-icv-search[vespa]

Configure via settings::

    ICV_SEARCH_BACKEND = "icv_search.backends.vespa.VespaBackend"
    ICV_SEARCH_URL = "https://my-app.vespa-app.cloud"
    ICV_SEARCH_API_KEY = "..."
    ICV_SEARCH_OPTIONS = {
        "application": "my-app",
        "content_cluster": "content",
        "schema": "product",
    }

Vespa schemas are deployed via ``vespa deploy`` and cannot be created at
runtime. ``create_index()`` validates connectivity and registers the index
locally but does not modify the Vespa application package.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ImproperlyConfigured

from icv_search.backends.base import BaseSearchBackend
from icv_search.exceptions import IndexNotFoundError, SearchBackendError, SearchTimeoutError

logger = logging.getLogger(__name__)

try:
    from vespa.application import Vespa as VespaApp  # type: ignore[import-untyped]

    _pyvespa_available = True
except ImportError:
    _pyvespa_available = False
    VespaApp = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# YQL translation helpers
# ---------------------------------------------------------------------------

# Operator suffix mapping for Django-style range lookups.
_RANGE_OPERATORS: dict[str, str] = {
    "__gte": ">=",
    "__gt": ">",
    "__lte": "<=",
    "__lt": "<",
}


def _yql_escape(value: str) -> str:
    """Escape a string value for safe embedding in a YQL double-quoted string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def translate_filter_to_yql(filters: dict[str, Any] | str) -> str:
    """Convert a Django-native filter dict to a YQL WHERE clause fragment.

    Supported forms:

    * ``{"category": "shoes"}`` → ``category contains "shoes"``
    * ``{"price": 12.99}`` → ``price = 12.99``
    * ``{"count": 5}`` → ``count = 5``
    * ``{"is_active": True}`` → ``is_active = true``
    * ``{"is_active": False}`` → ``is_active = false``
    * ``{"status": None}`` → ``!(status)`` (null / absent check)
    * ``{"category": ["shoes", "boots"]}`` → ``category in ["shoes", "boots"]``
    * ``{"price__gte": 20}`` → ``price >= 20``
    * ``{"price__lte": 100}`` → ``price <= 100``
    * ``{"price__gt": 0}`` → ``price > 0``
    * ``{"price__lt": 50}`` → ``price < 50``

    Multiple conditions are combined with `` AND ``.

    Args:
        filters: A dict of field/value pairs.

    Returns:
        A YQL WHERE clause fragment string, or ``""`` when the input is empty.

    Raises:
        SearchBackendError: When a raw string is passed instead of a dict.
    """
    if isinstance(filters, str):
        raise SearchBackendError(
            "translate_filter_to_yql does not accept raw YQL filter strings. "
            "Pass a dict of field/value pairs instead. To inject raw YQL, "
            "assign the string directly to the filter_yql variable in search()."
        )

    if not isinstance(filters, dict) or not filters:
        return ""

    parts: list[str] = []

    for field, value in filters.items():
        # Detect range-operator suffix (e.g. price__gte).
        range_op: str | None = None
        real_field = field
        for suffix, op in _RANGE_OPERATORS.items():
            if field.endswith(suffix):
                real_field = field[: -len(suffix)]
                range_op = op
                break

        if range_op is not None:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                parts.append(f"{real_field} {range_op} {value}")
            # Non-numeric range values are silently skipped.
        elif isinstance(value, bool):
            parts.append(f"{field} = {str(value).lower()}")
        elif isinstance(value, (int, float)):
            parts.append(f"{field} = {value}")
        elif isinstance(value, list):
            # YQL IN list — strings stay quoted, numerics unquoted.
            formatted_items = [f'"{_yql_escape(str(v))}"' if isinstance(v, str) else str(v) for v in value]
            parts.append(f"{field} in [{', '.join(formatted_items)}]")
        elif value is None:
            # Vespa does not have an IS NULL operator; negate the field's
            # presence using the NOT form.
            parts.append(f"!({field})")
        else:
            # String equality — Vespa uses ``contains`` for text fields.
            parts.append(f'{field} contains "{_yql_escape(str(value))}"')

    return " AND ".join(parts)


def translate_sort_to_yql(sort: list[str] | str) -> str:
    """Convert a Django-native sort list to a YQL ORDER BY clause fragment.

    Passes through strings unchanged so callers can inject raw YQL.

    Mapping rules:

    * ``["price"]`` → ``price asc``
    * ``["-price"]`` → ``price desc``
    * ``["price", "-created_at"]`` → ``price asc, created_at desc``
    * ``[]`` → ``""`` (no ORDER BY)

    When the returned value is non-empty it should be appended to the YQL
    statement as ``ORDER BY <value>``.

    Args:
        sort: A list of field names with an optional ``-`` prefix, or a
            pre-formatted string.

    Returns:
        A comma-separated YQL ORDER BY expression, or ``""`` when empty.
    """
    if isinstance(sort, str):
        return sort

    if not isinstance(sort, list) or not sort:
        return ""

    parts: list[str] = []
    for field in sort:
        if field.startswith("-"):
            parts.append(f"{field[1:]} desc")
        else:
            parts.append(f"{field} asc")

    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Response normalisation
# ---------------------------------------------------------------------------


def _normalise_hits(vespa_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten Vespa hit dicts to the icv-search canonical format.

    Vespa hit structure::

        {
            "id": "id:products:products::42",
            "relevance": 0.876,
            "source": "content/documents",
            "fields": {"title": "Widget", "price": 9.99}
        }

    Normalised to::

        {"id": "42", "title": "Widget", "price": 9.99, "_relevance": 0.876}
    """
    result: list[dict[str, Any]] = []
    for hit in vespa_hits:
        fields: dict[str, Any] = dict(hit.get("fields") or {})
        # Extract the short doc ID from Vespa's compound ID string.
        vespa_id: str = hit.get("id", "")
        short_id = vespa_id.rsplit("::", 1)[-1] if "::" in vespa_id else vespa_id
        fields.setdefault("id", short_id)
        fields["_relevance"] = hit.get("relevance", 0.0)
        result.append(fields)
    return result


# ---------------------------------------------------------------------------
# VespaBackend
# ---------------------------------------------------------------------------


class VespaBackend(BaseSearchBackend):
    """Search backend for Yahoo Vespa using the pyvespa SDK.

    Requires the ``[vespa]`` optional dependency::

        pip install django-icv-search[vespa]

    Unlike Meilisearch, Vespa schemas are deployed via the application
    package (``vespa deploy``). ``create_index()`` registers the index
    locally and validates connectivity but cannot create schemas at runtime.

    ``swap_indexes()`` is not supported — use Vespa's application
    redeployment for zero-downtime updates.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        timeout: int = 30,
        **kwargs: Any,
    ) -> None:
        if not _pyvespa_available:
            raise ImproperlyConfigured(
                "pyvespa is required to use VespaBackend. Install it with: pip install django-icv-search[vespa]"
            )

        super().__init__(url=url, api_key=api_key, timeout=timeout, **kwargs)

        # Vespa-specific constructor kwargs.
        self.application: str = kwargs.get("application", "")
        self.content_cluster: str = kwargs.get("content_cluster", "content")
        self.schema: str = kwargs.get("schema", "")
        cert_path: str = kwargs.get("cert_path", "")
        key_path: str = kwargs.get("key_path", "")

        # In-memory registries — per-instance state, not shared across workers.
        self._index_registry: dict[str, dict[str, Any]] = {}
        self._settings_registry: dict[str, dict[str, Any]] = {}

        # Build the pyvespa client.
        vespa_kwargs: dict[str, Any] = {}
        if cert_path:
            vespa_kwargs["cert"] = cert_path
        if key_path:
            vespa_kwargs["key"] = key_path
        if api_key and not cert_path:
            vespa_kwargs["auth_client_token_id"] = api_key

        self._app: VespaApp = VespaApp(url=url.rstrip("/"), **vespa_kwargs)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _schema_for(self, uid: str) -> str:
        """Return the schema name to use for a given index UID.

        Uses the UID itself if a default schema is not configured, which is
        the usual Vespa convention (schema name == document type == uid).
        """
        entry = self._index_registry.get(uid, {})
        return entry.get("schema", uid) or self.schema or uid

    def _wrap(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Call a pyvespa method and translate exceptions to icv-search types."""
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            exc_str = str(exc).lower()
            if "timeout" in exc_str or "timed out" in exc_str:
                raise SearchTimeoutError(f"Vespa request timed out: {exc}", exc) from exc
            if "404" in exc_str or "not found" in exc_str:
                raise IndexNotFoundError(f"Vespa resource not found: {exc}") from exc
            raise SearchBackendError(f"Vespa error: {exc}", exc) from exc

    def _check_response(self, response: Any, context: str = "") -> None:
        """Raise the appropriate icv-search exception for a failed VespaResponse."""
        if response is None:
            return
        status_code = getattr(response, "status_code", None)
        if status_code is None:
            return
        if status_code in (408, 503):
            raise SearchTimeoutError(
                f"Vespa timeout ({status_code}) {context}",
            )
        if status_code == 404:
            raise IndexNotFoundError(f"Vespa 404 {context}")
        if status_code >= 400:
            raise SearchBackendError(f"Vespa error {status_code} {context}")

    # ------------------------------------------------------------------
    # Abstract methods — index lifecycle
    # ------------------------------------------------------------------

    def create_index(self, uid: str, primary_key: str = "id") -> dict[str, Any]:
        """Register a Vespa schema locally and validate connectivity.

        Vespa schemas are deployed via ``vespa deploy`` and cannot be
        created at runtime. This method:

        1. Checks connectivity via the application status endpoint.
        2. Registers the UID in the local registry.
        3. Logs a warning if the schema is not visible via the Document API.

        Returns a TaskResult-compatible dict with ``status: "succeeded"``.
        """
        # Step 1 — connectivity check.
        try:
            self._app.get_application_status()
        except Exception as exc:
            raise SearchBackendError(f"Vespa connectivity check failed for '{uid}': {exc}", exc) from exc

        # Step 2 — local registry.
        self._index_registry[uid] = {"primary_key": primary_key, "schema": uid}

        # Step 3 — advisory schema check via Document API.
        try:
            self._app.query(
                body={
                    "yql": f"select * from {uid} where true limit 0",
                    "hits": 0,
                }
            )
        except Exception:
            logger.warning(
                "Vespa schema '%s' does not appear to exist in the deployed "
                "application package. Add a schema definition to your "
                "application package and redeploy with 'vespa deploy'.",
                uid,
            )

        return {
            "taskUid": f"vespa-create-{uid}",
            "indexUid": uid,
            "status": "succeeded",
        }

    def delete_index(self, uid: str) -> None:
        """Delete all documents in the Vespa schema identified by ``uid``.

        Uses the Document API visitor endpoint with ``selection=true`` to
        remove all documents. The Vespa schema itself is not dropped —
        schema changes require an application package redeployment.

        404 responses are treated as a no-op (already absent).
        """
        schema = self._schema_for(uid)
        try:
            self._app.delete_all_docs(
                content_cluster_name=self.content_cluster,
                schema=schema,
            )
        except Exception as exc:
            exc_str = str(exc).lower()
            if "404" in exc_str or "not found" in exc_str:
                # Already absent — treat as success.
                pass
            elif "timeout" in exc_str or "timed out" in exc_str:
                raise SearchTimeoutError(f"Vespa timeout deleting index '{uid}': {exc}", exc) from exc
            else:
                raise SearchBackendError(f"Vespa error deleting index '{uid}': {exc}", exc) from exc

        self._index_registry.pop(uid, None)
        self._settings_registry.pop(uid, None)

    def update_settings(self, uid: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Store settings locally and return an advisory success response.

        Vespa settings (``searchableAttributes``, ``filterableAttributes``,
        ``sortableAttributes``) correspond to schema field configurations
        that must be deployed via the application package. This method
        stores them locally for advisory output from the
        ``icv_search_setup`` management command.

        No network call is made.
        """
        logger.warning(
            "Vespa settings for '%s' are schema-level and cannot be applied "
            "at runtime. Configure them in your application package's .sd "
            "file and redeploy with 'vespa deploy'. Settings stored locally "
            "for advisory purposes only.",
            uid,
        )
        self._settings_registry[uid] = settings
        return {
            "taskUid": f"vespa-settings-{uid}",
            "indexUid": uid,
            "status": "succeeded",
        }

    def get_settings(self, uid: str) -> dict[str, Any]:
        """Return locally stored settings for ``uid``.

        Returns an empty dict when no settings have been registered.
        Vespa does not expose a runtime settings API so no network call
        is made.
        """
        return dict(self._settings_registry.get(uid, {}))

    def add_documents(
        self,
        uid: str,
        documents: list[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Feed documents to Vespa using pyvespa's ``feed_batch()``.

        pyvespa's ``feed_batch`` uses HTTP/2 feed sessions for concurrent
        ingestion, handling back-pressure and retry automatically.

        Documents are wrapped in Vespa's feed format::

            {"id": "id:{uid}:{uid}::{doc_id}", "fields": {...}}

        Args:
            uid: Index UID (used as the schema name).
            documents: List of document dicts.
            primary_key: Field name of the document primary key.

        Returns:
            Task-compatible dict with document count.
        """
        schema = self._schema_for(uid)
        vespa_docs = [
            {
                "id": f"id:{schema}:{schema}::{doc[primary_key]}",
                "fields": {k: v for k, v in doc.items() if k != primary_key},
            }
            for doc in documents
        ]
        try:
            self._app.feed_batch(vespa_docs, schema=schema)
        except Exception as exc:
            exc_str = str(exc).lower()
            if "timeout" in exc_str or "timed out" in exc_str:
                raise SearchTimeoutError(f"Vespa feed timeout for '{uid}': {exc}", exc) from exc
            raise SearchBackendError(f"Vespa feed error for '{uid}': {exc}", exc) from exc

        return {
            "taskUid": f"vespa-add-{uid}-{len(documents)}",
            "indexUid": uid,
            "status": "succeeded",
            "documentCount": len(documents),
        }

    def delete_documents(
        self,
        uid: str,
        document_ids: list[str],
    ) -> dict[str, Any]:
        """Delete specific documents by ID from Vespa.

        Uses pyvespa's ``delete_data()`` for each document ID. 404
        responses (document already absent) are treated as a no-op.

        Args:
            uid: Index UID (used as the schema name).
            document_ids: List of document primary key values.

        Returns:
            Task-compatible dict.
        """
        schema = self._schema_for(uid)
        errors: list[str] = []

        for doc_id in document_ids:
            try:
                response = self._app.delete_data(schema=schema, data_id=str(doc_id))
                if response is not None:
                    status = getattr(response, "status_code", None)
                    if status is not None and status not in (200, 204, 404):
                        errors.append(f"doc {doc_id}: HTTP {status}")
            except Exception as exc:
                exc_str = str(exc).lower()
                if "404" in exc_str or "not found" in exc_str:
                    continue  # Already absent.
                errors.append(f"doc {doc_id}: {exc}")

        if errors:
            raise SearchBackendError(f"Vespa delete_documents errors for '{uid}': {'; '.join(errors)}")

        return {
            "taskUid": f"vespa-delete-{uid}",
            "indexUid": uid,
            "status": "succeeded",
            "documentCount": len(document_ids),
        }

    def clear_documents(self, uid: str) -> dict[str, Any]:
        """Remove all documents from the Vespa schema without dropping it.

        Uses pyvespa's ``delete_all_docs()`` which sends a selection-based
        DELETE to the Document API (``selection=true``).

        Returns:
            Task-compatible dict.
        """
        schema = self._schema_for(uid)
        try:
            self._app.delete_all_docs(
                content_cluster_name=self.content_cluster,
                schema=schema,
            )
        except Exception as exc:
            exc_str = str(exc).lower()
            if "404" in exc_str or "not found" in exc_str:
                pass  # Already empty — not an error.
            elif "timeout" in exc_str or "timed out" in exc_str:
                raise SearchTimeoutError(f"Vespa timeout clearing '{uid}': {exc}", exc) from exc
            else:
                raise SearchBackendError(f"Vespa error clearing '{uid}': {exc}", exc) from exc

        return {
            "taskUid": f"vespa-clear-{uid}",
            "indexUid": uid,
            "status": "succeeded",
        }

    def search(self, uid: str, query: str, **params: Any) -> dict[str, Any]:
        """Execute a YQL search query against Vespa.

        YQL is constructed from the query string and optional filter/sort
        params. The ``userQuery()`` Vespa built-in is used for the text
        match component so that Vespa can apply query rewriting, stemming,
        and the configured ranking profile.

        Recognised params:

        * ``limit`` (int) — max hits (default 20). Maps to Vespa ``hits``.
        * ``offset`` (int) — pagination offset.
        * ``filter`` (dict | str) — Django-native filter dict or raw YQL fragment.
        * ``sort`` (list[str]) — Django-native sort list.
        * ``ranking`` (str) — Vespa ranking profile name.
        * ``ranking.features`` (dict) — Feature overrides for the ranking profile.
        * ``attributesToRetrieve`` (list[str]) — Field projection (BR-010).
        * ``highlight`` (bool) — Enable Vespa bolding / dynamic summaries.
        * ``summary`` (str) — Vespa summary class override (default: ``"dynamic"``).
        * ``facets`` (list[str]) — Fields to facet on.
        * ``geo`` (dict) — Geo filter config (keys: lat, lng, radius_m, field,
          rank_by_distance).

        Returns:
            Normalised search result dict compatible with ``SearchResult.from_engine()``.
        """
        limit: int = params.pop("limit", 20)
        offset: int = params.pop("offset", 0)
        filter_param: Any = params.pop("filter", None)
        sort_param: Any = params.pop("sort", None)
        ranking: str = params.pop("ranking", "default")
        ranking_features: dict[str, Any] | None = params.pop("ranking.features", None)
        attrs_to_retrieve: list[str] | None = params.pop(
            "attributes_to_retrieve", params.pop("attributesToRetrieve", None)
        )
        highlight: bool = params.pop("highlight", False)
        summary_class: str | None = params.pop("summary", None)
        facets: list[str] | None = params.pop("facets", None)
        geo: dict[str, Any] | None = params.pop("geo", None)

        # --- SELECT clause ---
        if attrs_to_retrieve:
            # Always include id in the projection (BR-010).
            fields = attrs_to_retrieve if "id" in attrs_to_retrieve else ["id", *attrs_to_retrieve]
            select_clause = ", ".join(fields)
        else:
            select_clause = "*"

        # --- WHERE clause ---
        schema = self._schema_for(uid)
        text_clause = "userQuery()"

        filter_yql = ""
        if filter_param is not None:
            if isinstance(filter_param, dict):
                filter_yql = translate_filter_to_yql(filter_param)
            elif isinstance(filter_param, str):
                filter_yql = filter_param

        # Geo filter.
        geo_yql = ""
        if geo:
            geo_lat = geo.get("lat", 0.0)
            geo_lng = geo.get("lng", 0.0)
            geo_radius_m = geo.get("radius_m", 0)
            geo_field = geo.get("field", "location")
            radius_km = geo_radius_m / 1000.0
            geo_yql = f'geoLocation({geo_field}, {geo_lat}, {geo_lng}, "{radius_km} km")'

        all_filters = " AND ".join(f for f in [filter_yql, geo_yql] if f)

        where_clause = f"({text_clause}) AND ({all_filters})" if all_filters else text_clause

        # --- ORDER BY clause ---
        order_clause = ""
        if sort_param:
            order_clause = translate_sort_to_yql(sort_param)

        # Assemble YQL.
        yql = f"select {select_clause} from {schema} where {where_clause}"
        if order_clause:
            yql = f"{yql} order by {order_clause}"

        # --- Request body ---
        body: dict[str, Any] = {
            "yql": yql,
            "query": query,
            "hits": limit,
            "offset": offset,
            "timeout": f"{self.timeout}s",
        }

        if ranking and ranking != "default":
            body["ranking"] = ranking
        if ranking_features:
            body["ranking.features"] = ranking_features
        if highlight:
            body["presentation.bolding"] = True
            body["summary"] = summary_class or "dynamic"
        elif summary_class:
            body["summary"] = summary_class

        # Faceting — Vespa grouping syntax.
        if facets:
            grouping_items = " ".join(f"all(group({f}) max(10) each(output(count())))" for f in facets)
            body["select.grouping"] = grouping_items

        # Pass remaining unrecognised kwargs directly to Vespa.
        body.update(params)

        try:
            response = self._app.query(body=body)
        except Exception as exc:
            exc_str = str(exc).lower()
            if "timeout" in exc_str or "timed out" in exc_str:
                raise SearchTimeoutError(f"Vespa search timeout: {exc}", exc) from exc
            raise SearchBackendError(f"Vespa search error: {exc}", exc) from exc

        self._check_response(response, context=f"search uid={uid}")

        # --- Normalise response ---
        raw_json: dict[str, Any] = {}
        if hasattr(response, "json"):
            raw_json = response.json or {}
        elif hasattr(response, "get_json"):
            raw_json = response.get_json() or {}

        root = raw_json.get("root", {})
        root_fields = root.get("fields", {})
        total_count: int = root_fields.get("totalCount", 0)

        # Extract hits from root.children.
        raw_hits: list[dict[str, Any]] = []
        for child in root.get("children", []):
            if child.get("id") == "toplevel":
                raw_hits = child.get("children", [])
                break
        if not raw_hits and hasattr(response, "hits"):
            # Fallback: some pyvespa versions surface hits directly via .hits.
            raw_hits = response.hits or []

        hits = _normalise_hits(raw_hits)

        # Collect ranking scores.
        ranking_scores = [h.pop("_relevance", 0.0) for h in hits]
        # Re-add so they are available in the canonical format.
        for hit, score in zip(hits, ranking_scores, strict=True):
            hit["_relevance"] = score

        # Timing.
        timing = raw_json.get("timing", {})
        processing_ms = int(timing.get("querytime", 0) * 1000)

        # Facet distribution from grouping results.
        facet_distribution: dict[str, dict[str, int]] = {}
        # (Grouping result parsing is engine-specific; basic extraction.)
        for child in root.get("children", []):
            if child.get("id") == "grouping":
                for group in child.get("children", []):
                    field_name = group.get("id", "")
                    facet_distribution[field_name] = {
                        item.get("value", ""): item.get("count", 0) for item in group.get("children", [])
                    }

        return {
            "hits": hits,
            "query": query,
            "processingTimeMs": processing_ms,
            "estimatedTotalHits": total_count,
            "limit": limit,
            "offset": offset,
            "facetDistribution": facet_distribution,
            "formatted_hits": hits if highlight else [],
            "ranking_scores": ranking_scores,
        }

    def get_stats(self, uid: str) -> dict[str, Any]:
        """Return document count and index statistics for the given UID.

        Queries the Document API visitor endpoint with
        ``wantedDocumentCount=0`` to retrieve only the count metadata.

        Returns:
            Dict compatible with ``IndexStats.from_engine()``.
        """
        schema = self._schema_for(uid)
        try:
            response = self._app.query(
                body={
                    "yql": f"select * from {schema} where true",
                    "hits": 0,
                    "ranking.matching.numThreadsPerSearch": 1,
                }
            )
        except Exception as exc:
            exc_str = str(exc).lower()
            if "timeout" in exc_str or "timed out" in exc_str:
                raise SearchTimeoutError(f"Vespa get_stats timeout for '{uid}': {exc}", exc) from exc
            raise SearchBackendError(f"Vespa get_stats error for '{uid}': {exc}", exc) from exc

        self._check_response(response, context=f"get_stats uid={uid}")

        raw_json: dict[str, Any] = {}
        if hasattr(response, "json"):
            raw_json = response.json or {}
        elif hasattr(response, "get_json"):
            raw_json = response.get_json() or {}

        total_count = raw_json.get("root", {}).get("fields", {}).get("totalCount", 0)

        return {
            "document_count": total_count,
            "is_indexing": False,
            "field_distribution": {},
        }

    def health(self) -> bool:
        """Check Vespa application connectivity.

        Uses pyvespa's ``get_application_status()`` method. Returns
        ``False`` (never raises) on any failure so callers can distinguish
        degraded from fully unavailable (BR-005).
        """
        try:
            self._app.get_application_status()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Optional methods
    # ------------------------------------------------------------------

    def get_task(self, task_uid: str) -> dict[str, Any]:
        """Return an immediate success response.

        Vespa Document API operations are synchronous — there is no
        async task queue. This mirrors ``PostgresBackend.get_task()``.
        """
        return {
            "uid": task_uid,
            "status": "succeeded",
        }

    def swap_indexes(self, pairs: list[tuple[str, str]]) -> dict[str, Any]:
        """Not supported by Vespa.

        Atomic production deployments use ``vespa deploy`` (application
        redeployment), which is an infrastructure-level operation outside
        icv-search's scope.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            "VespaBackend does not support swap_indexes(). "
            "Use Vespa's application deployment (vespa deploy) for "
            "zero-downtime index updates."
        )

    def get_document(self, uid: str, document_id: str) -> dict[str, Any]:
        """Fetch a single document by its primary key.

        Uses pyvespa's ``get_data()`` method. The returned dict always
        includes ``id`` (BR-012).

        Args:
            uid: Index UID (used as the schema name).
            document_id: Document primary key value.

        Returns:
            Dict of document fields with ``id`` guaranteed.

        Raises:
            SearchBackendError: When the document cannot be retrieved.
        """
        schema = self._schema_for(uid)
        try:
            response = self._app.get_data(schema=schema, data_id=str(document_id))
        except Exception as exc:
            exc_str = str(exc).lower()
            if "404" in exc_str or "not found" in exc_str:
                raise IndexNotFoundError(f"Document '{document_id}' not found in '{uid}'") from exc
            if "timeout" in exc_str or "timed out" in exc_str:
                raise SearchTimeoutError(f"Vespa get_document timeout for '{uid}/{document_id}': {exc}", exc) from exc
            raise SearchBackendError(f"Vespa get_document error for '{uid}/{document_id}': {exc}", exc) from exc

        self._check_response(response, context=f"get_document uid={uid} id={document_id}")

        doc_json: dict[str, Any] = {}
        if hasattr(response, "get_json"):
            doc_json = response.get_json() or {}
        elif hasattr(response, "json"):
            doc_json = response.json or {}

        fields: dict[str, Any] = dict(doc_json.get("fields", {}))
        fields["id"] = str(document_id)  # BR-012
        return fields

    def facet_search(
        self,
        uid: str,
        facet_name: str,
        facet_query: str = "",
        **params: Any,
    ) -> list[dict[str, Any]]:
        """Not supported by the Vespa backend.

        Vespa does not have a native facet-value typeahead endpoint
        equivalent to Meilisearch's ``/facet-search``. Facet counts can
        be retrieved via grouping in ``search()``.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            "VespaBackend does not support facet_search(). "
            "Use the 'facets' param in search() to obtain facet counts via "
            "Vespa grouping, or implement a custom YQL grouping query."
        )

    def similar_documents(
        self,
        uid: str,
        document_id: str,
        **params: Any,
    ) -> dict[str, Any]:
        """Not supported by the Vespa backend without schema-specific config.

        Vespa's ``nearestNeighbor`` operator requires a ``tensor`` typed
        field and a matching query vector. This cannot be expressed as a
        generic backend call without schema knowledge.

        Consuming projects that need similarity search should query the
        ``nearestNeighbor`` operator directly via the ``filter`` param in
        ``search()`` with a raw YQL fragment.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            "VespaBackend does not support similar_documents(). "
            "Use the 'filter' param in search() with a raw nearestNeighbor "
            "YQL fragment and the 'ranking.features' param for vector injection."
        )

    def compact(self, uid: str) -> dict[str, Any]:
        """No-op — Vespa manages compaction automatically.

        Returns ``{}`` without a network call (BR-016).
        """
        return {}

    def update_documents(
        self,
        uid: str,
        documents: list[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Perform partial field updates on existing Vespa documents.

        Uses pyvespa's ``update_data()`` with Vespa's ``assign`` operator
        for each field. Only the supplied fields are modified; all other
        fields are preserved (BR-015).

        Args:
            uid: Index UID (used as the schema name).
            documents: List of partial document dicts. Each must include
                the primary key field.
            primary_key: Field name of the document primary key.

        Returns:
            Task-compatible dict.
        """
        schema = self._schema_for(uid)
        errors: list[str] = []

        for doc in documents:
            doc_id = doc[primary_key]
            fields = {k: {"assign": v} for k, v in doc.items() if k != primary_key}
            try:
                response = self._app.update_data(
                    schema=schema,
                    data_id=str(doc_id),
                    fields=fields,
                )
                if response is not None:
                    status = getattr(response, "status_code", None)
                    if status is not None and status >= 400:
                        errors.append(f"doc {doc_id}: HTTP {status}")
            except Exception as exc:
                errors.append(f"doc {doc_id}: {exc}")

        if errors:
            raise SearchBackendError(f"Vespa update_documents errors for '{uid}': {'; '.join(errors)}")

        return {
            "taskUid": f"vespa-update-{uid}",
            "indexUid": uid,
            "status": "succeeded",
        }
