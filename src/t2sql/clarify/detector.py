"""Deterministic slot detector

Rule-based ambiguity detection over a parsed `Intent`. Cheap, fast,
testable, and runs before the LLM-based self-consistency detector. The
target here is **precision** on dev for METRIC, ENTITY, GRAIN (>=0.9) --
recall is explicitly not the goal (that's what self-consistency detection
is for), so every rule below is written to fire only when it's confident,
not to catch everything.

Seven rules, one per `AmbiguityType`:

  METRIC        multiple metric candidates matched
  ENTITY        multiple entity candidates matched, restricted to pairs that
                actually differ in grain (see DIFFERENT_GRAIN_ENTITY_PAIRS)
  GRAIN         a matched metric is an averaging metric (a mean, which is
                itself denominator-ambiguous -- per order/customer/month)
  TEMPORAL      `intent.time_range` has more than one common reading
  RESULT_SHAPE  ranking language ("top", "best") with no explicit row count
  SCOPE         a matched metric carries default_filters and the question
                states no explicit scope language at all
  COMPARISON    a trend word ("growing", "declining") with no stated baseline
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from t2sql.clarify.intent import Intent
from t2sql.clarify.taxonomy import AmbiguityType
from t2sql.semantic.models import Metric, SemanticLayer

# The only table pair in this schema where the same natural-language noun
# ("customer") genuinely refers to two tables at different grain -- see
# entities.yaml's customers/users description and docs/taxonomy.md's ENTITY
# example. Not derived generically from the schema (e.g. "any two FK-linked
# tables") because that over-fires: products/categories are FK-linked too,
# but nobody confuses "categories" with "products" the way "customer" is
# confused with "user account". Extend this set if the schema grows another
# such conflation.
DIFFERENT_GRAIN_ENTITY_PAIRS: frozenset[frozenset[str]] = frozenset({frozenset({"customers", "users"})})

# Fixed candidate readings for slots without a semantic-layer-backed
# candidate list (dimensions/comparison baselines aren't modeled as
# resolvable entities the way metrics/tables are) -- mirrors the fixed
# reading lists intent parsing already uses for time_range.
GRAIN_DEFAULT_READINGS = ["per_order", "per_customer", "per_month"]
COMPARISON_DEFAULT_READINGS = ["month_over_month", "trailing_period_average"]

COMPARISON_TREND_TERMS = ["growing", "grown", "growth", "declining", "decline", "trending", "increasing", "decreasing"]
COMPARISON_BASELINE_TERMS = ["compared to", "vs.", "vs ", "versus", "relative to", "against last", "against the"]

# Deterministic-rule confidences. Not empirically calibrated to a
# probability scale -- just a fixed per-rule strength ranking so a
# downstream consumer (the policy engine) can prefer a stronger
# signal over a weaker one when several rules fire on the same question.
CONFIDENCE_ENTITY = 0.9
CONFIDENCE_METRIC = 0.85
CONFIDENCE_TEMPORAL = 0.85
CONFIDENCE_GRAIN = 0.75
CONFIDENCE_RESULT_SHAPE = 0.7
CONFIDENCE_COMPARISON = 0.6
CONFIDENCE_SCOPE = 0.5


class DetectedAmbiguity(BaseModel):
    type: AmbiguityType
    slot: str
    candidates: list[str]
    confidence: float
    source: Literal["rule", "self_consistency"] = "rule"


# Phrases that mean the question has already spelled out its own metric
# formula or ranking basis ("... by number of orders", "using unit price
# times quantity") -- a superlative word like "top"/"biggest" alongside one
# of these isn't really METRIC-ambiguous, the question just also happens to
# use vague language on top of an explicit definition.
EXPLICIT_METRIC_DEFINITION_TERMS = [
    "using ", "unit price", " by number of", " by total", " by delivered",
    "gap between", "difference between", "order count", "address count",
]

# Terms that pin "customer"/"user" to a specific table via some other
# relationship or already-explicit wording, so the bare word "customer"
# co-occurring with it isn't the genuine grain confusion ENTITY is about.
# Necessarily a heuristic, not exhaustive -- see the precision-vs-recall
# tradeoff note in the module docstring.
ENTITY_DISAMBIGUATING_TERMS = [
    "address", "entity", "entities", " id ", "customer id", "by id", "login", "email",
    "session", "'s ", "category", "categories", "product", "per customer", "distinct customers",
]


def _is_averaging_metric(metric: Metric) -> bool:
    if "/" in metric.sql_expression:
        return True
    return any("average" in s.lower() or "avg" in s.lower() for s in metric.synonyms)


def _detect_metric(intent: Intent) -> DetectedAmbiguity | None:
    if len(intent.metric.candidates) < 2:
        return None
    question_lower = intent.question.lower()
    if intent.dimensions.candidates or any(t in question_lower for t in EXPLICIT_METRIC_DEFINITION_TERMS):
        return None  # the ranking basis/formula is already spelled out -- not actually ambiguous
    return DetectedAmbiguity(
        type=AmbiguityType.METRIC,
        slot="metric",
        candidates=intent.metric.candidates,
        confidence=CONFIDENCE_METRIC,
    )


def _detect_entity(intent: Intent) -> DetectedAmbiguity | None:
    candidates = set(intent.entity.candidates)
    if len(candidates) < 2:
        return None
    question_lower = intent.question.lower()
    if any(t in question_lower for t in ENTITY_DISAMBIGUATING_TERMS):
        return None  # "customer"/"user" co-occurs, but something else already pins the table
    if intent.dimensions.candidates or any(t in question_lower for t in EXPLICIT_METRIC_DEFINITION_TERMS):
        return None  # ranking basis/formula already spelled out -- same signal the METRIC rule uses
    if intent.limit.resolved is None and intent.limit.reason == "ranked request with no explicit row count":
        return None  # vague "top"/"best" language with no count is RESULT_SHAPE's territory, not ENTITY's
    for pair in DIFFERENT_GRAIN_ENTITY_PAIRS:
        if pair <= candidates:
            return DetectedAmbiguity(
                type=AmbiguityType.ENTITY,
                slot="entity",
                candidates=sorted(pair),
                confidence=CONFIDENCE_ENTITY,
            )
    return None  # multiple tables matched, but none of the pairs actually differ in grain


def _detect_grain(intent: Intent, layer: SemanticLayer) -> DetectedAmbiguity | None:
    matched_metric_names = intent.metric.candidates
    averaging = [name for name in matched_metric_names if name in layer.metrics and _is_averaging_metric(layer.metrics[name])]
    if not averaging:
        return None
    return DetectedAmbiguity(
        type=AmbiguityType.GRAIN,
        slot="metric",
        candidates=GRAIN_DEFAULT_READINGS,
        confidence=CONFIDENCE_GRAIN,
    )


def _detect_temporal(intent: Intent) -> DetectedAmbiguity | None:
    if len(intent.time_range.candidates) < 2:
        return None
    return DetectedAmbiguity(
        type=AmbiguityType.TEMPORAL,
        slot="time_range",
        candidates=intent.time_range.candidates,
        confidence=CONFIDENCE_TEMPORAL,
    )


def _detect_result_shape(intent: Intent) -> DetectedAmbiguity | None:
    if intent.limit.resolved is not None:
        return None
    if intent.limit.reason != "ranked request with no explicit row count":
        return None
    return DetectedAmbiguity(
        type=AmbiguityType.RESULT_SHAPE,
        slot="limit",
        candidates=[],
        confidence=CONFIDENCE_RESULT_SHAPE,
    )


def _detect_scope(intent: Intent, layer: SemanticLayer) -> DetectedAmbiguity | None:
    if intent.filters.candidates:
        return None  # question already states scope explicitly -- default+disclose, not ambiguous
    matched_metric_names = intent.metric.candidates or ([intent.metric.resolved] if intent.metric.resolved else [])
    scoped_metrics = [
        name for name in matched_metric_names if name in layer.metrics and layer.metrics[name].default_filters
    ]
    if not scoped_metrics:
        return None
    return DetectedAmbiguity(
        type=AmbiguityType.SCOPE,
        slot="filters",
        candidates=scoped_metrics,
        confidence=CONFIDENCE_SCOPE,
    )


def _detect_comparison(intent: Intent) -> DetectedAmbiguity | None:
    question_lower = intent.question.lower()
    has_trend_word = any(term in question_lower for term in COMPARISON_TREND_TERMS)
    if not has_trend_word:
        return None
    has_explicit_baseline = any(term in question_lower for term in COMPARISON_BASELINE_TERMS)
    if has_explicit_baseline:
        return None
    return DetectedAmbiguity(
        type=AmbiguityType.COMPARISON,
        slot="time_range",
        candidates=COMPARISON_DEFAULT_READINGS,
        confidence=CONFIDENCE_COMPARISON,
    )


def detect_ambiguities(intent: Intent, layer: SemanticLayer) -> list[DetectedAmbiguity]:
    detectors = (
        _detect_metric(intent),
        _detect_entity(intent),
        _detect_grain(intent, layer),
        _detect_temporal(intent),
        _detect_result_shape(intent),
        _detect_scope(intent, layer),
        _detect_comparison(intent),
    )
    return [d for d in detectors if d is not None]
