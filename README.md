# django-icv-search

[![CI](https://github.com/nigelcopley/icv-oss/actions/workflows/ci.yml/badge.svg)](https://github.com/nigelcopley/icv-oss/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/django-icv-search)](https://pypi.org/project/django-icv-search/)
[![Python](https://img.shields.io/pypi/pyversions/django-icv-search)](https://pypi.org/project/django-icv-search/)
[![Django](https://img.shields.io/badge/django-5.1%2B-green)](https://pypi.org/project/django-icv-search/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

Search for Django without the lock-in. Point it at Meilisearch, PostgreSQL, or your own backend — the same service API, filters, and query builder work across all of them. Swap engines without rewriting application code.

Part of the [ICV-Django](https://github.com/nigelcopley/icv-oss) ecosystem.

---

## Why this over the alternatives?

**Over the raw Meilisearch SDK:** you get Django-native filters (`price__gte=10`), auto-indexing on model save, zero-downtime reindexing, Celery integration, search analytics, and a merchandising layer — none of which the SDK provides.

**Over django-haystack:** modern async-capable HTTP client (httpx), swappable backends without engine-specific query syntax, built-in multi-tenancy, and a merchandising pipeline for controlling result presentation — all with a much smaller footprint.

**Over both:** one consistent service API regardless of backend, normalised response dataclasses that insulate your code from engine-specific shapes, and a `DummyBackend` for fast, deterministic tests without a running search engine.

---

## Requirements

- Python 3.11+
- Django 5.1+

---

## Features

### Core
- Swappable backends modelled on Django's email backend pattern — change one setting, no application code changes
- Django-native filter dicts and sort lists translated to each engine's syntax automatically
- Normalised `SearchResult`, `TaskResult`, and `IndexStats` dataclasses insulate your code from engine-specific response shapes
- `SearchableMixin` — declare a model as indexable with a handful of class attributes
- Auto-indexing via `post_save` / `post_delete` signals; disable per-block with `skip_index_update()`
- `SearchQuery` fluent builder: `.text().filter().sort().facets().highlight().geo_near().execute()`
- `ICVSearchPaginator` — uses `estimated_total_hits` from the engine; no extra `COUNT` query

### Backends
- **Meilisearch** — default; uses `httpx` directly, keeping dependencies minimal
- **PostgreSQL** — zero-infrastructure full-text search using `tsvector` and `ts_rank`; no external service needed
- **DummyBackend** — in-memory backend for fast, deterministic tests
- **Custom backends** — subclass `BaseSearchBackend` and implement the abstract interface

### Indexing and index management
- Create, configure, sync, and delete indexes; Django is the source of truth
- Zero-downtime reindex — builds a temp index, then atomically swaps with the live one
- Celery integration for async indexing with exponential backoff; degrades gracefully to synchronous when Celery is absent
- Signal debouncing — batches rapid saves into a single indexing call
- Soft-delete awareness — auto-excludes soft-deleted records on reindex and removes them on save
- Management commands: `icv_search_setup`, `icv_search_health`, `icv_search_sync`, `icv_search_reindex`, `icv_search_create_index`, `icv_search_clear`

### Query features
- Facet distribution — normalised `facet_distribution` dict with `get_facet_values()` helper
- Range filters — `__gte`, `__gt`, `__lte`, `__lt` suffixes work across all backends
- Highlighting — `formatted_hits` with custom pre/post tags; native on Meilisearch, `ts_headline()` on PostgreSQL
- Ranking scores — `_rankingScore` on Meilisearch, `ts_rank` on PostgreSQL, term-frequency on Dummy
- Geo-distance search — filter and sort by proximity; native `_geoRadius` on Meilisearch, Haversine on others
- Multi-search — execute multiple queries in one request
- Synonym, stop-word, and typo-tolerance management

### Analytics
- `SearchQueryLog` per-query logging and `SearchQueryAggregate` daily rollups
- Three logging strategies: `individual`, `aggregate`, or `both`; sample rate control for high-traffic sites
- `get_popular_queries()`, `get_zero_result_queries()`, `get_search_stats()`, `get_query_trend()`
- `get_trending_searches()` and `get_suggested_queries()` from existing aggregate data — no external service needed

### Merchandising (optional)
- Query redirects, query rewrites, pinned results, boost rules, search banners, and zero-result fallbacks
- 9-step `merchandised_search()` pipeline; each step is individually skippable
- Rule scheduling with `starts_at` / `ends_at` windows; database-cached rule loading
- Django admin with bulk enable/disable actions and hit count tracking
- Gated behind `ICV_SEARCH_MERCHANDISING_ENABLED`; when disabled, `merchandised_search()` delegates directly to `search()`

### Infrastructure
- Multi-tenancy — tenant-prefixed index names via a configurable callable; no coupling to any tenant model
- Result caching via Django's cache framework; automatic invalidation on index changes
- Health check endpoint (`/health/`) for load balancer probes
- Django signals for index lifecycle events (`search_index_created`, `documents_indexed`, etc.)
- `icv_search.testing` — fixtures, factories, and helpers for consuming projects

---

## Installation

### Basic

```bash
pip install django-icv-search
```

Add to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    # ...
    "icv_search",
]
```

Run migrations:

```bash
python manage.py migrate icv_search
```

### With icv-core

Installing with the `icv-core` extra gives you `BaseModel` (UUID primary key plus `created_at` / `updated_at` timestamps) from [icv-core](https://github.com/nigelcopley/icv-oss):

```bash
pip install "django-icv-search[icv-core]"
```

```python
INSTALLED_APPS = [
    # ...
    "icv_core",
    "icv_search",
]
```

Both `SearchIndex` and `IndexSyncLog` inherit from `icv_core.models.BaseModel` automatically when `icv_core` is present.

---

## Quick Start

```python
# settings.py
ICV_SEARCH_BACKEND = "icv_search.backends.meilisearch.MeilisearchBackend"
ICV_SEARCH_URL = "http://localhost:7700"
ICV_SEARCH_API_KEY = "your-meilisearch-master-key"

# 1. Make your model searchable
# myapp/models.py
from django.db import models
from icv_search.mixins import SearchableMixin

class Article(SearchableMixin, models.Model):
    search_index_name = "articles"
    search_fields = ["title", "body"]
    search_filterable_fields = ["published", "author_id"]
    search_sortable_fields = ["published_at", "title"]

    title = models.CharField(max_length=200)
    body = models.TextField()
    author_id = models.IntegerField()
    published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True)

# 2. Create the index (run once, e.g. in a migration or management command)
from icv_search.services import create_index
index = create_index("articles", model_class=Article)

# 3. Index documents
from icv_search.services import index_documents
index_documents("articles", [
    {"id": "1", "title": "Django tips", "body": "...", "published": True},
    {"id": "2", "title": "Search patterns", "body": "...", "published": True},
])

# 4. Search
from icv_search.services import search
results = search("articles", "django", limit=10)
for hit in results.hits:
    print(hit["title"])
```

---

## Configuration

### Settings Reference

All settings are namespaced under `ICV_SEARCH_*`. Every setting has a sensible default so the package works out of the box for local development.

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `ICV_SEARCH_BACKEND` | `str` | `"icv_search.backends.meilisearch.MeilisearchBackend"` | Dotted path to the active search backend class |
| `ICV_SEARCH_URL` | `str` | `"http://localhost:7700"` | Search engine base URL |
| `ICV_SEARCH_API_KEY` | `str` | `""` | Master or admin API key for the search engine |
| `ICV_SEARCH_TIMEOUT` | `int` | `30` | Request timeout in seconds for all backend calls |
| `ICV_SEARCH_TENANT_PREFIX_FUNC` | `str` | `""` | Dotted path to a callable `(request_or_none) -> str` returning the tenant prefix. Empty string disables multi-tenancy |
| `ICV_SEARCH_AUTO_SYNC` | `bool` | `True` | Automatically push index settings to the engine when a `SearchIndex` record is saved |
| `ICV_SEARCH_ASYNC_INDEXING` | `bool` | `True` | Use Celery for document indexing. Falls back to synchronous when Celery is unavailable |
| `ICV_SEARCH_INDEX_PREFIX` | `str` | `""` | Global prefix applied to all engine index names (e.g. `"staging_"` to segregate environments) |
| `ICV_SEARCH_AUTO_INDEX` | `dict` | `{}` | Automatic model-level indexing configuration. See below |
| `ICV_SEARCH_DEBOUNCE_SECONDS` | `int` | `0` | Debounce window for auto-index signal batching. Requires Django's cache framework. `0` disables debouncing |
| `ICV_SEARCH_LOG_QUERIES` | `bool` | `False` | Log every `search()` call to `SearchQueryLog` |
| `ICV_SEARCH_LOG_ZERO_RESULTS_ONLY` | `bool` | `False` | When `True`, only zero-result queries are logged |
| `ICV_SEARCH_LOG_MODE` | `str` | `"individual"` | Logging strategy: `"individual"`, `"aggregate"`, or `"both"` |
| `ICV_SEARCH_LOG_SAMPLE_RATE` | `float` | `1.0` | Fraction of individual `SearchQueryLog` rows to write (0.0–1.0). Aggregate counts always record at 100% |
| `ICV_SEARCH_CACHE_ENABLED` | `bool` | `False` | Enable search result caching via Django's cache framework |
| `ICV_SEARCH_CACHE_TIMEOUT` | `int` | `60` | Cache TTL in seconds for stored search results |
| `ICV_SEARCH_CACHE_ALIAS` | `str` | `"default"` | Django cache alias used by the search result cache |
| `ICV_SEARCH_MERCHANDISING_ENABLED` | `bool` | `False` | Enable the merchandising layer |
| `ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT` | `int` | `300` | Cache TTL in seconds for merchandising rules loaded from the database |

### Auto-Indexing Configuration

`ICV_SEARCH_AUTO_INDEX` wires `post_save` and `post_delete` signal handlers automatically for any model you declare. The package's `AppConfig.ready()` reads this setting and connects the handlers on startup.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `model` | `str` | required | `"app_label.ModelName"` — the Django model to watch |
| `on_save` | `bool` | `True` | Index the document when the model instance is saved |
| `on_delete` | `bool` | `True` | Remove the document when the model instance is deleted |
| `async` | `bool` | from `ICV_SEARCH_ASYNC_INDEXING` | Override async behaviour for this index only |
| `auto_create` | `bool` | `True` | Create the `SearchIndex` record and engine index if they do not yet exist |
| `should_update` | `str` | `""` | Dotted path to a callable `(instance) -> bool`. Document is only indexed when the callable returns `True` |

```python
ICV_SEARCH_AUTO_INDEX = {
    "articles": {
        "model": "blog.Article",
        "on_save": True,
        "on_delete": True,
        "async": True,
        "should_update": "blog.search.should_index_article",
    },
    "products": {
        "model": "catalogue.Product",
        "async": False,  # Synchronous for this index
    },
}
```

```python
# blog/search.py
def should_index_article(instance) -> bool:
    """Only index published articles."""
    return instance.published
```

---

## SearchableMixin

Add `SearchableMixin` to any Django model to make it indexable. Declare the index configuration as class attributes.

```python
from django.db import models
from icv_search.mixins import SearchableMixin

class Product(SearchableMixin, models.Model):
    search_index_name = "products"
    search_fields = ["name", "description", "sku"]
    search_filterable_fields = ["category_id", "is_active", "price"]
    search_sortable_fields = ["price", "created_at", "name"]

    name = models.CharField(max_length=200)
    description = models.TextField()
    sku = models.CharField(max_length=50, unique=True)
    category_id = models.IntegerField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
```

### Customising the document representation

Override `to_search_document()` to control exactly what is sent to the engine:

```python
def to_search_document(self) -> dict:
    return {
        "id": str(self.id),
        "name": self.name,
        "description": self.description,
        "sku": self.sku,
        "price": float(self.price),          # Decimal -> float for JSON
        "category_id": self.category_id,
        "is_active": self.is_active,
        "category_name": self.category.name, # Denormalised for search
    }
```

### Customising the reindex queryset

Override `get_search_queryset()` to control which records are included in a full reindex and to add `select_related` / `prefetch_related` for performance:

```python
@classmethod
def get_search_queryset(cls):
    return (
        cls.objects
        .filter(is_active=True)
        .select_related("category")
    )
```

---

## Service API

Import service functions from `icv_search.services`:

```python
from icv_search.services import (
    # Index management
    create_index, delete_index, update_index_settings,
    get_index_settings, get_index_stats,
    # Synonym / stop-word / typo management
    get_synonyms, update_synonyms, reset_synonyms,
    get_stop_words, update_stop_words, reset_stop_words,
    get_typo_tolerance, update_typo_tolerance,
    # Document operations
    index_documents, remove_documents,
    index_model_instances, reindex_all, reindex_zero_downtime,
    # Search
    search, multi_search, get_task,
    # Analytics
    get_popular_queries, get_zero_result_queries,
    get_search_stats, get_query_trend,
    clear_query_logs, clear_query_aggregates,
    # Utilities
    get_current_tenant_id, ICVSearchCache,
)
```

### Index Management

#### `create_index`

Creates a `SearchIndex` record, provisions the index in the engine, and pushes any settings. If `model_class` is provided and uses `SearchableMixin`, its field lists seed the index settings automatically.

```python
from icv_search.services import create_index
from myapp.models import Product

index = create_index(
    name="products",
    model_class=Product,
    settings={"rankingRules": ["words", "typo", "proximity"]},
)
```

#### `delete_index`

Deletes the `SearchIndex` record from Django and removes the index from the engine. Raises `SearchBackendError` on engine failure.

```python
from icv_search.services import delete_index
delete_index("products")
```

#### `update_index_settings`

Merges settings into the existing index, saves to Django, and syncs to the engine:

```python
from icv_search.services import update_index_settings
update_index_settings("products", {
    "synonyms": {"phone": ["mobile", "handset"]},
})
```

#### `get_index_stats`

Returns a normalised `IndexStats` dataclass with live data from the engine:

```python
from icv_search.services import get_index_stats
stats = get_index_stats("products")
print(stats.document_count)
print(stats.is_indexing)
```

### Document Operations

#### `index_documents`

```python
from icv_search.services import index_documents
result = index_documents("products", [
    {"id": "abc123", "name": "Widget", "price": 9.99},
    {"id": "def456", "name": "Gadget", "price": 24.99},
])
print(result.task_uid)
```

#### `remove_documents`

```python
from icv_search.services import remove_documents
remove_documents("products", ["abc123", "def456"])
```

#### `index_model_instances`

Indexes model instances using their `SearchableMixin` configuration. Iterates the queryset in batches. Returns the total documents indexed.

```python
from icv_search.services import index_model_instances
from myapp.models import Product

count = index_model_instances(Product, batch_size=500)
print(f"Indexed {count} products")
```

#### `reindex_all`

Full reindex: clears all existing documents, then re-indexes from `get_search_queryset()`.

```python
from icv_search.services import reindex_all
total = reindex_all("products", Product, batch_size=500)
```

#### `reindex_zero_downtime`

Creates a temporary index, populates it from the model queryset, then atomically swaps with the live index. Falls back to `reindex_all()` if the backend does not support index swaps.

```python
from icv_search.services import reindex_zero_downtime
total = reindex_zero_downtime("products", Product, batch_size=500)
```

### Search

Filters and sort orders use Django-native syntax. The service layer translates them to each engine's format automatically, so the same calling code works across all backends.

```python
from icv_search.services import search

# Django-native filter dict and sort list
result = search(
    "products",
    "padel",
    filter={"city": "Madrid", "is_active": True, "price__lt": 200},
    sort=["-created_at"],
    limit=10,
)

for hit in result.hits:
    print(hit["name"], hit["price"])

print(f"About {result.estimated_total_hits} results")
```

### Pagination

`ICVSearchPaginator` uses `estimated_total_hits` as the count instead of running a separate `queryset.count()` query:

```python
from icv_search import ICVSearchPaginator
from icv_search.services import search

page_number = int(request.GET.get("page", 1))
per_page = 25
result = search("products", query, limit=per_page, offset=(page_number - 1) * per_page)

paginator = ICVSearchPaginator(result, per_page=per_page)
page_obj = paginator.get_page(page_number)
```

```html
{% for hit in page_obj %}{{ hit.name }}{% endfor %}

{% if page_obj.is_estimated %}
    {{ page_obj.display_count }} results
{% else %}
    {{ page_obj.paginator.count }} results
{% endif %}
```

### Facets

```python
result = search("products", "shoes", facets=["brand", "colour"])
print(result.facet_distribution)
# {"brand": {"Nike": 42, "Adidas": 31}, "colour": {"black": 55, "white": 28}}

for facet in result.get_facet_values("brand"):
    print(f"{facet['name']}: {facet['count']}")
```

### Range Filters

Use Django-style lookup suffixes for numeric range queries. Supported suffixes: `__gte` (>=), `__gt` (>), `__lte` (<=), `__lt` (<). Works across all backends.

```python
result = search("products", "",
    filter={"price__gte": 10, "price__lte": 100, "is_active": True},
)
```

### Bulk Operations

`skip_index_update` is a context manager that temporarily disables auto-indexing signal handlers. Use it in bulk imports, data migrations, and test factories.

```python
from icv_search.auto_index import skip_index_update

articles = [Article(title=f"Article {i}") for i in range(1000)]

with skip_index_update():
    Article.objects.bulk_create(articles)

# Trigger a single reindex after the bulk operation
from icv_search.services import reindex_all
reindex_all("articles", Article)
```

The context manager is nestable. Auto-indexing resumes when the outermost `with` block exits.

### Highlighting

```python
result = search("articles", "django tips",
    highlight_fields=["title", "body"],
    highlight_pre_tag="<mark>",
    highlight_post_tag="</mark>",
)

for hit in result.get_highlighted_hits():
    print(hit["title"])  # "...about <mark>Django</mark> <mark>tips</mark>..."
```

Works across all backends: Meilisearch uses native `_formatted`, PostgreSQL uses `ts_headline()`, DummyBackend wraps matching substrings.

### Ranking Scores

```python
result = search("products", "shoes", show_ranking_score=True)

for i, hit in enumerate(result.hits):
    hit, score = result.get_hit_with_score(i)
    print(f"{hit['name']}: {score:.2f}")
```

Meilisearch returns `_rankingScore` (0.0–1.0), PostgreSQL uses `ts_rank`, DummyBackend computes term-frequency.

### Geo-Distance Search

```python
result = search("restaurants", "",
    geo_point=(51.5074, -0.1278),  # London (lat, lng)
    geo_radius=5000,                # metres
    geo_sort="asc",                 # nearest first
)

for hit in result.hits:
    print(f"{hit['name']}: {hit.get('_geoDistance')}m away")
```

Declare geo fields on the mixin:

```python
class Restaurant(SearchableMixin, models.Model):
    search_index_name = "restaurants"
    search_fields = ["name", "cuisine"]
    search_lat_field = "latitude"
    search_lng_field = "longitude"

    latitude = models.FloatField()
    longitude = models.FloatField()
```

### Multi-Search

```python
from icv_search.services import multi_search

results = multi_search([
    {"index_name": "products", "query": "shoes", "limit": 5},
    {"index_name": "articles", "query": "shoes", "limit": 3, "facets": ["category"]},
])

product_results, article_results = results
```

Meilisearch uses the native `POST /multi-search` endpoint. Other backends execute queries sequentially.

### Synonym and Stop-Word Management

```python
from icv_search.services import (
    get_synonyms, update_synonyms, reset_synonyms,
    get_stop_words, update_stop_words, reset_stop_words,
    get_typo_tolerance, update_typo_tolerance,
)

update_synonyms("products", {"phone": ["mobile", "handset"], "laptop": ["notebook"]})
update_stop_words("products", ["the", "a", "an"])
update_typo_tolerance("products", {"enabled": True, "minWordSizeForTypos": {"oneTypo": 4}})
```

### SearchQuery Builder

A fluent API for constructing search queries:

```python
from icv_search import SearchQuery

results = (
    SearchQuery("products")
    .text("running shoes")
    .filter(brand="Nike", price__gte=50)
    .sort("-price", "name")
    .facets("brand", "category")
    .highlight("name", "description")
    .geo_near(lat=51.5, lng=-0.12, radius=5000)
    .with_ranking_scores()
    .limit(20)
    .execute()
)

# Or get a paginator directly
paginator = SearchQuery("products").text("shoes").limit(25).paginate()
page = paginator.get_page(1)
```

### Search Analytics

Enable query logging to track search behaviour:

```python
# settings.py
ICV_SEARCH_LOG_QUERIES = True
ICV_SEARCH_LOG_MODE = "individual"  # or "aggregate" or "both"
```

| Mode | Storage | Best for |
|------|---------|----------|
| `"individual"` | One `SearchQueryLog` row per query | Low/medium traffic — full query history |
| `"aggregate"` | Daily rollups in `SearchQueryAggregate` | High traffic — compact long-term storage |
| `"both"` | Both individual rows and daily rollups | Detailed recent logs plus long-term trends |

```python
from icv_search.services import (
    get_popular_queries,
    get_zero_result_queries,
    get_search_stats,
    get_query_trend,
)

# Most frequent queries in the last 7 days
popular = get_popular_queries("products", days=7, limit=20)

# Queries returning no results — find content gaps
gaps = get_zero_result_queries("products", days=7)

# Aggregate stats
stats = get_search_stats("products", days=7)
# {"total_queries": 1234, "avg_processing_time_ms": 12, "zero_result_rate": 0.05}

# Day-by-day trend
trend = get_query_trend("running shoes", "products", days=30)
```

All analytics functions read from the correct model automatically based on `ICV_SEARCH_LOG_MODE`.

### Tenant Middleware

Auto-inject tenant context from the request instead of passing `tenant_id` on every call:

```python
# settings.py
MIDDLEWARE = [
    # ...
    "icv_search.middleware.ICVSearchTenantMiddleware",
]
ICV_SEARCH_TENANT_PREFIX_FUNC = "myproject.search.get_tenant_prefix"
```

```python
# In a view — tenant_id is injected automatically
results = search("products", "widget")

# Explicit tenant_id always takes precedence
results = search("products", "widget", tenant_id="other_tenant")
```

### Search Result Cache

Enable caching to reduce backend load for repeated queries:

```python
ICV_SEARCH_CACHE_ENABLED = True
ICV_SEARCH_CACHE_TIMEOUT = 60       # seconds
ICV_SEARCH_CACHE_ALIAS = "default"  # Django cache alias
```

Cache is automatically invalidated when documents are indexed or removed.

---

## Merchandising

The merchandising layer lets non-technical users control what shoppers see in search results. It is entirely optional and gated behind a single feature flag.

### Enabling

```python
ICV_SEARCH_MERCHANDISING_ENABLED = True
ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT = 300  # seconds
```

When disabled (the default), `merchandised_search()` is a thin wrapper around `search()` that returns a `MerchandisedSearchResult` with no merchandising metadata.

### Rule Types

All merchandising models inherit from `MerchandisingRuleBase`, which provides query matching, scheduling (`starts_at` / `ends_at`), priority ordering, and `hit_count` tracking.

| Rule | What it does |
|------|-------------|
| `QueryRedirect` | Redirect a search query to a URL instead of showing results |
| `QueryRewrite` | Transparently rewrite the query before execution, and optionally inject filters or sort |
| `SearchPin` | Pin a document to a fixed position (or use `-1` to bury it) |
| `BoostRule` | Multiply ranking scores based on a field value comparison |
| `SearchBanner` | Attach a banner (title, content, image, CTA) to search results |
| `ZeroResultFallback` | Define what to show when a query returns nothing — redirect, alternative query, curated results, or popular-in-category |

### The Pipeline

`merchandised_search()` composes all features into a 9-step pipeline:

```
1. Feature gate     — disabled? delegate to search()
2. Normalise query  — strip, collapse whitespace, lowercase
3. Redirect check   — short-circuit with URL if matched
4. Query rewrite    — replace query, merge filters/sort
5. Search           — call search() with the (possibly rewritten) query
6. Pin insertion    — insert/move pinned documents
7. Boost re-rank    — multiply scores, re-sort
8. Fallback         — zero results? try alternative strategy
9. Banner attach    — attach matching banners to the result
```

Each step is individually skippable:

```python
from icv_search.services import merchandised_search

result = merchandised_search(
    "products",
    "red shoes",
    tenant_id="acme",
    skip_redirects=True,
    skip_boosts=True,
    limit=20,
)
```

### Full Pipeline Example

```python
result = merchandised_search("products", "trainers", tenant_id="acme")

if result.redirect:
    return HttpResponseRedirect(result.redirect["url"])

print(result.original_query)   # "trainers"
print(result.was_rewritten)    # True if a rewrite rule matched
print(result.is_fallback)      # True if zero-result fallback triggered
print(result.applied_rules)    # Audit trail of every rule that fired

for banner in result.banners:
    print(banner["title"], banner["position"])

for hit in result.hits:
    if hit.get("_pinned"):
        print(f"[{hit.get('_pin_label', 'pinned')}]", hit["id"])
    else:
        print(hit["id"])
```

### Search Suggestions

`get_trending_searches()` and `get_suggested_queries()` derive from existing `SearchQueryAggregate` data — no external service needed:

```python
from icv_search.services import get_trending_searches, get_suggested_queries

trending = get_trending_searches("products", days=1, limit=10)
# [{"query": "iphone", "count": 342}, ...]

suggestions = get_suggested_queries("products", "iph", limit=5)
# [{"query": "iphone", "count": 342}, {"query": "iphone case", "count": 87}, ...]
```

### Admin

All six merchandising models are registered in the Django admin with list display, filters, search, scheduling fieldsets, bulk enable/disable actions, and read-only `hit_count` tracking.

---

## Response Types

All service functions return normalised dataclasses, insulating your code from engine-specific response shapes.

### `SearchResult`

```python
@dataclass
class SearchResult:
    hits: list[dict]
    query: str
    processing_time_ms: int
    estimated_total_hits: int
    limit: int
    offset: int
    facet_distribution: dict[str, dict[str, int]]
    formatted_hits: list[dict]
    ranking_scores: list[float | None]
    raw: dict

    def get_highlighted_hits(self) -> list[dict]: ...
    def get_facet_values(facet_name: str) -> list[dict]: ...
    def get_hit_with_score(index: int) -> tuple[dict, float | None]: ...
```

### `TaskResult`

Returned by document indexing and deletion operations:

```python
@dataclass
class TaskResult:
    task_uid: str    # Engine-assigned task identifier
    status: str      # e.g. "enqueued", "succeeded"
    detail: str      # Operation type or description
    raw: dict        # Original engine response
```

### `IndexStats`

```python
@dataclass
class IndexStats:
    document_count: int
    is_indexing: bool
    field_distribution: dict[str, int]
    raw: dict
```

### `MerchandisedSearchResult`

Extends `SearchResult` with:

| Field | Type | Description |
|-------|------|-------------|
| `redirect` | `Any \| None` | Redirect dict with `url`, `status`, and `type`. Present when a redirect matched |
| `banners` | `list` | Banner dicts (title, content, image_url, link_url, position, etc.) |
| `applied_rules` | `list[dict]` | Audit trail of every rule that fired |
| `original_query` | `str` | The query before any rewrite |
| `was_rewritten` | `bool` | `True` when a rewrite rule modified the query |
| `is_fallback` | `bool` | `True` when results came from a zero-result fallback |

---

## Backends

### Meilisearch (default)

Requires a running Meilisearch instance (v1.0+). Uses `httpx` directly rather than the official SDK, keeping dependencies minimal.

```python
ICV_SEARCH_BACKEND = "icv_search.backends.meilisearch.MeilisearchBackend"
ICV_SEARCH_URL = "http://localhost:7700"
ICV_SEARCH_API_KEY = "your-master-key"
```

### PostgreSQL (zero infrastructure)

Uses Django's built-in `django.contrib.postgres.search` with `tsvector` indexing. No external services required. Supports full-text search with `ts_rank`, Django-native filters and sorts.

Best for projects that want search without running Meilisearch, or as a starting point before upgrading to a dedicated engine.

```python
ICV_SEARCH_BACKEND = "icv_search.backends.postgres.PostgresBackend"
# ICV_SEARCH_URL and ICV_SEARCH_API_KEY are ignored by this backend.
```

### DummyBackend (testing)

An in-memory backend that stores documents in module-level dicts. Supports basic substring search, limit, and offset. No running search engine required.

```python
# tests/settings.py
ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
ICV_SEARCH_ASYNC_INDEXING = False  # Keep tests synchronous
```

### Writing a Custom Backend

Subclass `BaseSearchBackend` and implement the abstract interface:

```python
from icv_search.backends.base import BaseSearchBackend

class MyBackend(BaseSearchBackend):

    def __init__(self, url: str, api_key: str, timeout: int = 30, **kwargs):
        super().__init__(url=url, api_key=api_key, timeout=timeout, **kwargs)

    def create_index(self, uid: str, primary_key: str = "id") -> dict: ...
    def delete_index(self, uid: str) -> None: ...
    def update_settings(self, uid: str, settings: dict) -> dict: ...
    def get_settings(self, uid: str) -> dict: ...
    def add_documents(self, uid: str, documents: list[dict], primary_key: str = "id") -> dict: ...
    def delete_documents(self, uid: str, document_ids: list[str]) -> dict: ...
    def clear_documents(self, uid: str) -> dict: ...
    def search(self, uid: str, query: str, **params) -> dict: ...
    def get_stats(self, uid: str) -> dict: ...
    def health(self) -> bool: ...
```

Raise `icv_search.exceptions.SearchBackendError` on failure so the service layer handles errors consistently.

```python
ICV_SEARCH_BACKEND = "myproject.search_backends.MyBackend"
```

---

## Management Commands

| Command | Purpose |
|---------|---------|
| `icv_search_setup [--dry-run]` | Recommended first step. Creates `SearchIndex` records for all entries in `ICV_SEARCH_AUTO_INDEX`, syncs settings to the engine, verifies connectivity |
| `icv_search_health [--verbose]` | Check engine connectivity; `--verbose` prints per-index document counts and sync status |
| `icv_search_sync [--index NAME] [--force] [--tenant TENANT]` | Push index settings from Django to the engine |
| `icv_search_reindex --index NAME --model DOTTED.PATH [--batch-size N] [--tenant TENANT]` | Clear and re-index from `get_search_queryset()` in batches |
| `icv_search_create_index --name NAME [--primary-key FIELD] [--tenant TENANT]` | Create a `SearchIndex` record and provision it in the engine |
| `icv_search_clear --index NAME [--tenant TENANT]` | Remove all documents from an index without deleting it |

```bash
python manage.py icv_search_setup
python manage.py icv_search_setup --dry-run
python manage.py icv_search_health --verbose
python manage.py icv_search_sync --index products --force
python manage.py icv_search_reindex --index products --model myapp.models.Product --batch-size 500
python manage.py icv_search_clear --index products
```

`SearchIndex` records are also auto-created on first use — calling `search("products", "shoes")` creates the record if it does not exist. `icv_search_setup` is the recommended way to provision indexes explicitly during deployment.

---

## Celery Tasks

Celery is optional. When not installed, all operations run synchronously. When installed with `ICV_SEARCH_ASYNC_INDEXING = True`, operations are dispatched as background tasks with exponential backoff (maximum three retries).

| Task | Purpose |
|------|---------|
| `sync_index_settings` | Push settings for one index |
| `sync_all_indexes` | Sync all unsynced active indexes (periodic, every 5 min) |
| `add_documents` | Add or update documents |
| `remove_documents` | Remove documents |
| `reindex` | Full reindex from model queryset |
| `reindex_zero_downtime_task` | Zero-downtime reindex via index swap |
| `flush_debounce_buffer` | Drain debounce buffer and batch-index buffered documents |
| `refresh_document_counts` | Refresh cached document counts from engine stats (periodic, hourly) |
| `cleanup_search_query_logs` | Delete old query log entries (periodic, daily) |
| `cleanup_search_query_aggregates` | Delete old aggregate rows (periodic, daily) |

```python
from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    "icv-search-sync-all": {
        "task": "icv_search.tasks.sync_all_indexes",
        "schedule": crontab(minute="*/5"),
    },
    "icv-search-refresh-counts": {
        "task": "icv_search.tasks.refresh_document_counts",
        "schedule": crontab(minute=0),
    },
    "icv-search-cleanup-query-logs": {
        "task": "icv_search.tasks.cleanup_search_query_logs",
        "schedule": crontab(hour=3, minute=0),
    },
    "icv-search-cleanup-query-aggregates": {
        "task": "icv_search.tasks.cleanup_search_query_aggregates",
        "schedule": crontab(hour=3, minute=15),
    },
}
```

---

## Signals

All signals are defined in `icv_search.signals`.

| Signal | When |
|--------|------|
| `search_index_created` | After a new index is created and provisioned |
| `search_index_deleted` | After an index is deleted from Django and the engine |
| `search_index_synced` | After settings are pushed to the engine successfully |
| `documents_indexed` | After documents are added or updated (`count`, `document_ids` kwargs) |
| `documents_removed` | After documents are removed (`count`, `document_ids` kwargs) |

```python
from django.dispatch import receiver
from icv_search.signals import documents_indexed
from icv_search.models import SearchIndex

@receiver(documents_indexed, sender=SearchIndex)
def on_documents_indexed(sender, instance, count, document_ids, **kwargs):
    print(f"{count} documents indexed in '{instance.name}'")
```

---

## Testing

### Using DummyBackend

```python
# tests/settings.py
ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
ICV_SEARCH_ASYNC_INDEXING = False  # Synchronous so assertions work immediately
```

### Fixtures and Factories

`icv_search.testing` provides ready-made fixtures and factories for consuming projects:

```python
# conftest.py
from icv_search.testing.fixtures import search_backend, search_index  # noqa: F401
```

| Fixture | What it does |
|---------|-------------|
| `search_backend` | Configures `DummyBackend`, resets state before and after the test |
| `search_index` | Creates a `SearchIndex` instance via `SearchIndexFactory` |
| `merchandising_enabled` | Sets `MERCHANDISING_ENABLED=True` and `MERCHANDISING_CACHE_TIMEOUT=0` for the test scope |

Factories in `icv_search.testing.factories`: `SearchIndexFactory`, `IndexSyncLogFactory`, `SearchQueryAggregateFactory`, `QueryRedirectFactory`, `QueryRewriteFactory`, `SearchPinFactory`, `BoostRuleFactory`, `SearchBannerFactory`, `ZeroResultFallbackFactory`.

### Asserting Documents

```python
from icv_search.testing.helpers import (
    get_indexed_documents,
    assert_document_indexed,
)

def test_product_indexed(db, search_backend):
    product = ProductFactory()
    assert_document_indexed("products", str(product.pk))

def test_all_products_indexed(db, search_backend):
    ProductFactory.create_batch(5)
    docs = get_indexed_documents("products")
    assert len(docs) == 5
```

### `skip_index_update` in Factories

Use `skip_index_update()` in test factories to prevent auto-index noise when creating supporting data:

```python
import factory
from icv_search.auto_index import skip_index_update

class ArticleFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Article

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        with skip_index_update():
            return super()._create(model_class, *args, **kwargs)
```

---

## Multi-Tenancy

Tenant-prefixed index names via a configurable callable. No foreign key to a tenant model — no coupling to any specific tenant implementation.

```python
# myproject/search.py
def get_tenant_prefix(request_or_none) -> str:
    if request_or_none and hasattr(request_or_none, "tenant"):
        return request_or_none.tenant.slug
    return ""

# settings.py
ICV_SEARCH_TENANT_PREFIX_FUNC = "myproject.search.get_tenant_prefix"
```

`engine_uid` is computed as:

```
{ICV_SEARCH_INDEX_PREFIX}{tenant_id}_{name}

"staging_acme_products"   # prefix="staging_", tenant="acme", name="products"
"acme_products"           # no prefix, tenant="acme", name="products"
"products"                # single-tenant
```

Omit `ICV_SEARCH_TENANT_PREFIX_FUNC` for single-tenant deployments.

---

## Roadmap

- SQLite FTS5 backend
- MySQL FULLTEXT backend
- Async (`httpx.AsyncClient`) support for ASGI applications
- Typesense backend
- Search result click-through tracking
- A/B testing for ranking rules
- PostGIS-backed geo search

---

## Licence

MIT
