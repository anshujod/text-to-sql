"""Dataset schema for the eval harness -- the scaffolding Phase 2's
ambiguous/unambiguous question set gets poured into.

`ambiguity_types` uses the 7-way taxonomy from PLAN.md's Phase 2 table
(METRIC/TEMPORAL/ENTITY/SCOPE/GRAIN/COMPARISON/RESULT_SHAPE) as a plain
Literal here -- Task 2.1 owns the real `AmbiguityType` enum (with per-type
policy and detection hints) in `t2sql.clarify.taxonomy`; this just needs the
same fixed vocabulary for validation, not the machinery around it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

AmbiguityType = Literal[
    "METRIC", "TEMPORAL", "ENTITY", "SCOPE", "GRAIN", "COMPARISON", "RESULT_SHAPE"
]


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
