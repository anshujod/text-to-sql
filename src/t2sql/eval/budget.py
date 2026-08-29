"""Task 4.3's hard spend cap.

Every LLM call already goes through `t2sql.generation.generate._call_once`,
which logs one JSONL record per call to `data/traces/generation.jsonl`
including OpenRouter's own reported `cost` (real dollars billed, not an
estimate from a guessed per-token rate). `BudgetGuard` tails that same file:
it remembers how many lines existed when it was created, and `check()` sums
the `cost` field of every line appended since, raising `BudgetExceeded`
before the ceiling is crossed.

"""

from __future__ import annotations

import json
from pathlib import Path

from t2sql.generation.trace import GENERATION_TRACE_PATH


class BudgetExceeded(RuntimeError):
    def __init__(self, spent_usd: float, ceiling_usd: float) -> None:
        self.spent_usd = spent_usd
        self.ceiling_usd = ceiling_usd
        super().__init__(f"spent ${spent_usd:.4f} >= ceiling ${ceiling_usd:.2f} -- refusing further LLM calls")


class BudgetGuard:
    def __init__(self, ceiling_usd: float, trace_path: Path = GENERATION_TRACE_PATH) -> None:
        self.ceiling_usd = ceiling_usd
        self.trace_path = trace_path
        self._start_line_count = self._line_count()
        self.spent_usd = 0.0
        self.calls_with_unknown_cost = 0

    def _line_count(self) -> int:
        if not self.trace_path.exists():
            return 0
        with open(self.trace_path) as f:
            return sum(1 for _ in f)

    def refresh(self) -> float:
        """Re-sum spend from every trace line appended since this guard was
        created. Safe to call as often as needed -- it's just a file read.
        """
        if not self.trace_path.exists():
            self.spent_usd = 0.0
            return self.spent_usd

        with open(self.trace_path) as f:
            lines = f.readlines()

        total = 0.0
        unknown = 0
        for line in lines[self._start_line_count :]:
            cost = json.loads(line).get("cost")
            if cost is None:
                unknown += 1
                continue
            total += cost

        self.spent_usd = total
        self.calls_with_unknown_cost = unknown
        return self.spent_usd

    def check(self) -> None:
        """Call before every LLM call this run makes. Raises BudgetExceeded
        instead of letting one more call through once the ceiling's hit.
        """
        self.refresh()
        if self.spent_usd >= self.ceiling_usd:
            raise BudgetExceeded(self.spent_usd, self.ceiling_usd)
