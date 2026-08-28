"""CLI: python -m t2sql.eval run --dataset data/dev.jsonl --config baseline"""

from __future__ import annotations

import argparse
from pathlib import Path

from t2sql.eval.pipelines import PIPELINES
from t2sql.eval.runner import run_eval


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m t2sql.eval")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run a dataset through a pipeline and report accuracy")
    run_parser.add_argument("--dataset", required=True, type=Path)
    run_parser.add_argument("--config", required=True, choices=sorted(PIPELINES))
    run_parser.add_argument("--run-id", default=None)

    args = parser.parse_args(argv)

    if args.command == "run":
        summary = run_eval(args.dataset, PIPELINES[args.config], config=args.config, run_id=args.run_id)
        print(
            f"run_id={summary.run_id} n_items={summary.n_items} "
            f"n_correct={summary.n_correct} n_errors={summary.n_errors} "
            f"accuracy={summary.accuracy:.2%}"
        )


if __name__ == "__main__":
    main()
