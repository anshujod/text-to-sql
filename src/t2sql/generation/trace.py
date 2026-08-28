"""Append-only JSONL logging for every LLM generation call.

One line per call: prompt, response(s), token counts, cost, and latency.
Never read back by the app itself -- it's for later inspection/eval, so a
flat append-only file keeps this dependency-free.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TRACES_DIR = Path(__file__).resolve().parents[3] / "data" / "traces"
GENERATION_TRACE_PATH = TRACES_DIR / "generation.jsonl"


def log_trace(record: dict[str, Any], path: Path = GENERATION_TRACE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
