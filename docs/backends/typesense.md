# Typesense Backend

Fast, typo-tolerant search with a strict schema and an instant-search feel.
A strong choice for medium-to-large datasets where search-as-you-type
responsiveness and easy clustering matter.

---

## Overview

- **Typo tolerance**: built-in, configurable tolerance for character transpositions
  and spelling errors
- **Schema-enforced**: field types are declared upfront; documents must match the
  schema or they are rejected
- **Fast**: written in C++ for low-latency responses
- **Simple HA clustering**: pass a `nodes` list to get multi-node redundancy with
  no ZooKeeper or complex consensus protocol
- **Partial updates**: `update_documents()` uses `action=emplace`, preserving
  existing fields not included in the update

---

## Installation

```bash
pip install "django-icv-search[typesense]"
```

This installs the `typesense` Python SDK alongside `django-icv-search`.

---

## Docker Quick Start

```bash
mkdir -p /tmp/typesense-data
docker run -d \
  --name typesense \
  -p 8108:8108 \
  -v /tmp/typesense-data:/data \
  typesense/typesense:27.1 \
  --data-dir /data \
  --api-key=your-api-key-here \
  --enable-cors
```

Use a specific version tag (e.g. `27.1`); Typesense does not publish a `latest` tag consistently. `--enable-cors` is required for browser-based clients making direct API calls.

Verify:

```bash
curl -H "X-TYPESENSE-API-KEY: your-api-key" http://localhost:8108/health
# {"ok":true}
```

---

## Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `ICV_SEARCH_BACKEND` | (none) | Set to `"icv_search.backends.typesense.TypesenseBackend"` |
| `ICV_SEARCH_URL` | (none) | Typesense server URL, e.g. `"http://localhost:8108"` |
| `ICV_SEARCH_API_KEY` | `""` | Admin or scoped API key |
| `ICV_SEARCH_TIMEOUT` | `30` | Connection timeout in seconds |
| `ICV_SEARCH_TYPESENSE_FIELD_TYPES` | `{}` | Mapping of field name → Typesense type string for schema generation |
| `ICV_SEARCH_TYPESENSE_GEO_FIELD` | `"_geo"` | Field name for geo search (type `geopoint`) |
| `ICV_SEARCH_BACKEND_OPTIONS` | `{}` | Extra constructor kwargs: see below |

### `ICV_SEARCH_BACKEND_OPTIONS` Keys

| Key | Type | Description |
|-----|------|-------------|
| `nodes` | `list[dict]` | HA cluster node list. Each dict has `host`, `port`, `protocol` |
| `connection_timeout` | `int` | Connection timeout in seconds. Defaults to `ICV_SEARCH_TIMEOUT` |

---

## Example Configurations

**Single-node (development):**

```python
ICV_SEARCH_BACKEND = "icv_search.backends.typesense.TypesenseBackend"
ICV_SEARCH_URL = "http://localhost:8108"
ICV_SEARCH_API_KEY = "your-api-key"
```

**HA cluster:**

```python
import os

ICV_SEARCH_BACKEND = "icv_search.backends.typesense.TypesenseBackend"
ICV_SEARCH_URL = "https://node1.typesense.example.com:443"
ICV_SEARCH_API_KEY = os.environ["TYPESENSE_API_KEY"]
ICV_SEARCH_BACKEND_OPTIONS = {
    "nodes": [
        {"host": "node1.typesense.example.com", "port": "443", "protocol": "https"},
        {"host": "node2.typesense.example.com", "port": "443", "protocol": "https"},
        {"host": "node3.typesense.example.com", "port": "443", "protocol": "https"},
    ],
}
```

When `nodes` is provided in `ICV_SEARCH_BACKEND_OPTIONS`, `ICV_SEARCH_URL` is
still required but is used only to satisfy the setting; the nodes list takes
precedence for connection routing.

---

## Schema and Field Type Mapping

Typesense requires every field to have an explicit type. Use
`ICV_SEARCH_TYPESENSE_FIELD_TYPES` to declare the type for each field that
appears in `searchableAttributes`, `filterableAttributes`, or
`sortableAttributes`:

```python
ICV_SEARCH_TYPESENSE_FIELD_TYPES = {
    "title": "string",
    "description": "string",
    "price": "float",
    "stock_count": "int32",
    "tags": "string[]",
    "published_at": "int64",   # Unix timestamp
    "is_active": "bool",
}
```

Fields not listed in `ICV_SEARCH_TYPESENSE_FIELD_TYPES` default to `"string"`.

**Common Typesense types:**

| Python type | Typesense type |
|-------------|----------------|
| `str` | `string` |
| `int` | `int32` or `int64` |
| `float` | `float` |
| `bool` | `bool` |
| `list[str]` | `string[]` |
| `list[int]` | `int32[]` |
| Geo coordinates | `geopoint` |

---

## Geo Search

The geo field must be declared as `geopoint` in `ICV_SEARCH_TYPESENSE_FIELD_TYPES`
and the field name must match `ICV_SEARCH_TYPESENSE_GEO_FIELD` (default `"_geo"`):

```python
ICV_SEARCH_TYPESENSE_FIELD_TYPES = {
    "_geo": "geopoint",
    # ...
}
```

Documents store coordinates as `[lat, lng]`:

```python
{"id": "1", "name": "Coffee shop", "_geo": [51.5074, -0.1278]}
```

Search with geo params:

```python
from icv_search.services import search

results = search(
    "venues",
    "coffee",
    geo_point=(51.5074, -0.1278),
    geo_radius=5000,
    geo_sort="asc",
)
```

---

## HA Cluster Configuration

Typesense uses a Raft-based consensus protocol to elect a leader. To set up a
3-node cluster:

1. Start three Typesense nodes, each with the same `--api-key` and pointing
   to each other via `--nodes` peer discovery.
2. Pass all three nodes to `ICV_SEARCH_BACKEND_OPTIONS["nodes"]` as shown above.
3. The SDK load-balances requests across nodes and retries on failure.

Minimum 3 nodes is required for a fault-tolerant cluster (1 node can fail).

---

## API Key Scoping

Generate scoped API keys for production:

- **Admin key**: full access, used by Django for indexing and management
- **Search key**: limited to search operations only, safe for client-side use

Create scoped keys via the Typesense API or admin UI. Set `ICV_SEARCH_API_KEY`
to the admin key in your Django application.

---

## Production Considerations

**RAM requirements**
- Typesense keeps its index in memory. Provision RAM generously: a rough guide
  is 2 to 4× the size of your raw document data.
- Monitor memory usage and set OS-level swap to prevent OOM kills.

**Schema changes**
- Typesense does not support renaming or changing the type of existing fields.
  Adding new fields is supported via the collection update API.
- To change a field type, you must drop and recreate the collection, then
  re-index all documents. Plan schema changes carefully before going to
  production.
- `update_settings()` uses Typesense's collection `PATCH` endpoint to add new
  fields. If you need a full schema rebuild, call `delete_index()` then
  `create_index()` followed by `update_settings()` before re-indexing.

**API key rotation**
- Rotate API keys without downtime by creating a new key, updating your
  application config, deploying, then revoking the old key.

---

## Known Limitations

- **No similar documents**: Typesense does not have a native
  more-like-this or similarity search feature. `similar_documents()` raises
  `NotImplementedError`.
- **Schema changes require field recreation**: changing the type of an
  existing field requires a full collection drop and recreate.
- **No multi-search aggregation of facets**: facet counts are per-query;
  cross-index facet merging must be done at the application layer.
- **Sorting requires a `sort: true` field**: fields used in `sort` must have
  `ICV_SEARCH_TYPESENSE_FIELD_TYPES` declared and the `sortableAttributes`
  setting configured before documents are indexed.
- **No async task queue**: all operations are synchronous; `get_task()` returns
  a synthetic succeeded dict.
