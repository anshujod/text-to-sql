"""Execution result types.

ResultSet is deliberately narrow (just columns + rows) and carries the
stable fingerprint() used by the divergence gate to tell whether two
candidate interpretations of a question actually produced different data,
not just different-looking SQL. ExecutionResult is the fuller bookkeeping record:
types, timing, error, and how many repair attempts it took.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field


def _stringify_cell(value: Any) -> str:
    if value is None:
        return "\x00NULL"
    return str(value)


class ResultSet(BaseModel):
    columns: list[str]
    rows: list[list[Any]]

    def fingerprint(self) -> str:
        """Stable hash of this result's content.

        Insensitive to row order (SQL doesn't guarantee it without ORDER BY)
        and to column order, but sensitive to column names and cell values --
        two queries naming the same columns with the same values fingerprint
        identically regardless of how either was written.
        """
        row_dicts = [dict(zip(self.columns, row, strict=True)) for row in self.rows]
        canonical_rows = sorted(
            json.dumps({k: _stringify_cell(v) for k, v in row.items()}, sort_keys=True)
            for row in row_dicts
        )
        canonical = json.dumps({"columns": sorted(self.columns), "rows": canonical_rows})
        return hashlib.sha256(canonical.encode()).hexdigest()


class ExecutionResult(BaseModel):
    ok: bool
    sql: str
    result_set: ResultSet | None = None
    column_types: list[str] = Field(default_factory=list)
    row_count: int = 0
    wall_time_seconds: float = 0.0
    error: str | None = None
    timed_out: bool = False
    repair_attempts: int = 0
