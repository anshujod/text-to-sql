"""Gate: a query with a deliberately hallucinated column recovers
within 2 repairs, and a query with pg_sleep(10) times out cleanly rather
than hanging.

The repair step makes a real, billed OpenRouter call, so this file is
opt-in -- skipped unless RUN_LLM_TESTS=1. The initial (deliberately bad, or
deliberately hanging) SQL is forced via monkeypatch so the test is
deterministic; only the *recovery* goes through a real model.
"""

import os
import time

import pytest

from t2sql.execution.repair import generate_and_execute
from t2sql.generation.models import GeneratedSQL
from t2sql.retrieval import build_schema_context

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS") != "1",
    reason="set RUN_LLM_TESTS=1 to run tests that make real, billed LLM calls",
)


def test_hallucinated_column_recovers_within_two_repairs(monkeypatch: pytest.MonkeyPatch) -> None:
    question = "How many orders have been placed in total?"
    context = build_schema_context(question)

    def fake_generate_sql(question: str, context: str, dialect: str = "postgres", model=None) -> GeneratedSQL:
        return GeneratedSQL(
            sql="SELECT totally_made_up_column FROM orders",
            tables_used=["orders"],
            assumptions=["deliberately wrong, for the repair-loop gate test"],
            confidence=0.1,
        )

    monkeypatch.setattr("t2sql.execution.repair.generate_sql", fake_generate_sql)

    result = generate_and_execute(question, context, max_repairs=2)

    assert result.ok is True, f"did not recover: {result.error}"
    assert result.repair_attempts <= 2
    assert result.result_set is not None
    assert result.row_count > 0


def test_pg_sleep_times_out_cleanly_without_burning_a_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_generate_sql(question: str, context: str, dialect: str = "postgres", model=None) -> GeneratedSQL:
        return GeneratedSQL(sql="SELECT pg_sleep(10)", tables_used=[], assumptions=[], confidence=0.1)

    monkeypatch.setattr("t2sql.execution.repair.generate_sql", fake_generate_sql)

    start = time.monotonic()
    result = generate_and_execute("irrelevant question", "irrelevant context", max_repairs=2, timeout=2)
    elapsed = time.monotonic() - start

    assert result.ok is False
    assert result.timed_out is True
    assert result.repair_attempts == 0  # a timeout isn't fixable by regenerating -- no repair attempted
    assert elapsed < 5, f"took {elapsed:.1f}s -- a timeout must short-circuit, not hang or retry"
