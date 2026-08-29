"""Splits the dataset into data/dev.jsonl and data/test.jsonl.

60/40 split, stratified by ambiguity type: every stratum is shuffled independently
with a fixed seed and split 60/40, so each split gets a proportional share
of every type rather than, say, all the COMPARISON items landing in test by
chance. Within each stratum we further sub-split by a secondary key --
expected_divergence for ambiguous items, difficulty for unambiguous items --
so near-miss items and difficulty levels are also spread proportionally,
not just the primary type.

Deterministic: same seed, same output, every run.

"""

from __future__ import annotations

import json
import random
import re
from collections import defaultdict
from pathlib import Path

from t2sql.eval.dataset import DatasetItem, load_dataset

SEED = 42
DEV_FRACTION = 0.6

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = [REPO_ROOT / "data" / "unambiguous.jsonl", REPO_ROOT / "data" / "ambiguous.jsonl"]
DEV_PATH = REPO_ROOT / "data" / "dev.jsonl"
TEST_PATH = REPO_ROOT / "data" / "test.jsonl"

_DIFFICULTY_RE = re.compile(r"difficulty:\s*(\w+)")


def _primary_key(item: DatasetItem) -> str:
    if not item.is_ambiguous:
        return "UNAMBIGUOUS"
    return item.ambiguity_types[0].value


def _secondary_key(item: DatasetItem) -> str:
    if item.is_ambiguous:
        return item.expected_divergence or "unlabeled"
    m = _DIFFICULTY_RE.search(item.notes)
    return m.group(1) if m else "unlabeled"


def _split_stratum(items: list[DatasetItem], rng: random.Random) -> tuple[list[DatasetItem], list[DatasetItem]]:
    shuffled = items[:]
    rng.shuffle(shuffled)
    n_dev = round(len(shuffled) * DEV_FRACTION)
    return shuffled[:n_dev], shuffled[n_dev:]


def split_items(items: list[DatasetItem], seed: int = SEED) -> tuple[list[DatasetItem], list[DatasetItem]]:
    # group by (primary, secondary) so both the ambiguity type and the
    # secondary axis (divergence / difficulty) are proportionally represented
    strata: dict[tuple[str, str], list[DatasetItem]] = defaultdict(list)
    for item in items:
        strata[(_primary_key(item), _secondary_key(item))].append(item)

    rng = random.Random(seed)
    dev: list[DatasetItem] = []
    test: list[DatasetItem] = []
    for key in sorted(strata):  # sorted so the split is stable regardless of dict insertion order
        stratum_dev, stratum_test = _split_stratum(strata[key], rng)
        dev.extend(stratum_dev)
        test.extend(stratum_test)

    # shuffle the final concatenated order too, so each file isn't visibly
    # grouped by stratum -- separate rng draw, still deterministic
    random.Random(seed + 1).shuffle(dev)
    random.Random(seed + 2).shuffle(test)
    return dev, test


def _write_jsonl(items: list[DatasetItem], path: Path) -> None:
    with open(path, "w") as f:
        for item in items:
            f.write(json.dumps(item.model_dump(mode="json", exclude_none=True)) + "\n")


def main() -> None:
    all_items: list[DatasetItem] = []
    for path in SOURCE_FILES:
        all_items.extend(load_dataset(path))

    assert len({item.id for item in all_items}) == len(all_items), "duplicate ids across source files"

    dev, test = split_items(all_items)

    _write_jsonl(dev, DEV_PATH)
    _write_jsonl(test, TEST_PATH)

    print(f"Total items: {len(all_items)}")
    print(f"dev:  {len(dev)} items -> {DEV_PATH}")
    print(f"test: {len(test)} items -> {TEST_PATH}")

    def _dist(items: list[DatasetItem]) -> dict[str, int]:
        d: dict[str, int] = defaultdict(int)
        for item in items:
            d[_primary_key(item)] += 1
        return dict(sorted(d.items()))

    print("\ndev distribution by primary key: ", _dist(dev))
    print("test distribution by primary key:", _dist(test))


if __name__ == "__main__":
    main()
