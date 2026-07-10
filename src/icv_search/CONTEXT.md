# icv-search: Context

## Role
Pluggable search engine integration layer for Django. Manages search index lifecycle,
document indexing, query execution, search merchandising, click tracking, and search
intelligence through a backend abstraction modelled after Django's cache and email
backend pattern.

## Architecture Layer
Domain (Layer 2)

## Key Models

| Model | Purpose |
|-------|---------|
| `SearchIndex` | Index configuration and sync state; maps a logical index name to backend settings |
| `IndexSyncLog` | Audit trail for index create/configure/delete operations against the backend |
| `SearchQueryLog` | Individual search query record; FK to `AUTH_USER_MODEL` (nullable) |
| `SearchQueryAggregate` | Daily rollup of query volume and zero-result counts per query string |
| `SearchClick` | Individual click-through event linking a query to a document position |
| `SearchClickAggregate` | Daily rollup of clicks per query/document pair |
| `MerchandisingRuleBase` | Abstract base for all merchandising rules; provides scheduling and query-matching fields |
| `QueryRedirect` | Routes matching queries to a destination URL instead of executing search |
| `QueryRewrite` | Transforms the query string and/or auto-applies filters before engine execution |
| `SearchPin` | Forces specific documents to fixed positions or excludes them from results |
| `BoostRule` | Adjusts result ranking based on document attributes and query context |
| `SearchBanner` | Attaches promotional content blocks to matching search queries |
| `ZeroResultFallback` | Defines recovery strategies when a query returns no results |

## Services
16 modules, 40+ exported functions: `search`, `documents`, `indexing`,
`merchandising`, `redirects`, `rewrites`, `pins`, `boosts`, `banners`,
`fallbacks`, `suggestions`, `click_tracking`, `analytics`, `intelligence`,
`preprocessing`, `discovery`.

All services are module-level functions, re-exported via `services/__init__.py`
with `__all__`.

## Dependencies
- `APP-001 icv-core`: optional; provides `BaseModel` (UUID PK + timestamps). When
  absent, a bundled equivalent is used. Install via `pip install django-icv-search[icv-core]`
- `django.contrib.auth`: `settings.AUTH_USER_MODEL` FK on `SearchQueryLog` (nullable)

## Consumed By
All consuming projects needing search. No other `icv-*` packages depend on
icv-search: consuming projects wire search into their own domains.

## Specs & Docs
- APP spec: `docs/specs/django-icv-search/README.md`
- Sub-feature specs: `docs/specs/django-icv-search/features/` (FEAT-001 through FEAT-012)

## Current Status
v0.8.0: 1219 tests passing. Three bundled backends: Meilisearch (default),
PostgreSQL (zero-infrastructure), and Dummy (testing). Four additional backends
are planned (OpenSearch, Solr, Typesense, Vespa, each as an optional SDK extra).
Full merchandising pipeline implemented. Search intelligence includes click
tracking, demand signal extraction, query clustering, and auto-synonym suggestion.

## Conventions Specific to This App

- **Settings are namespaced `ICV_SEARCH_*`**: all package settings carry this
  prefix; see the settings reference in the APP-013 spec.
- **Backend is swappable via `ICV_SEARCH_BACKEND`**: the dotted-path setting
  follows Django's cache/email backend pattern. Never hard-code a backend class.
- **Merchandising is opt-in**: models and the pipeline are only active when
  `ICV_SEARCH_MERCHANDISING_ENABLED = True` and migrations have been run. Do not
  assume merchandising tables exist unless the setting is confirmed.
- **Multi-tenancy via `ICV_SEARCH_TENANT_PREFIX_FUNC`**: a configurable callable
  returns a per-request tenant string prepended to index names. Single-tenant
  deployments leave this empty; the app must not assume either mode.
- **icv-core is optional**: the `models/base.py` module resolves `BaseModel`
  at import time: icv-core's `BaseModel` if installed, otherwise the bundled
  fallback. Do not import `icv_core.models` directly from within this package.
- **Auto-indexing via signal wiring, not `SearchableMixin` alone**: the
  `ICV_SEARCH_AUTO_INDEX` dict drives signal registration in `auto_index.py`;
  `SearchableMixin` declares the index mapping on the model but does not connect
  signals itself.
- **Async indexing requires Celery**: `ICV_SEARCH_ASYNC_INDEXING = True`
  (default) dispatches indexing operations as Celery tasks. The package falls back
  to synchronous indexing when Celery is unavailable; tests should use the Dummy
  backend or `skip_index_update()` to avoid hard Celery dependencies.
