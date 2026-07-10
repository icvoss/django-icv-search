# Vespa Backend

Purpose-built for massive-scale search and recommendation with first-class
support for ML ranking, hybrid vector+text search, and real-time updates at
billions-of-documents scale.

---

## Overview

- **Massive scale**: designed for billions of documents with horizontal
  scaling across content clusters
- **ML ranking**: native support for custom ranking expressions, tensor
  features, and ONNX/TensorFlow model integration
- **Hybrid search**: combine dense vector (ANN) retrieval with traditional
  BM25 text search in a single query via `nearestNeighbor` + `userQuery()`
- **Real-time updates**: `update_documents()` uses Vespa's `assign` operator
  for field-level partial updates with no re-indexing required
- **YQL query language**: a SQL-like query language that can be passed
  directly via the `filter` param for advanced use cases

---

## Installation

```bash
pip install "django-icv-search[vespa]"
```

This installs `pyvespa` alongside `django-icv-search`.

---

## Vespa Quick Start

### Docker (local development)

```bash
docker run -d \
  --name vespa \
  --hostname vespa-container \
  -p 8080:8080 \
  -p 19071:19071 \
  vespaengine/vespa
```

Port 8080 is the search and feed API. Port 19071 is the deploy endpoint. Only one container named `vespa` can run at a time.

Wait for Vespa to start (takes ~30 seconds):

```bash
curl -s http://localhost:19071/state/v1/health
# {"status":{"code":"up"}}
```

### Vespa CLI

The Vespa CLI provides a simpler local development workflow:

```bash
# Install (macOS)
brew install vespa-cli

# Deploy your application package
vespa deploy --wait 300
```

See [Vespa documentation](https://docs.vespa.ai/en/getting-started.html) for
full application package setup.

---

## Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `ICV_SEARCH_BACKEND` | (none) | Set to `"icv_search.backends.vespa.VespaBackend"` |
| `ICV_SEARCH_URL` | (none) | Vespa application URL, e.g. `"http://localhost:8080"` or `"https://my-app.vespa-app.cloud"` |
| `ICV_SEARCH_API_KEY` | `""` | Vespa Cloud token ID, or leave empty for self-hosted with mTLS |
| `ICV_SEARCH_TIMEOUT` | `30` | Request timeout in seconds |
| `ICV_SEARCH_BACKEND_OPTIONS` | `{}` | Extra constructor kwargs: see below |

### `ICV_SEARCH_BACKEND_OPTIONS` Keys

| Key | Type | Description |
|-----|------|-------------|
| `application` | `str` | Vespa application name (informational) |
| `content_cluster` | `str` | Content cluster name. Default `"content"` |
| `schema` | `str` | Default schema name when `uid` cannot be used directly |
| `cert_path` | `str` | Path to client certificate for mTLS (Vespa Cloud) |
| `key_path` | `str` | Path to client private key for mTLS (Vespa Cloud) |

---

## Example Configurations

**Local Docker:**

```python
ICV_SEARCH_BACKEND = "icv_search.backends.vespa.VespaBackend"
ICV_SEARCH_URL = "http://localhost:8080"
```

**Vespa Cloud (mTLS):**

```python
import os

ICV_SEARCH_BACKEND = "icv_search.backends.vespa.VespaBackend"
ICV_SEARCH_URL = os.environ["VESPA_ENDPOINT"]
ICV_SEARCH_BACKEND_OPTIONS = {
    "application": "my-app",
    "content_cluster": "content",
    "cert_path": os.environ["VESPA_CERT_PATH"],
    "key_path": os.environ["VESPA_KEY_PATH"],
}
```

**Self-hosted with token auth:**

```python
import os

ICV_SEARCH_BACKEND = "icv_search.backends.vespa.VespaBackend"
ICV_SEARCH_URL = os.environ["VESPA_ENDPOINT"]
ICV_SEARCH_API_KEY = os.environ["VESPA_TOKEN"]
ICV_SEARCH_BACKEND_OPTIONS = {
    "content_cluster": "content",
    "schema": "product",
}
```

---

## Schema Deployment Workflow

Unlike other backends, Vespa schemas cannot be created at runtime. They must be
defined in an **application package** and deployed via `vespa deploy`.

### Step 1: Define your schema

Create a `.sd` file in your application package
(e.g. `application/schemas/product.sd`):

```
schema product {
    document product {
        field id type string {
            indexing: attribute | summary
        }
        field title type string {
            indexing: index | summary
            index: enable-bm25
        }
        field price type float {
            indexing: attribute | summary
        }
        field category type string {
            indexing: attribute | summary
        }
    }

    rank-profile default {
        first-phase {
            expression: bm25(title)
        }
    }
}
```

### Step 2: Deploy the application package

```bash
vespa deploy --wait 300
```

### Step 3: Register the index in icv-search

```python
from icv_search.services import create_index

# Validates connectivity and registers the UID locally.
# Does NOT create a schema in Vespa; that was done by vespa deploy.
create_index("product")
```

### Step 4: Index documents

```python
from icv_search.services import index_documents

index_documents("product", [
    {"id": "1", "title": "Widget", "price": 9.99, "category": "hardware"},
])
```

### Step 5: Reindex after schema changes

When you add or modify fields in the `.sd` file:

1. Deploy the updated application package: `vespa deploy`
2. Reindex via: `python manage.py icv_search_reindex --index product`

---

## Authentication

### Self-hosted token

Set `ICV_SEARCH_API_KEY` to your token. The backend passes it as
`auth_client_token_id` to pyvespa.

### Vespa Cloud (mTLS)

Vespa Cloud uses mutual TLS for authentication. Set `cert_path` and `key_path`
in `ICV_SEARCH_BACKEND_OPTIONS` to the paths of your data plane certificate
and private key:

```python
ICV_SEARCH_BACKEND_OPTIONS = {
    "cert_path": "/run/secrets/vespa-cert.pem",
    "key_path": "/run/secrets/vespa-key.pem",
}
```

Generate credentials via the Vespa Cloud console. Store them as secrets in your
deployment environment (Kubernetes secrets, AWS Secrets Manager, etc.).

---

## Ranking Profiles and Tensor Features

Pass a `ranking` param to use a custom ranking profile:

```python
from icv_search.services import search

results = search(
    "product",
    "widget",
    ranking="personalized",
    **{"ranking.features": {"query(user_vector)": [0.1, 0.8, 0.3]}},
)
```

For hybrid vector + text search, include a `nearestNeighbor` clause in the
`filter` param as a raw YQL fragment:

```python
results = search(
    "product",
    "widget",
    filter=(
        "nearestNeighbor(embedding, query_embedding) AND "
        "category contains 'hardware'"
    ),
    **{"ranking.features": {"query(query_embedding)": [0.1, 0.8, 0.3]}},
    ranking="hybrid",
)
```

The `embedding` field in the schema must be declared as a `tensor<float>(x[N])`
field with `hnsw` indexing.

---

## Production Considerations

**Application package management**
- Store your application package in version control alongside your Django code.
- Use CI/CD to deploy schema changes: `vespa deploy` in your deploy pipeline.
- Test schema changes on a staging cluster before deploying to production.

**Content cluster sizing**
- Content nodes store documents and serve queries. Size them based on:
  - Document count × average document size × replication factor × headroom (1.5×)
  - Typically 60 to 70% of node memory for data storage, 30% for query buffers

**Schema change caveats**
- Adding new fields is non-breaking and handled via redeploy.
- Changing the type of an existing field requires all documents to be
  re-fed after deploying the schema change.
- Removing a field requires a redeploy followed by a refeed or a
  `delete_all_docs()` and complete reingest.

**Vespa Cloud vs self-hosted**
- Vespa Cloud handles operations, upgrades, and scaling automatically.
- Self-hosted gives you full control but requires expertise in Java application
  management, ZooKeeper, and Vespa's config server.

---

## When NOT to Use Vespa

Vespa has a steep operational curve. Do not choose it if:

- Your dataset is under a few million documents: Meilisearch or OpenSearch
  will be faster to set up and easier to operate.
- Your team does not have capacity to manage a distributed Java application
  or learn YQL and Vespa's application package model.
- You only need simple keyword search without ML ranking: the operational
  overhead is not justified.
- You need index swap (zero-downtime reindex via swap): Vespa uses
  application redeployment for this, which is outside icv-search's scope.

---

## Known Limitations

- **No index swap**: `swap_indexes()` raises `NotImplementedError`. Use
  Vespa's application redeployment (`vespa deploy`) for zero-downtime schema
  updates.
- **No facet search**: `facet_search()` raises `NotImplementedError`. Use the
  `facets` param in `search()` to obtain facet counts via Vespa's grouping API.
- **No similar documents via the generic backend**: `similar_documents()` raises
  `NotImplementedError`. Implement nearest-neighbour similarity by passing a
  raw `nearestNeighbor` YQL fragment in the `filter` param to `search()`.
- **Schema lives outside Django**: Vespa schemas must be managed as files in
  the application package. `create_index()` validates connectivity and registers
  the UID locally but does not create a Vespa schema.
- **Settings are advisory only**: `update_settings()` stores settings locally
  and logs a warning. It does not push schema changes to Vespa.
- **In-memory registry is per-process**: `_index_registry` and
  `_settings_registry` are instance-level dicts. In a multi-worker deployment,
  each worker builds its own registry from `create_index()` calls made during
  startup or first use.
