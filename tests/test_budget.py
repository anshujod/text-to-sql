"""Budget guard: $0, pure file-tailing logic against a fake trace file --
no real LLM calls needed to test the cap itself.
"""

import json

import pytest

from t2sql.eval.budget import BudgetExceeded, BudgetGuard


def _write_lines(path, records) -> None:
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_guard_ignores_pre_existing_trace_lines(tmp_path) -> None:
    trace = tmp_path / "generation.jsonl"
    _write_lines(trace, [{"cost": 5.0}])  # spend from a previous, unrelated run

    guard = BudgetGuard(ceiling_usd=1.0, trace_path=trace)
    guard.check()  # must not raise -- the pre-existing $5 isn't this run's spend
    assert guard.spent_usd == 0.0


def test_guard_sums_only_lines_appended_after_creation(tmp_path) -> None:
    trace = tmp_path / "generation.jsonl"
    _write_lines(trace, [{"cost": 5.0}])
    guard = BudgetGuard(ceiling_usd=1.0, trace_path=trace)

    with open(trace, "a") as f:
        f.write(json.dumps({"cost": 0.01}) + "\n")
        f.write(json.dumps({"cost": 0.02}) + "\n")

    assert guard.refresh() == pytest.approx(0.03)


def test_guard_raises_once_ceiling_is_crossed(tmp_path) -> None:
    trace = tmp_path / "generation.jsonl"
    trace.write_text("")
    guard = BudgetGuard(ceiling_usd=0.05, trace_path=trace)

    with open(trace, "a") as f:
        f.write(json.dumps({"cost": 0.03}) + "\n")
    guard.check()  # under ceiling, no raise

    with open(trace, "a") as f:
        f.write(json.dumps({"cost": 0.03}) + "\n")  # cumulative 0.06 >= 0.05
    with pytest.raises(BudgetExceeded):
        guard.check()


def test_guard_treats_missing_cost_as_zero_but_flags_it(tmp_path) -> None:
    trace = tmp_path / "generation.jsonl"
    trace.write_text("")
    guard = BudgetGuard(ceiling_usd=1.0, trace_path=trace)

    with open(trace, "a") as f:
        f.write(json.dumps({"cost": None}) + "\n")
        f.write(json.dumps({"cost": 0.02}) + "\n")

    assert guard.refresh() == pytest.approx(0.02)
    assert guard.calls_with_unknown_cost == 1


def test_guard_on_nonexistent_trace_file_is_zero_spend(tmp_path) -> None:
    guard = BudgetGuard(ceiling_usd=1.0, trace_path=tmp_path / "does_not_exist.jsonl")
    guard.check()
    assert guard.spent_usd == 0.0
