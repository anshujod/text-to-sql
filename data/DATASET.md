# Dataset

200 hand-constructed, hand-verified natural-language questions against the
seeded e-commerce schema (`docker/init/sql/schema.sql`), split into
`dev.jsonl` (120) and `test.jsonl` (80). This is the benchmark the whole
project is built to move a number on -- the most valuable artifact in the
repo.

> **Test-set discipline.** `data/test.jsonl` is touched exactly once, at
> the very end of the project, and never again. Every detector threshold,
> every prompt, every policy-engine rule is tuned against `data/dev.jsonl`
> only. This file records that discipline so it's checkable, not just
> claimed.

## Files

| File | Items | Role |
|---|---|---|
| `data/unambiguous.jsonl` | 100 | Source: single-interpretation questions |
| `data/ambiguous.jsonl` | 100 | Source: multi-interpretation questions |
| `data/dev.jsonl` | 120 (60%) | Iterate here through the whole build |
| `data/test.jsonl` | 80 (40%) | Untouched until the final evaluation |

`dev.jsonl` and `test.jsonl` are produced from the two source files by
`scripts/split_dataset.py` (deterministic, fixed seed) and re-checked by
`scripts/validate_dataset.py`. Nobody hand-edits the split files directly --
to change an item, edit the relevant source file and re-run both scripts.

## Construction method

Every question was either written by hand or drafted and then individually
verified against the live seeded database (`docker compose up`, `make
seed`) -- no gold SQL was accepted on the strength of "looks right." For
each item:

- the gold SQL was executed against `app_readonly` and its result inspected
  (not just checked for "no error")
- for the unambiguous set, the question wording was checked to admit
  exactly one reasonable reading -- scope decisions (cancelled orders,
  refunds, internal accounts, soft-deletes, currency) are stated explicitly
  in the question text rather than left to a house default
- for the ambiguous set, every interpretation's result was computed and a
  divergence signal (relative difference for scalar answers, Jaccard
  overlap of top-N ids for ranked lists) was checked before assigning
  `expected_divergence`. Several items were relabeled after the computed
  number contradicted the original guess -- e.g. "average refund amount"
  was assumed to split like AOV per-order-vs-per-customer (a large gap) but
  turned out identical, because no order in this seed data has more than
  one refund row.
- `scripts/validate_dataset.py` re-runs every gold SQL through the same AST
  validator (`t2sql.validation.ast_validator`) that generated SQL is
  checked with, plus id-uniqueness and duplicate/near-duplicate question
  detection, on every source and split file.

## Label definitions

### Item-level

| Field | Meaning |
|---|---|
| `id` | Stable id, `unamb-NNN` / `amb-NNN` |
| `question` | The natural-language question |
| `is_ambiguous` | `true` for items sourced from `ambiguous.jsonl`, `false` for `unambiguous.jsonl` |
| `ambiguity_types` | 0 or more of the 7 `AmbiguityType` values (`src/t2sql/clarify/taxonomy.py`); empty for unambiguous items. A multi-label item (e.g. METRIC + TEMPORAL) is ambiguous along more than one axis at once. |
| `expected_divergence` | Ambiguous items only: the annotator's prediction of whether the interpretations produce *meaningfully* different results -- `high` or `low`. This is what the measured `DivergenceReport` (the divergence gate) gets validated against. |
| `gold_sql` | 1 entry for unambiguous items, 2-4 for ambiguous items |
| `notes` | Free text: for unambiguous items, a `difficulty: <tag>` prefix (see below) plus rationale; for ambiguous items, the verification numbers behind the `expected_divergence` call |

### Per-interpretation (`gold_sql[i]`)

| Field | Meaning |
|---|---|
| `sql` | Executable gold SQL for this reading |
| `interpretation` | Short human-readable label for this reading |
| `label` | Machine-friendly slug (e.g. `revenue_net`, `calendar_month`) -- ambiguous items only |
| `clarification_answer` | What a user would type back if asked to disambiguate -- ambiguous items only, feeds the simulated user used in evaluation |

### `expected_divergence: low` -- the near-miss items

27 of the 100 ambiguous items are deliberate near-misses: phrasing that
*looks* ambiguous but whose interpretations converge on essentially the
same answer. These exist to validate that the clarification system doesn't
over-ask -- the over-ask rate and unnecessary-ask rate metrics are measured
against exactly this label. Examples, with the verified gap:

- *"What's our average order value for the past month?"* -- calendar-month
  vs. trailing-30-day AOV differ by only 4.7%, even though the underlying
  order/revenue totals differ by 40%+ (both shrink together)
- *"What's the average refund amount?"* -- per-refund-row and
  per-refunded-order averages are exactly identical (no order in this seed
  data has received more than one refund)
- *"How many customers are marked internal or staff accounts?"* -- the
  customer-grain and user-grain counts happen to coincide (6 internal
  logins across 6 distinct customers)
- *"Has the Electronics category grown this year?"* -- H1-vs-H2 and
  Q1-vs-Q4 2025 agree on the *sign* of the answer (yes) even though the
  exact growth percentage differs by baseline
- The `RESULT_SHAPE` items are mostly near-misses by construction: "top 5"
  is a strict prefix of "top 10" for a ranked-by-revenue list, so the
  identity of the leader is unaffected by the row-count ambiguity

### `difficulty` (unambiguous items only, in `notes`)

| Tag | Meaning | Target share |
|---|---|---|
| `single_table` | No joins | ~30% |
| `one_join` | One join, aggregate | ~40% |
| `multi_join` | 2+ joins | ~20% (combined with `window`) |
| `window` | Window function | |
| `subquery_cte` | Subquery or CTE | ~10% |

## Split methodology

`scripts/split_dataset.py` combines the two source files (200 items) and
splits 60/40 stratified by ambiguity type: every stratum -- the 7
`AmbiguityType` values, plus `UNAMBIGUOUS` for unambiguous items -- is
shuffled independently (fixed seed, `random.Random(42)`) and split 60/40,
then concatenated and re-shuffled. Within each type stratum, items are
further sub-split by a secondary key (`expected_divergence` for ambiguous
items, `difficulty` for unambiguous items) so near-miss items and
difficulty levels are also spread proportionally between dev and test, not
just the primary type. Re-running the script is a no-op (deterministic).

## Distribution

### By primary ambiguity type (first tag, or `UNAMBIGUOUS`)

| Type | dev | test | total |
|---|---|---|---|
| UNAMBIGUOUS | 61 | 39 | 100 |
| METRIC | 10 | 6 | 16 |
| TEMPORAL | 9 | 6 | 15 |
| SCOPE | 8 | 6 | 14 |
| GRAIN | 8 | 6 | 14 |
| COMPARISON | 9 | 7 | 16 |
| ENTITY | 8 | 5 | 13 |
| RESULT_SHAPE | 7 | 5 | 12 |
| **Total items** | **120** | **80** | **200** |

(Ambiguity-type *tag* counts run slightly higher than primary-type counts
above, since 16 items carry two labels -- 10 in dev, 6 in test.)

### By `expected_divergence` (ambiguous items only)

| | dev | test | total |
|---|---|---|---|
| high | 44 | 29 | 73 |
| low (near-miss) | 15 | 12 | 27 |
| **Total ambiguous** | **59** | **41** | **100** |

### By `difficulty` (unambiguous items only)

| | dev | test | total |
|---|---|---|---|
| single_table | 18 | 12 | 30 |
| one_join | 26 | 17 | 43 |
| multi_join | 6 | 4 | 10 |
| window | 4 | 2 | 6 |
| subquery_cte | 7 | 4 | 11 |
| **Total unambiguous** | **61** | **39** | **100** |

### Interpretation count (ambiguous items)

dev: 58 items with 2 interpretations, 1 item with 3.
test: 41 items with 2 interpretations.
No item in either split needed a wider interpretation range -- every
ambiguity here resolved cleanly into 2 (occasionally 3) genuinely distinct
readings; going further started to feel like manufacturing readings nobody
would actually mean.

## Known limitations

- **Ambiguity-type tagging is single-annotator.** All 200 items were typed
  by one pass of judgment (mine), not cross-checked by a second rater.
  Inter-annotator agreement was not measured -- a real limitation for a
  benchmark whose central claim is about *when a question is ambiguous*.
- **`expected_divergence` is a prediction, not a ground truth.** It's
  exactly what the divergence gate exists to validate (or refute) against a
  measured `DivergenceReport`. Treat the 73/27 high/low split as a
  hypothesis the project is testing, not an established fact.
- **Multi-label stratification uses only the first tag.** An item tagged
  `[METRIC, TEMPORAL]` is stratified as METRIC; the split methodology
  above sub-stratifies by divergence/difficulty but not by the *second*
  ambiguity type, so a rare type that only appears as a second label could
  end up unevenly distributed. With only 16 multi-label items total this
  is a minor effect, not zero.
- **Near-duplicate detection is lexical, not semantic.** `validate_dataset.py`
  flags questions sharing >=90% of their normalized tokens. It would miss a
  genuine paraphrase ("How many orders came in last month?" vs "What was
  last month's order volume?") and can still false-positive on short,
  legitimately-distinct scoped variants near the threshold -- two such
  pairs were manually reviewed and confirmed not-duplicates during
  construction (see git history for `scripts/validate_dataset.py`'s
  threshold tuning).
- **Seed-data-specific numbers age with the seed generator.** Every
  verified gap (e.g. "4.7% apart," "6 internal logins") is true of the
  current `scripts/generate_seed.py` output. Regenerating the seed with
  different parameters would require re-running `scripts/validate_dataset.py`
  and spot-checking the `expected_divergence` labels, not just re-running
  the gold SQL.
- **All 200 questions are English, single-turn.** No paraphrase-of-a-
  paraphrase robustness testing, no multi-turn follow-up questions in this
  set (that's the session-state mechanism, tested separately).
- **The near-miss/high split (27/73) is not calibrated to any target
  ratio.** It reflects what was actually found verifying against this seed
  data, not an attempt to hit a specific "how often should the system ask"
  number -- that number is an output of evaluation, not an input here.
