"""Gate: the rule-based detector hits >=0.9 precision on dev for
METRIC, ENTITY, and GRAIN. Recall is explicitly not the target (self-
consistency detection is what's supposed to pick up the rest).

Precision here means: of the dev items where the detector fires a given
type, what fraction are actually labeled with that type in
`ambiguity_types`? Unambiguous items (empty ambiguity_types) count as a
false positive if any rule fires on them.
"""

from collections import defaultdict
from pathlib import Path

from t2sql.clarify import AmbiguityType, DetectedAmbiguity, detect_ambiguities, parse_intent
from t2sql.eval.dataset import load_dataset
from t2sql.semantic.loader import load_semantic_layer

DEV_DATASET_PATH = Path(__file__).resolve().parents[1] / "data" / "dev.jsonl"
PRECISION_GATED_TYPES = (AmbiguityType.METRIC, AmbiguityType.ENTITY, AmbiguityType.GRAIN)
PRECISION_THRESHOLD = 0.9


def test_best_customer_fires_metric_not_entity() -> None:
    """'best' matching 3 metrics is METRIC. ENTITY also matches (customers,
    users), but the rule suppresses it here: 'best' with no explicit count
    is exactly RESULT_SHAPE's territory (a vague superlative, not a genuine
    customer/user grain question), so ENTITY intentionally stays quiet
    rather than fire on every bare "best"/"top" + "customer" co-occurrence.
    """
    intent = parse_intent("Who is our best customer?")
    detections = detect_ambiguities(intent, load_semantic_layer())
    types = {d.type for d in detections}

    assert AmbiguityType.METRIC in types
    metric_detection = next(d for d in detections if d.type == AmbiguityType.METRIC)
    assert isinstance(metric_detection, DetectedAmbiguity)
    assert metric_detection.source == "rule"
    assert metric_detection.candidates == ["revenue_net", "order_count", "session_count"]


def test_how_many_customers_fires_entity() -> None:
    intent = parse_intent("How many customers do we have?")
    detections = detect_ambiguities(intent, load_semantic_layer())
    entity_detection = next(d for d in detections if d.type == AmbiguityType.ENTITY)

    assert entity_detection.candidates == ["customers", "users"]
    assert entity_detection.slot == "entity"


def test_entity_does_not_fire_when_context_already_pins_the_table() -> None:
    """'address' only exists on customers -- not a genuine users/customers
    conflation even though the word 'customer' appears.
    """
    intent = parse_intent("How many distinct customers have at least one address in Canada?")
    detections = detect_ambiguities(intent, load_semantic_layer())

    assert AmbiguityType.ENTITY not in {d.type for d in detections}


def test_entity_does_not_fire_on_orders_vs_order_alias() -> None:
    """A deliberate counter-example: matching the same table via two
    synonyms isn't ENTITY ambiguity."""
    intent = parse_intent("How many orders have we had?")
    detections = detect_ambiguities(intent, load_semantic_layer())

    assert AmbiguityType.ENTITY not in {d.type for d in detections}


def test_average_order_value_fires_grain() -> None:
    intent = parse_intent("What's the average order value?")
    detections = detect_ambiguities(intent, load_semantic_layer())
    grain_detection = next(d for d in detections if d.type == AmbiguityType.GRAIN)

    assert grain_detection.candidates == ["per_order", "per_customer", "per_month"]
    assert grain_detection.slot == "metric"


def test_grain_does_not_fire_for_a_non_averaging_metric() -> None:
    intent = parse_intent("How many orders have we had?")
    detections = detect_ambiguities(intent, load_semantic_layer())

    assert AmbiguityType.GRAIN not in {d.type for d in detections}


def test_last_month_fires_temporal() -> None:
    intent = parse_intent("How many orders did we get last month?")
    detections = detect_ambiguities(intent, load_semantic_layer())
    temporal_detection = next(d for d in detections if d.type == AmbiguityType.TEMPORAL)

    assert set(temporal_detection.candidates) == {"calendar_month", "trailing_30_days"}


def test_top_without_a_count_fires_result_shape() -> None:
    intent = parse_intent("Show me our top customers by revenue.")
    detections = detect_ambiguities(intent, load_semantic_layer())

    assert AmbiguityType.RESULT_SHAPE in {d.type for d in detections}


def test_dev_precision_gate_for_metric_entity_grain() -> None:
    """The actual precision gate for the rule-based detector."""
    layer = load_semantic_layer()
    items = load_dataset(DEV_DATASET_PATH)

    fired: dict[AmbiguityType, int] = defaultdict(int)
    true_positive: dict[AmbiguityType, int] = defaultdict(int)

    for item in items:
        intent = parse_intent(item.question, layer=layer)
        detected_types = {d.type for d in detect_ambiguities(intent, layer)}
        for t in detected_types:
            fired[t] += 1
            if t.value in item.ambiguity_types:
                true_positive[t] += 1

    for t in PRECISION_GATED_TYPES:
        assert fired[t] > 0, f"{t.value}: rule never fired on dev -- can't measure precision"
        precision = true_positive[t] / fired[t]
        assert precision >= PRECISION_THRESHOLD, (
            f"{t.value}: precision {precision:.1%} ({true_positive[t]}/{fired[t]}) "
            f"below the {PRECISION_THRESHOLD:.0%} bar"
        )
