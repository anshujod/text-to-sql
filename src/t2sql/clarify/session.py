"""Session state and turn orchestration.

`Session` is the stateful conversation memory `decide_clarification`
is deliberately pure with respect to -- resolved slots, how many
clarifications have been asked, the question/answer history, and which
defaults were silently-to-the-model-but-not-to-the-user applied. It wraps
(not replaces) the policy engine's `SessionState`: `to_policy_state()` is
the bridge.

`process_turn` ties the whole clarification pipeline together for one turn
of a conversation: parse the question, detect ambiguity, decide whether to
ask, render the
question if so, and record everything into the session. It does not call
an LLM or touch the database -- SQL generation/regeneration for the
resolved question is deliberately out of scope here (that's the real
pipeline's job); this module is the clarification loop's control flow.

Follow-up inheritance ("now show me the month before" keeps the metric
resolved on an earlier turn): `Session.effective_value` prefers whatever
*this* question's own parsed intent resolved, and only falls back to an
earlier resolution when the current question is silent on that slot --
never the other way around, so a follow-up that *does* re-specify a slot
overrides the earlier answer rather than being shadowed by it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from t2sql.clarify.detector import detect_ambiguities
from t2sql.clarify.divergence import DivergenceReport
from t2sql.clarify.intent import Intent, Slot, parse_intent
from t2sql.clarify.policy import ClarificationAction, ClarificationDecision, PolicyConfig, SessionState, decide_clarification
from t2sql.clarify.question import render_clarification_question
from t2sql.semantic.loader import load_semantic_layer
from t2sql.semantic.models import SemanticLayer


class ClarificationTurn(BaseModel):
    question: str
    slot: str
    options: list[str]
    answer: str | None = None


class Session(BaseModel):
    resolved_slots: dict[str, str] = Field(default_factory=dict)
    clarification_count: int = 0
    question_history: list[ClarificationTurn] = Field(default_factory=list)
    resolved_defaults: dict[str, str] = Field(default_factory=dict)

    def to_policy_state(self) -> SessionState:
        return SessionState(resolved_slots=dict(self.resolved_slots), clarification_count=self.clarification_count)

    def record_decision(self, decision: ClarificationDecision, question_text: str | None = None) -> None:
        """Every default the policy took is remembered here too -- Task
        3.5's "never silent" rule extends across turns, not just within one.
        """
        self.resolved_defaults.update(decision.defaults_applied)
        if decision.action == ClarificationAction.ASK:
            assert decision.slot is not None
            self.question_history.append(
                ClarificationTurn(question=question_text or "", slot=decision.slot, options=decision.options)
            )
            self.clarification_count += 1

    def record_answer(self, slot: str, answer: str) -> None:
        self.resolved_slots[slot] = answer
        for turn in reversed(self.question_history):
            if turn.slot == slot and turn.answer is None:
                turn.answer = answer
                return

    def effective_value(self, slot: str, intent_slot: Slot) -> str | None:
        """What to actually use for `slot`: this turn's own resolution if
        it has one, else whatever the session already resolved earlier.
        """
        if intent_slot.resolved is not None:
            return intent_slot.resolved
        return self.resolved_slots.get(slot)


_INTENT_SLOT_NAMES = ("metric", "entity", "dimensions", "filters", "time_range", "limit", "sort")


def effective_slots(intent: Intent, session: Session) -> dict[str, str | None]:
    """Every named Intent slot, resolved via Session.effective_value --
    the follow-up-inherits-the-resolved-metric behavior, generalized to
    every slot intent parsing produces, not just metric.
    """
    return {name: session.effective_value(name, getattr(intent, name)) for name in _INTENT_SLOT_NAMES}


def process_turn(
    question: str,
    session: Session,
    layer: SemanticLayer | None = None,
    policy_config: PolicyConfig | None = None,
    divergence_report: DivergenceReport | None = None,
) -> tuple[Intent, ClarificationDecision, str | None]:
    """One turn: parse -> detect -> decide -> (render if ASK) -> record.

    Returns (intent, decision, question_text). `question_text` is None
    unless `decision.action == ASK`.
    """
    layer = layer or load_semantic_layer()
    intent = parse_intent(question, layer=layer)
    ambiguities = detect_ambiguities(intent, layer)
    decision = decide_clarification(
        ambiguities, session.to_policy_state(), config=policy_config, divergence_report=divergence_report
    )

    question_text = render_clarification_question(decision, divergence_report) if decision.action == ClarificationAction.ASK else None
    session.record_decision(decision, question_text)
    return intent, decision, question_text
