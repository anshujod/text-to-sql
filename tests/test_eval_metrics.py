import pytest

from t2sql.eval.dataset import GoldInterpretation
from t2sql.eval.metrics import (
    EvalRecord,
    bootstrap_ci,
    detection_precision_recall_f1,
    end_to_end_correctness,
    execution_accuracy,
    over_ask_rate,
    silent_error_rate,
    unnecessary_ask_rate,
)


def test_exact_match_is_correct() -> None:
    gold = [GoldInterpretation(sql="SELECT COUNT(*) FROM categories")]
    assert execution_accuracy("SELECT COUNT(*) FROM categories", gold) is True


def test_semantically_equivalent_but_textually_different_sql_is_correct() -> None:
    gold = [GoldInterpretation(sql="SELECT COUNT(*) FROM categories")]
    assert execution_accuracy("SELECT COUNT(id) AS n FROM categories", gold) is True


def test_wrong_result_is_incorrect() -> None:
    gold = [GoldInterpretation(sql="SELECT COUNT(*) FROM categories")]
    assert execution_accuracy("SELECT COUNT(*) + 1 FROM categories", gold) is False


def test_order_insensitive_when_gold_has_no_order_by() -> None:
    gold = [GoldInterpretation(sql="SELECT id FROM categories WHERE id <= 3")]
    pred = "SELECT id FROM categories WHERE id <= 3 ORDER BY id DESC"
    assert execution_accuracy(pred, gold) is True


def test_order_sensitive_when_gold_has_order_by() -> None:
    gold = [GoldInterpretation(sql="SELECT id FROM categories WHERE id <= 3 ORDER BY id ASC")]
    pred_reversed = "SELECT id FROM categories WHERE id <= 3 ORDER BY id DESC"
    assert execution_accuracy(pred_reversed, gold) is False


def test_float_tolerance() -> None:
    gold = [GoldInterpretation(sql="SELECT 100.0000001::float AS x")]
    assert execution_accuracy("SELECT 100.0000002::float AS x", gold) is True


def test_float_tolerance_does_not_mask_real_differences() -> None:
    gold = [GoldInterpretation(sql="SELECT 100.0::float AS x")]
    assert execution_accuracy("SELECT 101.0::float AS x", gold) is False


def test_matches_any_of_multiple_gold_interpretations() -> None:
    gold = [GoldInterpretation(sql="SELECT 1 AS x"), GoldInterpretation(sql="SELECT 2 AS x")]
    assert execution_accuracy("SELECT 2 AS x", gold) is True
    assert execution_accuracy("SELECT 3 AS x", gold) is False


def test_invalid_pred_sql_is_incorrect_not_raising() -> None:
    gold = [GoldInterpretation(sql="SELECT COUNT(*) FROM categories")]
    assert execution_accuracy("SELECT COUNT(*) FROM not_a_real_table", gold) is False


def test_different_row_counts_is_incorrect() -> None:
    gold = [GoldInterpretation(sql="SELECT id FROM categories WHERE id <= 3")]
    pred = "SELECT id FROM categories WHERE id <= 2"
    assert execution_accuracy(pred, gold) is False


# ---------------------------------------------------------------------------
# Run-level metrics, all against hand-constructed EvalRecord lists
# ---------------------------------------------------------------------------


def test_detection_precision_recall_f1_known_answer() -> None:
    records = [
        EvalRecord(id="1", is_ambiguous=True, detected_ambiguous=True),  # TP
        EvalRecord(id="2", is_ambiguous=True, detected_ambiguous=True),  # TP
        EvalRecord(id="3", is_ambiguous=True, detected_ambiguous=False),  # FN
        EvalRecord(id="4", is_ambiguous=False, detected_ambiguous=True),  # FP
        EvalRecord(id="5", is_ambiguous=False, detected_ambiguous=False),  # TN
    ]
    result = detection_precision_recall_f1(records)
    # TP=2, FP=1, FN=1 -> precision = recall = f1 = 2/3
    assert result["precision"] == pytest.approx(2 / 3)
    assert result["recall"] == pytest.approx(2 / 3)
    assert result["f1"] == pytest.approx(2 / 3)


def test_detection_precision_recall_f1_no_positives_is_zero_not_nan() -> None:
    records = [EvalRecord(id="1", is_ambiguous=False, detected_ambiguous=False)]
    result = detection_precision_recall_f1(records)
    assert result == {"precision": 0.0, "recall": 0.0, "f1": 0.0}


def test_detection_precision_recall_f1_perfect_detector() -> None:
    records = [
        EvalRecord(id="1", is_ambiguous=True, detected_ambiguous=True),
        EvalRecord(id="2", is_ambiguous=False, detected_ambiguous=False),
    ]
    result = detection_precision_recall_f1(records)
    assert result == {"precision": 1.0, "recall": 1.0, "f1": 1.0}


def test_over_ask_rate_counts_asks_over_all_records() -> None:
    records = [
        EvalRecord(id="1", is_ambiguous=True, asked=True),
        EvalRecord(id="2", is_ambiguous=False, asked=False),
        EvalRecord(id="3", is_ambiguous=False, asked=False),
        EvalRecord(id="4", is_ambiguous=False, asked=False),
    ]
    assert over_ask_rate(records) == pytest.approx(0.25)


def test_over_ask_rate_empty_is_zero() -> None:
    assert over_ask_rate([]) == 0.0


def test_unnecessary_ask_rate_only_considers_near_miss_items() -> None:
    records = [
        EvalRecord(id="1", is_ambiguous=True, expected_divergence="low", asked=True),
        EvalRecord(id="2", is_ambiguous=True, expected_divergence="low", asked=False),
        EvalRecord(id="3", is_ambiguous=True, expected_divergence="low", asked=True),
        # not near-miss items -- must not affect the rate even though they were asked
        EvalRecord(id="4", is_ambiguous=True, expected_divergence="high", asked=True),
        EvalRecord(id="5", is_ambiguous=False, expected_divergence=None, asked=True),
    ]
    assert unnecessary_ask_rate(records) == pytest.approx(2 / 3)


def test_unnecessary_ask_rate_no_near_miss_items_is_zero() -> None:
    records = [EvalRecord(id="1", is_ambiguous=True, expected_divergence="high", asked=True)]
    assert unnecessary_ask_rate(records) == 0.0


def test_end_to_end_correctness_known_answer() -> None:
    records = [
        EvalRecord(id="1", is_ambiguous=False, correct=True),
        EvalRecord(id="2", is_ambiguous=False, correct=True),
        EvalRecord(id="3", is_ambiguous=False, correct=True),
        EvalRecord(id="4", is_ambiguous=False, correct=False),
    ]
    assert end_to_end_correctness(records) == pytest.approx(0.75)


def test_silent_error_rate_only_counts_wrong_unasked_undisclosed() -> None:
    records = [
        EvalRecord(id="1", is_ambiguous=False, correct=False, asked=False, disclosed=False),  # silent error
        EvalRecord(id="2", is_ambiguous=False, correct=False, asked=True, disclosed=False),  # asked -- not silent
        EvalRecord(id="3", is_ambiguous=False, correct=False, asked=False, disclosed=True),  # disclosed -- not silent
        EvalRecord(id="4", is_ambiguous=False, correct=True, asked=False, disclosed=False),  # correct -- not an error
    ]
    assert silent_error_rate(records) == pytest.approx(0.25)


def test_silent_error_rate_empty_is_zero() -> None:
    assert silent_error_rate([]) == 0.0


def test_bootstrap_ci_point_estimate_matches_direct_metric_call() -> None:
    records = [
        EvalRecord(id="1", is_ambiguous=True, asked=True),
        EvalRecord(id="2", is_ambiguous=False, asked=False),
        EvalRecord(id="3", is_ambiguous=False, asked=False),
        EvalRecord(id="4", is_ambiguous=False, asked=True),
    ]
    point, lo, hi = bootstrap_ci(records, over_ask_rate, n_bootstrap=500, seed=42)
    assert point == pytest.approx(over_ask_rate(records))
    assert 0.0 <= lo <= point <= hi <= 1.0


def test_bootstrap_ci_constant_metric_has_zero_width_interval() -> None:
    items = [1, 2, 3, 4, 5]
    point, lo, hi = bootstrap_ci(items, lambda xs: 0.5, n_bootstrap=200)
    assert (point, lo, hi) == (0.5, 0.5, 0.5)


def test_bootstrap_ci_empty_items_returns_metric_default_with_no_crash() -> None:
    point, lo, hi = bootstrap_ci([], over_ask_rate)
    assert (point, lo, hi) == (0.0, 0.0, 0.0)
