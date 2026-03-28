# django-icv-search

[![PyPI](https://img.shields.io/pypi/v/django-icv-search)](https://pypi.org/project/django-icv-search/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

Pluggable search engine integration for Django — index management, document
indexing, and search queries with swappable backends.

Part of the [ICV-Django](https://github.com/nigelcopley/icv-oss) ecosystem.

---

## Features

- **Backend abstraction** — swappable search backends modelled after Django's
  email backend pattern; swap Meilisearch for any engine by pointing a setting
  at your own `BaseSearchBackend` subclass
- **Meilisearch backend** — default implementation using `httpx` directly,
  keeping dependencies minimal and leaving the door open for async support
- **PostgreSQL backend** — zero-infrastructure search using built-in full-text search
- **Django-native filter/sort** — use dicts and lists instead of engine-specific syntax
- **Index management** — create, configure, sync, and delete search indexes;
  Django is the source of truth, the engine is the secondary store
- **Document indexing** — add, update, delete, and bulk-index documents with
  full audit logging via `IndexSyncLog`
- **SearchableMixin** — declare Django models as indexable with a small set of
  class attributes; override two methods for custom serialisation and queryset
  filtering
- **Auto-indexing** — `ICV_SEARCH_AUTO_INDEX` wires `post_save` / `post_delete`
  signal handlers automatically; disable per-block with `skip_index_update()`
- **Multi-tenancy** — optional tenant-prefixed index names via a configurable
  callable; no hard coupling to any specific tenant model
- **Management commands** — sync, reindex, health check, create, and clear
  indexes from the command line
- **Celery integration** — async document indexing and periodic sync tasks;
  degrades gracefully to synchronous when Celery is not installed
- **DummyBackend** — in-memory backend for testing without a running search
  engine; ships with pytest fixtures and helper functions in `icv_search.testing`
- **Normalised response types** — `TaskResult`, `SearchResult`, and `IndexStats`
  dataclasses insulate consuming code from engine-specific response shapes
- **ICVSearchPaginator** — Django `Paginator` subclass that uses `estimated_total_hits` from the search engine; no `queryset.count()` query; `is_estimated` flag for approximate display
- **Range filters** — `__gte`, `__gt`, `__lte`, `__lt` suffixes on filter dict keys for numeric range queries across all backends
- **Facet distribution** — `SearchResult.facet_distribution` normalised from the engine response, with `get_facet_values()` helper
- **Health check endpoint** — `/health/` JSON view for load balancer probes; include via `icv_search.urls`
- **Zero-downtime reindex** — `reindex_zero_downtime()` creates a temp index, populates it, then atomically swaps with the live index
- **Signal debouncing** — `ICV_SEARCH_DEBOUNCE_SECONDS` batches rapid auto-index signals into a single indexing call
- **Highlighting** — `SearchResult.formatted_hits` with `get_highlighted_hits()` helper; supports `highlight_fields`, custom pre/post tags across all three backends
- **Ranking scores** — `SearchResult.ranking_scores` with `get_hit_with_score()` helper; Meilisearch `_rankingScore`, PostgreSQL `ts_rank`, DummyBackend term-frequency
- **Geo-distance search** — `geo_point`, `geo_radius`, `geo_sort` params using Meilisearch native `_geoRadius`/`_geoPoint` or Haversine on other backends; `_geoDistance` on hits
- **Soft-delete awareness** — `SearchableMixin` auto-excludes soft-deleted records (`is_deleted`, `deleted_at`); auto-index removes soft-deleted instances on save
- **Multi-search** — `multi_search()` executes multiple queries in one request (native `POST /multi-search` on Meilisearch)
- **Synonym/stop-word/typo management** — dedicated service functions for `get_synonyms()`, `update_synonyms()`, `get_stop_words()`, `get_typo_tolerance()`, and more
- **SearchQuery DSL** — fluent chainable query builder: `.text().filter().sort().facets().highlight().geo_near().execute()`
- **Search analytics** — `SearchQueryLog` for individual queries and `SearchQueryAggregate` for daily rollups; `ICV_SEARCH_LOG_MODE` controls strategy (`"individual"`, `"aggregate"`, `"both"`); sample rate control for high-traffic sites; `get_popular_queries()`, `get_zero_result_queries()`, `get_search_stats()`, `get_query_trend()` service functions
- **Tenant middleware** — `ICVSearchTenantMiddleware` auto-injects tenant context from request; `get_current_tenant_id()` for automatic scoping
- **Search result cache** — optional `ICVSearchCache` layer using Django's cache framework with automatic invalidation on index changes
- **Search merchandising** — optional layer for controlling search result presentation:
  query redirects, query rewrites, pinned results, boost rules, search banners,
  zero-result fallbacks, and a 9-step `merchandised_search()` pipeline that composes
  them all; gated behind `ICV_SEARCH_MERCHANDISING_ENABLED`
- **Search suggestions** — `get_trending_searches()` and `get_suggested_queries()` from
  existing `SearchQueryAggregate` data — no external service needed

---

## Installation

### Basic (standalone)

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

Installing with the `icv-core` extra gives you `BaseModel` (UUID primary key
plus `created_at` / `updated_at` timestamps) from
[icv-core](https://github.com/nigelcopley/icv-oss):

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

Both `SearchIndex` and `IndexSyncLog` inherit from `icv_core.models.BaseModel`
automatically when `icv_core` is present.

---

## Quick Start

The following example creates a search index, indexes a handful of documents,
and runs a search against Meilisearch running on `localhost:7700`.

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

All settings are namespaced under `ICV_SEARCH_*`. Every setting has a
sensible default so the package works out of the box for local development.

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `ICV_SEARCH_BACKEND` | `str` | `"icv_search.backends.meilisearch.MeilisearchBackend"` | Dotted path to the active search backend class |
| `ICV_SEARCH_URL` | `str` | `"http://localhost:7700"` | Search engine base URL |
| `ICV_SEARCH_API_KEY` | `str` | `""` | Master or admin API key for the search engine |
| `ICV_SEARCH_TIMEOUT` | `int` | `30` | Request timeout in seconds for all backend calls |
| `ICV_SEARCH_TENANT_PREFIX_FUNC` | `str` | `""` | Dotted path to a callable `(request_or_none) -> str` that returns the tenant prefix. Empty string disables multi-tenancy |
| `ICV_SEARCH_AUTO_SYNC` | `bool` | `True` | Automatically push index settings to the engine when a `SearchIndex` record is saved |
| `ICV_SEARCH_ASYNC_INDEXING` | `bool` | `True` | Use Celery for document indexing operations. Falls back to synchronous when Celery is unavailable |
| `ICV_SEARCH_INDEX_PREFIX` | `str` | `""` | Global prefix applied to all engine index names (e.g. `"staging_"` to segregate environments) |
| `ICV_SEARCH_AUTO_INDEX` | `dict` | `{}` | Automatic model-level indexing configuration. See below |
| `ICV_SEARCH_DEBOUNCE_SECONDS` | `int` | `0` | Debounce window in seconds for auto-index signal batching. When > 0, rapid saves are collected and indexed in a single batch after this delay. Requires Django's cache framework. `0` disables debouncing |
| `ICV_SEARCH_LOG_QUERIES` | `bool` | `False` | Log every `search()` call to `SearchQueryLog` for analytics |
| `ICV_SEARCH_LOG_ZERO_RESULTS_ONLY` | `bool` | `False` | When `True` (and `LOG_QUERIES` is `True`), only zero-result queries are logged |
| `ICV_SEARCH_LOG_MODE` | `str` | `"individual"` | Logging strategy: `"individual"` writes per-query rows to `SearchQueryLog`, `"aggregate"` writes daily rollups to `SearchQueryAggregate`, `"both"` writes to both. See [Search Analytics](#search-analytics) |
| `ICV_SEARCH_LOG_SAMPLE_RATE` | `float` | `1.0` | Fraction of individual `SearchQueryLog` rows to write (0.0–1.0). Only applies to `"individual"` and `"both"` modes. Aggregate counts are always recorded at 100% regardless of this setting |
| `ICV_SEARCH_CACHE_ENABLED` | `bool` | `False` | Enable search result caching via Django's cache framework |
| `ICV_SEARCH_CACHE_TIMEOUT` | `int` | `60` | Cache TTL in seconds for stored search results |
| `ICV_SEARCH_CACHE_ALIAS` | `str` | `"default"` | Django cache alias used by the search result cache |
| `ICV_SEARCH_MERCHANDISING_ENABLED` | `bool` | `False` | Enable the merchandising layer (query redirects, rewrites, pins, boosts, banners, zero-result fallbacks). When `False`, `merchandised_search()` delegates directly to `search()` with no rule evaluation |
| `ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT` | `int` | `300` | Cache TTL in seconds for merchandising rules loaded from the database. Set to `0` to disable caching (useful in tests) |

### Auto-Indexing Configuration

`ICV_SEARCH_AUTO_INDEX` wires `post_save` and `post_delete` signal handlers
automatically for any model you declare. The package's `AppConfig.ready()` reads
this setting and connects the handlers on startup.

Each key in the dict is the logical index name. The value is a configuration
dict with the following keys:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `model` | `str` | required | `"app_label.ModelName"` — the Django model to watch |
| `on_save` | `bool` | `True` | Index the document when the model instance is saved |
| `on_delete` | `bool` | `True` | Remove the document when the model instance is deleted |
| `async` | `bool` | from `ICV_SEARCH_ASYNC_INDEXING` | Override async behaviour for this index only |
| `auto_create` | `bool` | `True` | Create the `SearchIndex` record and engine index if they do not yet exist |
| `should_update` | `str` | `""` | Dotted path to a callable `(instance) -> bool`. When provided, the document is only indexed when the callable returns `True` |
| `updated_field` | `str` | `""` | Field name used to filter records for incremental reindexing (reserved for future use) |

**Example — multiple models:**

```python
ICV_SEARCH_AUTO_INDEX = {
    "articles": {
        "model": "blog.Article",
        "on_save": True,
        "on_delete": True,
        "async": True,
        "auto_create": True,
        "should_update": "blog.search.should_index_article",
    },
    "products": {
        "model": "catalogue.Product",
        "on_save": True,
        "on_delete": True,
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

Add `SearchableMixin` to any Django model to make it indexable. Declare the
index configuration as class attributes.

```python
from django.db import models
from icv_search.mixins import SearchableMixin

class Product(SearchableMixin, models.Model):
    # Required: the logical name of the search index
    search_index_name = "products"

    # Fields included in full-text search
    search_fields = ["name", "description", "sku"]

    # Fields that can be used in filter expressions
    search_filterable_fields = ["category_id", "is_active", "price"]

    # Fields that can be used in sort expressions
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

Override `to_search_document()` to control exactly what is sent to the engine.
The default implementation includes `id` and all fields listed in
`search_fields`, converting dates to ISO strings and other non-primitive types
to strings.

```python
def to_search_document(self) -> dict:
    return {
        "id": str(self.id),
        "name": self.name,
        "description": self.description,
        "sku": self.sku,
        "price": float(self.price),  # Decimal -> float for JSON
        "category_id": self.category_id,
        "is_active": self.is_active,
        "category_name": self.category.name,  # Denormalised for search
    }
```

### Customising the reindex queryset

Override `get_search_queryset()` to control which records are included in a
full reindex and to add `select_related` / `prefetch_related` for performance.

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

```python
def create_index(
    name: str,
    tenant_id: str = "",
    settings: dict | None = None,
    primary_key: str = "id",
    model_class: type | None = None,
) -> SearchIndex:
```

Creates a `SearchIndex` record, provisions the index in the engine, and pushes
any settings. If `model_class` is provided and it uses `SearchableMixin`, its
field lists seed the index settings automatically.

```python
from icv_search.services import create_index
from myapp.models import Product

index = create_index(
    name="products",
    model_class=Product,            # Reads search_filterable_fields etc.
    settings={"rankingRules": ["words", "typo", "proximity"]},  # Overrides model
)
```

#### `delete_index`

```python
def delete_index(name_or_index: str | SearchIndex, tenant_id: str = "") -> None:
```

Deletes the `SearchIndex` record from Django and removes the index from the
engine. Raises `SearchBackendError` on engine failure.

```python
from icv_search.services import delete_index
delete_index("products")
```

#### `update_index_settings`

```python
def update_index_settings(
    name_or_index: str | SearchIndex,
    settings: dict,
    tenant_id: str = "",
) -> SearchIndex:
```

Merges `settings` into the existing index settings, saves to Django, and syncs
to the engine.

```python
from icv_search.services import update_index_settings
index = update_index_settings("products", {
    "synonyms": {"phone": ["mobile", "handset"]},
})
```

#### `get_index_stats`

```python
def get_index_stats(name_or_index: str | SearchIndex, tenant_id: str = "") -> IndexStats:
```

Returns a normalised `IndexStats` dataclass with live data from the engine.

```python
from icv_search.services import get_index_stats
stats = get_index_stats("products")
print(stats.document_count)
print(stats.is_indexing)
```

### Document Operations

#### `index_documents`

```python
def index_documents(
    name_or_index: str | SearchIndex,
    documents: list[dict],
    tenant_id: str = "",
    primary_key: str = "id",
) -> TaskResult:
```

Adds or updates documents in the search index. Returns a `TaskResult`.

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
def remove_documents(
    name_or_index: str | SearchIndex,
    document_ids: list[str],
    tenant_id: str = "",
) -> TaskResult:
```

Removes documents from the index by their primary key values.

```python
from icv_search.services import remove_documents
remove_documents("products", ["abc123", "def456"])
```

#### `index_model_instances`

```python
def index_model_instances(
    model_class: type,
    queryset=None,
    batch_size: int = 1000,
) -> int:
```

Indexes model instances using their `SearchableMixin` configuration. Iterates
the queryset in batches to avoid loading the entire dataset into memory. Returns
the total number of documents indexed.

```python
from icv_search.services import index_model_instances
from myapp.models import Product

count = index_model_instances(Product, batch_size=500)
print(f"Indexed {count} products")
```

#### `reindex_all`

```python
def reindex_all(
    name_or_index: str | SearchIndex,
    model_class: type,
    tenant_id: str = "",
    batch_size: int = 1000,
) -> int:
```

Full reindex: clears all existing documents, then re-indexes from the model's
`get_search_queryset()`. Use `index_model_instances` instead if you do not want
to clear first.

```python
from icv_search.services import reindex_all
from myapp.models import Product

total = reindex_all("products", Product, batch_size=500)
print(f"Reindexed {total} products")
```

#### `reindex_zero_downtime`

```python
def reindex_zero_downtime(
    name_or_index: str | SearchIndex,
    model_class: type,
    tenant_id: str = "",
    batch_size: int = 1000,
) -> int:
```

Zero-downtime reindex: creates a temporary index with the same settings,
populates it from the model queryset, then atomically swaps it with the live
index. The old index is deleted after the swap. Falls back to `reindex_all()`
if the backend does not support index swaps.

```python
from icv_search.services import reindex_zero_downtime
from myapp.models import Product

total = reindex_zero_downtime("products", Product, batch_size=500)
```

### Search

Filters and sort orders use Django-native syntax — the service layer translates
them to each engine's native format automatically. This means the same calling
code works across all backends.

```python
from icv_search.services import search

# Django-native filter dict
result = search("products", "padel", filter={"category": "equipment", "is_active": True})

# Django-native sort list (- prefix = descending)
result = search("products", "", sort=["-price", "name"])

# Combined
result = search("products", "padel",
    filter={"city": "Madrid", "is_active": True},
    sort=["-created_at"],
    limit=10,
)
```

#### `search`

```python
def search(
    name_or_index: str | SearchIndex,
    query: str,
    tenant_id: str = "",
    **params,
) -> SearchResult:
```

Executes a search query and returns a normalised `SearchResult`. Additional
keyword arguments are passed to the engine (e.g. `filter`, `sort`, `limit`,
`offset`, `facets`).

```python
from icv_search.services import search

# Basic search
results = search("products", "widget")

# With Django-native filter dict and sort list
results = search(
    "products",
    "widget",
    filter={"is_active": True, "price__lt": 50},
    sort=["-price"],
    limit=20,
    offset=0,
)

for hit in results.hits:
    print(hit["name"], hit["price"])

print(f"About {results.estimated_total_hits} results")
```

### Pagination

`ICVSearchPaginator` wraps a `SearchResult` for use with Django's pagination
machinery (`ListView`, templates). It uses `estimated_total_hits` as the count
instead of running a separate `queryset.count()` query.

```python
from icv_search import ICVSearchPaginator
from icv_search.services import search

# In a view
page_number = int(request.GET.get("page", 1))
per_page = 25
result = search("products", query, limit=per_page, offset=(page_number - 1) * per_page)

paginator = ICVSearchPaginator(result, per_page=per_page)
page_obj = paginator.get_page(page_number)

# In a template
{% for hit in page_obj %}
    {{ hit.name }}
{% endfor %}

{% if page_obj.is_estimated %}
    {{ page_obj.display_count }} results
{% else %}
    {{ page_obj.paginator.count }} results
{% endif %}
```

### Facets

When requesting facets from the search engine, the normalised `facet_distribution`
is available directly on `SearchResult`:

```python
result = search("products", "shoes", facets=["brand", "colour"])
print(result.facet_distribution)
# {"brand": {"Nike": 42, "Adidas": 31}, "colour": {"black": 55, "white": 28}}

# Convenience helper — sorted by count descending
for facet in result.get_facet_values("brand"):
    print(f"{facet['name']}: {facet['count']}")
```

### Range Filters

Use Django-style lookup suffixes for numeric range queries:

```python
result = search("products", "",
    filter={"price__gte": 10, "price__lte": 100, "is_active": True},
)
```

Supported suffixes: `__gte` (>=), `__gt` (>), `__lte` (<=), `__lt` (<).
Works across all backends.

### Bulk Operations

#### `skip_index_update`

A context manager that temporarily disables auto-indexing signal handlers.
Use this in bulk imports, data migrations, and test factories to avoid
triggering individual index updates for every `save()` call.

```python
from icv_search.auto_index import skip_index_update
from myapp.models import Article

articles = [Article(title=f"Article {i}") for i in range(1000)]

with skip_index_update():
    Article.objects.bulk_create(articles)
    # No search index updates during this block

# Trigger a single reindex after the bulk operation
from icv_search.services import reindex_all
reindex_all("articles", Article)
```

The context manager is nestable. Auto-indexing resumes when the outermost
`with` block exits.

### Highlighting

Pass `highlight_fields` to get highlighted versions of matching text:

```python
result = search("articles", "django tips",
    highlight_fields=["title", "body"],
    highlight_pre_tag="<mark>",   # default
    highlight_post_tag="</mark>",  # default
)

# Highlighted versions of each hit
for hit in result.get_highlighted_hits():
    print(hit["title"])  # "...about <mark>Django</mark> <mark>tips</mark>..."

# Or access directly
result.formatted_hits  # list of highlighted hit dicts
```

Works across all backends: Meilisearch uses native `_formatted`, PostgreSQL uses
`ts_headline()`, DummyBackend wraps matching substrings.

### Ranking Scores

Request ranking scores to understand result relevance:

```python
result = search("products", "shoes", show_ranking_score=True)

for i, hit in enumerate(result.hits):
    hit, score = result.get_hit_with_score(i)
    print(f"{hit['name']}: {score:.2f}")
```

Meilisearch returns `_rankingScore` (0.0–1.0), PostgreSQL uses `ts_rank`, and
DummyBackend computes a simple term-frequency score.

### Geo-Distance Search

Filter and sort results by geographic distance:

```python
# Find restaurants within 5km of a point
result = search("restaurants", "",
    geo_point=(51.5074, -0.1278),  # London (lat, lng)
    geo_radius=5000,                # metres
    geo_sort="asc",                 # nearest first
)

for hit in result.hits:
    print(f"{hit['name']}: {hit.get('_geoDistance')}m away")
```

Models with geo data should declare it on the mixin:

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

Execute multiple queries in a single request:

```python
from icv_search.services import multi_search

results = multi_search([
    {"index_name": "products", "query": "shoes", "limit": 5},
    {"index_name": "articles", "query": "shoes", "limit": 3, "facets": ["category"]},
])

product_results, article_results = results
```

Meilisearch uses the native `POST /multi-search` endpoint. Other backends
execute queries sequentially.

### Synonym and Stop-Word Management

```python
from icv_search.services import (
    get_synonyms, update_synonyms, reset_synonyms,
    get_stop_words, update_stop_words, reset_stop_words,
    get_typo_tolerance, update_typo_tolerance,
)

# Synonyms
update_synonyms("products", {"phone": ["mobile", "handset"], "laptop": ["notebook"]})
print(get_synonyms("products"))
reset_synonyms("products")

# Stop words
update_stop_words("products", ["the", "a", "an", "is"])
print(get_stop_words("products"))

# Typo tolerance
update_typo_tolerance("products", {"enabled": True, "minWordSizeForTypos": {"oneTypo": 4}})
```

### SearchQuery Builder

A fluent API for building search queries:

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
ICV_SEARCH_LOG_ZERO_RESULTS_ONLY = False  # Set True to reduce storage
```

#### Logging strategies

`ICV_SEARCH_LOG_MODE` controls how queries are recorded:

| Mode | Storage | Best for |
|------|---------|----------|
| `"individual"` (default) | One `SearchQueryLog` row per query | Low/medium traffic sites that need full query history |
| `"aggregate"` | Daily rollups in `SearchQueryAggregate` | High-traffic sites where individual rows would be too large |
| `"both"` | Both individual rows and daily rollups | Sites that want detailed logs for recent queries plus long-term trends |

```python
# settings.py — high-traffic configuration
ICV_SEARCH_LOG_QUERIES = True
ICV_SEARCH_LOG_MODE = "aggregate"        # Daily rollups only
ICV_SEARCH_LOG_SAMPLE_RATE = 1.0         # Not applicable in aggregate-only mode

# Or keep individual logs with sampling
ICV_SEARCH_LOG_MODE = "both"
ICV_SEARCH_LOG_SAMPLE_RATE = 0.1         # Write only 10% of individual rows
```

Aggregate queries are normalised (stripped and lowercased) for consistent grouping. The sample rate only affects `SearchQueryLog` rows — aggregate counts always reflect 100% of queries.

#### Analytics service functions

```python
from icv_search.services import (
    get_popular_queries,
    get_zero_result_queries,
    get_search_stats,
    get_query_trend,
    clear_query_logs,
    clear_query_aggregates,
)

# Most frequent queries in the last 7 days
popular = get_popular_queries("products", days=7, limit=20)

# Queries that returned no results
gaps = get_zero_result_queries("products", days=7)

# Aggregate stats
stats = get_search_stats("products", days=7)
# {"total_queries": 1234, "avg_processing_time_ms": 12, "zero_result_rate": 0.05}

# Day-by-day trend for a specific query (reads from SearchQueryAggregate)
trend = get_query_trend("running shoes", "products", days=30)
# [{"date": date(2026, 3, 1), "count": 42, "zero_result_count": 3, "avg_processing_time_ms": 8.5}, ...]

# Cleanup
deleted = clear_query_logs(days_older_than=30)
deleted = clear_query_aggregates(days_older_than=90)
```

All analytics functions (`get_popular_queries`, `get_zero_result_queries`, `get_search_stats`) automatically read from the correct model based on `ICV_SEARCH_LOG_MODE`.

### Tenant Middleware

Auto-inject tenant context from the request instead of passing `tenant_id`
on every call:

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
results = search("products", "widget")  # No tenant_id needed

# Explicit tenant_id always takes precedence
results = search("products", "widget", tenant_id="other_tenant")
```

### Search Result Cache

Enable caching to reduce backend load for repeated queries:

```python
# settings.py
ICV_SEARCH_CACHE_ENABLED = True
ICV_SEARCH_CACHE_TIMEOUT = 60         # seconds
ICV_SEARCH_CACHE_ALIAS = "default"    # Django cache alias
```

Cache is automatically invalidated when documents are indexed or removed.
Queries with a `user` param bypass the cache (analytics-aware searches may
vary by user).

---

## Merchandising

The merchandising layer lets non-technical users control what shoppers see in
search results. It is entirely optional and gated behind a single feature flag.

### Enabling

```python
# settings.py
ICV_SEARCH_MERCHANDISING_ENABLED = True

# Optional: adjust rule cache TTL (default 300s)
ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT = 300
```

When disabled (the default), `merchandised_search()` is a thin wrapper around
`search()` that returns a `MerchandisedSearchResult` with no merchandising
metadata.

### Models

All merchandising models inherit from `MerchandisingRuleBase`, an abstract base
providing common fields for query matching, scheduling, and activation:

| Field | Type | Description |
|-------|------|-------------|
| `index_name` | `CharField(200)` | Logical index name this rule applies to |
| `tenant_id` | `CharField(200)` | Tenant identifier. Blank applies to all tenants |
| `query_pattern` | `CharField(500)` | Pattern to match against search queries |
| `match_type` | `CharField(20)` | Match strategy: `exact`, `contains`, `starts_with`, `regex` |
| `is_active` | `BooleanField` | Inactive rules are never evaluated |
| `priority` | `IntegerField` | Higher priority rules are evaluated first |
| `starts_at` | `DateTimeField` | Rule is active from this date/time (blank = no constraint) |
| `ends_at` | `DateTimeField` | Rule is active until this date/time (blank = no constraint) |
| `hit_count` | `PositiveIntegerField` | Automatically incremented each time the rule fires |

#### QueryRedirect

Redirect a search query to a URL instead of showing results.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `destination_url` | `URLField(2000)` | — | URL to redirect to |
| `destination_type` | `CharField(20)` | `"url"` | Classification: `url`, `category`, `product`, `page` |
| `preserve_query` | `BooleanField` | `False` | Append `?q=<original query>` to the destination |
| `http_status` | `IntegerField` | `302` | HTTP status: `301` (permanent) or `302` (temporary) |

#### QueryRewrite

Transparently rewrite a search query before execution.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `rewritten_query` | `CharField(500)` | — | Query string to use instead of the original |
| `apply_filters` | `JSONField` | `{}` | Additional filters to inject |
| `apply_sort` | `JSONField` | `[]` | Sort order to inject |
| `merge_filters` | `BooleanField` | `True` | Merge with existing filters (when `False`, replaces them) |

#### SearchPin

Pin a document to a fixed position in results.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `document_id` | `CharField(255)` | — | ID of the document to pin |
| `position` | `IntegerField` | `0` | Zero-based position. Use `-1` to bury (push to end) |
| `label` | `CharField(200)` | `""` | Optional label: `"sponsored"`, `"editorial pick"`, etc. |

A `UniqueConstraint` prevents the same document being pinned twice for the
same query/index/tenant combination.

#### BoostRule

Adjust ranking scores based on field values.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `field` | `CharField(200)` | — | Document field to evaluate |
| `field_value` | `CharField(500)` | `""` | Value to compare against (ignored for `exists` operator) |
| `operator` | `CharField(20)` | `"eq"` | Comparison: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `contains`, `exists` |
| `boost_weight` | `DecimalField(6,3)` | `1.0` | Multiplicative weight. `>1` promotes, `<1` demotes |

#### SearchBanner

Display a banner alongside search results.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `title` | `CharField(200)` | — | Banner headline |
| `content` | `TextField` | `""` | Body content (supports HTML) |
| `image_url` | `URLField(2000)` | `""` | Optional image URL |
| `link_url` | `URLField(2000)` | `""` | Optional click-through URL |
| `link_text` | `CharField(200)` | `""` | Call-to-action text |
| `position` | `CharField(20)` | `"top"` | Where to display: `top`, `inline`, `bottom`, `sidebar` |
| `banner_type` | `CharField(20)` | `"informational"` | Semantic type: `informational`, `promotional`, `warning` |
| `metadata` | `JSONField` | `{}` | Arbitrary data for custom rendering |

#### ZeroResultFallback

Define fallback behaviour when a query returns no results.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `fallback_type` | `CharField(30)` | — | Strategy: `redirect`, `alternative_query`, `curated_results`, `popular_in_category` |
| `fallback_value` | `CharField(2000)` | — | Meaning depends on type: URL, query string, comma-separated IDs, or category identifier |
| `fallback_filters` | `JSONField` | `{}` | Additional filters for fallback searches |
| `max_retries` | `PositiveIntegerField` | `1` | Maximum fallback attempts before giving up |

### The Pipeline

`merchandised_search()` composes all features into a 9-step pipeline:

```
1. Feature gate     → disabled? delegate to search()
2. Normalise query  → strip, collapse whitespace, lowercase
3. Redirect check   → short-circuit with URL if matched
4. Query rewrite    → replace query, merge filters/sort
5. Search           → call search() with the (possibly rewritten) query
6. Pin insertion    → insert/move pinned documents
7. Boost re-rank    → multiply scores, re-sort
8. Fallback         → zero results? try alternative strategy
9. Banner attach    → attach matching banners to the result
```

Each step is individually skippable:

```python
from icv_search.services import merchandised_search

result = merchandised_search(
    "products",
    "red shoes",
    tenant_id="acme",
    skip_redirects=True,   # Skip step 3
    skip_boosts=True,      # Skip step 7
    limit=20,
)
```

### Service Functions

#### Redirects

```python
from icv_search.services import check_redirect, resolve_redirect_url

redirect = check_redirect("products", "sale", tenant_id="")
if redirect:
    url = resolve_redirect_url(redirect, "sale")
    # url = "https://example.com/sale" (or with ?q=sale if preserve_query=True)
```

#### Rewrites

```python
from icv_search.services import apply_rewrite

rewritten_query, filters, sort, rule = apply_rewrite("products", "sneakers")
# rewritten_query = "shoes", filters = {"category": "footwear"}, sort = [], rule = <QueryRewrite>
```

#### Pins

```python
from icv_search.services import get_pins_for_query, apply_pins, search

pins = get_pins_for_query("products", "shoes")
result = search("products", "shoes")
result = apply_pins(result, pins, "products")
# result.hits[0]["_pinned"] == True
```

#### Boosts

```python
from icv_search.services import get_boost_rules_for_query, apply_boosts, search

rules = get_boost_rules_for_query("products", "shoes")
result = search("products", "shoes")
result = apply_boosts(result, rules)
# Documents matching the boost condition are re-ranked
```

#### Banners

```python
from icv_search.services import get_banners_for_query

banners = get_banners_for_query("products", "shoes")
# [<SearchBanner: Summer Sale! (top)>, ...]
```

#### Fallbacks

```python
from icv_search.services import get_fallback_for_query, execute_fallback

fallback = get_fallback_for_query("products", "xyznonexistent")
if fallback:
    result = execute_fallback(fallback, "products", "xyznonexistent")
    # result.is_fallback == True
```

#### Suggestions

```python
from icv_search.services import get_trending_searches, get_suggested_queries

trending = get_trending_searches("products", days=1, limit=10)
# [{"query": "iphone", "count": 342}, {"query": "airpods", "count": 198}, ...]

suggestions = get_suggested_queries("products", "iph", limit=5)
# [{"query": "iphone", "count": 342}, {"query": "iphone case", "count": 87}, ...]
```

### Full Pipeline Example

```python
from icv_search.services import merchandised_search

result = merchandised_search("products", "trainers", tenant_id="acme")

# Redirect? (step 3 short-circuits)
if result.redirect:
    return HttpResponseRedirect(result.redirect["url"])

# Metadata
print(result.original_query)   # "trainers"
print(result.was_rewritten)    # True (if a rewrite rule matched)
print(result.is_fallback)      # True (if zero-result fallback was triggered)
print(result.applied_rules)    # [{"type": "rewrite", ...}, {"type": "pin", ...}, ...]

# Banners
for banner in result.banners:
    print(banner["title"], banner["position"])

# Results (with pins and boosts already applied)
for hit in result.hits:
    if hit.get("_pinned"):
        print(f"[{hit.get('_pin_label', 'pinned')}]", hit["id"])
    else:
        print(hit["id"])
```

### Admin

All six merchandising models are registered in the Django admin with:

- List display with key fields, activation status, and hit counts
- List filters for `is_active`, `match_type`, and model-specific fields
- Search on `query_pattern` and model-specific text fields
- Fieldsets grouping configuration, scheduling, and statistics
- Bulk **Enable** / **Disable** actions for batch rule management
- `hit_count` as a read-only field (auto-incremented by the matching engine)

### Rule Matching

All rules share the same matching engine:

1. **Load** — active rules for the index/tenant are loaded from the database
   (cached for `ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT` seconds)
2. **Schedule** — rules outside their `starts_at`/`ends_at` window are excluded
3. **Match** — the normalised query is tested against each rule's `query_pattern`
   using its `match_type` (`exact`, `contains`, `starts_with`, `regex`)
4. **Hit count** — matched rules have their `hit_count` incremented via an
   `F()` expression (race-condition safe)

Cache is automatically invalidated on `post_save` and `post_delete` of any
merchandising model.

---

## Response Types

All service functions return normalised dataclasses, insulating your code from
engine-specific response shapes.

### `TaskResult`

Returned by document indexing and deletion operations.

```python
@dataclass
class TaskResult:
    task_uid: str        # Engine-assigned task identifier
    status: str          # Task status (e.g. "enqueued", "succeeded")
    detail: str          # Operation type or description
    raw: dict            # Original engine response
```

### `SearchResult`

Returned by `search()`.

```python
@dataclass
class SearchResult:
    hits: list[dict]         # Matching documents
    query: str               # The query string as echoed by the engine
    processing_time_ms: int  # Time taken by the engine (milliseconds)
    estimated_total_hits: int  # Approximate total matching documents
    limit: int               # Page size applied
    offset: int              # Offset applied
    facet_distribution: dict[str, dict[str, int]]  # Facet counts by field
    formatted_hits: list[dict]  # Highlighted versions of hits
    ranking_scores: list[float | None]  # Relevance scores per hit
    raw: dict                # Original engine response

    def get_highlighted_hits(self) -> list[dict]: ...
    def get_facet_values(facet_name: str) -> list[dict]: ...
    def get_hit_with_score(index: int) -> tuple[dict, float | None]: ...
```

### `IndexStats`

Returned by `get_index_stats()`.

```python
@dataclass
class IndexStats:
    document_count: int              # Number of indexed documents
    is_indexing: bool                # Whether the engine is currently indexing
    field_distribution: dict[str, int]  # Field name -> document count
    raw: dict                        # Original engine response
```

### `MerchandisedSearchResult`

Extended search result carrying merchandising metadata. Contains all fields
from `SearchResult` plus:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `redirect` | `Any \| None` | `None` | Redirect instruction dict with `url`, `status`, and `type` keys. Present when a redirect or redirect-fallback matched |
| `banners` | `list` | `[]` | List of banner dicts (title, content, image_url, link_url, position, etc.) |
| `applied_rules` | `list[dict]` | `[]` | Audit trail of every rule that fired, each with a `type` key (`redirect`, `rewrite`, `pin`, `boost`, `banner`, `fallback`) |
| `original_query` | `str` | `""` | The query before any rewrite was applied |
| `was_rewritten` | `bool` | `False` | `True` when a rewrite rule modified the query |
| `is_fallback` | `bool` | `False` | `True` when the results came from a zero-result fallback |

```python
from icv_search.types import MerchandisedSearchResult, SearchResult

# Create from an existing SearchResult
result = search("products", "shoes")
merch = MerchandisedSearchResult.from_search_result(result, banners=[...])

# All SearchResult methods are available
merch.get_highlighted_hits()
merch.get_hit_with_score(0)
merch.get_facet_values("category")
```

---

## Backends

### Meilisearch (default)

Requires a running Meilisearch instance (v1.0+). Uses `httpx` directly rather
than the official SDK, keeping dependencies minimal.

**Required settings:**

```python
ICV_SEARCH_BACKEND = "icv_search.backends.meilisearch.MeilisearchBackend"
ICV_SEARCH_URL = "http://localhost:7700"
ICV_SEARCH_API_KEY = "your-master-key"  # Leave blank if no auth configured
```

### PostgreSQL (zero infrastructure)

Uses Django's built-in `django.contrib.postgres.search` to provide full-text
search without any external services. Documents are stored in PostgreSQL tables
with tsvector indexing.

```python
ICV_SEARCH_BACKEND = "icv_search.backends.postgres.PostgresBackend"
# ICV_SEARCH_URL and ICV_SEARCH_API_KEY are ignored by this backend.
```

The backend automatically creates its tables on first use — no additional
migrations required. Supports:

- Full-text search with ranking (ts_rank)
- Django-native filter dicts
- Django-native sort lists
- searchableAttributes from index settings

Best for projects that want search without running Meilisearch, or as a
starting point before upgrading to a dedicated search engine.

### DummyBackend (testing)

An in-memory backend that stores documents in module-level dicts. No running
search engine required. Supports basic substring search, limit, and offset.

```python
# tests/settings.py
ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
ICV_SEARCH_ASYNC_INDEXING = False  # Keep tests synchronous
```

See the [Testing](#testing) section for fixtures and helpers.

### Writing a Custom Backend

Subclass `BaseSearchBackend` and implement all abstract methods. Point
`ICV_SEARCH_BACKEND` at the dotted path to your class.

```python
# myproject/search_backends.py
from icv_search.backends.base import BaseSearchBackend

class TypesenseBackend(BaseSearchBackend):

    def __init__(self, url: str, api_key: str, timeout: int = 30, **kwargs):
        super().__init__(url=url, api_key=api_key, timeout=timeout, **kwargs)
        # Initialise your HTTP client here

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

```python
# settings.py
ICV_SEARCH_BACKEND = "myproject.search_backends.TypesenseBackend"
```

Raise `icv_search.exceptions.SearchBackendError` on failure so the service
layer handles errors consistently.

---

## Management Commands

| Command | Purpose |
|---------|---------|
| `icv_search_setup [--dry-run]` | **Recommended first step.** Creates `SearchIndex` records for all entries in `ICV_SEARCH_AUTO_INDEX`, syncs settings to the engine, and verifies connectivity. Use `--dry-run` to preview without making changes |
| `icv_search_health [--verbose]` | Check engine connectivity; `--verbose` prints per-index document counts and sync status |
| `icv_search_sync [--index NAME] [--force] [--tenant TENANT]` | Push index settings from Django to the engine; without `--force`, skips indexes already marked as synced |
| `icv_search_reindex --index NAME --model DOTTED.PATH [--batch-size N] [--tenant TENANT]` | Clear and re-index from `get_search_queryset()` in batches (default 1000) |
| `icv_search_create_index --name NAME [--primary-key FIELD] [--tenant TENANT]` | Create a `SearchIndex` record and provision it in the engine |
| `icv_search_clear --index NAME [--tenant TENANT]` | Remove all documents from an index without deleting it |

```bash
# First-time setup — creates all indexes from ICV_SEARCH_AUTO_INDEX
python manage.py icv_search_setup

# Preview what would be created
python manage.py icv_search_setup --dry-run

# Other commands
python manage.py icv_search_health --verbose
python manage.py icv_search_sync --index products --force
python manage.py icv_search_reindex --index products --model myapp.models.Product --batch-size 500
python manage.py icv_search_create_index --name orders --primary-key order_id
python manage.py icv_search_clear --index products
```

> **Note:** `SearchIndex` records are also auto-created on first use — calling
> `search("products", "shoes")` will create the `SearchIndex` record
> automatically if it does not exist. The `icv_search_setup` command is the
> recommended way to provision indexes explicitly during deployment.

---

## Celery Tasks

Celery is optional. When not installed the `shared_task` decorator is replaced
with a no-op and all operations run synchronously. When installed with
`ICV_SEARCH_ASYNC_INDEXING = True`, operations are dispatched as background
tasks with exponential backoff (max three retries).

| Task | Signature | Purpose |
|------|-----------|---------|
| `sync_index_settings` | `(index_pk)` | Push settings for one index |
| `sync_all_indexes` | `()` | Sync all unsynced active indexes (periodic, every 5 min) |
| `add_documents` | `(index_pk, documents, primary_key="id")` | Add/update documents |
| `remove_documents` | `(index_pk, document_ids)` | Remove documents |
| `reindex` | `(index_pk, model_path, batch_size=1000)` | Full reindex from model queryset |
| `refresh_document_counts` | `()` | Refresh cached `document_count` from engine stats (periodic, hourly) |
| `reindex_zero_downtime_task` | `(index_pk, model_path, batch_size=1000)` | Zero-downtime reindex via index swap |
| `flush_debounce_buffer` | `(index_pk)` | Drain debounce buffer and batch-index buffered documents |
| `cleanup_search_query_logs` | `(days_older_than=30)` | Delete old search query log entries (periodic, daily) |
| `cleanup_search_query_aggregates` | `(days_older_than=90)` | Delete old search query aggregate rows (periodic, daily) |

```python
# Celery Beat schedule
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
        "schedule": crontab(hour=3, minute=0),  # daily at 03:00
    },
    "icv-search-cleanup-query-aggregates": {
        "task": "icv_search.tasks.cleanup_search_query_aggregates",
        "schedule": crontab(hour=3, minute=15),  # daily at 03:15
    },
}
```

---

## Signals

All signals are defined in `icv_search.signals`. Connect in your consuming
project to react to search index lifecycle events.

| Signal | Sender | Kwargs | When |
|--------|--------|--------|------|
| `search_index_created` | `SearchIndex` | `instance` | After a new index is created and provisioned |
| `search_index_deleted` | `SearchIndex` | `instance` | After an index is deleted from Django and the engine |
| `search_index_synced` | `SearchIndex` | `instance` | After settings are pushed to the engine successfully |
| `documents_indexed` | `SearchIndex` | `instance`, `count`, `document_ids` | After documents are added or updated |
| `documents_removed` | `SearchIndex` | `instance`, `count`, `document_ids` | After documents are removed |

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

Configure the dummy backend in your test settings:

```python
# tests/settings.py
ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
ICV_SEARCH_ASYNC_INDEXING = False  # Synchronous so assertions work immediately
```

### Test Fixtures

`icv_search.testing` provides ready-made fixtures and factories:

```python
# conftest.py
from icv_search.testing.fixtures import search_backend, search_index  # noqa: F401
```

| Fixture | What it does |
|---------|-------------|
| `search_backend` | Configures `DummyBackend`, resets state before and after the test |
| `search_index` | Creates a `SearchIndex` instance via `SearchIndexFactory` |

**Factories** (`icv_search.testing.factories`):

| Factory | Model |
|---------|-------|
| `SearchIndexFactory` | `SearchIndex` |
| `IndexSyncLogFactory` | `IndexSyncLog` |
| `SearchQueryAggregateFactory` | `SearchQueryAggregate` |

#### Merchandising Factories

```python
from icv_search.testing import (
    QueryRedirectFactory,
    QueryRewriteFactory,
    SearchPinFactory,
    BoostRuleFactory,
    SearchBannerFactory,
    ZeroResultFallbackFactory,
)

redirect = QueryRedirectFactory(index_name="products", query_pattern="sale")
pin = SearchPinFactory(index_name="products", document_id="doc-1", position=0)
```

#### `merchandising_enabled` Fixture

```python
def test_merchandised_search(merchandising_enabled, search_backend):
    """The merchandising_enabled fixture sets MERCHANDISING_ENABLED=True
    and MERCHANDISING_CACHE_TIMEOUT=0 for the test scope."""
    result = merchandised_search("products", "shoes")
    assert isinstance(result, MerchandisedSearchResult)
```

### Asserting Documents

Inspect the DummyBackend's in-memory state directly:

```python
from icv_search.backends.dummy import _documents

def test_article_is_indexed(db, search_backend):
    article = ArticleFactory()

    # After save, the document should be in the dummy backend
    docs = _documents.get(article.search_index_name, {})
    assert str(article.pk) in docs
    assert docs[str(article.pk)]["title"] == article.title
```

Use the provided helper functions for common assertions:

```python
from icv_search.testing.helpers import (
    get_indexed_documents,
    get_dummy_indexes,
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

### `skip_index_update` in Tests

Use `skip_index_update()` in test factories and fixtures to prevent auto-index
noise when creating supporting data that is not the subject of the test:

```python
# tests/factories.py
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

In a multi-tenant application, each tenant's search index is distinguished by
a prefix on the `engine_uid`. The `tenant_id` is stored as a plain `CharField`
on `SearchIndex` — there is no foreign key to a tenant model, so icv-search has
no dependency on any specific tenant implementation.

**Configure the prefix callable:**

```python
# myproject/search.py
def get_tenant_prefix(request_or_none) -> str:
    """Return the current tenant's slug for use as an index prefix."""
    if request_or_none and hasattr(request_or_none, "tenant"):
        return request_or_none.tenant.slug
    return ""

# settings.py
ICV_SEARCH_TENANT_PREFIX_FUNC = "myproject.search.get_tenant_prefix"
```

**How `engine_uid` is computed:**

```
engine_uid = {ICV_SEARCH_INDEX_PREFIX}{tenant_id}_{name}
           = "staging_acme_products"   (prefix="staging_", tenant="acme", name="products")
           = "acme_products"           (no prefix, tenant="acme", name="products")
           = "products"               (single-tenant — no prefix, no tenant)
```

At save time, the callable is invoked with `None` as the request argument
(there is no HTTP request context during a model save). For request-scoped
prefix resolution, pass `tenant_id` explicitly when calling service functions:

```python
from icv_search.services import search
results = search("products", "widget", tenant_id=request.tenant.slug)
```

Omit `ICV_SEARCH_TENANT_PREFIX_FUNC` (leave it as `""`) for single-tenant
deployments — all indexes exist in a flat namespace.

---

## Roadmap

- SQLite FTS5 backend
- MySQL FULLTEXT backend
- Async (`httpx.AsyncClient`) support for ASGI applications
- Typesense backend
- Search result click-through tracking
- A/B testing for ranking rules
- PostGIS-backed geo search (production-grade alternative to Haversine)

---

## Licence

MIT
