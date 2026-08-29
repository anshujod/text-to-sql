"""Threshold calibration: tune the self-consistency divergence
threshold against data/dev.jsonl only.

This makes real, billed LLM calls (N=5 generations per item) -- expensive
enough that it's a deliberate, explicit script to run, not a pytest test
that fires on every `RUN_LLM_TESTS=1` run. See tests/test_self_consistency.py
for the fast, free unit tests and cheap opt-in smoke tests.

Two phases, split so a crash or interruption during generation doesn't
lose completed work and doesn't cost anything to re-analyze:

  --generate   run compute_divergence (5 LLM calls each) over every dev
               ambiguous item Task 3.2's rule detector missed, plus every
               dev unambiguous item. Appends one JSON line per item to the
               cache file; already-cached ids are skipped on a re-run.
  --report     (default) read the cache and sweep threshold candidates,
               reporting for each: how many rule-missed ambiguous items it
               newly catches, and what fraction of the unambiguous set it
               false-fires on -- PLAN.md 3.3's Done when is >=5 caught and
               <=15% false-fire rate.

Usage:
    uv run python scripts/tune_self_consistency_threshold.py --generate
    uv run python scripts/tune_self_consistency_threshold.py --report
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from t2sql.clarify import compute_divergence, detect_ambiguities, parse_intent
from t2sql.eval.dataset import load_dataset
from t2sql.retrieval import build_schema_context
from t2sql.retrieval.embeddings import embed_query
from t2sql.semantic.loader import load_semantic_layer

REPO_ROOT = Path(__file__).resolve().parents[1]
DEV_PATH = REPO_ROOT / "data" / "dev.jsonl"
CACHE_PATH = REPO_ROOT / "data" / "traces" / "self_consistency_dev_cache.jsonl"

N = 5
TEMPERATURE = 0.8
MAX_WORKERS = 8
THRESHOLD_CANDIDATES = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
MIN_CAUGHT = 5
MAX_FALSE_FIRE_RATE = 0.15


def _rule_missed_ambiguous_ids(items) -> set[str]:
    layer = load_semantic_layer()
    missed = set()
    for item in items:
        if not item.is_ambiguous:
            continue
        intent = parse_intent(item.question, layer=layer)
        fired = {d.type.value for d in detect_ambiguities(intent, layer)}
        if not (fired & set(item.ambiguity_types)):
            missed.add(item.id)
    return missed


def _load_cache() -> dict[str, dict]:
    if not CACHE_PATH.exists():
        return {}
    cache = {}
    with open(CACHE_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                cache[rec["id"]] = rec
    return cache


def generate() -> None:
    items = load_dataset(DEV_PATH)
    missed_ids = _rule_missed_ambiguous_ids(items)
    unambiguous = [it for it in items if not it.is_ambiguous]
    missed = [it for it in items if it.id in missed_ids]

    print(f"rule-missed ambiguous: {len(missed)}, unambiguous: {len(unambiguous)}")

    to_run = [("missed", it) for it in missed] + [("unamb", it) for it in unambiguous]
    cache = _load_cache()
    # a cached *error* record isn't a completed result -- retry it
    pending = [(kind, it) for kind, it in to_run if it.id not in cache or "error" in cache[it.id]]
    print(f"already cached (succeeded): {len(to_run) - len(pending)}, to run/retry: {len(pending)}")
    if not pending:
        return

    print("warming embedding model + precomputing schema contexts...")
    embed_query("warmup")
    contexts = {it.id: build_schema_context(it.question) for _, it in pending}
    print("done warming, starting generation")

    def work(kind_item: tuple[str, object]) -> dict:
        kind, it = kind_item
        try:
            result = compute_divergence(it.question, context=contexts[it.id], n=N, temperature=TEMPERATURE)
            return {
                "id": it.id,
                "kind": kind,
                "question": it.question,
                "true_types": it.ambiguity_types,
                "score": result.score,
                "largest_cluster_size": result.largest_cluster_size,
                "unparseable_count": result.unparseable_count,
                "distinct_signatures": result.distinct_signatures,
                "raw_sql": result.raw_sql,  # kept so a signature-extraction fix can re-score without re-generating
            }
        except Exception as e:  # keep going -- one bad item shouldn't kill the whole batch
            return {"id": it.id, "kind": kind, "question": it.question, "error": str(e)}

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(work, ki) for ki in pending]
        for fut in as_completed(futures):
            rec = fut.result()
            with open(CACHE_PATH, "a") as f:
                f.write(json.dumps(rec) + "\n")
            done += 1
            status = rec.get("error") or f"score={rec['score']:.2f}"
            print(f"[{done}/{len(pending)}] {rec['id']}: {status}")


def rescore() -> None:
    """Re-run signature extraction (no LLM calls) over cached raw_sql --
    use this after a fix to extract_signature instead of --generate, since
    the generated SQL itself doesn't change, only how it's compared.
    """
    from t2sql.clarify.self_consistency import extract_signature
    from collections import Counter

    cache = _load_cache()
    updated = 0
    for rec in cache.values():
        if "raw_sql" not in rec:
            continue
        signatures = [extract_signature(sql) for sql in rec["raw_sql"]]
        counts = Counter(signatures)
        largest = max(counts.values())
        rec["score"] = 1 - (largest / len(signatures))
        rec["largest_cluster_size"] = largest
        rec["unparseable_count"] = counts.get(None, 0)
        rec["distinct_signatures"] = [s.describe() for s in counts if s is not None]
        updated += 1

    with open(CACHE_PATH, "w") as f:
        for rec in cache.values():
            f.write(json.dumps(rec) + "\n")
    print(f"rescored {updated} cached items (no LLM calls made)")


def report() -> None:
    cache = _load_cache()
    missed_recs = [r for r in cache.values() if r.get("kind") == "missed" and "score" in r]
    unamb_recs = [r for r in cache.values() if r.get("kind") == "unamb" and "score" in r]
    errors = [r for r in cache.values() if "error" in r]

    print(f"cached: {len(missed_recs)} rule-missed ambiguous, {len(unamb_recs)} unambiguous, {len(errors)} errors")
    if errors:
        print("errors:", [(e["id"], e["error"]) for e in errors])

    print(f"\n{'threshold':>9} | {'caught (of missed)':>19} | {'false-fire rate (unamb)':>24} | {'PASS?':>5}")
    best = None
    for t in THRESHOLD_CANDIDATES:
        caught = sum(1 for r in missed_recs if r["score"] > t)
        false_fires = sum(1 for r in unamb_recs if r["score"] > t)
        rate = false_fires / len(unamb_recs) if unamb_recs else 0.0
        passed = caught >= MIN_CAUGHT and rate <= MAX_FALSE_FIRE_RATE
        print(f"{t:>9.2f} | {caught:>10}/{len(missed_recs):<8} | {false_fires:>10}/{len(unamb_recs)} ({rate:.1%})".ljust(60) + f"| {'PASS' if passed else '-'}")
        if passed and best is None:
            best = t

    print(f"\nchosen threshold: {best}" if best is not None else "\nno candidate threshold satisfies both constraints")

    if best is not None:
        print("\nrule-missed items caught at chosen threshold:")
        for r in missed_recs:
            if r["score"] > best:
                print(f"  {r['id']} (score={r['score']:.2f}, true_types={r['true_types']}): {r['question']}")
        print("\nunambiguous items false-fired at chosen threshold:")
        for r in unamb_recs:
            if r["score"] > best:
                print(f"  {r['id']} (score={r['score']:.2f}): {r['question']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--rescore", action="store_true")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()

    if args.generate:
        generate()
    if args.rescore:
        rescore()
    if args.report or not (args.generate or args.rescore):
        report()
