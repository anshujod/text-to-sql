"""Validates dataset files.

For every item in every given JSONL file, checks:
  - the DatasetItem schema itself parses
  - ids are unique across all files given on the command line
  - unambiguous items (is_ambiguous=False) have exactly 1 gold interpretation;
    ambiguous items have 2-4
  - every gold SQL parses under the project's real AST validator
    (t2sql.validation.ast_validator, the same gate generated SQL goes
    through) and executes against the live app_readonly DB, returning at
    least one row
  - no duplicate or near-duplicate questions, within or across files
    (near-duplicate = normalized token-Jaccard similarity >= 0.90 -- short
    questions naturally share most of their words, so a lower threshold
    flags legitimate scoped/category variants as false positives)

Usage:
    uv run python scripts/validate_dataset.py data/unambiguous.jsonl data/ambiguous.jsonl
    uv run python scripts/validate_dataset.py data/dev.jsonl data/test.jsonl

Exits nonzero if any check fails, printing every failure found (not just
the first) so a broken dataset can be fixed in one pass.
"""

from __future__ import annotations

import re
import sys
from itertools import combinations
from pathlib import Path

from t2sql.db.connection import get_connection
from t2sql.eval.dataset import DatasetItem, load_dataset
from t2sql.validation.ast_validator import validate_sql

NEAR_DUPLICATE_THRESHOLD = 0.90
_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(question: str) -> set[str]:
    return set(_WORD_RE.findall(question.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def validate_files(paths: list[Path]) -> list[str]:
    errors: list[str] = []
    all_items: list[tuple[Path, DatasetItem]] = []

    for path in paths:
        try:
            items = load_dataset(path)
        except Exception as e:
            errors.append(f"{path}: failed to load/parse as a dataset file -- {e}")
            continue
        if not items:
            errors.append(f"{path}: contains no items")
            continue
        all_items.extend((path, item) for item in items)

    if errors:
        return errors  # a file-level parse failure makes further checks meaningless

    # id uniqueness, across all files given
    seen_ids: dict[str, Path] = {}
    for path, item in all_items:
        if item.id in seen_ids:
            errors.append(f"{path}: duplicate id '{item.id}' (first seen in {seen_ids[item.id]})")
        else:
            seen_ids[item.id] = path

    # interpretation count bounds
    for path, item in all_items:
        n = len(item.gold_sql)
        if item.is_ambiguous:
            if not (2 <= n <= 4):
                errors.append(f"{path}: {item.id} is_ambiguous=true but has {n} gold_sql entries (need 2-4)")
        else:
            if n != 1:
                errors.append(f"{path}: {item.id} is_ambiguous=false but has {n} gold_sql entries (need exactly 1)")

    # ambiguous items should carry ambiguity_types; unambiguous ones shouldn't
    for path, item in all_items:
        if item.is_ambiguous and not item.ambiguity_types:
            errors.append(f"{path}: {item.id} is_ambiguous=true but ambiguity_types is empty")
        if not item.is_ambiguous and item.ambiguity_types:
            errors.append(f"{path}: {item.id} is_ambiguous=false but ambiguity_types is non-empty")

    # exact and near-duplicate questions, across the whole combined set
    seen_exact: dict[str, str] = {}
    for path, item in all_items:
        norm = item.question.strip().lower()
        if norm in seen_exact:
            errors.append(f"{path}: {item.id} question is an exact duplicate of {seen_exact[norm]}: '{item.question}'")
        else:
            seen_exact[norm] = item.id

    token_sets = [(item.id, _tokens(item.question)) for _, item in all_items]
    for (id_a, toks_a), (id_b, toks_b) in combinations(token_sets, 2):
        sim = _jaccard(toks_a, toks_b)
        if sim >= NEAR_DUPLICATE_THRESHOLD:
            errors.append(f"near-duplicate questions ({sim:.0%} token overlap): {id_a} / {id_b}")

    # every gold SQL parses (via the real AST validator) and executes
    with get_connection(role="readonly") as conn:
        for path, item in all_items:
            for interp in item.gold_sql:
                result = validate_sql(interp.sql, conn=conn)
                if not result.ok:
                    errors.append(
                        f"{path}: {item.id} interpretation '{interp.label or interp.interpretation}' "
                        f"failed AST validation: {[e.message for e in result.errors]}"
                    )
                    continue
                try:
                    with conn.cursor() as cur:
                        cur.execute(result.rewritten_sql or interp.sql)
                        rows = cur.fetchall()
                except Exception as e:
                    errors.append(
                        f"{path}: {item.id} interpretation '{interp.label or interp.interpretation}' "
                        f"failed to execute: {e}"
                    )
                    conn.rollback()
                    continue
                if len(rows) == 0:
                    errors.append(
                        f"{path}: {item.id} interpretation '{interp.label or interp.interpretation}' "
                        f"returned 0 rows"
                    )

    return errors


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: validate_dataset.py FILE.jsonl [FILE.jsonl ...]", file=sys.stderr)
        return 2

    paths = [Path(p) for p in argv]
    errors = validate_files(paths)

    if errors:
        print(f"VALIDATION FAILED: {len(errors)} problem(s)\n")
        for e in errors:
            print(f"  - {e}")
        return 1

    total = sum(len(load_dataset(p)) for p in paths)
    print(f"OK: {total} items across {len(paths)} file(s) passed all checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
