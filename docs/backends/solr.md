# Solr Backend

Battle-tested, enterprise-grade search with deep faceting via the JSON Facet
API. The right choice for massive catalogues where Solr's maturity and
SolrCloud's horizontal scalability are priorities.

---

## Overview

- **Mature and stable**: Apache Solr has been production-proven for 15+ years
- **Deep faceting**: JSON Facet API supports nested, pivoted, and statistical
  facets beyond what other backends offer
- **SolrCloud**: built-in distributed search with ZooKeeper-based coordination
- **Cursor-based pagination**: efficient deep pagination via `cursorMark`
- **Atomic updates**: `update_documents()` supports Solr's modifier syntax for
  true partial field updates
- **MoreLikeThis**: `similar_documents()` uses the Solr MLT handler

---

## Installation

```bash
pip install "django-icv-search[solr]"
```

This installs `pysolr` alongside `django-icv-search`.

---

## Docker Quick Start

### Standalone Solr (development)

```bash
docker run -d \
  --name solr \
  -p 8983:8983 \
  -v $(pwd)/solr_data:/var/solr \
  solr:9-slim \
  solr-precreate my_collection
```

`solr-precreate` creates a collection with default settings on startup. For production, use config sets and the Collections API.

Access the admin UI at `http://localhost:8983/solr`.

### SolrCloud with ZooKeeper (closer to production)

```yaml
# docker-compose.yml
version: "3.8"
services:
  zookeeper:
    image: zookeeper:3.9
    ports:
      - "2181:2181"
    environment:
      ZOO_MY_ID: 1

  solr1:
    image: solr:9-slim
    ports:
      - "8983:8983"
    environment:
      ZK_HOST: zookeeper:2181
    command: ["solr", "-f", "-c", "-z", "zookeeper:2181"]
    depends_on:
      - zookeeper

  solr2:
    image: solr:9-slim
    ports:
      - "8984:8983"
    environment:
      ZK_HOST: zookeeper:2181
    command: ["solr", "-f", "-c", "-z", "zookeeper:2181"]
    depends_on:
      - zookeeper
```

```bash
docker compose up -d
```

---

## Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `ICV_SEARCH_BACKEND` | (none) | Set to `"icv_search.backends.solr.SolrBackend"` |
| `ICV_SEARCH_URL` | (none) | Solr base URL, e.g. `"http://localhost:8983/solr"` |
| `ICV_SEARCH_API_KEY` | `""` | Solr Basic Auth password. Leave empty if auth is disabled |
| `ICV_SEARCH_TIMEOUT` | `30` | Request timeout in seconds |
| `ICV_SEARCH_BACKEND_OPTIONS` | `{}` | Extra constructor kwargs: see below |

### `ICV_SEARCH_BACKEND_OPTIONS` Keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `collection_config` | `str` | `"default"` | Config set name for new collections. Must exist in ZooKeeper or on-disk |
| `commit_within` | `int` | `1000` | Milliseconds before a soft commit after document operations. Lower = less latency |
| `zookeeper_hosts` | `str` | `""` | ZooKeeper connection string for SolrCloud, e.g. `"zoo1:2181,zoo2:2181"`. When non-empty a `pysolr.SolrCloud` client is used |

---

## Example Configurations

**Standalone Solr (development):**

```python
ICV_SEARCH_BACKEND = "icv_search.backends.solr.SolrBackend"
ICV_SEARCH_URL = "http://localhost:8983/solr"
```

**SolrCloud with ZooKeeper:**

```python
import os

ICV_SEARCH_BACKEND = "icv_search.backends.solr.SolrBackend"
ICV_SEARCH_URL = os.environ["SOLR_URL"]  # e.g. http://solr1:8983/solr
ICV_SEARCH_API_KEY = os.environ.get("SOLR_PASSWORD", "")
ICV_SEARCH_BACKEND_OPTIONS = {
    "zookeeper_hosts": os.environ["ZK_HOSTS"],  # zoo1:2181,zoo2:2181
    "collection_config": "my_config",
    "commit_within": 500,
}
```

---

## SolrCloud vs Standalone Mode

The backend selects the pysolr client based on `zookeeper_hosts`:

| Mode | `zookeeper_hosts` | Client |
|------|-------------------|--------|
| Standalone | `""` (empty) | `pysolr.Solr`: connects directly to `{url}/{collection}` |
| SolrCloud | `"zoo1:2181,..."` | `pysolr.SolrCloud`: routes via ZooKeeper leader election |

For production, always use SolrCloud mode. Standalone mode is convenient for
local development but does not provide replication or automatic failover.

---

## Config Sets and Managed Schema

Solr requires a **config set** (a directory of configuration files including
`solrconfig.xml` and `managed-schema.xml`) to exist before a collection can be
created. The `create_index()` call specifies the config set name via the
`collection_config` option.

For managed schema (the default since Solr 6), the schema can be updated at
runtime via the Schema API. icv-search uses the Schema API to push
`searchableAttributes` (stored internally for `qf` construction), `synonyms`,
and `stopWords`.

Note: `filterableAttributes`, `sortableAttributes`, `rankingRules`, and
`typoTolerance` are silently skipped; configure these directly in your Solr
schema or `solrconfig.xml`.

---

## Deep Pagination with cursorMark

For browsing large result sets beyond the first few thousand hits, pass
`cursorMark` instead of `offset`:

```python
from icv_search.services import search

# First page
results = search("products", "", cursorMark="*", sort=["id"])
cursor = results.raw.get("nextCursorMark")

# Next page
results = search("products", "", cursorMark=cursor, sort=["id"])
```

`sort` must include the unique key field (`id`) when using cursor pagination.

---

## Atomic Updates

Solr supports atomic field-level updates via modifier dicts. Pass them
directly to `update_documents()`:

```python
from icv_search.services import update_documents

update_documents("products", [
    {"id": "prod-123", "stock_count": {"set": 0}},
    {"id": "prod-456", "tags": {"add": "sale"}},
])
```

---

## Production Considerations

**ZooKeeper ensemble**
- Run ZooKeeper as a 3-node ensemble (odd number required for quorum) in
  production. A single ZooKeeper node is a single point of failure.
- ZooKeeper nodes should have dedicated disks: latency spikes cause leader
  re-election and can briefly impact Solr collection availability.

**Collection sizing**
- Each shard is a separate Lucene index. Start with enough shards to distribute
  your data evenly across nodes, then add replicas for fault tolerance.
- A reasonable starting point: 1 to 2 shards per node, 2 replicas per shard.

**Auto-commit tuning**
- `commit_within` controls when documents become visible to searchers. Lower
  values (e.g. 500ms) reduce indexing latency at the cost of more frequent
  segment merges. The default of 1000ms is a good balance.
- For batch reindexing, temporarily increase `commit_within` to reduce
  segment churn, then force-commit at the end.

**Health checks**
- `health()` polls `/solr/admin/info/system` and (when SolrCloud is enabled)
  `/api/cluster`. Use this endpoint in your load balancer health check.

---

## Known Limitations

- **No facet search on text fields**: `facet_search()` uses the JSON Facet API
  `prefix` filter, which works on `string` (keyword) fields only. Text fields
  require a `copyField` to a string field for faceting.
- **Index swap is not atomic**: `swap_indexes()` uses two sequential
  `CREATEALIAS` calls. A failure on the second call leaves one alias updated
  and one not. Handle partial-swap state at the service layer.
- **MoreLikeThis requires configuration**: `similar_documents()` requires
  the MLT request handler to be configured in `solrconfig.xml`. It raises
  `SearchBackendError` if the component is not present.
- **Settings are partially applied**: only `searchableAttributes`, `synonyms`,
  and `stopWords` are pushed to Solr. All other icv-search settings are logged
  and skipped.
- **No async task queue**: all operations are synchronous; `get_task()` always
  returns a synthetic succeeded dict.
