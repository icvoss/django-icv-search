# Meilisearch Backend

The default backend. No extra dependencies: `httpx` is already bundled with
`django-icv-search`. Ideal for getting search running in minutes.

---

## Overview

- **Default backend**: works out of the box without changing `ICV_SEARCH_BACKEND`
- **No SDK dependency**: communicates directly with the Meilisearch HTTP API via `httpx`
- **Feature-complete**: full-text search, filtering, sorting, faceting, facet search,
  highlighting, geo search, similar documents, multi-search, index swap, NDJSON import
- **Performance sweet spot**: excellent up to ~10 to 20M documents on a single node

---

## Installation

No extra packages required:

```bash
pip install django-icv-search
```

---

## Docker Quick Start

```bash
docker run -d \
  --name meilisearch \
  -p 7700:7700 \
  -e MEILI_MASTER_KEY='your-master-key-here' \
  -v $(pwd)/meili_data:/meili_data \
  getmeili/meilisearch:latest
```

Set `MEILI_MASTER_KEY` to enable authentication. Without it, Meilisearch runs in development mode with no auth; never use this in production.

Verify it is running:

```bash
curl http://localhost:7700/health
# {"status":"available"}
```

---

## Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `ICV_SEARCH_BACKEND` | `"icv_search.backends.meilisearch.MeilisearchBackend"` | This is the default: no change needed |
| `ICV_SEARCH_URL` | `"http://localhost:7700"` | Meilisearch instance URL |
| `ICV_SEARCH_API_KEY` | `""` | Master key or a scoped search API key |
| `ICV_SEARCH_TIMEOUT` | `30` | Request timeout in seconds |

`ICV_SEARCH_URL` and `ICV_SEARCH_API_KEY` are the only settings you typically
need to change.

---

## Example Configuration

**Development (local Docker):**

```python
# settings/local.py
ICV_SEARCH_BACKEND = "icv_search.backends.meilisearch.MeilisearchBackend"
ICV_SEARCH_URL = "http://localhost:7700"
ICV_SEARCH_API_KEY = "dev-master-key"
```

**Production:**

```python
# settings/production.py
import os

ICV_SEARCH_BACKEND = "icv_search.backends.meilisearch.MeilisearchBackend"
ICV_SEARCH_URL = os.environ["MEILISEARCH_URL"]
ICV_SEARCH_API_KEY = os.environ["MEILISEARCH_API_KEY"]
ICV_SEARCH_TIMEOUT = 10
ICV_SEARCH_ASYNC_INDEXING = True
ICV_SEARCH_INDEX_PREFIX = "prod_"
```

---

## API Key Pattern

Meilisearch distinguishes between the **master key** (full admin access) and
**API keys** (scoped permissions). In production you should:

1. Start Meilisearch with a master key set via `MEILI_MASTER_KEY`.
2. Use the master key to create a scoped **search key** (read-only) for your
   frontend and a scoped **indexing key** for your Django application.
3. Never expose the master key in client-side code.

```bash
# Create a scoped key for Django indexing
curl -X POST http://localhost:7700/keys \
  -H "Authorization: Bearer your-master-key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Django indexing key",
    "actions": ["indexes.*", "documents.*", "tasks.*", "settings.*"],
    "indexes": ["*"],
    "expiresAt": null
  }'
```

Set `ICV_SEARCH_API_KEY` to the `key` value returned.

---

## Geo Search

Documents that need geo search must include a `_geo` field:

```python
{"id": "1", "title": "Coffee shop", "_geo": {"lat": 51.5074, "lng": -0.1278}}
```

Pass `geo_point`, `geo_radius`, and `geo_sort` to `search()`:

```python
from icv_search.services import search

results = search(
    "venues",
    "coffee",
    geo_point=(51.5074, -0.1278),
    geo_radius=5000,   # metres
    geo_sort="asc",
)
```

---

## Similar Documents

Requires embedders to be configured on the index in Meilisearch (v1.6+):

```python
from icv_search.services import similar_documents

results = similar_documents("products", document_id="prod-42")
```

Configure embedders via `update_settings()` or directly in the Meilisearch
dashboard before calling `similar_documents()`.

---

## Production Considerations

**API key security**
- Never commit API keys to source control; use environment variables.
- Use the master/search key pattern: restrict `ICV_SEARCH_API_KEY` to only
  the actions your application needs.

**Index size limits**
- A single Meilisearch instance is limited by available RAM. As a rough guide,
  allow ~2 to 3× the uncompressed size of your indexed data as RAM.
- Meilisearch loads its entire index into memory. 10 to 20M small documents on a
  single node is a practical ceiling; above this consider OpenSearch or Solr.

**Async indexing**
- Enable `ICV_SEARCH_ASYNC_INDEXING = True` (the default) so document writes
  happen in Celery tasks and do not block your HTTP request cycle.

**Environment prefixes**
- Use `ICV_SEARCH_INDEX_PREFIX = "prod_"` (or `"staging_"`) to prevent index
  name collisions when running multiple environments against the same instance.

**Zero-downtime reindex**
- Use `reindex_zero_downtime()` from `icv_search.services` when you need to
  rebuild an index without a search outage. It creates a temporary index,
  populates it, then calls `swap_indexes()` to make it live atomically.

---

## Known Limitations

- **Single-node only**: Meilisearch does not support horizontal sharding across
  multiple nodes in the community edition. For distributed search, use OpenSearch.
- **No native partial updates**: `update_documents()` performs a full document
  upsert (replaces the entire document). Use `add_documents()` with the complete
  document if you need to change a single field.
- **Async task queue**: index operations return a task UID; the engine processes
  them asynchronously. If you need to confirm completion, poll `get_task()` or
  use Meilisearch's `waitForTask` pattern. icv-search does not automatically wait.
- **Performance ceiling**: above ~20M documents, query latency begins to
  degrade on typical hardware. Consider migrating to OpenSearch at that scale.
