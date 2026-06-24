"""Meilisearch backend using httpx."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

import httpx

from icv_search.backends.base import BaseSearchBackend
from icv_search.backends.filters import translate_filter_to_meilisearch, translate_sort_to_meilisearch
from icv_search.exceptions import IndexNotFoundError, SearchBackendError, SearchTimeoutError

logger = logging.getLogger(__name__)


class MeilisearchBackend(BaseSearchBackend):
    """Search backend for Meilisearch using httpx.

    Uses httpx directly rather than the official meilisearch Python SDK
    for lighter dependencies and future async support.
    """

    def __init__(self, url: str, api_key: str, timeout: int = 30, **kwargs: Any) -> None:
        super().__init__(url=url.rstrip("/"), api_key=api_key, timeout=timeout, **kwargs)
        self._client = httpx.Client(
            base_url=self.url,
            headers=self._build_headers(),
            timeout=self.timeout,
        )

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Execute an HTTP request with error handling."""
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise SearchTimeoutError(f"Meilisearch request timed out: {method} {path}", exc) from exc
        except httpx.HTTPError as exc:
            raise SearchBackendError(f"Meilisearch request failed: {method} {path} — {exc}", exc) from exc

        if response.status_code == 404:
            raise IndexNotFoundError(f"Index not found: {path}")
        if response.status_code >= 400:
            detail = response.text
            raise SearchBackendError(f"Meilisearch error {response.status_code}: {detail}")

        if response.status_code == 204:
            return {}
        return response.json()

    def create_index(self, uid: str, primary_key: str = "id") -> dict[str, Any]:
        return self._request("POST", "/indexes", json={"uid": uid, "primaryKey": primary_key})

    def delete_index(self, uid: str) -> None:
        self._request("DELETE", f"/indexes/{uid}")

    def update_settings(self, uid: str, settings: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/indexes/{uid}/settings", json=settings)

    def get_settings(self, uid: str) -> dict[str, Any]:
        return self._request("GET", f"/indexes/{uid}/settings")

    def add_documents(self, uid: str, documents: list[dict[str, Any]], primary_key: str = "id") -> dict[str, Any]:
        return self._request(
            "POST",
            f"/indexes/{uid}/documents",
            json=documents,
            params={"primaryKey": primary_key},
        )

    def add_documents_ndjson(
        self,
        uid: str,
        documents: Iterable[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Add or update documents using NDJSON content type.

        Serialises each document as a single JSON line and sends with
        ``Content-Type: application/x-ndjson``.  Avoids building a large
        JSON array in memory.
        """
        body = b"".join(json.dumps(doc, separators=(",", ":")).encode() + b"\n" for doc in documents)
        try:
            response = self._client.request(
                "POST",
                f"/indexes/{uid}/documents",
                content=body,
                params={"primaryKey": primary_key},
                headers={"Content-Type": "application/x-ndjson"},
            )
        except httpx.TimeoutException as exc:
            raise SearchTimeoutError(
                f"Meilisearch NDJSON request timed out: POST /indexes/{uid}/documents", exc
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchBackendError(
                f"Meilisearch NDJSON request failed: POST /indexes/{uid}/documents — {exc}", exc
            ) from exc

        if response.status_code == 404:
            raise IndexNotFoundError(f"Index not found: /indexes/{uid}/documents")
        if response.status_code >= 400:
            raise SearchBackendError(f"Meilisearch error {response.status_code}: {response.text}")
        if response.status_code == 204:
            return {}
        return response.json()

    def delete_documents(self, uid: str, document_ids: list[str]) -> dict[str, Any]:
        return self._request("POST", f"/indexes/{uid}/documents/delete-batch", json=document_ids)

    def clear_documents(self, uid: str) -> dict[str, Any]:
        """Remove all documents using Meilisearch's DELETE /indexes/{uid}/documents."""
        return self._request("DELETE", f"/indexes/{uid}/documents")

    def delete_documents_by_filter(self, uid: str, filter_expr: Any) -> dict[str, Any]:
        """Remove documents matching a filter expression.

        Uses ``POST /indexes/{uid}/documents/delete`` with a JSON body
        containing the filter.  Requires Meilisearch v1.2+.

        Accepts either an engine-native filter string or a Django-native
        filter dict (translated automatically), matching :meth:`search`.
        """
        if isinstance(filter_expr, dict):
            filter_expr = translate_filter_to_meilisearch(filter_expr)
        return self._request(
            "POST",
            f"/indexes/{uid}/documents/delete",
            json={"filter": filter_expr},
        )

    def search(self, uid: str, query: str, **params: Any) -> dict[str, Any]:
        """Execute a search query against a Meilisearch index.

        Recognised params beyond the standard Meilisearch options:

        - ``highlight_fields`` (list[str]): Fields to highlight in results.
          Maps to ``attributesToHighlight``.
        - ``highlight_pre_tag`` (str): Opening tag for highlighted terms.
          Defaults to ``<mark>``.
        - ``highlight_post_tag`` (str): Closing tag for highlighted terms.
          Defaults to ``</mark>``.
        - ``crop_fields`` (list[str]): Fields to crop/snippet. Maps to
          ``attributesToCrop``.
        - ``crop_length`` (int): Words per cropped excerpt. Maps to
          ``cropLength``.
        - ``crop_marker`` (str): Boundary marker for crops. Maps to
          ``cropMarker``.
        - ``show_ranking_score`` (bool): When ``True``, include each hit's
          relevance score as ``_rankingScore`` (Meilisearch v1.3+).  Maps to
          ``showRankingScore``.
        - ``show_ranking_score_details`` (bool): When ``True``, include
          per-rule score breakdown as ``_rankingScoreDetails``.  Maps to
          ``showRankingScoreDetails``.
        - ``show_matches_position`` (bool): When ``True``, return byte
          offsets of matched terms. Maps to ``showMatchesPosition``.
        - ``matching_strategy`` (str): Controls how query terms are matched.
          One of ``"all"``, ``"last"``, or ``"frequency"``.  Maps to
          Meilisearch's ``matchingStrategy`` parameter.
        - ``attributes_to_retrieve`` (list[str]): Restrict returned fields.
          Maps to ``attributesToRetrieve``.
        - ``attributes_to_search_on`` (list[str]): Restrict search scope at
          query time. Maps to ``attributesToSearchOn``.
        - ``ranking_score_threshold`` (float): Exclude results below this
          score (0–1). Maps to ``rankingScoreThreshold``.
        - ``distinct`` (str): Query-time deduplication field. Maps to
          ``distinct``.
        - ``hybrid`` (dict): Hybrid/semantic search options.  Pass
          ``{"semanticRatio": 0.5, "embedder": "default"}`` to blend
          keyword and vector results. Maps to ``hybrid``.
        - ``vector`` (list[float]): Raw float array as query vector. Maps
          to ``vector``.
        - ``retrieve_vectors`` (bool): Return ``_vectors`` on each hit.
          Maps to ``retrieveVectors``.
        - ``page`` (int): Page number (1-indexed, use with ``hits_per_page``).
          Maps to ``page``.
        - ``hits_per_page`` (int): Documents per page. Maps to
          ``hitsPerPage``.
        - ``locales`` (list[str]): ISO-639 language codes for query. Maps
          to ``locales``.
        - ``geo_point`` (tuple[float, float]): ``(lat, lng)`` origin for
          geo-distance filtering and sorting.  Must be accompanied by
          ``geo_radius``, ``geo_bbox``, ``geo_polygon``, and/or ``geo_sort``.
        - ``geo_radius`` (int | None): Radius in metres.  When combined with
          ``geo_point`` a ``_geoRadius`` filter is appended to the existing
          filter expression.
        - ``geo_bbox`` (tuple[tuple[float,float],tuple[float,float]]): Bounding
          box as ``((top_right_lat, top_right_lng), (bottom_left_lat, bottom_left_lng))``.
          Appends a ``_geoBoundingBox`` filter.
        - ``geo_polygon`` (list[tuple[float, float]]): Polygon vertices as
          ``[(lat, lng), ...]``.  Appends a ``_geoPolygon`` filter.
        - ``geo_sort`` (str): ``"asc"`` or ``"desc"``.  When combined with
          ``geo_point`` a ``_geoPoint`` sort expression is prepended to the
          sort list so that results are ordered by distance from the point.
          Documents must have a ``_geo`` field (``{"lat": ..., "lng": ...}``)
          for Meilisearch to evaluate geo filters and sorts.
        """
        # Extract geo params before any other processing.
        geo_point: tuple[float, float] | None = params.pop("geo_point", None)
        geo_radius: int | None = params.pop("geo_radius", None)
        geo_bbox: tuple | None = params.pop("geo_bbox", None)
        geo_polygon: list | None = params.pop("geo_polygon", None)
        geo_sort: str | None = params.pop("geo_sort", None)

        # Translate Django-native filter dict to Meilisearch filter string
        if "filter" in params and isinstance(params["filter"], dict):
            params = {**params, "filter": translate_filter_to_meilisearch(params["filter"])}

        # Translate Django-native sort list (with - prefix) to Meilisearch sort format
        if "sort" in params and isinstance(params["sort"], list):
            params = {**params, "sort": translate_sort_to_meilisearch(params["sort"])}

        # Collect geo filter expressions.
        geo_filters: list[str] = []

        if geo_point is not None and geo_radius is not None:
            lat, lng = geo_point
            geo_filters.append(f"_geoRadius({lat}, {lng}, {geo_radius})")

        if geo_bbox is not None:
            (tr_lat, tr_lng), (bl_lat, bl_lng) = geo_bbox
            geo_filters.append(f"_geoBoundingBox([{tr_lat}, {tr_lng}], [{bl_lat}, {bl_lng}])")

        if geo_polygon is not None:
            vertices = ", ".join(f"[{lat}, {lng}]" for lat, lng in geo_polygon)
            geo_filters.append(f"_geoPolygon({vertices})")

        if geo_filters:
            existing_filter: str = params.pop("filter", "") or ""
            combined_geo = " AND ".join(geo_filters)
            if existing_filter:
                params["filter"] = f"{existing_filter} AND {combined_geo}"
            else:
                params["filter"] = combined_geo

        # Prepend geo distance sort when both geo_point and geo_sort are given.
        if geo_point is not None and geo_sort in ("asc", "desc"):
            lat, lng = geo_point
            geo_sort_expr = f"_geoPoint({lat}, {lng}):{geo_sort}"
            existing_sort: list[str] = params.pop("sort", []) or []
            params["sort"] = [geo_sort_expr, *existing_sort]

        # Extract highlighting params and map to Meilisearch naming convention.
        highlight_fields: list[str] | None = params.pop("highlight_fields", None)
        pre_tag: str = params.pop("highlight_pre_tag", "<mark>")
        post_tag: str = params.pop("highlight_post_tag", "</mark>")

        # Extract crop params.
        crop_fields: list[str] | None = params.pop("crop_fields", None)
        crop_length: int | None = params.pop("crop_length", None)
        crop_marker: str | None = params.pop("crop_marker", None)

        # Extract ranking score params.
        show_ranking_score: bool = params.pop("show_ranking_score", False)
        show_ranking_score_details: bool = params.pop("show_ranking_score_details", False)
        show_matches_position: bool = params.pop("show_matches_position", False)

        # Extract matching strategy.
        matching_strategy: str | None = params.pop("matching_strategy", None)

        # Extract field restriction params.
        attributes_to_retrieve: list[str] | None = params.pop("attributes_to_retrieve", None)
        attributes_to_search_on: list[str] | None = params.pop("attributes_to_search_on", None)

        # Extract score threshold.
        ranking_score_threshold: float | None = params.pop("ranking_score_threshold", None)

        # Extract query-time distinct.
        distinct_field: str | None = params.pop("distinct", None)

        # Extract hybrid/semantic search params.
        hybrid: dict[str, Any] | None = params.pop("hybrid", None)
        vector: list[float] | None = params.pop("vector", None)
        retrieve_vectors: bool = params.pop("retrieve_vectors", False)

        # Extract page-based pagination params.
        page: int | None = params.pop("page", None)
        hits_per_page: int | None = params.pop("hits_per_page", None)

        # Extract locale params.
        locales: list[str] | None = params.pop("locales", None)

        body: dict[str, Any] = {"q": query, **params}

        if highlight_fields:
            body["attributesToHighlight"] = highlight_fields
            body["highlightPreTag"] = pre_tag
            body["highlightPostTag"] = post_tag

        if crop_fields:
            body["attributesToCrop"] = crop_fields
            if crop_length is not None:
                body["cropLength"] = crop_length
            if crop_marker is not None:
                body["cropMarker"] = crop_marker

        if show_ranking_score:
            body["showRankingScore"] = True

        if show_ranking_score_details:
            body["showRankingScoreDetails"] = True

        if show_matches_position:
            body["showMatchesPosition"] = True

        if matching_strategy is not None:
            body["matchingStrategy"] = matching_strategy

        if attributes_to_retrieve is not None:
            body["attributesToRetrieve"] = attributes_to_retrieve

        if attributes_to_search_on is not None:
            body["attributesToSearchOn"] = attributes_to_search_on

        if ranking_score_threshold is not None:
            body["rankingScoreThreshold"] = ranking_score_threshold

        if distinct_field is not None:
            body["distinct"] = distinct_field

        if hybrid is not None:
            body["hybrid"] = hybrid

        if vector is not None:
            body["vector"] = vector

        if retrieve_vectors:
            body["retrieveVectors"] = True

        if page is not None:
            body["page"] = page

        if hits_per_page is not None:
            body["hitsPerPage"] = hits_per_page

        if locales is not None:
            body["locales"] = locales

        return self._request("POST", f"/indexes/{uid}/search", json=body)

    def get_stats(self, uid: str) -> dict[str, Any]:
        return self._request("GET", f"/indexes/{uid}/stats")

    def health(self) -> bool:
        try:
            result = self._request("GET", "/health")
            return result.get("status") == "available"
        except (SearchBackendError, SearchTimeoutError):
            return False

    def get_task(self, task_uid: str) -> dict[str, Any]:
        return self._request("GET", f"/tasks/{task_uid}")

    def multi_search(self, queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Execute multiple search queries via Meilisearch's POST /multi-search.

        Each query dict must contain ``uid`` (the Meilisearch index UID) and
        ``query`` (the search string). All other keys are forwarded as
        Meilisearch search parameters — filter/sort translation and
        highlight_fields mapping are applied automatically.

        Returns:
            A list of raw Meilisearch search result dicts in query order.
        """
        searches: list[dict[str, Any]] = []
        for query in queries:
            uid = query["uid"]
            q = query.get("query", "")
            params = {k: v for k, v in query.items() if k not in ("uid", "query")}

            # Apply the same filter / sort translation as single search
            if "filter" in params and isinstance(params["filter"], dict):
                params = {**params, "filter": translate_filter_to_meilisearch(params["filter"])}
            if "sort" in params and isinstance(params["sort"], list):
                params = {**params, "sort": translate_sort_to_meilisearch(params["sort"])}

            # Map highlight_fields to attributesToHighlight
            highlight_fields: list[str] | None = params.pop("highlight_fields", None)
            pre_tag: str = params.pop("highlight_pre_tag", "<mark>")
            post_tag: str = params.pop("highlight_post_tag", "</mark>")

            entry: dict[str, Any] = {"indexUid": uid, "q": q, **params}
            if highlight_fields:
                entry["attributesToHighlight"] = highlight_fields
                entry["highlightPreTag"] = pre_tag
                entry["highlightPostTag"] = post_tag

            searches.append(entry)

        response = self._request("POST", "/multi-search", json={"queries": searches})
        return response.get("results", [])

    def swap_indexes(self, pairs: list[tuple[str, str]]) -> dict[str, Any]:
        """Atomically swap index names using Meilisearch's swap-indexes endpoint."""
        payload = [{"indexes": [a, b]} for a, b in pairs]
        return self._request("POST", "/swap-indexes", json=payload)

    def get_document(self, uid: str, document_id: str) -> dict[str, Any]:
        """Fetch a single document by primary key.

        Uses ``GET /indexes/{uid}/documents/{id}``. Meilisearch includes ``id``
        in the returned document dict directly.
        """
        return self._request("GET", f"/indexes/{uid}/documents/{document_id}")

    def get_documents(
        self,
        uid: str,
        document_ids: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch multiple documents, optionally filtered to specific IDs.

        When ``document_ids`` is provided, uses ``POST /indexes/{uid}/documents/fetch``
        (Meilisearch batch fetch endpoint). When ``document_ids`` is ``None``,
        browses the index via ``GET /indexes/{uid}/documents``.

        ``id`` is always included in returned fields (BR-010).
        """
        # Ensure id is always present in the fields list.
        effective_fields: list[str] | None = None
        if fields is not None:
            effective_fields = fields if "id" in fields else ["id", *fields]

        if document_ids is not None:
            body: dict[str, Any] = {"ids": document_ids}
            if effective_fields is not None:
                body["fields"] = effective_fields
            response = self._request("POST", f"/indexes/{uid}/documents/fetch", json=body)
            return response.get("results", [])

        # Browse mode — GET with pagination params.
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if effective_fields is not None:
            params["fields"] = ",".join(effective_fields)
        response = self._request("GET", f"/indexes/{uid}/documents", params=params)
        return response.get("results", [])

    def facet_search(
        self,
        uid: str,
        facet_name: str,
        facet_query: str = "",
        **params: Any,
    ) -> list[dict[str, Any]]:
        """Search within facet values for typeahead filter UIs.

        Uses ``POST /indexes/{uid}/facet-search``. Meilisearch returns
        ``{"facetHits": [{"value": "...", "count": N}, ...]}``. This method
        normalises that to ``[{"value": str, "count": int}]`` sorted by count
        descending (BR-014).
        """
        body: dict[str, Any] = {"facetName": facet_name, "facetQuery": facet_query, **params}
        response = self._request("POST", f"/indexes/{uid}/facet-search", json=body)
        results = [{"value": str(hit["value"]), "count": int(hit["count"])} for hit in response.get("facetHits", [])]
        return sorted(results, key=lambda x: x["count"], reverse=True)

    def similar_documents(
        self,
        uid: str,
        document_id: str,
        **params: Any,
    ) -> dict[str, Any]:
        """Find documents similar to a given document.

        Uses ``POST /indexes/{uid}/similar``. Returns the raw search result dict
        in the same format as ``search()``.

        Requires embedders to be configured on the index in Meilisearch.
        """
        body: dict[str, Any] = {"id": document_id, **params}
        return self._request("POST", f"/indexes/{uid}/similar", json=body)

    def compact(self, uid: str) -> dict[str, Any]:
        """No-op for Meilisearch — compaction is managed internally by the engine.

        Returns ``{}`` (BR-016).
        """
        return {}

    def update_documents(
        self,
        uid: str,
        documents: list[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Add or update documents.

        Meilisearch's ``add_documents`` endpoint is a full-document upsert;
        there is no native partial-update endpoint. This method delegates to
        ``add_documents()`` directly (BR-015).
        """
        return self.add_documents(uid, documents, primary_key)
