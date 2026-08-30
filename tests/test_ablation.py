"""Ablation runner: $0 -- every LLM-calling function is
monkeypatched, so these tests verify the *control flow* (call-sharing
across configs, budget enforcement, report rendering), not real model
output. The real, budgeted run against OpenRouter is done separately, once
this logic is trusted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

import t2sql.eval.ablation as ab
from t2sql.clarify.self_consistency import DivergenceResult
from t2sql.db.connection import get_connection
from t2sql.eval.ablation import CONFIGS, render_ablation_md, run_ablation, run_ablation_item, write_ablation_report
from t2sql.eval.budget import BudgetGuard
from t2sql.eval.dataset import DatasetItem, GoldInterpretation, load_dataset
from t2sql.generation.models import GeneratedSQL
from t2sql.generation.trace import log_trace
from t2sql.semantic.loader import load_semantic_layer

_LAYER = load_semantic_layer()
_AMBIGUOUS = {i.id: i for i in load_dataset(Path("data/ambiguous.jsonl"))}
_UNAMBIGUOUS = load_dataset(Path("data/unambiguous.jsonl"))


def _fake_generate_sql(question, context, dialect="postgres", temperature=0.0, n=1, model=None):
    g = GeneratedSQL(sql="SELECT COUNT(*) FROM categories", tables_used=["categories"], assumptions=[], confidence=0.9)
    return [g] * n if n > 1 else g


def _fake_compute_divergence(question, context=None, n=5, temperature=0.8, model=None, dialect="postgres"):
    return DivergenceResult(
        question=question,
        n=n,
        score=0.7,
        largest_cluster_size=2,
        distinct_signatures=["sig1", "sig2"],
        unparseable_count=0,
        raw_sql=["SELECT a FROM t", "SELECT b FROM t", "SELECT a FROM t", "SELECT a FROM t", "SELECT b FROM t"],
    )


def _fake_judge(question, model):
    return True


# ---------------------------------------------------------------------------
# Call-sharing across configs
# ---------------------------------------------------------------------------


def test_baseline_generation_is_shared_not_regenerated_per_config() -> None:
    """An item with no real informative resolution anywhere (unambiguous)
    should cost exactly one generate_sql call total, across all 6 configs.
    """
    item = _UNAMBIGUOUS[0]
    guard = BudgetGuard(ceiling_usd=1000.0)
    calls = {"n": 0}

    def counting_generate_sql(*a, **kw):
        calls["n"] += 1
        return _fake_generate_sql(*a, **kw)

    with mock.patch.object(ab, "generate_sql", side_effect=counting_generate_sql), mock.patch.object(
        ab, "compute_divergence", side_effect=_fake_compute_divergence
    ), mock.patch.object(ab, "_judge_is_ambiguous", side_effect=_fake_judge), get_connection(role="readonly") as conn:
        result = run_ablation_item(item, guard, model="fake", layer=_LAYER, conn=conn)

    assert len(result.outcomes) == 6
    assert calls["n"] == 1  # just the shared baseline generation


def test_self_consistency_samples_generated_once_not_per_config() -> None:
    """Three configs (self_consistency_only, hybrid_no_gate, full) all
    consult self-consistency -- compute_divergence must be called once.
    """
    item = _AMBIGUOUS["amb-001"]
    guard = BudgetGuard(ceiling_usd=1000.0)
    calls = {"n": 0}

    def counting_compute_divergence(*a, **kw):
        calls["n"] += 1
        return _fake_compute_divergence(*a, **kw)

    with mock.patch.object(ab, "generate_sql", side_effect=_fake_generate_sql), mock.patch.object(
        ab, "compute_divergence", side_effect=counting_compute_divergence
    ), mock.patch.object(ab, "_judge_is_ambiguous", side_effect=_fake_judge), get_connection(role="readonly") as conn:
        run_ablation_item(item, guard, model="fake", layer=_LAYER, conn=conn)

    assert calls["n"] == 1


def test_judge_call_made_once_not_per_config() -> None:
    item = _AMBIGUOUS["amb-001"]
    guard = BudgetGuard(ceiling_usd=1000.0)
    calls = {"n": 0}

    def counting_judge(*a, **kw):
        calls["n"] += 1
        return _fake_judge(*a, **kw)

    with mock.patch.object(ab, "generate_sql", side_effect=_fake_generate_sql), mock.patch.object(
        ab, "compute_divergence", side_effect=_fake_compute_divergence
    ), mock.patch.object(ab, "_judge_is_ambiguous", side_effect=counting_judge), get_connection(role="readonly") as conn:
        run_ablation_item(item, guard, model="fake", layer=_LAYER, conn=conn)

    assert calls["n"] == 1


def test_llm_judge_detection_signal_is_independent_of_rule_candidates() -> None:
    """On a genuinely unambiguous item, the rule detector correctly finds
    nothing to ask about -- llm_judge must still report its own raw
    detected_ambiguous=True (the judge said yes), even though there's
    nothing to actually ask (asked=False), since these measure different
    things (detection precision/recall vs. over-ask rate).
    """
    item = _UNAMBIGUOUS[0]
    guard = BudgetGuard(ceiling_usd=1000.0)

    with mock.patch.object(ab, "generate_sql", side_effect=_fake_generate_sql), mock.patch.object(
        ab, "compute_divergence", side_effect=_fake_compute_divergence
    ), mock.patch.object(ab, "_judge_is_ambiguous", side_effect=_fake_judge), get_connection(role="readonly") as conn:
        result = run_ablation_item(item, guard, model="fake", layer=_LAYER, conn=conn)

    llm_judge = result.outcomes["llm_judge"].record
    assert llm_judge.detected_ambiguous is True
    assert llm_judge.asked is False


def test_baseline_config_never_detects_never_asks() -> None:
    item = _AMBIGUOUS["amb-001"]
    guard = BudgetGuard(ceiling_usd=1000.0)

    with mock.patch.object(ab, "generate_sql", side_effect=_fake_generate_sql), mock.patch.object(
        ab, "compute_divergence", side_effect=_fake_compute_divergence
    ), mock.patch.object(ab, "_judge_is_ambiguous", side_effect=_fake_judge), get_connection(role="readonly") as conn:
        result = run_ablation_item(item, guard, model="fake", layer=_LAYER, conn=conn)

    baseline = result.outcomes["baseline"].record
    assert baseline.detected_ambiguous is False
    assert baseline.asked is False


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


def _fake_generate_sql_with_cost(cost: float, trace_path: Path):
    """`log_trace`'s `path` default is bound at function-definition time, so
    patching the module-level GENERATION_TRACE_PATH attribute after import
    would silently do nothing -- the fake must pass `path=` explicitly to
    land in the test's own tmp trace file instead of the real one.
    """

    def fn(question, context, dialect="postgres", temperature=0.0, n=1, model=None):
        log_trace(
            {"timestamp": datetime.now(timezone.utc).isoformat(), "kind": "fake", "cost": cost * max(n, 1)},
            path=trace_path,
        )
        g = GeneratedSQL(sql="SELECT COUNT(*) FROM categories", tables_used=["categories"], assumptions=[], confidence=0.9)
        return [g] * n if n > 1 else g

    return fn


def test_budget_guard_stops_the_batch_run_cleanly(tmp_path) -> None:
    trace_path = tmp_path / "generation.jsonl"
    trace_path.write_text("")
    items = [_UNAMBIGUOUS[0], _UNAMBIGUOUS[1], _UNAMBIGUOUS[2]]

    with mock.patch.object(ab, "generate_sql", side_effect=_fake_generate_sql_with_cost(0.5, trace_path)), mock.patch.object(
        ab, "compute_divergence", side_effect=_fake_compute_divergence
    ), mock.patch.object(ab, "_judge_is_ambiguous", side_effect=_fake_judge):
        guard = BudgetGuard(ceiling_usd=0.6, trace_path=trace_path)
        results = []
        stopped = False
        with get_connection(role="readonly") as conn:
            for item in items:
                try:
                    results.append(run_ablation_item(item, guard, model="fake", layer=_LAYER, conn=conn))
                except ab.BudgetExceeded:
                    stopped = True
                    break

    assert stopped is True
    assert len(results) < len(items)  # did not complete all items -- stopped early
    assert guard.spent_usd >= 0.6


def test_run_ablation_reports_stopped_on_budget(tmp_path) -> None:
    trace_path = tmp_path / "generation.jsonl"
    trace_path.write_text("")
    items = _UNAMBIGUOUS[:5]

    with mock.patch.object(ab, "generate_sql", side_effect=_fake_generate_sql_with_cost(0.5, trace_path)), mock.patch.object(
        ab, "compute_divergence", side_effect=_fake_compute_divergence
    ), mock.patch.object(ab, "_judge_is_ambiguous", side_effect=_fake_judge), mock.patch.object(
        ab, "BudgetGuard", lambda ceiling_usd: BudgetGuard(ceiling_usd, trace_path=trace_path)
    ):
        run = run_ablation(items, ceiling_usd=0.6, model="fake", layer=_LAYER)

    assert run.stopped_on_budget is True
    assert run.n_items_completed < run.n_items_attempted


def test_run_ablation_completes_normally_under_a_generous_ceiling(tmp_path) -> None:
    trace_path = tmp_path / "generation.jsonl"
    trace_path.write_text("")
    items = _UNAMBIGUOUS[:2]

    with mock.patch.object(ab, "generate_sql", side_effect=_fake_generate_sql_with_cost(0.01, trace_path)), mock.patch.object(
        ab, "compute_divergence", side_effect=_fake_compute_divergence
    ), mock.patch.object(ab, "_judge_is_ambiguous", side_effect=_fake_judge), mock.patch.object(
        ab, "BudgetGuard", lambda ceiling_usd: BudgetGuard(ceiling_usd, trace_path=trace_path)
    ):
        run = run_ablation(items, ceiling_usd=1000.0, model="fake", layer=_LAYER)

    assert run.stopped_on_budget is False
    assert run.n_items_completed == 2


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def test_render_ablation_md_has_all_six_config_rows() -> None:
    item = _AMBIGUOUS["amb-001"]
    guard = BudgetGuard(ceiling_usd=1000.0)

    with mock.patch.object(ab, "generate_sql", side_effect=_fake_generate_sql), mock.patch.object(
        ab, "compute_divergence", side_effect=_fake_compute_divergence
    ), mock.patch.object(ab, "_judge_is_ambiguous", side_effect=_fake_judge), get_connection(role="readonly") as conn:
        run = ab.AblationRun(
            dataset="test",
            model="fake",
            n_self_consistency=5,
            ceiling_usd=1.0,
            spent_usd=0.0,
            n_items_attempted=1,
            n_items_completed=1,
            stopped_on_budget=False,
            items=[run_ablation_item(item, guard, model="fake", layer=_LAYER, conn=conn)],
        )

    md = render_ablation_md(run)
    for config in CONFIGS:
        assert f"| {config} |" in md


def test_write_ablation_report_persists_full_per_item_detail_alongside_the_table(tmp_path) -> None:
    """The aggregate table alone can't support 4.5's per-type breakdown,
    CIs, or qualitative examples -- write_ablation_report must also
    persist every item's raw outcomes, not just the summary numbers.
    """
    item = _AMBIGUOUS["amb-001"]
    guard = BudgetGuard(ceiling_usd=1000.0)

    with mock.patch.object(ab, "generate_sql", side_effect=_fake_generate_sql), mock.patch.object(
        ab, "compute_divergence", side_effect=_fake_compute_divergence
    ), mock.patch.object(ab, "_judge_is_ambiguous", side_effect=_fake_judge), get_connection(role="readonly") as conn:
        run = ab.AblationRun(
            dataset="test",
            model="fake",
            n_self_consistency=5,
            ceiling_usd=1.0,
            spent_usd=0.0,
            n_items_attempted=1,
            n_items_completed=1,
            stopped_on_budget=False,
            items=[run_ablation_item(item, guard, model="fake", layer=_LAYER, conn=conn)],
        )

    md_path = tmp_path / "ablation.md"
    write_ablation_report(run, path=md_path)

    raw_path = tmp_path / "ablation.raw.json"
    assert raw_path.exists()
    reloaded = ab.AblationRun.model_validate_json(raw_path.read_text())
    assert reloaded.items[0].id == "amb-001"
    assert set(reloaded.items[0].outcomes) == set(CONFIGS)
