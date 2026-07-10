# Testing Guide

How to test search functionality in your Django application using
`icv-search`'s built-in testing utilities.

---

## DummyBackend

`DummyBackend` is an in-memory search backend that stores documents in a
module-level dict. It requires no running search engine and is fast enough
for unit and integration tests.

**Capabilities:**
- Full `add_documents()`, `delete_documents()`, `search()`, `facet_search()`
- Basic text matching (substring + term-frequency scoring)
- Geo radius filtering and distance sorting (Haversine)
- Highlighting via regex substitution
- `swap_indexes()`, `get_document()`, `get_documents()`, `update_documents()`
- `similar_documents()` returns all other documents (useful for verifying
  service-layer wiring)

**Limitations:**
- No real relevance ranking: term frequency only
- No typo tolerance or stemming
- Not suitable for testing relevance quality or engine-specific features

---

## Test Settings Configuration

Add this to your test settings (or `conftest.py`):

```python
# settings/test.py
ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
ICV_SEARCH_ASYNC_INDEXING = False  # run indexing synchronously in tests
ICV_SEARCH_AUTO_SYNC = False       # do not push settings to engine on save
ICV_SEARCH_DEBOUNCE_SECONDS = 0    # disable debouncing
ICV_SEARCH_LOG_QUERIES = False
ICV_SEARCH_CACHE_ENABLED = False
ICV_SEARCH_MERCHANDISING_ENABLED = False
```

---

## Using the `search_backend` Fixture

`icv_search.testing` ships with a ready-to-use pytest fixture:

```python
# conftest.py
from icv_search.testing.fixtures import search_backend  # noqa: F401
```

Or import it directly in your test file:

```python
import pytest
from icv_search.backends.dummy import DummyBackend
from icv_search.backends import reset_search_backend


@pytest.fixture
def search_backend(settings):
    settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
    reset_search_backend()
    DummyBackend.reset()
    yield DummyBackend
    DummyBackend.reset()
    reset_search_backend()
```

Use it in tests:

```python
def test_search_returns_matching_documents(search_backend, db):
    from icv_search.services import create_index, index_documents, search

    create_index("articles")
    index_documents("articles", [
        {"id": "1", "title": "Django tips", "body": "Testing Django apps"},
        {"id": "2", "title": "Postgres tricks", "body": "Advanced queries"},
    ])

    results = search("articles", "Django")

    assert results.estimated_total_hits == 1
    assert results.hits[0]["id"] == "1"
```

---

## Testing Utilities

### Factories

`icv_search.testing.factories` provides factory-boy factories for search models:

```python
from icv_search.testing.factories import SearchIndexFactory

# In a test
index = SearchIndexFactory(name="products", primary_key="id")
```

### Fixtures

```python
from icv_search.testing.fixtures import search_backend, search_index

# search_index: creates a SearchIndex instance via SearchIndexFactory
# search_backend: configures DummyBackend and resets between tests
```

### Helpers

`icv_search.testing.helpers` provides utility functions for common test
assertions:

```python
from icv_search.testing.helpers import assert_indexed, assert_not_indexed
```

---

## Resetting Between Tests

`DummyBackend.reset()` clears all in-memory indexes, documents, and settings.
The `search_backend` fixture calls it automatically in setup and teardown.

If you are not using the fixture, call reset manually:

```python
import pytest
from icv_search.backends.dummy import DummyBackend


@pytest.fixture(autouse=True)
def reset_dummy_backend():
    DummyBackend.reset()
    yield
    DummyBackend.reset()
```

---

## Testing Auto-Indexing

When `ICV_SEARCH_AUTO_INDEX` is configured, test that signals trigger indexing
correctly:

```python
def test_article_indexed_on_save(search_backend, db):
    from myapp.models import Article
    from icv_search.backends.dummy import _documents

    article = Article.objects.create(
        title="Test article",
        published=True,
    )

    # Verify the document was added to the dummy backend
    assert str(article.pk) in _documents.get("articles", {})
```

Set `ICV_SEARCH_ASYNC_INDEXING = False` in test settings so indexing happens
synchronously in the same process, without needing a Celery worker.

---

## Testing with Real Backends

For integration tests that verify engine-specific behaviour, use Docker Compose
to spin up real backend services in CI.

### docker-compose.ci.yml

```yaml
version: "3.8"
services:
  meilisearch:
    image: getmeili/meilisearch:latest
    ports:
      - "7700:7700"
    environment:
      MEILI_MASTER_KEY: "test-master-key"
      MEILI_ENV: "development"

  postgres:
    image: postgres:16
    ports:
      - "5432:5432"
    environment:
      POSTGRES_DB: test_db
      POSTGRES_USER: test_user
      POSTGRES_PASSWORD: test_pass

  opensearch:
    image: opensearchproject/opensearch:latest
    ports:
      - "9200:9200"
      - "9600:9600"
    environment:
      discovery.type: single-node
      DISABLE_SECURITY_PLUGIN: "true"
    ulimits:
      memlock:
        soft: -1
        hard: -1

  typesense:
    image: typesense/typesense:27.1
    ports:
      - "8108:8108"
    volumes:
      - typesense-data:/data
    command: ["--data-dir=/data", "--api-key=test-key", "--enable-cors"]

  solr:
    image: solr:9-slim
    ports:
      - "8983:8983"
    command: ["solr-precreate", "test_collection"]

volumes:
  typesense-data:
```

### GitHub Actions example

```yaml
jobs:
  integration-tests:
    runs-on: ubuntu-latest
    services:
      meilisearch:
        image: getmeili/meilisearch:latest
        ports:
          - 7700:7700
        env:
          MEILI_MASTER_KEY: test-master-key
          MEILI_ENV: development

    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: pip install -e ".[test,meilisearch]"
      - name: Run integration tests
        env:
          ICV_SEARCH_URL: http://localhost:7700
          ICV_SEARCH_API_KEY: test-master-key
        run: pytest tests/integration/
```

### Conditional backend tests

Use `pytest.mark.skipif` to skip real-backend tests when the service is
unavailable (useful for running the full test suite locally without Docker):

```python
import pytest
import httpx


def meilisearch_available() -> bool:
    try:
        response = httpx.get("http://localhost:7700/health", timeout=2)
        return response.json().get("status") == "available"
    except Exception:
        return False


@pytest.mark.skipif(not meilisearch_available(), reason="Meilisearch not running")
def test_meilisearch_geo_search(db):
    from icv_search.services import create_index, index_documents, search
    # ... test against real Meilisearch
```

---

## Mock Patterns for SDK Tests

When testing code that wraps vendor SDK calls (e.g. testing your own
`BaseSearchBackend` subclass), patch the SDK at the point of use:

```python
from unittest.mock import MagicMock, patch
from icv_search.backends.meilisearch import MeilisearchBackend


def test_meilisearch_backend_handles_timeout():
    import httpx
    from icv_search.exceptions import SearchTimeoutError

    backend = MeilisearchBackend(url="http://localhost:7700", api_key="key")

    with patch.object(backend._client, "request", side_effect=httpx.TimeoutException("timeout")):
        with pytest.raises(SearchTimeoutError):
            backend.search("articles", "django")
```

For the OpenSearch backend:

```python
from unittest.mock import MagicMock, patch
from icv_search.backends.opensearch import OpenSearchBackend


@patch("icv_search.backends.opensearch.OpenSearch")
def test_opensearch_backend_constructs_correctly(mock_os):
    backend = OpenSearchBackend(url="http://localhost:9200", api_key="")
    mock_os.assert_called_once()
```

Prefer the `DummyBackend` for service-layer tests and reserve mocking for
testing the backend class itself in isolation.
