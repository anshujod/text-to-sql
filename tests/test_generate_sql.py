"""10 hand-picked simple questions must produce executable SQL
returning non-empty results.

Every test in this file makes a real, billed OpenRouter LLM call, so it's
opt-in -- skipped unless RUN_LLM_TESTS=1 is set, so `make test` (and CI)
stay free and fast by default even with OPENROUTER_API_KEY configured. Run
explicitly with:
    RUN_LLM_TESTS=1 uv run pytest tests/test_generate_sql.py -q
"""

import os

import pytest

from t2sql.db.connection import get_connection
from t2sql.generation import GeneratedSQL, generate_sql
from t2sql.retrieval import build_schema_context

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS") != "1",
    reason="set RUN_LLM_TESTS=1 to run tests that make real, billed LLM calls",
)

SIMPLE_QUESTIONS = [
    "How many orders have been placed in total?",
    "What is the total revenue?",
    "How many products are in the catalog?",
    "List the top 5 categories by number of products.",
    "How many customers do we have?",
    "What is the average order value?",
    "How many sessions have been recorded?",
    "How many refunds have been issued?",
    "List the 5 most recently placed orders.",
    "How many distinct customers have made a purchase?",
]


@pytest.mark.parametrize("question", SIMPLE_QUESTIONS)
def test_generated_sql_executes_and_returns_rows(question: str) -> None:
    context = build_schema_context(question)
    result = generate_sql(question, context)

    assert isinstance(result, GeneratedSQL)
    assert result.sql.strip()
    assert result.tables_used
    assert 0.0 <= result.confidence <= 1.0

    with get_connection(role="readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(result.sql)
            rows = cur.fetchall()

    assert rows, f"query for {question!r} returned no rows:\n{result.sql}"
