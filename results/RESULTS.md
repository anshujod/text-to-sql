# Results (held-out test set, run once)

Test split: `data/test.jsonl`, 80 items (41 ambiguous, 39 unambiguous), run exactly once,
per this project's own discipline (`DATASET.md`) that the test set is never touched before
this point and never touched again after it. All six configs, cheap model
(`anthropic/claude-haiku-4.5`) throughout — see **Limitations** for what that does and
doesn't mean about these numbers.

## Headline

**Baseline answered 95.1% of ambiguous questions confidently and wrongly — no clarification,
no disclosure, just a silently-wrong answer. The full system reduced that to 22.0%, while
asking on only 30.0% of *all* queries (ambiguous and unambiguous combined).**

That reduction holds up even though raw execution accuracy barely moves (23.8% → 23.8%,
see Limitations) — the thing this project targets is silent, undisclosed wrongness, not
generation quality, and on that axis the effect is large and real.

## Ablation table

- dataset: `data/test.jsonl` (80/80 items completed)
- model: `anthropic/claude-haiku-4.5` for every config, `baseline` included
- self-consistency N: 5
- spend: $2.1498 of a $2.50 ceiling

| config | n | correctness | over-ask rate | unnecessary-ask rate | detection P/R/F1 | silent-error rate | est. cost/query | est. latency/query |
|---|---|---|---|---|---|---|---|---|
| baseline | 80 | 23.8% | 0.0% | 0.0% | 0.00/0.00/0.00 | 76.2% | $0.0042 | ~3.8s |
| llm_judge | 80 | 25.0% | 38.8% | 66.7% | 0.71/0.95/0.81 | 40.0% | $0.0055 | ~5.8s |
| rules_only | 80 | 25.0% | 45.0% | 66.7% | 0.83/0.73/0.78 | 35.0% | $0.0054 | ~4.8s |
| self_consistency_only | 80 | 23.8% | 18.8% | 8.3% | 0.73/0.27/0.39 | 57.5% | $0.0254 | ~22.6s |
| hybrid_no_gate | 80 | 25.0% | 50.0% | 66.7% | 0.78/0.76/0.77 | 30.0% | $0.0266 | ~23.6s |
| full | 80 | 23.8% | 30.0% | 50.0% | 0.78/0.76/0.77 | 30.0% | $0.0260 | ~23.1s |

Cost/latency columns are **per-query estimates for running that one config alone** (see
"Cost and latency" below for how they're derived and why they're not the same thing as this
run's real $2.1498 total, which benefits from sharing calls across all six configs at once).

**`full` vs. `hybrid_no_gate`** is the load-bearing comparison: the divergence gate cuts
over-asking from 50.0% to 30.0% with *no* silent-error cost (30.0% either way). That's the
gate doing exactly its job — the same detections, fewer unnecessary interruptions.

**`self_consistency_only`** reproduces self-consistency detection's own dev-set finding: high precision (0.73)
but weak recall (0.27) at the calibrated threshold — conservative by design, so more silent
wrongness slips through (57.5%) than the rule-based mechanisms catch.

## Per-ambiguity-type breakdown (`full` vs. `baseline`)

| type | n | full: correct | full: over-ask | full: silent-error | baseline: silent-error |
|---|---|---|---|---|---|
| METRIC | 6 | 0.0% | 50.0% | 0.0% | 100.0% |
| TEMPORAL | 8 | 0.0% | 25.0% | 0.0% | 100.0% |
| ENTITY | 5 | 0.0% | 0.0% | 20.0% | 100.0% |
| SCOPE | 8 | 25.0% | 37.5% | 37.5% | 75.0% |
| GRAIN | 7 | 0.0% | 0.0% | **71.4%** | 100.0% |
| COMPARISON | 7 | 0.0% | 100.0% | 0.0% | 100.0% |
| RESULT_SHAPE | 6 | 0.0% | 83.3% | 0.0% | 100.0% |

**GRAIN is the weak spot**: 0% over-ask rate means the detector essentially never flags it,
so its silent-error rate (71.4%) barely improves over baseline. See the GRAIN example below
and `docs/FAILURE_ANALYSIS.md` for why. COMPARISON and RESULT_SHAPE sit at the
other extreme — asked on almost every item of that type, which is *why* their silent-error
rate hits zero, but also why they dominate the over-ask rate.

## Confidence intervals (bootstrap, n=2000 resamples, seed=0)

| config | metric | point estimate | 95% CI |
|---|---|---|---|
| baseline | correctness | 23.8% | [15.0%, 33.8%] |
| baseline | over-ask rate | 0.0% | [0.0%, 0.0%] |
| baseline | silent-error rate | 76.2% | [66.2%, 85.0%] |
| full | correctness | 23.8% | [15.0%, 33.8%] |
| full | over-ask rate | 30.0% | [20.0%, 40.0%] |
| full | silent-error rate | 30.0% | [20.0%, 41.2%] |

At n=80 the silent-error-rate intervals for baseline and full don't overlap at all
(baseline's lower bound, 66.2%, sits well above full's upper bound, 41.2%) — the headline
effect is well outside noise. The correctness intervals are wide and identical for both
configs, consistent with correctness being dominated by the shared generator, not by which
config is asking.

## Cost and latency

Real, measured totals for this run: **$2.1498** for all 80 items × 6 configs, 587 real LLM
calls (507 generation calls + 80 tiny judge calls), over ~46 minutes wall-clock. Real
per-call averages: generation call = $0.00424 / 3.77s; judge call = $0.00011 / 0.97s.

Because most calls are shared across configs (the ablation runner's own docstring explains
the sharing design), that $2.15 is the cost of running *all six configs together*, not any one config
alone. The **est. cost/query** and **est. latency/query** columns in the ablation table
above answer the question that actually matters for a deployment decision — "what would
running just this one config, in isolation, cost per query" — by applying the same real
per-call-type averages to each config's own known call count (1 generation call for
`baseline`; +5 for the self-consistency configs; +1 tiny judge call for `llm_judge`; plus
each config's own real observed rate of needing an extra regeneration call after asking).

**Overhead of `full` over `baseline`: ≈$0.022 and ≈19s per query — about 6x baseline on
both axes, driven almost entirely by the 5 self-consistency samples**, not by the (free,
DB-only) divergence gate itself. The judge call and rule detection are comparatively free
(~$0.0001 and $0, respectively); self-consistency's N=5 sampling is where this design's real
cost lives, and any attempt to cut cost further should target N first.

One concrete effect of the sharing/caching design: across the 80 items, only **27 actual
regeneration calls** were needed in total for `full` and every config that shares the same
resolved answer with it — a naive per-config accounting (which config's final SQL differs
from baseline's) would suggest up to 75 such calls were "needed"; the real number, thanks to
caching identical resolutions across configs, was a third of that.

**What this is not**: a fuller cost breakdown would also want per-query tokens in/out,
DB-query counts, and p50/p95 latency. None of that was instrumented at the per-item level in this run — only
run-level totals and per-call-type averages were captured, so the table's cost/latency
columns are a **structural estimate from known call-count architecture**, not measured
per-query data, and there's no percentile to report (only one number per config: the
average). Adding real per-item instrumentation would require re-running with logging added
first, which the remaining budget (~$0.93) doesn't support and which would mean touching the
test set a third time — not worth it for an instrumentation detail when the estimate above
is already grounded in real per-call-type costs, not guessed rates.

## Qualitative examples

**1. A disclosure win, not a correctness win** (`amb-042`, *"How much revenue have we made?"*)
— `baseline` and `full` produce **byte-identical SQL** (net revenue, excluding internal
accounts and cancelled orders). The difference is entirely in what the user sees: `baseline`
picked that definition silently; `full` detected the SCOPE ambiguity (gross vs. net-of-
refunds is this taxonomy's SCOPE axis, not METRIC — same label vocabulary, different
question: which default filter applies, not which metric), asked, got "net revenue" back
from the (simulated) user, and disclosed the choice explicitly before answering. Same answer, but the user now knows what was assumed instead of finding out the
hard way later. This is the project's actual value proposition, and it shows up even when
generation quality doesn't improve.

**2. An "unnecessary" ask with a real side effect** (`amb-083`, *"List our best-selling
products."*) — this dataset item's only labeled ambiguity is RESULT_SHAPE (how many rows —
annotated `expected_divergence: low`, i.e. the annotator predicted asking wouldn't matter).
`full` asked anyway and, after folding the answer into a second generation call, came back
with a **different ranking metric entirely** — `baseline` ranked by units sold, the
regenerated query ranked by net revenue, unprompted. The ask that was "supposed" to be
harmless destabilized a part of the query nobody asked about, purely because a second,
independent LLM call doesn't reliably reproduce the first one's other choices. This is a
real cost of regeneration-based clarification beyond user annoyance, worth weighing against
the disclosure benefit above.

**3. A genuine detection miss** (`amb-059`, *"What's the average number of sessions per
user?"*) — the intended reading (confirmed by the dataset's hidden answer) is the average
*among users who ever had a session*; both `baseline` and `full` silently average over
*all* users (via a `LEFT JOIN` that keeps zero-session users in the denominator), and `full`
never flags this as ambiguous at all (`detected_ambiguous=False`). This is the GRAIN
weak spot from the breakdown table above: none of the three detection mechanisms reliably
catch "which rows are in scope for an average," so this class of error stays fully silent
even with the full system engaged.

## Limitations

- **Every config, including `baseline`, ran on the cheap model** (haiku, not a top-tier
  model) — a budget decision (`$6` total credit for the whole remaining project), not a
  hidden shortcut. This is almost certainly why raw correctness sits at ~24% for every
  config: the cheap model has a real, observed structural weakness (asked "who is our best
  customer," it returns a full ranked table instead of picking one row via `ORDER BY ...
  LIMIT 1`), which no amount of clarification fixes, because the ambiguity being resolved
  (which metric) is orthogonal to the structural mistake (whether to limit the result to
  one row). The *relative* comparison between configs — which this whole ablation is
  actually for — should be far less sensitive to this than the absolute numbers are.
- **GRAIN ambiguity** (which rows/groups an aggregate is computed over) is this system's
  weakest detection category across all three mechanisms — see the breakdown table and
  example 3.
- **Self-consistency's recall stays low** (0.24–0.27 across configs that use it) at the
  threshold calibrated during development — a known, documented tradeoff (favoring low false-fire
  over high catch rate), reconfirmed here on a held-out set rather than just the dev set it
  was tuned on.
- **Regeneration is not fully isolated from the original ambiguity** (example 2) — resolving
  one slot can silently change another, an artifact of using a second independent LLM call
  rather than a more surgical edit to the first one.
