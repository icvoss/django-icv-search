"""Tests for the boost rule service functions."""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from icv_search.models.merchandising import BoostRule
from icv_search.services.boosts import _evaluate_operator, apply_boosts, get_boost_rules_for_query
from icv_search.types import SearchResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rule(**kwargs) -> BoostRule:
    """Create and save a BoostRule with sensible defaults."""
    defaults = {
        "index_name": "products",
        "tenant_id": "",
        "query_pattern": "shoes",
        "match_type": "exact",
        "field": "category",
        "field_value": "footwear",
        "operator": "eq",
        "boost_weight": Decimal("2.0"),
        "is_active": True,
        "priority": 0,
    }
    defaults.update(kwargs)
    return BoostRule.objects.create(**defaults)


def _make_result(
    hits: list | None = None,
    query: str = "shoes",
    estimated_total_hits: int | None = None,
    ranking_scores: list | None = None,
) -> SearchResult:
    """Build a SearchResult for use in apply_boosts() tests."""
    if hits is None:
        hits = [
            {"id": "1", "name": "Trainer", "category": "footwear", "price": "50"},
            {"id": "2", "name": "Hat", "category": "accessories", "price": "20"},
            {"id": "3", "name": "Boot", "category": "footwear", "price": "80"},
        ]
    return SearchResult(
        hits=hits,
        query=query,
        estimated_total_hits=estimated_total_hits if estimated_total_hits is not None else len(hits),
        ranking_scores=ranking_scores or [],
    )


# ---------------------------------------------------------------------------
# get_boost_rules_for_query()
# ---------------------------------------------------------------------------


class TestGetBoostRulesForQuery:
    """get_boost_rules_for_query() — rule matching."""

    @pytest.mark.django_db
    def test_returns_matching_rules(self):
        rule = _make_rule()
        result = get_boost_rules_for_query("products", "shoes")
        assert len(result) == 1
        assert result[0].pk == rule.pk

    @pytest.mark.django_db
    def test_returns_empty_list_when_no_match(self):
        _make_rule(query_pattern="boots", match_type="exact")
        result = get_boost_rules_for_query("products", "shoes")
        assert result == []

    @pytest.mark.django_db
    def test_inactive_rule_is_skipped(self):
        _make_rule(is_active=False)
        result = get_boost_rules_for_query("products", "shoes")
        assert result == []

    @pytest.mark.django_db
    def test_scheduled_rule_not_yet_started_is_skipped(self):
        _make_rule(starts_at=timezone.now() + timedelta(hours=1))
        result = get_boost_rules_for_query("products", "shoes")
        assert result == []

    @pytest.mark.django_db
    def test_expired_rule_is_skipped(self):
        _make_rule(ends_at=timezone.now() - timedelta(hours=1))
        result = get_boost_rules_for_query("products", "shoes")
        assert result == []

    @pytest.mark.django_db
    def test_tenant_scoped_rule_matches_same_tenant(self):
        rule = _make_rule(tenant_id="acme")
        result = get_boost_rules_for_query("products", "shoes", tenant_id="acme")
        assert len(result) == 1
        assert result[0].pk == rule.pk

    @pytest.mark.django_db
    def test_tenant_scoped_rule_does_not_match_other_tenant(self):
        _make_rule(tenant_id="acme")
        result = get_boost_rules_for_query("products", "shoes", tenant_id="other")
        assert result == []

    @pytest.mark.django_db
    def test_global_rule_matches_when_tenant_provided(self):
        """A rule with blank tenant_id applies to all tenants."""
        rule = _make_rule(tenant_id="")
        result = get_boost_rules_for_query("products", "shoes", tenant_id="acme")
        assert len(result) == 1
        assert result[0].pk == rule.pk


# ---------------------------------------------------------------------------
# apply_boosts() — no-op cases
# ---------------------------------------------------------------------------


class TestApplyBoostsNoOp:
    """apply_boosts() returns the original result unchanged for empty inputs."""

    def test_empty_rules_returns_result_unchanged(self):
        result = _make_result()
        out = apply_boosts(result, [])
        assert out is result

    def test_empty_hits_returns_result_unchanged(self):
        result = SearchResult(hits=[], query="shoes", estimated_total_hits=0)
        rule = BoostRule(field="category", operator="eq", field_value="footwear", boost_weight=Decimal("2.0"))
        out = apply_boosts(result, [rule])
        assert out is result


# ---------------------------------------------------------------------------
# apply_boosts() — promotion and demotion
# ---------------------------------------------------------------------------


class TestApplyBoostsPromotion:
    """apply_boosts() — weight > 1 promotes matching documents."""

    def test_boosted_document_moves_to_first_position(self):
        """The last footwear item should jump to the top after a sufficiently high boost.

        With 3 hits and no ranking_scores, position-based defaults are:
        id=1 → 1.0, id=2 → 0.5, id=3 → 0.0.
        A 10× boost on id=3 gives 0.0 × 10 = 0.0, which is still last.
        Using 2 hits keeps the maths simple:
        id=1 (accessories) → 1.0, id=2 (footwear) → 0.0.
        A 5× boost on id=2 → 0.0 × 5 = 0.0 — still zero.

        So the cleanest approach is to supply explicit ranking_scores where the
        boosted document starts just below the top hit; a large-enough weight
        then unambiguously promotes it.
        """
        rule = BoostRule(
            field="category",
            operator="eq",
            field_value="footwear",
            boost_weight=Decimal("10.0"),
            index_name="products",
            query_pattern="shoes",
        )
        hits = [
            {"id": "1", "name": "Hat", "category": "accessories"},
            {"id": "2", "name": "Bag", "category": "accessories"},
            {"id": "3", "name": "Trainer", "category": "footwear"},
        ]
        # Give id=3 a score that, after ×10, exceeds the others.
        result = _make_result(hits=hits, ranking_scores=[0.9, 0.8, 0.2])
        out = apply_boosts(result, [rule])
        # id=3: 0.2 × 10 = 2.0 > 0.9 and 0.8 → should be first.
        assert out.hits[0]["id"] == "3"

    def test_demoted_document_moves_toward_end(self):
        """Weight < 1 demotes matching documents."""
        rule = BoostRule(
            field="category",
            operator="eq",
            field_value="footwear",
            boost_weight=Decimal("0.1"),
            index_name="products",
            query_pattern="shoes",
        )
        hits = [
            {"id": "1", "name": "Trainer", "category": "footwear"},
            {"id": "2", "name": "Hat", "category": "accessories"},
            {"id": "3", "name": "Bag", "category": "accessories"},
        ]
        result = _make_result(hits=hits)
        out = apply_boosts(result, [rule])
        assert out.hits[-1]["id"] == "1"

    def test_estimated_total_hits_unchanged(self):
        rule = BoostRule(
            field="category",
            operator="eq",
            field_value="footwear",
            boost_weight=Decimal("2.0"),
            index_name="products",
            query_pattern="shoes",
        )
        result = _make_result()
        out = apply_boosts(result, [rule])
        assert out.estimated_total_hits == result.estimated_total_hits

    def test_all_hits_preserved_after_boost(self):
        rule = BoostRule(
            field="category",
            operator="eq",
            field_value="footwear",
            boost_weight=Decimal("2.0"),
            index_name="products",
            query_pattern="shoes",
        )
        result = _make_result()
        out = apply_boosts(result, [rule])
        assert len(out.hits) == len(result.hits)


# ---------------------------------------------------------------------------
# apply_boosts() — ranking scores
# ---------------------------------------------------------------------------


class TestApplyBoostsRankingScores:
    """apply_boosts() — ranking score handling."""

    def test_position_based_fallback_when_no_ranking_scores(self):
        """With no ranking_scores provided, position-based defaults are used."""
        rule = BoostRule(
            field="category",
            operator="eq",
            field_value="footwear",
            boost_weight=Decimal("5.0"),
            index_name="products",
            query_pattern="shoes",
        )
        hits = [
            {"id": "1", "category": "accessories"},
            {"id": "2", "category": "footwear"},
        ]
        result = _make_result(hits=hits, ranking_scores=[])
        out = apply_boosts(result, [rule])
        # Footwear document (id=2) should come first after boost.
        assert out.hits[0]["id"] == "2"

    def test_existing_ranking_scores_used_as_base(self):
        """Boosts multiply into existing ranking_scores, not position-defaults."""
        rule = BoostRule(
            field="category",
            operator="eq",
            field_value="footwear",
            boost_weight=Decimal("2.0"),
            index_name="products",
            query_pattern="shoes",
        )
        # id=2 has a low engine score but is footwear; after 2x boost it should beat id=1.
        hits = [
            {"id": "1", "category": "accessories"},
            {"id": "2", "category": "footwear"},
        ]
        result = _make_result(hits=hits, ranking_scores=[0.9, 0.4])
        out = apply_boosts(result, [rule])
        # 0.4 * 2.0 = 0.8 < 0.9, so id=1 stays first.
        assert out.hits[0]["id"] == "1"

    def test_re_sorted_scores_match_hit_order(self):
        """ranking_scores in the output must correspond to the re-sorted hit order."""
        rule = BoostRule(
            field="category",
            operator="eq",
            field_value="footwear",
            boost_weight=Decimal("10.0"),
            index_name="products",
            query_pattern="shoes",
        )
        hits = [
            {"id": "1", "category": "accessories"},
            {"id": "2", "category": "footwear"},
        ]
        result = _make_result(hits=hits, ranking_scores=[0.8, 0.1])
        out = apply_boosts(result, [rule])
        # After boost: id=2 score = 0.1 * 10 = 1.0 > 0.8 → id=2 first.
        assert out.hits[0]["id"] == "2"
        assert out.ranking_scores[0] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# apply_boosts() — multiple rules, multiplicative
# ---------------------------------------------------------------------------


class TestApplyBoostsMultipleRules:
    """apply_boosts() — multiple rules are applied multiplicatively."""

    def test_two_matching_rules_multiply_weights(self):
        """Two rules both matching a document multiply their weights."""
        rules = [
            BoostRule(
                field="category",
                operator="eq",
                field_value="footwear",
                boost_weight=Decimal("2.0"),
                index_name="products",
                query_pattern="shoes",
            ),
            BoostRule(
                field="in_stock",
                operator="eq",
                field_value="true",
                boost_weight=Decimal("2.0"),
                index_name="products",
                query_pattern="shoes",
            ),
        ]
        hits = [
            {"id": "1", "category": "footwear", "in_stock": "true"},
            {"id": "2", "category": "accessories", "in_stock": "false"},
        ]
        result = _make_result(hits=hits, ranking_scores=[0.5, 0.9])
        out = apply_boosts(result, rules)
        # id=1: 0.5 * 2.0 * 2.0 = 2.0 > 0.9 → id=1 comes first.
        assert out.hits[0]["id"] == "1"


# ---------------------------------------------------------------------------
# _evaluate_operator() — unit tests for each operator
# ---------------------------------------------------------------------------


class TestEvaluateOperatorEq:
    """_evaluate_operator() — eq operator."""

    def test_eq_string_match(self):
        assert _evaluate_operator({"cat": "footwear"}, "cat", "eq", "footwear") is True

    def test_eq_string_no_match(self):
        assert _evaluate_operator({"cat": "accessories"}, "cat", "eq", "footwear") is False

    def test_eq_numeric_match(self):
        assert _evaluate_operator({"price": 50}, "price", "eq", "50") is True

    def test_eq_numeric_no_match(self):
        assert _evaluate_operator({"price": 40}, "price", "eq", "50") is False

    def test_eq_case_insensitive(self):
        assert _evaluate_operator({"cat": "Footwear"}, "cat", "eq", "footwear") is True


class TestEvaluateOperatorNeq:
    """_evaluate_operator() — neq operator."""

    def test_neq_different_values(self):
        assert _evaluate_operator({"cat": "accessories"}, "cat", "neq", "footwear") is True

    def test_neq_same_values(self):
        assert _evaluate_operator({"cat": "footwear"}, "cat", "neq", "footwear") is False


class TestEvaluateOperatorNumericComparisons:
    """_evaluate_operator() — gt, gte, lt, lte with numeric values."""

    def test_gt_true(self):
        assert _evaluate_operator({"price": 60}, "price", "gt", "50") is True

    def test_gt_false(self):
        assert _evaluate_operator({"price": 40}, "price", "gt", "50") is False

    def test_gte_equal(self):
        assert _evaluate_operator({"price": 50}, "price", "gte", "50") is True

    def test_gte_greater(self):
        assert _evaluate_operator({"price": 51}, "price", "gte", "50") is True

    def test_gte_less(self):
        assert _evaluate_operator({"price": 49}, "price", "gte", "50") is False

    def test_lt_true(self):
        assert _evaluate_operator({"price": 30}, "price", "lt", "50") is True

    def test_lt_false(self):
        assert _evaluate_operator({"price": 70}, "price", "lt", "50") is False

    def test_lte_equal(self):
        assert _evaluate_operator({"price": 50}, "price", "lte", "50") is True

    def test_lte_less(self):
        assert _evaluate_operator({"price": 49}, "price", "lte", "50") is True

    def test_lte_greater(self):
        assert _evaluate_operator({"price": 51}, "price", "lte", "50") is False


class TestEvaluateOperatorContains:
    """_evaluate_operator() — contains operator (string only)."""

    def test_contains_substring_present(self):
        assert _evaluate_operator({"name": "running shoes"}, "name", "contains", "shoe") is True

    def test_contains_substring_absent(self):
        assert _evaluate_operator({"name": "hat"}, "name", "contains", "shoe") is False

    def test_contains_case_insensitive(self):
        assert _evaluate_operator({"name": "Running Shoes"}, "name", "contains", "shoes") is True


class TestEvaluateOperatorExists:
    """_evaluate_operator() — exists operator."""

    def test_exists_field_present(self):
        assert _evaluate_operator({"badge": "sale"}, "badge", "exists", "") is True

    def test_exists_field_absent(self):
        assert _evaluate_operator({"name": "Trainer"}, "badge", "exists", "") is False

    def test_exists_field_with_none_value(self):
        assert _evaluate_operator({"badge": None}, "badge", "exists", "") is False


class TestEvaluateOperatorMissingField:
    """_evaluate_operator() — field absent from hit."""

    def test_missing_field_eq_returns_false(self):
        assert _evaluate_operator({}, "missing", "eq", "x") is False

    def test_missing_field_neq_returns_false(self):
        assert _evaluate_operator({}, "missing", "neq", "x") is False

    def test_missing_field_gt_returns_false(self):
        assert _evaluate_operator({}, "missing", "gt", "1") is False


class TestEvaluateOperatorNumericFallthrough:
    """_evaluate_operator() logs when numeric comparison falls through to string."""

    def test_eq_fallthrough_logs_debug(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="icv_search.services.boosts"):
            result = _evaluate_operator({"sku": "ABC"}, "sku", "eq", "ABC")
        assert result is True
        records = [r for r in caplog.records if "Boost numeric comparison failed" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.DEBUG
        assert "eq" in records[0].message

    def test_gt_fallthrough_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="icv_search.services.boosts"):
            result = _evaluate_operator({"sku": "ABC"}, "sku", "gt", "10")
        # String comparison still runs; "abc" > "10" is True lexicographically.
        assert result is True
        records = [r for r in caplog.records if "Boost numeric comparison failed" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert "gt" in records[0].message

    def test_gte_fallthrough_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="icv_search.services.boosts"):
            _evaluate_operator({"tag": "premium"}, "tag", "gte", "1")
        assert any(
            r.levelno == logging.WARNING and "gte" in r.message
            for r in caplog.records
            if "Boost numeric comparison failed" in r.message
        )

    def test_eq_fallthrough_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="icv_search.services.boosts"):
            _evaluate_operator({"sku": "ABC"}, "sku", "eq", "ABC")
        assert not any("Boost numeric comparison failed" in r.message for r in caplog.records)

