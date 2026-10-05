"""
Grid search over ranking parameters on the IN-SAMPLE persona queries.

The held-out queries are evaluated once, for the chosen configuration
only, and are never part of the objective. Usage:

    python benchmarks/tune_retrieval.py
"""
from __future__ import annotations

import itertools
import time

from mnemosyne.config import Settings
from mnemosyne.core import vocabulary
from mnemosyne.evaluation.harness import evaluate, format_report
from mnemosyne.evaluation.persona import HELDOUT_QUERIES

GRID = {
    "concept_share": [0.0, 0.1, 0.2, 0.3],
    "semantic_share": [0.3, 0.5, 0.7],
    "weight_scope": [0.08, 0.13, 0.2],
    "weight_relevance": [0.55, 0.7],
}
DOMAIN_WEIGHTS = [0.5, 1.0]


def objective(report) -> float:
    return 0.5 * report.ndcg_at_k + 0.3 * report.mrr + 0.2 * report.recall_at_k


def main() -> None:
    base = Settings(database_url="sqlite:///:memory:", app_env="testing", log_level="WARNING")
    results = []
    started = time.perf_counter()
    for domain_weight in DOMAIN_WEIGHTS:
        vocabulary.DOMAIN_CONCEPT_WEIGHT = domain_weight
        for values in itertools.product(*GRID.values()):
            overrides = dict(zip(GRID, values, strict=True))
            report = evaluate("hybrid", settings=base.model_copy(update=overrides))
            results.append((objective(report), domain_weight, overrides, report))
    results.sort(key=lambda r: r[0], reverse=True)
    print(f"{len(results)} configurations in {time.perf_counter() - started:.0f}s\n")
    for score, domain_weight, overrides, report in results[:8]:
        print(f"{score:.4f} domain_weight={domain_weight} {overrides} {report.summary()}")

    _, domain_weight, overrides, best = results[0]
    vocabulary.DOMAIN_CONCEPT_WEIGHT = domain_weight
    heldout = evaluate("hybrid", settings=base.model_copy(update=overrides), queries=HELDOUT_QUERIES)
    best.variant, heldout.variant = "in-sample", "held-out"
    print("\nChosen configuration:", {"domain_concept_weight": domain_weight, **overrides})
    print(format_report([best, heldout]))


if __name__ == "__main__":
    main()
