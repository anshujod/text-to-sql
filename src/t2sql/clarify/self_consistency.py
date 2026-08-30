"""Self-consistency detector.


Candidates are compared **semantically, not textually**: each is parsed
with sqlglot into a `QuerySignature` (tables referenced, SELECT projection
expressions, GROUP BY keys, WHERE predicates, ORDER BY, LIMIT), so two
queries that are token-for-token different but compute the same thing
cluster together, and two queries that read alike but pick a different
metric/scope/grain don't.

Divergence score = 1 - (largest cluster size / N).

Dev calibration (scripts/tune_self_consistency_threshold.py, full 74-item
run: all 13 dev ambiguous items Task 3.2's rules missed, plus all 61 dev
unambiguous items, N=5, temperature=0.8, OPENROUTER_DETECTION_MODEL =
claude-haiku-4.5): **PLAN.md 3.3's Done-when (>=5 caught, <=15% false-fire)
is not cleanly met by any single threshold** on this run --

    threshold | caught (of 13) | false-fire (of 61)
    0.4 / 0.5 |       4        |   13 (21.3%)
    0.6 / 0.7 |       2        |    5 ( 8.2%)

DEFAULT_THRESHOLD=0.6 below is the practical choice: it keeps false-fire
comfortably under the 15% ceiling (over-asking is the failure mode this
whole project is built to avoid) at the cost of recall staying below the
literal >=5 bar -- an explicitly sanctioned tradeoff ("recall will be
mediocre -- that is what 3.3 is for"). Two fixes to `extract_signature`
already found and applied during this calibration (table-alias
canonicalization, excluding CTE names from the `tables` set) removed a
lot of pure naming noise; most of what's left is the cheap detection
model genuinely being inconsistent about applying house defaults (scope
filters, LIMIT semantics) across samples, not a text-comparison artifact.
Re-tuning with a stronger model and/or higher N would very likely close
the gap, but wasn't run here -- see git history / the calibration script
for how to redo it when that's worth the cost.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

from t2sql.clarify.detector import DetectedAmbiguity
from t2sql.clarify.taxonomy import AmbiguityType
from t2sql.generation import GeneratedSQL, generate_sql
from t2sql.retrieval import build_schema_context

DEFAULT_N = 5
DEFAULT_TEMPERATURE = 0.8
# See the module docstring's "Dev calibration" section for how this was
# picked and its known gap against PLAN.md 3.3's literal Done-when bar.
DEFAULT_THRESHOLD = 0.6

# Which Intent-ish slot label to report for each inferred type -- mirrors
# Task 3.2's detector.py slot naming so downstream consumers (3.5's policy
# engine) don't need to special-case the detection source.
_TYPE_TO_SLOT: dict[AmbiguityType, str] = {
    AmbiguityType.ENTITY: "entity",
    AmbiguityType.METRIC: "metric",
    AmbiguityType.GRAIN: "dimensions",
    AmbiguityType.TEMPORAL: "time_range",
    AmbiguityType.SCOPE: "filters",
    AmbiguityType.RESULT_SHAPE: "limit",
    AmbiguityType.COMPARISON: "time_range",
}

_DATE_LITERAL_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class QuerySignature:
    """The semantic shape of a query -- what Task 3.4's divergence test
    also cares about, but computed from SQL structure instead of results.
    """

    tables: frozenset[str]
    select_exprs: tuple[str, ...]
    group_by: tuple[str, ...]
    where_predicates: frozenset[str]
    order_by: tuple[str, ...]
    limit: int | None

    def describe(self) -> str:
        parts = [f"tables={sorted(self.tables)}"]
        if self.select_exprs:
            parts.append(f"select={list(self.select_exprs)}")
        if self.group_by:
            parts.append(f"group_by={list(self.group_by)}")
        if self.where_predicates:
            parts.append(f"where={sorted(self.where_predicates)}")
        if self.order_by:
            parts.append(f"order_by={list(self.order_by)}")
        if self.limit is not None:
            parts.append(f"limit={self.limit}")
        return "; ".join(parts)


def _split_and(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.And):
        return _split_and(node.left) + _split_and(node.right)
    return [node]


def _canonicalize_table_aliases(tree: exp.Select) -> exp.Select:
    """Rewrite every table-alias reference to the real table name, and drop
    SELECT-list aliases (the "AS x" naming).

    Two independently-generated candidates that compute the exact same
    thing routinely pick different aliases ("s"/"u" vs "sessions"/"users")
    and different output column names ("total_sessions" vs "session_count")
    -- neither is a semantic difference, and comparing raw SQL text would
    count both as "different queries," inflating the divergence score with
    pure naming noise instead of real ambiguity.
    """
    tree = tree.copy()
    alias_to_table: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        alias = table.alias
        if alias:
            alias_to_table[alias] = table.name
        table.set("alias", None)
    for col in tree.find_all(exp.Column):
        table_ref = col.table
        if table_ref and table_ref in alias_to_table:
            col.set("table", exp.to_identifier(alias_to_table[table_ref]))
    for projection in list(tree.expressions):
        if isinstance(projection, exp.Alias):
            projection.replace(projection.this)
    return tree


def extract_signature(sql: str, dialect: str = "postgres") -> QuerySignature | None:
    """Parse `sql` and extract its semantic signature. None if unparseable
    or not a single SELECT -- a real failure mode the caller should count
    as evidence of instability, not silently skip.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return None
    if not isinstance(tree, exp.Select):
        return None

    tree = _canonicalize_table_aliases(tree)

    # CTE names are arbitrary, model-chosen local names ("product_revenue",
    # "avg_product_revenue", ...), not real database tables -- sqlglot's
    # exp.Table matches both, so exclude anything defined by a WITH clause
    # or two candidates that compute the identical thing via differently-
    # named CTEs would look like they used different tables entirely.
    cte_names = {cte.alias.lower() for cte in tree.find_all(exp.CTE)}
    tables = frozenset(t.name.lower() for t in tree.find_all(exp.Table) if t.name.lower() not in cte_names)
    select_exprs = tuple(sorted(e.sql(dialect=dialect).lower() for e in tree.expressions))

    group = tree.args.get("group")
    group_by = tuple(sorted(e.sql(dialect=dialect).lower() for e in group.expressions)) if group else ()

    where = tree.args.get("where")
    where_predicates = (
        frozenset(p.sql(dialect=dialect).lower() for p in _split_and(where.this)) if where else frozenset()
    )

    order = tree.args.get("order")
    order_by = tuple(e.sql(dialect=dialect).lower() for e in order.expressions) if order else ()

    limit_node = tree.args.get("limit")
    limit: int | None = None
    if limit_node is not None:
        try:
            limit = int(limit_node.expression.this)
        except (AttributeError, ValueError, TypeError):
            limit = None

    return QuerySignature(
        tables=tables,
        select_exprs=select_exprs,
        group_by=group_by,
        where_predicates=where_predicates,
        order_by=order_by,
        limit=limit,
    )


class DivergenceResult(BaseModel):
    """Raw output of one self-consistency pass -- deliberately separate
    from the threshold decision so a threshold can be tuned against
    already-generated results without re-calling the model.
    """

    model_config = {"arbitrary_types_allowed": True}

    question: str
    n: int
    score: float
    largest_cluster_size: int
    distinct_signatures: list[str] = Field(default_factory=list)
    unparseable_count: int = 0
    raw_sql: list[str] = Field(default_factory=list)


def compute_divergence(
    question: str,
    context: str | None = None,
    n: int = DEFAULT_N,
    temperature: float = DEFAULT_TEMPERATURE,
    model: str | None = None,
    dialect: str = "postgres",
) -> DivergenceResult:
    """Generate `n` candidates and score how much they disagree. Makes `n`
    real LLM calls -- this is the expensive part of Task 3.3, callers doing
    batch evaluation should cache this result, not just the final decision.

    Defaults to `OPENROUTER_DETECTION_MODEL` (a cheaper model than the
    baseline generator's `OPENROUTER_GENERATION_MODEL`) when `model` isn't
    given explicitly -- self-consistency only needs candidates cheap and
    fast enough to sample N of them, not the baseline's best-effort answer.
    """
    model = model or os.environ.get("OPENROUTER_DETECTION_MODEL")
    context = context if context is not None else build_schema_context(question)
    results = generate_sql(question, context, dialect=dialect, temperature=temperature, n=n, model=model)
    candidates: list[GeneratedSQL] = results if isinstance(results, list) else [results]

    signatures = [extract_signature(c.sql, dialect=dialect) for c in candidates]
    counts = Counter(signatures)
    largest = max(counts.values())
    score = 1 - (largest / len(signatures))

    distinct = [sig for sig in counts if sig is not None]
    return DivergenceResult(
        question=question,
        n=n,
        score=score,
        largest_cluster_size=largest,
        distinct_signatures=[sig.describe() for sig in distinct],
        unparseable_count=counts.get(None, 0),
        raw_sql=[c.sql for c in candidates],
    )


def _infer_type(signatures: list[QuerySignature]) -> AmbiguityType:
    """Best-guess which taxonomy axis the disagreement is along, from which
    signature component actually varies. Not gated by Task 3.3's Done-when
    (which only cares that ambiguity gets flagged at all), but useful for
    Task 3.5's policy engine to have *something* better than "unknown".
    """
    if len({s.tables for s in signatures}) > 1:
        return AmbiguityType.ENTITY
    if len({s.select_exprs for s in signatures}) > 1:
        return AmbiguityType.METRIC
    if len({s.group_by for s in signatures}) > 1:
        return AmbiguityType.GRAIN
    if len({s.where_predicates for s in signatures}) > 1:
        all_predicates = set().union(*(s.where_predicates for s in signatures))
        if any(_DATE_LITERAL_RE.search(p) or "interval" in p for p in all_predicates):
            return AmbiguityType.TEMPORAL
        return AmbiguityType.SCOPE
    if len({(s.order_by, s.limit) for s in signatures}) > 1:
        return AmbiguityType.RESULT_SHAPE
    return AmbiguityType.COMPARISON


def detect_self_consistency(
    question: str,
    context: str | None = None,
    n: int = DEFAULT_N,
    temperature: float = DEFAULT_TEMPERATURE,
    threshold: float = DEFAULT_THRESHOLD,
    model: str | None = None,
    dialect: str = "postgres",
) -> DetectedAmbiguity | None:
    """Full pipeline: generate, score, and flag if `score > threshold`.
    See `compute_divergence` if you want to tune `threshold` against
    already-generated results instead of re-calling the model each time.
    """
    result = compute_divergence(question, context=context, n=n, temperature=temperature, model=model, dialect=dialect)
    return decide(result, threshold=threshold)


def decide(result: DivergenceResult, threshold: float = DEFAULT_THRESHOLD) -> DetectedAmbiguity | None:
    """Apply a threshold to an already-computed DivergenceResult."""
    if result.score <= threshold:
        return None

    signatures = [extract_signature(sql) for sql in result.raw_sql]
    distinct_signatures = [s for s in signatures if s is not None]
    inferred_type = _infer_type(distinct_signatures) if len(distinct_signatures) > 1 else AmbiguityType.COMPARISON

    return DetectedAmbiguity(
        type=inferred_type,
        slot=_TYPE_TO_SLOT[inferred_type],
        candidates=result.distinct_signatures or ["(all candidates unparseable)"],
        confidence=result.score,
        source="self_consistency",
    )
