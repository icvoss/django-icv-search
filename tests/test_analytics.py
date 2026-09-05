"""Tests for search analytics — query logging and analytics service functions."""

from __future__ import annotations

import pytest

from icv_search.backends import reset_search_backend
from icv_search.backends.dummy import DummyBackend
from icv_search.services import (
    clear_query_aggregates,
    clear_query_logs,
    create_index,
    get_popular_queries,
    get_query_trend,
    get_search_stats,
    get_zero_result_queries,
    index_documents,
    search,
)
from icv_search.testing.factories import SearchQueryAggregateFactory, SearchQueryLogFactory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def use_dummy_backend(settings):
    """Use DummyBackend and reset state between tests."""
    settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
    settings.ICV_SEARCH_AUTO_SYNC = False
    settings.ICV_SEARCH_LOG_QUERIES = False
    settings.ICV_SEARCH_LOG_ZERO_RESULTS_ONLY = False
    reset_search_backend()
    DummyBackend.reset()
    yield
    DummyBackend.reset()
    reset_search_backend()


# ---------------------------------------------------------------------------
# Query logging — disabled by default
# ---------------------------------------------------------------------------


class TestQueryLoggingDisabledByDefault:
    """Logging must be opt-in via ICV_SEARCH_LOG_QUERIES."""

    @pytest.mark.django_db
    def test_no_log_created_when_logging_off(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = False
        index = create_index("articles")
        search(index, "hello")

        assert SearchQueryLog.objects.count() == 0

    @pytest.mark.django_db
    def test_log_created_when_logging_enabled(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        index = create_index("articles")
        search(index, "hello")

        assert SearchQueryLog.objects.count() == 1

    @pytest.mark.django_db
    def test_log_records_index_name(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        index = create_index("articles")
        search(index, "hello")

        log = SearchQueryLog.objects.get()
        assert log.index_name == "articles"

    @pytest.mark.django_db
    def test_log_records_query_string(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        index = create_index("articles")
        search(index, "django rest framework")

        log = SearchQueryLog.objects.get()
        assert log.query == "django rest framework"

    @pytest.mark.django_db
    def test_log_records_hit_count(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        index = create_index("articles")
        index_documents(index, [{"id": "1", "title": "Django tips"}])
        search(index, "Django")

        log = SearchQueryLog.objects.get()
        assert log.hit_count == 1

    @pytest.mark.django_db
    def test_log_records_processing_time(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        index = create_index("articles")
        search(index, "hello")

        log = SearchQueryLog.objects.get()
        assert isinstance(log.processing_time_ms, int)


# ---------------------------------------------------------------------------
# log_query parameter
# ---------------------------------------------------------------------------


class TestLogQueryParameter:
    """log_query=False must suppress SearchQueryLog creation."""

    @pytest.mark.django_db
    def test_no_log_when_log_query_false(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        index = create_index("articles")
        search(index, "hello", log_query=False)

        assert SearchQueryLog.objects.count() == 0

    @pytest.mark.django_db
    def test_log_created_when_log_query_true(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        index = create_index("articles")
        search(index, "hello", log_query=True)

        assert SearchQueryLog.objects.count() == 1

    @pytest.mark.django_db
    def test_log_query_defaults_to_true(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        index = create_index("articles")
        search(index, "hello")

        assert SearchQueryLog.objects.count() == 1


# ---------------------------------------------------------------------------
# Zero-result detection
# ---------------------------------------------------------------------------


class TestZeroResultDetection:
    """is_zero_result must be set correctly."""

    @pytest.mark.django_db
    def test_is_zero_result_false_when_hits_found(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        index = create_index("articles")
        index_documents(index, [{"id": "1", "title": "Django"}])
        search(index, "Django")

        log = SearchQueryLog.objects.get()
        assert log.is_zero_result is False

    @pytest.mark.django_db
    def test_is_zero_result_true_when_no_hits(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        index = create_index("articles")
        search(index, "xyzzy_no_match_please")

        log = SearchQueryLog.objects.get()
        assert log.is_zero_result is True


# ---------------------------------------------------------------------------
# LOG_ZERO_RESULTS_ONLY
# ---------------------------------------------------------------------------


class TestZeroResultsOnlyMode:
    """When LOG_ZERO_RESULTS_ONLY is True, only zero-result queries are logged."""

    @pytest.mark.django_db
    def test_hit_query_not_logged_when_zero_only(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_ZERO_RESULTS_ONLY = True
        index = create_index("articles")
        index_documents(index, [{"id": "1", "title": "Django"}])
        search(index, "Django")

        assert SearchQueryLog.objects.count() == 0

    @pytest.mark.django_db
    def test_zero_result_query_logged_when_zero_only(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_ZERO_RESULTS_ONLY = True
        index = create_index("articles")
        search(index, "xyzzy_no_match")

        assert SearchQueryLog.objects.count() == 1


# ---------------------------------------------------------------------------
# user and metadata params
# ---------------------------------------------------------------------------


class TestUserAndMetadata:
    """User and metadata are stored on the log when provided."""

    @pytest.mark.django_db
    def test_anonymous_user_stored_as_null(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        index = create_index("articles")
        search(index, "hello", user=None)

        log = SearchQueryLog.objects.get()
        assert log.user_id is None

    @pytest.mark.django_db
    def test_metadata_stored_on_log(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        index = create_index("articles")
        search(index, "hello", metadata={"page": "homepage", "variant": "A"})

        log = SearchQueryLog.objects.get()
        assert log.metadata == {"page": "homepage", "variant": "A"}

    @pytest.mark.django_db
    def test_metadata_defaults_to_empty_dict(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        index = create_index("articles")
        search(index, "hello")

        log = SearchQueryLog.objects.get()
        assert log.metadata == {}

    @pytest.mark.django_db
    def test_unsaved_user_stored_as_null(self, settings):
        """Objects without a pk (e.g. AnonymousUser) must not be stored."""
        from django.contrib.auth.models import AnonymousUser

        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        index = create_index("articles")
        search(index, "hello", user=AnonymousUser())

        log = SearchQueryLog.objects.get()
        assert log.user_id is None


# ---------------------------------------------------------------------------
# Logging does not break search on failure
# ---------------------------------------------------------------------------


class TestLoggingDoesNotBreakSearch:
    """A logging failure must never raise from search()."""

    @pytest.mark.django_db
    def test_search_result_returned_even_if_logging_fails(self, settings, monkeypatch):
        from icv_search.services import SearchResult

        settings.ICV_SEARCH_LOG_QUERIES = True
        index = create_index("articles")

        # Make the log creation explode
        from icv_search.models import analytics as analytics_module

        monkeypatch.setattr(
            analytics_module.SearchQueryLog.objects,
            "create",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("DB offline")),
        )

        result = search(index, "hello")
        assert isinstance(result, SearchResult)


# ---------------------------------------------------------------------------
# get_popular_queries
# ---------------------------------------------------------------------------


class TestGetPopularQueries:
    """get_popular_queries() returns the most frequent queries."""

    @pytest.mark.django_db
    def test_returns_list_of_dicts(self):
        SearchQueryLogFactory.create_batch(3, index_name="products", query="shoes")
        result = get_popular_queries("products")
        assert isinstance(result, list)
        assert all(isinstance(r, dict) for r in result)

    @pytest.mark.django_db
    def test_most_frequent_query_first(self):
        SearchQueryLogFactory.create_batch(5, index_name="products", query="shoes")
        SearchQueryLogFactory.create_batch(2, index_name="products", query="boots")
        result = get_popular_queries("products")
        assert result[0]["query"] == "shoes"
        assert result[0]["count"] == 5

    @pytest.mark.django_db
    def test_respects_limit(self):
        for i in range(10):
            SearchQueryLogFactory(index_name="products", query=f"query_{i}")
        result = get_popular_queries("products", limit=3)
        assert len(result) <= 3

    @pytest.mark.django_db
    def test_filters_by_index_name(self):
        SearchQueryLogFactory.create_batch(3, index_name="products", query="shoes")
        SearchQueryLogFactory.create_batch(2, index_name="articles", query="shoes")
        result = get_popular_queries("products")
        assert all(r["count"] for r in result)
        # Should only include results for "products" index
        # (can't inspect index_name directly since it's grouped by query)
        # Verify total count matches products-only records
        total = sum(r["count"] for r in result)
        assert total == 3

    @pytest.mark.django_db
    def test_filters_by_tenant_id(self):
        SearchQueryLogFactory.create_batch(4, index_name="products", query="shoes", tenant_id="acme")
        SearchQueryLogFactory.create_batch(2, index_name="products", query="shoes", tenant_id="beta")
        result = get_popular_queries("products", tenant_id="acme")
        assert result[0]["count"] == 4

    @pytest.mark.django_db
    def test_returns_empty_list_when_no_logs(self):
        result = get_popular_queries("products")
        assert result == []

    @pytest.mark.django_db
    def test_respects_days_window(self):
        from datetime import timedelta

        from django.utils import timezone

        # Create a log outside the window
        log = SearchQueryLogFactory(index_name="products", query="shoes")
        # Bypass auto_now_add by using queryset update
        from icv_search.models.analytics import SearchQueryLog

        SearchQueryLog.objects.filter(pk=log.pk).update(created_at=timezone.now() - timedelta(days=30))

        result = get_popular_queries("products", days=7)
        assert result == []


# ---------------------------------------------------------------------------
# get_zero_result_queries
# ---------------------------------------------------------------------------


class TestGetZeroResultQueries:
    """get_zero_result_queries() returns only zero-result queries."""

    @pytest.mark.django_db
    def test_excludes_queries_with_hits(self):
        SearchQueryLogFactory(index_name="products", query="shoes", is_zero_result=False, hit_count=5)
        result = get_zero_result_queries("products")
        assert result == []

    @pytest.mark.django_db
    def test_includes_zero_result_queries(self):
        SearchQueryLogFactory.create_batch(3, index_name="products", query="xyzzy", is_zero_result=True)
        result = get_zero_result_queries("products")
        assert len(result) == 1
        assert result[0]["query"] == "xyzzy"
        assert result[0]["count"] == 3

    @pytest.mark.django_db
    def test_ordered_by_frequency(self):
        SearchQueryLogFactory.create_batch(5, index_name="products", query="abc", is_zero_result=True)
        SearchQueryLogFactory.create_batch(2, index_name="products", query="xyz", is_zero_result=True)
        result = get_zero_result_queries("products")
        assert result[0]["query"] == "abc"

    @pytest.mark.django_db
    def test_filters_by_tenant(self):
        SearchQueryLogFactory.create_batch(3, index_name="products", query="abc", is_zero_result=True, tenant_id="acme")
        SearchQueryLogFactory.create_batch(2, index_name="products", query="abc", is_zero_result=True, tenant_id="beta")
        result = get_zero_result_queries("products", tenant_id="acme")
        assert result[0]["count"] == 3


# ---------------------------------------------------------------------------
# get_search_stats
# ---------------------------------------------------------------------------


class TestGetSearchStats:
    """get_search_stats() returns aggregate statistics."""

    @pytest.mark.django_db
    def test_returns_dict_with_expected_keys(self):
        stats = get_search_stats("products")
        assert set(stats.keys()) == {"total_queries", "zero_result_count", "zero_result_rate", "avg_processing_time_ms"}

    @pytest.mark.django_db
    def test_total_queries_count(self):
        SearchQueryLogFactory.create_batch(5, index_name="products")
        stats = get_search_stats("products")
        assert stats["total_queries"] == 5

    @pytest.mark.django_db
    def test_zero_result_count(self):
        SearchQueryLogFactory.create_batch(3, index_name="products", is_zero_result=True)
        SearchQueryLogFactory.create_batch(2, index_name="products", is_zero_result=False)
        stats = get_search_stats("products")
        assert stats["zero_result_count"] == 3

    @pytest.mark.django_db
    def test_zero_result_rate_calculation(self):
        SearchQueryLogFactory.create_batch(1, index_name="products", is_zero_result=True)
        SearchQueryLogFactory.create_batch(3, index_name="products", is_zero_result=False)
        stats = get_search_stats("products")
        assert stats["zero_result_rate"] == pytest.approx(0.25)

    @pytest.mark.django_db
    def test_zero_result_rate_is_zero_when_no_logs(self):
        stats = get_search_stats("products")
        assert stats["zero_result_rate"] == 0.0

    @pytest.mark.django_db
    def test_avg_processing_time_ms(self):
        SearchQueryLogFactory(index_name="products", processing_time_ms=10)
        SearchQueryLogFactory(index_name="products", processing_time_ms=20)
        stats = get_search_stats("products")
        assert stats["avg_processing_time_ms"] == pytest.approx(15.0)

    @pytest.mark.django_db
    def test_avg_processing_time_ms_none_when_no_logs(self):
        stats = get_search_stats("products")
        assert stats["avg_processing_time_ms"] is None

    @pytest.mark.django_db
    def test_filters_by_tenant(self):
        SearchQueryLogFactory.create_batch(3, index_name="products", tenant_id="acme")
        SearchQueryLogFactory.create_batch(5, index_name="products", tenant_id="beta")
        stats = get_search_stats("products", tenant_id="acme")
        assert stats["total_queries"] == 3

    @pytest.mark.django_db
    def test_filters_by_index_name(self):
        SearchQueryLogFactory.create_batch(4, index_name="products")
        SearchQueryLogFactory.create_batch(2, index_name="articles")
        stats = get_search_stats("products")
        assert stats["total_queries"] == 4


# ---------------------------------------------------------------------------
# clear_query_logs
# ---------------------------------------------------------------------------


class TestClearQueryLogs:
    """clear_query_logs() deletes logs older than the given threshold."""

    @pytest.mark.django_db
    def test_returns_count_of_deleted_records(self):
        from datetime import timedelta

        from django.utils import timezone

        from icv_search.models.analytics import SearchQueryLog

        logs = SearchQueryLogFactory.create_batch(3, index_name="products")
        SearchQueryLog.objects.filter(pk__in=[log.pk for log in logs]).update(
            created_at=timezone.now() - timedelta(days=60)
        )

        deleted = clear_query_logs(days_older_than=30)
        assert deleted == 3

    @pytest.mark.django_db
    def test_does_not_delete_recent_logs(self):
        from icv_search.models.analytics import SearchQueryLog

        SearchQueryLogFactory.create_batch(3, index_name="products")

        clear_query_logs(days_older_than=30)
        assert SearchQueryLog.objects.count() == 3

    @pytest.mark.django_db
    def test_returns_zero_when_nothing_deleted(self):
        SearchQueryLogFactory.create_batch(2, index_name="products")
        deleted = clear_query_logs(days_older_than=30)
        assert deleted == 0

    @pytest.mark.django_db
    def test_deletes_only_old_records(self):
        from datetime import timedelta

        from django.utils import timezone

        from icv_search.models.analytics import SearchQueryLog

        old_logs = SearchQueryLogFactory.create_batch(2, index_name="products")
        SearchQueryLog.objects.filter(pk__in=[log.pk for log in old_logs]).update(
            created_at=timezone.now() - timedelta(days=60)
        )
        # Recent logs
        SearchQueryLogFactory.create_batch(3, index_name="products")

        deleted = clear_query_logs(days_older_than=30)
        assert deleted == 2
        assert SearchQueryLog.objects.count() == 3


# ---------------------------------------------------------------------------
# Log mode: aggregate
# ---------------------------------------------------------------------------


class TestLogModeAggregate:
    """When ICV_SEARCH_LOG_MODE is 'aggregate', only aggregate rows are created."""

    @pytest.mark.django_db
    def test_aggregate_mode_creates_aggregate_row(self, settings):
        from icv_search.models.aggregates import SearchQueryAggregate

        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_MODE = "aggregate"
        index = create_index("articles")
        search(index, "hello")

        assert SearchQueryAggregate.objects.count() == 1
        agg = SearchQueryAggregate.objects.get()
        assert agg.index_name == "articles"
        assert agg.query == "hello"
        assert agg.total_count == 1

    @pytest.mark.django_db
    def test_aggregate_mode_does_not_create_individual_log(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_MODE = "aggregate"
        index = create_index("articles")
        search(index, "hello")

        assert SearchQueryLog.objects.count() == 0

    @pytest.mark.django_db
    def test_aggregate_increments_existing_row(self, settings):
        from icv_search.models.aggregates import SearchQueryAggregate

        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_MODE = "aggregate"
        index = create_index("articles")
        search(index, "hello")
        search(index, "hello")
        search(index, "hello")

        assert SearchQueryAggregate.objects.count() == 1
        agg = SearchQueryAggregate.objects.get()
        assert agg.total_count == 3

    @pytest.mark.django_db
    def test_aggregate_normalises_query(self, settings):
        from icv_search.models.aggregates import SearchQueryAggregate

        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_MODE = "aggregate"
        index = create_index("articles")
        search(index, "Django Tips")
        search(index, "  django tips  ")
        search(index, "DJANGO TIPS")

        assert SearchQueryAggregate.objects.count() == 1
        agg = SearchQueryAggregate.objects.get()
        assert agg.query == "django tips"
        assert agg.total_count == 3

    @pytest.mark.django_db
    def test_aggregate_tracks_zero_results(self, settings):
        from icv_search.models.aggregates import SearchQueryAggregate

        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_MODE = "aggregate"
        index = create_index("articles")
        # No documents — all searches return zero results
        search(index, "xyzzy")
        search(index, "xyzzy")

        agg = SearchQueryAggregate.objects.get()
        assert agg.zero_result_count == 2

    @pytest.mark.django_db
    def test_aggregate_accumulates_processing_time(self, settings):
        from icv_search.models.aggregates import SearchQueryAggregate

        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_MODE = "aggregate"
        index = create_index("articles")
        search(index, "hello")
        search(index, "hello")

        agg = SearchQueryAggregate.objects.get()
        assert agg.total_processing_time_ms >= 0
        assert agg.total_count == 2

    @pytest.mark.django_db
    def test_aggregate_separates_by_tenant(self, settings):
        from icv_search.models.aggregates import SearchQueryAggregate

        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_MODE = "aggregate"
        idx_acme = create_index("articles", tenant_id="acme")
        idx_beta = create_index("articles", tenant_id="beta")
        search(idx_acme, "hello")
        search(idx_acme, "hello")
        search(idx_beta, "hello")

        assert SearchQueryAggregate.objects.count() == 2
        acme_agg = SearchQueryAggregate.objects.get(tenant_id="acme")
        assert acme_agg.total_count == 2
        beta_agg = SearchQueryAggregate.objects.get(tenant_id="beta")
        assert beta_agg.total_count == 1

    @pytest.mark.django_db
    def test_aggregate_different_queries_separate_rows(self, settings):
        from icv_search.models.aggregates import SearchQueryAggregate

        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_MODE = "aggregate"
        index = create_index("articles")
        search(index, "django")
        search(index, "flask")

        assert SearchQueryAggregate.objects.count() == 2


# ---------------------------------------------------------------------------
# Log mode: both
# ---------------------------------------------------------------------------


class TestLogModeBoth:
    """When ICV_SEARCH_LOG_MODE is 'both', both individual and aggregate rows are created."""

    @pytest.mark.django_db
    def test_both_mode_creates_individual_and_aggregate(self, settings):
        from icv_search.models.aggregates import SearchQueryAggregate
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_MODE = "both"
        index = create_index("articles")
        search(index, "hello")

        assert SearchQueryLog.objects.count() == 1
        assert SearchQueryAggregate.objects.count() == 1

    @pytest.mark.django_db
    def test_both_mode_individual_preserves_original_case(self, settings):
        from icv_search.models.aggregates import SearchQueryAggregate
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_MODE = "both"
        index = create_index("articles")
        search(index, "Django Tips")

        log = SearchQueryLog.objects.get()
        assert log.query == "Django Tips"  # individual preserves case

        agg = SearchQueryAggregate.objects.get()
        assert agg.query == "django tips"  # aggregate normalises


# ---------------------------------------------------------------------------
# Sample rate
# ---------------------------------------------------------------------------


class TestSampleRate:
    """ICV_SEARCH_LOG_SAMPLE_RATE controls individual log sampling."""

    @pytest.mark.django_db
    def test_sample_rate_zero_skips_individual_logging(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_MODE = "individual"
        settings.ICV_SEARCH_LOG_SAMPLE_RATE = 0.0
        index = create_index("articles")
        search(index, "hello")

        assert SearchQueryLog.objects.count() == 0

    @pytest.mark.django_db
    def test_sample_rate_one_logs_all(self, settings):
        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_MODE = "individual"
        settings.ICV_SEARCH_LOG_SAMPLE_RATE = 1.0
        index = create_index("articles")
        search(index, "hello")

        assert SearchQueryLog.objects.count() == 1

    @pytest.mark.django_db
    def test_sample_rate_does_not_affect_aggregate(self, settings):
        """Aggregate mode always counts 100% regardless of sample rate."""
        from icv_search.models.aggregates import SearchQueryAggregate

        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_MODE = "both"
        settings.ICV_SEARCH_LOG_SAMPLE_RATE = 0.0
        index = create_index("articles")
        search(index, "hello")

        # Individual skipped due to 0.0 rate, but aggregate must still record
        assert SearchQueryAggregate.objects.count() == 1
        assert SearchQueryAggregate.objects.get().total_count == 1

    @pytest.mark.django_db
    def test_sample_rate_mid_value(self, settings, monkeypatch):
        """With 50% rate and controlled random, verify sampling works."""
        import random as random_mod

        from icv_search.models.analytics import SearchQueryLog

        settings.ICV_SEARCH_LOG_QUERIES = True
        settings.ICV_SEARCH_LOG_MODE = "individual"
        settings.ICV_SEARCH_LOG_SAMPLE_RATE = 0.5

        # random.random() returns 0.3 < 0.5 — should log
        monkeypatch.setattr(random_mod, "random", lambda: 0.3)
        index = create_index("articles")
        search(index, "logged")
        assert SearchQueryLog.objects.count() == 1

        # random.random() returns 0.8 > 0.5 — should skip
        monkeypatch.setattr(random_mod, "random", lambda: 0.8)
        search(index, "skipped")
        assert SearchQueryLog.objects.count() == 1  # still 1


# ---------------------------------------------------------------------------
# Analytics functions with aggregate mode
# ---------------------------------------------------------------------------


class TestGetPopularQueriesAggregate:
    """get_popular_queries() uses SearchQueryAggregate in aggregate mode."""

    @pytest.mark.django_db
    def test_returns_from_aggregate(self, settings):
        settings.ICV_SEARCH_LOG_MODE = "aggregate"
        SearchQueryAggregateFactory(index_name="products", query="shoes", total_count=20)
        SearchQueryAggregateFactory(index_name="products", query="boots", total_count=10)

        result = get_popular_queries("products")
        assert len(result) == 2
        assert result[0]["query"] == "shoes"
        assert result[0]["count"] == 20

    @pytest.mark.django_db
    def test_respects_days_window(self, settings):
        import datetime

        settings.ICV_SEARCH_LOG_MODE = "aggregate"
        SearchQueryAggregateFactory(
            index_name="products",
            query="old",
            date=datetime.date.today() - datetime.timedelta(days=30),
        )
        SearchQueryAggregateFactory(
            index_name="products",
            query="recent",
            date=datetime.date.today(),
        )

        result = get_popular_queries("products", days=7)
        assert len(result) == 1
        assert result[0]["query"] == "recent"

    @pytest.mark.django_db
    def test_filters_by_tenant(self, settings):
        settings.ICV_SEARCH_LOG_MODE = "aggregate"
        SearchQueryAggregateFactory(index_name="products", query="shoes", total_count=10, tenant_id="acme")
        SearchQueryAggregateFactory(index_name="products", query="shoes", total_count=5, tenant_id="beta")

        result = get_popular_queries("products", tenant_id="acme")
        assert result[0]["count"] == 10


class TestGetZeroResultQueriesAggregate:
    """get_zero_result_queries() uses SearchQueryAggregate in aggregate mode."""

    @pytest.mark.django_db
    def test_returns_from_aggregate(self, settings):
        settings.ICV_SEARCH_LOG_MODE = "aggregate"
        SearchQueryAggregateFactory(index_name="products", query="xyzzy", zero_result_count=5, total_count=5)
        SearchQueryAggregateFactory(index_name="products", query="shoes", zero_result_count=0, total_count=20)

        result = get_zero_result_queries("products")
        assert len(result) == 1
        assert result[0]["query"] == "xyzzy"
        assert result[0]["count"] == 5


class TestGetSearchStatsAggregate:
    """get_search_stats() uses SearchQueryAggregate in aggregate mode."""

    @pytest.mark.django_db
    def test_returns_stats_from_aggregate(self, settings):
        settings.ICV_SEARCH_LOG_MODE = "aggregate"
        SearchQueryAggregateFactory(
            index_name="products",
            total_count=100,
            zero_result_count=10,
            total_processing_time_ms=5000,
        )

        stats = get_search_stats("products")
        assert stats["total_queries"] == 100
        assert stats["zero_result_count"] == 10
        assert stats["zero_result_rate"] == pytest.approx(0.1)
        assert stats["avg_processing_time_ms"] == pytest.approx(50.0)

    @pytest.mark.django_db
    def test_empty_aggregate_stats(self, settings):
        settings.ICV_SEARCH_LOG_MODE = "aggregate"
        stats = get_search_stats("products")
        assert stats["total_queries"] == 0
        assert stats["zero_result_rate"] == 0.0
        assert stats["avg_processing_time_ms"] is None


# ---------------------------------------------------------------------------
# get_query_trend
# ---------------------------------------------------------------------------


class TestGetQueryTrend:
    """get_query_trend() returns daily counts from SearchQueryAggregate."""

    @pytest.mark.django_db
    def test_returns_daily_counts(self):
        import datetime

        today = datetime.date.today()
        yesterday = today - datetime.timedelta(days=1)
        SearchQueryAggregateFactory(
            index_name="products",
            query="shoes",
            date=yesterday,
            total_count=5,
            zero_result_count=1,
            total_processing_time_ms=500,
        )
        SearchQueryAggregateFactory(
            index_name="products",
            query="shoes",
            date=today,
            total_count=10,
            zero_result_count=0,
            total_processing_time_ms=800,
        )

        trend = get_query_trend("shoes", "products")
        assert len(trend) == 2
        assert trend[0]["date"] == yesterday
        assert trend[0]["count"] == 5
        assert trend[0]["zero_result_count"] == 1
        assert trend[0]["avg_processing_time_ms"] == pytest.approx(100.0)
        assert trend[1]["date"] == today
        assert trend[1]["count"] == 10

    @pytest.mark.django_db
    def test_returns_empty_list_when_no_data(self):
        trend = get_query_trend("nonexistent", "products")
        assert trend == []

    @pytest.mark.django_db
    def test_normalises_query_for_lookup(self):
        import datetime

        SearchQueryAggregateFactory(
            index_name="products",
            query="django tips",
            date=datetime.date.today(),
            total_count=3,
        )

        # Mixed case and whitespace should still match
        trend = get_query_trend("  Django Tips  ", "products")
        assert len(trend) == 1
        assert trend[0]["count"] == 3

    @pytest.mark.django_db
    def test_filters_by_tenant(self):
        import datetime

        today = datetime.date.today()
        SearchQueryAggregateFactory(
            index_name="products",
            query="shoes",
            date=today,
            total_count=5,
            tenant_id="acme",
        )
        SearchQueryAggregateFactory(
            index_name="products",
            query="shoes",
            date=today,
            total_count=3,
            tenant_id="beta",
        )

        trend = get_query_trend("shoes", "products", tenant_id="acme")
        assert len(trend) == 1
        assert trend[0]["count"] == 5

    @pytest.mark.django_db
    def test_respects_days_window(self):
        import datetime

        old_date = datetime.date.today() - datetime.timedelta(days=60)
        SearchQueryAggregateFactory(
            index_name="products",
            query="shoes",
            date=old_date,
            total_count=10,
        )

        trend = get_query_trend("shoes", "products", days=30)
        assert trend == []


# ---------------------------------------------------------------------------
# clear_query_aggregates
# ---------------------------------------------------------------------------


class TestClearQueryAggregates:
    """clear_query_aggregates() deletes old aggregate rows."""

    @pytest.mark.django_db
    def test_deletes_old_aggregates(self):
        import datetime

        from icv_search.models.aggregates import SearchQueryAggregate

        old_date = datetime.date.today() - datetime.timedelta(days=120)
        SearchQueryAggregateFactory(index_name="products", query="shoes", date=old_date)

        deleted = clear_query_aggregates(days_older_than=90)
        assert deleted == 1
        assert SearchQueryAggregate.objects.count() == 0

    @pytest.mark.django_db
    def test_does_not_delete_recent_aggregates(self):
        import datetime

        from icv_search.models.aggregates import SearchQueryAggregate

        SearchQueryAggregateFactory(
            index_name="products",
            query="shoes",
            date=datetime.date.today(),
        )

        deleted = clear_query_aggregates(days_older_than=90)
        assert deleted == 0
        assert SearchQueryAggregate.objects.count() == 1

    @pytest.mark.django_db
    def test_returns_zero_when_nothing_deleted(self):
        deleted = clear_query_aggregates(days_older_than=90)
        assert deleted == 0


# ---------------------------------------------------------------------------
# SearchQueryAggregate model
# ---------------------------------------------------------------------------


class TestSearchQueryAggregateModel:
    """SearchQueryAggregate model properties and behaviour."""

    @pytest.mark.django_db
    def test_avg_processing_time_ms_property(self):
        agg = SearchQueryAggregateFactory(total_count=10, total_processing_time_ms=500)
        assert agg.avg_processing_time_ms == pytest.approx(50.0)

    @pytest.mark.django_db
    def test_avg_processing_time_ms_zero_count(self):
        agg = SearchQueryAggregateFactory(total_count=0, total_processing_time_ms=0)
        assert agg.avg_processing_time_ms == 0.0

    @pytest.mark.django_db
    def test_str_representation(self):
        import datetime

        agg = SearchQueryAggregateFactory(
            query="shoes",
            index_name="products",
            date=datetime.date(2026, 3, 22),
            total_count=42,
        )
        s = str(agg)
        assert "shoes" in s
        assert "products" in s
        assert "2026-03-22" in s


class TestSearchQueryLogUserFkFollowsIcvAuthUserModel:
    """SearchQueryLog.user targets the resolved ICV_AUTH_USER_MODEL (ADR-037),
    not settings.AUTH_USER_MODEL directly."""

    def test_deconstructs_to_settings_auth_user_model_when_unset(self, settings):
        """With ICV_AUTH_USER_MODEL unset, the FK must still deconstruct to
        settings.AUTH_USER_MODEL so shipped migrations stay byte-stable
        (ADR-037, 'Migration-state consequence')."""
        from icv_search.models.analytics import SearchQueryLog

        assert not hasattr(settings, "ICV_AUTH_USER_MODEL")
        field = SearchQueryLog._meta.get_field("user")
        _, _, _, kwargs = field.deconstruct()
        assert kwargs["to"] == settings.AUTH_USER_MODEL

    def test_conf_resolves_to_the_override_when_set(self, settings):
        """icv_search.conf.ICV_AUTH_USER_MODEL (what the field is built from
        at model-import time) honours an explicit override."""
        import importlib

        from icv_search import conf

        settings.ICV_AUTH_USER_MODEL = "auth.User"
        importlib.reload(conf)
        try:
            assert conf.ICV_AUTH_USER_MODEL == "auth.User"
        finally:
            importlib.reload(conf)
