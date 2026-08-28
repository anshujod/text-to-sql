"""Named, configurable pipelines the eval runner can point at. `baseline` is
the unclarified path (1.1 retrieval -> 1.2 generation -> 1.3 validation);
later phases register a `clarify` config here that adds the clarification
engine, so `--config` can compare them apples-to-apples on the same dataset.
"""

from __future__ import annotations

from t2sql.generation import generate_sql
from t2sql.retrieval import build_schema_context
from t2sql.validation import validate_sql


def baseline_pipeline(question: str, dialect: str = "postgres") -> str:
    context = build_schema_context(question)
    generated = generate_sql(question, context, dialect=dialect)
    validation = validate_sql(generated.sql, dialect=dialect)
    return validation.rewritten_sql if validation.ok else generated.sql


PIPELINES = {
    "baseline": baseline_pipeline,
}
