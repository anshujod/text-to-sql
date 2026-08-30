"""Run the ablation across all 6 configs on a slice of the dev set, under a
hard dollar ceiling enforced by BudgetGuard, and write results/ablation.md.

Usage: uv run python scripts/run_ablation.py --n-items 25 --ceiling 1.20
"""

from __future__ import annotations

import argparse
from pathlib import Path

from t2sql.eval.ablation import run_ablation, write_ablation_report
from t2sql.eval.dataset import load_dataset

import os


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="data/dev.jsonl", type=Path)
    parser.add_argument("--n-items", default=25, type=int)
    parser.add_argument("--ceiling", default=1.20, type=float)
    parser.add_argument("--model", default=None, help="defaults to OPENROUTER_DETECTION_MODEL (the cheap model)")
    parser.add_argument("--n-self-consistency", default=5, type=int)
    parser.add_argument("--seed", default=0, type=int)
    parser.add_argument("--out", default="results/ablation.md", type=Path)
    args = parser.parse_args()

    model = args.model or os.environ["OPENROUTER_DETECTION_MODEL"]
    items = load_dataset(args.dataset)[: args.n_items]

    print(f"Running ablation: {len(items)} items from {args.dataset}, model={model}, ceiling=${args.ceiling:.2f}")
    run = run_ablation(
        items,
        ceiling_usd=args.ceiling,
        model=model,
        dataset_label=str(args.dataset),
        n_self_consistency=args.n_self_consistency,
        seed=args.seed,
    )
    path = write_ablation_report(run, path=args.out)
    print(f"n_items_completed={run.n_items_completed}/{run.n_items_attempted} spent=${run.spent_usd:.4f} stopped_on_budget={run.stopped_on_budget}")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
