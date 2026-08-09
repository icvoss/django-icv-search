"""
Package-level settings with defaults.

All settings are namespaced under ICV_SEARCH_* and accessed via this module.
Consuming projects override in their Django settings file.

Usage:
    from icv_search.conf import ICV_SEARCH_BACKEND
"""

from django.conf import settings

# Dotted path to the search backend class
ICV_SEARCH_BACKEND: str = getattr(settings, "ICV_SEARCH_BACKEND", "icv_search.backends.meilisearch.MeilisearchBackend")

# Search engine connection URL
ICV_SEARCH_URL: str = getattr(settings, "ICV_SEARCH_URL", "http://localhost:7700")

# Master/admin API key for the search engine
ICV_SEARCH_API_KEY: str = getattr(settings, "ICV_SEARCH_API_KEY", "")

# Request timeout in seconds for backend calls
ICV_SEARCH_TIMEOUT: int = getattr(settings, "ICV_SEARCH_TIMEOUT", 30)

# Extra keyword arguments forwarded to the backend constructor.
# Use this to pass backend-specific options without polluting the
# top-level settings namespace.
#
# Example::
#
#     # OpenSearch with AWS SigV4 auth
#     ICV_SEARCH_BACKEND_OPTIONS = {
#         "aws_region": "eu-west-1",
#         "verify_certs": True,
#     }
#
#     # Typesense HA cluster
#     ICV_SEARCH_BACKEND_OPTIONS = {
#         "nodes": [
#             {"host": "node1.example.com", "port": 8108, "protocol": "https"},
#             {"host": "node2.example.com", "port": 8108, "protocol": "https"},
#         ],
#     }
#
#     # Vespa Cloud with mTLS
#     ICV_SEARCH_BACKEND_OPTIONS = {
#         "application": "my-app",
#         "schema": "products",
#         "cert_path": "/path/to/cert.pem",
#         "key_path": "/path/to/key.pem",
#     }
ICV_SEARCH_BACKEND_OPTIONS: dict = getattr(settings, "ICV_SEARCH_BACKEND_OPTIONS", {})

# Dotted path to callable (request_or_none) -> str returning the tenant prefix.
# Empty string disables multi-tenancy.
ICV_SEARCH_TENANT_PREFIX_FUNC: str = getattr(settings, "ICV_SEARCH_TENANT_PREFIX_FUNC", "")

# Auto-sync index settings to the engine on SearchIndex.save()
ICV_SEARCH_AUTO_SYNC: bool = getattr(settings, "ICV_SEARCH_AUTO_SYNC", True)

# Use Celery for document indexing operations
ICV_SEARCH_ASYNC_INDEXING: bool = getattr(settings, "ICV_SEARCH_ASYNC_INDEXING", True)

# Global prefix applied to all index names (e.g. environment prefix "staging_")
ICV_SEARCH_INDEX_PREFIX: str = getattr(settings, "ICV_SEARCH_INDEX_PREFIX", "")

# Auto-index configuration — dict mapping index names to model configs.
# When configured, post_save/post_delete signals are automatically connected
# in IcvSearchConfig.ready(). An empty dict (the default) disables auto-indexing.
#
# Example::
#
#     ICV_SEARCH_AUTO_INDEX = {
#         "articles": {
#             "model": "blog.Article",       # required — "app_label.ModelName"
#             "on_save": True,               # index on post_save (default True)
#             "on_delete": True,             # remove on post_delete (default True)
#             "async": True,                 # use Celery; falls back to ICV_SEARCH_ASYNC_INDEXING
#             "auto_create": True,           # auto-create SearchIndex if missing (default True)
#             "should_update": "blog.utils.should_index_article",  # optional callable(instance) -> bool
#             "updated_field": "updated_at", # for incremental reindex (informational)
#         },
#     }
ICV_SEARCH_AUTO_INDEX: dict = getattr(settings, "ICV_SEARCH_AUTO_INDEX", {})

# Default batch size for bulk indexing operations (bulk_index,
# index_model_instances(bulk=True), reindex_zero_downtime(bulk=True)).
# Larger values reduce HTTP round trips but increase per-batch memory.
ICV_SEARCH_BULK_BATCH_SIZE: int = getattr(settings, "ICV_SEARCH_BULK_BATCH_SIZE", 5000)

# Number of concurrent HTTP sender threads for bulk indexing.
# 2 is optimal for single-index operations (overlap one send with one DB read).
ICV_SEARCH_BULK_CONCURRENCY: int = getattr(settings, "ICV_SEARCH_BULK_CONCURRENCY", 2)

# Debounce window for auto-index signal batching (in seconds).
# When > 0, rapid successive saves (and, since #6, deletes) are batched into
# a single indexing/removal task dispatched after this delay. Requires
# Django's cache framework. Set to 0 to disable debouncing (each save/delete
# dispatches immediately).
ICV_SEARCH_DEBOUNCE_SECONDS: int = getattr(settings, "ICV_SEARCH_DEBOUNCE_SECONDS", 0)

# Maximum number of buffered documents/IDs per index before the debounce
# buffer flushes early, regardless of the time window (#7). Without a cap, a
# high-rate save/delete loop (bulk backfill, reprocessing) accumulates the
# entire operation in one in-memory cache buffer and flushes it as a single
# request. When the buffer reaches this size, a flush is scheduled
# immediately (countdown=0) instead of waiting out the remaining debounce
# window.
ICV_SEARCH_DEBOUNCE_MAX_BUFFER_SIZE: int = getattr(settings, "ICV_SEARCH_DEBOUNCE_MAX_BUFFER_SIZE", 500)

# Chunk size used when flushing a debounce buffer to the backend (#7). The
# buffer itself has no hard ceiling beyond ICV_SEARCH_DEBOUNCE_MAX_BUFFER_SIZE
# (which only triggers an early flush, not a drop), so the flush task sends
# the buffered documents/IDs to the backend in chunks of this size rather
# than a single unbounded request.
ICV_SEARCH_DEBOUNCE_FLUSH_CHUNK_SIZE: int = getattr(settings, "ICV_SEARCH_DEBOUNCE_FLUSH_CHUNK_SIZE", 500)

# Query logging — when True, every search() call creates a SearchQueryLog record.
ICV_SEARCH_LOG_QUERIES: bool = getattr(settings, "ICV_SEARCH_LOG_QUERIES", False)

# When True (and LOG_QUERIES is True), only queries that returned zero results
# are logged.  Useful for reducing storage whilst still capturing actionable data.
ICV_SEARCH_LOG_ZERO_RESULTS_ONLY: bool = getattr(settings, "ICV_SEARCH_LOG_ZERO_RESULTS_ONLY", False)

# Logging mode — controls how search queries are recorded.
# "individual" = one row per query (default, current behaviour)
# "aggregate"  = increment counters on a (query, index_name, date) key
# "both"       = do both
ICV_SEARCH_LOG_MODE: str = getattr(settings, "ICV_SEARCH_LOG_MODE", "individual")

# Sample rate for individual query logging (0.0–1.0).
# 1.0 = log every query (default), 0.5 = log ~50% of queries.
# Only applies to "individual" mode rows. Aggregate mode always counts 100%.
ICV_SEARCH_LOG_SAMPLE_RATE: float = getattr(settings, "ICV_SEARCH_LOG_SAMPLE_RATE", 1.0)

# Enable the search result cache layer (requires Django's cache framework).
# When False (the default), search() always calls the backend directly.
ICV_SEARCH_CACHE_ENABLED: bool = getattr(settings, "ICV_SEARCH_CACHE_ENABLED", False)

# Cache TTL in seconds for stored search results.
ICV_SEARCH_CACHE_TIMEOUT: int = getattr(settings, "ICV_SEARCH_CACHE_TIMEOUT", 60)

# Django cache alias used by the search result cache.
# Defaults to "default". Set to a dedicated alias (e.g. "search") when
# you want to control eviction independently from other cached data.
ICV_SEARCH_CACHE_ALIAS: str = getattr(settings, "ICV_SEARCH_CACHE_ALIAS", "default")

# Enable the merchandising layer (query redirects, rewrites, pins, boosts,
# banners, zero-result fallbacks). When False (the default), merchandised_search()
# delegates directly to search() with no rule evaluation.
ICV_SEARCH_MERCHANDISING_ENABLED: bool = getattr(settings, "ICV_SEARCH_MERCHANDISING_ENABLED", False)

# Cache TTL in seconds for merchandising rules loaded from the database.
# Set to 0 to disable caching (useful in tests).
ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT: int = getattr(settings, "ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT", 300)

# Enable click-through tracking. When False, log_click() is a no-op and
# the click endpoint returns 403.
ICV_SEARCH_CLICK_TRACKING: bool = getattr(settings, "ICV_SEARCH_CLICK_TRACKING", False)

# Click logging mode — controls how click events are recorded.
# "individual" = write only SearchClick rows
# "aggregate"  = write only SearchClickAggregate (via daily rollup command)
# "both"       = write both
ICV_SEARCH_CLICK_LOG_MODE: str = getattr(settings, "ICV_SEARCH_CLICK_LOG_MODE", "aggregate")

# Default min_volume threshold for get_demand_signals(). Queries with fewer
# occurrences than this are excluded from demand signal reports.
ICV_SEARCH_INTELLIGENCE_MIN_VOLUME: int = getattr(settings, "ICV_SEARCH_INTELLIGENCE_MIN_VOLUME", 5)

# Default confidence threshold for auto_create_rewrites(). Synonym suggestions
# below this value are returned but SearchRewrite rules are not created.
ICV_SEARCH_AUTO_SYNONYM_CONFIDENCE: float = getattr(settings, "ICV_SEARCH_AUTO_SYNONYM_CONFIDENCE", 0.8)

# Dotted path to a query preprocessor callable:
#   (query: str, context: QueryContext) -> PreprocessedQuery
# When empty, all queries pass through unchanged. Validated at startup.
ICV_SEARCH_QUERY_PREPROCESSOR: str = getattr(settings, "ICV_SEARCH_QUERY_PREPROCESSOR", "")

# Typesense-specific settings
# Mapping of field name → Typesense type string for schema generation.
# Example: {"price": "float", "tags": "string[]", "published_at": "int64"}
ICV_SEARCH_TYPESENSE_FIELD_TYPES: dict = getattr(settings, "ICV_SEARCH_TYPESENSE_FIELD_TYPES", {})

# Name of the geo field in Typesense documents for geo search.
# Typesense uses a "geopoint" type field (lat/lng stored as [lat, lng]).
ICV_SEARCH_TYPESENSE_GEO_FIELD: str = getattr(settings, "ICV_SEARCH_TYPESENSE_GEO_FIELD", "_geo")

# Number of days to retain IndexSyncLog entries before cleanup deletes them.
ICV_SEARCH_SYNC_LOG_RETENTION_DAYS: int = getattr(settings, "ICV_SEARCH_SYNC_LOG_RETENTION_DAYS", 90)
