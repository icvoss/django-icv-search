"""Boost rule service functions."""

from __future__ import annotations

import logging
from typing import Any

from icv_search.types import SearchResult

logger = logging.getLogger(__name__)


def get_boost_rules_for_query(
    index_name: str,
    query: str,
    tenant_id: str = "",
) -> list:
    """Return all active BoostRule instances matching the query.

    Args:
        index_name: Logical search index name to scope the rule lookup.
        query: The user's search query string.
        tenant_id: Optional tenant identifier. Blank matches global rules only.

    Returns:
        List of matching :class:`~icv_search.models.merchandising.BoostRule`
        instances in priority order (highest priority first).
    """
    from icv_search.merchandising_cache import get_matching_rules
    from icv_search.models.merchandising import BoostRule

    return get_matching_rules(
        BoostRule,
        "BoostRule",
        index_name,
        query,
        tenant_id,
        single_winner=False,
    )


def apply_boosts(result: SearchResult, rules: list) -> SearchResult:
    """Apply boost rules to a SearchResult, adjusting scores and re-sorting.

    Each matching rule's ``boost_weight`` is multiplied into the document's
    ranking score. Documents without a ranking score receive a position-based
    default score (``1.0 - position/total``) so that boost weights can still
    take effect even when the backend does not return ranking scores.

    After all rules have been applied, hits are re-sorted by their adjusted
    score descending so that boosted documents rise and demoted documents fall.

    Args:
        result: The :class:`~icv_search.types.SearchResult` to transform.
        rules: List of :class:`~icv_search.models.merchandising.BoostRule`
            instances to apply (order does not matter; all are applied).

    Returns:
        A new :class:`~icv_search.types.SearchResult` with boosts applied and
        hits re-sorted.
    """
    if not rules or not result.hits:
        return result

    hits = list(result.hits)
    total = len(hits)

    # Build a mutable scores list, filling in position-based defaults where
    # the backend did not return ranking scores.
    scores: list[float] = []
    for i, _ in enumerate(hits):
        if i < len(result.ranking_scores) and result.ranking_scores[i] is not None:
            scores.append(float(result.ranking_scores[i]))  # type: ignore[arg-type]
        else:
            # Position-based fallback: first hit ≈ 1.0, last hit ≈ 0.0.
            scores.append(1.0 - (i / max(total, 1)))

    # Apply each rule to each hit, multiplying the weight in when the condition
    # is satisfied.
    for rule in rules:
        for i, hit in enumerate(hits):
            if _evaluate_operator(hit, rule.field, rule.operator, rule.field_value):
                scores[i] *= float(rule.boost_weight)

    # Re-sort hits by adjusted score descending.
    paired = list(zip(hits, scores, strict=True))
    paired.sort(key=lambda x: x[1], reverse=True)
    sorted_hits = [p[0] for p in paired]
    sorted_scores: list[float | None] = [p[1] for p in paired]

    return SearchResult(
        hits=sorted_hits,
        query=result.query,
        processing_time_ms=result.processing_time_ms,
        estimated_total_hits=result.estimated_total_hits,
        limit=result.limit,
        offset=result.offset,
        facet_distribution=result.facet_distribution,
        formatted_hits=result.formatted_hits,
        ranking_scores=sorted_scores,
        raw=result.raw,
    )


def _evaluate_operator(hit: dict[str, Any], field: str, operator: str, value: str) -> bool:
    """Evaluate whether a hit's field satisfies the boost rule's condition.

    Numeric comparison is attempted first (both sides cast to ``float``); if
    either side is non-numeric the comparison falls back to case-insensitive
    string comparison.

    Args:
        hit: A single search result document dict.
        field: The document field name to evaluate.
        operator: One of ``eq``, ``neq``, ``gt``, ``gte``, ``lt``, ``lte``,
            ``contains``, or ``exists``.
        value: The value to compare against (ignored for ``exists``).

    Returns:
        ``True`` when the condition is satisfied, ``False`` otherwise.
    """
    field_val = hit.get(field)

    if operator == "exists":
        return field_val is not None

    if field_val is None:
        return False

    # Attempt numeric comparison first.
    try:
        num_field = float(field_val)
        num_value = float(value)
        if operator == "eq":
            return num_field == num_value
        if operator == "neq":
            return num_field != num_value
        if operator == "gt":
            return num_field > num_value
        if operator == "gte":
            return num_field >= num_value
        if operator == "lt":
            return num_field < num_value
        if operator == "lte":
            return num_field <= num_value
    except (TypeError, ValueError):
        # Numeric cast failed; fall through to string comparison. Relational
        # operators get WARNING so a misconfigured numeric rule is visible;
        # eq/neq fallthrough is DEBUG to avoid log storms on mixed-type fields.
        if operator in {"gt", "gte", "lt", "lte"}:
            logger.warning(
                "Boost numeric comparison failed for field '%s' operator '%s' "
                "(field_value=%r, rule_value=%r); falling back to string comparison.",
                field,
                operator,
                field_val,
                value,
                exc_info=True,
            )
        else:
            logger.debug(
                "Boost numeric comparison failed for field '%s' operator '%s' "
                "(field_value=%r, rule_value=%r); falling back to string comparison.",
                field,
                operator,
                field_val,
                value,
                exc_info=True,
            )

    # String comparison fallback.
    str_field = str(field_val).lower()
    str_value = str(value).lower()

    if operator == "eq":
        return str_field == str_value
    if operator == "neq":
        return str_field != str_value
    if operator == "contains":
        return str_value in str_field
    if operator == "gt":
        return str_field > str_value
    if operator == "gte":
        return str_field >= str_value
    if operator == "lt":
        return str_field < str_value
    if operator == "lte":
        return str_field <= str_value

    return False
