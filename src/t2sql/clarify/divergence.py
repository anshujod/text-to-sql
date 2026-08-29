"""Result divergence test (Task 3.4) [GATE].

The core idea the whole project rests on: do not ask unless the answer
would actually change. Given K candidate interpretations of a question
(each a piece of SQL), execute all of them and compare the *results* --
not the SQL text, not a static signature -- with a similarity measure
matched to the shape of the result:

  scalar        (1 row, 1 column)      -> relative difference
  multi-value   (1 row, 2+ columns)    -> max relative difference across
                                           the columns both results share
  ranked list   (2+ rows, no time key) -> overlap coefficient on the top-N
                                           identifiers (see _rank_overlap)
                                           (first column, by convention)
  time series   (2+ rows, a date/time
                 column present)       -> Pearson correlation of the value
                                           column at matching time keys,
                                           blended with magnitude difference

Divergence is 0.0 for identical results, up to 1.0 for maximally different
ones. `compute_divergence_report`'s overall `score` is the *worst* pairwise
divergence found, matching the "do not ask unless it would change the
answer" framing: one genuinely different pair among K readings is reason
enough to consider asking, an unresolved decision that's identical no
matter the reading is not.

Execution results are cached by a hash of the SQL text (not the question --
two different questions producing the same SQL get the same result either
way, so hashing the SQL is a strict refinement of "cache by
(question_hash, interpretation)": more cache hits, same correctness, since
the SQL text alone fully determines the result on a static database).

Cost guard: `max_k` truncates to PLAN.md's K=4 cap. Real (non-gold) callers
generating candidate SQL via an LLM are expected to check their own budget
*before* calling this -- see Task 3.5's policy engine -- so this module
stays a pure "given K queries, how much do they disagree" function with no
LLM dependency of its own.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any

import numpy as np
import psycopg
from pydantic import BaseModel, Field

from t2sql.execution.executor import execute
from t2sql.execution.models import ExecutionResult, ResultSet

DEFAULT_MAX_K = 4
DEFAULT_TOP_N = 10
DEFAULT_TIMEOUT_SECONDS = 5.0
_DATE_TYPE_NAMES = {"date", "timestamp", "timestamptz", "timestamp without time zone", "timestamp with time zone"}

_execution_cache: dict[str, ExecutionResult] = {}


def _cache_key(sql: str) -> str:
    return hashlib.sha256(sql.strip().encode()).hexdigest()


def clear_execution_cache() -> None:
    _execution_cache.clear()


class ResultKind(str, Enum):
    EMPTY = "empty"
    ERROR = "error"
    SCALAR = "scalar"
    MULTI_VALUE = "multi_value"
    TIME_SERIES = "time_series"
    RANKED_LIST = "ranked_list"


class DivergenceReport(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    labels: list[str]
    pairwise_matrix: list[list[float]]
    result_kind_per_interpretation: dict[str, ResultKind]
    sample_rows_per_interpretation: dict[str, list[list[Any]]]
    columns_per_interpretation: dict[str, list[str]] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)


def classify_result(result_set: ResultSet | None, column_types: list[str]) -> ResultKind:
    if result_set is None:
        return ResultKind.ERROR
    if not result_set.rows:
        return ResultKind.EMPTY
    n_rows, n_cols = len(result_set.rows), len(result_set.columns)
    if n_rows == 1 and n_cols == 1:
        return ResultKind.SCALAR
    if n_rows == 1:
        return ResultKind.MULTI_VALUE
    if column_types and any(t in _DATE_TYPE_NAMES for t in column_types):
        return ResultKind.TIME_SERIES
    return ResultKind.RANKED_LIST


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _relative_diff(a: float, b: float) -> float:
    denom = max(abs(a), abs(b))
    if denom == 0:
        return 0.0
    return min(abs(a - b) / denom, 1.0)


def _scalar_divergence(a: ResultSet, b: ResultSet) -> float:
    va, vb = _to_float(a.rows[0][0]), _to_float(b.rows[0][0])
    if va is None or vb is None:
        return 0.0 if a.rows[0][0] == b.rows[0][0] else 1.0
    return _relative_diff(va, vb)


def _is_identifier_column(name: str) -> bool:
    """A surrogate key ("id", "customer_id", ...) is a *label*, not a
    quantity -- product 224 vs 230 is not "2.6% different," it's a
    different product. Compared by equality, never by numeric distance.
    """
    name = name.lower()
    return name == "id" or name.endswith("_id")


def _multi_value_divergence(a: ResultSet, b: ResultSet) -> float:
    common_cols = [c for c in a.columns if c in b.columns]
    diffs: list[float] = []
    for col in common_cols:
        va, vb = a.rows[0][a.columns.index(col)], b.rows[0][b.columns.index(col)]
        if _is_identifier_column(col):
            diffs.append(0.0 if va == vb else 1.0)
            continue
        fa, fb = _to_float(va), _to_float(vb)
        if fa is not None and fb is not None:
            diffs.append(_relative_diff(fa, fb))
    return max(diffs) if diffs else 1.0  # nothing comparable between the two shapes -- treat as fully diverged


def _rank_overlap(a: list, b: list) -> float:
    """Overlap coefficient (Szymkiewicz-Simpson): |A n B| / min(|A|, |B|).

    PLAN.md 3.4 names rank-biased overlap as the preferred measure for
    ranked lists specifically because plain Jaccard mishandles a
    different-length pair that's otherwise a prefix match: top-5 vs.
    top-10 of the identical ranking gives Jaccard = 5/10 = 0.5 ("50%
    different"), treating 5 extra low-ranked rows as just as significant
    as a completely different top-N. The overlap coefficient divides by
    the *smaller* set instead of the union, so an exact prefix match
    (RESULT_SHAPE's canonical near-miss shape) scores 1.0, same as an
    exact match -- while two same-length, half-overlapping lists still
    score 0.5, same as Jaccard would. A full positionally-weighted RBO
    was tried first and dropped: the textbook extrapolated formula is
    easy to get subtly wrong (an earlier version of this function scored
    identical lists above 1.0), and this simpler measure gives the exact
    property this dataset's near-misses need without that risk.
    """
    set_a, set_b = set(a), set(b)
    if not set_a and not set_b:
        return 1.0
    denom = min(len(set_a), len(set_b))
    if denom == 0:
        return 0.0
    return len(set_a & set_b) / denom


def _ranked_list_divergence(a: ResultSet, b: ResultSet, top_n: int) -> float:
    a_ids = [row[0] for row in a.rows[:top_n]]
    b_ids = [row[0] for row in b.rows[:top_n]]
    return 1.0 - _rank_overlap(a_ids, b_ids)


def _time_series_divergence(a: ResultSet, b: ResultSet) -> float:
    def series(rs: ResultSet) -> dict[Any, float]:
        val_idx = 1 if len(rs.columns) > 1 else 0
        out = {}
        for row in rs.rows:
            v = _to_float(row[val_idx])
            if v is not None:
                out[row[0]] = v
        return out

    sa, sb = series(a), series(b)
    common_keys = sorted(set(sa) & set(sb), key=str)
    if len(common_keys) < 2:
        return 1.0  # not enough overlap to say anything -- treat as fully diverged

    xa = np.array([sa[k] for k in common_keys])
    xb = np.array([sb[k] for k in common_keys])

    magnitude_a, magnitude_b = float(np.sum(np.abs(xa))), float(np.sum(np.abs(xb)))
    magnitude_divergence = _relative_diff(magnitude_a, magnitude_b)

    if np.std(xa) == 0 or np.std(xb) == 0:
        correlation_divergence = 0.0 if np.array_equal(xa, xb) else 1.0
    else:
        correlation = float(np.corrcoef(xa, xb)[0, 1])
        correlation_divergence = (1.0 - correlation) / 2.0  # correlation in [-1,1] -> divergence in [0,1]

    return max(correlation_divergence, magnitude_divergence)


def _pairwise_divergence(ra: ExecutionResult, rb: ExecutionResult, top_n: int) -> float:
    if not ra.ok or not rb.ok:
        return 1.0
    a, b = ra.result_set, rb.result_set
    if a is None or b is None:
        return 1.0
    if a.fingerprint() == b.fingerprint():
        return 0.0

    kind_a = classify_result(a, ra.column_types)
    kind_b = classify_result(b, rb.column_types)
    if kind_a != kind_b:
        return 1.0  # different result shapes entirely -- not meaningfully comparable
    if kind_a == ResultKind.EMPTY:
        return 0.0
    if kind_a == ResultKind.SCALAR:
        return _scalar_divergence(a, b)
    if kind_a == ResultKind.MULTI_VALUE:
        return _multi_value_divergence(a, b)
    if kind_a == ResultKind.TIME_SERIES:
        return _time_series_divergence(a, b)
    return _ranked_list_divergence(a, b, top_n=top_n)


def _execute_cached(sql: str, conn: psycopg.Connection | None, timeout: float) -> ExecutionResult:
    key = _cache_key(sql)
    cached = _execution_cache.get(key)
    if cached is not None:
        return cached
    result = execute(sql, timeout=timeout, conn=conn)
    _execution_cache[key] = result
    return result


def compute_divergence_report(
    interpretations: list[tuple[str, str]],
    conn: psycopg.Connection | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_k: int = DEFAULT_MAX_K,
    top_n: int = DEFAULT_TOP_N,
) -> DivergenceReport:
    """`interpretations`: [(label, sql), ...], K <= max_k (extras dropped).

    Executes every interpretation (against `conn`, or a fresh readonly
    connection per call if none given), classifies each result, and scores
    every pair with the shape-appropriate comparator above. `score` is the
    worst (max) pairwise divergence across all K*(K-1)/2 pairs.
    """
    interpretations = interpretations[:max_k]
    labels = [label for label, _ in interpretations]

    def _run(label_sql: tuple[str, str]) -> tuple[str, ExecutionResult]:
        label, sql = label_sql
        return label, _execute_cached(sql, conn, timeout)

    results: dict[str, ExecutionResult] = dict(_run(item) for item in interpretations)

    kinds: dict[str, ResultKind] = {}
    samples: dict[str, list[list[Any]]] = {}
    columns: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    for label in labels:
        r = results[label]
        kinds[label] = classify_result(r.result_set, r.column_types) if r.ok else ResultKind.ERROR
        # capped at top_n, not a small fixed preview -- Task 3.6's question
        # rendering needs real overlap counts ("only 3 customers appear in
        # both"), computed from these same samples, not just a display snippet
        samples[label] = r.result_set.rows[:top_n] if r.ok and r.result_set else []
        columns[label] = r.result_set.columns if r.ok and r.result_set else []
        if not r.ok and r.error:
            errors[label] = r.error

    n = len(labels)
    matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = _pairwise_divergence(results[labels[i]], results[labels[j]], top_n=top_n)
            matrix[i][j] = matrix[j][i] = d

    score = max((matrix[i][j] for i in range(n) for j in range(i + 1, n)), default=0.0)

    return DivergenceReport(
        score=score,
        labels=labels,
        pairwise_matrix=matrix,
        result_kind_per_interpretation=kinds,
        sample_rows_per_interpretation=samples,
        columns_per_interpretation=columns,
        errors=errors,
    )
