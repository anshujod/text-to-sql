"""Task 4.3 -- the ablation runner.

Six configs (PLAN.md's table), one pipeline, sharing every LLM call that's
genuinely identical across configs so the real dollar cost stays close to
"one item's worth of work," not "six times one item's worth of work":

  baseline               no detection at all
  llm_judge              one "is this ambiguous?" LLM call gates the ask
  rules_only             3.2's free rule detector gates the ask
  self_consistency_only  3.3's N=5 sampling gates the ask
  hybrid_no_gate         3.2 + 3.3 together gate the ask
  full                   3.2 + 3.3, plus 3.4's (free, DB-only) divergence
                         gate overriding the ask when the candidates don't
                         actually disagree on results

Sharing, per item:
  - the "unclarified" baseline generation (1 call) is reused by every
    config whose own detection doesn't end up asking anything -- if a
    config doesn't ask, its final SQL *is* baseline's, there's no reason
    to regenerate it
  - the 5 self-consistency samples (Task 3.3) are generated once and
    reused by all three configs that need them (self_consistency_only,
    hybrid_no_gate, full) instead of 15 separate calls
  - `full`'s divergence gate (3.4) executes those same 5 samples' distinct
    SQL variants against the DB -- no extra LLM calls, per divergence.py's
    own "pure function of already-generated SQL" design
  - a config only pays for a fresh "clarified" generation once, at the end
    of its own turn loop, with every slot it actually resolved folded into
    one combined prompt -- never once per turn -- and that result is
    cached per item by the exact set of resolved answers, so two configs
    that land on the same resolution (common, since 3.2's rules feed both
    `rules_only` and `hybrid_no_gate`) share the same regeneration call

Every real LLM call in this module -- including the judge call, which
doesn't otherwise go through `t2sql.generation` -- is logged via
`t2sql.generation.trace.log_trace`, because `BudgetGuard` (budget.py)
tails that same file to enforce a hard dollar ceiling. `check_budget()` is
called before every real call; a run that hits the ceiling stops cleanly
with whatever items it already scored, rather than continuing over budget.

Deliberately run on the cheap model (`OPENROUTER_DETECTION_MODEL`) for
*every* config here, `baseline` included -- a budget call, documented in
`results/ablation.md`'s own preamble, not a hidden shortcut. This changes
the run's absolute accuracy numbers; it does not change what the ablation
is actually for, which is the *relative* effect of each detection
mechanism on over-asking and correctness, holding the generator constant
across all six configs.
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from openai import OpenAI
from pydantic import BaseModel, Field

from t2sql.clarify.detector import DetectedAmbiguity, detect_ambiguities
from t2sql.clarify.divergence import DivergenceReport, compute_divergence_report
from t2sql.clarify.intent import parse_intent
from t2sql.clarify.policy import ESCAPE_OPTION, ClarificationAction, ClarificationDecision, SessionState, decide_clarification
from t2sql.clarify.self_consistency import DEFAULT_N, DEFAULT_TEMPERATURE, DEFAULT_THRESHOLD, DivergenceResult, compute_divergence, decide
from t2sql.db.connection import get_connection
from t2sql.eval.budget import BudgetExceeded, BudgetGuard
from t2sql.eval.dataset import DatasetItem, GoldInterpretation
from t2sql.eval.metrics import (
    EvalRecord,
    detection_precision_recall_f1,
    end_to_end_correctness,
    execution_accuracy,
    over_ask_rate,
    silent_error_rate,
    unnecessary_ask_rate,
)
from t2sql.eval.simulated_user import _choose_option, _pick_hidden_interpretation
from t2sql.generation import generate_sql
from t2sql.generation.trace import log_trace
from t2sql.retrieval import build_schema_context
from t2sql.semantic.loader import load_semantic_layer
from t2sql.semantic.models import SemanticLayer
from t2sql.validation import validate_sql

CONFIGS = ["baseline", "llm_judge", "rules_only", "self_consistency_only", "hybrid_no_gate", "full"]
MAX_TURNS_PER_CONFIG = 2  # matches PolicyConfig.max_clarifications_per_session

_JUDGE_SYSTEM_PROMPT = (
    "You are checking whether a business question is genuinely ambiguous -- answerable in "
    "more than one reasonable way (which metric, which entity, what time range, what scope) "
    "such that different reasonable analysts would write different SQL for it. Reply with "
    "exactly one word: AMBIGUOUS or UNAMBIGUOUS."
)


@lru_cache(maxsize=1)
def _judge_client() -> OpenAI:
    return OpenAI(base_url=os.environ["OPENROUTER_BASE_URL"], api_key=os.environ["OPENROUTER_API_KEY"])


def _judge_is_ambiguous(question: str, model: str) -> bool:
    """The `llm_judge` config's one call: a plain-text yes/no, not a
    structured GeneratedSQL, so it's cheap in tokens too. Logged through
    the same `log_trace` as every other call so `BudgetGuard` sees its cost.
    """
    start = time.monotonic()
    completion = _judge_client().chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        temperature=0.0,
        max_tokens=5,
        extra_body={"usage": {"include": True}},
    )
    latency = time.monotonic() - start
    text = (completion.choices[0].message.content or "").strip().upper()
    usage = completion.usage
    log_trace(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": "llm_judge",
            "question": question,
            "model": model,
            "temperature": 0.0,
            "response": text,
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None,
            "total_tokens": usage.total_tokens if usage else None,
            "cost": getattr(usage, "cost", None) if usage else None,
            "latency_seconds": latency,
        }
    )
    return text.startswith("AMBIGUOUS")


class ConfigOutcome(BaseModel):
    config: str
    record: EvalRecord
    final_sql: str | None = None
    error: str | None = None


class ItemAblationResult(BaseModel):
    id: str
    hidden_label: str
    outcomes: dict[str, ConfigOutcome] = Field(default_factory=dict)


def _top_ambiguity(ambiguities: list[DetectedAmbiguity]) -> DetectedAmbiguity | None:
    return max(ambiguities, key=lambda a: a.confidence, default=None)


def _run_turns(
    ambiguities: list[DetectedAmbiguity],
    hidden: GoldInterpretation,
    divergence_report: DivergenceReport | None = None,
) -> tuple[dict[str, str], bool, bool, bool]:
    """Drive one config's clarification loop against a fixed, precomputed
    `ambiguities` list (recomputing it per turn would be redundant -- it's
    deterministic given the question, same as re-calling detect_ambiguities
    would be). Returns (resolved_answers, asked_any, missed_any, disclosed_any).
    """
    session = SessionState()
    resolved: dict[str, str] = {}
    asked_any = missed_any = disclosed_any = False

    for _ in range(MAX_TURNS_PER_CONFIG):
        decision = decide_clarification(ambiguities, session, divergence_report=divergence_report)
        if decision.disclosure_text:
            disclosed_any = True
        if decision.action != ClarificationAction.ASK:
            break
        asked_any = True
        assert decision.slot is not None
        match = _choose_option(hidden, decision.options, decision.ambiguity_type)
        missed_any = missed_any or match is None
        answer = match or ESCAPE_OPTION
        resolved[decision.slot] = answer
        session = SessionState(
            resolved_slots={**session.resolved_slots, decision.slot: answer},
            clarification_count=session.clarification_count + 1,
        )

    return resolved, asked_any, missed_any, disclosed_any


def _regeneration_clause(resolved: dict[str, str]) -> str:
    informative = {slot: value for slot, value in resolved.items() if value != ESCAPE_OPTION}
    return "; ".join(f"for {slot}, use: {value}" for slot, value in sorted(informative.items()))


def run_ablation_item(
    item: DatasetItem,
    guard: BudgetGuard,
    model: str,
    layer: SemanticLayer | None = None,
    n_self_consistency: int = DEFAULT_N,
    seed: int = 0,
    conn=None,
) -> ItemAblationResult:
    layer = layer or load_semantic_layer()
    hidden = _pick_hidden_interpretation(item, random.Random(seed))

    context = build_schema_context(item.question, layer=layer, conn=conn)

    guard.check()
    baseline_gen = generate_sql(item.question, context, model=model)
    baseline_validation = validate_sql(baseline_gen.sql, conn=conn)
    baseline_sql = baseline_validation.rewritten_sql if baseline_validation.ok else baseline_gen.sql

    intent = parse_intent(item.question, layer=layer)
    rule_ambiguities = detect_ambiguities(intent, layer)

    _sc_result: DivergenceResult | None = None

    def sc_result() -> DivergenceResult:
        nonlocal _sc_result
        if _sc_result is None:
            guard.check()
            _sc_result = compute_divergence(
                item.question, context=context, n=n_self_consistency, temperature=DEFAULT_TEMPERATURE, model=model
            )
        return _sc_result

    _judge_ambiguous: bool | None = None

    def judge_ambiguous() -> bool:
        nonlocal _judge_ambiguous
        if _judge_ambiguous is None:
            guard.check()
            _judge_ambiguous = _judge_is_ambiguous(item.question, model=model)
        return _judge_ambiguous

    regen_cache: dict[str, str] = {}

    def resolve_final_sql(resolved: dict[str, str]) -> str:
        clause = _regeneration_clause(resolved)
        if not clause:
            return baseline_sql
        if clause not in regen_cache:
            guard.check()
            gen = generate_sql(f"{item.question}\n\n(Clarifications from the user: {clause}.)", context, model=model)
            validation = validate_sql(gen.sql, conn=conn)
            regen_cache[clause] = validation.rewritten_sql if validation.ok else gen.sql
        return regen_cache[clause]

    def sc_ambiguities() -> list[DetectedAmbiguity]:
        d = decide(sc_result(), threshold=DEFAULT_THRESHOLD)
        return [d] if d else []

    def full_divergence_report() -> DivergenceReport | None:
        result = sc_result()
        distinct_sql: dict[str, str] = {}
        for sql in result.raw_sql:
            distinct_sql.setdefault(sql.strip(), sql)  # dedupe identical text cheaply, no LLM/db cost
        interpretations = [(f"candidate_{i}", sql) for i, sql in enumerate(distinct_sql.values())]
        if len(interpretations) < 2:
            return None
        return compute_divergence_report(interpretations, conn=conn)

    result = ItemAblationResult(id=item.id, hidden_label=hidden.label)

    def score(
        config: str,
        ambiguities: list[DetectedAmbiguity],
        divergence_report: DivergenceReport | None = None,
        detected: bool | None = None,
    ) -> None:
        """`detected` overrides the record's raw detection signal when the
        mechanism deciding whether-to-ask isn't simply "ambiguities is
        non-empty" (llm_judge: the judge's own yes/no, independent of
        whether the rule detector could supply anything to ask about).
        """
        resolved, asked, _missed, disclosed = _run_turns(ambiguities, hidden, divergence_report)
        final_sql = resolve_final_sql(resolved)
        correct = execution_accuracy(final_sql, [hidden], conn=conn)
        record = EvalRecord(
            id=item.id,
            is_ambiguous=item.is_ambiguous,
            expected_divergence=item.expected_divergence,
            detected_ambiguous=detected if detected is not None else len(ambiguities) > 0,
            asked=asked,
            disclosed=disclosed,
            correct=correct,
        )
        result.outcomes[config] = ConfigOutcome(config=config, record=record, final_sql=final_sql)

    try:
        score("baseline", [])
        score("rules_only", rule_ambiguities)

        judge_flag = judge_ambiguous()
        score("llm_judge", rule_ambiguities if judge_flag else [], detected=judge_flag)

        sc_amb = sc_ambiguities()
        score("self_consistency_only", sc_amb)
        score("hybrid_no_gate", rule_ambiguities + sc_amb)
        score("full", rule_ambiguities + sc_amb, divergence_report=full_divergence_report())
    except Exception as e:
        if isinstance(e, BudgetExceeded):
            raise
        for config in CONFIGS:
            if config not in result.outcomes:
                result.outcomes[config] = ConfigOutcome(
                    config=config,
                    record=EvalRecord(id=item.id, is_ambiguous=item.is_ambiguous, expected_divergence=item.expected_divergence),
                    error=str(e),
                )

    return result


class AblationRun(BaseModel):
    dataset: str
    model: str
    n_self_consistency: int
    ceiling_usd: float
    spent_usd: float
    n_items_attempted: int
    n_items_completed: int
    stopped_on_budget: bool
    items: list[ItemAblationResult] = Field(default_factory=list)


def run_ablation(
    items: list[DatasetItem],
    ceiling_usd: float,
    model: str,
    dataset_label: str = "dev",
    n_self_consistency: int = DEFAULT_N,
    seed: int = 0,
    layer: SemanticLayer | None = None,
) -> AblationRun:
    """Runs every item through `run_ablation_item` against one shared
    read-only DB connection, stopping cleanly (not crashing) the moment
    `BudgetGuard` reports the ceiling's been reached -- whatever items
    completed before that stay in the result.
    """
    layer = layer or load_semantic_layer()
    guard = BudgetGuard(ceiling_usd=ceiling_usd)
    results: list[ItemAblationResult] = []
    stopped_on_budget = False

    with get_connection(role="readonly") as conn:
        for item in items:
            try:
                results.append(
                    run_ablation_item(
                        item, guard, model=model, layer=layer, n_self_consistency=n_self_consistency, seed=seed, conn=conn
                    )
                )
            except BudgetExceeded:
                stopped_on_budget = True
                break

    guard.refresh()
    return AblationRun(
        dataset=dataset_label,
        model=model,
        n_self_consistency=n_self_consistency,
        ceiling_usd=ceiling_usd,
        spent_usd=guard.spent_usd,
        n_items_attempted=len(items),
        n_items_completed=len(results),
        stopped_on_budget=stopped_on_budget,
        items=results,
    )


def records_by_config(run: AblationRun) -> dict[str, list[EvalRecord]]:
    by_config: dict[str, list[EvalRecord]] = {c: [] for c in CONFIGS}
    for item_result in run.items:
        for config, outcome in item_result.outcomes.items():
            if outcome.error is None:
                by_config[config].append(outcome.record)
    return by_config


def render_ablation_md(run: AblationRun) -> str:
    by_config = records_by_config(run)
    lines = [
        "# Ablation results",
        "",
        f"- dataset: `{run.dataset}` ({run.n_items_completed}/{run.n_items_attempted} items completed"
        + (", stopped early on budget" if run.stopped_on_budget else "")
        + ")",
        f"- model: `{run.model}` (used for **every** config, including `baseline` -- a budget call, "
        "not a hidden shortcut; see this project's README/limitations for what that does and doesn't "
        "change about the comparison)",
        f"- self-consistency N: {run.n_self_consistency}",
        f"- spend: ${run.spent_usd:.4f} of a ${run.ceiling_usd:.2f} ceiling",
        "",
        "| config | n | correctness | over-ask rate | unnecessary-ask rate | detection P/R/F1 | silent-error rate |",
        "|---|---|---|---|---|---|---|",
    ]
    for config in CONFIGS:
        records = by_config[config]
        n = len(records)
        if n == 0:
            lines.append(f"| {config} | 0 | - | - | - | - | - |")
            continue
        acc = end_to_end_correctness(records)
        over = over_ask_rate(records)
        unnecessary = unnecessary_ask_rate(records)
        prf = detection_precision_recall_f1(records)
        silent = silent_error_rate(records)
        lines.append(
            f"| {config} | {n} | {acc:.1%} | {over:.1%} | {unnecessary:.1%} | "
            f"{prf['precision']:.2f}/{prf['recall']:.2f}/{prf['f1']:.2f} | {silent:.1%} |"
        )

    return "\n".join(lines) + "\n"


RESULTS_DIR = Path(__file__).resolve().parents[3] / "results"


def write_ablation_report(run: AblationRun, path: Path = RESULTS_DIR / "ablation.md") -> Path:
    """Writes both the human-readable table at `path` and a `.raw.json`
    sibling with the full per-item, per-config detail (every question,
    final SQL, and record) -- the aggregate table alone can't support
    Task 4.5's per-ambiguity-type breakdown, bootstrap CIs, or qualitative
    examples, all of which need to go back to individual items after the
    fact. Each real LLM call is billed once; losing this would mean paying
    for it again just to look at it a second way.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_ablation_md(run))
    raw_path = path.with_suffix(".raw.json")
    raw_path.write_text(run.model_dump_json(indent=2))
    return path
