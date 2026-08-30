"""The ambiguity taxonomy as code, not just prose.

Seven types, drawn from where this specific schema (docker/init/sql/schema.sql)
and semantic layer (src/t2sql/semantic/) deliberately leave more than one
defensible reading of a question. Every type below has a worked example with
2+ SQL variants that return visibly different results on the seed data --
see docs/taxonomy.md for the verified numbers.

Detection hints are lightweight lexical/structural cues for the rule-based
detector -- they are a starting point, not a claim of completeness.
Recall is expected to be mediocre.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AmbiguityType(str, Enum):
    METRIC = "METRIC"
    TEMPORAL = "TEMPORAL"
    ENTITY = "ENTITY"
    SCOPE = "SCOPE"
    GRAIN = "GRAIN"
    COMPARISON = "COMPARISON"
    RESULT_SHAPE = "RESULT_SHAPE"


class ClarificationPolicy(str, Enum):
    """What to do by default when this ambiguity type is detected.

    ASK: the interpretations are usually far enough apart, and picking wrong
    is costly enough, that guessing isn't acceptable -- surface the options.
    DEFAULT_AND_DISCLOSE: pick the house default from semantic/defaults.yaml,
    execute, and say what was assumed in the answer. Never silent either way.
    """

    ASK = "ASK"
    DEFAULT_AND_DISCLOSE = "DEFAULT_AND_DISCLOSE"


@dataclass(frozen=True)
class AmbiguityTypeSpec:
    type: AmbiguityType
    summary: str
    description: str
    default_policy: ClarificationPolicy
    example_question: str
    detection_hints: list[str] = field(default_factory=list)


TAXONOMY: dict[AmbiguityType, AmbiguityTypeSpec] = {
    AmbiguityType.METRIC: AmbiguityTypeSpec(
        type=AmbiguityType.METRIC,
        summary='"best", "top", "most valuable" -- which metric?',
        description=(
            "A superlative or vague success term that resolves to more than one "
            "metric in semantic/metrics.yaml. 'Best customer' could mean highest "
            "revenue_net, highest order_count, or highest session_count -- these "
            "are deliberately overlapping synonyms (see metrics.yaml's comment) "
            "and the top-ranked entity genuinely differs depending on which one "
            "is meant."
        ),
        default_policy=ClarificationPolicy.ASK,
        example_question="Who is our best customer?",
        detection_hints=[
            "best", "top", "most valuable", "highest", "greatest",
            "term matches synonyms in >1 metrics.yaml entry",
        ],
    ),
    AmbiguityType.TEMPORAL: AmbiguityTypeSpec(
        type=AmbiguityType.TEMPORAL,
        summary='"last month" -- calendar month or trailing 30 days? anchored to when?',
        description=(
            "A relative time expression with more than one common reading: "
            "calendar last month vs. trailing 30 days, and anchored to "
            "wall-clock now() vs. max(orders.created_at) (see "
            "semantic/defaults.yaml's anchor rationale -- the seed data is a "
            "fixed historical window with a Black Friday spike that sits inside "
            "the calendar-month reading but outside the trailing-30-day one)."
        ),
        default_policy=ClarificationPolicy.DEFAULT_AND_DISCLOSE,
        example_question="How many orders did we get last month?",
        detection_hints=[
            "last month", "last week", "recently", "this quarter", "ytd",
            "trailing", "past N days", "relative date phrase with no explicit range",
        ],
    ),
    AmbiguityType.ENTITY: AmbiguityTypeSpec(
        type=AmbiguityType.ENTITY,
        summary='"customers" -- the customers table, or users?',
        description=(
            "A noun that maps to more than one table at a different grain. "
            "'Customer' is ambiguous between customers (one row per real-world "
            "entity) and users (one row per login account; several logins can "
            "share a customer) -- see entities.yaml. Ask only when the "
            "candidate tables actually differ in grain; 'orders' vs. an 'order' "
            "alias is not this."
        ),
        default_policy=ClarificationPolicy.ASK,
        example_question="How many customers do we have?",
        detection_hints=[
            "customers", "users", "term matches >1 entity with different grain",
        ],
    ),
    AmbiguityType.SCOPE: AmbiguityTypeSpec(
        type=AmbiguityType.SCOPE,
        summary="include refunds? cancelled orders? internal accounts? deleted rows?",
        description=(
            "The question doesn't say whether to include rows that are "
            "arguably out of scope: cancelled/returned orders, refunded "
            "amounts, internal/staff accounts (users.is_internal), or "
            "soft-deleted rows (deleted_at). semantic/defaults.yaml fixes a "
            "house default for each -- apply it and say so, rather than "
            "asking every time."
        ),
        default_policy=ClarificationPolicy.DEFAULT_AND_DISCLOSE,
        example_question="How many orders have we had?",
        detection_hints=[
            "cancelled", "returned", "refund", "deleted", "internal", "staff",
            "no explicit status/scope filter on a table with a documented default",
        ],
    ),
    AmbiguityType.GRAIN: AmbiguityTypeSpec(
        type=AmbiguityType.GRAIN,
        summary='"average order value" -- per order, per customer, per month?',
        description=(
            "An aggregation whose denominator (or grouping level) isn't fully "
            "specified. 'Average order value' could mean total revenue divided "
            "by order count (per-order grain) or the average of each "
            "customer's own average spend (per-customer grain) -- these are "
            "arithmetically different quantities, not just different phrasings "
            "of the same one."
        ),
        default_policy=ClarificationPolicy.ASK,
        example_question="What's our average order value?",
        detection_hints=[
            "average", "per customer", "per month", "per order",
            "aggregation with no stated grouping level",
        ],
    ),
    AmbiguityType.COMPARISON: AmbiguityTypeSpec(
        type=AmbiguityType.COMPARISON,
        summary='"growing" -- growing relative to what baseline?',
        description=(
            "A trend or comparison word with no stated baseline. 'Is Books "
            "growing?' can flip sign depending on whether growth is measured "
            "month-over-month (which lands right after a Black Friday spike, "
            "so it reads as decline) or against a trailing multi-month average "
            "(which reads as growth) -- opposite answers to the same question."
        ),
        default_policy=ClarificationPolicy.ASK,
        example_question="Which product category is growing the fastest?",
        detection_hints=[
            "growing", "declining", "trending", "increasing", "decreasing",
            "compared to", "vs.", "no explicit baseline period",
        ],
    ),
    AmbiguityType.RESULT_SHAPE: AmbiguityTypeSpec(
        type=AmbiguityType.RESULT_SHAPE,
        summary='"top customers" -- how many rows?',
        description=(
            "A ranked-list request with no explicit row count. The ranking "
            "itself may be uncontested, but the cutoff isn't -- LIMIT 5 and "
            "LIMIT 10 return literally different result sets. Low-stakes "
            "enough to default rather than ask; semantic/defaults.yaml fixes "
            "10 as the house default."
        ),
        default_policy=ClarificationPolicy.DEFAULT_AND_DISCLOSE,
        example_question="Show me our top customers by revenue.",
        detection_hints=[
            "top", "best", "list", "show me", "ranked request with no explicit count/LIMIT",
        ],
    ),
}


def get_spec(ambiguity_type: AmbiguityType) -> AmbiguityTypeSpec:
    return TAXONOMY[ambiguity_type]
