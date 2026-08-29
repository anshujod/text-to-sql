"""Dataset schema for the eval harness -- the scaffolding Phase 2's
ambiguous/unambiguous question set gets poured into.

`ambiguity_types` uses the real `AmbiguityType` enum from
`t2sql.clarify.taxonomy` (Task 2.1), which carries the per-type default
policy and detection hints alongside the fixed 7-way vocabulary.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from t2sql.clarify.taxonomy import AmbiguityType


class GoldInterpretation(BaseModel):
    sql: str
    interpretation: str = ""


class DatasetItem(BaseModel):
    id: str
    question: str
    is_ambiguous: bool
    ambiguity_types: list[AmbiguityType] = Field(default_factory=list)
    gold_sql: list[GoldInterpretation]
    notes: str = ""


def load_dataset(path: Path) -> list[DatasetItem]:
    """Load a JSONL dataset file, one DatasetItem per non-empty line."""
    items: list[DatasetItem] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(DatasetItem.model_validate(json.loads(line)))
    return items
