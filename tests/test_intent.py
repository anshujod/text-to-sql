"""Gate: parse_intent resolves against the semantic layer, and on
the dev ambiguous set it surfaces multiple candidates for >=80% of METRIC
and >=80% of ENTITY items.
"""

from pathlib import Path

from t2sql.clarify.intent import Intent, Slot, parse_intent
from t2sql.eval.dataset import load_dataset
from t2sql.semantic.loader import load_semantic_layer

DEV_DATASET_PATH = Path(__file__).resolve().parents[1] / "data" / "dev.jsonl"
MULTI_CANDIDATE_THRESHOLD = 0.8


def test_best_customer_surfaces_the_three_way_metric_ambiguity() -> None:
    """The canonical example: 'best' -> [revenue_net, order_count, session_count]."""
    intent = parse_intent("Who is our best customer?")

    assert isinstance(intent, Intent)
    assert intent.metric.candidates == ["revenue_net", "order_count", "session_count"]
    assert intent.metric.resolved is None
    assert intent.metric.reason


def test_how_many_customers_surfaces_the_entity_ambiguity() -> None:
    intent = parse_intent("How many customers do we have?")

    assert set(intent.entity.candidates) == {"customers", "users"}
    assert intent.entity.resolved is None
    assert intent.entity.reason


def test_unambiguous_metric_resolves_to_a_single_candidate() -> None:
    intent = parse_intent("What's the average order value?")

    assert intent.metric.candidates == ["aov"]
    assert intent.metric.resolved == "aov"
    assert intent.metric.reason is None


def test_every_unresolved_slot_carries_a_reason() -> None:
    intent = parse_intent("How many browsing sessions have been recorded?")

    for slot in (intent.metric, intent.dimensions, intent.filters, intent.time_range, intent.limit, intent.sort):
        assert isinstance(slot, Slot)
        if slot.resolved is None:
            assert slot.reason, f"unresolved slot has no reason: {slot}"
        else:
            assert slot.reason is None

    assert intent.entity.resolved == "sessions"


def test_explicit_limit_and_relative_time_resolve() -> None:
    intent = parse_intent("Show me our top 5 customers by revenue last month.")

    assert intent.limit.resolved == "5"
    assert intent.sort.resolved == "desc"
    # calendar-month vs trailing-30-days is a real TEMPORAL ambiguity (see docs/taxonomy.md)
    assert set(intent.time_range.candidates) == {"calendar_month", "trailing_30_days"}
    assert intent.time_range.resolved is None


def test_ranked_request_without_a_count_stays_unresolved() -> None:
    intent = parse_intent("Show me our top customers by revenue.")

    assert intent.limit.candidates == []
    assert intent.limit.resolved is None
    assert "no explicit row count" in intent.limit.reason


def test_dev_ambiguous_set_metric_and_entity_recall() -> None:
    """The actual recall gate: on dev's ambiguous items, >=80% of
    METRIC items and >=80% of ENTITY items get >=2 metric/entity candidates.
    """
    layer = load_semantic_layer()
    items = load_dataset(DEV_DATASET_PATH)

    for ambiguity_type, slot_name in (("METRIC", "metric"), ("ENTITY", "entity")):
        subset = [item for item in items if ambiguity_type in item.ambiguity_types]
        assert subset, f"no dev items tagged {ambiguity_type}"

        hits = 0
        for item in subset:
            intent = parse_intent(item.question, layer=layer)
            slot: Slot = getattr(intent, slot_name)
            if len(slot.candidates) >= 2:
                hits += 1

        recall = hits / len(subset)
        assert recall >= MULTI_CANDIDATE_THRESHOLD, (
            f"{ambiguity_type}: only {hits}/{len(subset)} ({recall:.1%}) dev items surfaced "
            f"multiple {slot_name} candidates, need >= {MULTI_CANDIDATE_THRESHOLD:.0%}"
        )
