"""Tests for the search intelligence service module.

Most tests run against whichever database the test settings configure.
Tests that exercise ``cluster_queries`` and ``suggest_synonyms`` (which
require PostgreSQL's ``pg_trgm`` extension) are collected under a separate
class and are skipped when PostgreSQL is not available. The ``pg_trgm``
extension itself is created for the test database by the ``django_db_setup``
fixture override in ``conftest.py``.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest
from django.db import connection

from icv_search.services.intelligence import (
    auto_create_rewrites,
    cluster_queries,
    get_demand_signals,
    suggest_synonyms,
)
from icv_search.testing.factories import SearchQueryAggregateFactory

# ---------------------------------------------------------------------------
# PostgreSQL + pg_trgm availability check
# ---------------------------------------------------------------------------
#
# The vendor check below is a plain attribute comparison (matches the
# convention in test_postgres_backend.py) and is safe to evaluate at import
# time: it does not open a cursor or touch a database connection.
#
# The pg_trgm-extension check is different: it has to run a query, so it
# cannot run at import/collection time. Collection happens before
# pytest-django creates the test database (typically ``test_<DB_NAME>``), so
# a query at import time would run against the wrong database. Instead it
# runs from the ``_pg_trgm_installed`` fixture below, applied to the two
# classes that need it via ``usefixtures``. That fixture depends on ``db``,
# which guarantees pytest-django has already created and connected to the
# real test database. The extension itself is created by the
# ``django_db_setup`` override in conftest.py, so by the time this fixture
# runs pg_trgm should always be present when PostgreSQL is in use; if it is
# genuinely missing (or the query fails for any other reason) that error is
# allowed to propagate rather than being swallowed into a silent skip.

_requires_postgres = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="pg_trgm tests require a PostgreSQL database",
)


@pytest.fixture()
def _pg_trgm_installed(db):
    """Confirm ``pg_trgm`` is installed before a pg_trgm-only test runs.

    Used explicitly (not module-wide autouse) by the two classes below that
    exercise ``cluster_queries``/``suggest_synonyms``, since those are the
    only tests in this module that actually require the extension.
    """
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
        if cursor.fetchone() is None:
            pytest.fail(
                "PostgreSQL is available but the pg_trgm extension is not installed. "
                "It should have been created by the django_db_setup fixture in "
                "conftest.py; check that the test database user has permission to "
                "run CREATE EXTENSION."
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today() -> datetime.date:
    return datetime.date.today()


def _days_ago(n: int) -> datetime.date:
    return _today() - datetime.timedelta(days=n)


# ---------------------------------------------------------------------------
# get_demand_signals
# ---------------------------------------------------------------------------


class TestGetDemandSignals:
    """Unit tests for get_demand_signals()."""

    @pytest.mark.django_db
    def test_returns_empty_when_no_data(self):
        result = get_demand_signals("products")
        assert result == []

    @pytest.mark.django_db
    def test_basic_signal_returned(self):
        SearchQueryAggregateFactory(
            index_name="products",
            query="waterproof jacket",
            date=_days_ago(5),
            total_count=100,
            zero_result_count=90,
        )
        result = get_demand_signals("products", min_volume=1)
        assert len(result) == 1
        row = result[0]
        assert row["query"] == "waterproof jacket"
        assert row["volume"] == 100
        assert abs(row["zero_result_rate"] - 0.9) < 0.001
        assert abs(row["gap_score"] - 90.0) < 0.001

    @pytest.mark.django_db
    def test_min_volume_filter_from_setting(self, settings):
        """Queries below ICV_SEARCH_INTELLIGENCE_MIN_VOLUME are excluded."""
        settings.ICV_SEARCH_INTELLIGENCE_MIN_VOLUME = 10
        SearchQueryAggregateFactory(
            index_name="products",
            query="rare query",
            date=_days_ago(1),
            total_count=5,
            zero_result_count=5,
        )
        result = get_demand_signals("products")
        assert result == []

    @pytest.mark.django_db
    def test_min_volume_explicit_overrides_setting(self, settings):
        settings.ICV_SEARCH_INTELLIGENCE_MIN_VOLUME = 100
        SearchQueryAggregateFactory(
            index_name="products",
            query="some query",
            date=_days_ago(1),
            total_count=10,
            zero_result_count=10,
        )
        result = get_demand_signals("products", min_volume=5)
        assert len(result) == 1

    @pytest.mark.django_db
    def test_min_volume_enforced_to_at_least_1(self):
        """min_volume=0 or negative is normalised to 1."""
        SearchQueryAggregateFactory(
            index_name="products",
            query="q",
            date=_days_ago(1),
            total_count=1,
            zero_result_count=1,
        )
        result = get_demand_signals("products", min_volume=0)
        assert len(result) == 1
        result = get_demand_signals("products", min_volume=-5)
        assert len(result) == 1

    @pytest.mark.django_db
    def test_sorted_by_gap_score_descending(self):
        SearchQueryAggregateFactory(
            index_name="products",
            query="low gap",
            date=_days_ago(1),
            total_count=10,
            zero_result_count=1,
        )
        SearchQueryAggregateFactory(
            index_name="products",
            query="high gap",
            date=_days_ago(1),
            total_count=100,
            zero_result_count=90,
        )
        result = get_demand_signals("products", min_volume=1)
        assert result[0]["query"] == "high gap"
        assert result[1]["query"] == "low gap"

    @pytest.mark.django_db
    def test_min_gap_score_filter(self):
        SearchQueryAggregateFactory(
            index_name="products",
            query="low score",
            date=_days_ago(1),
            total_count=10,
            zero_result_count=1,
        )
        result = get_demand_signals("products", min_volume=1, min_gap_score=5.0)
        assert result == []

    @pytest.mark.django_db
    def test_exclude_patterns_applied(self):
        SearchQueryAggregateFactory(
            index_name="products",
            query="test navigation query",
            date=_days_ago(1),
            total_count=20,
            zero_result_count=18,
        )
        SearchQueryAggregateFactory(
            index_name="products",
            query="waterproof jacket",
            date=_days_ago(1),
            total_count=100,
            zero_result_count=90,
        )
        result = get_demand_signals(
            "products",
            min_volume=1,
            exclude_patterns=[r"^test\s"],
        )
        queries = [r["query"] for r in result]
        assert "test navigation query" not in queries
        assert "waterproof jacket" in queries

    @pytest.mark.django_db
    def test_tenant_id_filter(self):
        SearchQueryAggregateFactory(
            index_name="products",
            query="tenant a query",
            date=_days_ago(1),
            total_count=50,
            zero_result_count=50,
            tenant_id="tenant-a",
        )
        SearchQueryAggregateFactory(
            index_name="products",
            query="global query",
            date=_days_ago(1),
            total_count=50,
            zero_result_count=50,
            tenant_id="",
        )
        result = get_demand_signals("products", min_volume=1, tenant_id="tenant-a")
        queries = [r["query"] for r in result]
        assert "tenant a query" in queries
        assert "global query" not in queries

    @pytest.mark.django_db
    def test_ctr_included_when_no_click_data(self):
        SearchQueryAggregateFactory(
            index_name="products",
            query="some query",
            date=_days_ago(1),
            total_count=50,
            zero_result_count=40,
        )
        result = get_demand_signals("products", min_volume=1)
        assert result[0]["ctr"] == 0.0

    @pytest.mark.django_db
    def test_excludes_data_outside_window(self):
        SearchQueryAggregateFactory(
            index_name="products",
            query="old query",
            date=_days_ago(60),
            total_count=200,
            zero_result_count=180,
        )
        result = get_demand_signals("products", days=30, min_volume=1)
        assert result == []

    @pytest.mark.django_db
    def test_zero_result_rate_of_zero_when_no_zeros(self):
        SearchQueryAggregateFactory(
            index_name="products",
            query="good query",
            date=_days_ago(1),
            total_count=100,
            zero_result_count=0,
        )
        result = get_demand_signals("products", min_volume=1)
        assert len(result) == 1
        assert result[0]["zero_result_rate"] == 0.0
        assert result[0]["gap_score"] == 0.0

    @pytest.mark.django_db
    def test_trend_is_zero_when_no_previous_week_data(self):
        SearchQueryAggregateFactory(
            index_name="products",
            query="trending query",
            date=_days_ago(3),
            total_count=50,
            zero_result_count=45,
        )
        result = get_demand_signals("products", min_volume=1)
        assert result[0]["trend"] == 0.0

    @pytest.mark.django_db
    def test_trend_positive_when_volume_growing(self):
        # Previous week: 10, recent week: 20 → 100% growth.
        for i in range(7):
            SearchQueryAggregateFactory(
                index_name="products",
                query="growing query",
                date=_today() - datetime.timedelta(days=8 + i),
                total_count=1,
                zero_result_count=1,
            )
        for i in range(7):
            SearchQueryAggregateFactory(
                index_name="products",
                query="growing query",
                date=_today() - datetime.timedelta(days=1 + i),
                total_count=2,
                zero_result_count=2,
            )
        result = get_demand_signals("products", min_volume=1)
        assert result[0]["trend"] == pytest.approx(100.0, abs=1.0)

    @pytest.mark.django_db
    def test_invalid_exclude_pattern_is_skipped_with_warning(self, caplog):
        import logging

        SearchQueryAggregateFactory(
            index_name="products",
            query="some query",
            date=_days_ago(1),
            total_count=50,
            zero_result_count=40,
        )
        with caplog.at_level(logging.WARNING):
            result = get_demand_signals(
                "products",
                min_volume=1,
                exclude_patterns=[r"[invalid regex"],
            )
        # Invalid pattern is skipped — query still appears.
        assert len(result) == 1
        assert "Invalid exclude_pattern" in caplog.text


# ---------------------------------------------------------------------------
# cluster_queries — pg_trgm required
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_pg_trgm_installed")
class TestClusterQueriesPgTrgm:
    """Tests for cluster_queries(). Skipped when PostgreSQL is unavailable."""

    pytestmark = _requires_postgres

    @pytest.mark.django_db
    def test_raises_when_pg_trgm_absent(self):
        """ImproperlyConfigured is raised when pg_trgm is not installed."""
        from django.core.exceptions import ImproperlyConfigured

        with (
            patch(
                "icv_search.services.intelligence._check_pg_trgm",
                side_effect=ImproperlyConfigured("pg_trgm missing"),
            ),
            pytest.raises(ImproperlyConfigured, match="pg_trgm"),
        ):
            cluster_queries("products")

    @pytest.mark.django_db
    def test_returns_empty_when_no_data(self):
        result = cluster_queries("products")
        assert result == []

    @pytest.mark.django_db
    def test_single_query_forms_one_cluster(self):
        SearchQueryAggregateFactory(
            index_name="products",
            query="running shoes",
            date=_days_ago(1),
            total_count=100,
            zero_result_count=5,
        )
        result = cluster_queries("products")
        assert len(result) == 1
        assert result[0]["representative_query"] == "running shoes"
        assert result[0]["member_queries"] == []
        assert result[0]["total_volume"] == 100

    @pytest.mark.django_db
    def test_similar_queries_clustered(self):
        SearchQueryAggregateFactory(
            index_name="products",
            query="running shoes",
            date=_days_ago(1),
            total_count=200,
            zero_result_count=5,
        )
        SearchQueryAggregateFactory(
            index_name="products",
            query="running shoe",
            date=_days_ago(1),
            total_count=50,
            zero_result_count=2,
        )
        result = cluster_queries("products", similarity_threshold=0.4)
        # Both are similar — should collapse into one cluster.
        assert len(result) >= 1
        largest = result[0]
        assert largest["representative_query"] == "running shoes"

    @pytest.mark.django_db
    def test_dissimilar_queries_remain_separate(self):
        SearchQueryAggregateFactory(
            index_name="products",
            query="running shoes",
            date=_days_ago(1),
            total_count=100,
            zero_result_count=5,
        )
        SearchQueryAggregateFactory(
            index_name="products",
            query="waterproof jacket",
            date=_days_ago(1),
            total_count=80,
            zero_result_count=70,
        )
        result = cluster_queries("products", similarity_threshold=0.8)
        assert len(result) == 2

    @pytest.mark.django_db
    def test_sorted_by_total_volume_descending(self):
        SearchQueryAggregateFactory(
            index_name="products",
            query="small cluster query",
            date=_days_ago(1),
            total_count=10,
            zero_result_count=0,
        )
        SearchQueryAggregateFactory(
            index_name="products",
            query="large cluster query",
            date=_days_ago(1),
            total_count=500,
            zero_result_count=10,
        )
        result = cluster_queries("products", similarity_threshold=0.9)
        assert result[0]["total_volume"] >= result[-1]["total_volume"]


# ---------------------------------------------------------------------------
# suggest_synonyms — pg_trgm required
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_pg_trgm_installed")
class TestSuggestSynonymsPgTrgm:
    """Tests for suggest_synonyms(). Skipped when PostgreSQL is unavailable."""

    pytestmark = _requires_postgres

    @pytest.mark.django_db
    def test_raises_when_pg_trgm_absent(self):
        from django.core.exceptions import ImproperlyConfigured

        with (
            patch(
                "icv_search.services.intelligence._check_pg_trgm",
                side_effect=ImproperlyConfigured("pg_trgm missing"),
            ),
            pytest.raises(ImproperlyConfigured, match="pg_trgm"),
        ):
            suggest_synonyms("products")

    @pytest.mark.django_db
    def test_returns_empty_when_no_data(self):
        result = suggest_synonyms("products")
        assert result == []

    @pytest.mark.django_db
    def test_returns_empty_when_no_zero_result_queries(self):
        SearchQueryAggregateFactory(
            index_name="products",
            query="good query",
            date=_days_ago(1),
            total_count=100,
            zero_result_count=0,
        )
        result = suggest_synonyms("products")
        assert result == []

    @pytest.mark.django_db
    def test_returns_empty_when_no_successful_queries(self):
        SearchQueryAggregateFactory(
            index_name="products",
            query="bad query",
            date=_days_ago(1),
            total_count=50,
            zero_result_count=45,
        )
        result = suggest_synonyms("products")
        assert result == []

    @pytest.mark.django_db
    def test_similar_zero_result_to_successful_suggested(self):
        # "rain coat" → successful (mostly results)
        SearchQueryAggregateFactory(
            index_name="products",
            query="rain coat",
            date=_days_ago(1),
            total_count=100,
            zero_result_count=5,
        )
        # "rain coats" → zero-result (mostly fails)
        SearchQueryAggregateFactory(
            index_name="products",
            query="rain coats",
            date=_days_ago(1),
            total_count=60,
            zero_result_count=55,
        )
        result = suggest_synonyms("products", confidence_threshold=0.0)
        assert len(result) >= 1
        suggestion = result[0]
        assert suggestion["source_query"] == "rain coats"
        assert suggestion["suggested_synonym"] == "rain coat"
        assert suggestion["confidence"] > 0.0
        assert suggestion["evidence_count"] >= 1

    @pytest.mark.django_db
    def test_confidence_threshold_filters_low_confidence(self):
        # Force only borderline-similar queries.
        SearchQueryAggregateFactory(
            index_name="products",
            query="aa",
            date=_days_ago(1),
            total_count=100,
            zero_result_count=10,
        )
        SearchQueryAggregateFactory(
            index_name="products",
            query="bb",
            date=_days_ago(1),
            total_count=50,
            zero_result_count=48,
        )
        # Very different strings → very low similarity → should not pass 0.9 threshold.
        result = suggest_synonyms("products", confidence_threshold=0.9)
        assert result == []

    @pytest.mark.django_db
    def test_sorted_by_confidence_descending(self):
        SearchQueryAggregateFactory(
            index_name="products",
            query="rain coat",
            date=_days_ago(1),
            total_count=200,
            zero_result_count=5,
        )
        SearchQueryAggregateFactory(
            index_name="products",
            query="waterproof coat",
            date=_days_ago(1),
            total_count=200,
            zero_result_count=5,
        )
        SearchQueryAggregateFactory(
            index_name="products",
            query="rain coats",
            date=_days_ago(1),
            total_count=60,
            zero_result_count=55,
        )
        result = suggest_synonyms("products", confidence_threshold=0.0)
        # Verify descending order.
        confidences = [r["confidence"] for r in result]
        assert confidences == sorted(confidences, reverse=True)


# ---------------------------------------------------------------------------
# auto_create_rewrites — SQLite safe (patches suggest_synonyms + pg_trgm)
# ---------------------------------------------------------------------------


class TestAutoCreateRewrites:
    """Tests for auto_create_rewrites()."""

    @pytest.fixture(autouse=True)
    def enable_merchandising(self, settings):
        settings.ICV_SEARCH_MERCHANDISING_ENABLED = True

    @pytest.mark.django_db
    def test_raises_when_merchandising_disabled(self, settings):
        from django.core.exceptions import ImproperlyConfigured

        settings.ICV_SEARCH_MERCHANDISING_ENABLED = False
        with pytest.raises(ImproperlyConfigured, match="ICV_SEARCH_MERCHANDISING_ENABLED"):
            auto_create_rewrites("products")

    @pytest.mark.django_db
    def test_uses_setting_when_threshold_none(self, settings):
        settings.ICV_SEARCH_AUTO_SYNONYM_CONFIDENCE = 0.99

        with patch(
            "icv_search.services.intelligence.suggest_synonyms",
            return_value=[
                {
                    "source_query": "q",
                    "suggested_synonym": "s",
                    "confidence": 0.85,
                    "evidence_count": 1,
                }
            ],
        ):
            result = auto_create_rewrites("products")

        assert result[0]["action"] == "skipped"

    @pytest.mark.django_db
    def test_creates_rewrite_above_threshold(self, settings):
        settings.ICV_SEARCH_AUTO_SYNONYM_CONFIDENCE = 0.8

        with patch(
            "icv_search.services.intelligence.suggest_synonyms",
            return_value=[
                {
                    "source_query": "waterproof jacket",
                    "suggested_synonym": "rain coat",
                    "confidence": 0.87,
                    "evidence_count": 3,
                }
            ],
        ):
            result = auto_create_rewrites("products")

        assert result[0]["action"] == "created"
        assert result[0]["rewrite_id"] is not None

    @pytest.mark.django_db
    def test_skipped_when_below_threshold(self):
        with patch(
            "icv_search.services.intelligence.suggest_synonyms",
            return_value=[
                {
                    "source_query": "vague query",
                    "suggested_synonym": "other",
                    "confidence": 0.50,
                    "evidence_count": 1,
                }
            ],
        ):
            result = auto_create_rewrites("products", confidence_threshold=0.8)

        assert result[0]["action"] == "skipped"
        assert result[0]["rewrite_id"] is None

    @pytest.mark.django_db
    def test_already_exists_when_rewrite_present(self):
        from icv_search.testing.factories import QueryRewriteFactory

        QueryRewriteFactory(
            index_name="products",
            query_pattern="waterproof jacket",
            match_type="exact",
            tenant_id="",
        )

        with patch(
            "icv_search.services.intelligence.suggest_synonyms",
            return_value=[
                {
                    "source_query": "waterproof jacket",
                    "suggested_synonym": "rain coat",
                    "confidence": 0.87,
                    "evidence_count": 2,
                }
            ],
        ):
            result = auto_create_rewrites("products", confidence_threshold=0.8)

        assert result[0]["action"] == "already_exists"
        assert result[0]["rewrite_id"] is not None

    @pytest.mark.django_db
    def test_dry_run_does_not_create_record(self):
        from icv_search.models.merchandising import QueryRewrite

        with patch(
            "icv_search.services.intelligence.suggest_synonyms",
            return_value=[
                {
                    "source_query": "waterproof jacket",
                    "suggested_synonym": "rain coat",
                    "confidence": 0.87,
                    "evidence_count": 2,
                }
            ],
        ):
            result = auto_create_rewrites(
                "products",
                confidence_threshold=0.8,
                dry_run=True,
            )

        assert result[0]["action"] == "created"
        assert result[0]["rewrite_id"] is None
        assert QueryRewrite.objects.filter(query_pattern="waterproof jacket").count() == 0

    @pytest.mark.django_db
    def test_tenant_id_scoped_rewrite(self):
        with patch(
            "icv_search.services.intelligence.suggest_synonyms",
            return_value=[
                {
                    "source_query": "query a",
                    "suggested_synonym": "synonym a",
                    "confidence": 0.90,
                    "evidence_count": 1,
                }
            ],
        ):
            result = auto_create_rewrites(
                "products",
                confidence_threshold=0.8,
                tenant_id="tenant-1",
            )

        assert result[0]["action"] == "created"
        from icv_search.models.merchandising import QueryRewrite

        rewrite = QueryRewrite.objects.get(pk=result[0]["rewrite_id"])
        assert rewrite.tenant_id == "tenant-1"

    @pytest.mark.django_db
    def test_mixed_actions_returned_in_single_call(self):
        suggestions = [
            {"source_query": "a", "suggested_synonym": "x", "confidence": 0.95, "evidence_count": 2},
            {"source_query": "b", "suggested_synonym": "y", "confidence": 0.60, "evidence_count": 1},
        ]

        with patch(
            "icv_search.services.intelligence.suggest_synonyms",
            return_value=suggestions,
        ):
            result = auto_create_rewrites("products", confidence_threshold=0.8)

        actions = {r["source_query"]: r["action"] for r in result}
        assert actions["a"] == "created"
        assert actions["b"] == "skipped"
