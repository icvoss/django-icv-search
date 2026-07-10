# OpenSearch Backend

Distributed, production-grade search compatible with AWS OpenSearch Service and
self-managed OpenSearch clusters. The right choice when you need rich
aggregations, full-text search at scale, or an AWS-native deployment.

---

## Overview

- **Distributed**: horizontal sharding and replication across a cluster
- **Rich aggregations**: `terms`, `date_histogram`, `range`, `nested`, and more
  via the `facets` param or raw aggregation dicts
- **AWS-compatible**: supports AWS SigV4 authentication for OpenSearch Service
- **More-like-this**: `similar_documents()` uses OpenSearch's `more_like_this` query
- **Partial updates**: `update_documents()` uses bulk `update` actions, so only
  specified fields are modified

---

## Installation

```bash
pip install "django-icv-search[opensearch]"
```

This installs `opensearch-py` alongside `django-icv-search`.

---

## Docker Quick Start

Single-node for local development (security disabled for simplicity):

```bash
docker run -d \
  --name opensearch \
  -p 9200:9200 \
  -p 9600:9600 \
  -e "discovery.type=single-node" \
  -e "DISABLE_SECURITY_PLUGIN=true" \
  opensearchproject/opensearch:latest
```

Port 9200 is the REST API. Port 9600 is the performance analyser. `DISABLE_SECURITY_PLUGIN=true` disables auth for local development; never use this in production.

Verify:

```bash
curl http://localhost:9200
```

---

## Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `ICV_SEARCH_BACKEND` | (none) | Set to `"icv_search.backends.opensearch.OpenSearchBackend"` |
| `ICV_SEARCH_URL` | `"http://localhost:7700"` | OpenSearch node URL, e.g. `"https://opensearch.internal:9200"` |
| `ICV_SEARCH_API_KEY` | `""` | API key for API-key auth, or leave empty for other auth methods |
| `ICV_SEARCH_TIMEOUT` | `30` | Request timeout in seconds |
| `ICV_SEARCH_BACKEND_OPTIONS` | `{}` | Extra kwargs passed to `OpenSearchBackend.__init__()`: see below |

### `ICV_SEARCH_BACKEND_OPTIONS` Keys

| Key | Type | Description |
|-----|------|-------------|
| `basic_auth` | `[str, str]` | `[username, password]` for Basic authentication |
| `aws_region` | `str` | AWS region for SigV4 signing (e.g. `"eu-west-1"`) |
| `use_ssl` | `bool` | Force SSL on/off. Inferred from URL scheme by default |
| `verify_certs` | `bool` | Verify TLS certificates. Default `True` |
| `connection_class` | class | opensearch-py connection class. Default `RequestsHttpConnection` |

---

## Example Configurations

**Local development:**

```python
ICV_SEARCH_BACKEND = "icv_search.backends.opensearch.OpenSearchBackend"
ICV_SEARCH_URL = "http://localhost:9200"
```

**Self-managed cluster with Basic auth:**

```python
import os

ICV_SEARCH_BACKEND = "icv_search.backends.opensearch.OpenSearchBackend"
ICV_SEARCH_URL = "https://opensearch.internal:9200"
ICV_SEARCH_BACKEND_OPTIONS = {
    "basic_auth": [os.environ["OPENSEARCH_USER"], os.environ["OPENSEARCH_PASSWORD"]],
    "verify_certs": True,
}
```

**AWS OpenSearch Service:**

```python
import os

ICV_SEARCH_BACKEND = "icv_search.backends.opensearch.OpenSearchBackend"
ICV_SEARCH_URL = os.environ["OPENSEARCH_ENDPOINT"]  # https://...us-east-1.es.amazonaws.com
ICV_SEARCH_BACKEND_OPTIONS = {
    "aws_region": os.environ["AWS_REGION"],
    "use_ssl": True,
    "verify_certs": True,
}
```

AWS SigV4 signing requires `boto3` to be installed and your environment to have
valid AWS credentials (IAM role, environment variables, or `~/.aws/credentials`).

---

## Authentication Options

### API Key

Pass the API key as `ICV_SEARCH_API_KEY`. The backend sends it as HTTP Basic
auth with an empty username:

```python
ICV_SEARCH_API_KEY = "your-opensearch-api-key"
```

### Basic Auth

Pass credentials via `ICV_SEARCH_BACKEND_OPTIONS`:

```python
ICV_SEARCH_BACKEND_OPTIONS = {
    "basic_auth": ["admin", "your-password"],
}
```

### AWS SigV4

Set `aws_region` in `ICV_SEARCH_BACKEND_OPTIONS`. The backend uses `boto3` to
retrieve credentials from the standard AWS credential chain (IAM role, env
vars, config file):

```python
ICV_SEARCH_BACKEND_OPTIONS = {
    "aws_region": "eu-west-1",
    "use_ssl": True,
    "verify_certs": True,
}
```

Install `boto3` if not already present: `pip install boto3`.

---

## AWS OpenSearch Service Setup

1. Create an OpenSearch domain in the AWS console or via CloudFormation/Terraform.
2. Choose **VPC access** for production (attach your application's security group).
3. Note the domain endpoint URL (e.g. `https://search-my-domain-....eu-west-1.es.amazonaws.com`).
4. Attach an IAM policy to your application's role allowing `es:ESHttpGet`,
   `es:ESHttpPost`, `es:ESHttpPut`, `es:ESHttpDelete` on the domain ARN.
5. Set `ICV_SEARCH_URL` to the endpoint and `aws_region` in `ICV_SEARCH_BACKEND_OPTIONS`.

---

## Index Mappings and Field Types

`update_settings()` translates icv-search canonical settings to OpenSearch mappings:

| icv-search setting | OpenSearch mapping |
|-------------------|--------------------|
| `searchableAttributes` | `text` with `standard` analyser |
| `filterableAttributes` | `keyword` |
| Both searchable and filterable | `text` with `.keyword` sub-field |
| `sortableAttributes` | `keyword` (or `fielddata: true` for text-only sort) |
| `synonyms` | `synonym` filter in analysis settings (requires index close/open) |
| `stopWords` | `stop` filter in analysis settings (requires index close/open) |

Example `SearchIndex` settings in Django admin:

```json
{
  "searchableAttributes": ["title", "description"],
  "filterableAttributes": ["category", "status"],
  "sortableAttributes": ["price", "created_at"],
  "synonyms": [["mobile", "phone", "smartphone"]],
  "stopWords": ["the", "a", "an"]
}
```

---

## Facets and Aggregations

Use the `facets` param for simple `terms` aggregations:

```python
from icv_search.services import search

results = search(
    "products",
    "shoes",
    facets=["category", "brand"],
    filter={"status": "active"},
)
print(results.facet_distribution)
# {"category": {"Trainers": 142, "Boots": 87}, "brand": {"Nike": 95, ...}}
```

---

## Production Considerations

**Shard sizing**
- Aim for shards between 10 to 50 GB. Oversharding is a common mistake: a
  cluster of 50M documents typically needs 2 to 5 primary shards, not 50.
- Start with 1 primary shard per index and increase as data grows.

**Replica count**
- Set `number_of_replicas: 1` minimum for high availability. Each replica
  doubles storage and doubles read throughput.

**JVM heap**
- Allocate no more than 50% of available RAM to the JVM heap, up to a maximum
  of 32 GB (above 32 GB, compressed ordinary object pointers are disabled and
  performance degrades).

**Snapshot backups**
- Configure automated snapshots to an S3 bucket. AWS OpenSearch Service handles
  this automatically; for self-managed clusters, use the snapshot API.

**Index templates**
- For predictable field mapping across rolling indexes, define an index template
  in OpenSearch and point `ICV_SEARCH_URL` at your cluster before calling
  `create_index()`.

---

## Known Limitations

- **Synonyms and stop-words require index close/open**: applying analysis
  settings causes a brief period where the index is unavailable. Schedule
  these changes during a maintenance window on production.
- **No facet search on keyword fields without extra mapping**: `facet_search()`
  uses a `terms` aggregation with an `include` regex. On high-cardinality
  fields this can be slow; consider a dedicated completion suggester instead.
- **Geo field default is `"location"`**: if your documents use a different
  field name, pass `geo_field` in the search params.
- **Index swap uses aliases**: when `index_a` is not already an alias, the
  backend creates `index_a_live` pointing to `index_b`. Your application must
  query the alias name, not the physical index name.
