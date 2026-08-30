"""Self-consistency detection: signature extraction and threshold-decision
logic are pure and fast to test (no LLM calls). The real target -- catching >=5 dev
ambiguous items the rule detector missed, false-firing on <=15% of the
unambiguous set -- is an expensive, LLM-driven calibration exercise done
once via scripts/tune_self_consistency_threshold.py, not re-run on every
test invocation. See data/DATASET.md-adjacent notes in that script for the
result of that calibration.

A couple of RUN_LLM_TESTS=1-gated smoke tests exercise the real
generate -> extract -> cluster -> decide path end to end, same opt-in
pattern as tests/test_generate_sql.py.
"""

import os

import pytest

from t2sql.clarify.self_consistency import (
    DivergenceResult,
    decide,
    detect_self_consistency,
    extract_signature,
)


def test_identical_queries_have_identical_signatures() -> None:
    a = extract_signature("SELECT COUNT(*) FROM orders WHERE status != 'cancelled'")
    b = extract_signature("select count(*) from orders where status != 'cancelled'")

    assert a == b


def test_different_aggregate_expression_changes_the_signature() -> None:
    """revenue_gross vs revenue_net -- same tables, different SELECT shape."""
    gross = extract_signature("SELECT SUM(amount) FROM payments WHERE status = 'succeeded'")
    net = extract_signature(
        "SELECT SUM(p.amount) - SUM(r.amount) FROM payments p, refunds r WHERE p.status = 'succeeded'"
    )

    assert gross != net
    assert gross.select_exprs != net.select_exprs


def test_different_tables_changes_the_signature() -> None:
    """customers vs users -- the canonical ENTITY case."""
    customers = extract_signature("SELECT COUNT(*) FROM customers")
    users = extract_signature("SELECT COUNT(*) FROM users WHERE deleted_at IS NULL")

    assert customers.tables == frozenset({"customers"})
    assert users.tables == frozenset({"users"})
    assert customers != users


def test_different_limit_changes_the_signature_but_not_the_rest() -> None:
    top5 = extract_signature("SELECT id FROM customers ORDER BY id DESC LIMIT 5")
    top10 = extract_signature("SELECT id FROM customers ORDER BY id DESC LIMIT 10")

    assert top5.limit == 5
    assert top10.limit == 10
    assert top5.tables == top10.tables
    assert top5.order_by == top10.order_by
    assert top5 != top10


def test_where_predicates_are_order_independent() -> None:
    a = extract_signature("SELECT * FROM orders WHERE a = 1 AND b = 2")
    b = extract_signature("SELECT * FROM orders WHERE b = 2 AND a = 1")

    assert a.where_predicates == b.where_predicates


def test_unparseable_sql_returns_none() -> None:
    assert extract_signature("this is not sql at all") is None
    assert extract_signature("DROP TABLE orders") is None  # not a SELECT


def test_decide_returns_none_at_or_below_threshold() -> None:
    result = DivergenceResult(question="q", n=5, score=0.4, largest_cluster_size=3, distinct_signatures=["a", "b"])
    assert decide(result, threshold=0.4) is None


def test_decide_flags_above_threshold_with_self_consistency_source() -> None:
    result = DivergenceResult(
        question="Who is our best customer?",
        n=5,
        score=0.6,
        largest_cluster_size=2,
        distinct_signatures=[
            "tables=['customers']; select=['count(*)']",
            "tables=['customers', 'orders']; select=['sum(amount)']",
        ],
        raw_sql=[
            "SELECT COUNT(*) FROM customers",
            "SELECT COUNT(*) FROM customers",
            "SELECT SUM(amount) FROM customers c JOIN orders o ON o.customer_id = c.id",
            "SELECT SUM(amount) FROM customers c JOIN orders o ON o.customer_id = c.id",
            "SELECT SUM(amount) FROM customers c JOIN orders o ON o.customer_id = c.id",
        ],
    )
    detection = decide(result, threshold=0.4)

    assert detection is not None
    assert detection.source == "self_consistency"
    assert detection.confidence == 0.6
    assert len(detection.candidates) == 2


@pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS") != "1",
    reason="set RUN_LLM_TESTS=1 to run tests that make real, billed LLM calls",
)
class TestRealGeneration:
    def test_genuinely_ambiguous_question_shows_high_divergence(self) -> None:
        detection = detect_self_consistency("Who is our best customer?", n=5, threshold=0.3)

        assert detection is not None
        assert detection.source == "self_consistency"
        assert detection.confidence > 0.3

    def test_simple_unambiguous_question_shows_low_divergence(self) -> None:
        detection = detect_self_consistency("How many categories are there?", n=5, threshold=0.6)

        assert detection is None
