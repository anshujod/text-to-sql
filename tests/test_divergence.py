"""Task 3.4 [GATE]: result divergence test.

No LLM calls anywhere in this file -- `compute_divergence_report` takes SQL
directly, and the dev-set validation below reuses Task 2.3's hand-verified
gold_sql as the "K candidate interpretations," executed against the local
Postgres. Runs on every `make test`.
"""

from pathlib import Path

from t2sql.clarify.divergence import (
    ResultKind,
    _is_identifier_column,
    _rank_overlap,
    classify_result,
    compute_divergence_report,
)
from t2sql.eval.dataset import load_dataset
from t2sql.execution.models import ResultSet

DEV_DATASET_PATH = Path(__file__).resolve().parents[1] / "data" / "dev.jsonl"


# ---------------------------------------------------------------------------
# classify_result
# ---------------------------------------------------------------------------


def test_classifies_scalar() -> None:
    rs = ResultSet(columns=["count"], rows=[[5]])
    assert classify_result(rs, ["int8"]) == ResultKind.SCALAR


def test_classifies_multi_value() -> None:
    rs = ResultSet(columns=["dec", "nov", "pct_change"], rows=[[573, 903, -36.5]])
    assert classify_result(rs, ["int8", "int8", "numeric"]) == ResultKind.MULTI_VALUE


def test_classifies_ranked_list() -> None:
    rs = ResultSet(columns=["customer_id", "revenue"], rows=[[1, 100], [2, 90], [3, 80]])
    assert classify_result(rs, ["int8", "numeric"]) == ResultKind.RANKED_LIST


def test_classifies_time_series() -> None:
    rs = ResultSet(columns=["month", "revenue"], rows=[["2025-01", 100], ["2025-02", 110]])
    assert classify_result(rs, ["timestamptz", "numeric"]) == ResultKind.TIME_SERIES


def test_classifies_empty() -> None:
    rs = ResultSet(columns=["count"], rows=[])
    assert classify_result(rs, ["int8"]) == ResultKind.EMPTY


# ---------------------------------------------------------------------------
# identifier-column detection (the bug this fixed: comparing product id 224
# vs 230 as if "2.6% different" instead of "a different product")
# ---------------------------------------------------------------------------


def test_identifier_columns_detected_by_name() -> None:
    assert _is_identifier_column("id")
    assert _is_identifier_column("customer_id")
    assert _is_identifier_column("Product_ID")
    assert not _is_identifier_column("revenue")
    assert not _is_identifier_column("total_quantity")


# ---------------------------------------------------------------------------
# _rank_overlap: overlap coefficient, not Jaccard -- see divergence.py's
# docstring on _rank_overlap for why (a top-5-of-top-10 prefix must score
# near-identical, not "50% different")
# ---------------------------------------------------------------------------


def test_rank_overlap_identical_lists_is_one() -> None:
    assert _rank_overlap([1, 2, 3], [1, 2, 3]) == 1.0


def test_rank_overlap_exact_prefix_is_one() -> None:
    """The canonical RESULT_SHAPE near-miss shape: top-5 is a strict
    prefix of top-10 by the same ranking."""
    assert _rank_overlap([1, 2, 3, 4, 5], list(range(1, 11))) == 1.0


def test_rank_overlap_disjoint_lists_is_zero() -> None:
    assert _rank_overlap([1, 2, 3], [4, 5, 6]) == 0.0


def test_rank_overlap_partial_same_size() -> None:
    assert _rank_overlap([1, 2, 3, 4], [3, 4, 5, 6]) == 0.5


# ---------------------------------------------------------------------------
# compute_divergence_report, against the real DB (no LLM)
# ---------------------------------------------------------------------------


def test_identical_queries_score_zero() -> None:
    report = compute_divergence_report(
        [("a", "SELECT COUNT(*) FROM customers"), ("b", "SELECT COUNT(*) FROM customers")]
    )
    assert report.score == 0.0


def test_genuinely_different_scalars_score_high() -> None:
    """Gross vs net revenue -- differ by several percent on the seed data."""
    report = compute_divergence_report(
        [
            ("gross", "SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='succeeded' AND currency='USD'"),
            (
                "net",
                "SELECT COALESCE((SELECT SUM(amount) FROM payments WHERE status='succeeded' AND currency='USD'),0) "
                "- COALESCE((SELECT SUM(amount) FROM refunds WHERE currency='USD'),0)",
            ),
        ]
    )
    assert 0.0 < report.score < 1.0
    assert report.result_kind_per_interpretation["gross"] == ResultKind.SCALAR


def test_different_winner_scores_maximally_diverged() -> None:
    """Two LIMIT-1 'winner' queries picking a different row -- id-column
    equality must dominate, not numeric closeness of the ids.
    """
    report = compute_divergence_report(
        [
            ("by_revenue", "SELECT id, price FROM products WHERE deleted_at IS NULL ORDER BY price DESC LIMIT 1"),
            ("by_id", "SELECT id, price FROM products WHERE deleted_at IS NULL ORDER BY id ASC LIMIT 1"),
        ]
    )
    assert report.score == 1.0


def test_ranked_list_prefix_scores_low() -> None:
    report = compute_divergence_report(
        [
            ("top_5", "SELECT id FROM customers ORDER BY id LIMIT 5"),
            ("top_10", "SELECT id FROM customers ORDER BY id LIMIT 10"),
        ]
    )
    assert report.score == 0.0


def test_k_is_capped_at_max_k() -> None:
    interps = [(f"i{i}", "SELECT 1") for i in range(6)]
    report = compute_divergence_report(interps, max_k=4)
    assert len(report.labels) == 4


def test_failed_query_scores_maximal_divergence() -> None:
    report = compute_divergence_report(
        [("ok", "SELECT COUNT(*) FROM customers"), ("broken", "SELECT * FROM not_a_real_table")]
    )
    assert report.score == 1.0
    assert "broken" in report.errors


# ---------------------------------------------------------------------------
# The actual PLAN.md 3.4 gate: score correlates with expected_divergence
# ---------------------------------------------------------------------------


def test_dev_divergence_score_separates_near_miss_from_high() -> None:
    """Reuses Task 2.3's hand-verified gold_sql per item as the K candidate
    interpretations -- no LLM needed, the interpretations already exist.
    """
    items = [item for item in load_dataset(DEV_DATASET_PATH) if item.is_ambiguous]
    assert items, "no dev ambiguous items found"

    low_scores = []
    high_scores = []
    for item in items:
        interpretations = [(g.label or g.interpretation, g.sql) for g in item.gold_sql]
        report = compute_divergence_report(interpretations)
        (low_scores if item.expected_divergence == "low" else high_scores).append(report.score)

    assert low_scores and high_scores

    threshold = 0.10
    low_correct = sum(1 for s in low_scores if s <= threshold)
    high_correct = sum(1 for s in high_scores if s > threshold)

    low_rate = low_correct / len(low_scores)
    high_rate = high_correct / len(high_scores)

    # PLAN.md 3.4: "the ~15 near-miss items score low and the high-divergence
    # items score high" -- not 100% (some items are borderline by design,
    # e.g. COMPARISON near-misses that agree in sign but differ in
    # magnitude), but the large majority must separate cleanly.
    assert low_rate >= 0.8, f"only {low_correct}/{len(low_scores)} near-miss items scored <= {threshold}"
    assert high_rate >= 0.8, f"only {high_correct}/{len(high_scores)} high-divergence items scored > {threshold}"

    mean_low = sum(low_scores) / len(low_scores)
    mean_high = sum(high_scores) / len(high_scores)
    assert mean_high > mean_low
