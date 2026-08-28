"""Iterates a dataset through a configurable pipeline (question -> SQL),
scores each item with execution_accuracy, and writes per-item results plus
a run summary.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from t2sql.eval.dataset import DatasetItem, load_dataset
from t2sql.eval.metrics import execution_accuracy

RESULTS_DIR = Path(__file__).resolve().parents[3] / "results"


class Pipeline(Protocol):
    def __call__(self, question: str) -> str:
        """Return generated SQL for `question`."""
        ...


class ItemResult(BaseModel):
    id: str
    question: str
    is_ambiguous: bool
    pred_sql: str | None
    correct: bool
    error: str | None = None
    latency_seconds: float = 0.0


class RunSummary(BaseModel):
    run_id: str
    config: str
    dataset: str
    n_items: int
    n_correct: int
    n_errors: int
    accuracy: float


def _new_run_id(config: str) -> str:
    return f"{config}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def run_eval(
    dataset_path: Path,
    pipeline: Pipeline,
    config: str = "baseline",
    run_id: str | None = None,
    results_dir: Path = RESULTS_DIR,
) -> RunSummary:
    run_id = run_id or _new_run_id(config)
    items: list[DatasetItem] = load_dataset(dataset_path)

    results_dir.mkdir(parents=True, exist_ok=True)
    results_path = results_dir / f"{run_id}.jsonl"

    item_results: list[ItemResult] = []
    with open(results_path, "w") as f:
        for item in items:
            start = time.monotonic()
            try:
                pred_sql: str | None = pipeline(item.question)
                error = None
            except Exception as e:
                pred_sql = None
                error = str(e)
            latency_seconds = time.monotonic() - start

            correct = execution_accuracy(pred_sql, item.gold_sql) if pred_sql and error is None else False

            result = ItemResult(
                id=item.id,
                question=item.question,
                is_ambiguous=item.is_ambiguous,
                pred_sql=pred_sql,
                correct=correct,
                error=error,
                latency_seconds=latency_seconds,
            )
            item_results.append(result)
            f.write(result.model_dump_json() + "\n")

    n_correct = sum(1 for r in item_results if r.correct)
    n_errors = sum(1 for r in item_results if r.error is not None)
    n_items = len(item_results)

    summary = RunSummary(
        run_id=run_id,
        config=config,
        dataset=str(dataset_path),
        n_items=n_items,
        n_correct=n_correct,
        n_errors=n_errors,
        accuracy=(n_correct / n_items) if n_items else 0.0,
    )
    (results_dir / f"{run_id}.summary.json").write_text(summary.model_dump_json(indent=2))

    return summary
