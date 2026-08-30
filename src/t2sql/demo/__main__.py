"""The demo interface.

A terminal walkthrough of the clarification engine, built around 6 curated
questions pulled straight from Task 4.5's real, already-paid-for held-out
test-set evaluation (`data/demo_presets.json`, extracted from
`results/test_ablation.raw.json`) -- not fresh LLM output. That's a
deliberate choice, not a shortcut: this project's remaining budget is
essentially $0, and every number this demo shows (the baseline SQL, the
"clarified" SQL, whether the system asked) is real data from a real
evaluation run, not a live regeneration for whatever a demo viewer happens
to type.

What *is* live, for every preset, at $0 (no LLM call): intent parsing,
rule-based ambiguity detection, the rendered question text for items the
real run asked about, and a divergence score computed by executing the
dataset's own labeled candidate interpretations against the live DB (Task
3.4's divergence test, reused for context here -- shown, not used to
decide whether to ask, since it scores a different candidate set than the
real run's self-consistency-driven gate did and can legitimately disagree
with it). The SQL results tables are also executed live, every run,
against the real seeded database. Whether each preset was actually asked
about, and what the resolved SQL was, comes from the real evaluation run.

Run: `make demo` or `uv run python -m t2sql.demo`
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from t2sql.clarify.detector import detect_ambiguities
from t2sql.clarify.divergence import compute_divergence_report
from t2sql.clarify.intent import parse_intent
from t2sql.clarify.policy import ClarificationAction, PolicyConfig, SessionState, decide_clarification
from t2sql.clarify.question import render_clarification_question
from t2sql.clarify.taxonomy import AmbiguityType, get_spec
from t2sql.db.connection import get_connection
from t2sql.execution.executor import execute
from t2sql.execution.models import ExecutionResult
from t2sql.semantic.loader import load_semantic_layer

PRESETS_PATH = Path(__file__).resolve().parents[3] / "data" / "demo_presets.json"
MAX_PREVIEW_ROWS = 5

console = Console()


def _load_presets() -> list[dict]:
    return json.loads(PRESETS_PATH.read_text())


def _results_table(title: str, result: ExecutionResult) -> Table:
    table = Table(title=title, title_justify="left", show_lines=False)
    if not result.ok or result.result_set is None:
        table.add_column("error", style="red")
        table.add_row(result.error or "unknown error")
        return table
    for col in result.result_set.columns:
        table.add_column(col)
    for row in result.result_set.rows[:MAX_PREVIEW_ROWS]:
        table.add_row(*[str(v) for v in row])
    if len(result.result_set.rows) > MAX_PREVIEW_ROWS:
        table.caption = f"... {len(result.result_set.rows) - MAX_PREVIEW_ROWS} more row(s)"
    return table


def _run_preset(preset: dict, layer, conn) -> None:
    console.print(Panel(preset["question"], title=f"[bold]{preset['id']}[/bold]", border_style="cyan"))

    intent = parse_intent(preset["question"], layer=layer)
    ambiguities = detect_ambiguities(intent, layer)

    console.print("[bold]Live detection (rule-based, $0):[/bold]")
    if ambiguities:
        for a in ambiguities:
            spec = get_spec(AmbiguityType(a.type))
            console.print(f"  • [yellow]{a.type.value}[/yellow] ({a.source}, confidence={a.confidence:.2f}) -- {spec.summary}")
            console.print(f"    candidates: {a.candidates}")
    else:
        console.print("  [dim]no rule-based ambiguity flagged[/dim]")

    console.print()
    console.print("[bold]Baseline (no clarification):[/bold]")
    baseline_result = execute(preset["baseline_sql"], conn=conn)
    console.print(_results_table("baseline result", baseline_result))

    # Shown for context only -- NOT fed into the ask/no-ask decision below.
    # The real evaluation's divergence gate (Task 3.4) scored self-consistency
    # samples, which weren't cached; scoring this question's own labeled gold
    # readings instead is a different (if related) measurement and can
    # legitimately disagree with what the real run's gate decided. Branching
    # on it here would make the demo contradict its own "real, already-
    # evaluated resolution" framing.
    if len(preset["gold_sql"]) >= 2:
        interpretations = [(g["label"] or f"reading_{i}", g["sql"]) for i, g in enumerate(preset["gold_sql"])]
        info_report = compute_divergence_report(interpretations, conn=conn)
        console.print(f"\n[bold]For context (live, $0 -- how much this question's known readings actually differ):[/bold]")
        console.print(f"  divergence={info_report.score:.2f} across: {', '.join(info_report.labels)}")

    # Branch on the real evaluation's own outcome, not a fresh recomputation --
    # decide_clarification here (rule confidence only, no divergence report)
    # is used only to get a real decision object to render a question from,
    # for the items the real run actually asked about.
    session = SessionState()
    decision = decide_clarification(ambiguities, session, config=PolicyConfig())

    console.print()
    if preset["asked"]:
        # Live rule-confidence recomputation usually agrees with the real
        # run's own ask/no-ask call, but the real gate for `full` combined
        # more signals than rule confidence alone (see the module
        # docstring) -- fall back to a plain restatement from the top
        # detected ambiguity rather than crash if it doesn't, since
        # render_clarification_question requires an ASK decision.
        if decision.action == ClarificationAction.ASK:
            question_text = render_clarification_question(decision)
        else:
            top = max(ambiguities, key=lambda a: a.confidence, default=None)
            question_text = f"Which {top.slot} did you mean?" if top else "Which reading did you mean?"
        console.print(Panel(question_text, title="[bold]Clarification question[/bold]", border_style="magenta"))
        console.print(
            "[dim]This project's remaining budget is ~$0, so this demo shows the real, already-evaluated "
            "resolution for this question rather than regenerating SQL live for whatever you type -- "
            "type anything to continue.[/dim]"
        )
        try:
            input("  > your answer: ")
        except EOFError:
            pass
        console.print()
        console.print(f"[bold]Resolved (using the evaluated answer: {preset['hidden_label']!r}):[/bold]")
        resolved_result = execute(preset["resolved_sql"], conn=conn)
        console.print(_results_table("clarified result", resolved_result))
    else:
        console.print(
            Panel(
                "This question's real evaluation run did not ask here. The live rule-confidence "
                "check above is shown for context; the actual gate that made this call combined "
                "signals (self-consistency, the divergence gate) not all reproduced live in this "
                "$0 demo -- see results/RESULTS.md for the full reasoning.",
                title="[bold]System declined to ask[/bold]",
                border_style="green",
            )
        )
        console.print("[dim]No second query needed -- baseline's own answer stands.[/dim]")

    console.print()
    console.rule(style="dim")


def main() -> None:
    presets = _load_presets()
    layer = load_semantic_layer()

    console.print(
        Panel(
            "Text-to-SQL with a clarification engine\n\n"
            "6 real questions from this project's held-out evaluation, showing the "
            "clarification round-trip and a baseline-vs-clarified comparison for each.",
            title="[bold cyan]Demo[/bold cyan]",
            border_style="cyan",
        )
    )

    with get_connection(role="readonly") as conn:
        while True:
            console.print()
            for i, p in enumerate(presets, 1):
                types = "/".join(p["ambiguity_types"])
                console.print(f"  [bold]{i}[/bold]. [{types}] {p['question']}")
            console.print("  [bold]q[/bold]. quit")
            console.print()
            try:
                choice = input("Pick a question (1-6, q): ").strip().lower()
            except EOFError:
                break
            if choice == "q":
                break
            if not choice.isdigit() or not (1 <= int(choice) <= len(presets)):
                console.print("[red]invalid choice[/red]")
                continue
            console.print()
            _run_preset(presets[int(choice) - 1], layer, conn)


if __name__ == "__main__":
    main()
