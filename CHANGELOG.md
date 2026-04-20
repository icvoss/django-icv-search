# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [1.1.0] - 2026-04-20

### Added

- `clear_sync_logs(days_older_than=90)` service function to purge old
  `IndexSyncLog` entries beyond the retention period.
- `cleanup_sync_logs` Celery task for periodic log cleanup via beat.
- `icv_search_cleanup_sync_logs` management command with `--days` and
  `--dry-run` options.

## [1.0.0] - 2026-04-16

First stable release. Promotes 1.0.0b1 to GA with fixes and expanded test
coverage for cross-backend parameter handling.

### Fixed

- `multi_search()` now forwards all search params and normalises naming
  consistently with `search()`
- Non-Meilisearch backends emit warnings for unsupported params instead of
  silently dropping them; docstrings updated to reflect backend capabilities
- `SearchableMixin` UUID default assertion relaxed to accept `_make_uuid`

### Added

- Cross-backend tests covering param forwarding and field restriction
- Tests for unsupported-param warnings on non-Meilisearch backends
- 90% coverage threshold

### Changed

- README documents all 1.0.0b1 features
- Classifier bumped to `Development Status :: 5 - Production/Stable`

## [1.0.0b1] - 2026-04-07

First beta towards 1.0 — full Meilisearch capability parity.

### Added

- Full Meilisearch capability coverage — hybrid/semantic search, cropping,
  page-based pagination, geo bounding box/polygon, and 20+ new search params
- **Search parameters**: `attributes_to_retrieve`, `attributes_to_search_on`,
  `crop_fields`/`crop_length`/`crop_marker`, `show_ranking_score_details`,
  `show_matches_position`, `ranking_score_threshold`, `distinct` (query-time),
  `hybrid`/`vector`/`retrieve_vectors` (semantic search), `page`/`hits_per_page`,
  `locales`, `geo_bbox`, `geo_polygon`
- **Index settings services** (30+ functions): `displayedAttributes`,
  `distinctAttribute`, `pagination.maxTotalHits`, `faceting` (sortFacetValuesBy,
  maxValuesPerFacet), `proximityPrecision`, `searchCutoffMs`, `dictionary`,
  `separatorTokens`/`nonSeparatorTokens`, `prefixSearch`, `embedders`,
  `localizedAttributes`, `rankingRules` — each with get/update/reset
- `delete_documents_by_filter()` — filter-based document deletion (backend + service)
- `SearchQuery` builder: 14 new chainable methods (`.crop()`, `.hybrid()`,
  `.vector()`, `.distinct()`, `.attributes_to_retrieve()`, `.attributes_to_search_on()`,
  `.ranking_score_threshold()`, `.show_matches_position()`, `.show_ranking_score_details()`,
  `.retrieve_vectors()`, `.locales()`, `.page()`, `.geo_bbox()`, `.geo_polygon()`)
- `SearchResult` new fields: `ranking_score_details`, `matches_position`,
  `page`, `hits_per_page`, `total_hits`, `total_pages`
- `SearchableMixin.search_displayed_fields` class attribute
- 62 new tests covering all additions

### Fixed

- `SearchQuery.geo_near()` params now correctly translate to backend-recognised
  `geo_point`/`geo_radius`/`geo_sort` keys (previously emitted unused `_geo` key)

## [0.10.0] - 2026-04-04

### Added

- `log_query` parameter on `search()` and `merchandised_search()` (default `True`).
  Pass `False` for system-generated searches (category browse, trending products,
  dynamic filter pages) to keep programmatic queries out of search analytics.

## [0.8.0] - 2026-03-27

### Added

- 6 new optional methods on `BaseSearchBackend` ABC: `get_document`, `get_documents`,
  `facet_search`, `similar_documents`, `compact`, `update_documents`
- `MeilisearchBackend` implementations for all 6 new methods
- `DummyBackend` implementations for all 6 new methods (in-memory)
- `PostgresBackend` implementations for `get_document`, `get_documents`,
  `update_documents` (JSONB merge for partial updates)
- New service functions: `get_document()`, `get_documents()`, `update_documents()`,
  `facet_search()`, `similar_documents()`, `compact_index()`
- New `services/discovery.py` module for facet search and similar documents
- Business rules BR-012 through BR-016 covering new method contracts
- 71 new tests in `test_abc_expansion.py`

### Fixed

- PostgresBackend parameter ordering bug in search queries
- PostgresBackend sort type bug (text vs JSONB for numeric sorting)
- Test infrastructure: PostgreSQL as default test database, URL routing for view tests

## [0.7.0] - 2026-03-26

### Added

- **`bulk_index()` service function** — high-throughput document indexing using NDJSON serialisation and concurrent HTTP sends via `ThreadPoolExecutor`; producer/consumer pipeline with bounded queue for backpressure; single summary `IndexSyncLog` and `documents_indexed` signal instead of per-batch overhead
- **`add_documents_ndjson()` backend method** — `BaseSearchBackend` gains optional `add_documents_ndjson()` with default fallback to `add_documents()`; `MeilisearchBackend` implements native NDJSON ingestion (`Content-Type: application/x-ndjson`); `DummyBackend` implements for testing
- **`bulk=True` parameter on `index_model_instances()`** — opt-in fast path that delegates to `bulk_index()`, creating one summary log and firing one signal at completion instead of per-batch
- **`bulk=True` parameter on `reindex_zero_downtime()`** — populates the temporary index via `bulk_index()` for concurrent NDJSON sends during zero-downtime reindex
- **`progress_callback` parameter** on `index_model_instances()`, `reindex_zero_downtime()`, and `bulk_index()` — receives `(indexed_so_far, total)` after each batch for progress reporting without the service layer knowing about stdout
- **`ICV_SEARCH_BULK_BATCH_SIZE`** setting — default batch size for bulk operations (default `5000`)
- **`ICV_SEARCH_BULK_CONCURRENCY`** setting — number of concurrent HTTP sender threads for bulk indexing (default `2`)
- **Benchmark script** (`benchmarks/bench_bulk_index.py`) — standalone script for comparing old vs new indexing paths against a live Meilisearch instance
- 14 new tests covering NDJSON backend methods, `bulk_index()`, bulk model indexing, progress callbacks, and signal/log behaviour

### Changed

- Version bumped to 0.7.0
- `services/__init__.py` exports `bulk_index`
- All existing callers of `index_model_instances()` and `reindex_zero_downtime()` work unchanged — the fast path is opt-in via `bulk=True`

## [0.6.0] - 2026-03-25

### Added

- **Click-through tracking** (FEAT-008) — `SearchClick` and `SearchClickAggregate` models for recording and analysing search result click events; `log_click()`, `get_click_through_rate()`, `get_top_clicked_documents()` service functions; `POST /click/` API endpoint for client-side click logging; `icv_search_click_aggregate` management command for daily rollup
- **Demand signal extraction** (FEAT-008) — `get_demand_signals()` service function identifies high-volume zero-result queries as unmet demand; returns gap score, trend, CTR, and zero-result rate per query
- **Query clustering** (FEAT-008) — `cluster_queries()` groups similar queries by trigram similarity using PostgreSQL's `pg_trgm` extension; returns clusters with representative query, member queries, total volume, and average zero-result rate
- **Auto-synonym suggestion** (FEAT-008) — `suggest_synonyms()` finds zero-result queries similar to successful queries; `auto_create_rewrites()` creates `SearchRewrite` rules from high-confidence suggestions; `icv_search_auto_synonyms` management command
- **Query preprocessor hook** (FEAT-008) — `ICV_SEARCH_QUERY_PREPROCESSOR` setting configures a callable `(query, QueryContext) -> PreprocessedQuery` for NLP/AI query transformation; runs as step 1.5 in the merchandised search pipeline; extracted filters and sort are merged into search params; failing preprocessors fall back silently (BR-027)
- **`QueryContext` and `PreprocessedQuery` dataclasses** in `icv_search.types` — structured input/output for the preprocessor hook
- **`MerchandisedSearchResult.preprocessed` and `.detected_intent` fields** — expose preprocessor output to consuming projects
- **`icv_search_intelligence` management command** — generates full intelligence reports (popular queries, zero-result queries, demand signals, clusters, synonym suggestions) in text or JSON format
- **`MockPreprocessor` test helper** in `icv_search.testing.helpers` — records calls and returns configurable results for testing preprocessor integration
- **`SearchClickFactory` and `SearchClickAggregateFactory`** in `icv_search.testing.factories`
- **`SearchClickAdmin` and `SearchClickAggregateAdmin`** — read-only admin registrations for click tracking models
- **`ICV_SEARCH_CLICK_TRACKING`** setting — enable/disable click-through tracking (default `False`)
- **`ICV_SEARCH_CLICK_LOG_MODE`** setting — click logging strategy: `"individual"`, `"aggregate"`, or `"both"` (default `"aggregate"`)
- **`ICV_SEARCH_INTELLIGENCE_MIN_VOLUME`** setting — minimum query volume for demand signal extraction (default `5`)
- **`ICV_SEARCH_AUTO_SYNONYM_CONFIDENCE`** setting — confidence threshold for auto-created rewrite rules (default `0.8`)
- **`ICV_SEARCH_QUERY_PREPROCESSOR`** setting — dotted path to query preprocessor callable (default `""`)
- **Business rules BR-020 through BR-029** covering click tracking, demand signals, synonyms, preprocessing, and pipeline integration
- Migration `0005_click_tracking` creating `SearchClick` and `SearchClickAggregate` tables

### Changed

- Version bumped to 0.6.0
- `merchandised_search()` pipeline expanded with step 1.5 (query preprocessing); new `skip_preprocessing` parameter; preprocessing output recorded in `applied_rules` (BR-029)
- `services/__init__.py` expanded to export click tracking, intelligence, and preprocessing functions
- Top-level `__init__.py` now exports `PreprocessedQuery` and `QueryContext`
- `apps.py` validates `ICV_SEARCH_QUERY_PREPROCESSOR` at startup (BR-026)

## [0.5.0] - 2026-03-24

### Added

- **`autocomplete()` service function** — lightweight prefix-match query for typeahead use cases; default `limit=5`, `fields` param maps to `attributesToRetrieve`; does not create `SearchQueryLog` rows regardless of `ICV_SEARCH_LOG_QUERIES` (BR-009)
- **`attributesToRetrieve` support on DummyBackend and PostgresBackend** — when passed to `search()`, filters returned hit dicts to only the specified keys plus `id` (BR-010); Meilisearch already handles this natively
- **`delete_document()` convenience function** — removes a single document by ID; delegates to `remove_documents()` (BR-011)
- **`SearchQueryLogFactory` export** — now exported from `icv_search.testing` (was defined but missing from `__all__`)

### Changed

- Version bumped to 0.5.0
- `services/__init__.py` exports `autocomplete` and `delete_document`
- `testing/__init__.py` exports `SearchQueryLogFactory`

## [0.4.0] - 2026-03-23

### Added

- **Search merchandising layer** — optional feature for controlling search result presentation, gated behind `ICV_SEARCH_MERCHANDISING_ENABLED`
- **`MerchandisingRuleBase`** abstract model — shared fields for query matching (`exact`, `contains`, `starts_with`, `regex`), scheduling (`starts_at`/`ends_at`), activation, priority ordering, and hit count tracking
- **`QueryRedirect`** model (FEAT-001) — redirect search queries to a destination URL; supports preserve-query, destination type classification, and HTTP status selection
- **`QueryRewrite`** model (FEAT-002) — transparently rewrite queries before execution with optional filter and sort injection; `merge_filters` controls merge vs replace behaviour
- **`SearchPin`** model (FEAT-003) — pin documents to fixed positions in results or bury them (`position=-1`); unique constraint on `(index_name, tenant_id, query_pattern, document_id)`
- **`BoostRule`** model (FEAT-004) — promote or demote results based on field values; 8 comparison operators (`eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `contains`, `exists`); multiplicative weight applied to ranking scores
- **`SearchBanner`** model (FEAT-005) — display banners alongside search results with position (`top`, `inline`, `bottom`, `sidebar`), type (`informational`, `promotional`, `warning`), and arbitrary metadata
- **`ZeroResultFallback`** model (FEAT-006) — 4 fallback strategies when a query returns no results: `redirect`, `alternative_query` (with retry/word-dropping), `curated_results`, `popular_in_category`
- **`merchandised_search()`** pipeline (FEAT-007) — 9-step orchestrator composing redirect check, query rewrite, search execution, pin insertion, boost re-ranking, zero-result fallback, and banner attachment; each step individually skippable via `skip_*` parameters
- **`MerchandisedSearchResult`** dataclass — extends `SearchResult` with `redirect`, `banners`, `applied_rules`, `original_query`, `was_rewritten`, and `is_fallback` fields; `from_search_result()` factory method
- **Merchandising service functions** — `check_redirect()`, `resolve_redirect_url()`, `apply_rewrite()`, `get_pins_for_query()`, `apply_pins()`, `get_boost_rules_for_query()`, `apply_boosts()`, `get_banners_for_query()`, `get_fallback_for_query()`, `execute_fallback()`
- **Search suggestions** — `get_trending_searches()` and `get_suggested_queries()` using existing `SearchQueryAggregate` data
- **Merchandising rule cache** — database rules cached in Django's cache framework with configurable TTL (`ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT`); automatic invalidation on `post_save`/`post_delete` via signal handlers
- **Merchandising admin** — 6 new admin classes with bulk enable/disable actions, scheduling fieldsets, hit count statistics, and model-specific filters
- **Merchandising factories** — `QueryRedirectFactory`, `QueryRewriteFactory`, `SearchPinFactory`, `BoostRuleFactory`, `SearchBannerFactory`, `ZeroResultFallbackFactory` in `icv_search.testing`
- **`merchandising_enabled`** pytest fixture — sets `ICV_SEARCH_MERCHANDISING_ENABLED=True` and `ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT=0`
- **`ICV_SEARCH_MERCHANDISING_ENABLED`** setting — feature gate for the entire merchandising layer (default `False`)
- **`ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT`** setting — cache TTL for rule lookups (default `300` seconds)
- Migration `0004_merchandising` creating all 6 concrete merchandising tables

### Changed

- Version bumped to 0.4.0
- `services/__init__.py` expanded to export all merchandising service functions and `MerchandisedSearchResult`
- Top-level `__init__.py` now exports `MerchandisedSearchResult`

## [0.3.3] - 2026-03-22

### Added

- **`icv_search_setup` management command** — creates `SearchIndex` records for all entries in `ICV_SEARCH_AUTO_INDEX`, syncs settings, and verifies connectivity; supports `--dry-run`
- **Auto-create `SearchIndex` on first use** — `search()`, `index_documents()`, and other service functions auto-create the `SearchIndex` record instead of crashing with `DoesNotExist`

### Fixed

- `SearchQueryAggregate` index names shortened to stay within the 30-character database limit
- Auto-index with `auto_create=False` correctly skips indexing when the `SearchIndex` record does not exist

## [0.3.2] - 2026-03-22

### Added

- **`icv_search_setup` management command** — creates `SearchIndex` records for all entries in `ICV_SEARCH_AUTO_INDEX`, syncs settings to the engine, and verifies connectivity; supports `--dry-run`
- **Auto-create `SearchIndex` on first use** — `search()`, `index_documents()`, and other service functions now auto-create the `SearchIndex` record (and provision it in the engine) instead of crashing with `DoesNotExist`; uses model class metadata from `ICV_SEARCH_AUTO_INDEX` when available

### Fixed

- `SearchQueryLog.hit_count` now records `estimated_total_hits` (total matching documents) instead of `len(result.hits)` (current page size)
- `SearchQueryAggregate` index names shortened to stay within the 30-character database limit
- Auto-index with `auto_create=False` correctly skips indexing when the `SearchIndex` record does not exist (previously the new auto-create in `resolve_index` would override the setting)

## [0.3.1] - 2026-03-22

### Added

- **Aggregate query analytics** — `SearchQueryAggregate` model for day-level query statistics; stores normalised queries with `total_count`, `zero_result_count`, and `total_processing_time_ms` per `(index_name, query, date, tenant_id)` composite key
- **Dual logging strategy** — `ICV_SEARCH_LOG_MODE` setting (`"individual"`, `"aggregate"`, `"both"`) controls whether `search()` writes to `SearchQueryLog`, `SearchQueryAggregate`, or both; defaults to `"individual"` for backwards compatibility
- **Sample rate control** — `ICV_SEARCH_LOG_SAMPLE_RATE` setting (0.0–1.0) controls what fraction of individual `SearchQueryLog` rows are written; aggregate counts are always recorded at 100%
- **`get_query_trend()`** service function — day-by-day trend for a specific query from `SearchQueryAggregate` rows; returns date, count, zero-result count, and average processing time per day
- **`clear_query_aggregates()`** service function — delete aggregate rows older than a given number of days (default 90)
- **`cleanup_search_query_aggregates`** Celery task — periodic cleanup of old aggregate rows (90-day default retention)
- **`SearchQueryAggregateFactory`** in `icv_search.testing` for test data
- **`SearchQueryAggregateAdmin`** — read-only admin with `date_hierarchy` and list filters
- Migration `0003_searchqueryaggregate` for the new aggregate model

### Changed

- Version bumped to 0.3.1
- `get_popular_queries()`, `get_zero_result_queries()`, and `get_search_stats()` now dispatch based on `ICV_SEARCH_LOG_MODE` — aggregate mode reads from `SearchQueryAggregate` sums instead of counting individual log rows
- Celery `shared_task` fallback (when Celery is not installed) now correctly handles `bind=True` tasks by injecting a fake task instance with a `retry()` method

## [0.3.0] - 2026-03-22

### Added

- **Search result highlighting** — `SearchResult.formatted_hits` and `get_highlighted_hits()` helper; pass `highlight_fields`, `highlight_pre_tag`, `highlight_post_tag` to `search()`; supported across all three backends (Meilisearch `_formatted`, PostgreSQL `ts_headline()`, DummyBackend substring wrapping)
- **Facet distribution on all backends** — DummyBackend and PostgreSQL now compute `facet_distribution` when `facets` param is passed; previously Meilisearch-only
- **Ranking scores** — `SearchResult.ranking_scores` list and `get_hit_with_score()` helper; Meilisearch extracts `_rankingScore`, PostgreSQL uses `ts_rank`, DummyBackend computes term-frequency scores; pass `show_ranking_score=True` to enable on Meilisearch
- **Matching strategy** — `matching_strategy` param on `search()` maps to Meilisearch's `matchingStrategy` (`"all"`, `"last"`, `"frequency"`)
- **Geo-distance search** — `geo_point`, `geo_radius`, `geo_sort` params on `search()`; Meilisearch uses native `_geoRadius`/`_geoPoint`, DummyBackend and PostgreSQL use Haversine calculation; `_geoDistance` preserved on hits; `SearchableMixin.search_geo_field`, `search_lat_field`, `search_lng_field` for automatic `_geo` document generation
- **Soft-delete awareness** — `SearchableMixin.get_search_queryset()` automatically excludes records with `is_deleted=True` or non-null `deleted_at`; `search_exclude_soft_deleted` attribute to opt out; auto-index `_handle_post_save` removes soft-deleted instances from the index
- **`get_task()` service function** — poll async engine task status, returns normalised `TaskResult`
- **`get_index_settings()` service function** — retrieve live engine-side settings for an index
- **`multi_search()` service function** — execute multiple queries in a single request; Meilisearch uses native `POST /multi-search`, other backends loop
- **Synonym management** — `get_synonyms()`, `update_synonyms()`, `reset_synonyms()` service functions
- **Stop-word management** — `get_stop_words()`, `update_stop_words()`, `reset_stop_words()` service functions
- **Typo tolerance management** — `get_typo_tolerance()`, `update_typo_tolerance()` service functions
- **`SearchQuery` fluent query builder** — chainable DSL: `.text()`, `.filter()`, `.sort()`, `.facets()`, `.highlight()`, `.geo_near()`, `.limit()`, `.offset()`, `.with_ranking_scores()`, `.matching_strategy()`, `.tenant()`, `.user()`, `.metadata()`, `.execute()`, `.paginate()`; exported at top level
- **Search analytics** — `SearchQueryLog` model for query logging; `ICV_SEARCH_LOG_QUERIES` and `ICV_SEARCH_LOG_ZERO_RESULTS_ONLY` settings; `get_popular_queries()`, `get_zero_result_queries()`, `get_search_stats()`, `clear_query_logs()` service functions; `cleanup_search_query_logs` Celery task; admin registration (read-only)
- **`ICVSearchTenantMiddleware`** — request-scoped tenant context via `threading.local()`; `get_current_tenant_id()` for automatic tenant injection in service functions; explicit `tenant_id` always takes precedence
- **`ICVSearchCache`** — optional search result cache layer using Django's cache framework; `ICV_SEARCH_CACHE_ENABLED`, `ICV_SEARCH_CACHE_TIMEOUT`, `ICV_SEARCH_CACHE_ALIAS` settings; automatic invalidation on `documents_indexed`/`documents_removed` signals
- **`SearchQueryLogFactory`** in testing utilities
- Migration `0002_searchquerylog` for the new analytics model

### Changed

- Version bumped to 0.3.0
- `search()` now accepts optional `user` and `metadata` keyword arguments for analytics logging
- `SearchResult.from_engine()` now strips `_rankingScore` from plain hits (alongside `_formatted`); `_geoDistance` is intentionally preserved
- `services/__init__.py` expanded from 15 to 33 exported functions
- Top-level `__init__.py` now exports `SearchQuery`

## [0.2.0] - 2026-03-22

### Added

- `ICVSearchPaginator` and `ICVSearchPage` — Django `Paginator` subclass for search results that uses `estimated_total_hits` instead of `queryset.count()`; includes `is_estimated` flag and `display_count()` helper for approximate result counts
- `facet_distribution` field on `SearchResult` — normalised from the engine response (`facetDistribution`/`facet_distribution`); no longer need to dig into `.raw`
- `SearchResult.get_facet_values(facet_name)` — convenience method returning facet values sorted by count descending
- Range filter support (`__gte`, `__gt`, `__lte`, `__lt`) in filter dicts — works across Meilisearch, PostgreSQL, and Dummy backends
- `/health/` JSON endpoint via `icv_search.urls` — returns `{"status": "ok"}` (200) or `{"status": "unavailable"}` (503) for load balancer probes
- `reindex_zero_downtime()` service function — creates a temporary index, populates it, then atomically swaps via Meilisearch's `/swap-indexes`; falls back to `reindex_all()` when the backend does not support swaps
- `reindex_zero_downtime_task` Celery task
- `swap_indexes()` method on `BaseSearchBackend` (optional, raises `NotImplementedError` by default), with implementations on `MeilisearchBackend` and `DummyBackend`
- `ICV_SEARCH_DEBOUNCE_SECONDS` setting — when > 0, batches rapid auto-index signals into a single `add_documents` call after the debounce window; uses Django cache for the buffer
- `flush_debounce_buffer` Celery task for draining the debounce buffer

### Changed

- Version bumped to 0.2.0
- Licence changed to MIT

## [0.1.0] - 2026-03-14

### Added

- Backend abstraction layer with `BaseSearchBackend`
- `MeilisearchBackend` — default backend using httpx directly
- `PostgresBackend` — zero-infrastructure full-text search using Django's built-in PostgreSQL search
- `DummyBackend` — in-memory backend for testing without external services
- `SearchIndex` and `IndexSyncLog` models with optional icv-core integration
- `SearchableMixin` for declaring Django models as indexable
- Auto-indexing via `ICV_SEARCH_AUTO_INDEX` configuration with `post_save`/`post_delete` signal handlers
- `skip_index_update()` context manager for bulk operations
- Index management services: `create_index()`, `delete_index()`, `update_index_settings()`, `get_index_stats()`
- Document services: `index_documents()`, `remove_documents()`, `index_model_instances()`, `reindex_all()`
- Search service with Django-native filter dicts and sort lists
- Normalised response types: `TaskResult`, `SearchResult`, `IndexStats`
- Multi-tenancy support via configurable tenant prefix callable
- Signals: `search_index_created`, `search_index_deleted`, `search_index_synced`, `documents_indexed`, `documents_removed`
- Celery integration for async indexing and periodic sync tasks
- Management commands: `icv_search_sync`, `icv_search_reindex`, `icv_search_health`, `icv_search_create_index`, `icv_search_clear`
- Testing utilities: `SearchIndexFactory`, `IndexSyncLogFactory`, pytest fixtures, test helpers
- Comprehensive test suite with 90%+ coverage
