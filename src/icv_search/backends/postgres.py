"""PostgreSQL full-text search backend.

Uses Django's ``django.contrib.postgres.search`` module to provide full-text
search without any external search engine. Documents are stored in a single
``icv_search_document`` table with a ``tsvector`` column, partitioned logically
by ``index_uid``.

Requirements:
    - PostgreSQL database
    - ``django.contrib.postgres`` in ``INSTALLED_APPS``

Usage::

    ICV_SEARCH_BACKEND = "icv_search.backends.postgres.PostgresBackend"
    # ICV_SEARCH_URL and ICV_SEARCH_API_KEY are ignored by this backend.

Notes:
    Tables are created by the backend itself via raw SQL (``CREATE TABLE IF NOT
    EXISTS``) rather than Django migrations. This makes the backend self-contained
    and avoids requiring consumers to run ``migrate`` when switching backends.

    All operations are synchronous — PostgreSQL processes them in the same
    transaction as the caller. The ``get_task`` method therefore always returns a
    succeeded status.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from django.db import connection

from icv_search.backends.base import BaseSearchBackend
from icv_search.backends.filters import _RANGE_OPERATORS
from icv_search.exceptions import IndexNotFoundError, SearchBackendError

logger = logging.getLogger(__name__)

# Table names used by this backend.
_TABLE = "icv_search_document"
_META_TABLE = "icv_search_index_meta"


class PostgresBackend(BaseSearchBackend):
    """PostgreSQL full-text search backend.

    Stores documents in ``icv_search_document`` (one row per document), logically
    partitioned by ``index_uid``. Full-text search is performed with PostgreSQL's
    ``tsvector`` / ``tsquery`` with ``ts_rank`` for relevance ordering.

    The ``'simple'`` text-search configuration is used throughout so that the
    backend works across all locales without requiring a locale-specific dictionary
    to be installed.

    Index settings (``searchableAttributes``, ``filterableAttributes``, etc.) are
    stored in ``icv_search_index_meta`` and consulted when building the tsvector
    for newly indexed documents.
    """

    def __init__(self, url: str = "", api_key: str = "", timeout: int = 30, **kwargs: Any) -> None:
        super().__init__(url=url, api_key=api_key, timeout=timeout, **kwargs)
        self._ensure_tables()

    # ------------------------------------------------------------------
    # Table bootstrap
    # ------------------------------------------------------------------

    def _ensure_tables(self) -> None:
        """Create document and metadata tables if they do not already exist.

        Idempotent — safe to call on every backend instantiation.
        """
        with connection.cursor() as cursor:
            # Main document store
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    id          BIGSERIAL PRIMARY KEY,
                    index_uid   VARCHAR(255) NOT NULL,
                    doc_id      VARCHAR(255) NOT NULL,
                    body        JSONB        NOT NULL DEFAULT '{{}}',
                    search_vector TSVECTOR,
                    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                    UNIQUE (index_uid, doc_id)
                )
                """
            )
            # GIN index on tsvector — the core of fast FTS lookups.
            cursor.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{_TABLE}_search_vector
                ON {_TABLE} USING GIN (search_vector)
                """
            )
            # B-tree index for partition-style filtering by index_uid.
            cursor.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{_TABLE}_index_uid
                ON {_TABLE} (index_uid)
                """
            )
            # Metadata table for per-index settings and primary-key config.
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_META_TABLE} (
                    index_uid   VARCHAR(255) PRIMARY KEY,
                    primary_key VARCHAR(100) NOT NULL DEFAULT 'id',
                    settings    JSONB        NOT NULL DEFAULT '{{}}',
                    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
                )
                """
            )

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def create_index(self, uid: str, primary_key: str = "id") -> dict[str, Any]:
        """Register a logical index in the metadata table.

        Idempotent — if the index already exists, the primary key is updated.
        """
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO {_META_TABLE} (index_uid, primary_key)
                    VALUES (%s, %s)
                    ON CONFLICT (index_uid) DO UPDATE
                        SET primary_key = EXCLUDED.primary_key
                    """,
                    [uid, primary_key],
                )
        except Exception as exc:
            raise SearchBackendError(f"Failed to create index '{uid}'.", original_exception=exc) from exc

        return {
            "taskUid": f"pg-create-{uid}",
            "indexUid": uid,
            "status": "succeeded",
        }

    def delete_index(self, uid: str) -> None:
        """Delete all documents and the metadata record for a logical index."""
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {_TABLE} WHERE index_uid = %s", [uid])
                cursor.execute(f"DELETE FROM {_META_TABLE} WHERE index_uid = %s", [uid])
        except Exception as exc:
            raise SearchBackendError(f"Failed to delete index '{uid}'.", original_exception=exc) from exc

    def update_settings(self, uid: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Store index settings in the metadata table.

        Raises ``IndexNotFoundError`` if the index does not exist.
        """
        settings_json = json.dumps(settings)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {_META_TABLE} SET settings = %s WHERE index_uid = %s",
                    [settings_json, uid],
                )
                if cursor.rowcount == 0:
                    raise IndexNotFoundError(f"Index '{uid}' not found.")
        except IndexNotFoundError:
            raise
        except Exception as exc:
            raise SearchBackendError(f"Failed to update settings for index '{uid}'.", original_exception=exc) from exc

        return {
            "taskUid": f"pg-settings-{uid}",
            "indexUid": uid,
            "status": "succeeded",
        }

    def get_settings(self, uid: str) -> dict[str, Any]:
        """Retrieve index settings from the metadata table.

        Raises ``IndexNotFoundError`` if the index does not exist.
        """
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT settings FROM {_META_TABLE} WHERE index_uid = %s",
                    [uid],
                )
                row = cursor.fetchone()
        except Exception as exc:
            raise SearchBackendError(f"Failed to retrieve settings for index '{uid}'.", original_exception=exc) from exc

        if row is None:
            raise IndexNotFoundError(f"Index '{uid}' not found.")

        raw = row[0]
        return raw if isinstance(raw, dict) else json.loads(raw)

    # ------------------------------------------------------------------
    # Document operations
    # ------------------------------------------------------------------

    def add_documents(
        self,
        uid: str,
        documents: list[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Add or update documents in the index.

        Builds a ``tsvector`` from the document's searchable fields. When
        ``searchableAttributes`` is configured in the index settings only those
        fields contribute to the tsvector; otherwise all string-valued fields
        are included.
        """
        if not documents:
            return {
                "taskUid": f"pg-add-{uid}",
                "indexUid": uid,
                "status": "succeeded",
            }

        searchable_fields = self._get_searchable_fields(uid)

        try:
            with connection.cursor() as cursor:
                for doc in documents:
                    doc_id = str(doc.get(primary_key, ""))
                    body_json = json.dumps(doc, default=str)
                    search_text = self._build_search_text(doc, searchable_fields)

                    cursor.execute(
                        f"""
                        INSERT INTO {_TABLE}
                            (index_uid, doc_id, body, search_vector, updated_at)
                        VALUES
                            (%s, %s, %s::jsonb, to_tsvector('simple', %s), NOW())
                        ON CONFLICT (index_uid, doc_id) DO UPDATE SET
                            body          = EXCLUDED.body,
                            search_vector = EXCLUDED.search_vector,
                            updated_at    = NOW()
                        """,
                        [uid, doc_id, body_json, search_text],
                    )
        except Exception as exc:
            raise SearchBackendError(f"Failed to add documents to index '{uid}'.", original_exception=exc) from exc

        return {
            "taskUid": f"pg-add-{uid}",
            "indexUid": uid,
            "status": "succeeded",
        }

    def delete_documents(self, uid: str, document_ids: list[str]) -> dict[str, Any]:
        """Remove documents by their primary-key values."""
        if not document_ids:
            return {
                "taskUid": f"pg-delete-{uid}",
                "indexUid": uid,
                "status": "succeeded",
            }

        placeholders = ", ".join(["%s"] * len(document_ids))
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"DELETE FROM {_TABLE} WHERE index_uid = %s AND doc_id IN ({placeholders})",
                    [uid, *document_ids],
                )
        except Exception as exc:
            raise SearchBackendError(f"Failed to delete documents from index '{uid}'.", original_exception=exc) from exc

        return {
            "taskUid": f"pg-delete-{uid}",
            "indexUid": uid,
            "status": "succeeded",
        }

    def clear_documents(self, uid: str) -> dict[str, Any]:
        """Remove all documents from an index without deleting the index itself."""
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {_TABLE} WHERE index_uid = %s", [uid])
        except Exception as exc:
            raise SearchBackendError(f"Failed to clear documents from index '{uid}'.", original_exception=exc) from exc

        return {
            "taskUid": f"pg-clear-{uid}",
            "indexUid": uid,
            "status": "succeeded",
        }

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, uid: str, query: str, **params: Any) -> dict[str, Any]:
        """Execute a full-text search query against a logical index.

        Supports filtering, sorting, highlighting, and facets via Django-native
        parameter conventions.

        Args:
            uid: The index UID to search.
            query: Free-text query string. An empty string returns all documents.
            **params:
                limit (int): Maximum number of results (default 20).
                offset (int): Number of results to skip (default 0).
                filter (dict | str): Filters to apply.

                    - ``dict`` — maps field names to expected values.
                      Supports ``str``, ``bool``, ``int``/``float``, and ``list``
                      (IN filter) values::

                          filter={"category": "equipment", "is_active": True}
                          filter={"price": [10, 20, 30]}  # IN filter

                    - ``str`` — passed through as a raw SQL ``WHERE`` fragment
                      (advanced use; caller is responsible for injection safety).

                sort (list[str]): Field names to sort by. Prefix with ``-`` for
                    descending order::

                        sort=["-price", "name"]

                highlight_fields (list[str]): Document fields to highlight using
                    PostgreSQL ``ts_headline()``.
                highlight_pre_tag (str): Opening HTML tag for matches.
                    Defaults to ``<mark>``.
                highlight_post_tag (str): Closing HTML tag for matches.
                    Defaults to ``</mark>``.
                geo_point (tuple[float, float]): ``(lat, lng)`` origin for
                    geo-distance filtering and sorting.
                geo_radius (int | None): When combined with ``geo_point``,
                    only documents whose ``_geo`` position lies within this
                    radius (metres) are returned.  Uses a Haversine
                    approximation computed in pure SQL — accurate but not
                    indexed.  For high-volume geo queries consider PostGIS.
                geo_sort (str): ``"asc"`` or ``"desc"``.  When combined with
                    ``geo_point``, results are ordered by Haversine distance
                    from the point.

                .. note::
                    The PostgreSQL geo implementation uses a pure-SQL
                    Haversine approximation and is suitable for prototyping.
                    For production geo search with large datasets install
                    PostGIS and use its spatial indexes.

        Returns:
            dict with keys ``hits``, ``query``, ``processingTimeMs``,
            ``estimatedTotalHits``, ``limit``, ``offset``, and optionally
            ``formatted_hits`` when ``highlight_fields`` is provided.
        """
        start = time.monotonic()

        limit: int = int(params.get("limit", 20))
        offset: int = int(params.get("offset", 0))
        filters: dict[str, Any] | str = params.get("filter", {})
        sort: list[str] = params.get("sort", [])
        facets: list[str] | None = params.get("facets")
        highlight_fields: list[str] = params.get("highlight_fields") or []
        pre_tag: str = params.get("highlight_pre_tag", "<mark>")
        post_tag: str = params.get("highlight_post_tag", "</mark>")
        geo_point: tuple[float, float] | None = params.get("geo_point")
        geo_radius: int | None = params.get("geo_radius")
        geo_sort: str | None = params.get("geo_sort")

        # --- WHERE clause ---------------------------------------------------
        where_clauses: list[str] = ["index_uid = %s"]
        where_params: list[Any] = [uid]

        if query:
            where_clauses.append("search_vector @@ plainto_tsquery('simple', %s)")
            where_params.append(query)

        if isinstance(filters, dict):
            for field, value in filters.items():
                # Check for range lookup suffixes (e.g. price__gte, created_at__lt).
                range_op: str | None = None
                real_field = field
                for suffix, op in _RANGE_OPERATORS.items():
                    if field.endswith(suffix):
                        real_field = field[: -len(suffix)]
                        range_op = op
                        break

                if range_op is not None:
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        where_clauses.append(f"(body->>%s)::numeric {range_op} %s")
                        where_params.extend([real_field, value])
                    # Non-numeric range values are silently skipped.
                elif isinstance(value, bool):
                    where_clauses.append("(body->>%s)::boolean = %s")
                    where_params.extend([field, value])
                elif isinstance(value, (int, float)):
                    where_clauses.append("(body->>%s)::numeric = %s")
                    where_params.extend([field, value])
                elif isinstance(value, list):
                    placeholders = ", ".join(["%s"] * len(value))
                    where_clauses.append(f"body->>%s IN ({placeholders})")
                    where_params.extend([field, *[str(v) for v in value]])
                else:
                    where_clauses.append("body->>%s = %s")
                    where_params.extend([field, str(value)])
        elif isinstance(filters, str) and filters:
            raise SearchBackendError(
                "PostgresBackend does not accept raw SQL filter strings. Pass a dict of field/value pairs instead."
            )

        # Geo radius filter: Haversine approximation in SQL.
        # Documents must have body->>'_geo' parseable as {"lat": ..., "lng": ...}.
        if geo_point is not None and geo_radius is not None:
            origin_lat, origin_lng = geo_point
            # 6371000 metres = mean Earth radius.
            where_clauses.append(
                """
                (
                    body->'_geo' IS NOT NULL
                    AND (
                        6371000.0 * 2 * ASIN(SQRT(
                            POWER(SIN((RADIANS((body->'_geo'->>'lat')::float) - RADIANS(%s)) / 2), 2)
                            + COS(RADIANS(%s)) * COS(RADIANS((body->'_geo'->>'lat')::float))
                            * POWER(SIN((RADIANS((body->'_geo'->>'lng')::float) - RADIANS(%s)) / 2), 2)
                        ))
                    ) <= %s
                )
                """
            )
            where_params.extend([origin_lat, origin_lat, origin_lng, geo_radius])

        where_sql = " AND ".join(where_clauses)

        # --- ORDER BY clause ------------------------------------------------
        order_parts: list[str] = []
        order_params: list[Any] = []

        # Geo distance sort takes precedence over other sort fields.
        if geo_point is not None and geo_sort in ("asc", "desc"):
            origin_lat, origin_lng = geo_point
            direction = "ASC" if geo_sort == "asc" else "DESC"
            order_parts.append(
                f"""
                6371000.0 * 2 * ASIN(SQRT(
                    POWER(SIN((RADIANS((body->'_geo'->>'lat')::float) - RADIANS(%s)) / 2), 2)
                    + COS(RADIANS(%s)) * COS(RADIANS((body->'_geo'->>'lat')::float))
                    * POWER(SIN((RADIANS((body->'_geo'->>'lng')::float) - RADIANS(%s)) / 2), 2)
                )) {direction} NULLS LAST
                """
            )
            order_params.extend([origin_lat, origin_lat, origin_lng])

        if query:
            order_parts.append("ts_rank(search_vector, plainto_tsquery('simple', %s)) DESC")
            order_params.append(query)

        if isinstance(sort, list):
            for field in sort:
                # Use the JSONB operator (body->%s) rather than the text cast
                # (body->>%s) so that numeric values sort correctly.
                if field.startswith("-"):
                    order_parts.append("body->%s DESC NULLS LAST")
                    order_params.append(field[1:])
                else:
                    order_parts.append("body->%s ASC NULLS LAST")
                    order_params.append(field)

        order_sql = f"ORDER BY {', '.join(order_parts)}" if order_parts else ""

        # --- COUNT query ----------------------------------------------------
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(*) FROM {_TABLE} WHERE {where_sql}",
                    where_params,
                )
                total: int = cursor.fetchone()[0]

            # --- Fetch results ----------------------------------------------
            # When a query is provided also fetch ts_rank so we can populate
            # ranking_scores in the response.
            #
            # Parameter order must match the SQL placeholder order:
            #   1. ts_rank %s in the SELECT clause (query)
            #   2. WHERE clause placeholders (where_params)
            #   3. ORDER BY clause placeholders (order_params)
            #   4. LIMIT / OFFSET
            if query:
                select_sql = (
                    f"SELECT body, ts_rank(search_vector, plainto_tsquery('simple', %s)) "
                    f"FROM {_TABLE} WHERE {where_sql} {order_sql} LIMIT %s OFFSET %s"
                )
                all_params = [query, *where_params, *order_params, limit, offset]
            else:
                select_sql = f"SELECT body FROM {_TABLE} WHERE {where_sql} {order_sql} LIMIT %s OFFSET %s"
                all_params = [*where_params, *order_params, limit, offset]

            with connection.cursor() as cursor:
                cursor.execute(select_sql, all_params)
                rows = cursor.fetchall()
        except Exception as exc:
            raise SearchBackendError(f"Search query failed for index '{uid}'.", original_exception=exc) from exc

        hits: list[dict[str, Any]] = []
        ranking_scores: list[float | None] = []
        for row in rows:
            raw = row[0]
            hit = raw if isinstance(raw, dict) else json.loads(raw)
            hits.append(hit)
            if query:
                ranking_scores.append(float(row[1]))

        # --- Highlighting ---------------------------------------------------
        formatted_hits: list[dict[str, Any]] = []
        if highlight_fields and query and hits:
            ts_headline_options = f"StartSel={pre_tag}, StopSel={post_tag}"
            try:
                for hit in hits:
                    formatted: dict[str, Any] = dict(hit)
                    for field_name in highlight_fields:
                        field_value = hit.get(field_name)
                        if field_value is None:
                            continue
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT ts_headline('simple', %s, plainto_tsquery('simple', %s), %s)",
                                [str(field_value), query, ts_headline_options],
                            )
                            headline = cursor.fetchone()[0]
                        formatted[field_name] = headline
                    formatted_hits.append(formatted)
            except Exception as exc:
                raise SearchBackendError(f"Highlight query failed for index '{uid}'.", original_exception=exc) from exc

        # --- attributesToRetrieve filter ------------------------------------
        # Accept both camelCase (Meilisearch native) and snake_case (SearchQuery builder).
        # The primary key ("id") is always included regardless of the list.
        attributes_to_retrieve: list[str] | None = params.get(
            "attributes_to_retrieve", params.get("attributesToRetrieve")
        )
        if attributes_to_retrieve is not None:
            allowed = set(attributes_to_retrieve) | {"id"}
            hits = [{k: v for k, v in hit.items() if k in allowed} for hit in hits]
            if formatted_hits:
                formatted_hits = [{k: v for k, v in hit.items() if k in allowed} for hit in formatted_hits]

        # --- Facet distribution ---------------------------------------------
        facet_distribution: dict[str, dict[str, int]] = {}
        if facets:
            try:
                for facet_field in facets:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f"""
                            SELECT body->>%s AS facet_value, COUNT(*) AS cnt
                            FROM {_TABLE}
                            WHERE {where_sql}
                              AND body->>%s IS NOT NULL
                            GROUP BY body->>%s
                            ORDER BY cnt DESC
                            """,
                            [facet_field, *where_params, facet_field, facet_field],
                        )
                        facet_distribution[facet_field] = {row[0]: int(row[1]) for row in cursor.fetchall()}
            except Exception as exc:
                raise SearchBackendError(f"Facet query failed for index '{uid}'.", original_exception=exc) from exc

        elapsed_ms = int((time.monotonic() - start) * 1000)

        response: dict[str, Any] = {
            "hits": hits,
            "query": query,
            "processingTimeMs": elapsed_ms,
            "estimatedTotalHits": total,
            "limit": limit,
            "offset": offset,
        }
        if ranking_scores:
            response["ranking_scores"] = ranking_scores
        if formatted_hits:
            response["formatted_hits"] = formatted_hits
        if facets:
            response["facetDistribution"] = facet_distribution
        return response

    # ------------------------------------------------------------------
    # Stats and health
    # ------------------------------------------------------------------

    def get_stats(self, uid: str) -> dict[str, Any]:
        """Return document count and indexing status for a logical index."""
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(*) FROM {_TABLE} WHERE index_uid = %s",
                    [uid],
                )
                count: int = cursor.fetchone()[0]
        except Exception as exc:
            raise SearchBackendError(f"Failed to retrieve stats for index '{uid}'.", original_exception=exc) from exc

        return {
            "numberOfDocuments": count,
            "isIndexing": False,
        }

    def health(self) -> bool:
        """Check PostgreSQL connectivity. Returns ``True`` when reachable."""
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            return True
        except Exception:
            logger.warning("PostgresBackend health check failed.", exc_info=True)
            return False

    def get_task(self, task_uid: str) -> dict[str, Any]:
        """PostgreSQL backend operations are synchronous — tasks are always complete."""
        return {"uid": task_uid, "status": "succeeded"}

    # ------------------------------------------------------------------
    # Optional document retrieval methods
    # ------------------------------------------------------------------

    def get_document(self, uid: str, document_id: str) -> dict[str, Any]:
        """Fetch a single document by its primary key.

        Raises ``IndexNotFoundError`` if the index does not exist in
        ``icv_search_index_meta``.  Raises ``SearchBackendError`` if the
        document is not found.
        """
        # Verify the index exists first.
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT 1 FROM {_META_TABLE} WHERE index_uid = %s",
                    [uid],
                )
                if cursor.fetchone() is None:
                    raise IndexNotFoundError(f"Index '{uid}' not found.")
        except IndexNotFoundError:
            raise
        except Exception as exc:
            raise SearchBackendError(f"Failed to look up index '{uid}'.", original_exception=exc) from exc

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT doc_id, body FROM {_TABLE} WHERE index_uid = %s AND doc_id = %s",
                    [uid, document_id],
                )
                row = cursor.fetchone()
        except Exception as exc:
            raise SearchBackendError(
                f"Failed to fetch document '{document_id}' from index '{uid}'.",
                original_exception=exc,
            ) from exc

        if row is None:
            raise SearchBackendError(f"Document '{document_id}' not found in index '{uid}'.")

        doc_id, body = row
        raw_body: dict[str, Any] = body if isinstance(body, dict) else json.loads(body)
        return {"id": doc_id, **raw_body}

    def get_documents(
        self,
        uid: str,
        document_ids: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch multiple documents, optionally filtered by IDs.

        When ``document_ids`` is provided, fetches those specific documents
        using a single ``WHERE doc_id = ANY(%s)`` query.  When ``None``,
        returns up to ``limit`` documents from ``offset`` (browse mode).

        ``fields`` restricts the returned keys; ``id`` is always included.
        """
        try:
            if document_ids is not None:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT doc_id, body FROM {_TABLE} WHERE index_uid = %s AND doc_id = ANY(%s)",
                        [uid, document_ids],
                    )
                    rows = cursor.fetchall()
            else:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT doc_id, body FROM {_TABLE} WHERE index_uid = %s LIMIT %s OFFSET %s",
                        [uid, limit, offset],
                    )
                    rows = cursor.fetchall()
        except Exception as exc:
            raise SearchBackendError(f"Failed to fetch documents from index '{uid}'.", original_exception=exc) from exc

        results: list[dict[str, Any]] = []
        for doc_id, body in rows:
            raw_body: dict[str, Any] = body if isinstance(body, dict) else json.loads(body)
            doc = {"id": doc_id, **raw_body}
            results.append(doc)

        if fields is not None:
            keep = set(fields) | {"id"}
            results = [{k: v for k, v in doc.items() if k in keep} for doc in results]

        return results

    def update_documents(
        self,
        uid: str,
        documents: list[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Partially update document fields using JSONB merge (``body || patch``).

        For each document, only the supplied fields are written; existing fields
        not present in the update dict are preserved.  When a document does not
        yet exist it is inserted as a new document.

        Raises ``SearchBackendError`` on database failure.
        """
        if not documents:
            return {
                "taskUid": f"pg-update-{uid}",
                "indexUid": uid,
                "status": "succeeded",
            }

        try:
            with connection.cursor() as cursor:
                for doc in documents:
                    doc_id = str(doc.get(primary_key, ""))
                    patch_json = json.dumps(doc, default=str)

                    # Attempt a partial-merge update first.
                    cursor.execute(
                        f"""
                        UPDATE {_TABLE}
                           SET body       = body || %s::jsonb,
                               updated_at = NOW()
                         WHERE index_uid = %s AND doc_id = %s
                        """,
                        [patch_json, uid, doc_id],
                    )

                    if cursor.rowcount == 0:
                        # Document does not yet exist — insert it.
                        searchable_fields = self._get_searchable_fields(uid)
                        search_text = self._build_search_text(doc, searchable_fields)
                        cursor.execute(
                            f"""
                            INSERT INTO {_TABLE}
                                (index_uid, doc_id, body, search_vector, updated_at)
                            VALUES
                                (%s, %s, %s::jsonb, to_tsvector('simple', %s), NOW())
                            ON CONFLICT (index_uid, doc_id) DO UPDATE SET
                                body          = {_TABLE}.body || EXCLUDED.body,
                                search_vector = EXCLUDED.search_vector,
                                updated_at    = NOW()
                            """,
                            [uid, doc_id, patch_json, search_text],
                        )
        except Exception as exc:
            raise SearchBackendError(f"Failed to update documents in index '{uid}'.", original_exception=exc) from exc

        return {
            "taskUid": f"pg-update-{uid}",
            "indexUid": uid,
            "status": "succeeded",
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_searchable_fields(self, uid: str) -> list[str]:
        """Return ``searchableAttributes`` from index settings, or ``[]``."""
        try:
            settings = self.get_settings(uid)
            return settings.get("searchableAttributes", [])
        except (IndexNotFoundError, SearchBackendError):
            return []

    def _build_search_text(self, doc: dict[str, Any], searchable_fields: list[str]) -> str:
        """Produce a plain-text string for ``to_tsvector`` ingestion.

        When ``searchable_fields`` is non-empty only those keys are included.
        Otherwise all string-valued leaf fields in the document contribute.
        """
        parts: list[str] = []
        if searchable_fields:
            for field in searchable_fields:
                value = doc.get(field)
                if value is not None:
                    parts.append(str(value))
        else:
            for value in doc.values():
                if isinstance(value, str):
                    parts.append(value)
        return " ".join(parts)
