"""OpenSearch backend using the opensearch-py SDK."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlparse

try:
    import opensearchpy
    import opensearchpy.helpers
    from opensearchpy import OpenSearch, RequestsHttpConnection

    _HAS_OPENSEARCH = True
except ImportError:  # pragma: no cover
    _HAS_OPENSEARCH = False

from icv_search.backends.base import BaseSearchBackend
from icv_search.exceptions import IndexNotFoundError, SearchBackendError, SearchTimeoutError

logger = logging.getLogger(__name__)

# Django-style range lookup suffixes.
_RANGE_OPERATORS: dict[str, str] = {
    "__gte": "gte",
    "__gt": "gt",
    "__lte": "lte",
    "__lt": "lt",
}


def translate_filter_to_opensearch(
    filters: dict[str, Any] | str,
    dual_mapped_fields: set[str] | None = None,
) -> dict[str, Any]:
    """Convert a Django-native filter dict to an OpenSearch bool query.

    Passes through a pre-built OpenSearch ``bool`` dict unchanged.

    Args:
        filters: Either a dict of field:value pairs or a pre-built OpenSearch
            bool query dict.
        dual_mapped_fields: Fields that have both ``text`` and ``keyword``
            sub-fields (i.e. present in both ``searchableAttributes`` and
            ``filterableAttributes``). ``term``/``terms`` clauses for these
            fields target the ``.keyword`` sub-field.

    Returns:
        An OpenSearch bool query fragment suitable for embedding in a
        ``query.bool.filter`` array or as a standalone query.
    """
    if isinstance(filters, dict) and "bool" in filters:
        # Already an OpenSearch query dict — pass through unchanged.
        return filters

    if not isinstance(filters, dict) or not filters:
        return {}

    dual = dual_mapped_fields or set()
    clauses: list[dict[str, Any]] = []

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
            clauses.append({"range": {real_field: {range_op: value}}})

        elif value is None:
            clauses.append({"bool": {"must_not": [{"exists": {"field": field}}]}})

        elif isinstance(value, list):
            target_field = f"{field}.keyword" if field in dual else field
            clauses.append({"terms": {target_field: value}})

        else:
            # Scalar value — term query.
            target_field = f"{field}.keyword" if field in dual else field
            clauses.append({"term": {target_field: value}})

    if len(clauses) == 1:
        return clauses[0]

    return {"bool": {"filter": clauses}}


def translate_sort_to_opensearch(sort: list[str] | str) -> list[dict[str, Any]]:
    """Convert a Django-native sort list to an OpenSearch sort array.

    Fields already in OpenSearch dict format pass through unchanged.
    ``missing`` is always set to ``"_last"`` to match the Meilisearch and
    PostgreSQL backend behaviour.

    Args:
        sort: Either a list of field names (with optional ``-`` prefix for
            descending) or a single field name string.

    Returns:
        A list of OpenSearch sort dicts.
    """
    if isinstance(sort, str):
        sort = [sort] if sort else []

    if not isinstance(sort, list) or not sort:
        return []

    result: list[dict[str, Any]] = []
    for field in sort:
        if isinstance(field, dict):
            # Already an OpenSearch sort dict — pass through.
            result.append(field)
        elif field.startswith("-"):
            result.append({field[1:]: {"order": "desc", "missing": "_last"}})
        else:
            result.append({field: {"order": "asc", "missing": "_last"}})

    return result


class OpenSearchBackend(BaseSearchBackend):
    """Search backend for OpenSearch using the opensearch-py SDK.

    Supports self-managed OpenSearch clusters, AWS OpenSearch Service, and
    any OpenSearch-compatible deployment.

    Install the optional extra before use::

        pip install django-icv-search[opensearch]

    Configure via settings::

        ICV_SEARCH_BACKEND = "icv_search.backends.opensearch.OpenSearchBackend"
        ICV_SEARCH_URL     = "https://opensearch.internal:9200"
        ICV_SEARCH_API_KEY = ""

    For Basic auth or AWS auth, pass kwargs via ``ICV_SEARCH_BACKEND_OPTIONS``.
    """

    def __init__(self, url: str, api_key: str, timeout: int = 30, **kwargs: Any) -> None:
        if not _HAS_OPENSEARCH:
            from django.core.exceptions import ImproperlyConfigured

            raise ImproperlyConfigured(
                "opensearch-py is required to use OpenSearchBackend. "
                "Install it with: pip install django-icv-search[opensearch]"
            )

        super().__init__(url=url, api_key=api_key, timeout=timeout, **kwargs)

        verify_certs: bool = kwargs.get("verify_certs", True)
        basic_auth: tuple[str, str] | None = kwargs.get("basic_auth")
        aws_region: str | None = kwargs.get("aws_region")
        connection_class = kwargs.get("connection_class", RequestsHttpConnection)

        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 9200)
        use_ssl: bool = kwargs.get("use_ssl", parsed.scheme == "https")

        # Determine http_auth — AWS auth takes precedence, then basic_auth, then api_key.
        http_auth: Any = None
        if aws_region:
            import boto3
            from opensearchpy import AWSV4SignerAuth

            credentials = boto3.Session().get_credentials()
            http_auth = AWSV4SignerAuth(credentials, aws_region)
        elif basic_auth is not None:
            http_auth = basic_auth
        elif api_key:
            http_auth = ("", api_key)

        self._client: OpenSearch = OpenSearch(
            hosts=[{"host": host, "port": port}],
            http_auth=http_auth,
            use_ssl=use_ssl,
            verify_certs=verify_certs,
            timeout=timeout,
            connection_class=connection_class,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Wrap an opensearch-py call with icv-search exception mapping.

        Catch order matters — ``ConnectionTimeout`` extends ``ConnectionError``
        extends ``TransportError``, so most-specific must come first.
        """
        try:
            return fn(*args, **kwargs)
        except opensearchpy.ConnectionTimeout as exc:
            raise SearchTimeoutError(f"OpenSearch request timed out: {exc}") from exc
        except opensearchpy.ConnectionError as exc:
            raise SearchBackendError(f"OpenSearch unreachable: {exc}") from exc
        except opensearchpy.TransportError as exc:
            status = getattr(exc, "status_code", None)
            try:
                info = str(exc.info) if len(exc.args) > 2 else ""
            except (IndexError, AttributeError):
                info = ""
            if status == 404 and "index_not_found_exception" in info:
                raise IndexNotFoundError(str(exc)) from exc
            error = getattr(exc, "error", str(exc))
            raise SearchBackendError(f"OpenSearch error {status}: {error}") from exc

    # ------------------------------------------------------------------
    # Abstract methods (10)
    # ------------------------------------------------------------------

    def create_index(self, uid: str, primary_key: str = "id") -> dict[str, Any]:
        body = {
            "mappings": {
                "_meta": {"primary_key": primary_key},
                "_source": {"enabled": True},
            }
        }
        return self._call(self._client.indices.create, index=uid, body=body)

    def delete_index(self, uid: str) -> None:
        self._call(self._client.indices.delete, index=uid)

    def update_settings(self, uid: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Translate icv-search canonical settings and push to OpenSearch.

        Handles:
        - ``searchableAttributes`` → ``_mapping`` text fields
        - ``filterableAttributes`` → ``_mapping`` keyword fields
        - Fields in both → text + keyword sub-field
        - ``sortableAttributes`` → ``_mapping`` with doc_values / fielddata
        - ``synonyms`` → ``_settings`` analysis (requires close/open cycle)
        - ``stopWords`` → ``_settings`` analysis (requires close/open cycle)
        """
        searchable = set(settings.get("searchableAttributes", []))
        filterable = set(settings.get("filterableAttributes", []))
        sortable = set(settings.get("sortableAttributes", []))
        synonyms = settings.get("synonyms", [])
        stop_words = settings.get("stopWords", [])

        # Build mapping properties.
        properties: dict[str, Any] = {}
        all_fields = searchable | filterable | sortable

        for field in all_fields:
            in_search = field in searchable
            in_filter = field in filterable
            in_sort = field in sortable

            if in_search and in_filter:
                # Dual-mapped: text with keyword sub-field.
                field_def: dict[str, Any] = {
                    "type": "text",
                    "analyzer": "standard",
                    "fields": {"keyword": {"type": "keyword"}},
                }
            elif in_search:
                field_def = {"type": "text", "analyzer": "standard"}
            elif in_filter:
                field_def = {"type": "keyword"}
            else:
                # sortable-only — keyword for doc_values.
                field_def = {"type": "keyword"}

            if in_sort and field_def.get("type") == "text" and "fields" not in field_def:
                # Pure text sort field needs fielddata.
                field_def["fielddata"] = True

            properties[field] = field_def

        result: dict[str, Any] = {}

        if properties:
            mapping_body = {"properties": properties}
            result["mapping"] = self._call(
                self._client.indices.put_mapping,
                index=uid,
                body=mapping_body,
            )

        # Analysis settings require close/open cycle.
        analysis_filters: dict[str, Any] = {}

        if synonyms:
            synonym_strings = [", ".join(group) for group in synonyms]
            analysis_filters["icv_search_synonyms"] = {
                "type": "synonym",
                "synonyms": synonym_strings,
            }

        if stop_words:
            analysis_filters["icv_search_stop_words"] = {
                "type": "stop",
                "stopwords": stop_words,
            }

        if analysis_filters:
            self._call(self._client.indices.close, index=uid)
            try:
                analysis_settings: dict[str, Any] = {"index": {"analysis": {"filter": analysis_filters}}}
                result["settings"] = self._call(
                    self._client.indices.put_settings,
                    index=uid,
                    body=analysis_settings,
                )
            finally:
                self._call(self._client.indices.open, index=uid)

        return result

    def get_settings(self, uid: str) -> dict[str, Any]:
        """Retrieve index settings from OpenSearch and translate to icv-search format."""
        mapping_resp = self._call(self._client.indices.get_mapping, index=uid)
        settings_resp = self._call(self._client.indices.get_settings, index=uid)

        # Extract properties from mapping response.
        index_mapping = mapping_resp.get(uid, mapping_resp)
        properties: dict[str, Any] = index_mapping.get("mappings", {}).get("properties", {})

        searchable: list[str] = []
        filterable: list[str] = []

        for field, defn in properties.items():
            field_type = defn.get("type")
            sub_fields = defn.get("fields", {})

            if field_type == "text":
                searchable.append(field)
                if "keyword" in sub_fields:
                    filterable.append(field)
            elif field_type == "keyword":
                filterable.append(field)

        result: dict[str, Any] = {
            "searchableAttributes": searchable,
            "filterableAttributes": filterable,
        }

        # Extract analysis settings if present.
        index_settings = settings_resp.get(uid, settings_resp)
        analysis = index_settings.get("settings", {}).get("index", {}).get("analysis", {}).get("filter", {})

        if "icv_search_synonyms" in analysis:
            raw_synonyms = analysis["icv_search_synonyms"].get("synonyms", [])
            result["synonyms"] = [s.split(", ") for s in raw_synonyms]

        if "icv_search_stop_words" in analysis:
            result["stopWords"] = analysis["icv_search_stop_words"].get("stopwords", [])

        return result

    def add_documents(
        self,
        uid: str,
        documents: list[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Add or update documents using the opensearch-py bulk helper."""
        actions = [
            {
                "_op_type": "index",
                "_index": uid,
                "_id": str(doc.get(primary_key, "")),
                "_source": doc,
            }
            for doc in documents
        ]
        success, errors = self._call(
            opensearchpy.helpers.bulk,
            self._client,
            actions,
            raise_on_error=False,
        )
        return {"succeeded": success, "failed": len(errors), "errors": errors}

    def delete_documents(self, uid: str, document_ids: list[str]) -> dict[str, Any]:
        """Remove documents by ID using the opensearch-py bulk helper."""
        actions = [{"_op_type": "delete", "_index": uid, "_id": str(doc_id)} for doc_id in document_ids]
        success, errors = self._call(
            opensearchpy.helpers.bulk,
            self._client,
            actions,
            raise_on_error=False,
        )
        return {"succeeded": success, "failed": len(errors), "errors": errors}

    def clear_documents(self, uid: str) -> dict[str, Any]:
        """Remove all documents without deleting the index.

        Uses ``delete_by_query`` with ``wait_for_completion=False`` and returns
        the task ID.
        """
        return self._call(
            self._client.delete_by_query,
            index=uid,
            body={"query": {"match_all": {}}},
            wait_for_completion=False,
        )

    def search(self, uid: str, query: str, **params: Any) -> dict[str, Any]:
        """Execute a search query against an OpenSearch index.

        Recognised params:

        - ``filter`` (dict): Django-native filter dict.
        - ``sort`` (list[str]): Sort fields with optional ``-`` prefix.
        - ``limit`` (int): Maximum hits to return. Default 20.
        - ``offset`` (int): Number of hits to skip. Default 0.
        - ``facets`` (list[str]): Fields to aggregate for facet counts.
        - ``highlight_fields`` (list[str]): Fields to highlight.
        - ``highlight_pre_tag`` (str): Opening highlight tag. Default ``<mark>``.
        - ``highlight_post_tag`` (str): Closing highlight tag. Default ``</mark>``.
        - ``attributesToRetrieve`` (list[str]): Fields to include in ``_source``.
        - ``show_ranking_score`` (bool): Include ``_score`` in all hits.
        - ``geo_point`` (tuple[float, float]): ``(lat, lng)`` origin.
        - ``geo_radius`` (int): Radius in metres for geo filter.
        - ``geo_sort`` (str): ``"asc"`` or ``"desc"`` for geo sort.
        - ``geo_field`` (str): Geo field name. Default ``"location"``.
        """
        limit: int = params.pop("limit", 20)
        offset: int = params.pop("offset", 0)

        # Build the query.
        if query:
            searchable_fields: list[str] = params.pop("searchableAttributes", ["*"])
            query_clause: dict[str, Any] = {"multi_match": {"query": query, "fields": searchable_fields}}
            must_clauses: list[dict[str, Any]] = [query_clause]
        else:
            params.pop("searchableAttributes", None)
            must_clauses = []

        # Filter translation.
        filter_param = params.pop("filter", None)
        filter_clauses: list[dict[str, Any]] = []
        if filter_param:
            if isinstance(filter_param, dict):
                translated = translate_filter_to_opensearch(filter_param)
                if translated:
                    # Unwrap single-clause results or use filter list directly.
                    if "bool" in translated and "filter" in translated["bool"]:
                        filter_clauses.extend(translated["bool"]["filter"])
                    else:
                        filter_clauses.append(translated)
            elif isinstance(filter_param, list):
                filter_clauses.extend(filter_param)

        # Geo search.
        geo_point: tuple[float, float] | None = params.pop("geo_point", None)
        geo_radius: int | None = params.pop("geo_radius", None)
        geo_sort: str | None = params.pop("geo_sort", None)
        geo_field: str = params.pop("geo_field", "location")

        if geo_point is not None and geo_radius is not None:
            lat, lng = geo_point
            filter_clauses.append(
                {
                    "geo_distance": {
                        "distance": f"{geo_radius}m",
                        geo_field: {"lat": lat, "lon": lng},
                    }
                }
            )

        # Compose bool query.
        bool_body: dict[str, Any] = {}
        if must_clauses:
            bool_body["must"] = must_clauses
        if filter_clauses:
            bool_body["filter"] = filter_clauses

        if bool_body:
            body_query: dict[str, Any] = {"bool": bool_body}
        else:
            body_query = {"match_all": {}}

        body: dict[str, Any] = {"query": body_query, "size": limit, "from": offset}

        # Sort.
        sort_param = params.pop("sort", None)
        sort_list: list[dict[str, Any]] = []

        if geo_point is not None and geo_sort in ("asc", "desc"):
            lat, lng = geo_point
            sort_list.append(
                {
                    "_geo_distance": {
                        geo_field: {"lat": lat, "lon": lng},
                        "order": geo_sort,
                        "unit": "m",
                        "distance_type": "arc",
                    }
                }
            )

        if sort_param:
            sort_list.extend(translate_sort_to_opensearch(sort_param))

        if sort_list:
            body["sort"] = sort_list

        # Facets / aggregations.
        facets: list[str] | None = params.pop("facets", None)
        if facets:
            body["aggs"] = {field: {"terms": {"field": field, "size": 100}} for field in facets}

        # Highlighting.
        highlight_fields: list[str] | None = params.pop("highlight_fields", None)
        pre_tag: str = params.pop("highlight_pre_tag", "<mark>")
        post_tag: str = params.pop("highlight_post_tag", "</mark>")
        if highlight_fields:
            body["highlight"] = {
                "pre_tags": [pre_tag],
                "post_tags": [post_tag],
                "fields": {field: {} for field in highlight_fields},
            }

        # Source filtering.
        attributes_to_retrieve: list[str] | None = params.pop(
            "attributes_to_retrieve", params.pop("attributesToRetrieve", None)
        )
        if attributes_to_retrieve:
            includes = list(attributes_to_retrieve)
            if "id" not in includes:
                includes.append("id")
            body["_source"] = {"includes": includes}

        # Ranking score.
        show_ranking_score: bool = params.pop("show_ranking_score", False)
        if show_ranking_score:
            body["track_scores"] = True

        response = self._call(self._client.search, index=uid, body=body)
        return self._normalise_search_response(response, query, limit, offset, highlight_fields)

    def _normalise_search_response(
        self,
        response: dict[str, Any],
        query: str,
        limit: int,
        offset: int,
        highlight_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Translate a raw OpenSearch response to icv-search canonical format."""
        raw_hits: list[dict[str, Any]] = response.get("hits", {}).get("hits", [])
        total: int = response.get("hits", {}).get("total", {}).get("value", 0)
        took: int = response.get("took", 0)

        hits: list[dict[str, Any]] = []
        formatted_hits: list[dict[str, Any]] = []

        for hit in raw_hits:
            source = dict(hit.get("_source", {}))
            source["id"] = hit["_id"]
            hits.append(source)

            if highlight_fields is not None:
                raw_highlight = hit.get("highlight", {})
                formatted: dict[str, Any] = {
                    k: v[0] if isinstance(v, list) and v else v for k, v in raw_highlight.items()
                }
                formatted_hits.append(formatted)

        aggregations = response.get("aggregations", {})
        facet_distribution: dict[str, dict[str, int]] = {
            agg_name: {bucket["key"]: bucket["doc_count"] for bucket in agg_data.get("buckets", [])}
            for agg_name, agg_data in aggregations.items()
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

    def get_stats(self, uid: str) -> dict[str, Any]:
        return self._call(self._client.indices.stats, index=uid)

    def health(self) -> bool:
        try:
            result = self._call(self._client.cluster.health)
            return result.get("status") in ("green", "yellow")
        except (SearchBackendError, SearchTimeoutError):
            return False

    # ------------------------------------------------------------------
    # Optional method overrides
    # ------------------------------------------------------------------

    def get_task(self, task_uid: str) -> dict[str, Any]:
        return self._call(self._client.tasks.get, task_id=task_uid)

    def multi_search(self, queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Execute multiple queries via OpenSearch ``_msearch`` in a single round-trip."""
        body_lines: list[dict[str, Any]] = []

        for query_dict in queries:
            uid = query_dict["uid"]
            q = query_dict.get("query", "")
            params = {k: v for k, v in query_dict.items() if k not in ("uid", "query")}

            body_lines.append({"index": uid})

            limit = params.pop("limit", 20)
            offset = params.pop("offset", 0)

            if q:
                searchable_fields = params.pop("searchableAttributes", ["*"])
                must_clauses: list[dict[str, Any]] = [{"multi_match": {"query": q, "fields": searchable_fields}}]
            else:
                params.pop("searchableAttributes", None)
                must_clauses = []

            filter_param = params.pop("filter", None)
            filter_clauses: list[dict[str, Any]] = []
            if filter_param and isinstance(filter_param, dict):
                translated = translate_filter_to_opensearch(filter_param)
                if translated:
                    if "bool" in translated and "filter" in translated["bool"]:
                        filter_clauses.extend(translated["bool"]["filter"])
                    else:
                        filter_clauses.append(translated)

            bool_body: dict[str, Any] = {}
            if must_clauses:
                bool_body["must"] = must_clauses
            if filter_clauses:
                bool_body["filter"] = filter_clauses

            body_query = {"bool": bool_body} if bool_body else {"match_all": {}}
            sub_body: dict[str, Any] = {
                "query": body_query,
                "size": limit,
                "from": offset,
            }

            sort_param = params.pop("sort", None)
            if sort_param:
                sub_body["sort"] = translate_sort_to_opensearch(sort_param)

            facets = params.pop("facets", None)
            if facets:
                sub_body["aggs"] = {field: {"terms": {"field": field, "size": 100}} for field in facets}

            highlight_fields = params.pop("highlight_fields", None)
            pre_tag = params.pop("highlight_pre_tag", "<mark>")
            post_tag = params.pop("highlight_post_tag", "</mark>")
            if highlight_fields:
                sub_body["highlight"] = {
                    "pre_tags": [pre_tag],
                    "post_tags": [post_tag],
                    "fields": {f: {} for f in highlight_fields},
                }

            body_lines.append(sub_body)

        raw_response = self._call(self._client.msearch, body=body_lines)
        responses = raw_response.get("responses", [])

        results: list[dict[str, Any]] = []
        for i, resp in enumerate(responses):
            q_dict = queries[i]
            q_str = q_dict.get("query", "")
            lim = q_dict.get("limit", 20)
            off = q_dict.get("offset", 0)
            results.append(self._normalise_search_response(resp, q_str, lim, off))

        return results

    def add_documents_ndjson(
        self,
        uid: str,
        documents: Iterable[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Add or update documents using opensearch-py streaming_bulk helper."""

        def _actions() -> Iterable[dict[str, Any]]:
            for doc in documents:
                yield {
                    "_op_type": "index",
                    "_index": uid,
                    "_id": str(doc.get(primary_key, "")),
                    "_source": doc,
                }

        success = 0
        errors: list[Any] = []
        for ok, info in self._call(
            opensearchpy.helpers.streaming_bulk,
            self._client,
            _actions(),
            raise_on_error=False,
        ):
            if ok:
                success += 1
            else:
                errors.append(info)

        return {"succeeded": success, "failed": len(errors), "errors": errors}

    def swap_indexes(self, pairs: list[tuple[str, str]]) -> dict[str, Any]:
        """Atomically swap index names via the OpenSearch aliases API.

        For each ``(index_a, index_b)`` pair, the operation re-points any
        alias currently named ``index_a`` to ``index_b``. When ``index_a``
        is a real index (not an alias), an alias ``index_a_live`` is created
        pointing to ``index_b``.
        """
        actions: list[dict[str, Any]] = []

        for index_a, index_b in pairs:
            # Check if index_a is already an alias.
            try:
                alias_info = self._call(self._client.indices.get_alias, name=index_a)
                # alias_info is {real_index: {"aliases": {alias_name: {}}}}
                for real_index, alias_data in alias_info.items():
                    if index_a in alias_data.get("aliases", {}):
                        actions.append({"remove": {"index": real_index, "alias": index_a}})
                        actions.append({"add": {"index": index_b, "alias": index_a}})
                        break
                else:
                    # index_a exists as a real index with no alias named index_a.
                    actions.append({"add": {"index": index_b, "alias": f"{index_a}_live"}})
            except (IndexNotFoundError, SearchBackendError):
                # index_a does not exist — create a live alias pointing to index_b.
                actions.append({"add": {"index": index_b, "alias": f"{index_a}_live"}})

        if not actions:
            return {}

        return self._call(
            self._client.indices.update_aliases,
            body={"actions": actions},
        )

    def get_document(self, uid: str, document_id: str) -> dict[str, Any]:
        """Fetch a single document by primary key.

        Returns ``_source`` merged with ``{"id": _id}`` (BR-012).
        """
        response = self._call(self._client.get, index=uid, id=document_id)
        source = dict(response.get("_source", {}))
        source["id"] = response["_id"]
        return source

    def get_documents(
        self,
        uid: str,
        document_ids: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch multiple documents, optionally by IDs (BR-010).

        Uses ``mget`` for ID-based fetches and ``search`` with ``match_all``
        for browse mode.
        """
        effective_fields: list[str] | None = None
        if fields is not None:
            effective_fields = fields if "id" in fields else ["id", *fields]

        if document_ids is not None:
            mget_body: dict[str, Any] = {"ids": [str(d) for d in document_ids]}
            response = self._call(self._client.mget, index=uid, body=mget_body)
            results: list[dict[str, Any]] = []
            for doc in response.get("docs", []):
                if not doc.get("found", False):
                    continue
                source = dict(doc.get("_source", {}))
                source["id"] = doc["_id"]
                if effective_fields is not None:
                    source = {k: v for k, v in source.items() if k in effective_fields}
                results.append(source)
            return results

        # Browse mode — search with match_all.
        body: dict[str, Any] = {
            "query": {"match_all": {}},
            "size": limit,
            "from": offset,
        }
        if effective_fields is not None:
            body["_source"] = {"includes": effective_fields}

        response = self._call(self._client.search, index=uid, body=body)
        docs: list[dict[str, Any]] = []
        for hit in response.get("hits", {}).get("hits", []):
            source = dict(hit.get("_source", {}))
            source["id"] = hit["_id"]
            docs.append(source)
        return docs

    def facet_search(
        self,
        uid: str,
        facet_name: str,
        facet_query: str = "",
        **params: Any,
    ) -> list[dict[str, Any]]:
        """Search within facet values for typeahead filter UIs.

        Uses a ``terms`` aggregation with an optional ``include`` regex pattern
        for filtering. Returns ``[{"value": str, "count": int}]`` sorted by
        count descending (BR-014).
        """
        agg_body: dict[str, Any] = {"field": facet_name, "size": 100}
        if facet_query:
            escaped = re.escape(facet_query)
            agg_body["include"] = f".*{escaped}.*"

        body: dict[str, Any] = {
            "size": 0,
            "aggs": {"facet_values": {"terms": agg_body}},
        }

        # Apply any additional search params as a query filter.
        filter_param = params.pop("filter", None)
        if filter_param and isinstance(filter_param, dict):
            translated = translate_filter_to_opensearch(filter_param)
            if translated:
                body["query"] = translated

        response = self._call(self._client.search, index=uid, body=body)
        buckets = response.get("aggregations", {}).get("facet_values", {}).get("buckets", [])
        results = [{"value": str(bucket["key"]), "count": int(bucket["doc_count"])} for bucket in buckets]
        return sorted(results, key=lambda x: x["count"], reverse=True)

    def similar_documents(
        self,
        uid: str,
        document_id: str,
        **params: Any,
    ) -> dict[str, Any]:
        """Find documents similar to the given document using ``more_like_this``.

        Returns the search result in icv-search canonical format.
        """
        limit: int = params.pop("limit", 20)
        offset: int = params.pop("offset", 0)
        min_term_freq: int = params.pop("min_term_freq", 1)
        min_doc_freq: int = params.pop("min_doc_freq", 1)
        fields: list[str] | None = params.pop("fields", None)

        mlt_query: dict[str, Any] = {
            "like": [{"_index": uid, "_id": document_id}],
            "min_term_freq": min_term_freq,
            "min_doc_freq": min_doc_freq,
        }
        if fields:
            mlt_query["fields"] = fields

        body: dict[str, Any] = {
            "query": {"more_like_this": mlt_query},
            "size": limit,
            "from": offset,
        }

        response = self._call(self._client.search, index=uid, body=body)
        return self._normalise_search_response(response, "", limit, offset)

    def compact(self, uid: str) -> dict[str, Any]:
        """Reclaim storage space by calling OpenSearch forcemerge (BR-016).

        Never raises — returns ``{}`` on failure per BR-016.
        """
        try:
            return self._call(self._client.indices.forcemerge, index=uid)
        except (SearchBackendError, SearchTimeoutError):
            return {}

    def update_documents(
        self,
        uid: str,
        documents: list[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Partial update of document fields using ``update`` bulk actions (BR-015).

        Only the supplied fields are modified; existing fields are preserved.
        """
        actions = [
            {
                "_op_type": "update",
                "_index": uid,
                "_id": str(doc.get(primary_key, "")),
                "doc": doc,
            }
            for doc in documents
        ]
        success, errors = self._call(
            opensearchpy.helpers.bulk,
            self._client,
            actions,
            raise_on_error=False,
        )
        return {"succeeded": success, "failed": len(errors), "errors": errors}
