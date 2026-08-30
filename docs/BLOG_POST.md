# Don't ask unless it matters: a clarification layer for text-to-SQL

Ask a text-to-SQL system "who is our best customer?" and it will answer. Confidently. With a
real customer name and a real number. What it won't tell you is that "best" was never
defined — the model picked one reading (usually revenue, sometimes order count) and moved
on, the same way it picks a join path it's never seen and hopes for the best.

That's the actual failure mode of text-to-SQL in production, and it's a worse one than
"wrong answer." A wrong answer that looks wrong gets caught. A wrong answer that looks
exactly like a right answer — formatted table, real customer name, plausible number — gets
trusted, and it gets trusted precisely because the system never signaled it wasn't sure. The
person reading the dashboard has no way to tell "I computed this confidently" from "I
guessed, and I'm not going to mention that."

I built a small clarification layer to test whether that's fixable, and how much it actually
costs to fix — not costs in the abstract, but measured: how often does it have to interrupt
someone, and what does that interruption buy. The honest answer, from a held-out 80-question
test set: yes, a lot — silent wrongness on ambiguous questions drops from 95% to 22% — but
not for free, and not evenly across every kind of ambiguity. This is the story of building
that, what worked, and one significant piece that didn't.

## What "ambiguous" actually means here

The first problem with "detect ambiguity" as a design goal is that it's not one problem.
"Who is our best customer" and "how many orders came in last month" are both ambiguous, but
in completely different ways, and a system that treats them the same way — one keyword list,
one threshold, one kind of question — will do badly at both. So before writing any detection
code, the actual work was cataloguing the specific, concrete ways a question against this
particular schema admits more than one defensible reading. Seven showed up, repeatedly,
across a hundred hand-written ambiguous questions:

- **METRIC** — a vague success term ("best," "top," "most valuable") that maps to more than
  one real metric: highest revenue, most orders, most sessions.
- **TEMPORAL** — "last month" could mean the calendar month or a trailing 30-day window, and
  those two windows land on genuinely different data around a seasonal spike in this
  project's seed data.
- **ENTITY** — "customers" could mean the customers table (one row per real-world buyer) or
  the users table (one row per login — several logins can share a customer).
- **SCOPE** — does the count include cancelled orders? Refunded amounts? Internal staff
  accounts? Soft-deleted rows? The question rarely says.
- **GRAIN** — "average order value" could be total revenue divided by order count, or the
  average of each customer's own average — arithmetically different quantities.
- **COMPARISON** — "is this category growing?" relative to what baseline? Month-over-month
  can read as decline right after a seasonal spike that a trailing-average comparison reads
  as growth.
- **RESULT_SHAPE** — "top customers" with no stated count. Low-stakes, but LIMIT 5 and LIMIT
  10 are literally different result sets.

Every one of these has a worked example in the project, with real SQL for each reading,
executed against the real seed data, with the actual divergence measured — not asserted.
That groundwork mattered more than any individual piece of detection logic downstream: it's
the difference between "ask a model if this is ambiguous" (which is what most systems do,
and which conflates seven different problems into one vague judgment call) and having seven
specific, checkable hypotheses about what could be wrong with a given question.

## The pipeline

The system a question actually passes through, front to back:

1. **Schema retrieval.** Embed the question, compare against embeddings of each table's
   description, pull the top few relevant tables (plus anything needed to join them) rather
   than dumping the entire schema into every prompt. Free — no model call, just vector
   similarity.
2. **Intent parsing.** A rule-based pass that pulls out the structured slots a question might
   specify: which metric, which entity, what time range, what filters, how many rows, how to
   sort. Also free.
3. **Detection**, two independent mechanisms running in parallel:
   - **Rule-based**: keyword and pattern matching against the semantic layer's own synonym
     lists — cheap, fast, precise when it fires, and only as good as the keyword list.
   - **Self-consistency**: generate five candidate SQL queries at high temperature and check
     whether the model agrees with itself. If it doesn't, that disagreement *is* a signal,
     independent of any hand-written rule.
4. **The divergence gate.** This is the piece the rest of this post is actually about — see
   below.
5. **The policy engine.** A pure function: given what's been detected, what's already been
   resolved earlier in the conversation, and how many times this session has already asked —
   decide to ask, or to pick a sensible default and say so out loud. Never silent either way.
   Capped at two clarifications per session, because a system that keeps asking is failing in
   a different way than a system that never asks.
6. **Resolution.** If the system asks and gets an answer, that answer gets folded into a
   fresh generation call. If it defaults, the default and the reasoning get surfaced in the
   output, not buried in a log.
7. **Validation and execution.** Every candidate — baseline or clarified — gets checked
   against the live database schema before it's allowed to run (catching hallucinated
   columns before they become a database error, not after), then executed read-only with a
   timeout and a row cap.

Everything through step 3's rule-based half, all of the divergence gate, and the entire
policy engine runs without touching a model at all — the only steps that cost anything are
the two detection paths that call an LLM (self-consistency's five samples) and generation
itself. That's a deliberate design constraint, not an afterthought: a clarification layer
that itself needs an expensive model call just to decide whether to ask a *cheap* question is
solving the wrong problem.

## The divergence insight

Here's the idea the whole project rests on: don't ask because a question looks ambiguous —
ask because two plausible readings of it would give visibly different answers. If "average
order value" could mean per-order or per-customer, and on this data those two numbers happen
to be $47.20 and $47.80, nobody needs to be bothered. If they're $47 and $312 because one
customer placed four hundred tiny orders, that's a real fork in the road.

The mechanism is almost embarrassingly simple once you see it: take the candidate SQL for the
different plausible readings, actually execute them against the real database, and compare
the *results* — not the SQL text (semantically identical queries can look nothing alike),
the actual rows. Rank-overlap for lists, relative difference for scalars, correlation for
time series. If the results converge, default to the sensible choice and disclose it. If they
genuinely diverge, that's when you ask.

This is the one piece of the system I'd defend hardest, because it's the one that
distinguishes "ambiguous" from "ambiguous and consequential," and only the second kind is
worth a human's attention. It's also cheap relative to everything else in the pipeline —
once candidate SQL already exists, comparing results is a handful of read-only database
queries, not another model call.

## A concrete example

"Who is our best customer?" against this project's seed data, live, no cherry-picking: the
baseline path silently ranks by revenue and returns a top five — Michael Clarke, Christy
Bolton, Charles Baker, Alex Nguyen, Monica Coleman, 9 to 16 orders each, $30K–$40K in
tracked revenue. Detected as METRIC-ambiguous, asked, and told "number of orders" instead of
revenue: the resolved top five is Michelle Phillips, Hunter Spencer, Christopher Mayer, Mary
Hebert, Robert Brown — 43 to 45 orders each.

Zero names in common. Same question, same underlying data, same database — a completely
different answer depending on one word nobody defined. That's not a contrived example built
to make a point; it's what actually happens when you ask this specific, ordinary-sounding
question of this dataset, and it's exactly the gap this project exists to surface instead of
paper over.

## The evaluation

None of the above means anything without a number attached to it, so the project is built
around a 200-question hand-verified benchmark: 100 unambiguous questions (single defensible
reading, scope decisions stated explicitly in the question) and 100 ambiguous ones (every
interpretation's SQL written, executed, and its actual divergence measured before the item
was labeled — several items got relabeled after the computed divergence contradicted the
original guess about how different the answers would be). Split 120/80 into a development set
and a held-out test set that gets touched exactly once, at the very end, specifically so that
every threshold and every design decision along the way is answerable without having peeked
at the number that's supposed to be the final grade.

Six configurations went through that held-out set: a plain baseline with no detection at all;
a single "is this ambiguous?" LLM call, which is the thing most people build first; rule-based
detection alone; self-consistency sampling alone; the two combined; and the full system with
the divergence gate on top.

| config | correctness | over-ask rate | silent-error rate |
|---|---|---|---|
| baseline | 23.8% | 0.0% | 76.2% |
| llm_judge | 25.0% | 38.8% | 40.0% |
| rules_only | 25.0% | 45.0% | 35.0% |
| self_consistency_only | 23.8% | 18.8% | 57.5% |
| hybrid_no_gate | 25.0% | 50.0% | 30.0% |
| **full** | 23.8% | **30.0%** | **30.0%** |

Two things jump out. First, correctness barely moves across any configuration — that's a
budget artifact of the whole remaining project running on a few dollars of API credit, which
meant every configuration here, baseline included, ran on the cheap end of the available
models rather than a top-tier one. It's a real limitation on the absolute numbers, and it's
genuinely not the point of the table. Second, and this is the actual headline: **silent-error
rate drops from 76% to 30%** even though raw correctness doesn't improve, because a
wrong-but-disclosed answer is not the same failure as a wrong-and-confident one, and this
project's whole premise is that the second kind is the one worth eliminating.

The row that earns the divergence gate its place in the pipeline is `full` versus
`hybrid_no_gate`: identical detection signal feeding into both, but the gate cuts over-asking
from 50% to 30% with *zero* cost to the silent-error rate. Same catches, a third fewer
interruptions — that's the gate doing exactly the job it was built for, not a marginal
improvement, a structural one.

## The honest thing that didn't work

I built the self-consistency detector — generate five candidate queries at high temperature,
see if the model disagrees with itself — expecting it to be the workhorse: catch what rules
can't, since it doesn't need a hand-written keyword list for every kind of ambiguity. On the
development set, calibrating its threshold, I got it to a comfortable false-fire rate. On the
actual held-out test set, its recall came in at 0.24–0.27. It's *precise* — when it flags
something, it's usually right — but it misses roughly three-quarters of the real ambiguity in
the test set on its own.

The honest reason, digging into it: this project deliberately used a cheap, fast model for
detection, and that model is inconsistent about applying its own defaults across five
independent samples — not because the underlying question is ambiguous, but because it's
sloppy. Some of that sample-to-sample disagreement is real signal. A lot of it is noise that
happens to look like signal, and a stronger model would very likely close a meaningful chunk
of that gap. I didn't get to test that directly, because retuning a whole detector against a
better model wasn't in the remaining budget. That's a real limitation, not a caveat buried in
a footnote — self-consistency is currently the weakest of the three signals in this pipeline,
and I'd want to re-run it against a stronger model before trusting it in anything real.

A second, smaller honest failure, found while digging through the test-set results
afterward: one ambiguity type — the row-count question, "top customers" with no stated
number — turned out to have a real bug, not just a weak signal. The rule detector correctly
flags it as ambiguous but never actually proposes candidate row counts to offer, so the
system asks a question it has no way to let anyone actually answer. It's a small, specific,
fixable bug, and finding it by reading through every real failure on the held-out set rather
than trusting the aggregate metrics is exactly the kind of thing an aggregate ablation table
alone will never surface.

## Why not just always ask?

Because the over-ask rate is the actual product. A system that interrupts on every ambiguous
question is trivially safe and trivially annoying, and annoying systems get bypassed,
ignored, or replaced — which puts you right back at silently-wrong-and-trusted, just with
extra steps in between. The number that matters isn't "did we catch the ambiguity," it's "did
we catch it while asking as rarely as we could get away with." `full` asks on 30% of *all*
queries — ambiguous and unambiguous combined — and that's the number I'd defend in a review,
not the detection recall in isolation.

There's a cost dimension to this too, worth stating plainly: the divergence gate itself is
essentially free (database queries, no model call), but the self-consistency sampling that
feeds it is not — roughly six times the cost and latency of the plain baseline path, almost
entirely from generating five extra candidates. If a production version of this needed to cut
that cost, the self-consistency sampling is exactly where the budget should be spent trimming
first, not the divergence check itself.

The short version, and the thing I'd want a reader to leave with: asking less, more
precisely, is a harder engineering problem than asking more, and it's the only version of a
clarification layer I'd actually want sitting between a person and a database they're trying
to trust.
