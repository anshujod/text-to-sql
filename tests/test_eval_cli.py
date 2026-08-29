"""Exercises the actual `python -m t2sql.eval run --dataset ... --config
baseline` path end to end, through the real LLM-backed baseline pipeline.
Opt-in -- skipped unless RUN_LLM_TESTS=1, since this makes real, billed
OpenRouter calls. Run explicitly with:
    RUN_LLM_TESTS=1 uv run pytest tests/test_eval_cli.py -q

Deliberately runs against a small fixture (3 items), not the full
data/dev.jsonl (120 items as of Task 2.4) -- this is a CLI smoke test, not
a real eval run, and shouldn't scale its bill with the dataset size.

Writes into the real (gitignored) results/ dir, same as a normal run.
"""

import json
import os
from pathlib import Path

import pytest

from t2sql.eval.__main__ import main
from t2sql.eval.dataset import load_dataset
from t2sql.eval.runner import RESULTS_DIR

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS") != "1",
    reason="set RUN_LLM_TESTS=1 to run tests that make real, billed LLM calls",
)

DEV_DATASET_PATH = Path(__file__).resolve().parents[1] / "data" / "dev.jsonl"
RUN_ID = "cli-smoke-test"
SMOKE_TEST_SIZE = 3


@pytest.fixture
def smoke_dataset_path(tmp_path: Path) -> Path:
    items = load_dataset(DEV_DATASET_PATH)[:SMOKE_TEST_SIZE]
    path = tmp_path / "smoke.jsonl"
    with open(path, "w") as f:
        for item in items:
            f.write(item.model_dump_json() + "\n")
    return path


def test_cli_run_reports_an_accuracy_number(smoke_dataset_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["run", "--dataset", str(smoke_dataset_path), "--config", "baseline", "--run-id", RUN_ID])

    captured = capsys.readouterr()
    assert "accuracy=" in captured.out
    assert f"n_items={SMOKE_TEST_SIZE}" in captured.out

    results_path = RESULTS_DIR / f"{RUN_ID}.jsonl"
    assert results_path.exists()
    lines = results_path.read_text().strip().splitlines()
    assert len(lines) == SMOKE_TEST_SIZE
    for line in lines:
        record = json.loads(line)
        assert record["pred_sql"], "baseline pipeline should produce SQL for every item"

    summary_path = RESULTS_DIR / f"{RUN_ID}.summary.json"
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text())
    assert summary["n_items"] == SMOKE_TEST_SIZE
    assert 0.0 <= summary["accuracy"] <= 1.0
