"""Task 3.5 gate: decide_clarification is a pure function (no LLM, no DB)
covering every PLAN.md 3.5 rule with a dedicated test.
"""

from t2sql.clarify.detector import DetectedAmbiguity
from t2sql.clarify.divergence import DivergenceReport
from t2sql.clarify.policy import (
    ESCAPE_OPTION,
    ClarificationAction,
    ClarificationDecision,
    PolicyConfig,
    SessionState,
    decide_clarification,
)
from t2sql.clarify.taxonomy import AmbiguityType


def _ambiguity(
    ambiguity_type: AmbiguityType = AmbiguityType.METRIC,
    slot: str = "metric",
    candidates: list[str] | None = None,
    confidence: float = 0.85,
    source: str = "rule",
) -> DetectedAmbiguity:
    return DetectedAmbiguity(
        type=ambiguity_type,
        slot=slot,
        candidates=candidates or ["revenue_net", "order_count", "session_count"],
        confidence=confidence,
        source=source,
    )


def _divergence(score: float) -> DivergenceReport:
    return DivergenceReport(
        score=score,
        labels=["a", "b"],
        pairwise_matrix=[[0.0, score], [score, 0.0]],
        result_kind_per_interpretation={},
        sample_rows_per_interpretation={},
    )


# ---------------------------------------------------------------------------
# 1. Never ask when divergence is below threshold
# ---------------------------------------------------------------------------


def test_low_divergence_report_forces_proceed_even_with_high_confidence() -> None:
    ambiguity = _ambiguity(confidence=0.95)  # confidence alone would say "ask"
    decision = decide_clarification(
        [ambiguity], SessionState(), divergence_report=_divergence(0.05)
    )

    assert decision.action == ClarificationAction.PROCEED
    assert "metric" in decision.defaults_applied
    assert decision.disclosure_text


def test_low_confidence_without_divergence_report_forces_proceed() -> None:
    ambiguity = _ambiguity(confidence=0.1)
    decision = decide_clarification([ambiguity], SessionState())

    assert decision.action == ClarificationAction.PROCEED


def test_high_divergence_report_permits_asking() -> None:
    ambiguity = _ambiguity(confidence=0.5)  # confidence alone is borderline
    decision = decide_clarification(
        [ambiguity], SessionState(), divergence_report=_divergence(0.9)
    )

    assert decision.action == ClarificationAction.ASK


def test_no_detected_ambiguities_proceeds_trivially() -> None:
    decision = decide_clarification([], SessionState())

    assert decision.action == ClarificationAction.PROCEED
    assert decision.defaults_applied == {}
    assert decision.disclosure_text == []


# ---------------------------------------------------------------------------
# 2. At most one clarification question per call
# ---------------------------------------------------------------------------


def test_ask_decision_names_exactly_one_slot() -> None:
    ambiguities = [
        _ambiguity(AmbiguityType.METRIC, "metric", confidence=0.9),
        _ambiguity(AmbiguityType.ENTITY, "entity", candidates=["customers", "users"], confidence=0.8),
    ]
    decision = decide_clarification(ambiguities, SessionState())

    assert decision.action == ClarificationAction.ASK
    assert decision.slot is not None
    assert isinstance(decision.slot, str)


# ---------------------------------------------------------------------------
# 3. Multiple ambiguous slots -> ask about the highest-confidence one,
#    default the rest
# ---------------------------------------------------------------------------


def test_asks_about_the_highest_confidence_slot() -> None:
    low = _ambiguity(AmbiguityType.ENTITY, "entity", candidates=["customers", "users"], confidence=0.5)
    high = _ambiguity(AmbiguityType.METRIC, "metric", confidence=0.9)
    decision = decide_clarification([low, high], SessionState())  # order shouldn't matter

    assert decision.slot == "metric"


def test_non_asked_slots_are_defaulted_and_disclosed() -> None:
    low = _ambiguity(AmbiguityType.ENTITY, "entity", candidates=["customers", "users"], confidence=0.5)
    high = _ambiguity(AmbiguityType.METRIC, "metric", confidence=0.9)
    decision = decide_clarification([low, high], SessionState())

    assert decision.slot == "metric"
    assert "entity" in decision.defaults_applied
    assert decision.defaults_applied["entity"] == "customers"
    assert any("entity" in text.lower() for text in decision.disclosure_text)


def test_asked_slot_is_not_also_defaulted() -> None:
    low = _ambiguity(AmbiguityType.ENTITY, "entity", candidates=["customers", "users"], confidence=0.5)
    high = _ambiguity(AmbiguityType.METRIC, "metric", confidence=0.9)
    decision = decide_clarification([low, high], SessionState())

    assert "metric" not in decision.defaults_applied


def test_duplicate_slot_from_two_sources_is_deduped_to_higher_confidence() -> None:
    rule_hit = _ambiguity(AmbiguityType.METRIC, "metric", candidates=["revenue_net"], confidence=0.6, source="rule")
    sc_hit = _ambiguity(
        AmbiguityType.METRIC, "metric", candidates=["revenue_net", "unit_count"], confidence=0.9, source="self_consistency"
    )
    decision = decide_clarification([rule_hit, sc_hit], SessionState())

    assert decision.action == ClarificationAction.ASK
    assert decision.options[:-1] == ["revenue_net", "unit_count"]  # the higher-confidence candidate set won


# ---------------------------------------------------------------------------
# 4. Never re-ask a slot already resolved this session
# ---------------------------------------------------------------------------


def test_already_resolved_slot_is_excluded_entirely() -> None:
    ambiguity = _ambiguity(confidence=0.9)
    session = SessionState(resolved_slots={"metric": "revenue_net"})
    decision = decide_clarification([ambiguity], session)

    assert decision.action == ClarificationAction.PROCEED
    assert "metric" not in decision.defaults_applied  # it's resolved, not defaulted
    assert decision.disclosure_text == []


def test_resolved_slot_is_skipped_but_other_ambiguous_slot_still_asked() -> None:
    resolved = _ambiguity(AmbiguityType.METRIC, "metric", confidence=0.9)
    unresolved = _ambiguity(AmbiguityType.ENTITY, "entity", candidates=["customers", "users"], confidence=0.8)
    session = SessionState(resolved_slots={"metric": "revenue_net"})
    decision = decide_clarification([resolved, unresolved], session)

    assert decision.action == ClarificationAction.ASK
    assert decision.slot == "entity"


# ---------------------------------------------------------------------------
# 5. ASK always includes the escape option
# ---------------------------------------------------------------------------


def test_ask_options_include_escape_hatch() -> None:
    decision = decide_clarification([_ambiguity()], SessionState())

    assert decision.action == ClarificationAction.ASK
    assert decision.options[-1] == ESCAPE_OPTION


# ---------------------------------------------------------------------------
# 6. Hard budget: max clarifications per session
# ---------------------------------------------------------------------------


def test_budget_exhausted_forces_proceed_despite_high_divergence() -> None:
    ambiguity = _ambiguity(confidence=0.95)
    session = SessionState(clarification_count=2)
    decision = decide_clarification([ambiguity], session, divergence_report=_divergence(0.99))

    assert decision.action == ClarificationAction.PROCEED
    assert "budget" in decision.reason.lower()
    assert "metric" in decision.defaults_applied


def test_budget_boundary_one_below_max_still_allows_asking() -> None:
    ambiguity = _ambiguity(confidence=0.9)
    session = SessionState(clarification_count=1)
    config = PolicyConfig(max_clarifications_per_session=2)
    decision = decide_clarification([ambiguity], session, config=config)

    assert decision.action == ClarificationAction.ASK


def test_budget_is_configurable() -> None:
    ambiguity = _ambiguity(confidence=0.9)
    session = SessionState(clarification_count=1)
    config = PolicyConfig(max_clarifications_per_session=1)
    decision = decide_clarification([ambiguity], session, config=config)

    assert decision.action == ClarificationAction.PROCEED


# ---------------------------------------------------------------------------
# 7. Every default taken is recorded and disclosed, never silent
# ---------------------------------------------------------------------------


def test_every_defaulted_slot_has_a_disclosure_sentence() -> None:
    ambiguities = [
        _ambiguity(AmbiguityType.METRIC, "metric", confidence=0.2),
        _ambiguity(AmbiguityType.ENTITY, "entity", candidates=["customers", "users"], confidence=0.1),
    ]
    decision = decide_clarification(ambiguities, SessionState())

    assert decision.action == ClarificationAction.PROCEED
    assert set(decision.defaults_applied) == {"metric", "entity"}
    assert len(decision.disclosure_text) == 2


def test_custom_default_resolver_is_honored() -> None:
    ambiguity = _ambiguity(AmbiguityType.ENTITY, "entity", candidates=["customers", "users"], confidence=0.1)
    decision = decide_clarification(
        [ambiguity], SessionState(), default_resolver=lambda a: a.candidates[-1]
    )

    assert decision.defaults_applied["entity"] == "users"


# ---------------------------------------------------------------------------
# Purity / general sanity
# ---------------------------------------------------------------------------


def test_inputs_are_not_mutated() -> None:
    ambiguity = _ambiguity()
    session = SessionState(clarification_count=0, resolved_slots={})
    decide_clarification([ambiguity], session)

    assert session.clarification_count == 0
    assert session.resolved_slots == {}
    assert ambiguity.confidence == 0.85


def test_returns_a_clarification_decision() -> None:
    decision = decide_clarification([_ambiguity()], SessionState())
    assert isinstance(decision, ClarificationDecision)


def test_reason_is_always_populated() -> None:
    cases = [
        decide_clarification([], SessionState()),
        decide_clarification([_ambiguity(confidence=0.9)], SessionState()),
        decide_clarification([_ambiguity(confidence=0.1)], SessionState()),
        decide_clarification([_ambiguity(confidence=0.9)], SessionState(clarification_count=2)),
    ]
    for decision in cases:
        assert decision.reason.strip()
