"""Ties generation, static validation, and execution into one pipeline with
a bounded repair loop.

A candidate is validated before it ever touches the database (1.3); a
validation failure and a real DB error are treated the same way -- both are
just "here's what was wrong, ask the model to fix it" -- and share the same
`max_repairs` budget. A timeout is different: it's not something regenerating
the SQL can fix, so it short-circuits the loop immediately instead of
burning a repair attempt.
"""

from __future__ import annotations

from datetime import datetime, timezone

from t2sql.execution.executor import DEFAULT_ROW_CAP, DEFAULT_TIMEOUT_SECONDS, execute
from t2sql.execution.models import ExecutionResult
from t2sql.generation import generate_sql, repair_sql
from t2sql.generation.trace import TRACES_DIR, log_trace
from t2sql.validation import validate_sql

REPAIR_TRACE_PATH = TRACES_DIR / "repair.jsonl"


def _log_repair_attempt(question: str, attempt: int, previous_sql: str, error: str) -> None:
    log_trace(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "question": question,
            "repair_attempt": attempt,
            "previous_sql": previous_sql,
            "error": error,
        },
        path=REPAIR_TRACE_PATH,
    )


def generate_and_execute(
    question: str,
    context: str,
    dialect: str = "postgres",
    max_repairs: int = 2,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    row_cap: int = DEFAULT_ROW_CAP,
    model: str | None = None,
) -> ExecutionResult:
    """Generate SQL for `question`, then validate + execute it, repairing
    (regenerating from the error) up to `max_repairs` times on failure.
    """
    sql = generate_sql(question, context, dialect=dialect, model=model).sql
    result: ExecutionResult

    for attempt in range(max_repairs + 1):
        validation = validate_sql(sql, dialect=dialect)
        if validation.ok:
            result = execute(validation.rewritten_sql, timeout=timeout, row_cap=row_cap)
            result.sql = validation.rewritten_sql
            result.repair_attempts = attempt
            if result.ok or result.timed_out:
                return result
            error_message = result.error or "unknown execution error"
        else:
            error_message = "; ".join(f"[{e.type.value}] {e.message}" for e in validation.errors)
            result = ExecutionResult(ok=False, sql=sql, error=error_message, repair_attempts=attempt)

        if attempt == max_repairs:
            return result

        _log_repair_attempt(question, attempt + 1, sql, error_message)
        sql = repair_sql(question, context, sql, error_message, dialect=dialect, model=model).sql

    return result
