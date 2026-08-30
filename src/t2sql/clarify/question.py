"""Question generation.

Renders an ASK `ClarificationDecision` as a natural question with concrete,
consequence-bearing options -- template-based, not LLM-generated, so this
is free and deterministic. Target shape:

    "Revenue (total spend), number of orders, or number of visits? Revenue
    and order count give quite different top-10 lists here -- only 3
    customers appear in both."

The first sentence lists the candidate readings in plain language (see
LABEL_MAPS below -- a small, hand-curated vocabulary since this project's
candidate set is fixed and known, not open-ended). The second sentence is
pulled from the actual `DivergenceReport`: the most-diverging pair among
the offered options, described in whatever way fits its result shape (a
row-overlap count for ranked lists, a value-vs-value comparison for
scalars). Concreteness, not LLM fluency, is the point here.
"""

from __future__ import annotations

from t2sql.clarify.divergence import DivergenceReport, ResultKind
from t2sql.clarify.policy import ESCAPE_OPTION, ClarificationAction, ClarificationDecision
from t2sql.clarify.taxonomy import AmbiguityType

METRIC_LABELS: dict[str, str] = {
    "revenue_gross": "gross revenue (before refunds)",
    "revenue_net": "revenue (net of refunds)",
    "order_count": "number of orders",
    "unit_count": "number of units sold",
    "aov": "average order value",
    "session_count": "number of visits",
    "distinct_active_customers": "number of distinct active customers",
}

ENTITY_LABELS: dict[str, str] = {
    "customers": "customer entities",
    "users": "individual login accounts",
    "addresses": "addresses",
    "categories": "product categories",
    "products": "products",
    "orders": "orders",
    "order_items": "order line items",
    "payments": "payments",
    "refunds": "refunds",
    "sessions": "browsing sessions",
}

GRAIN_LABELS: dict[str, str] = {
    "per_order": "per order",
    "per_customer": "per customer, averaged across each customer's own total",
    "per_month": "per month, averaged across each month's own total",
}

TEMPORAL_LABELS: dict[str, str] = {
    "calendar_month": "the calendar month",
    "trailing_30_days": "the trailing 30 days",
    "calendar_week": "the calendar week",
    "trailing_7_days": "the trailing 7 days",
    "calendar_quarter": "the calendar quarter",
    "trailing_90_days": "the trailing 90 days",
    "calendar_year_to_date": "year to date",
    "trailing_24_hours": "the last 24 hours",
    "calendar_day": "today",
}

COMPARISON_LABELS: dict[str, str] = {
    "month_over_month": "month-over-month",
    "trailing_period_average": "vs. the trailing period average",
}

LABEL_MAPS: dict[AmbiguityType, dict[str, str]] = {
    AmbiguityType.METRIC: METRIC_LABELS,
    AmbiguityType.SCOPE: METRIC_LABELS,  # SCOPE candidates are the metrics whose default filters are unstated
    AmbiguityType.ENTITY: ENTITY_LABELS,
    AmbiguityType.GRAIN: GRAIN_LABELS,
    AmbiguityType.TEMPORAL: TEMPORAL_LABELS,
    AmbiguityType.COMPARISON: COMPARISON_LABELS,
}


def humanize_candidate(candidate: str, ambiguity_type: AmbiguityType | None) -> str:
    if candidate == ESCAPE_OPTION:
        return candidate
    label_map = LABEL_MAPS.get(ambiguity_type, {}) if ambiguity_type else {}
    return label_map.get(candidate, candidate.replace("_", " "))


def _join_natural(labels: list[str]) -> str:
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} or {labels[1]}"
    return ", ".join(labels[:-1]) + f", or {labels[-1]}"


def _most_diverging_pair(
    candidate_labels: list[str], report: DivergenceReport
) -> tuple[str, str, float] | None:
    relevant = [label for label in report.labels if label in candidate_labels]
    if len(relevant) < 2:
        return None
    index = {label: i for i, label in enumerate(report.labels)}
    best: tuple[str, str, float] | None = None
    for i in range(len(relevant)):
        for j in range(i + 1, len(relevant)):
            a, b = relevant[i], relevant[j]
            score = report.pairwise_matrix[index[a]][index[b]]
            if best is None or score > best[2]:
                best = (a, b, score)
    return best


def _entity_noun(column_name: str | None) -> str:
    """"customer_id" -> "customers", "id" (no useful prefix) -> "rows"."""
    if not column_name:
        return "rows"
    name = column_name.lower()
    if name.endswith("_id"):
        name = name[: -len("_id")]
    elif name == "id":
        return "rows"
    if not name:
        return "rows"
    return name if name.endswith("s") else f"{name}s"


def _consequence_clause(decision: ClarificationDecision, report: DivergenceReport | None) -> str | None:
    if report is None:
        return None
    candidate_labels = [o for o in decision.options if o != ESCAPE_OPTION]
    pair = _most_diverging_pair(candidate_labels, report)
    if pair is None or pair[2] <= 0:
        return None
    a, b, _score = pair

    label_a = humanize_candidate(a, decision.ambiguity_type).capitalize()
    label_b = humanize_candidate(b, decision.ambiguity_type)
    kind = report.result_kind_per_interpretation.get(a)
    rows_a = report.sample_rows_per_interpretation.get(a, [])
    rows_b = report.sample_rows_per_interpretation.get(b, [])

    if kind == ResultKind.RANKED_LIST and rows_a and rows_b:
        ids_a = {row[0] for row in rows_a}
        ids_b = {row[0] for row in rows_b}
        n = max(len(ids_a), len(ids_b))
        overlap = len(ids_a & ids_b)
        columns_a = report.columns_per_interpretation.get(a) or []
        noun = _entity_noun(columns_a[0] if columns_a else None)
        overlap_clause = f"no {noun} appear in both" if overlap == 0 else f"only {overlap} {noun} appear in both"
        return f"{label_a} and {label_b} give quite different top-{n} lists here -- {overlap_clause}."
    if kind == ResultKind.SCALAR and rows_a and rows_b:
        return f"{label_a} and {label_b} give different totals here -- {rows_a[0][0]} vs. {rows_b[0][0]}."
    if kind in (ResultKind.MULTI_VALUE, ResultKind.TIME_SERIES):
        return f"{label_a} and {label_b} lead to noticeably different results here."
    return None


def render_clarification_question(
    decision: ClarificationDecision, divergence_report: DivergenceReport | None = None
) -> str:
    """Only valid for an ASK decision -- raises otherwise, since there's no
    question to render for a PROCEED."""
    if decision.action != ClarificationAction.ASK:
        raise ValueError("render_clarification_question requires an ASK decision")

    candidate_options = [o for o in decision.options if o != ESCAPE_OPTION]
    labels = [humanize_candidate(o, decision.ambiguity_type) for o in candidate_options]
    options_sentence = _join_natural(labels)
    question = f"{options_sentence[:1].upper()}{options_sentence[1:]}?"

    clause = _consequence_clause(decision, divergence_report)
    if clause:
        question = f"{question} {clause}"
    return question
