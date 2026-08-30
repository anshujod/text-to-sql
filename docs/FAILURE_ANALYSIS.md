# Failure analysis (held-out test set, 80 items)

Every category below is a real count from `results/test_ablation.raw.json` (the completed
test-set run) or a live, deterministic, $0 recomputation against the same test set — nothing
here is a hand-picked anecdote. `full` is the config under analysis unless stated otherwise.

## 1. Detection misses and why

10 of 41 ambiguous items (24%) get no detection signal at all from `full`, concentrated in
two types:

| type | misses / total | items |
|---|---|---|
| GRAIN | 5 / 7 (71%) | amb-064, amb-096, amb-060, amb-062, amb-059 |
| SCOPE | 4 / 8 (50%) | amb-051, amb-044, amb-048, amb-052 |
| ENTITY | 1 / 5 (20%) | amb-093 |

**GRAIN is the systematic gap.** All five misses are "average X per Y" questions ("average
number of sessions per user," "average refund amount," "average customer lifetime value")
where the rule detector's hints (`detection_hints` in `taxonomy.py`: "average", "per
customer", "per order") only fire on an explicit "per X" phrase in the question text. None of
these five questions say "per" at all — the grain ambiguity is implicit in what an average
*is* (average per order? per customer? per engaged user, excluding zeros?), not signaled by
a keyword. This is a detection-strategy gap, not a semantic-layer gap: `metrics.yaml` doesn't
need to change, the rule needs to fire on "average of an aggregate with no stated
denominator," not on a literal "per" token.

**SCOPE misses** are the inverse problem: `detection_hints` list explicit scope words
("cancelled," "refund," "internal," "deleted"), so a question that doesn't mention any scope
word at all never fires the rule — correctly, in isolation. But three of the four SCOPE
misses (amb-044, amb-048, amb-052) are catalog-count questions ("how many products do we
sell") where the *absence* of a stated scope is itself the ambiguity (include discontinued
products or not?), and nothing about that absence is lexically distinguishable from a
question that's genuinely unambiguous. Self-consistency detection is supposed to catch what
rules miss here, but its own recall is low (see `results/RESULTS.md`) — these items fall
through both mechanisms.

## 2. Over-asks that survived the gate

Two distinct failure shapes, both real:

**Near-miss items asked anyway**: 6 of 12 items labeled `expected_divergence: low` (the
dataset's deliberately-tricky "looks ambiguous, isn't" items) still get asked about by
`full` — amb-081, amb-083, amb-028, amb-078, amb-082, amb-100. Four of six are COMPARISON or
RESULT_SHAPE questions ("is order volume growing," "show our top customers") where the rule
confidence (0.70-0.85) sits well above the 0.30 policy threshold regardless of what the
actual data shows — the gate only intervenes when a `DivergenceReport` is computed and
attached, and for `full` that only happens when self-consistency's own samples produce 2+
distinct SQL variants to compare. When self-consistency converges on one candidate (no
disagreement to measure), there's nothing for the gate to veto with, and the rule confidence
alone decides — the exact scenario the gate exists to prevent, slipping through because its
own input was empty.

**Genuinely unambiguous items asked anyway**: 6 of 39 (15%) — unamb-075, unamb-082,
unamb-079, unamb-042, unamb-097, unamb-006. Spot-checking unamb-006 ("How many orders have
been delivered?"): the rule detector's SCOPE hint list has no positive check for "the
question already states a fully-specific status filter" — `detection_hints` are pure
keyword presence/absence, so a status word appearing *without* being one of the specific
scope keywords ("cancelled," "refund," etc.) still reads as "no explicit status/scope filter
on a table with a documented default" and fires anyway. The rule is checking for the absence
of specific words, not the presence of an answer.

## 3. SQL generation errors on unambiguous questions

Only 1 of 39 unambiguous items (`unamb-042`) produced a hard execution error rather than a
wrong-but-valid answer: `column o.deleted_at does not exist`. This is worth more than its
count suggests. Hallucinated columns are a known risk for this kind of pipeline, and the
right place to catch one is AST validation against the live schema, not the database error
it eventually causes — and the AST validator *did* catch it
(`validate_sql` returned `ok=False` for the raw generated SQL). The gap is downstream: this
evaluation's pipeline (`t2sql.eval.ablation`), unlike the full production path
(`t2sql.generation.repair_sql`), has no repair step — on a validation
failure it falls back to the original, still-broken SQL rather than retrying with the
validator's error fed back to the model. The validator did its job; the harness around it
didn't use the result. A real deployment (which does call the repair loop) would not show
this specific failure; an ablation harness that's supposed to approximate one probably
should.

21 more unambiguous items execute without error but land on the wrong answer — a generation
*quality* problem (see `results/RESULTS.md`'s Limitations: this whole run used a cheap model
for every config, including baseline), not a clarification-engine problem, and out of this
analysis's scope.

## 4. Cases where clarification options did not contain the user's real intent

Recomputed live (deterministic, $0): of the 36 test items where rule-based detection alone
decides to ask, 13 offer no option matching the item's real hidden answer. Filtering out the
6 that are unambiguous items being asked about at all (already covered in §2 — there's no
"real intent" to match when the question wasn't ambiguous to begin with), **6 genuine misses
on truly ambiguous items** split into two different causes:

**Structurally empty candidate lists** (amb-085, amb-007): both are RESULT_SHAPE
ambiguities, and the rule detector's `DetectedAmbiguity` for RESULT_SHAPE never populates
`candidates` — the row-count keyword hints ("top," "show me") are how RESULT_SHAPE gets
*detected*, but nothing in the detector proposes candidate row counts (5? 10? 20?) to offer.
The policy engine still emits an ASK decision with an empty options list plus the escape
hatch, so a user answering honestly can never actually resolve it. This is also the one
finding that contradicts the taxonomy's own documentation: `taxonomy.py` lists
RESULT_SHAPE's `default_policy` as `DEFAULT_AND_DISCLOSE`, but the live decision engine
scores every type uniformly by confidence and doesn't consult `default_policy` at
all (also noted in the README) — if it did, RESULT_SHAPE would never reach this broken ASK
state in the first place.

**Incomplete candidate synonym coverage** (amb-003, "Which product category performs
best?"): the rule detector offers `revenue_net`, `order_count`, `session_count` but not
`unit_count`, even though `unit_count` is exactly right for a "performs best" reading about
sales volume, and *is* offered for a differently-worded question in this same dataset
(amb-083, "best-selling products"). The synonym match is keyed off which literal words
appear in the question, not the underlying semantic category — "best-selling" trips a
unit-count-flavored synonym, "performs best" doesn't, even though a human reads them as the
same question about a different subject.

**A second-order effect worth naming**: amb-094, amb-017, and amb-100 also show up in the
raw miss list, but for a different reason than the two above — the rule detector fires on
METRIC-flavored vocabulary ("revenue last month," "top spending customers") in questions the
dataset's own annotation marks as TEMPORAL-only or RESULT_SHAPE-only ambiguous (single
`ambiguity_types` entry). The detector isn't wrong that the vocabulary is metric-adjacent;
it's over-triggering on words that are ambiguous *in general* but weren't constructed to be
ambiguous *in this specific item*. This is really an over-asking precision issue (§2) wearing
a "missed target" costume — flagged here because it's how it surfaced in this
recomputation, not because it's a new root cause.

## 5. Where the semantic layer itself was the bottleneck

Two of the four categories above trace back to the semantic layer, not the detection logic
built on top of it:

- **GRAIN has no representation at all** in `metrics.yaml` — a metric's grain (per-order,
  per-customer, per-engaged-user) isn't a modeled property anywhere the detector or policy
  engine can consult; it only exists as prose in `taxonomy.py`'s GRAIN description. Every
  GRAIN miss in §1 is downstream of this: there's no structured signal to detect against.
- **Metric synonym coverage is per-question-wording, not per-concept** — `unit_count`'s
  synonym list (§4) apparently includes "best-selling" but not "performs best," which is a
  gap in how thoroughly `metrics.yaml`'s synonym lists were populated, not a flaw in the
  matching logic itself.

RESULT_SHAPE's empty-candidates bug (§4) is *not* a semantic layer gap — row counts aren't a
semantic-layer concept, that's a detector/policy implementation gap.

## What I'd fix first

1. **RESULT_SHAPE's empty candidate list** (§4) — the cheapest, highest-confidence fix here.
   Either populate real candidates (5/10/20, matching `defaults.yaml`'s house default) or
   make the policy engine actually consult `taxonomy.py`'s `default_policy` and stop asking
   about this type at all, matching its own documented intent.
2. **A GRAIN-specific rule**: "an aggregate function with no stated grouping noun," rather
   than requiring a literal "per X" — closes the single largest miss category (§1).
3. **Wire the ablation harness's SQL generation through the repair loop** — the one hard
   execution error in §3 is a harness gap, not a generation gap; the fix already exists in
   `t2sql.generation.repair_sql`, it's just not called from `t2sql.eval.ablation`.
4. **Make the divergence gate handle "self-consistency converged" as a real signal**, not an
   absence of one — right now zero disagreement among samples means no `DivergenceReport` at
   all, silently falling back to rule confidence alone (§2's near-miss over-asks). Zero
   disagreement is itself informative and should push the gate *toward* declining to ask,
   not leave the decision to a different, unrelated signal.

## What a production version would need

- **Caching** — the divergence gate re-executes every candidate interpretation per query;
  a production system would cache by SQL hash (already how this project's own divergence
  test avoids re-executing identical candidates within one call) across queries, not just
  within one.
- **Multi-tenant schemas / RLS** — this project assumes one tenant, one schema, one
  `app_readonly` role. Real multi-tenant SQL generation needs tenant-scoped row-level
  security enforced below the generated SQL, not trusted to the model to include a
  `WHERE tenant_id = ...` clause correctly every time.
- **Query cost estimation** — before executing K candidate interpretations for the
  divergence gate, a production system should estimate query cost (`EXPLAIN`) and cap K or
  skip the gate for expensive queries, rather than assuming every candidate is cheap to run
  (true on this seed data, not guaranteed on a real warehouse).
- **A human review queue** — for the silent-error cases this project's whole design is built
  to reduce, not eliminate. Some fraction of confidently-wrong answers will always get
  through (§3's generation-quality failures aren't a clarification-engine problem to solve);
  a production system needs a path for a human to catch what the automated layer doesn't,
  not just a lower rate of it.
