# Retrieval evaluation

Mnemosyne's value depends on one question: *when a developer starts a
task, do the right memories come back, and do the wrong ones stay out?*
This document describes how that is measured, what the numbers are, and
where they come from.

## Method

**Corpus.** `src/mnemosyne/evaluation/persona.py` defines "Sam", a senior
backend engineer, through 101 memories: global preferences and rules,
Python / TypeScript / Go conventions, four repositories (a FastAPI
payments service, a Next.js dashboard, a Go CLI, and a legacy Python 3.8
service whose conventions *contradict* Sam's usual ones), decisions,
incidents, and 24 true-but-irrelevant "haystack" memories (meeting
times, coffee order, on-call rota...).

**Queries.** Phrased the way developers talk to an assistant ("write unit
tests for the refund service", "the page throws a hydration error"),
each with a working context (repository, languages) and graded
relevance labels: 3 = the answer must use this memory, 2 = clearly
relevant, 1 = useful background.

* **In-sample** — 41 queries used while developing and tuning.
* **Held-out** — 15 queries written and committed (commit `bc5d3d6`)
  *before* any tuning, never used to choose parameters or vocabulary.
  These are the honest estimate of quality on unseen phrasings.

**Metrics** (`src/mnemosyne/evaluation/harness.py`):

| Metric | Definition |
|---|---|
| Recall@5 | Share of grade ≥ 2 memories in the top 5 (denominator capped at 5) |
| MRR | Reciprocal rank of the first grade-3 memory |
| nDCG@5 | Graded gain 2^g − 1, log2 discount, normalized by the ideal order |
| Leakage | Share of queries whose top 5 contains a memory from a *different* repository (or a label the query forbids) |

Leakage is treated as a correctness property, not a quality nuance:
suggesting another repository's conventions is a wrong answer.

## Results

Default configuration, zero-dependency `hashing` embedder:

| Query set | Recall@5 | MRR | nDCG@5 | Leakage | p50 / p95 latency |
|---|---|---|---|---|---|
| In-sample (41) | 0.935 | 0.874 | 0.869 | **0.00** | 1.4 / 2.0 ms |
| Held-out (15) | 0.900 | 0.806 | 0.817 | **0.00** | 2.0 / 3.1 ms |

Results are deterministic: repeated runs give identical figures. That
doesn't make 0.817 a precise estimate. Ranking configurations that tie
on the in-sample objective differ by about ±0.03 nDCG on the 15 held-out
queries, so read held-out quality as **0.78–0.82**.

### Compiled context

Recall is the ranked list. What the assistant actually receives is the
compiled context (`evaluate_context`, `mnemosyne eval --context 1500`):
the task-relevant layer plus the always-on profile, packed into a token
budget.

| 1,500-token budget | Essential (grade 3) present | Relevant (grade ≥ 2) present | Haystack noise | Leakage | Avg size |
|---|---|---|---|---|---|
| In-sample | **100%** | 98.8% | 0.2% | 0 | ~540 tokens |
| Held-out | **93.3%** | 93.3% | 0% | 0 | ~540 tokens |

### What tuning did and did not achieve

Starting point (before any of the changes below): in-sample nDCG@5
0.807, held-out 0.784. Changes, each motivated by an in-sample failure:

1. Consistent stemming (`translate` / `translated` / `translating`;
   lemma output stemmed like any other word).
2. A *concept* facet signal (testing, deployment, money, i18n, …) so a
   query's kind of task can match memories that share no words with it.
3. Concept detection on whole tokens only (`payments-api` is a project
   name, not the "money" concept) and phrase triggers ("unit test", not
   "unit", which also matched "minor units").
4. Grid search over 144 configurations of the ranking weights
   (`benchmarks/tune_retrieval.py`). The top of the objective is a flat
   plateau, so the defaults sit at its center rather than at a fragile
   peak.

In-sample nDCG@5 rose from 0.807 to 0.869. **Held-out nDCG@5 moved from
0.784 to 0.817 (0.78–0.82 across tied configurations), so most of the
in-sample gain was fit to the in-sample queries.** That is reported here rather than hidden: the
held-out number is the one to quote.

### Ablations (in-sample)

`python -c "from mnemosyne.evaluation.harness import *; print(format_report([evaluate(v) for v in VARIANTS]))"`

| Variant | Recall@5 | MRR | nDCG@5 |
|---|---|---|---|
| hybrid (default) | 0.935 | 0.874 | 0.869 |
| lexical-only | 0.915 | 0.877 | 0.863 |
| semantic-only (hashing embedder) | 0.935 | 0.850 | 0.850 |
| relevance-only (no importance/scope/recency priors) | 0.900 | 0.815 | 0.819 |

The priors (importance, confidence, scope affinity, recency,
reinforcement) are worth about 0.05 nDCG. With the hashing embedder,
fusing lexical and semantic signals adds little over lexical alone —
expected, since both are lexical at heart; the fusion is there so a
neural embedder can contribute when configured. Scope *exclusion* — not
ranking — is what keeps leakage at zero.

### Known limits

* The hashing embedder captures lexical and morphological similarity
  plus the curated developer-concept layer; it does not know that
  "refund" relates to "money" unless the vocabulary says so. Queries
  that need open-domain synonymy ("implement partial refunds" → the
  money-handling rule) are the weakest. A neural embedder
  (`MNEMOSYNE_EMBEDDER=http` or `sentence-transformers`) is expected to
  help there; it has not been measured in this environment.
* Several "this always applies here" conventions (named exports, no
  `any`) are not *about* any particular task, so recall ranks them low.
  The context compiler handles them with an always-on profile layer for
  the active scope rather than relying on query relevance.
* 56 labeled queries over one persona is enough to catch regressions and
  compare variants, not to make fine-grained claims. Customers can run
  the same harness against their own labeled queries.

## Performance

`python benchmarks/scale.py N` (one principal, SQLite file database, this
development container):

| Memories | Write throughput | Index build | New-process load (snapshot) | Query p50 / p95 |
|---|---|---|---|---|
| 2,000 | ~890 / s | 179 ms | 71 ms | 6.6 / 8.6 ms |
| 10,000 | ~770 / s | 1.6 s | 212 ms | 20.0 / 25.4 ms |

(The 10,000-memory row was re-measured after deterministic tie-breaking
was added; the 2,000-memory row predates it.)

### Editor hook latency

`python benchmarks/hook_latency.py` runs the real `mnemosyne hook`
command the way Claude Code does: a new process per event, with the JSON
payload on stdin. It runs against the 101-memory persona:

| Hook | Cold process p50 / p95 | Warm daemon p50 / p95 |
|---|---|---|
| `SessionStart` (profile) | 709 / 789 ms | 100 / 112 ms |
| `UserPromptSubmit` (learn + relevant context) | 752 / 832 ms | 95 / 106 ms |

Cold time is almost entirely Python start-up and imports; the work itself
is tens of milliseconds. The benchmark fails if the daemon's output
differs from the cold path's by a single byte. That check caught
non-deterministic ordering of near-tied memories across processes, which
was fixed by quantising recency and breaking ties on memory id.

Warm queries reuse an in-process index validated by a cheap database
signature (2–6 ms). Short-lived processes such as editor hooks load an
on-disk snapshot validated by the same signature; the snapshot is read
through an allow-listed unpickler, so a tampered cache file cannot
instantiate arbitrary objects.

## Regression guard

`tests/eval/test_retrieval_quality.py` runs both query sets on every test
run and fails if in-sample nDCG@5 or MRR drops below 0.84, held-out below
0.72, recall below 0.88, p95 latency above 50 ms, or leakage above zero.

CI also runs `mnemosyne eval` on both query sets and the hook-latency
benchmark (including its identical-output check) on every pull request
(`.github/workflows/ci.yml`, job `quality`).
