"""The metrics module.

`execution_accuracy` (below) runs predicted and gold SQL and compares
result sets -- never SQL strings, since semantically identical queries can
look nothing alike. Comparison is order-insensitive unless the gold query
has a top-level ORDER BY, and tolerant of float rounding. A prediction is
correct if it matches *any* of a question's gold interpretations --
gold_sql is a list because an ambiguous question can have several
equally-valid readings.

The rest of this module is the aggregate, per-run metrics this project's
evaluation calls for, all pure functions over a list of `EvalRecord` -- no
DB, no LLM, $0. Each one is deliberately a plain function of a plain list
so a unit test can hand-construct a handful of records with a known
answer.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Literal, Sequence

import psycopg
import random
import sqlglot
from pydantic import BaseModel
from sqlglot import exp

from t2sql.eval.dataset import GoldInterpretation
from t2sql.execution.executor import execute
from t2sql.execution.models import ResultSet

FLOAT_DECIMALS = 6


def _has_top_level_order_by(sql: str, dialect: str = "postgres") -> bool:
    try:
        root = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return False
    return isinstance(root, exp.Select) and root.args.get("order") is not None


def _canonical_cell(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, Decimal)):
        return round(float(value), FLOAT_DECIMALS)
    return str(value)


def _canonical_row(row: list[Any]) -> tuple:
    return tuple(_canonical_cell(v) for v in row)


def _result_sets_match(pred: ResultSet, gold: ResultSet, order_sensitive: bool) -> bool:
    if len(pred.rows) != len(gold.rows):
        return False
    if len(pred.columns) != len(gold.columns):
        return False

    pred_rows = [_canonical_row(r) for r in pred.rows]
    gold_rows = [_canonical_row(r) for r in gold.rows]

    if order_sensitive:
        return pred_rows == gold_rows
    # sort by repr rather than the tuples themselves: cells can mix None
    # with numbers/strings across rows, which Python can't order directly.
    return sorted(pred_rows, key=repr) == sorted(gold_rows, key=repr)


def execution_accuracy(
    pred_sql: str,
    gold_sql: list[GoldInterpretation],
    dialect: str = "postgres",
    conn: psycopg.Connection | None = None,
) -> bool:
    """True if executing `pred_sql` matches the result of any gold interpretation."""
    pred_result = execute(pred_sql, conn=conn)
    if not pred_result.ok or pred_result.result_set is None:
        return False

    for gold in gold_sql:
        gold_result = execute(gold.sql, conn=conn)
        if not gold_result.ok or gold_result.result_set is None:
            continue  # a broken gold query can't be matched against
        order_sensitive = _has_top_level_order_by(gold.sql, dialect=dialect)
        if _result_sets_match(pred_result.result_set, gold_result.result_set, order_sensitive):
            return True

    return False


# ---------------------------------------------------------------------------
# Run-level metrics
# ---------------------------------------------------------------------------


class EvalRecord(BaseModel):
    """One dataset item's outcome from a full (clarify-aware) eval run --
    the unit these aggregate metrics are computed over. The ablation runner
    is the intended producer of a list of these; unit tests below
    hand-construct lists directly.
    """

    id: str
    is_ambiguous: bool  # gold label
    expected_divergence: Literal["high", "low"] | None = None  # gold label, near-miss items are "low"
    detected_ambiguous: bool = False  # system's raw detection signal, pre-policy
    asked: bool = False  # system actually surfaced a clarification question to the user
    disclosed: bool = False  # system flagged uncertainty without asking (e.g. a caveat on the answer)
    correct: bool = False  # final SQL matched gold for the resolved/true interpretation


def detection_precision_recall_f1(records: Sequence[EvalRecord]) -> dict[str, float]:
    """Precision/recall/F1 of `detected_ambiguous` against the gold `is_ambiguous` label."""
    tp = sum(1 for r in records if r.is_ambiguous and r.detected_ambiguous)
    fp = sum(1 for r in records if not r.is_ambiguous and r.detected_ambiguous)
    fn = sum(1 for r in records if r.is_ambiguous and not r.detected_ambiguous)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def over_ask_rate(records: Sequence[EvalRecord]) -> float:
    """Fraction of *all* queries -- ambiguous or not -- where the system
    asked. Worth reporting prominently, since it's the metric most systems
    hide (a system that always asks has perfect "precision" on nothing).
    """
    if not records:
        return 0.0
    return sum(1 for r in records if r.asked) / len(records)


def unnecessary_ask_rate(records: Sequence[EvalRecord]) -> float:
    """Of the items whose interpretations actually converge despite looking
    ambiguous (`expected_divergence == "low"`, the dataset's near-miss
    items), what fraction did the system ask about anyway. Denominator is fixed to
    that known near-miss subset rather than "all asks" so the rate stays
    well-defined even when the system never asks, and measures exactly the
    failure mode the near-miss items were built to catch: triggering on
    superficial ambiguity that doesn't change the answer.
    """
    near_misses = [r for r in records if r.expected_divergence == "low"]
    if not near_misses:
        return 0.0
    return sum(1 for r in near_misses if r.asked) / len(near_misses)


def end_to_end_correctness(records: Sequence[EvalRecord]) -> float:
    """Fraction of items where the final SQL matched gold for the user's true intent."""
    if not records:
        return 0.0
    return sum(1 for r in records if r.correct) / len(records)


def silent_error_rate(records: Sequence[EvalRecord]) -> float:
    """Fraction of *all* items the system got wrong while neither asking nor
    disclosing uncertainty -- confidently wrong with no warning. The
    headline number this project exists to reduce.
    """
    if not records:
        return 0.0
    return sum(1 for r in records if not r.correct and not r.asked and not r.disclosed) / len(records)


def bootstrap_ci(
    items: Sequence[Any],
    metric_fn: Callable[[Sequence[Any]], float],
    n_bootstrap: int = 1000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Percentile bootstrap: resample `items` with replacement `n_bootstrap`
    times, recompute `metric_fn` on each resample, and return
    (point_estimate, ci_low, ci_high) at confidence `1 - alpha`. Generic
    over any of the rate functions above -- with n=100 dev items the
    error bars matter.
    """
    point = metric_fn(items)
    n = len(items)
    if n == 0:
        return point, point, point
    rng = random.Random(seed)
    samples = sorted(metric_fn([items[rng.randrange(n)] for _ in range(n)]) for _ in range(n_bootstrap))
    lo_idx = int((alpha / 2) * n_bootstrap)
    hi_idx = min(int((1 - alpha / 2) * n_bootstrap), n_bootstrap - 1)
    return point, samples[lo_idx], samples[hi_idx]
