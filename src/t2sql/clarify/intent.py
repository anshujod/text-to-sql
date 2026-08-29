"""Structured intent parser (Task 3.1).

Parses a question into slots *before* any SQL gets generated. Each slot is
resolved against the semantic layer the same way for every slot type: scan
the question for every candidate whose vocabulary (metric/entity synonyms
from metrics.yaml/entities.yaml, or a small inline keyword list for the
slots the semantic layer doesn't model) appears in the text, then:

  - exactly 1 candidate  -> resolved to that candidate
  - 0 candidates         -> resolved=None, reason explains nothing matched
  - 2+ candidates        -> resolved=None, reason lists what matched

That "2+ candidates -> unresolved" rule is deliberate, not a bug: it's
exactly how "best" surfaces `candidates=[revenue_net, order_count,
session_count]` per PLAN.md 3.1 -- the multiple matches *are* the ambiguity
signal Task 3.2's rule-based detector consumes downstream.

`dimensions` and `filters` can legitimately have more than one true match
at once (a question can filter on both "cancelled" and "refunded"), so
their multi-candidate case isn't "ambiguous" in the same sense -- see each
slot's reason text.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from t2sql.semantic.loader import load_semantic_layer
from t2sql.semantic.models import SemanticLayer


class Slot(BaseModel):
    candidates: list[str] = Field(default_factory=list)
    resolved: str | None = None
    reason: str | None = None


class Intent(BaseModel):
    question: str
    metric: Slot
    dimensions: Slot
    filters: Slot
    time_range: Slot
    limit: Slot
    sort: Slot
    entity: Slot


# ---------------------------------------------------------------------------
# Vocabulary for the slots the semantic layer doesn't already model
# (metric/entity use metrics.yaml/entities.yaml `synonyms` instead).
# ---------------------------------------------------------------------------

FILTER_TERMS: dict[str, list[str]] = {
    "cancelled_orders": ["cancelled", "canceled"],
    "returned_orders": ["returned", "return"],
    "refunds": ["refund", "refunded", "refunds"],
    "internal_accounts": ["internal", "staff", "employee", "test account"],
    "soft_deleted": ["deleted", "discontinued", "since-deleted"],
    "active_only": ["active", "non-internal", "excluding staff", "excluding internal"],
}

DIMENSION_ALIASES: dict[str, str] = {
    "category": "category",
    "categories": "category",
    "month": "month",
    "week": "week",
    "day": "day",
    "year": "year",
    "quarter": "quarter",
    "customer": "customer",
    "customers": "customer",
    "product": "product",
    "products": "product",
    "status": "status",
    "country": "country",
    "currency": "currency",
}

# (phrases that trigger this reading, candidate readings if matched). Order
# matters -- first match wins, mirroring taxonomy.py's TEMPORAL detection
# hints ("last month", "this quarter", "recently", ...).
TIME_PHRASE_CANDIDATES: list[tuple[list[str], list[str]]] = [
    (["last month", "past month", "previous month"], ["calendar_month", "trailing_30_days"]),
    (["last week", "past week", "previous week"], ["calendar_week", "trailing_7_days"]),
    (["this quarter", "current quarter", "last quarter"], ["calendar_quarter", "trailing_90_days"]),
    (["year to date", "ytd", "this year"], ["calendar_year_to_date"]),
    (["yesterday"], ["trailing_24_hours"]),
    (["today"], ["calendar_day"]),
    (["recently", "lately"], ["trailing_7_days", "trailing_30_days"]),
]

_LIMIT_RE = re.compile(r"\b(?:top|first|bottom|limit|the)\s+(\d+)\b", re.IGNORECASE)
_RANKED_NO_COUNT_TERMS = ["top", "best", "highest", "most", "greatest", "largest", "leading"]
_DESC_SORT_TERMS = ["top", "best", "highest", "most", "greatest", "largest", "biggest"]
_ASC_SORT_TERMS = ["bottom", "worst", "lowest", "least", "smallest"]
_GROUPING_RE = re.compile(r"\b(?:by|per|for each)\s+([a-z][a-z ]{0,20}?)(?=[.,?]|$)", re.IGNORECASE)


def _contains_term(question_lower: str, term: str) -> bool:
    term_lower = term.lower()
    if " " in term_lower or "-" in term_lower:
        return term_lower in question_lower
    return re.search(rf"\b{re.escape(term_lower)}\b", question_lower) is not None


def _match_vocab(question_lower: str, vocab: dict[str, list[str]]) -> list[str]:
    """Every key in `vocab` whose term list has a hit in the question, in vocab order."""
    return [name for name, terms in vocab.items() if any(_contains_term(question_lower, t) for t in terms)]


def _resolve(candidates: list[str], empty_reason: str, multi_reason: str | None = None) -> Slot:
    if len(candidates) == 1:
        return Slot(candidates=candidates, resolved=candidates[0])
    if not candidates:
        return Slot(candidates=[], resolved=None, reason=empty_reason)
    reason = multi_reason or f"{len(candidates)} candidates matched: {', '.join(candidates)}"
    return Slot(candidates=candidates, resolved=None, reason=reason)


def _extract_dimensions(question: str) -> list[str]:
    found: list[str] = []
    for m in _GROUPING_RE.finditer(question.lower()):
        phrase = m.group(1).strip()
        canonical = DIMENSION_ALIASES.get(phrase) or DIMENSION_ALIASES.get(phrase.rstrip("s"))
        term = canonical or phrase
        if term not in found:
            found.append(term)
    return found


def _extract_time_range(question_lower: str) -> list[str]:
    for phrases, candidates in TIME_PHRASE_CANDIDATES:
        if any(_contains_term(question_lower, p) for p in phrases):
            return candidates
    return []


def _extract_limit(question_lower: str) -> Slot:
    m = _LIMIT_RE.search(question_lower)
    if m:
        n = m.group(1)
        return Slot(candidates=[n], resolved=n)
    if any(_contains_term(question_lower, t) for t in _RANKED_NO_COUNT_TERMS):
        return Slot(candidates=[], resolved=None, reason="ranked request with no explicit row count")
    return Slot(candidates=[], resolved=None, reason="no ranking/limit language detected")


def _extract_sort(question_lower: str) -> Slot:
    has_desc = any(_contains_term(question_lower, t) for t in _DESC_SORT_TERMS)
    has_asc = any(_contains_term(question_lower, t) for t in _ASC_SORT_TERMS)
    if has_desc and has_asc:
        return Slot(candidates=["desc", "asc"], resolved=None, reason="both ascending and descending cues present")
    if has_desc:
        return Slot(candidates=["desc"], resolved="desc")
    if has_asc:
        return Slot(candidates=["asc"], resolved="asc")
    return Slot(candidates=[], resolved=None, reason="no sort-direction language detected")


def parse_intent(question: str, layer: SemanticLayer | None = None) -> Intent:
    if layer is None:
        layer = load_semantic_layer()

    q_lower = question.lower()

    metric_candidates = _match_vocab(q_lower, {name: m.synonyms for name, m in layer.metrics.items()})
    entity_candidates = _match_vocab(q_lower, {name: e.synonyms for name, e in layer.entities.items()})
    filter_candidates = _match_vocab(q_lower, FILTER_TERMS)
    dimension_candidates = _extract_dimensions(question)
    time_candidates = _extract_time_range(q_lower)

    return Intent(
        question=question,
        metric=_resolve(
            metric_candidates,
            empty_reason="no metric/success-term language detected",
            multi_reason=(
                f"term matches {len(metric_candidates)} metric synonyms: {', '.join(metric_candidates)}"
                if len(metric_candidates) > 1
                else None
            ),
        ),
        entity=_resolve(
            entity_candidates,
            empty_reason="no entity-referring noun detected",
            multi_reason=(
                f"term matches {len(entity_candidates)} entities with possibly different grain: "
                f"{', '.join(entity_candidates)}"
                if len(entity_candidates) > 1
                else None
            ),
        ),
        dimensions=_resolve(
            dimension_candidates,
            empty_reason="no grouping language ('by X' / 'per X') detected",
            multi_reason=(
                f"question requests grouping by {len(dimension_candidates)} dimensions: "
                f"{', '.join(dimension_candidates)} (not ambiguity -- all requested)"
                if len(dimension_candidates) > 1
                else None
            ),
        ),
        filters=_resolve(
            filter_candidates,
            empty_reason="no scope/filter language detected",
            multi_reason=(
                f"question mentions {len(filter_candidates)} filter concepts: {', '.join(filter_candidates)} "
                "(not ambiguity -- all mentioned)"
                if len(filter_candidates) > 1
                else None
            ),
        ),
        time_range=_resolve(
            time_candidates,
            empty_reason="no relative or explicit time expression detected",
            multi_reason=(
                f"relative time phrase has {len(time_candidates)} common readings: {', '.join(time_candidates)}"
                if len(time_candidates) > 1
                else None
            ),
        ),
        limit=_extract_limit(q_lower),
        sort=_extract_sort(q_lower),
    )
