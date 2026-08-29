"""Task 3.6 gate: a scripted 3-turn conversation resolves a metric on turn
1 and does not re-ask it on turns 2 and 3. Uses real parse_intent/
detect_ambiguities against the live DB (no LLM) -- same $0 pattern as
Tasks 3.1-3.5's dev-set gates.
"""

from t2sql.clarify.intent import Slot
from t2sql.clarify.policy import ClarificationAction
from t2sql.clarify.session import ClarificationTurn, Session, effective_slots, process_turn
from t2sql.clarify.taxonomy import AmbiguityType
from t2sql.semantic.loader import load_semantic_layer

_LAYER = load_semantic_layer()


# ---------------------------------------------------------------------------
# Session: record_decision / record_answer / effective_value
# ---------------------------------------------------------------------------


def test_new_session_starts_empty() -> None:
    session = Session()
    assert session.resolved_slots == {}
    assert session.clarification_count == 0
    assert session.question_history == []
    assert session.resolved_defaults == {}


def test_record_ask_decision_appends_history_and_increments_count() -> None:
    from t2sql.clarify.policy import ClarificationDecision

    session = Session()
    decision = ClarificationDecision(
        action=ClarificationAction.ASK, slot="metric", options=["a", "b"], ambiguity_type=AmbiguityType.METRIC
    )
    session.record_decision(decision, question_text="Which metric?")

    assert session.clarification_count == 1
    assert len(session.question_history) == 1
    assert session.question_history[0] == ClarificationTurn(
        question="Which metric?", slot="metric", options=["a", "b"], answer=None
    )


def test_record_proceed_decision_does_not_increment_count() -> None:
    from t2sql.clarify.policy import ClarificationDecision

    session = Session()
    decision = ClarificationDecision(action=ClarificationAction.PROCEED, defaults_applied={"metric": "revenue_net"})
    session.record_decision(decision)

    assert session.clarification_count == 0
    assert session.question_history == []


def test_record_decision_merges_defaults_into_resolved_defaults() -> None:
    from t2sql.clarify.policy import ClarificationDecision

    session = Session()
    session.record_decision(ClarificationDecision(action=ClarificationAction.PROCEED, defaults_applied={"filters": "revenue_net"}))
    session.record_decision(ClarificationDecision(action=ClarificationAction.PROCEED, defaults_applied={"limit": "10"}))

    assert session.resolved_defaults == {"filters": "revenue_net", "limit": "10"}


def test_record_answer_binds_resolved_slots() -> None:
    session = Session()
    session.record_answer("metric", "revenue_net")

    assert session.resolved_slots == {"metric": "revenue_net"}


def test_record_answer_fills_in_the_matching_open_turn() -> None:
    session = Session()
    session.question_history.append(ClarificationTurn(question="Which metric?", slot="metric", options=["a", "b"]))
    session.record_answer("metric", "revenue_net")

    assert session.question_history[0].answer == "revenue_net"


def test_effective_value_prefers_this_turns_own_resolution() -> None:
    session = Session(resolved_slots={"metric": "order_count"})
    slot = Slot(candidates=["revenue_net"], resolved="revenue_net")

    assert session.effective_value("metric", slot) == "revenue_net"


def test_effective_value_falls_back_to_session_when_unresolved_this_turn() -> None:
    session = Session(resolved_slots={"metric": "revenue_net"})
    slot = Slot(candidates=[], resolved=None, reason="no metric language detected")

    assert session.effective_value("metric", slot) == "revenue_net"


def test_effective_value_is_none_when_neither_resolved() -> None:
    session = Session()
    slot = Slot(candidates=[], resolved=None, reason="no metric language detected")

    assert session.effective_value("metric", slot) is None


def test_effective_slots_covers_every_named_intent_slot() -> None:
    from t2sql.clarify.intent import parse_intent

    session = Session(resolved_slots={"metric": "revenue_net"})
    intent = parse_intent("How many product categories are there?", layer=_LAYER)
    slots = effective_slots(intent, session)

    assert set(slots) == {"metric", "entity", "dimensions", "filters", "time_range", "limit", "sort"}
    assert slots["metric"] == "revenue_net"  # inherited from the session, question itself has no metric language


# ---------------------------------------------------------------------------
# process_turn
# ---------------------------------------------------------------------------


def test_process_turn_ask_records_the_question() -> None:
    session = Session()
    intent, decision, question_text = process_turn("Who is our best customer?", session, layer=_LAYER)

    assert decision.action == ClarificationAction.ASK
    assert question_text is not None
    assert session.clarification_count == 1
    assert session.question_history[-1].question == question_text


def test_process_turn_proceed_does_not_produce_question_text() -> None:
    session = Session()
    intent, decision, question_text = process_turn("How many product categories are there?", session, layer=_LAYER)

    assert decision.action == ClarificationAction.PROCEED
    assert question_text is None
    assert session.clarification_count == 0


# ---------------------------------------------------------------------------
# The actual PLAN.md 3.6 gate
# ---------------------------------------------------------------------------


def test_scripted_three_turn_conversation_resolves_metric_once_and_never_reasks() -> None:
    session = Session()

    # Turn 1: bare "best" is genuinely METRIC-ambiguous -- must ask.
    _intent1, decision1, question1 = process_turn("Who is our best customer?", session, layer=_LAYER)
    assert decision1.action == ClarificationAction.ASK
    assert decision1.slot == "metric"
    assert question1 is not None
    session.record_answer("metric", "revenue_net")

    # Turn 2: same "best" language would trigger METRIC again from a cold
    # start -- verified separately that detect_ambiguities *does* still
    # flag it here -- but the session must not re-ask about *metric*
    # specifically, since it's already resolved.
    _intent2, decision2, _question2 = process_turn("Who is our best customer this month?", session, layer=_LAYER)
    assert decision2.slot != "metric"
    if decision2.action == ClarificationAction.ASK:
        session.record_answer(decision2.slot, decision2.options[0])

    # Turn 3: same again.
    _intent3, decision3, _question3 = process_turn("Who is our best customer overall?", session, layer=_LAYER)
    assert decision3.slot != "metric"

    # Metric was asked exactly once, across all three turns.
    metric_turns = [t for t in session.question_history if t.slot == "metric"]
    assert len(metric_turns) == 1
    assert session.resolved_slots["metric"] == "revenue_net"


def test_followup_question_with_no_metric_language_inherits_resolved_metric() -> None:
    """'now show me the month before' style follow-up: no metric words at
    all, but the session already resolved one."""
    from t2sql.clarify.intent import parse_intent

    session = Session()
    session.record_answer("metric", "revenue_net")

    intent = parse_intent("Now show me the month before.", layer=_LAYER)
    assert intent.metric.resolved is None  # confirms the question itself carries no metric signal

    slots = effective_slots(intent, session)
    assert slots["metric"] == "revenue_net"
