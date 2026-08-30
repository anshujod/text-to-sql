"""Question rendering: template-based, deterministic, no LLM."""

import pytest

from t2sql.clarify.divergence import DivergenceReport, ResultKind
from t2sql.clarify.policy import ESCAPE_OPTION, ClarificationAction, ClarificationDecision
from t2sql.clarify.question import humanize_candidate, render_clarification_question
from t2sql.clarify.taxonomy import AmbiguityType


def _ask_decision(options: list[str], ambiguity_type: AmbiguityType = AmbiguityType.METRIC) -> ClarificationDecision:
    return ClarificationDecision(
        action=ClarificationAction.ASK, slot="metric", ambiguity_type=ambiguity_type, options=[*options, ESCAPE_OPTION]
    )


def _ranked_report(label_a: str, label_b: str, ids_a: list, ids_b: list, id_column: str = "customer_id") -> DivergenceReport:
    score = 1.0 - len(set(ids_a) & set(ids_b)) / min(len(ids_a), len(ids_b))
    return DivergenceReport(
        score=score,
        labels=[label_a, label_b],
        pairwise_matrix=[[0.0, score], [score, 0.0]],
        result_kind_per_interpretation={label_a: ResultKind.RANKED_LIST, label_b: ResultKind.RANKED_LIST},
        sample_rows_per_interpretation={label_a: [[i] for i in ids_a], label_b: [[i] for i in ids_b]},
        columns_per_interpretation={label_a: [id_column], label_b: [id_column]},
    )


def test_humanize_known_metric_candidate() -> None:
    assert humanize_candidate("revenue_net", AmbiguityType.METRIC) == "revenue (net of refunds)"


def test_humanize_unknown_candidate_falls_back_to_snake_case() -> None:
    assert humanize_candidate("some_new_metric", AmbiguityType.METRIC) == "some new metric"


def test_humanize_escape_option_is_passed_through() -> None:
    assert humanize_candidate(ESCAPE_OPTION, AmbiguityType.METRIC) == ESCAPE_OPTION


def test_render_raises_for_a_proceed_decision() -> None:
    decision = ClarificationDecision(action=ClarificationAction.PROCEED)
    with pytest.raises(ValueError):
        render_clarification_question(decision)


def test_render_two_options_joined_with_or() -> None:
    decision = _ask_decision(["customers", "users"], AmbiguityType.ENTITY)
    text = render_clarification_question(decision)
    assert text == "Customer entities or individual login accounts?"


def test_render_three_options_uses_oxford_style_list() -> None:
    decision = _ask_decision(["revenue_net", "order_count", "session_count"])
    text = render_clarification_question(decision)
    assert text.startswith("Revenue (net of refunds), number of orders, or number of visits?")


def test_render_without_divergence_report_has_no_consequence_clause() -> None:
    decision = _ask_decision(["revenue_net", "order_count"])
    text = render_clarification_question(decision)
    assert text.count("?") == 1  # just the options sentence, nothing appended


def test_render_includes_consequence_clause_for_ranked_lists() -> None:
    decision = _ask_decision(["revenue_net", "order_count"])
    report = _ranked_report("revenue_net", "order_count", [1, 2, 3], [4, 5, 6])
    text = render_clarification_question(decision, report)

    assert "different top-3 lists" in text
    assert "no customers appear in both" in text


def test_render_partial_overlap_reports_the_count() -> None:
    decision = _ask_decision(["revenue_net", "order_count"])
    report = _ranked_report("revenue_net", "order_count", [1, 2, 3, 4, 5], [3, 4, 5, 6, 7])
    text = render_clarification_question(decision, report)

    assert "only 3 customers appear in both" in text


def test_render_uses_column_name_to_pick_the_noun() -> None:
    decision = _ask_decision(["revenue_net", "order_count"])
    report = _ranked_report("revenue_net", "order_count", [1, 2], [3, 4], id_column="product_id")
    text = render_clarification_question(decision, report)

    assert "products" in text


def test_render_scalar_consequence_clause_shows_both_values() -> None:
    decision = _ask_decision(["revenue_gross", "revenue_net"])
    report = DivergenceReport(
        score=0.1,
        labels=["revenue_gross", "revenue_net"],
        pairwise_matrix=[[0.0, 0.1], [0.1, 0.0]],
        result_kind_per_interpretation={"revenue_gross": ResultKind.SCALAR, "revenue_net": ResultKind.SCALAR},
        sample_rows_per_interpretation={"revenue_gross": [[100000]], "revenue_net": [[92000]]},
    )
    text = render_clarification_question(decision, report)

    assert "100000" in text and "92000" in text


def test_render_ignores_pairs_not_among_the_offered_options() -> None:
    """The divergence report may cover more labels than the decision is
    actually asking about (e.g. it was computed for a superset) -- only
    pairs among decision.options should ever drive the consequence clause.
    """
    decision = _ask_decision(["revenue_net", "order_count"])
    report = _ranked_report("revenue_net", "session_count", [1, 2], [3, 4])  # session_count isn't offered
    text = render_clarification_question(decision, report)

    assert text.count("?") == 1  # no consequence clause -- no comparable pair among the offered options
