"""The consolidation policy, rule by rule, with no database involved."""
from __future__ import annotations

from dataclasses import replace

from mnemosyne.core.consolidation import Consolidator, NearDuplicate, polarity
from mnemosyne.core.memory_engine import MemoryEngine
from mnemosyne.domain.dto import NewMemoryData
from mnemosyne.domain.enums import EvidenceKind, MemoryOrigin, MemoryType
from mnemosyne.domain.learning import CandidateMemory, EvidenceDraft, IngestAction
from mnemosyne.utils.time import utcnow

ENGINE = MemoryEngine()
NOW = utcnow()


def existing(origin=MemoryOrigin.INFERRED, value="pytest", confidence=0.7, **kw):
    memory = ENGINE.build_new_memory(
        NewMemoryData(title=kw.pop("title", "Uses pytest"), key=kw.pop("key", "python.test_framework"), value=value,
                      origin=origin, confidence_score=confidence, **kw)
    )
    return memory


def candidate(origin=MemoryOrigin.INFERRED, value="pytest", confidence=0.8, refs=("ref:1",), **kw):
    return CandidateMemory(
        title=kw.pop("title", "Uses pytest"),
        content=kw.pop("content", ""),
        memory_type=kw.pop("memory_type", MemoryType.TOOLING),
        origin=origin,
        confidence=confidence,
        key=kw.pop("key", "python.test_framework"),
        value=value,
        evidence=[EvidenceDraft(EvidenceKind.CODE, r, "x") for r in refs],
        **kw,
    )


def decide(cand, **kw):
    return Consolidator().decide(cand, now=NOW, **kw)


def test_new_slot_is_created():
    assert decide(candidate()).action == IngestAction.CREATED


def test_same_observation_twice_is_skipped():
    memory = existing()
    decision = decide(candidate(), slot_matches=[memory], known_refs={memory.id: {"ref:1"}})
    assert decision.action == IngestAction.SKIPPED


def test_reanalysis_refreshes_instead_of_compounding_confidence():
    memory = existing(confidence=0.9)
    decision = decide(candidate(confidence=0.6, refs=("ref:2",)), slot_matches=[memory])
    assert decision.action == IngestAction.REFRESHED
    assert decision.changes["confidence_score"] == 0.6  # the fresh measurement replaces the old one
    assert decision.changes["reinforcement_count"] == 2


def test_conversation_restatement_reinforces_with_diminishing_returns():
    memory = existing(origin=MemoryOrigin.CONVERSATION, confidence=0.7)
    first = decide(candidate(origin=MemoryOrigin.CONVERSATION, confidence=0.7, refs=("s:1",)), slot_matches=[memory])
    assert first.action == IngestAction.REINFORCED
    assert 0.7 < first.changes["confidence_score"] < 0.8
    stronger = replace(memory, confidence_score=first.changes["confidence_score"])
    second = decide(candidate(origin=MemoryOrigin.CONVERSATION, confidence=0.7, refs=("s:2",)), slot_matches=[stronger])
    gain_1 = first.changes["confidence_score"] - 0.7
    gain_2 = second.changes["confidence_score"] - stronger.confidence_score
    assert gain_2 < gain_1


def test_explicit_confirmation_upgrades_an_inferred_memory():
    memory = existing(origin=MemoryOrigin.INFERRED)
    decision = decide(candidate(origin=MemoryOrigin.EXPLICIT, title="I use pytest", refs=("s:1",)),
                      slot_matches=[memory])
    assert decision.action == IngestAction.REINFORCED
    assert decision.changes["origin"] == MemoryOrigin.EXPLICIT
    assert decision.changes["title"] == "I use pytest"


def test_developer_changing_their_mind_supersedes():
    memory = existing(origin=MemoryOrigin.CONVERSATION, value="black")
    decision = decide(candidate(origin=MemoryOrigin.CONVERSATION, value="ruff-format"), slot_matches=[memory])
    assert decision.action == IngestAction.SUPERSEDED
    assert decision.reason == "changed mind"


def test_newer_analysis_supersedes_older_inference():
    memory = existing(origin=MemoryOrigin.INFERRED, value="unittest")
    assert decide(candidate(value="pytest"), slot_matches=[memory]).action == IngestAction.SUPERSEDED


def test_inference_does_not_overrule_what_the_developer_said():
    memory = existing(origin=MemoryOrigin.EXPLICIT, value="pytest", confidence=0.95)
    decision = decide(candidate(origin=MemoryOrigin.INFERRED, value="unittest"), slot_matches=[memory])
    assert decision.action == IngestAction.KEPT_EXISTING
    assert decision.changes == {}  # explicit memories are not weakened by inference


def test_weaker_contradiction_lowers_non_explicit_confidence():
    memory = existing(origin=MemoryOrigin.CONVERSATION, value="pytest", confidence=0.8)
    decision = decide(candidate(origin=MemoryOrigin.INFERRED, value="unittest"), slot_matches=[memory])
    assert decision.action == IngestAction.KEPT_EXISTING
    assert decision.changes["confidence_score"] < 0.8


def test_slot_values_compare_normalized():
    memory = existing(value="Pytest")
    decision = decide(candidate(value=" pytest. ", refs=("ref:9",)), slot_matches=[memory])
    assert decision.action == IngestAction.REFRESHED


def test_unkeyed_exact_duplicate_reinforces_via_fingerprint():
    memory = existing(key=None, value=None, title="Keep answers short", origin=MemoryOrigin.CONVERSATION)
    cand = candidate(key=None, value=None, title="keep answers SHORT!", origin=MemoryOrigin.CONVERSATION,
                     memory_type=MemoryType.UNKNOWN)
    assert decide(cand, fingerprint_matches=[memory]).action == IngestAction.REINFORCED


def test_near_duplicate_with_opposite_polarity_is_a_contradiction():
    memory = existing(key=None, value=None, title="Always use tabs", origin=MemoryOrigin.CONVERSATION,
                      memory_type=MemoryType.PREFERENCE)
    cand = candidate(key=None, value=None, title="Never use tabs", origin=MemoryOrigin.CONVERSATION,
                     memory_type=MemoryType.PREFERENCE)
    decision = decide(cand, near_duplicates=[NearDuplicate(memory, 0.97)])
    assert decision.action == IngestAction.SUPERSEDED


def test_near_duplicate_below_threshold_creates():
    memory = existing(key=None, value=None, title="Use tabs", memory_type=MemoryType.PREFERENCE)
    cand = candidate(key=None, value=None, title="Use tabs please", memory_type=MemoryType.PREFERENCE)
    assert decide(cand, near_duplicates=[NearDuplicate(memory, 0.5)]).action == IngestAction.CREATED


def test_polarity():
    assert polarity("Never use mutable defaults") == -1
    assert polarity("Don't force-push") == -1
    assert polarity("Use pytest") == 1
