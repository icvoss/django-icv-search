# PostgreSQL Backend

Zero-infrastructure search using your existing Django database. No additional
services, no new dependencies beyond what Django already requires.

---

## Overview

- **No extra infrastructure**: uses the same PostgreSQL database as the rest
  of your Django application
- **No additional packages**: requires only `django.contrib.postgres`, which
  is bundled with Django
- **Stores documents in a single table**: `icv_search_document` with a
  `tsvector` column, logically partitioned by `index_uid`
- **Self-bootstrapping**: tables are created automatically on first use via
  `CREATE TABLE IF NOT EXISTS`; no extra migration step required
- **Synchronous**: all operations run in the same database transaction as the
  caller; there is no async task queue

---

## Installation

No extra packages required:

```bash
pip install django-icv-search
```

Ensure `django.contrib.postgres` is in `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    # ...
    "django.contrib.postgres",
    "icv_search",
]
```

Run the icv-search migrations to create the `SearchIndex` and related tables:

```bash
python manage.py migrate icv_search
```

The `icv_search_document` and `icv_search_index_meta` tables are created
automatically the first time the backend is instantiated; you do not need
to add them to a migration.

---

## Settings Reference

| Setting | Value | Description |
|---------|-------|-------------|
| `ICV_SEARCH_BACKEND` | `"icv_search.backends.postgres.PostgresBackend"` | Required: selects this backend |
| `ICV_SEARCH_URL` | *(ignored)* | Not used by this backend |
| `ICV_SEARCH_API_KEY` | *(ignored)* | Not used by this backend |
| `ICV_SEARCH_TIMEOUT` | `30` | Not used (all operations are synchronous) |

---

## Example Configuration

```python
# settings.py
ICV_SEARCH_BACKEND = "icv_search.backends.postgres.PostgresBackend"

# Optional: disable async indexing since this backend is synchronous
ICV_SEARCH_ASYNC_INDEXING = False
```

That is all that is required. The backend uses Django's default database
connection (`django.db.connection`).

---

## When to Use

- **Prototyping**: get search running without any infrastructure work; swap
  to a dedicated engine when you outgrow it.
- **Small datasets**: works well for a few hundred thousand documents. Beyond
  ~500K documents you will likely notice query latency increasing under load.
- **Apps already using PostgreSQL**: if you already pay for a managed Postgres
  instance (RDS, Cloud SQL, Supabase), there is no marginal infrastructure cost.
- **Simple keyword search**: the backend uses `tsvector` / `tsquery` via
  `plainto_tsquery('simple', ...)`. It is not a typo-tolerant search engine.

---

## How It Works

Documents are stored in `icv_search_document`:

```sql
CREATE TABLE icv_search_document (
    id            BIGSERIAL PRIMARY KEY,
    index_uid     VARCHAR(255) NOT NULL,
    doc_id        VARCHAR(255) NOT NULL,
    body          JSONB        NOT NULL DEFAULT '{}',
    search_vector TSVECTOR,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (index_uid, doc_id)
);
CREATE INDEX ON icv_search_document USING GIN (search_vector);
CREATE INDEX ON icv_search_document (index_uid);
```

When you index a document, the backend concatenates the values of
`searchableAttributes` (or all string fields when unset) into a plain-text
string, then stores `to_tsvector('simple', text)` in `search_vector`.

Searches use `plainto_tsquery('simple', query)` matched against the GIN index,
with `ts_rank` for relevance ordering.

---

## pg_trgm for Intelligence Features

PostgreSQL's `pg_trgm` extension enables trigram-based similarity search and
improves partial-match performance. It is optional but recommended if you need
smarter matching:

```sql
-- Run once in a migration or directly in psql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

With `pg_trgm` installed you can add a trigram index on the `body` JSONB field
for faster LIKE/ILIKE queries, or implement fuzzy field-level matching directly
in your application queries alongside icv-search's `tsvector` search.

---

## Filtering

The backend supports Django-native filter dicts:

```python
from icv_search.services import search

results = search(
    "articles",
    "django",
    filter={"published": True, "author_id": 42},
)
```

Range lookups use the `__gte`, `__gt`, `__lte`, `__lt` suffixes:

```python
results = search("products", "", filter={"price__lte": 50.0})
```

Raw SQL filter strings are **not accepted**; pass a dict of field/value pairs.

---

## Geo Search

Documents need a `_geo` field:

```python
{"id": "1", "name": "Coffee shop", "_geo": {"lat": 51.5074, "lng": -0.1278}}
```

```python
results = search(
    "venues",
    "coffee",
    geo_point=(51.5074, -0.1278),
    geo_radius=5000,
    geo_sort="asc",
)
```

The geo implementation uses a pure-SQL Haversine approximation. It is accurate
but not indexed, suitable for prototyping or low-volume queries. For
production geo search with large datasets, install PostGIS and use its spatial
indexes.

---

## Limitations

- **No facet search**: `facet_search()` is not implemented. Use standard
  `facets` in `search()` to obtain facet counts.
- **No similar documents**: `similar_documents()` is not supported.
- **No multi-search**: `multi_search()` is not supported.
- **No index swap**: `swap_indexes()` is not supported.
- **No partial updates via NDJSON**: `add_documents_ndjson()` is not supported.
- **Basic relevance ranking**: `ts_rank` is a frequency-based score. It does
  not incorporate typo tolerance, semantic similarity, or ML-based ranking.
- **Single database**: all indexes live in the same database as your
  application. Heavy indexing activity will contend for connections and I/O
  with normal application traffic.
- **Scale ceiling**: above ~500K documents on a typical managed Postgres
  instance, full-table GIN lookups can become slow under concurrent load.
  Consider migrating to Meilisearch or OpenSearch when this becomes apparent.
