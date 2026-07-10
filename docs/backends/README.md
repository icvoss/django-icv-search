# Search Backend Comparison Guide

A reference for choosing, installing, and configuring the right search backend
for your project.

---

## Quick Comparison

| Backend | Best For | Scale | Infra Required | SDK | Install |
|---------|----------|-------|----------------|-----|---------|
| **Meilisearch** | Small to medium catalogues, developer experience | Single-node | None (default) | httpx (bundled) | `pip install django-icv-search` |
| **PostgreSQL** | Zero-infrastructure, small datasets | Existing Postgres | None (bundled) | psycopg2 (via Django) | `pip install django-icv-search` |
| **OpenSearch** | Large-scale, rich aggregations | Distributed cluster | OpenSearch cluster | opensearch-py | `pip install django-icv-search[opensearch]` |
| **Solr** | Massive catalogues, deep faceting | SolrCloud cluster | Solr + ZooKeeper | pysolr | `pip install django-icv-search[solr]` |
| **Typesense** | Medium to large, typo tolerance, instant search | HA cluster | Typesense cluster | typesense | `pip install django-icv-search[typesense]` |
| **Vespa** | Billions of documents, ML ranking, hybrid search | Vespa cluster | Vespa cluster | pyvespa | `pip install django-icv-search[vespa]` |
| **Dummy** | Testing only | In-memory | None (bundled) | (none) | `pip install django-icv-search` |

---

## Decision Flowchart

```
Start
  │
  ├── Testing / CI only?
  │     └── DummyBackend
  │
  ├── No external infra (zero ops overhead)?
  │     └── PostgreSQL backend
  │
  ├── Less than ~1M docs, want fast setup?
  │     └── Meilisearch  ← start here for most projects
  │
  ├── Need rich aggregations / analytics / comparison shopping?
  │     └── OpenSearch
  │
  ├── Need deep faceting over a massive catalogue?
  │     └── Solr
  │
  ├── Need typo tolerance / instant-search feel?
  │     └── Typesense
  │
  └── Need ML ranking / hybrid vector+text / billions of docs?
        └── Vespa
```

**When in doubt, start with Meilisearch.** It is the default backend, requires
no infrastructure beyond a single Docker container, and covers the majority of
search use cases up to ~20M documents.

---

## Feature Support Matrix

| Feature | Meilisearch | PostgreSQL | OpenSearch | Solr | Typesense | Vespa | Dummy |
|---------|:-----------:|:----------:|:----------:|:----:|:---------:|:-----:|:-----:|
| Full-text search | Yes | Yes | Yes | Yes | Yes | Yes | Yes (basic) |
| Filtering | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| Sorting | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| Faceting | Yes | Yes | Yes | Yes | Yes | Yes (grouping) | Yes |
| Facet search | Yes | No | Yes (regex) | Yes (prefix) | Yes | No | Yes |
| Highlighting | Yes | Yes (ts_headline) | Yes | Yes | Yes | Yes (bolding) | Yes (basic) |
| Geo search | Yes | Yes (Haversine) | Yes | No | Yes | Yes (geoLocation) | Yes (Haversine) |
| Similar documents | Yes (embedders) | No | Yes (MLT) | Yes (MLT) | No | No | Yes (stub) |
| Multi-search | Yes | No | Yes (_msearch) | No | Yes | No | No |
| Index swap | Yes | No | Yes (aliases) | Yes (aliases) | Yes (aliases) | No | Yes |
| Partial updates | No (full upsert) | Yes (JSONB merge) | Yes | Yes (atomic) | Yes (emplace) | Yes (assign) | Yes |
| Async tasks | Yes | No (sync) | No (sync) | No (sync) | No (sync) | No (sync) | No (sync) |
| NDJSON import | Yes | No | Yes (streaming_bulk) | No | No | No | Yes |
| Compaction | No-op (auto) | No-op | forcemerge | optimize | No-op (auto) | No-op (auto) | No-op |
| Multi-tenancy | Yes (index prefix) | Yes (index prefix) | Yes (index prefix) | Yes (index prefix) | Yes (index prefix) | Yes (index prefix) | Yes |

**Notes:**
- "Facet search" means searching within facet values for typeahead filter UIs.
- "Similar documents" requires embedders configured in Meilisearch, the MoreLikeThis
  handler configured in Solr, and nearestNeighbor tensor fields in Vespa.
- Vespa faceting uses its grouping syntax via the `facets` param; the `facet_search()`
  method is not supported.
- Multi-tenancy is implemented across all backends via `ICV_SEARCH_INDEX_PREFIX`
  and/or `ICV_SEARCH_TENANT_PREFIX_FUNC` at the index naming level.

---

## Individual Backend Guides

- [meilisearch.md](meilisearch.md): Default backend, easiest setup
- [postgresql.md](postgresql.md): Zero-infrastructure, uses your existing database
- [opensearch.md](opensearch.md): Large-scale, AWS-compatible, rich aggregations
- [solr.md](solr.md): Battle-tested, SolrCloud, deep faceting
- [typesense.md](typesense.md): Typo-tolerant, schema-enforced, instant search
- [vespa.md](vespa.md): ML ranking, hybrid search, billions of documents
- [testing.md](testing.md): DummyBackend for tests, CI Docker Compose
