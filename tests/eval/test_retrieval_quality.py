"""
Retrieval-quality regression guard.

Thresholds sit a little below the measured numbers (see
docs/EVALUATION.md) so that a change which makes retrieval meaningfully
worse fails CI, while noise from unrelated changes does not. Leakage —
surfacing another repository's memories — must stay at exactly zero.
"""
from __future__ import annotations

import pytest

from mnemosyne.evaluation.harness import evaluate, ndcg_at_k, recall_at_k, reciprocal_rank
from mnemosyne.evaluation.persona import HELDOUT_QUERIES, MEMORIES, QUERIES


@pytest.fixture(scope="module")
def in_sample():
    return evaluate("hybrid")


@pytest.fixture(scope="module")
def held_out():
    return evaluate("hybrid", queries=HELDOUT_QUERIES)


def test_in_sample_quality(in_sample):
    assert in_sample.ndcg_at_k >= 0.84, in_sample.summary()
    assert in_sample.mrr >= 0.84, in_sample.summary()
    assert in_sample.recall_at_k >= 0.88, in_sample.summary()


def test_held_out_quality(held_out):
    assert held_out.ndcg_at_k >= 0.72, held_out.summary()
    assert held_out.mrr >= 0.72, held_out.summary()


def test_no_cross_repository_leakage(in_sample, held_out):
    assert in_sample.leakage_rate == 0.0, [o.leaked for o in in_sample.outcomes if o.leaked]
    assert held_out.leakage_rate == 0.0, [o.leaked for o in held_out.outcomes if o.leaked]


def test_slot_resolution_prefers_repo_specific_convention(in_sample):
    """Inside legacy-billing, its unittest convention must win over the
    developer's general pytest preference — and pytest must not appear."""
    outcome = next(o for o in in_sample.outcomes if o.query.text == "write tests for the invoice generator")
    assert "lb_unittest" in outcome.ranked_labels[:3]
    assert "py_pytest" not in outcome.ranked_labels


def test_query_latency_is_interactive(in_sample):
    assert in_sample.p95_ms < 50, in_sample.summary()


def test_dataset_labels_reference_real_memories():
    labels = {m.label for m in MEMORIES}
    assert len(labels) == len(MEMORIES), "duplicate persona labels"
    for query in (*QUERIES, *HELDOUT_QUERIES):
        assert set(query.relevant) <= labels, query.text
        assert set(query.forbidden) <= labels, query.text


def test_metric_definitions():
    relevant = {"a": 3, "b": 2, "c": 1}
    assert recall_at_k(["a", "x", "b"], relevant, 5) == 1.0
    assert recall_at_k(["x", "a"], relevant, 1) == 0.0
    assert reciprocal_rank(["x", "a"], relevant) == 0.5
    assert ndcg_at_k(["a", "b", "c"], relevant, 3) == pytest.approx(1.0)
    assert ndcg_at_k(["c", "b", "a"], relevant, 3) < 1.0
