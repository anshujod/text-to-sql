from pathlib import Path

from t2sql.eval.dataset import DatasetItem, load_dataset

DEV_DATASET_PATH = Path(__file__).resolve().parents[1] / "data" / "dev.jsonl"


def test_dev_dataset_loads_and_validates() -> None:
    items = load_dataset(DEV_DATASET_PATH)

    assert len(items) == 10
    assert all(isinstance(item, DatasetItem) for item in items)
    assert len({item.id for item in items}) == 10, "item ids must be unique"
    assert all(item.gold_sql for item in items), "every item needs at least one gold interpretation"
