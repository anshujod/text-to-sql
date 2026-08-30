"""The naive baseline path: question + schema context in, SQL out, no clarification.

Deliberately makes silent choices on any ambiguity (which metric, which
entity, what time range, in/out-of-scope rows) and records each one in
`GeneratedSQL.assumptions` -- this is what the clarification engine is
measured against, and what the demo uses to show the baseline's blind spots.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from functools import lru_cache

from dotenv import load_dotenv
from openai import OpenAI

from t2sql.generation.models import GeneratedSQL
from t2sql.generation.trace import log_trace

load_dotenv()

SYSTEM_PROMPT = """\
You are a careful SQL analyst. Given schema context and a business question, \
write exactly one read-only SQL SELECT statement that answers it.

Rules:
- Output a single SELECT statement in the given dialect. No DDL/DML, no CTEs \
that write, no multiple statements, no markdown code fences, no commentary \
outside the structured fields.
- Only reference tables and columns that appear in the schema context. If the \
schema context doesn't contain what's needed to answer the question, still \
produce your best-effort SQL using what's available and reflect the gap in \
`assumptions`.
- Ambiguity is expected and NOT something to ask about here -- there is no \
user to ask. Silently resolve every ambiguous choice (which metric \
definition, which entity, what date range, whether to exclude \
cancelled/soft-deleted/internal rows, gross vs net, order-count vs \
unit-count, etc.) using the house defaults given in the schema context. \
Record every such choice you made as one short, plain-English sentence in \
`assumptions` -- specific enough that a reader could tell exactly what you \
assumed and second-guess it.
- `tables_used` must list every table your SQL references, and nothing else.
- `confidence` is your own honest estimate in [0, 1] of how likely this SQL \
answers the question the way a careful analyst would, given how much you had \
to assume.
"""


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    return OpenAI(
        base_url=os.environ["OPENROUTER_BASE_URL"],
        api_key=os.environ["OPENROUTER_API_KEY"],
    )


def _build_messages(question: str, context: str, dialect: str) -> list[dict[str, str]]:
    user_prompt = f"Dialect: {dialect}\n\nSchema context:\n{context}\n\nQuestion: {question}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def _build_repair_messages(
    question: str, context: str, dialect: str, previous_sql: str, error_message: str
) -> list[dict[str, str]]:
    user_prompt = (
        f"Dialect: {dialect}\n\nSchema context:\n{context}\n\nQuestion: {question}\n\n"
        f"Your previous attempt failed:\n{previous_sql}\n\n"
        f"Error:\n{error_message}\n\n"
        "Fix the SQL so it addresses this error. Keep using the same schema context "
        "and house defaults as before; only change what's needed to fix the error."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def _call_once(
    messages: list[dict[str, str]],
    question: str,
    dialect: str,
    model: str,
    temperature: float,
    kind: str = "generate",
) -> GeneratedSQL:
    start = time.monotonic()
    completion = _client().chat.completions.parse(
        model=model,
        messages=messages,
        response_format=GeneratedSQL,
        temperature=temperature,
        extra_body={"usage": {"include": True}},
    )
    latency_seconds = time.monotonic() - start

    parsed = completion.choices[0].message.parsed
    if parsed is None:
        raise ValueError(
            f"model returned no parseable structured output "
            f"(refusal: {completion.choices[0].message.refusal!r})"
        )

    usage = completion.usage
    log_trace(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "question": question,
            "dialect": dialect,
            "model": model,
            "temperature": temperature,
            "messages": messages,
            "response": parsed.model_dump(),
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None,
            "total_tokens": usage.total_tokens if usage else None,
            "cost": getattr(usage, "cost", None) if usage else None,
            "latency_seconds": latency_seconds,
        }
    )
    return parsed


def generate_sql(
    question: str,
    context: str,
    dialect: str = "postgres",
    temperature: float = 0.0,
    n: int = 1,
    model: str | None = None,
) -> GeneratedSQL | list[GeneratedSQL]:
    """Generate SQL for `question` given prebuilt schema `context`.

    `n` > 1 is for the self-consistency check: most OpenRouter-backed
    models (Claude included) silently ignore the OpenAI `n` parameter and
    return a single choice, so this issues `n` independent calls instead.
    Returns a single GeneratedSQL when n == 1, else a list of length n.
    """
    model = model or os.environ["OPENROUTER_GENERATION_MODEL"]
    messages = _build_messages(question, context, dialect)
    results = [_call_once(messages, question, dialect, model, temperature) for _ in range(n)]
    return results[0] if n == 1 else results


def repair_sql(
    question: str,
    context: str,
    previous_sql: str,
    error_message: str,
    dialect: str = "postgres",
    temperature: float = 0.0,
    model: str | None = None,
) -> GeneratedSQL:
    """Ask the model to fix `previous_sql` given the error it produced.

    Used by the execution repair loop (1.4): the error can come from static
    AST validation (e.g. a hallucinated column) just as well as from a real
    DB error, since both are just "here's what was wrong, try again."
    """
    model = model or os.environ["OPENROUTER_GENERATION_MODEL"]
    messages = _build_repair_messages(question, context, dialect, previous_sql, error_message)
    return _call_once(messages, question, dialect, model, temperature, kind="repair")
