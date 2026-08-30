from pathlib import Path

from t2sql.eval.dataset import DatasetItem, load_dataset

DEV_DATASET_PATH = Path(__file__).resolve().parents[1] / "data" / "dev.jsonl"


def test_dev_dataset_loads_and_validates() -> None:
    items = load_dataset(DEV_DATASET_PATH)

    # dev.jsonl is the 60% split of the 200-item dataset
    # (data/unambiguous.jsonl + data/ambiguous.jsonl), stratified by
    # ambiguity type via scripts/split_dataset.py.
    assert len(items) == 120
    assert all(isinstance(item, DatasetItem) for item in items)
    assert len({item.id for item in items}) == len(items), "item ids must be unique"
    assert all(item.gold_sql for item in items), "every item needs at least one gold interpretation"
