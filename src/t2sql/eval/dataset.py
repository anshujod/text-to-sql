"""Dataset schema for the eval harness -- the scaffolding Phase 2's
ambiguous/unambiguous question set gets poured into.

`ambiguity_types` uses the real `AmbiguityType` enum from
`t2sql.clarify.taxonomy` (Task 2.1), which carries the per-type default
policy and detection hints alongside the fixed 7-way vocabulary.
"""

from __future__ import annotations

import json
from pathlib import Path

from typing import Literal

from pydantic import BaseModel, Field

from t2sql.clarify.taxonomy import AmbiguityType


class GoldInterpretation(BaseModel):
    sql: str
    interpretation: str = ""
    # Populated for ambiguous items (Task 2.3): `label` is the short name of
    # this reading (e.g. "revenue_net"), `clarification_answer` is what the
    # simulated user (Task 4.2) types back when asked to disambiguate.
    label: str = ""
    clarification_answer: str = ""


class DatasetItem(BaseModel):
    id: str
    question: str
    is_ambiguous: bool
    ambiguity_types: list[AmbiguityType] = Field(default_factory=list)
    gold_sql: list[GoldInterpretation]
    notes: str = ""
    # Task 2.3: the annotator's prediction of whether the interpretations
    # actually produce meaningfully different result sets. `low` marks the
    # deliberate near-miss items -- superficially ambiguous phrasing whose
    # interpretations converge anyway. Task 3.4 validates this prediction
    # against the measured DivergenceReport.
    expected_divergence: Literal["high", "low"] | None = None


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
