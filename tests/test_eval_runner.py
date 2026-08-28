"""Task 1.5 gate: the harness runs end to end on the 10-item placeholder
dataset and reports an accuracy number. Uses a deterministic oracle
pipeline here (no LLM call needed) to keep this fast and free -- the CLI's
real `baseline` config is exercised separately in test_eval_cli.py.
"""

import json
from pathlib import Path

import pytest

from t2sql.eval.dataset import load_dataset
from t2sql.eval.runner import RunSummary, run_eval

DEV_DATASET_PATH = Path(__file__).resolve().parents[1] / "data" / "dev.jsonl"


def test_perfect_pipeline_scores_full_accuracy(tmp_path: Path) -> None:
    def oracle(question: str) -> str:
        items = load_dataset(DEV_DATASET_PATH)
        match = next(item for item in items if item.question == question)
        return match.gold_sql[0].sql

    summary = run_eval(DEV_DATASET_PATH, oracle, config="oracle", run_id="test-oracle", results_dir=tmp_path)

    assert isinstance(summary, RunSummary)
    assert summary.n_items == 10
    assert summary.n_correct == 10
    assert summary.n_errors == 0
    assert summary.accuracy == 1.0


def test_wrong_pipeline_scores_zero_accuracy(tmp_path: Path) -> None:
    summary = run_eval(
        DEV_DATASET_PATH,
        lambda question: "SELECT 1 WHERE FALSE",
        config="always-wrong",
        run_id="test-wrong",
        results_dir=tmp_path,
    )

    assert summary.n_items == 10
    assert summary.n_correct == 0
    assert summary.accuracy == 0.0


def test_pipeline_exception_is_captured_as_an_error_not_raised(tmp_path: Path) -> None:
    def broken(question: str) -> str:
        raise RuntimeError("pipeline blew up")

    summary = run_eval(DEV_DATASET_PATH, broken, config="broken", run_id="test-broken", results_dir=tmp_path)

    assert summary.n_items == 10
    assert summary.n_errors == 10
    assert summary.n_correct == 0


def test_results_and_summary_files_are_written(tmp_path: Path) -> None:
    summary = run_eval(
        DEV_DATASET_PATH,
        lambda question: "SELECT 1",
        config="baseline",
        run_id="test-files",
        results_dir=tmp_path,
    )

    results_path = tmp_path / "test-files.jsonl"
    summary_path = tmp_path / "test-files.summary.json"
    assert results_path.exists()
    assert summary_path.exists()

    lines = results_path.read_text().strip().splitlines()
    assert len(lines) == 10
    first = json.loads(lines[0])
    assert {"id", "question", "is_ambiguous", "pred_sql", "correct", "error", "latency_seconds"} <= first.keys()

    saved_summary = json.loads(summary_path.read_text())
    assert saved_summary["run_id"] == summary.run_id
    assert saved_summary["accuracy"] == summary.accuracy
