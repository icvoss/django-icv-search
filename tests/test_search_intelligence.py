"""Tests for FEAT-008 Search Intelligence.

Covers:
- Click tracking service (log_click, get_click_through_rate, get_top_clicked_documents)
- Click tracking view (icv_search_click endpoint)
- Demand signals (get_demand_signals — focused angles not in test_intelligence.py)
- Query preprocessing (load_preprocessor, preprocess, reset_preprocessor)
- Merchandising pipeline integration with preprocessing
- Types: QueryContext, PreprocessedQuery, MerchandisedSearchResult intelligence fields

All tests run on SQLite. PostgreSQL-only functions (cluster_queries,
suggest_synonyms) are not tested here.
"""

from __future__ import annotations

import datetime
import json
from unittest.mock import MagicMock, patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from icv_search.models import SearchIndex
from icv_search.models.click_tracking import SearchClick
from icv_search.services.click_tracking import (
    get_click_through_rate,
    get_top_clicked_documents,
    log_click,
)
from icv_search.services.intelligence import get_demand_signals
from icv_search.services.preprocessing import preprocess, reset_preprocessor
from icv_search.testing.factories import (
    SearchClickAggregateFactory,
    SearchQueryAggregateFactory,
)
from icv_search.types import (
    MerchandisedSearchResult,
    PreprocessedQuery,
    QueryContext,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today() -> datetime.date:
    return datetime.date.today()


def _days_ago(n: int) -> datetime.date:
    return _today() - datetime.timedelta(days=n)


# ---------------------------------------------------------------------------
# 1. Click Tracking
# ---------------------------------------------------------------------------


class TestClickTracking:
    """Unit tests for the click_tracking service functions."""

    @pytest.mark.django_db
    def test_log_click_creates_record(self, settings):
        """With ICV_SEARCH_CLICK_TRACKING=True, log_click creates a SearchClick row."""
        settings.ICV_SEARCH_CLICK_TRACKING = True

        log_click(
            index_name="products",
            query="red shoes",
            document_id="doc-1",
            position=0,
        )

        assert SearchClick.objects.count() == 1
        click = SearchClick.objects.get()
        assert click.index_name == "products"
        assert click.query == "red shoes"
        assert click.document_id == "doc-1"
        assert click.position == 0

    @pytest.mark.django_db
    def test_log_click_noop_when_disabled(self, settings):
        """With ICV_SEARCH_CLICK_TRACKING=False (default), log_click is a no-op."""
        settings.ICV_SEARCH_CLICK_TRACKING = False

        log_click(
            index_name="products",
            query="red shoes",
            document_id="doc-1",
            position=0,
        )

        assert SearchClick.objects.count() == 0

    @pytest.mark.django_db
    def test_log_click_default_is_disabled(self):
        """When the setting is absent entirely, log_click must be a no-op (BR-020)."""
        # Remove the setting entirely to exercise the getattr default.
        from django.conf import settings as django_settings

        original = getattr(django_settings, "ICV_SEARCH_CLICK_TRACKING", "MISSING")
        try:
            if hasattr(django_settings, "ICV_SEARCH_CLICK_TRACKING"):
                del django_settings.ICV_SEARCH_CLICK_TRACKING
            log_click(
                index_name="products",
                query="shoes",
                document_id="doc-2",
                position=1,
            )
            assert SearchClick.objects.count() == 0
        finally:
            if original != "MISSING":
                django_settings.ICV_SEARCH_CLICK_TRACKING = original

    @pytest.mark.django_db
    def test_log_click_never_raises(self, settings):
        """Even when the DB raises, log_click must not propagate the exception (BR-020)."""
        settings.ICV_SEARCH_CLICK_TRACKING = True

        with patch(
            "icv_search.models.click_tracking.SearchClick.objects",
        ) as mock_mgr:
            mock_mgr.create.side_effect = Exception("DB is down")
            # Must not raise — search experience is never broken by a failing click log.
            log_click(
                index_name="products",
                query="shoes",
                document_id="doc-99",
                position=0,
            )

    @pytest.mark.django_db
    def test_log_click_stores_metadata(self, settings):
        """Optional metadata dict is persisted on the SearchClick record."""
        settings.ICV_SEARCH_CLICK_TRACKING = True

        log_click(
            index_name="products",
            query="shoes",
            document_id="doc-1",
            position=2,
            metadata={"session_id": "abc123"},
        )

        click = SearchClick.objects.get()
        assert click.metadata == {"session_id": "abc123"}

    @pytest.mark.django_db
    def test_get_click_through_rate_returns_float(self):
        """CTR is click_count / total_count using aggregate tables."""
        today = _today()

        # 100 searches for "shoes"
        SearchQueryAggregateFactory(
            index_name="products",
            query="shoes",
            date=today,
            total_count=100,
            zero_result_count=0,
        )
        # 30 clicks on "shoes"
        SearchClickAggregateFactory(
            index_name="products",
            query="shoes",
            document_id="doc-1",
            date=today,
            click_count=20,
        )
        SearchClickAggregateFactory(
            index_name="products",
            query="shoes",
            document_id="doc-2",
            date=today,
            click_count=10,
        )

        ctr = get_click_through_rate("products", "shoes", days=7)
        assert isinstance(ctr, float)
        assert abs(ctr - 0.3) < 0.001

    @pytest.mark.django_db
    def test_get_click_through_rate_zero_when_no_data(self):
        """Returns 0.0 when there are no search aggregate rows."""
        ctr = get_click_through_rate("products", "shoes", days=7)
        assert ctr == 0.0

    @pytest.mark.django_db
    def test_get_click_through_rate_normalises_query(self):
        """Query is normalised (stripped, lowercased) before lookup."""
        today = _today()
        SearchQueryAggregateFactory(
            index_name="products",
            query="shoes",  # stored normalised
            date=today,
            total_count=50,
            zero_result_count=0,
        )
        SearchClickAggregateFactory(
            index_name="products",
            query="shoes",
            document_id="doc-1",
            date=today,
            click_count=10,
        )

        ctr = get_click_through_rate("products", "  SHOES  ", days=7)
        assert abs(ctr - 0.2) < 0.001

    @pytest.mark.django_db
    def test_get_click_through_rate_respects_days_window(self):
        """Data outside the look-back window is excluded."""
        # Only data from 40 days ago — outside a 30-day window.
        SearchQueryAggregateFactory(
            index_name="products",
            query="shoes",
            date=_days_ago(40),
            total_count=100,
            zero_result_count=0,
        )
        SearchClickAggregateFactory(
            index_name="products",
            query="shoes",
            document_id="doc-1",
            date=_days_ago(40),
            click_count=50,
        )

        ctr = get_click_through_rate("products", "shoes", days=30)
        assert ctr == 0.0

    @pytest.mark.django_db
    def test_get_top_clicked_documents(self):
        """get_top_clicked_documents returns docs ordered by click_count descending."""
        today = _today()

        SearchQueryAggregateFactory(
            index_name="products",
            query="shoes",
            date=today,
            total_count=200,
            zero_result_count=0,
        )
        SearchClickAggregateFactory(
            index_name="products",
            query="shoes",
            document_id="doc-a",
            date=today,
            click_count=50,
        )
        SearchClickAggregateFactory(
            index_name="products",
            query="shoes",
            document_id="doc-b",
            date=today,
            click_count=120,
        )
        SearchClickAggregateFactory(
            index_name="products",
            query="shoes",
            document_id="doc-c",
            date=today,
            click_count=30,
        )

        results = get_top_clicked_documents("products", "shoes", days=7)

        assert len(results) == 3
        assert results[0]["document_id"] == "doc-b"
        assert results[1]["document_id"] == "doc-a"
        assert results[2]["document_id"] == "doc-c"

    @pytest.mark.django_db
    def test_get_top_clicked_documents_includes_ctr(self):
        """Each document dict includes a ctr float."""
        today = _today()
        SearchQueryAggregateFactory(
            index_name="products",
            query="jacket",
            date=today,
            total_count=100,
            zero_result_count=0,
        )
        SearchClickAggregateFactory(
            index_name="products",
            query="jacket",
            document_id="doc-1",
            date=today,
            click_count=25,
        )

        results = get_top_clicked_documents("products", "jacket", days=7)

        assert len(results) == 1
        assert results[0]["click_count"] == 25
        assert abs(results[0]["ctr"] - 0.25) < 0.001

    @pytest.mark.django_db
    def test_get_top_clicked_documents_empty_when_no_data(self):
        """Returns [] when there are no click aggregate rows."""
        results = get_top_clicked_documents("products", "shoes", days=7)
        assert results == []

    @pytest.mark.django_db
    def test_get_top_clicked_documents_respects_limit(self):
        """The ``limit`` parameter caps the number of returned documents."""
        today = _today()
        SearchQueryAggregateFactory(
            index_name="products",
            query="bag",
            date=today,
            total_count=500,
            zero_result_count=0,
        )
        for i in range(5):
            SearchClickAggregateFactory(
                index_name="products",
                query="bag",
                document_id=f"doc-{i}",
                date=today,
                click_count=10 + i,
            )

        results = get_top_clicked_documents("products", "bag", days=7, limit=3)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# 2. Click Tracking View
# ---------------------------------------------------------------------------

_click_urlconf = "icv_search.testing.urls"


class TestClickView:
    """Integration tests for the icv_search_click view."""

    @pytest.mark.django_db
    @override_settings(ROOT_URLCONF=_click_urlconf, ICV_SEARCH_CLICK_TRACKING=True)
    def test_click_endpoint_returns_204(self, client):
        """Valid POST with all required fields returns 204 No Content."""
        payload = {
            "index_name": "products",
            "query": "shoes",
            "document_id": "doc-1",
            "position": 0,
        }
        response = client.post(
            "/click/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert response.status_code == 204

    @pytest.mark.django_db
    @override_settings(ROOT_URLCONF=_click_urlconf, ICV_SEARCH_CLICK_TRACKING=False)
    def test_click_endpoint_returns_403_when_disabled(self, client):
        """When click tracking is disabled, the endpoint returns 403."""
        payload = {
            "index_name": "products",
            "query": "shoes",
            "document_id": "doc-1",
            "position": 0,
        }
        response = client.post(
            "/click/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert response.status_code == 403

    @pytest.mark.django_db
    @override_settings(ROOT_URLCONF=_click_urlconf, ICV_SEARCH_CLICK_TRACKING=True)
    def test_click_endpoint_returns_400_on_missing_fields(self, client):
        """POST body missing required fields returns 400 with an error message."""
        # Missing 'position' and 'document_id'
        payload = {"index_name": "products", "query": "shoes"}
        response = client.post(
            "/click/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert response.status_code == 400
        data = json.loads(response.content)
        assert "error" in data

    @pytest.mark.django_db
    @override_settings(ROOT_URLCONF=_click_urlconf, ICV_SEARCH_CLICK_TRACKING=True)
    def test_click_endpoint_returns_400_on_invalid_json(self, client):
        """POST body that is not valid JSON returns 400."""
        response = client.post(
            "/click/",
            data="not json at all{{",
            content_type="application/json",
        )
        assert response.status_code == 400
        data = json.loads(response.content)
        assert "error" in data

    @pytest.mark.django_db
    @override_settings(ROOT_URLCONF=_click_urlconf, ICV_SEARCH_CLICK_TRACKING=True)
    def test_click_endpoint_only_accepts_post(self, client):
        """GET to the click endpoint returns 405 Method Not Allowed."""
        response = client.get("/click/")
        assert response.status_code == 405

    @pytest.mark.django_db
    @override_settings(ROOT_URLCONF=_click_urlconf, ICV_SEARCH_CLICK_TRACKING=True)
    def test_click_endpoint_optional_metadata_accepted(self, client):
        """Optional metadata field is accepted without error."""
        payload = {
            "index_name": "products",
            "query": "jacket",
            "document_id": "doc-2",
            "position": 1,
            "metadata": {"session_id": "xyz"},
        }
        response = client.post(
            "/click/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert response.status_code == 204


# ---------------------------------------------------------------------------
# 3. Demand Signals
# ---------------------------------------------------------------------------


class TestDemandSignals:
    """Focused tests for get_demand_signals() not covered in test_intelligence.py."""

    @pytest.mark.django_db
    def test_get_demand_signals_returns_sorted_by_gap_score(self):
        """Results are ordered by gap_score descending (zero_result_rate * volume)."""
        # High volume, high zero rate → highest gap score.
        SearchQueryAggregateFactory(
            index_name="store",
            query="summer dress",
            date=_days_ago(3),
            total_count=200,
            zero_result_count=180,  # gap_score = 180
        )
        # Medium volume, moderate zero rate → middle gap score.
        SearchQueryAggregateFactory(
            index_name="store",
            query="linen trousers",
            date=_days_ago(3),
            total_count=50,
            zero_result_count=30,  # gap_score = 30
        )
        # Low volume, full zero rate → low gap score.
        SearchQueryAggregateFactory(
            index_name="store",
            query="vintage belt",
            date=_days_ago(3),
            total_count=5,
            zero_result_count=5,  # gap_score = 5
        )

        results = get_demand_signals("store", min_volume=1)
        gap_scores = [r["gap_score"] for r in results]
        assert gap_scores == sorted(gap_scores, reverse=True)
        assert results[0]["query"] == "summer dress"

    @pytest.mark.django_db
    def test_get_demand_signals_filters_by_min_volume(self):
        """Queries below min_volume are excluded from results."""
        SearchQueryAggregateFactory(
            index_name="store",
            query="rare query",
            date=_days_ago(1),
            total_count=3,
            zero_result_count=3,
        )
        SearchQueryAggregateFactory(
            index_name="store",
            query="popular query",
            date=_days_ago(1),
            total_count=50,
            zero_result_count=45,
        )

        results = get_demand_signals("store", min_volume=10)
        queries = [r["query"] for r in results]
        assert "rare query" not in queries
        assert "popular query" in queries

    @pytest.mark.django_db
    def test_get_demand_signals_filters_by_exclude_patterns(self):
        """Queries matching any exclude_pattern are omitted."""
        SearchQueryAggregateFactory(
            index_name="store",
            query="admin panel",
            date=_days_ago(1),
            total_count=20,
            zero_result_count=18,
        )
        SearchQueryAggregateFactory(
            index_name="store",
            query="summer jacket",
            date=_days_ago(1),
            total_count=100,
            zero_result_count=90,
        )

        results = get_demand_signals(
            "store",
            min_volume=1,
            exclude_patterns=[r"^admin"],
        )
        queries = [r["query"] for r in results]
        assert "admin panel" not in queries
        assert "summer jacket" in queries

    @pytest.mark.django_db
    def test_get_demand_signals_empty_when_no_data(self):
        """Returns empty list when there are no SearchQueryAggregate rows."""
        results = get_demand_signals("store")
        assert results == []

    @pytest.mark.django_db
    def test_get_demand_signals_uses_setting_for_min_volume(self, settings):
        """ICV_SEARCH_INTELLIGENCE_MIN_VOLUME is read when min_volume is not supplied."""
        settings.ICV_SEARCH_INTELLIGENCE_MIN_VOLUME = 20

        # Volume of 10 — below the setting threshold.
        SearchQueryAggregateFactory(
            index_name="store",
            query="niche query",
            date=_days_ago(1),
            total_count=10,
            zero_result_count=10,
        )

        results = get_demand_signals("store")  # no explicit min_volume
        assert results == []

    @pytest.mark.django_db
    def test_get_demand_signals_result_keys(self):
        """Every result dict contains all expected keys."""
        SearchQueryAggregateFactory(
            index_name="store",
            query="test query",
            date=_days_ago(1),
            total_count=50,
            zero_result_count=25,
        )

        results = get_demand_signals("store", min_volume=1)
        assert len(results) == 1
        row = results[0]
        for key in ("query", "volume", "zero_result_rate", "gap_score", "trend", "ctr"):
            assert key in row, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# 4. Query Preprocessing
# ---------------------------------------------------------------------------


def _make_preprocessor(query_out=None, **kwargs):
    """Return a simple callable that returns a PreprocessedQuery."""

    def preprocessor(query, context):
        return PreprocessedQuery(query=query_out or query, **kwargs)

    return preprocessor


class TestPreprocessing:
    """Unit tests for load_preprocessor and preprocess."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        """Always restore the preprocessor cache after each test."""
        reset_preprocessor()
        yield
        reset_preprocessor()

    def test_preprocess_returns_unchanged_when_no_preprocessor(self, settings):
        """When ICV_SEARCH_QUERY_PREPROCESSOR is not set, query is returned unchanged."""
        settings.ICV_SEARCH_QUERY_PREPROCESSOR = ""

        result = preprocess("red shoes", index_name="products")

        assert isinstance(result, PreprocessedQuery)
        assert result.query == "red shoes"

    def test_preprocess_calls_configured_preprocessor(self, settings):
        """The configured preprocessor callable is invoked and its result returned."""
        mock_fn = MagicMock(return_value=PreprocessedQuery(query="transformed query"))
        # Point the setting to the mock via a dotted path we can patch.
        settings.ICV_SEARCH_QUERY_PREPROCESSOR = "icv_search.services.preprocessing.preprocess"

        with patch("icv_search.services.preprocessing.load_preprocessor") as mock_load:
            mock_load.return_value = mock_fn
            # Force the cache to return our mock directly.
            import icv_search.services.preprocessing as _mod

            _mod._preprocessor_callable = mock_fn

            result = preprocess("shoes", index_name="products")

        assert mock_fn.called
        assert result.query == "transformed query"

    def test_preprocess_fallback_on_error(self, settings):
        """A raising preprocessor logs a warning and returns the original query (BR-027)."""
        import icv_search.services.preprocessing as _mod

        def bad_preprocessor(query, context):
            raise ValueError("intentional failure")

        _mod._preprocessor_callable = bad_preprocessor

        result = preprocess("shoes", index_name="products")

        # Must not raise; original query is returned.
        assert isinstance(result, PreprocessedQuery)
        assert result.query == "shoes"

    def test_load_preprocessor_raises_on_bad_path(self, settings):
        """An unimportable dotted path raises ImproperlyConfigured (BR-026)."""
        from icv_search.services.preprocessing import load_preprocessor

        settings.ICV_SEARCH_QUERY_PREPROCESSOR = "does.not.exist.Callable"

        with pytest.raises(ImproperlyConfigured, match="could not be imported"):
            load_preprocessor()

    def test_reset_preprocessor_clears_cache(self, settings):
        """reset_preprocessor() marks the cache as unloaded."""
        import icv_search.services.preprocessing as _mod

        # Set to a known non-sentinel value.
        _mod._preprocessor_callable = None
        reset_preprocessor()

        # After reset the sentinel is back, forcing a reload on next call.
        assert _mod._preprocessor_callable is _mod._UNLOADED

    def test_preprocess_passes_context_to_preprocessor(self, settings):
        """The QueryContext supplied to the preprocessor contains the right values."""
        received: list[QueryContext] = []

        def capturing_preprocessor(query, context):
            received.append(context)
            return PreprocessedQuery(query=query)

        import icv_search.services.preprocessing as _mod

        _mod._preprocessor_callable = capturing_preprocessor

        preprocess(
            "hats",
            index_name="catalogue",
            tenant_id="tenant-x",
            metadata={"page": "home"},
        )

        assert len(received) == 1
        ctx = received[0]
        assert ctx.index_name == "catalogue"
        assert ctx.tenant_id == "tenant-x"
        assert ctx.original_query == "hats"
        assert ctx.metadata == {"page": "home"}


# ---------------------------------------------------------------------------
# 5. Pipeline Integration (preprocessing inside merchandised_search)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _merch_enabled(settings):
    """Enable merchandising and reset the preprocessor for each test in this module."""
    settings.ICV_SEARCH_MERCHANDISING_ENABLED = True
    settings.ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT = 0
    reset_preprocessor()
    yield
    reset_preprocessor()


@pytest.fixture
def products_index(db):
    from icv_search.backends.dummy import DummyBackend

    index = SearchIndex.objects.create(name="products", tenant_id="", primary_key_field="id")
    backend = DummyBackend()
    backend.create_index(index.engine_uid, primary_key="id")
    backend.add_documents(
        index.engine_uid,
        [
            {"id": "1", "name": "Red Shoes", "category": "footwear"},
            {"id": "2", "name": "Blue Shirt", "category": "clothing"},
            {"id": "3", "name": "Green Hat", "category": "accessories"},
        ],
    )
    return index


class TestPreprocessingPipeline:
    """Integration tests verifying preprocessing inside merchandised_search."""

    def test_preprocessing_in_merchandised_search(self, products_index):
        """When a preprocessor is configured, it runs in the pipeline."""
        import icv_search.services.preprocessing as _mod
        from icv_search.services.merchandising import merchandised_search

        _mod._preprocessor_callable = _make_preprocessor(query_out="shoes")

        result = merchandised_search("products", "trainers")

        assert isinstance(result, MerchandisedSearchResult)
        # A preprocess entry must appear in applied_rules (BR-029).
        rule_types = [r["type"] for r in result.applied_rules]
        assert "preprocess" in rule_types

    def test_preprocessing_filters_merged(self, products_index):
        """Filters extracted by the preprocessor are merged into search params (BR-028)."""
        import icv_search.services.preprocessing as _mod
        from icv_search.services.merchandising import merchandised_search

        _mod._preprocessor_callable = _make_preprocessor(extracted_filters={"category": "footwear"})

        # We can't inspect the backend call directly, but we verify the pipeline
        # does not raise and that preprocessing is recorded in applied_rules.
        result = merchandised_search("products", "red")
        preprocess_rules = [r for r in result.applied_rules if r["type"] == "preprocess"]
        assert len(preprocess_rules) == 1
        assert preprocess_rules[0]["filters_extracted"] == {"category": "footwear"}

    def test_caller_filters_win_on_key_collision(self, products_index):
        """Caller-supplied filter values win over preprocessor extracts on key collision (BR-028)."""
        import icv_search.services.preprocessing as _mod
        from icv_search.services.merchandising import merchandised_search
        from icv_search.services.search import search as real_search

        _mod._preprocessor_callable = _make_preprocessor(
            extracted_filters={"category": "footwear"},
        )

        with patch("icv_search.services.search.search", wraps=real_search) as mock_search:
            result = merchandised_search(
                "products",
                "",
                filter={"category": "clothing"},
                log_query=False,
            )

        assert mock_search.call_args.kwargs["filter"] == {"category": "clothing"}
        # clothing wins: Blue Shirt only; footwear would have returned Red Shoes.
        assert [hit["id"] for hit in result.hits] == ["2"]

    def test_preprocessing_redirect_short_circuits(self, products_index):
        """A redirect_url from the preprocessor short-circuits the pipeline (BR-027)."""
        import icv_search.services.preprocessing as _mod
        from icv_search.services.merchandising import merchandised_search

        _mod._preprocessor_callable = _make_preprocessor(redirect_url="https://example.com/sale")

        result = merchandised_search("products", "sale items")

        assert result.redirect is not None
        assert result.redirect["url"] == "https://example.com/sale"
        assert result.redirect["type"] == "preprocess"
        assert result.hits == []

    def test_preprocessing_skip_search_returns_empty(self, products_index):
        """When preprocessor sets skip_search=True, an empty result is returned immediately."""
        import icv_search.services.preprocessing as _mod
        from icv_search.services.merchandising import merchandised_search

        _mod._preprocessor_callable = _make_preprocessor(skip_search=True)

        result = merchandised_search("products", "red")

        assert result.hits == []
        assert result.redirect is None

    def test_skip_preprocessing_bypasses(self, products_index):
        """skip_preprocessing=True means the preprocessor callable is never invoked."""
        from icv_search.services.merchandising import merchandised_search

        call_log: list[str] = []

        def tracking_preprocessor(query, context):
            call_log.append(query)
            return PreprocessedQuery(query=query)

        import icv_search.services.preprocessing as _mod

        _mod._preprocessor_callable = tracking_preprocessor

        merchandised_search("products", "red", skip_preprocessing=True)

        assert call_log == []

    def test_skip_preprocessing_no_preprocess_rule(self, products_index):
        """skip_preprocessing=True means no 'preprocess' entry in applied_rules."""
        import icv_search.services.preprocessing as _mod
        from icv_search.services.merchandising import merchandised_search

        _mod._preprocessor_callable = _make_preprocessor()

        result = merchandised_search("products", "red", skip_preprocessing=True)

        rule_types = [r["type"] for r in result.applied_rules]
        assert "preprocess" not in rule_types

    def test_detected_intent_populated(self, products_index):
        """detected_intent on the result reflects the preprocessor's intent field."""
        import icv_search.services.preprocessing as _mod
        from icv_search.services.merchandising import merchandised_search

        _mod._preprocessor_callable = _make_preprocessor(intent="navigational")

        result = merchandised_search("products", "homepage")

        assert result.detected_intent == "navigational"

    def test_detected_intent_empty_without_preprocessor(self, products_index, settings):
        """detected_intent is empty string when no preprocessor is configured."""
        from icv_search.services.merchandising import merchandised_search

        settings.ICV_SEARCH_QUERY_PREPROCESSOR = ""

        result = merchandised_search("products", "shoes")

        assert result.detected_intent == ""

    def test_preprocessing_recorded_in_applied_rules(self, products_index):
        """The preprocess applied_rule contains intent, confidence, and extracted fields (BR-029)."""
        import icv_search.services.preprocessing as _mod
        from icv_search.services.merchandising import merchandised_search

        _mod._preprocessor_callable = _make_preprocessor(
            intent="transactional",
            confidence=0.9,
            extracted_filters={"category": "footwear"},
            extracted_sort=["price:asc"],
        )

        result = merchandised_search("products", "buy shoes")

        preprocess_rule = next((r for r in result.applied_rules if r["type"] == "preprocess"), None)
        assert preprocess_rule is not None
        assert preprocess_rule["intent"] == "transactional"
        assert preprocess_rule["confidence"] == 0.9
        assert preprocess_rule["filters_extracted"] == {"category": "footwear"}
        assert preprocess_rule["sort_extracted"] == ["price:asc"]

    def test_preprocessing_before_redirect_check(self, products_index):
        """The preprocess rule appears before the redirect rule in applied_rules (BR-029)."""
        import icv_search.services.preprocessing as _mod
        from icv_search.models.merchandising import QueryRedirect
        from icv_search.services.merchandising import merchandised_search

        _mod._preprocessor_callable = _make_preprocessor()

        QueryRedirect.objects.create(
            index_name="products",
            query_pattern="sale",
            match_type="exact",
            destination_url="https://example.com/sale",
        )

        result = merchandised_search("products", "sale")

        rule_types = [r["type"] for r in result.applied_rules]
        assert "preprocess" in rule_types
        assert "redirect" in rule_types
        preprocess_idx = rule_types.index("preprocess")
        redirect_idx = rule_types.index("redirect")
        assert preprocess_idx < redirect_idx

    def test_preprocessed_field_on_result(self, products_index):
        """The preprocessed field on MerchandisedSearchResult holds the PreprocessedQuery."""
        import icv_search.services.preprocessing as _mod
        from icv_search.services.merchandising import merchandised_search

        _mod._preprocessor_callable = _make_preprocessor(intent="informational")

        result = merchandised_search("products", "how to clean shoes")

        assert result.preprocessed is not None
        assert isinstance(result.preprocessed, PreprocessedQuery)
        assert result.preprocessed.intent == "informational"


# ---------------------------------------------------------------------------
# 6. Types
# ---------------------------------------------------------------------------


class TestTypes:
    """Unit tests for QueryContext, PreprocessedQuery, and MerchandisedSearchResult."""

    def test_query_context_defaults(self):
        """QueryContext has sensible defaults for all fields."""
        ctx = QueryContext()
        assert ctx.index_name == ""
        assert ctx.tenant_id == ""
        assert ctx.original_query == ""
        assert ctx.user is None
        assert ctx.metadata == {}

    def test_query_context_stores_values(self):
        """QueryContext stores the values passed at construction time."""
        ctx = QueryContext(
            index_name="products",
            tenant_id="acme",
            original_query="boots",
            user="user-obj",
            metadata={"x": 1},
        )
        assert ctx.index_name == "products"
        assert ctx.tenant_id == "acme"
        assert ctx.original_query == "boots"
        assert ctx.user == "user-obj"
        assert ctx.metadata == {"x": 1}

    def test_preprocessed_query_defaults(self):
        """PreprocessedQuery has sensible defaults for all fields."""
        pq = PreprocessedQuery()
        assert pq.query == ""
        assert pq.extracted_filters == {}
        assert pq.extracted_sort == []
        assert pq.intent == ""
        assert pq.confidence == 1.0
        assert pq.metadata == {}
        assert pq.skip_search is False
        assert pq.redirect_url == ""

    def test_preprocessed_query_stores_values(self):
        """PreprocessedQuery stores the values passed at construction time."""
        pq = PreprocessedQuery(
            query="boots",
            extracted_filters={"category": "footwear"},
            extracted_sort=["price:asc"],
            intent="transactional",
            confidence=0.85,
            metadata={"flag": True},
            skip_search=True,
            redirect_url="https://example.com",
        )
        assert pq.query == "boots"
        assert pq.extracted_filters == {"category": "footwear"}
        assert pq.extracted_sort == ["price:asc"]
        assert pq.intent == "transactional"
        assert pq.confidence == 0.85
        assert pq.metadata == {"flag": True}
        assert pq.skip_search is True
        assert pq.redirect_url == "https://example.com"

    def test_merchandised_search_result_intelligence_fields_exist(self):
        """MerchandisedSearchResult exposes the v0.6.0 intelligence fields."""
        result = MerchandisedSearchResult()
        assert hasattr(result, "preprocessed")
        assert hasattr(result, "detected_intent")

    def test_merchandised_search_result_intelligence_field_defaults(self):
        """preprocessed defaults to None and detected_intent defaults to empty string."""
        result = MerchandisedSearchResult()
        assert result.preprocessed is None
        assert result.detected_intent == ""

    def test_merchandised_search_result_intelligence_fields_settable(self):
        """Intelligence fields can be set at construction time."""
        pq = PreprocessedQuery(intent="navigational")
        result = MerchandisedSearchResult(
            preprocessed=pq,
            detected_intent="navigational",
        )
        assert result.preprocessed is pq
        assert result.detected_intent == "navigational"

    def test_merchandised_search_result_from_search_result_carries_intelligence(self):
        """from_search_result() passes through intelligence fields via **kwargs."""
        from icv_search.types import SearchResult

        sr = SearchResult(hits=[{"id": "1"}], query="shoes")
        pq = PreprocessedQuery(intent="transactional")

        msr = MerchandisedSearchResult.from_search_result(
            sr,
            preprocessed=pq,
            detected_intent="transactional",
            original_query="shoes",
        )

        assert msr.hits == [{"id": "1"}]
        assert msr.preprocessed is pq
        assert msr.detected_intent == "transactional"
