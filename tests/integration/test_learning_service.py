"""LearningService end to end: ingestion decisions persisted correctly,
secrets never stored, feedback, and cross-repository generalization."""
from __future__ import annotations

from mnemosyne.domain.dto import SearchCriteria
from mnemosyne.domain.enums import EvidenceKind, FeedbackSignal, MemoryOrigin, MemoryStatus, MemoryType
from mnemosyne.domain.learning import CandidateMemory, EvidenceDraft, IngestAction


def cand(title="Uses pytest", *, key="python.test_framework", value="pytest", origin=MemoryOrigin.CONVERSATION,
         confidence=0.8, ref="s:1", scope="lang:python", content="", **kw):
    return CandidateMemory(
        title=title, content=content, memory_type=kw.pop("memory_type", MemoryType.TOOLING), origin=origin,
        confidence=confidence, key=key, value=value, scope=scope,
        evidence=[EvidenceDraft(EvidenceKind.CONVERSATION, ref, f"said: {title}")], **kw,
    )


def actions(report):
    return [o.action for o in report.outcomes]


def test_create_then_replay_is_idempotent(container):
    first = container.learning.ingest([cand()])
    replay = container.learning.ingest([cand()])
    assert actions(first) == [IngestAction.CREATED]
    assert actions(replay) == [IngestAction.SKIPPED]
    memory = container.memories.get_memory(first.outcomes[0].memory_id, track_access=False)
    assert memory.reinforcement_count == 1
    assert [e.source_ref for e in container.memories.list_evidence(memory.id)] == ["s:1"]


def test_independent_restatement_reinforces_without_new_version(container):
    first = container.learning.ingest([cand(confidence=0.7)])
    again = container.learning.ingest([cand(confidence=0.7, ref="s:2")])
    assert actions(again) == [IngestAction.REINFORCED]
    memory = container.memories.get_memory(first.outcomes[0].memory_id, track_access=False)
    assert memory.reinforcement_count == 2
    assert memory.confidence_score > 0.7
    assert memory.version == 1  # belief strength is not a content revision
    assert len(container.memories.list_evidence(memory.id)) == 2


def test_changed_mind_archives_old_memory_and_links_replacement(container):
    old = container.learning.ingest([cand(title="Format with black", key="python.formatter", value="black")])
    new = container.learning.ingest(
        [cand(title="Format with ruff format", key="python.formatter", value="ruff-format", ref="s:2")]
    )
    outcome = new.outcomes[0]
    assert outcome.action == IngestAction.SUPERSEDED
    assert (outcome.previous_value, outcome.new_value) == ("black", "ruff-format")
    assert "python.formatter: black -> ruff-format" in new.summary_line()

    old_memory = container.memories.get_memory(old.outcomes[0].memory_id, track_access=False)
    new_memory = container.memories.get_memory(outcome.memory_id, track_access=False)
    assert old_memory.status == MemoryStatus.ARCHIVED
    assert old_memory.superseded_by == new_memory.id
    assert new_memory.metadata["supersedes"] == str(old_memory.id)
    history = container.memories.list_versions(old_memory.id)
    assert history[-1].change_reason.startswith("superseded")


def test_inference_cannot_overrule_explicit_memory_but_is_recorded(container):
    container.learning.ingest([cand(origin=MemoryOrigin.EXPLICIT, confidence=0.95)])
    report = container.learning.ingest(
        [cand(title="Uses unittest", value="unittest", origin=MemoryOrigin.INFERRED, confidence=0.9, ref="repo:x")]
    )
    assert actions(report) == [IngestAction.KEPT_EXISTING]
    memory_id = report.outcomes[0].memory_id
    memory = container.memories.get_memory(memory_id, track_access=False)
    assert memory.value == "pytest" and memory.confidence_score == 0.95
    contradiction = [e for e in container.memories.list_evidence(memory_id) if e.weight < 0]
    assert contradiction and "unittest" in contradiction[0].excerpt


def test_secrets_are_redacted_before_storage(container):
    secret = "ghp_" + "A1b2C3d4" * 5
    report = container.learning.ingest(
        [CandidateMemory(title="CI token setup", content=f"The CI uses token {secret} for releases",
                         memory_type=MemoryType.FACT, origin=MemoryOrigin.CONVERSATION, confidence=0.8,
                         evidence=[EvidenceDraft(EvidenceKind.CONVERSATION, "s:9", f"token {secret}")])]
    )
    memory = container.memories.get_memory(report.outcomes[0].memory_id, track_access=False)
    assert secret not in memory.content
    assert "[REDACTED:github_token]" in memory.content
    assert all(secret not in e.excerpt for e in container.memories.list_evidence(memory.id))


def test_mostly_secret_candidates_are_dropped(container):
    report = container.learning.ingest(
        [CandidateMemory(title="key", content="AKIAIOSFODNN7EXAMPLE", memory_type=MemoryType.FACT,
                         origin=MemoryOrigin.CONVERSATION, confidence=0.9)]
    )
    assert actions(report) == [IngestAction.SKIPPED]
    assert container.memories.search_memories(SearchCriteria()).total == 0


def test_low_confidence_inference_is_not_stored(container):
    report = container.learning.ingest([cand(origin=MemoryOrigin.INFERRED, confidence=0.3)])
    assert actions(report) == [IngestAction.SKIPPED]
    assert "below threshold" in report.outcomes[0].reason


def test_near_duplicates_merge_within_and_across_batches(container):
    a = cand(title="Keep explanations short and lead with the code", key=None, value=None, scope="global",
             memory_type=MemoryType.PREFERENCE, ref="s:1")
    b = cand(title="Keep explanations short and lead with code", key=None, value=None, scope="global",
             memory_type=MemoryType.PREFERENCE, ref="s:2")
    report = container.learning.ingest([a, b])
    assert actions(report) == [IngestAction.CREATED, IngestAction.REINFORCED]
    c = cand(title="Keep the explanations short, lead with the code", key=None, value=None, scope="global",
             memory_type=MemoryType.PREFERENCE, ref="s:3")
    assert actions(container.learning.ingest([c])) == [IngestAction.REINFORCED]


def test_feedback_signals(container):
    memory_id = container.learning.ingest([cand(origin=MemoryOrigin.INFERRED, confidence=0.7)]).outcomes[0].memory_id
    helpful = container.learning.feedback(memory_id, FeedbackSignal.HELPFUL)
    assert helpful.importance_score > 0.5 and helpful.reinforcement_count == 2
    confirmed = container.learning.feedback(memory_id, FeedbackSignal.CONFIRM)
    assert confirmed.origin == MemoryOrigin.EXPLICIT and confirmed.confidence_score >= 0.95
    wrong = container.learning.feedback(memory_id, FeedbackSignal.WRONG, note="we moved off pytest")
    assert wrong.status == MemoryStatus.ARCHIVED
    kinds = [e.kind for e in container.memories.list_evidence(memory_id)]
    assert kinds.count(EvidenceKind.FEEDBACK) == 3


def _repo_convention(repo, value, author_share=1.0, ref=None):
    return cand(
        title=f"In this repository: tests use {value}",
        value=value,
        origin=MemoryOrigin.INFERRED,
        confidence=0.85,
        scope=f"repo:{repo}",
        ref=ref or f"repo:{repo}@abc",
        metadata={"author_share": author_share},
    )


def test_generalization_lifts_consistent_conventions_to_language_scope(container):
    container.learning.ingest([
        _repo_convention("gh/a", "pytest"),
        _repo_convention("gh/b", "pytest"),
        _repo_convention("gh/c", "unittest"),
    ])
    report = container.learning.generalize()
    assert actions(report) == [IngestAction.CREATED]
    general = container.memories.get_memory(report.outcomes[0].memory_id, track_access=False)
    assert general.scope == "lang:python"
    assert general.value == "pytest"
    assert general.origin == MemoryOrigin.SYSTEM
    assert general.title == "Tests use pytest"
    assert sorted(general.metadata["generalized_from"]) == ["gh/a", "gh/b"]
    # Running it again changes nothing.
    assert actions(container.learning.generalize()) == [IngestAction.SKIPPED]


def test_generalization_ignores_repos_the_developer_barely_touches(container):
    container.learning.ingest([
        _repo_convention("gh/a", "pytest", author_share=0.9),
        _repo_convention("gh/b", "pytest", author_share=0.05),
    ])
    assert container.learning.generalize().outcomes == []


def test_generalization_requires_agreement(container):
    container.learning.ingest([
        _repo_convention("gh/a", "pytest"),
        _repo_convention("gh/b", "pytest"),
        _repo_convention("gh/c", "unittest"),
        _repo_convention("gh/d", "unittest"),
    ])
    assert container.learning.generalize().outcomes == []


def test_ingest_audits_one_event_per_batch(container):
    container.learning.ingest([cand(), cand(title="x", key="python.linter", value="ruff", ref="s:5")])
    with container.uow_factory() as uow:
        events = uow.audit.list(action_prefix="learning.")
        uow.commit()
    assert len(events) == 1
    assert events[0].details["counts"]["created"] == 2
