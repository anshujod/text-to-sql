"""Exercises the actual `python -m t2sql.eval run --dataset ... --config
baseline` path end to end, through the real LLM-backed baseline pipeline.
Opt-in -- skipped unless RUN_LLM_TESTS=1, since this makes 10 real,
billed OpenRouter calls. Run explicitly with:
    RUN_LLM_TESTS=1 uv run pytest tests/test_eval_cli.py -q

Writes into the real (gitignored) results/ dir, same as a normal run.
"""

import json
import os
from pathlib import Path

import pytest

from t2sql.eval.__main__ import main
from t2sql.eval.runner import RESULTS_DIR

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS") != "1",
    reason="set RUN_LLM_TESTS=1 to run tests that make real, billed LLM calls",
)

DEV_DATASET_PATH = Path(__file__).resolve().parents[1] / "data" / "dev.jsonl"
RUN_ID = "cli-smoke-test"


def test_cli_run_reports_an_accuracy_number(capsys: pytest.CaptureFixture[str]) -> None:
    main(["run", "--dataset", str(DEV_DATASET_PATH), "--config", "baseline", "--run-id", RUN_ID])

    captured = capsys.readouterr()
    assert "accuracy=" in captured.out
    assert "n_items=10" in captured.out

    results_path = RESULTS_DIR / f"{RUN_ID}.jsonl"
    assert results_path.exists()
    lines = results_path.read_text().strip().splitlines()
    assert len(lines) == 10
    for line in lines:
        record = json.loads(line)
        assert record["pred_sql"], "baseline pipeline should produce SQL for every item"

    summary_path = RESULTS_DIR / f"{RUN_ID}.summary.json"
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text())
    assert summary["n_items"] == 10
    assert 0.0 <= summary["accuracy"] <= 1.0
