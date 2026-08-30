"""Simulated user.

Automates the human side of the clarification loop so ablation
runner can drive the whole dataset unattended: given a `DatasetItem`, pick
one `GoldInterpretation` as that item's hidden true intent, then repeatedly
call `process_turn` and answer every ASK decision from the hidden intent
alone -- never from the question text, never from any other interpretation
-- until the session PROCEEDs or `max_turns` is exhausted.


Matching a hidden `GoldInterpretation` to one of a decision's offered
`options` (its own `label`, e.g. "revenue_net", vs. the taxonomy's
candidate vocabulary) is not always a literal string match -- an item with
several unstated axes has a compound label that only partly overlaps any one
slot's candidate strings. `_choose_option` tries three strategies in order
of confidence, falling through only when the previous one can't decide:

  1. exact match against the compound label
  2. the candidate's own underscore-split tokens appear as a contiguous
     run inside the label's tokens (catches "revenue_net" inside
     "revenue_net_excl_internal")
  3. token overlap between each candidate's humanized text and the
     interpretation's free-text `clarification_answer` (catches cases like
     "calendar_q4" vs. the taxonomy's "calendar_quarter", which share no
     literal substring but do share words once humanized)

If none of the three produces an unambiguous winner, that's not a bug to
paper over -- exactly this gets recorded as
`clarification_missed_target`, a real failure mode (the offered options
didn't cover the truth) worth reporting alongside accuracy.
"""

from __future__ import annotations

import random
import re
from enum import Enum

from pydantic import BaseModel, Field

from t2sql.clarify.policy import ESCAPE_OPTION, ClarificationAction
from t2sql.clarify.question import humanize_candidate
from t2sql.clarify.session import Session, process_turn
from t2sql.clarify.taxonomy import AmbiguityType
from t2sql.eval.dataset import DatasetItem, GoldInterpretation
from t2sql.semantic.loader import load_semantic_layer
from t2sql.semantic.models import SemanticLayer

DEFAULT_MAX_TURNS = 5

_STOPWORDS = {"the", "a", "an", "of", "and", "or", "to", "for", "in", "on", "at", "by", "with", "is", "are", "from"}


class UserStrategy(str, Enum):
    """How the simulated user answers an ASK decision."""

    ORACLE = "oracle"  # answers correctly from the hidden interpretation (the primary strategy)
    ALWAYS_DEFAULT = "always_default"  # adversarial: always takes the escape hatch, never actually answers
    VAGUE = "vague"  # adversarial: answers with a random offered option, ignoring the hidden truth


class SimulatedTurn(BaseModel):
    slot: str
    ambiguity_type: AmbiguityType | None = None
    options: list[str]
    answer: str
    missed_target: bool = False


class SimulatedConversationResult(BaseModel):
    id: str
    hidden_label: str
    strategy: UserStrategy
    turns: list[SimulatedTurn] = Field(default_factory=list)
    resolved_slots: dict[str, str] = Field(default_factory=dict)
    final_action: ClarificationAction
    clarification_missed_target: bool = False  # True if any turn's answer had to fall back (oracle only)
    hit_max_turns: bool = False


def _tokenize(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS}


def _label_tokens(label: str) -> list[str]:
    return [t for t in label.split("_") if t]


def _is_contiguous_subsequence(needle: list[str], haystack: list[str]) -> bool:
    n = len(needle)
    if n == 0 or n > len(haystack):
        return False
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


def _choose_option(hidden: GoldInterpretation, options: list[str], ambiguity_type: AmbiguityType | None) -> str | None:
    """Best-matching entry in `options` (excluding the escape hatch) for
    `hidden`, or None if no strategy above finds a confident winner --
    the caller records that as a missed target.
    """
    candidates = [o for o in options if o != ESCAPE_OPTION]
    if not candidates:
        return None

    for c in candidates:
        if c == hidden.label:
            return c

    hidden_tokens = _label_tokens(hidden.label)
    for c in candidates:
        if _is_contiguous_subsequence(_label_tokens(c), hidden_tokens):
            return c

    answer_tokens = _tokenize(hidden.clarification_answer)
    if answer_tokens:
        scored = sorted(
            ((len(_tokenize(humanize_candidate(c, ambiguity_type)) & answer_tokens), c) for c in candidates),
            key=lambda t: -t[0],
        )
        best_score, best_c = scored[0]
        runner_up_score = scored[1][0] if len(scored) > 1 else -1
        if best_score > 0 and best_score > runner_up_score:
            return best_c

    return None


def _pick_hidden_interpretation(item: DatasetItem, rng: random.Random) -> GoldInterpretation:
    return item.gold_sql[0] if len(item.gold_sql) == 1 else rng.choice(item.gold_sql)


def simulate_item(
    item: DatasetItem,
    seed: int = 0,
    strategy: UserStrategy = UserStrategy.ORACLE,
    max_turns: int = DEFAULT_MAX_TURNS,
    layer: SemanticLayer | None = None,
) -> SimulatedConversationResult:
    """Drive one dataset item's clarification loop to completion (or
    `max_turns`), answering every ASK purely from a single hidden
    `GoldInterpretation` -- deterministic given `seed` (both which
    interpretation is hidden, when there's more than one, and the `VAGUE`
    strategy's random pick).
    """
    layer = layer or load_semantic_layer()
    rng = random.Random(seed)
    hidden = _pick_hidden_interpretation(item, rng)

    session = Session()
    result = SimulatedConversationResult(
        id=item.id, hidden_label=hidden.label, strategy=strategy, final_action=ClarificationAction.PROCEED
    )

    decision = None
    for _ in range(max_turns):
        _intent, decision, _question_text = process_turn(item.question, session, layer=layer)
        if decision.action != ClarificationAction.ASK:
            break

        missed = False
        if strategy == UserStrategy.ALWAYS_DEFAULT:
            answer = ESCAPE_OPTION
        elif strategy == UserStrategy.VAGUE:
            answer = rng.choice(decision.options)
        else:
            match = _choose_option(hidden, decision.options, decision.ambiguity_type)
            missed = match is None
            answer = match or ESCAPE_OPTION

        assert decision.slot is not None
        result.turns.append(
            SimulatedTurn(
                slot=decision.slot,
                ambiguity_type=decision.ambiguity_type,
                options=decision.options,
                answer=answer,
                missed_target=missed,
            )
        )
        result.clarification_missed_target = result.clarification_missed_target or missed
        session.record_answer(decision.slot, answer)
    else:
        result.hit_max_turns = True

    result.final_action = decision.action if decision is not None else ClarificationAction.PROCEED
    result.resolved_slots = dict(session.resolved_slots)
    return result


def simulate_dataset(
    items: list[DatasetItem],
    seed: int = 0,
    strategy: UserStrategy = UserStrategy.ORACLE,
    max_turns: int = DEFAULT_MAX_TURNS,
    layer: SemanticLayer | None = None,
) -> list[SimulatedConversationResult]:
    layer = layer or load_semantic_layer()
    return [simulate_item(item, seed=seed, strategy=strategy, max_turns=max_turns, layer=layer) for item in items]
