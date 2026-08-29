"""Task 4.2 (simulated user): $0, no LLM -- drives the real 3.1-3.6
clarification loop against real dataset items and the live DB-backed
detector, answering only from a hidden GoldInterpretation.
"""

from pathlib import Path

from t2sql.clarify.policy import ESCAPE_OPTION, ClarificationAction
from t2sql.clarify.taxonomy import AmbiguityType
from t2sql.eval.dataset import DatasetItem, GoldInterpretation, load_dataset
from t2sql.eval.simulated_user import (
    UserStrategy,
    _choose_option,
    simulate_dataset,
    simulate_item,
)

DEV_ITEMS = load_dataset(Path("data/dev.jsonl"))
AMBIGUOUS_ITEMS = {i.id: i for i in load_dataset(Path("data/ambiguous.jsonl"))}


# ---------------------------------------------------------------------------
# _choose_option: the three-tier matching strategy
# ---------------------------------------------------------------------------


def test_choose_option_exact_label_match() -> None:
    hidden = GoldInterpretation(sql="", label="revenue_net", clarification_answer="Revenue (net of refunds)")
    options = ["revenue_net", "order_count", ESCAPE_OPTION]
    assert _choose_option(hidden, options, AmbiguityType.METRIC) == "revenue_net"


def test_choose_option_contiguous_subsequence_of_compound_label() -> None:
    """A multi-label item's compound label ("revenue_net_excl_internal")
    contains a candidate's own tokens as a contiguous run.
    """
    hidden = GoldInterpretation(
        sql="", label="revenue_net_excl_internal", clarification_answer="Revenue (net), non-internal customers"
    )
    options = ["revenue_net", "order_count", ESCAPE_OPTION]
    assert _choose_option(hidden, options, AmbiguityType.METRIC) == "revenue_net"


def test_choose_option_falls_back_to_clarification_answer_token_overlap() -> None:
    """"calendar_q4" (the annotator's free-form compound-label component)
    shares no substring with the taxonomy's "calendar_quarter", but the
    clarification_answer text does share words with its humanized label.
    """
    hidden = GoldInterpretation(
        sql="", label="revenue_net_calendar_q4", clarification_answer="Revenue (net), calendar Q4 2025 (Oct-Dec)"
    )
    options = ["calendar_quarter", "trailing_90_days", ESCAPE_OPTION]
    assert _choose_option(hidden, options, AmbiguityType.TEMPORAL) == "calendar_quarter"


def test_choose_option_returns_none_when_only_escape_is_offered() -> None:
    hidden = GoldInterpretation(sql="", label="revenue_net", clarification_answer="Revenue (net of refunds)")
    assert _choose_option(hidden, [ESCAPE_OPTION], AmbiguityType.METRIC) is None


def test_choose_option_returns_none_on_genuine_zero_overlap() -> None:
    hidden = GoldInterpretation(sql="", label="", clarification_answer="")
    options = ["revenue_net", "order_count", ESCAPE_OPTION]
    assert _choose_option(hidden, options, AmbiguityType.METRIC) is None


# ---------------------------------------------------------------------------
# simulate_item
# ---------------------------------------------------------------------------


def test_unambiguous_item_completes_with_no_asks() -> None:
    item = DatasetItem(
        id="fake-unamb",
        question="How many product categories are there?",
        is_ambiguous=False,
        gold_sql=[GoldInterpretation(sql="SELECT COUNT(*) FROM categories")],
    )
    result = simulate_item(item, seed=0)
    assert result.turns == []
    assert result.final_action == ClarificationAction.PROCEED
    assert result.hit_max_turns is False
    assert result.clarification_missed_target is False


def test_oracle_resolves_a_real_ambiguous_item_without_missing_the_metric_turn() -> None:
    """amb-001 ("Who is our best customer?") has 3 gold interpretations;
    whichever one seed=0 hides, the oracle must answer the metric turn
    with that exact label -- this is the core Task 4.2 behavior, checked
    against the live rule detector, not a hand-built decision.
    """
    result = simulate_item(AMBIGUOUS_ITEMS["amb-001"], seed=0)
    metric_turns = [t for t in result.turns if t.slot == "metric"]
    assert len(metric_turns) == 1
    assert metric_turns[0].answer == result.hidden_label
    assert metric_turns[0].missed_target is False
    assert result.final_action == ClarificationAction.PROCEED
    assert result.hit_max_turns is False


def test_oracle_resolves_both_axes_of_a_multi_label_item() -> None:
    """amb-009 is METRIC+TEMPORAL ambiguous with a compound label -- both
    turns must resolve to the hidden interpretation's own components.
    """
    result = simulate_item(AMBIGUOUS_ITEMS["amb-009"], seed=0)
    by_slot = {t.slot: t for t in result.turns}
    assert "metric" in by_slot and "time_range" in by_slot
    assert by_slot["metric"].missed_target is False
    assert by_slot["time_range"].missed_target is False
    assert result.hidden_label.startswith(by_slot["metric"].answer)
    assert result.final_action == ClarificationAction.PROCEED


def test_always_default_strategy_never_answers_from_the_hidden_intent() -> None:
    result = simulate_item(AMBIGUOUS_ITEMS["amb-001"], seed=0, strategy=UserStrategy.ALWAYS_DEFAULT)
    assert result.turns  # this item does get asked something
    assert all(t.answer == ESCAPE_OPTION for t in result.turns)
    assert result.clarification_missed_target is False  # not attempting to match isn't "missing" the target
    assert result.final_action == ClarificationAction.PROCEED


def test_vague_strategy_always_answers_with_an_offered_option() -> None:
    result = simulate_item(AMBIGUOUS_ITEMS["amb-001"], seed=0, strategy=UserStrategy.VAGUE)
    assert result.turns
    assert all(t.answer in t.options for t in result.turns)


def test_vague_strategy_is_deterministic_given_the_same_seed() -> None:
    a = simulate_item(AMBIGUOUS_ITEMS["amb-001"], seed=7, strategy=UserStrategy.VAGUE)
    b = simulate_item(AMBIGUOUS_ITEMS["amb-001"], seed=7, strategy=UserStrategy.VAGUE)
    assert [t.answer for t in a.turns] == [t.answer for t in b.turns]


def test_simulate_item_never_exceeds_max_turns_before_giving_up() -> None:
    result = simulate_item(AMBIGUOUS_ITEMS["amb-001"], seed=0, max_turns=1)
    assert len(result.turns) <= 1
    if len(result.turns) == 1:
        assert result.hit_max_turns is True


# ---------------------------------------------------------------------------
# The actual PLAN.md 4.2 gate
# ---------------------------------------------------------------------------


def test_simulated_user_completes_20_dev_items_end_to_end_with_no_manual_input() -> None:
    results = simulate_dataset(DEV_ITEMS[:20], seed=0)
    assert len(results) == 20
    for r in results:
        assert r.final_action == ClarificationAction.PROCEED
        assert r.hit_max_turns is False
