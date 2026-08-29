"""Clarification policy engine (Task 3.5).

Decide: ask, or default and disclose? A pure function over its inputs --
no LLM call, no DB query, no mutation of anything passed in. The caller
(Task 3.6's Session wrapper) is responsible for acting on the decision:
incrementing `clarification_count`, recording `resolved_slots` once the
user answers, etc. This module only decides, it never remembers.

Inputs, per PLAN.md 3.5:
  - `detected_ambiguities`: the slots Task 3.2's rules and/or Task 3.3's
    self-consistency check flagged, each carrying its own `candidates` and
    `confidence`
  - `divergence_report`: Task 3.4's measured result-divergence for the
    highest-priority ambiguity's candidate interpretations, if it was
    computed. Optional -- Task 3.4 itself is allowed to skip execution
    when it's over cost budget, "and fall back to the 3.2/3.3 signal
    alone." When absent, the top ambiguity's own `confidence` stands in
    for the divergence score.
  - `session`: resolved slots from earlier in the conversation, and how
    many clarifications have already been asked
  - `config`: thresholds and the hard per-session budget

Rules (each has a dedicated unit test in tests/test_policy.py):
  1. Never ask when divergence is below threshold -- default and disclose.
  2. At most one clarification question per call.
  3. Multiple ambiguous slots -> ask about the highest-confidence one,
     default the rest.
  4. Never re-ask a slot already in `session.resolved_slots`.
  5. An ASK decision's `options` always includes an escape hatch to just
     take the default.
  6. Hard budget: at `max_clarifications_per_session`, default everything,
     no matter how high the divergence is.
  7. Every defaulted slot lands in both `defaults_applied` and
     `disclosure_text` -- never silent.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum

from pydantic import BaseModel, Field

from t2sql.clarify.detector import DetectedAmbiguity
from t2sql.clarify.divergence import DivergenceReport
from t2sql.clarify.taxonomy import AmbiguityType

ESCAPE_OPTION = "(just use a sensible default)"

DefaultResolver = Callable[[DetectedAmbiguity], str]


def _first_candidate_resolver(ambiguity: DetectedAmbiguity) -> str:
    return ambiguity.candidates[0] if ambiguity.candidates else "default"


class SessionState(BaseModel):
    """The slice of conversation state Task 3.5's rules need. Task 3.6
    owns the full Session object (question history, etc.) and is expected
    to carry one of these alongside it, updating it after each decision.
    """

    resolved_slots: dict[str, str] = Field(default_factory=dict)
    clarification_count: int = 0


class PolicyConfig(BaseModel):
    # Compared against divergence_report.score when given, else against
    # the top pending ambiguity's own `confidence` -- both are nominally
    # [0,1], but a rule/self-consistency confidence and a measured result
    # divergence aren't the same kind of number. Documented simplification,
    # not a claim they're equivalent; recalibrate independently if the
    # fallback path turns out to fire often in practice.
    divergence_threshold: float = 0.3
    max_clarifications_per_session: int = 2


class ClarificationAction(str, Enum):
    ASK = "ASK"
    PROCEED = "PROCEED"


class ClarificationDecision(BaseModel):
    action: ClarificationAction
    slot: str | None = None
    ambiguity_type: AmbiguityType | None = None
    options: list[str] = Field(default_factory=list)
    defaults_applied: dict[str, str] = Field(default_factory=dict)
    disclosure_text: list[str] = Field(default_factory=list)
    reason: str = ""


def _dedupe_by_slot(ambiguities: list[DetectedAmbiguity]) -> list[DetectedAmbiguity]:
    """Keep the highest-confidence detection per slot -- 3.2's rule
    detector and 3.3's self-consistency check can both flag the same slot
    independently, and asking about it twice (or defaulting it twice with
    two different resolvers) makes no sense.
    """
    best: dict[str, DetectedAmbiguity] = {}
    for a in ambiguities:
        current = best.get(a.slot)
        if current is None or a.confidence > current.confidence:
            best[a.slot] = a
    return list(best.values())


def _apply_defaults(
    ambiguities: list[DetectedAmbiguity], resolver: DefaultResolver
) -> tuple[dict[str, str], list[str]]:
    defaults: dict[str, str] = {}
    disclosures: list[str] = []
    for a in ambiguities:
        value = resolver(a)
        defaults[a.slot] = value
        disclosures.append(
            f"Assumed {a.type.value.lower()} = {value!r} (candidates considered: {list(a.candidates)})."
        )
    return defaults, disclosures


def decide_clarification(
    detected_ambiguities: list[DetectedAmbiguity],
    session: SessionState,
    config: PolicyConfig | None = None,
    divergence_report: DivergenceReport | None = None,
    default_resolver: DefaultResolver | None = None,
) -> ClarificationDecision:
    config = config or PolicyConfig()
    resolver = default_resolver or _first_candidate_resolver

    deduped = _dedupe_by_slot(detected_ambiguities)
    pending = [a for a in deduped if a.slot not in session.resolved_slots]

    if not pending:
        return ClarificationDecision(action=ClarificationAction.PROCEED, reason="no unresolved ambiguous slots")

    if session.clarification_count >= config.max_clarifications_per_session:
        defaults, disclosures = _apply_defaults(pending, resolver)
        return ClarificationDecision(
            action=ClarificationAction.PROCEED,
            defaults_applied=defaults,
            disclosure_text=disclosures,
            reason=(
                f"clarification budget exhausted "
                f"({session.clarification_count}/{config.max_clarifications_per_session})"
            ),
        )

    ranked = sorted(pending, key=lambda a: a.confidence, reverse=True)
    top, rest = ranked[0], ranked[1:]

    effective_score = divergence_report.score if divergence_report is not None else top.confidence
    if effective_score < config.divergence_threshold:
        defaults, disclosures = _apply_defaults(pending, resolver)
        return ClarificationDecision(
            action=ClarificationAction.PROCEED,
            defaults_applied=defaults,
            disclosure_text=disclosures,
            reason=f"divergence signal {effective_score:.2f} below threshold {config.divergence_threshold:.2f}",
        )

    defaults, disclosures = _apply_defaults(rest, resolver)
    return ClarificationDecision(
        action=ClarificationAction.ASK,
        slot=top.slot,
        ambiguity_type=top.type,
        options=[*top.candidates, ESCAPE_OPTION],
        defaults_applied=defaults,
        disclosure_text=disclosures,
        reason=f"divergence signal {effective_score:.2f} >= threshold {config.divergence_threshold:.2f}",
    )
