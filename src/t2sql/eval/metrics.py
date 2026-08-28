"""execution_accuracy: run predicted and gold SQL, compare result sets --
never SQL strings, since semantically identical queries can look nothing
alike.

Comparison is order-insensitive unless the gold query has a top-level
ORDER BY, and tolerant of float rounding. A prediction is correct if it
matches *any* of a question's gold interpretations -- gold_sql is a list
because an ambiguous question can have several equally-valid readings.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg
import sqlglot
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
