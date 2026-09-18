"""Verify the public ``icv_search.testing`` export surface (#49)."""

from __future__ import annotations

import importlib

import pytest

EXPECTED_TESTING_EXPORTS = (
    "BoostRuleFactory",
    "IndexSyncLogFactory",
    "MockPreprocessor",
    "QueryRedirectFactory",
    "QueryRewriteFactory",
    "SearchBannerFactory",
    "SearchClickAggregateFactory",
    "SearchClickFactory",
    "SearchIndexFactory",
    "SearchPinFactory",
    "SearchQueryAggregateFactory",
    "SearchQueryLogFactory",
    "ZeroResultFallbackFactory",
)


class TestTestingExports:
    """All names in ``icv_search.testing.__all__`` are importable."""

    def test_all_has_exactly_thirteen_names(self):
        testing = importlib.import_module("icv_search.testing")

        assert len(testing.__all__) == 13
        assert set(testing.__all__) == set(EXPECTED_TESTING_EXPORTS)

    @pytest.mark.parametrize("name", EXPECTED_TESTING_EXPORTS)
    def test_export_is_importable_and_listed_in_all(self, name):
        testing = importlib.import_module("icv_search.testing")

        assert name in testing.__all__
        assert getattr(testing, name) is not None
