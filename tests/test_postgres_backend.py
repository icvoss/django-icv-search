"""Tests for the PostgreSQL full-text search backend.

These tests require a live PostgreSQL database. They are automatically skipped
when the default SQLite test database is used.

To run them locally, point ``DATABASES`` at a PostgreSQL instance — either by
editing ``tests/settings.py`` or by providing the ``DATABASE_URL`` environment
variable (if your project's settings consume it).

Example::

    DATABASE_URL=postgres://user:pass@localhost/icv_search_test \\
        pytest tests/test_postgres_backend.py -v
"""

from __future__ import annotations

import pytest
from django.db import connection

from icv_search.backends.postgres import _META_TABLE, _TABLE, PostgresBackend
from icv_search.exceptions import IndexNotFoundError, SearchBackendError

# ---------------------------------------------------------------------------
# Skip guard — all tests in this module are PostgreSQL-only.
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="PostgreSQL backend tests require a PostgreSQL database.",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def pg_backend(db):
    """Provide a ``PostgresBackend`` instance and clean up all test data after each test.

    The ``db`` fixture (from pytest-django) ensures we are inside a transaction /
    have database access. We truncate the tables rather than relying on Django's
    test-transaction rollback so that the table-creation DDL (which is committed
    immediately by PostgreSQL) does not interfere.
    """
    backend = PostgresBackend()
    yield backend
    # Teardown — remove any data written during this test.
    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {_TABLE}")
        cursor.execute(f"DELETE FROM {_META_TABLE}")


# ---------------------------------------------------------------------------
# Index management
# ---------------------------------------------------------------------------


class TestPostgresBackendIndexManagement:
    """create_index, delete_index, update_settings, get_settings."""

    def test_create_index_returns_success(self, pg_backend):
        result = pg_backend.create_index("test_idx")
        assert result["indexUid"] == "test_idx"
        assert result["status"] == "succeeded"
        assert "taskUid" in result

    def test_create_index_is_idempotent(self, pg_backend):
        """Calling create_index twice on the same UID must not raise."""
        pg_backend.create_index("test_idx")
        result = pg_backend.create_index("test_idx", primary_key="slug")
        assert result["status"] == "succeeded"

    def test_create_index_persists_primary_key(self, pg_backend):
        pg_backend.create_index("test_idx", primary_key="slug")
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT primary_key FROM {_META_TABLE} WHERE index_uid = %s",
                ["test_idx"],
            )
            row = cursor.fetchone()
        assert row is not None
        assert row[0] == "slug"

    def test_delete_index_removes_documents(self, pg_backend):
        pg_backend.create_index("test_idx")
        pg_backend.add_documents("test_idx", [{"id": "1", "title": "Test"}])
        pg_backend.delete_index("test_idx")
        assert pg_backend.get_stats("test_idx")["numberOfDocuments"] == 0

    def test_delete_index_removes_metadata(self, pg_backend):
        pg_backend.create_index("test_idx")
        pg_backend.delete_index("test_idx")
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT COUNT(*) FROM {_META_TABLE} WHERE index_uid = %s",
                ["test_idx"],
            )
            count = cursor.fetchone()[0]
        assert count == 0

    def test_delete_nonexistent_index_is_noop(self, pg_backend):
        """Deleting an index that does not exist must not raise."""
        pg_backend.delete_index("does_not_exist")

    def test_update_settings_stores_and_retrieves(self, pg_backend):
        pg_backend.create_index("test_idx")
        settings = {"searchableAttributes": ["title", "body"]}
        pg_backend.update_settings("test_idx", settings)
        result = pg_backend.get_settings("test_idx")
        assert result["searchableAttributes"] == ["title", "body"]

    def test_update_settings_overwrites_previous(self, pg_backend):
        pg_backend.create_index("test_idx")
        pg_backend.update_settings("test_idx", {"searchableAttributes": ["title"]})
        pg_backend.update_settings("test_idx", {"searchableAttributes": ["title", "body"]})
        result = pg_backend.get_settings("test_idx")
        assert result["searchableAttributes"] == ["title", "body"]

    def test_update_settings_nonexistent_raises_index_not_found(self, pg_backend):
        with pytest.raises(IndexNotFoundError):
            pg_backend.update_settings("nonexistent", {"foo": "bar"})

    def test_get_settings_nonexistent_raises_index_not_found(self, pg_backend):
        with pytest.raises(IndexNotFoundError):
            pg_backend.get_settings("nonexistent")

    def test_get_settings_returns_empty_dict_by_default(self, pg_backend):
        pg_backend.create_index("test_idx")
        result = pg_backend.get_settings("test_idx")
        assert result == {}


# ---------------------------------------------------------------------------
# Document operations
# ---------------------------------------------------------------------------


class TestPostgresBackendDocuments:
    """add_documents, delete_documents, clear_documents, upsert semantics."""

    def test_add_documents_returns_success(self, pg_backend):
        pg_backend.create_index("test_idx")
        result = pg_backend.add_documents("test_idx", [{"id": "1", "title": "Hello"}])
        assert result["status"] == "succeeded"
        assert result["indexUid"] == "test_idx"
        assert "taskUid" in result

    def test_add_documents_increases_count(self, pg_backend):
        pg_backend.create_index("test_idx")
        pg_backend.add_documents(
            "test_idx",
            [{"id": "1", "title": "A"}, {"id": "2", "title": "B"}],
        )
        assert pg_backend.get_stats("test_idx")["numberOfDocuments"] == 2

    def test_add_documents_empty_list_is_noop(self, pg_backend):
        pg_backend.create_index("test_idx")
        result = pg_backend.add_documents("test_idx", [])
        assert result["status"] == "succeeded"
        assert pg_backend.get_stats("test_idx")["numberOfDocuments"] == 0

    def test_add_documents_upserts_on_duplicate_id(self, pg_backend):
        pg_backend.create_index("test_idx")
        pg_backend.add_documents("test_idx", [{"id": "1", "title": "V1"}])
        pg_backend.add_documents("test_idx", [{"id": "1", "title": "V2"}])
        # Still only one document
        assert pg_backend.get_stats("test_idx")["numberOfDocuments"] == 1
        # Body reflects the latest version
        result = pg_backend.search("test_idx", "V2")
        assert len(result["hits"]) == 1
        assert result["hits"][0]["title"] == "V2"

    def test_delete_documents_removes_specified_ids(self, pg_backend):
        pg_backend.create_index("test_idx")
        pg_backend.add_documents(
            "test_idx",
            [{"id": "1", "title": "Keep"}, {"id": "2", "title": "Remove"}],
        )
        pg_backend.delete_documents("test_idx", ["2"])
        assert pg_backend.get_stats("test_idx")["numberOfDocuments"] == 1

    def test_delete_documents_leaves_others_intact(self, pg_backend):
        pg_backend.create_index("test_idx")
        pg_backend.add_documents(
            "test_idx",
            [{"id": "1", "title": "Keep"}, {"id": "2", "title": "Remove"}],
        )
        pg_backend.delete_documents("test_idx", ["2"])
        result = pg_backend.search("test_idx", "")
        assert result["hits"][0]["id"] == "1"

    def test_delete_documents_returns_success(self, pg_backend):
        pg_backend.create_index("test_idx")
        result = pg_backend.delete_documents("test_idx", ["nonexistent"])
        assert result["status"] == "succeeded"

    def test_delete_documents_empty_list_is_noop(self, pg_backend):
        pg_backend.create_index("test_idx")
        pg_backend.add_documents("test_idx", [{"id": "1", "title": "Keep"}])
        pg_backend.delete_documents("test_idx", [])
        assert pg_backend.get_stats("test_idx")["numberOfDocuments"] == 1

    def test_clear_documents_removes_all(self, pg_backend):
        pg_backend.create_index("test_idx")
        pg_backend.add_documents(
            "test_idx",
            [{"id": "1", "title": "A"}, {"id": "2", "title": "B"}],
        )
        pg_backend.clear_documents("test_idx")
        assert pg_backend.get_stats("test_idx")["numberOfDocuments"] == 0

    def test_clear_documents_returns_success(self, pg_backend):
        pg_backend.create_index("test_idx")
        result = pg_backend.clear_documents("test_idx")
        assert result["status"] == "succeeded"
        assert result["indexUid"] == "test_idx"

    def test_clear_documents_does_not_delete_index_metadata(self, pg_backend):
        pg_backend.create_index("test_idx")
        pg_backend.add_documents("test_idx", [{"id": "1", "title": "A"}])
        pg_backend.clear_documents("test_idx")
        # Metadata row should still exist
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT COUNT(*) FROM {_META_TABLE} WHERE index_uid = %s",
                ["test_idx"],
            )
            count = cursor.fetchone()[0]
        assert count == 1

    def test_documents_isolated_by_index_uid(self, pg_backend):
        pg_backend.create_index("idx_a")
        pg_backend.create_index("idx_b")
        pg_backend.add_documents("idx_a", [{"id": "1", "title": "Alpha doc"}])
        pg_backend.add_documents("idx_b", [{"id": "1", "title": "Beta doc"}])
        result_a = pg_backend.search("idx_a", "")
        result_b = pg_backend.search("idx_b", "")
        assert result_a["hits"][0]["title"] == "Alpha doc"
        assert result_b["hits"][0]["title"] == "Beta doc"


# ---------------------------------------------------------------------------
# Search — full-text matching
# ---------------------------------------------------------------------------


class TestPostgresBackendSearchFullText:
    """Full-text query matching, empty query, and searchable attributes."""

    @pytest.fixture(autouse=True)
    def _setup(self, pg_backend):
        self.backend = pg_backend
        pg_backend.create_index("articles")
        pg_backend.add_documents(
            "articles",
            [
                {"id": "1", "title": "Django REST Framework Guide", "body": "Build APIs with Django"},
                {"id": "2", "title": "Python Testing Best Practices", "body": "Write better tests"},
                {"id": "3", "title": "Advanced Django Patterns", "body": "Master Django architecture"},
            ],
        )

    def test_search_returns_matching_documents(self):
        result = self.backend.search("articles", "Django")
        titles = [h["title"] for h in result["hits"]]
        assert "Django REST Framework Guide" in titles
        assert "Advanced Django Patterns" in titles

    def test_search_excludes_non_matching_documents(self):
        result = self.backend.search("articles", "Django")
        titles = [h["title"] for h in result["hits"]]
        assert "Python Testing Best Practices" not in titles

    def test_empty_query_returns_all_documents(self):
        result = self.backend.search("articles", "")
        assert result["estimatedTotalHits"] == 3

    def test_search_includes_query_in_response(self):
        result = self.backend.search("articles", "Python")
        assert result["query"] == "Python"

    def test_search_includes_estimated_total_hits(self):
        result = self.backend.search("articles", "")
        assert "estimatedTotalHits" in result
        assert result["estimatedTotalHits"] == 3

    def test_search_includes_processing_time_ms(self):
        result = self.backend.search("articles", "Django")
        assert "processingTimeMs" in result
        assert isinstance(result["processingTimeMs"], int)
        assert result["processingTimeMs"] >= 0

    def test_search_includes_limit_and_offset(self):
        result = self.backend.search("articles", "", limit=5, offset=0)
        assert result["limit"] == 5
        assert result["offset"] == 0

    def test_search_no_matches_returns_empty_hits(self):
        result = self.backend.search("articles", "zzznomatch")
        assert result["hits"] == []
        assert result["estimatedTotalHits"] == 0

    def test_search_respects_searchable_attributes(self, pg_backend):
        """When searchableAttributes is set, only those fields contribute to tsvector."""
        pg_backend.create_index("restricted")
        pg_backend.update_settings("restricted", {"searchableAttributes": ["title"]})
        pg_backend.add_documents(
            "restricted",
            [
                {"id": "1", "title": "Only the title is searchable", "body": "hidden content"},
                {"id": "2", "title": "Unrelated title", "body": "hidden should match"},
            ],
        )
        # "hidden" appears only in body — should NOT match because body is not searchable.
        result = pg_backend.search("restricted", "hidden")
        assert result["estimatedTotalHits"] == 0

    def test_search_all_fields_when_no_searchable_attributes(self, pg_backend):
        """Without searchableAttributes configured, all string fields are searchable."""
        pg_backend.create_index("open")
        pg_backend.add_documents(
            "open",
            [{"id": "1", "title": "Unrelated", "body": "unique keyword here"}],
        )
        result = pg_backend.search("open", "unique")
        assert result["estimatedTotalHits"] == 1

    def test_add_documents_builds_search_vector(self, pg_backend):
        """add_documents should populate search_vector directly, not just via search behaviour."""
        pg_backend.create_index("vectors")
        pg_backend.add_documents("vectors", [{"id": "1", "title": "Django Framework"}])
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT search_vector FROM {_TABLE} WHERE index_uid = %s AND doc_id = %s",
                ["vectors", "1"],
            )
            row = cursor.fetchone()
        assert row is not None
        assert row[0] is not None


# ---------------------------------------------------------------------------
# Search — pagination
# ---------------------------------------------------------------------------


class TestPostgresBackendSearchPagination:
    """limit, offset, and total hit counts."""

    @pytest.fixture(autouse=True)
    def _setup(self, pg_backend):
        self.backend = pg_backend
        pg_backend.create_index("items")
        pg_backend.add_documents(
            "items",
            [{"id": str(i), "title": f"Item {i}"} for i in range(1, 11)],
        )

    def test_limit_restricts_result_count(self):
        result = self.backend.search("items", "", limit=3)
        assert len(result["hits"]) == 3

    def test_offset_skips_results(self):
        result_all = self.backend.search("items", "", limit=100, offset=0)
        result_offset = self.backend.search("items", "", limit=100, offset=5)
        assert len(result_offset["hits"]) == len(result_all["hits"]) - 5

    def test_total_hits_reflects_full_match_count(self):
        result = self.backend.search("items", "", limit=3, offset=0)
        assert result["estimatedTotalHits"] == 10

    def test_default_limit_is_20(self):
        result = self.backend.search("items", "")
        assert result["limit"] == 20

    def test_default_offset_is_0(self):
        result = self.backend.search("items", "")
        assert result["offset"] == 0


# ---------------------------------------------------------------------------
# Search — filtering
# ---------------------------------------------------------------------------


class TestPostgresBackendSearchFiltering:
    """Dict filter: string, boolean, numeric, and list (IN) values."""

    @pytest.fixture(autouse=True)
    def _setup(self, pg_backend):
        self.backend = pg_backend
        pg_backend.create_index("products")
        pg_backend.add_documents(
            "products",
            [
                {"id": "1", "name": "Padel Racket Pro", "category": "equipment", "price": 150, "is_active": True},
                {"id": "2", "name": "Tennis Ball Pack", "category": "accessories", "price": 10, "is_active": True},
                {"id": "3", "name": "Padel Court Shoes", "category": "equipment", "price": 80, "is_active": False},
                {"id": "4", "name": "Padel Grip Tape", "category": "accessories", "price": 5, "is_active": True},
            ],
        )

    def test_filter_by_string_field(self):
        result = self.backend.search("products", "", filter={"category": "equipment"})
        assert result["estimatedTotalHits"] == 2
        for hit in result["hits"]:
            assert hit["category"] == "equipment"

    def test_filter_by_boolean_true(self):
        result = self.backend.search("products", "", filter={"is_active": True})
        assert result["estimatedTotalHits"] == 3

    def test_filter_by_boolean_false(self):
        result = self.backend.search("products", "", filter={"is_active": False})
        assert result["estimatedTotalHits"] == 1
        assert result["hits"][0]["id"] == "3"

    def test_filter_by_numeric_equality(self):
        result = self.backend.search("products", "", filter={"price": 10})
        assert result["estimatedTotalHits"] == 1
        assert result["hits"][0]["name"] == "Tennis Ball Pack"

    def test_filter_by_list_in(self):
        result = self.backend.search("products", "", filter={"id": ["1", "3"]})
        assert result["estimatedTotalHits"] == 2

    def test_filter_combined_with_query(self):
        result = self.backend.search("products", "padel", filter={"category": "equipment"})
        names = [h["name"] for h in result["hits"]]
        assert all("Padel" in n or "padel" in n.lower() for n in names)
        for hit in result["hits"]:
            assert hit["category"] == "equipment"

    def test_no_filter_returns_all(self):
        result = self.backend.search("products", "")
        assert result["estimatedTotalHits"] == 4

    def test_filter_combines_string_and_boolean_fields(self):
        """Multiple filter keys of different types should combine as AND."""
        result = self.backend.search("products", "", filter={"category": "equipment", "is_active": False})
        assert result["estimatedTotalHits"] == 1
        assert result["hits"][0]["id"] == "3"


# ---------------------------------------------------------------------------
# Search — sorting
# ---------------------------------------------------------------------------


class TestPostgresBackendSearchSorting:
    """Sort by field with ascending / descending support."""

    @pytest.fixture(autouse=True)
    def _setup(self, pg_backend):
        self.backend = pg_backend
        pg_backend.create_index("products")
        pg_backend.add_documents(
            "products",
            [
                {"id": "1", "name": "Expensive", "price": 200},
                {"id": "2", "name": "Cheap", "price": 10},
                {"id": "3", "name": "Mid-range", "price": 75},
            ],
        )

    def test_sort_ascending(self):
        result = self.backend.search("products", "", sort=["price"])
        prices = [float(h["price"]) for h in result["hits"]]
        assert prices == sorted(prices)

    def test_sort_descending(self):
        result = self.backend.search("products", "", sort=["-price"])
        prices = [float(h["price"]) for h in result["hits"]]
        assert prices == sorted(prices, reverse=True)


# ---------------------------------------------------------------------------
# Stats and health
# ---------------------------------------------------------------------------


class TestPostgresBackendStatsAndHealth:
    """get_stats, health, get_task."""

    def test_get_stats_returns_document_count(self, pg_backend):
        pg_backend.create_index("test_idx")
        pg_backend.add_documents(
            "test_idx",
            [{"id": "1"}, {"id": "2"}],
        )
        stats = pg_backend.get_stats("test_idx")
        assert stats["numberOfDocuments"] == 2

    def test_get_stats_zero_for_empty_index(self, pg_backend):
        pg_backend.create_index("test_idx")
        stats = pg_backend.get_stats("test_idx")
        assert stats["numberOfDocuments"] == 0

    def test_get_stats_is_indexing_is_false(self, pg_backend):
        pg_backend.create_index("test_idx")
        stats = pg_backend.get_stats("test_idx")
        assert stats["isIndexing"] is False

    def test_health_returns_true(self, pg_backend):
        assert pg_backend.health() is True

    def test_get_task_always_returns_succeeded(self, pg_backend):
        result = pg_backend.get_task("any-uid-12345")
        assert result["status"] == "succeeded"
        assert result["uid"] == "any-uid-12345"


# ---------------------------------------------------------------------------
# Table bootstrap
# ---------------------------------------------------------------------------


class TestPostgresBackendTableBootstrap:
    """Tables are created on first instantiation."""

    def test_document_table_exists_after_init(self, pg_backend):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)",
                [_TABLE],
            )
            exists = cursor.fetchone()[0]
        assert exists is True

    def test_meta_table_exists_after_init(self, pg_backend):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)",
                [_META_TABLE],
            )
            exists = cursor.fetchone()[0]
        assert exists is True

    def test_ensure_tables_is_idempotent(self, pg_backend):
        """Calling _ensure_tables again on an existing schema must not raise."""
        pg_backend._ensure_tables()

    def test_creates_gin_and_btree_indexes_on_document_table(self, pg_backend):
        """Backend should create a GIN index on search_vector and a B-tree index on index_uid."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = %s",
                [_TABLE],
            )
            index_names = [row[0] for row in cursor.fetchall()]
        assert any("search_vector" in name for name in index_names)
        assert any("index_uid" in name for name in index_names)


# ---------------------------------------------------------------------------
# attributesToRetrieve filtering (BR-010)
# ---------------------------------------------------------------------------


class TestPostgresBackendAttributesToRetrieve:
    """attributesToRetrieve filters returned hit fields; 'id' is always present."""

    @pytest.fixture(autouse=True)
    def _setup(self, pg_backend):
        self.backend = pg_backend
        self.backend.create_index("products")
        self.backend.add_documents(
            "products",
            [
                {"id": "1", "name": "Widget", "price": 9.99, "category": "tools"},
                {"id": "2", "name": "Gadget", "price": 19.99, "category": "electronics"},
            ],
        )

    def test_filters_hits_to_requested_attributes(self):
        result = self.backend.search("products", "", attributesToRetrieve=["name"])
        for hit in result["hits"]:
            assert set(hit.keys()) == {"id", "name"}

    def test_id_always_included_even_if_not_listed(self):
        result = self.backend.search("products", "", attributesToRetrieve=["name"])
        for hit in result["hits"]:
            assert "id" in hit

    def test_multiple_attributes_returned(self):
        result = self.backend.search("products", "", attributesToRetrieve=["name", "price"])
        for hit in result["hits"]:
            assert set(hit.keys()) == {"id", "name", "price"}

    def test_unlisted_attributes_excluded(self):
        result = self.backend.search("products", "", attributesToRetrieve=["name"])
        for hit in result["hits"]:
            assert "price" not in hit
            assert "category" not in hit

    def test_no_filter_when_param_absent(self):
        result = self.backend.search("products", "")
        for hit in result["hits"]:
            assert {"id", "name", "price", "category"}.issubset(hit.keys())

    def test_filters_formatted_hits_when_highlighting(self):
        result = self.backend.search(
            "products",
            "Widget",
            attributesToRetrieve=["name"],
            highlight_fields=["name"],
        )
        assert "formatted_hits" in result
        for hit in result["formatted_hits"]:
            assert "price" not in hit
            assert "id" in hit
            assert "name" in hit

    def test_id_in_attributes_to_retrieve_not_duplicated(self):
        """Listing 'id' explicitly should not cause duplicate keys or errors."""
        result = self.backend.search("products", "", attributesToRetrieve=["id", "name"])
        for hit in result["hits"]:
            assert set(hit.keys()) == {"id", "name"}


# ---------------------------------------------------------------------------
# get_document
# ---------------------------------------------------------------------------


class TestPostgresBackendGetDocument:
    """get_document — fetch a single document by primary key."""

    @pytest.fixture(autouse=True)
    def _setup(self, pg_backend):
        self.backend = pg_backend
        pg_backend.create_index("docs")
        pg_backend.add_documents(
            "docs",
            [
                {"id": "1", "title": "Hello", "body": "World"},
                {"id": "2", "title": "Foo", "body": "Bar"},
            ],
        )

    def test_returns_correct_document(self):
        doc = self.backend.get_document("docs", "1")
        assert doc["title"] == "Hello"
        assert doc["body"] == "World"

    def test_returned_document_includes_id_field(self):
        doc = self.backend.get_document("docs", "2")
        assert "id" in doc
        assert doc["id"] == "2"

    def test_nonexistent_index_raises_index_not_found_error(self, pg_backend):
        with pytest.raises(IndexNotFoundError):
            pg_backend.get_document("no_such_index", "1")

    def test_nonexistent_document_raises_search_backend_error(self):
        with pytest.raises(SearchBackendError):
            self.backend.get_document("docs", "999")


# ---------------------------------------------------------------------------
# get_documents
# ---------------------------------------------------------------------------


class TestPostgresBackendGetDocuments:
    """get_documents — browse mode and ID-based fetch."""

    @pytest.fixture(autouse=True)
    def _setup(self, pg_backend):
        self.backend = pg_backend
        pg_backend.create_index("items")
        pg_backend.add_documents(
            "items",
            [{"id": str(i), "name": f"Item {i}", "category": "things"} for i in range(1, 7)],
        )

    def test_browse_mode_respects_limit(self):
        results = self.backend.get_documents("items", limit=3)
        assert len(results) == 3

    def test_browse_mode_respects_offset(self):
        all_results = self.backend.get_documents("items", limit=100, offset=0)
        offset_results = self.backend.get_documents("items", limit=100, offset=4)
        assert len(offset_results) == len(all_results) - 4

    def test_browse_mode_results_include_id(self):
        results = self.backend.get_documents("items", limit=2)
        for doc in results:
            assert "id" in doc

    def test_id_based_fetch_returns_only_requested_documents(self):
        results = self.backend.get_documents("items", document_ids=["2", "4"])
        ids = {doc["id"] for doc in results}
        assert ids == {"2", "4"}

    def test_id_based_fetch_excludes_unrequested_documents(self):
        results = self.backend.get_documents("items", document_ids=["1"])
        assert len(results) == 1
        assert results[0]["id"] == "1"

    def test_fields_parameter_filters_returned_keys(self):
        results = self.backend.get_documents("items", limit=2, fields=["name"])
        for doc in results:
            assert "name" in doc
            assert "category" not in doc

    def test_fields_parameter_always_includes_id(self):
        results = self.backend.get_documents("items", limit=2, fields=["name"])
        for doc in results:
            assert "id" in doc

    def test_fields_parameter_with_id_based_fetch(self):
        results = self.backend.get_documents("items", document_ids=["3", "5"], fields=["name"])
        for doc in results:
            assert set(doc.keys()) == {"id", "name"}


# ---------------------------------------------------------------------------
# update_documents
# ---------------------------------------------------------------------------


class TestPostgresBackendUpdateDocuments:
    """update_documents — partial JSONB merge and upsert behaviour."""

    @pytest.fixture(autouse=True)
    def _setup(self, pg_backend):
        self.backend = pg_backend
        pg_backend.create_index("articles")
        pg_backend.add_documents(
            "articles",
            [{"id": "1", "title": "Original Title", "status": "draft", "views": 0}],
        )

    def test_partial_update_preserves_untouched_fields(self):
        self.backend.update_documents("articles", [{"id": "1", "status": "published"}])
        doc = self.backend.get_document("articles", "1")
        assert doc["title"] == "Original Title"
        assert doc["views"] == 0

    def test_partial_update_writes_supplied_fields(self):
        self.backend.update_documents("articles", [{"id": "1", "status": "published"}])
        doc = self.backend.get_document("articles", "1")
        assert doc["status"] == "published"

    def test_update_nonexistent_document_inserts_it(self):
        self.backend.update_documents("articles", [{"id": "99", "title": "Brand New", "status": "draft"}])
        doc = self.backend.get_document("articles", "99")
        assert doc["title"] == "Brand New"

    def test_returns_status_succeeded(self):
        result = self.backend.update_documents("articles", [{"id": "1", "title": "Updated"}])
        assert result["status"] == "succeeded"

    def test_returns_index_uid_in_response(self):
        result = self.backend.update_documents("articles", [{"id": "1", "title": "Updated"}])
        assert result["indexUid"] == "articles"

    def test_returns_task_uid_in_response(self):
        result = self.backend.update_documents("articles", [{"id": "1", "title": "Updated"}])
        assert "taskUid" in result

    def test_empty_list_returns_succeeded_without_mutation(self):
        result = self.backend.update_documents("articles", [])
        assert result["status"] == "succeeded"
        # Existing document is untouched.
        doc = self.backend.get_document("articles", "1")
        assert doc["title"] == "Original Title"
