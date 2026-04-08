"""Apache Solr backend using pysolr.

Optional extra — install with::

    pip install django-icv-search[solr]

The backend raises ``ImproperlyConfigured`` at construction time when
``pysolr`` is not installed.

SolrCloud mode is assumed for production deployments.  A plain
``pysolr.Solr`` client is used when ``zookeeper_hosts`` is empty; a
``pysolr.SolrCloud`` client is used otherwise.

Administrative operations (index creation/deletion, schema management)
use the Solr Collections API v2 and Schema API via a separate
``httpx.Client``.  Document and search operations go through pysolr.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

try:
    import pysolr  # type: ignore[import-untyped]

    _PYSOLR_AVAILABLE = True
except ImportError:
    _PYSOLR_AVAILABLE = False

from icv_search.backends.base import BaseSearchBackend
from icv_search.backends.filters import translate_filter_to_solr, translate_sort_to_solr
from icv_search.exceptions import IndexNotFoundError, SearchBackendError, SearchTimeoutError

logger = logging.getLogger(__name__)

# Solr settings keys that have no direct Solr schema mapping.
_SKIPPED_SETTINGS = frozenset(
    {
        "filterableAttributes",
        "sortableAttributes",
        "rankingRules",
        "typoTolerance",
        "displayedAttributes",
    }
)


class SolrBackend(BaseSearchBackend):
    """Search backend for Apache Solr using pysolr.

    Uses pysolr for document and search operations and ``httpx`` for
    administrative Collections API and Schema API calls.

    Args:
        url: Base URL of the Solr instance, e.g. ``http://localhost:8983/solr``.
        api_key: Solr Basic Auth password or API key.  Empty string = no auth.
        timeout: Request timeout in seconds (BR-005).
        collection_config: Config set name for new collections.  Must exist in
            ZooKeeper or on the Solr node.  Defaults to ``"default"``.
        commit_within: Milliseconds before a soft commit is issued after document
            operations.  Lower values reduce indexing latency.  Defaults to 1000.
        zookeeper_hosts: ZooKeeper connection string for SolrCloud, e.g.
            ``"zoo1:2181,zoo2:2181"``.  When non-empty a ``pysolr.SolrCloud``
            client is created; otherwise a ``pysolr.Solr`` client is used.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        timeout: int = 30,
        *,
        collection_config: str = "default",
        commit_within: int = 1000,
        zookeeper_hosts: str = "",
        **kwargs: Any,
    ) -> None:
        if not _PYSOLR_AVAILABLE:
            from django.core.exceptions import ImproperlyConfigured

            raise ImproperlyConfigured(
                "The Solr backend requires pysolr. Install it with: pip install django-icv-search[solr]"
            )

        super().__init__(url=url.rstrip("/"), api_key=api_key, timeout=timeout, **kwargs)

        self.collection_config = collection_config
        self.commit_within = commit_within
        self.zookeeper_hosts = zookeeper_hosts

        # Authentication tuple for pysolr and httpx.
        # Solr Basic Auth uses an empty username by convention.
        self._auth: tuple[str, str] | None = ("", api_key) if api_key else None

        # pysolr client cache: uid → pysolr.Solr instance.
        # Created lazily by _solr(uid).
        self._clients: dict[str, Any] = {}

        # ZooKeeper object for SolrCloud (created once, shared across clients).
        self._zookeeper: Any = None
        if zookeeper_hosts:
            self._zookeeper = pysolr.ZooKeeper(zookeeper_hosts)

        # Cached searchable attributes per collection (uid → list[str]).
        # Populated by update_settings(), used by similar_documents().
        self._searchable_attrs: dict[str, list[str]] = {}

        # httpx client for admin / Collections API operations.
        admin_headers: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
        self._http = httpx.Client(
            base_url=self.url,
            headers=admin_headers,
            timeout=self.timeout,
            auth=self._auth,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _solr(self, uid: str) -> Any:
        """Return a cached pysolr client for the given collection ``uid``."""
        if uid not in self._clients:
            if self._zookeeper is not None:
                self._clients[uid] = pysolr.SolrCloud(
                    self._zookeeper,
                    uid,
                    auth=self._auth,
                    timeout=self.timeout,
                )
            else:
                self._clients[uid] = pysolr.Solr(
                    f"{self.url}/{uid}",
                    auth=self._auth,
                    timeout=self.timeout,
                )
        return self._clients[uid]

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Invoke a pysolr method, mapping exceptions to icv-search exceptions.

        Maps ``pysolr.SolrError`` containing timeout indicators to
        ``SearchTimeoutError`` (BR-005); all other ``SolrError`` instances
        map to ``SearchBackendError``.
        """
        try:
            return fn(*args, **kwargs)
        except pysolr.SolrError as exc:
            msg = str(exc).lower()
            if "timeout" in msg or "timed out" in msg:
                raise SearchTimeoutError(f"Solr request timed out: {exc}", exc) from exc
            raise SearchBackendError(f"Solr error: {exc}", exc) from exc

    def _admin_request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Execute an httpx request against the Solr admin / Collections API.

        Raises:
            IndexNotFoundError: On HTTP 404.
            SearchTimeoutError: On timeout.
            SearchBackendError: On other HTTP errors.
        """
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise SearchTimeoutError(f"Solr admin request timed out: {method} {path}", exc) from exc
        except httpx.HTTPError as exc:
            raise SearchBackendError(f"Solr admin request failed: {method} {path} — {exc}", exc) from exc

        if response.status_code == 404:
            raise IndexNotFoundError(f"Solr collection not found: {path}")
        if response.status_code >= 400:
            raise SearchBackendError(f"Solr admin error {response.status_code}: {response.text}")

        if response.status_code == 204:
            return {}
        try:
            return response.json()
        except Exception:
            return {}

    @staticmethod
    def _synthetic_task(uid: str, task_type: str = "documentAdditionOrUpdate") -> dict[str, Any]:
        """Return a synthetic task dict matching the icv-search canonical shape.

        Solr's ``/update`` endpoint is synchronous; there is no task UID.
        """
        return {
            "taskUid": "",
            "indexUid": uid,
            "status": "succeeded",
            "type": task_type,
        }

    # ------------------------------------------------------------------
    # Abstract methods — index management
    # ------------------------------------------------------------------

    def create_index(self, uid: str, primary_key: str = "id") -> dict[str, Any]:
        """Create a SolrCloud collection using the Collections API v2.

        ``primary_key`` is stored for schema reference but Solr uses the
        ``uniqueKey`` field defined in the config set (conventionally ``id``).
        """
        payload = {
            "name": uid,
            "config": self.collection_config,
            "numShards": 1,
            "replicationFactor": 1,
        }
        return self._admin_request("POST", "/api/collections", json=payload)

    def delete_index(self, uid: str) -> None:
        """Delete a SolrCloud collection using the Collections API v2."""
        self._admin_request("DELETE", f"/api/collections/{uid}")
        # Evict cached pysolr client for the deleted collection.
        self._clients.pop(uid, None)

    def update_settings(self, uid: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Push supported settings to the collection's managed schema.

        Supported settings:

        * ``searchableAttributes`` — stored internally for ``qf`` construction;
          no schema operation is performed.
        * ``synonyms`` — pushed to the managed synonyms resource.
        * ``stopWords`` — pushed to the managed stop-words resource.

        All other keys are recorded in the ``"skipped"`` list without
        raising an error, so that Meilisearch-tuned settings dicts work
        transparently with the Solr backend.
        """
        applied: list[str] = []
        skipped: list[str] = []

        for key, value in settings.items():
            if key == "searchableAttributes":
                self._searchable_attrs[uid] = list(value)
                applied.append(key)

            elif key == "synonyms":
                # Push each synonym entry to the managed resource.
                # Value is expected to be a list of synonym strings in Solr
                # format, e.g. ["quick,fast", "UK,United Kingdom"].
                entries = value if isinstance(value, list) else []
                if entries:
                    self._admin_request(
                        "POST",
                        f"/solr/{uid}/schema/analysis/synonyms/english",
                        json=entries,
                    )
                applied.append(key)

            elif key == "stopWords":
                entries = value if isinstance(value, list) else []
                if entries:
                    self._admin_request(
                        "POST",
                        f"/solr/{uid}/schema/analysis/stopwords/english",
                        json=entries,
                    )
                applied.append(key)

            elif key in _SKIPPED_SETTINGS:
                logger.debug("Solr backend: setting %r has no direct Solr mapping (skipped)", key)
                skipped.append(key)

            else:
                logger.debug("Solr backend: unknown setting %r (skipped)", key)
                skipped.append(key)

        return {"applied": applied, "skipped": skipped}

    def get_settings(self, uid: str) -> dict[str, Any]:
        """Retrieve schema summary and managed synonym/stop-word lists.

        Returns a normalised dict in the icv-search settings format.
        """
        schema = self._admin_request("GET", f"/api/collections/{uid}/schema")

        # Fetch managed resources — tolerate missing resources gracefully.
        try:
            synonyms_resp = self._admin_request("GET", f"/solr/{uid}/schema/analysis/synonyms/english")
            synonyms = synonyms_resp.get("synonymMappings", {}).get("managedMap", {})
        except (SearchBackendError, IndexNotFoundError):
            synonyms = {}

        try:
            stopwords_resp = self._admin_request("GET", f"/solr/{uid}/schema/analysis/stopwords/english")
            stopwords = stopwords_resp.get("wordSet", {}).get("managedList", [])
        except (SearchBackendError, IndexNotFoundError):
            stopwords = []

        # Extract field names from schema for searchable attrs fallback.
        fields = [f.get("name") for f in schema.get("schema", {}).get("fields", [])]

        return {
            "searchableAttributes": self._searchable_attrs.get(uid, fields),
            "filterableAttributes": [],
            "sortableAttributes": [],
            "synonyms": synonyms,
            "stopWords": stopwords,
            "rankingRules": [],
            "rawSchema": schema,
        }

    # ------------------------------------------------------------------
    # Abstract methods — document operations
    # ------------------------------------------------------------------

    def add_documents(
        self,
        uid: str,
        documents: list[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Add or replace documents using pysolr ``add()`` with ``commitWithin``."""
        self._call(
            self._solr(uid).add,
            documents,
            commit=False,
            commitWithin=self.commit_within,
        )
        return self._synthetic_task(uid)

    def delete_documents(self, uid: str, document_ids: list[str]) -> dict[str, Any]:
        """Delete documents by ID using pysolr ``delete()``."""
        self._call(
            self._solr(uid).delete,
            id=document_ids,
            commit=False,
            commitWithin=self.commit_within,
        )
        return self._synthetic_task(uid, "documentDeletion")

    def clear_documents(self, uid: str) -> dict[str, Any]:
        """Remove all documents using a match-all delete with immediate commit."""
        self._call(self._solr(uid).delete, q="*:*", commit=True)
        return self._synthetic_task(uid, "documentDeletion")

    def update_documents(
        self,
        uid: str,
        documents: list[dict[str, Any]],
        primary_key: str = "id",
    ) -> dict[str, Any]:
        """Partial/atomic update of document fields.

        Solr supports atomic updates via modifier dicts such as::

            {"id": "prod-123", "stock_count": {"set": 0}}

        pysolr passes these through to ``/update`` unchanged.  The backend
        does not validate atomic modifier syntax — that is delegated to Solr.
        """
        self._call(
            self._solr(uid).add,
            documents,
            commit=False,
            commitWithin=self.commit_within,
        )
        return self._synthetic_task(uid)

    # ------------------------------------------------------------------
    # Abstract method — search
    # ------------------------------------------------------------------

    def search(self, uid: str, query: str, **params: Any) -> dict[str, Any]:
        """Execute a search query using the edismax query parser.

        Parameter mapping
        -----------------
        ``filter`` (dict | list[str])
            Translated to one or more ``fq`` parameters.
        ``sort`` (list[str])
            Translated to Solr ``sort`` parameter string.
        ``limit`` (int)
            Maps to ``rows``.  Default 20.
        ``offset`` (int)
            Maps to ``start``.  Default 0.
        ``facets`` (list[str])
            Builds a JSON Facet API ``json.facet`` parameter.
        ``json_facet`` (dict)
            Raw JSON Facet API dict.  Takes precedence over ``facets``.
        ``attributesToRetrieve`` (list[str])
            Maps to ``fl`` field list.  ``id`` is always included (BR-010).
        ``attributesToHighlight`` (list[str])
            Enables the unified highlighter on listed fields.
        ``highlightPreTag`` (str)
            Defaults to ``<em>``.
        ``highlightPostTag`` (str)
            Defaults to ``</em>``.
        ``cursorMark`` (str)
            Enables cursor-based deep pagination.
        ``searchableAttributes`` (list[str])
            Overrides ``qf`` for this request.
        """
        q = query or "*:*"
        rows: int = int(params.pop("limit", 20))
        start: int = int(params.pop("offset", 0))

        # Filter translation.
        raw_filter = params.pop("filter", None)
        fq: list[str] = []
        if raw_filter is not None:
            fq = translate_filter_to_solr(raw_filter)

        # Sort translation.
        raw_sort = params.pop("sort", None)
        sort_str: str = ""
        if raw_sort is not None:
            sort_str = translate_sort_to_solr(raw_sort)

        # Field list (fl). id is always included (BR-010).
        attrs_to_retrieve: list[str] | None = params.pop(
            "attributes_to_retrieve", params.pop("attributesToRetrieve", None)
        )
        if attrs_to_retrieve is not None:
            fl_fields = set(attrs_to_retrieve) | {"id"}
            fl = ",".join(sorted(fl_fields))
        else:
            fl = "*,score"

        # Query fields (qf) from searchableAttributes setting or param override.
        searchable_override: list[str] | None = params.pop("searchableAttributes", None)
        qf_list = searchable_override or self._searchable_attrs.get(uid, [])
        qf = " ".join(qf_list) if qf_list else None

        # Facets — JSON Facet API.
        json_facet: dict[str, Any] | None = params.pop("json_facet", None)
        facets_list: list[str] | None = params.pop("facets", None)
        if json_facet is None and facets_list:
            json_facet = {f: {"type": "terms", "field": f, "limit": 100} for f in facets_list}

        # Highlighting.
        hl_fields: list[str] | None = params.pop("attributesToHighlight", None)
        hl_pre_tag: str = params.pop("highlightPreTag", "<em>")
        hl_post_tag: str = params.pop("highlightPostTag", "</em>")

        # Cursor-based pagination.
        cursor_mark: str | None = params.pop("cursorMark", None)
        if cursor_mark is not None:
            # Cursor mark requires a sort including the uniqueKey field.
            if sort_str and "id" not in sort_str:
                sort_str = f"{sort_str}, id asc"
            elif not sort_str:
                sort_str = "id asc"

        # Build pysolr kwargs.
        solr_kwargs: dict[str, Any] = {
            "defType": "edismax",
            "fl": fl,
            "rows": rows,
            "start": start,
        }
        if fq:
            solr_kwargs["fq"] = fq
        if sort_str:
            solr_kwargs["sort"] = sort_str
        if qf:
            solr_kwargs["qf"] = qf
        if json_facet is not None:
            solr_kwargs["json.facet"] = json.dumps(json_facet)
        if hl_fields:
            solr_kwargs["hl"] = "true"
            solr_kwargs["hl.method"] = "unified"
            solr_kwargs["hl.fl"] = ",".join(hl_fields)
            solr_kwargs["hl.tag.pre"] = hl_pre_tag
            solr_kwargs["hl.tag.post"] = hl_post_tag
        if cursor_mark is not None:
            solr_kwargs["cursorMark"] = cursor_mark

        # Merge any remaining caller-supplied params (pass-through).
        solr_kwargs.update(params)

        raw = self._call(self._solr(uid).search, q, **solr_kwargs)
        return self._normalise_search_response(raw, query, rows, start, hl_fields)

    @staticmethod
    def _normalise_search_response(
        raw: Any,
        query: str,
        rows: int,
        start: int,
        hl_fields: list[str] | None,
    ) -> dict[str, Any]:
        """Convert a pysolr Results object to the icv-search canonical shape."""
        docs: list[dict[str, Any]] = list(raw.docs) if raw.docs else []
        hits_count: int = raw.hits if raw.hits is not None else 0
        qtime: int = raw.qtime if raw.qtime is not None else 0

        # Highlighting: merge into formattedHits.
        formatted_hits: list[dict[str, Any]] = []
        if hl_fields and raw.highlighting:
            for doc in docs:
                doc_id = str(doc.get("id", ""))
                hl_doc = raw.highlighting.get(doc_id, {})
                merged = dict(doc)
                for field, snippets in hl_doc.items():
                    if snippets:
                        merged[field] = snippets[0] if len(snippets) == 1 else snippets
                formatted_hits.append(merged)
        else:
            formatted_hits = list(docs)

        # Facet distribution from JSON Facet API response.
        facet_distribution: dict[str, dict[str, int]] = {}
        if raw.facets:
            for facet_field, facet_data in raw.facets.items():
                if isinstance(facet_data, dict) and "buckets" in facet_data:
                    facet_distribution[facet_field] = {
                        bucket["val"]: bucket["count"] for bucket in facet_data["buckets"]
                    }

        result: dict[str, Any] = {
            "hits": docs,
            "query": query,
            "processingTimeMs": qtime,
            "estimatedTotalHits": hits_count,
            "limit": rows,
            "offset": start,
            "facetDistribution": facet_distribution,
            "formattedHits": formatted_hits,
        }

        # nextCursorMark for cursor-based pagination.
        next_cursor = getattr(raw, "nextCursorMark", None)
        if next_cursor is not None:
            result["nextCursorMark"] = next_cursor

        return result

    # ------------------------------------------------------------------
    # Abstract methods — stats and health
    # ------------------------------------------------------------------

    def get_stats(self, uid: str) -> dict[str, Any]:
        """Retrieve collection statistics from the Collections API and Luke handler."""
        collection_info = self._admin_request("GET", f"/api/collections/{uid}")

        # Luke handler for document count and index size.
        try:
            luke_resp = self._admin_request("GET", f"/solr/{uid}/admin/luke", params={"numTerms": "0", "wt": "json"})
        except (SearchBackendError, IndexNotFoundError):
            luke_resp = {}

        index_info = luke_resp.get("index", {})
        num_docs: int = index_info.get("numDocs", 0)
        fields: dict[str, Any] = luke_resp.get("fields", {})

        return {
            "uid": uid,
            "numberOfDocuments": num_docs,
            "isIndexing": False,
            "fieldDistribution": {k: v.get("docs", 0) for k, v in fields.items()} if fields else {},
            "rawStats": collection_info,
        }

    def health(self) -> bool:
        """Check Solr health by polling the admin info endpoint.

        Returns ``True`` if healthy, ``False`` without raising on error.
        """
        try:
            self._admin_request("GET", "/solr/admin/info/system", params={"wt": "json"})
            if self.zookeeper_hosts:
                self._admin_request("GET", "/api/cluster")
        except (SearchBackendError, SearchTimeoutError, IndexNotFoundError):
            return False
        return True

    # ------------------------------------------------------------------
    # Optional method overrides
    # ------------------------------------------------------------------

    def get_task(self, task_uid: str) -> dict[str, Any]:
        """Return a static succeeded dict — Solr operations are synchronous."""
        return {
            "taskUid": task_uid,
            "status": "succeeded",
            "type": "documentAdditionOrUpdate",
        }

    def swap_indexes(self, pairs: list[tuple[str, str]]) -> dict[str, Any]:
        """Swap collection names using SolrCloud ``CREATEALIAS`` actions.

        For each ``(index_a, index_b)`` pair, creates two aliases:
        ``{index_a}_alias → index_b`` and ``{index_b}_alias → index_a``.

        Note: The two ``CREATEALIAS`` calls are issued sequentially.  A failure
        on the second call leaves one alias updated and one not.  The service
        layer is responsible for handling partial-swap state.
        """
        responses: list[dict[str, Any]] = []
        for index_a, index_b in pairs:
            resp_a = self._admin_request(
                "GET",
                "/solr/admin/collections",
                params={"action": "CREATEALIAS", "name": f"{index_a}_alias", "collections": index_b},
            )
            resp_b = self._admin_request(
                "GET",
                "/solr/admin/collections",
                params={"action": "CREATEALIAS", "name": f"{index_b}_alias", "collections": index_a},
            )
            responses.append({"pair": [index_a, index_b], "alias_a": resp_a, "alias_b": resp_b})
        return {"swaps": responses}

    def get_document(self, uid: str, document_id: str) -> dict[str, Any]:
        """Fetch a single document via the Solr Real-Time Get handler.

        Uses ``GET /solr/{uid}/get?id={document_id}&wt=json``.

        Raises:
            IndexNotFoundError: When the collection does not exist.
            SearchBackendError: When the document is not found.
        """
        resp = self._admin_request("GET", f"/solr/{uid}/get", params={"id": document_id, "wt": "json"})
        doc = resp.get("doc")
        if doc is None:
            raise SearchBackendError(f"Solr document not found: collection={uid!r}, id={document_id!r}")
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
        """Fetch multiple documents via Real-Time Get or browse mode.

        When ``document_ids`` is provided uses ``/solr/{uid}/get?ids=...``.
        In browse mode uses ``_solr.search("*:*", rows=limit, start=offset)``.
        ``id`` is always present in returned documents (BR-010).
        """
        effective_fields: list[str] | None = None
        if fields is not None:
            effective_fields = fields if "id" in fields else ["id", *fields]

        if document_ids is not None:
            ids_str = ",".join(str(i) for i in document_ids)
            resp = self._admin_request("GET", f"/solr/{uid}/get", params={"ids": ids_str, "wt": "json"})
            docs: list[dict[str, Any]] = resp.get("response", {}).get("docs", [])
            if effective_fields is not None:
                keep = set(effective_fields)
                docs = [{k: v for k, v in doc.items() if k in keep} for doc in docs]
            return docs

        # Browse mode.
        solr_kwargs: dict[str, Any] = {"rows": limit, "start": offset}
        if effective_fields is not None:
            solr_kwargs["fl"] = ",".join(effective_fields)
        raw = self._call(self._solr(uid).search, "*:*", **solr_kwargs)
        return list(raw.docs) if raw.docs else []

    def facet_search(
        self,
        uid: str,
        facet_name: str,
        facet_query: str = "",
        **params: Any,
    ) -> list[dict[str, Any]]:
        """Search within facet values using the JSON Facet API with ``prefix``.

        Returns ``[{"value": str, "count": int}]`` sorted by count
        descending (BR-014).
        """
        facet_def = {
            "type": "terms",
            "field": facet_name,
            "limit": params.pop("limit", 20),
        }
        if facet_query:
            facet_def["prefix"] = facet_query

        raw = self._call(
            self._solr(uid).search,
            "*:*",
            **{"json.facet": json.dumps({facet_name: facet_def}), "rows": 0},
        )

        buckets: list[dict[str, Any]] = []
        if raw.facets and facet_name in raw.facets:
            facet_data = raw.facets[facet_name]
            if isinstance(facet_data, dict) and "buckets" in facet_data:
                buckets = [{"value": str(b["val"]), "count": int(b["count"])} for b in facet_data["buckets"]]

        return sorted(buckets, key=lambda x: x["count"], reverse=True)

    def similar_documents(
        self,
        uid: str,
        document_id: str,
        **params: Any,
    ) -> dict[str, Any]:
        """Find documents similar to the given document using MoreLikeThis.

        Uses pysolr's ``more_like_this()`` method against the ``/mlt`` handler.
        The ``mlt_fields`` param (comma-separated string) overrides the field
        list; when absent the backend uses the stored searchable attributes for
        the collection.

        Raises:
            SearchBackendError: When the MoreLikeThis component is not
                configured in the Solr schema.
        """
        mlt_fl = params.pop("mlt_fields", None)
        if mlt_fl is None:
            stored_attrs = self._searchable_attrs.get(uid, [])
            # Strip boost weights (e.g. "title^3" → "title").
            clean_attrs = [a.split("^")[0] for a in stored_attrs]
            mlt_fl = ",".join(clean_attrs) if clean_attrs else "id"

        rows: int = int(params.pop("limit", 20))

        try:
            raw = self._call(
                self._solr(uid).more_like_this,
                q=f"id:{document_id}",
                mltfl=mlt_fl,
                rows=rows,
                **params,
            )
        except SearchBackendError as exc:
            msg = str(exc).lower()
            if "morelikethis" in msg or "mlt" in msg or "component" in msg:
                raise SearchBackendError(
                    "Solr MoreLikeThis component is not configured. "
                    "Add the MLT component to your Solr schema to use similar_documents()."
                ) from exc
            raise

        return self._normalise_search_response(raw, f"id:{document_id}", rows, 0, None)

    def compact(self, uid: str) -> dict[str, Any]:
        """Trigger a Solr ``optimize`` command to compact index segments.

        Never raises an error (BR-016).
        """
        try:
            result = self._call(self._solr(uid).optimize)
            return result if result is not None else {}
        except (SearchBackendError, SearchTimeoutError):
            logger.warning("Solr optimize (compact) failed for collection %r — ignoring", uid)
            return {}
