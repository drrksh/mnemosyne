"""Context compilation: layering, scope handling, slot resolution,
budgets, formats, and the end-to-end coverage guard."""
from __future__ import annotations

import json

import pytest

from mnemosyne.domain.dto import NewMemoryData
from mnemosyne.domain.enums import MemoryOrigin, MemoryType
from mnemosyne.domain.working_context import WorkingContext
from mnemosyne.evaluation.harness import evaluate_context
from mnemosyne.evaluation.persona import HELDOUT_QUERIES
from mnemosyne.services.context_service import ContextRequest

API = WorkingContext.create(repo_id="github.com/acme/api", languages=["python"])


def add(container, title, **kw):
    kw.setdefault("importance_score", 0.7)
    return container.memories.create_memory(NewMemoryData(title=title, content=kw.pop("content", title), **kw))


def titles(pack, reason=None):
    return [i.memory.title for i in pack.items if reason is None or i.reason == reason]


def test_profile_rules_apply_to_every_task(container):
    add(container, "Never force-push to shared branches", memory_type=MemoryType.RULE)
    add(container, "Structured logging with extra fields", memory_type=MemoryType.CONVENTION, scope="lang:python",
        key="python.logging", value="logging")
    pack = container.context.compile(ContextRequest(task="rename a variable", context=API))
    assert set(titles(pack, "profile")) == {
        "Never force-push to shared branches", "Structured logging with extra fields"
    }


def test_relevant_layer_carries_detail(container):
    add(container, "Money as integer cents", content="Store amounts as integer minor units with a currency code.",
        memory_type=MemoryType.RULE, scope="repo:github.com/acme/api", importance_score=0.3)
    pack = container.context.compile(ContextRequest(task="store the payment amount", context=API))
    [item] = [i for i in pack.items if i.reason == "relevant"]
    assert "integer minor units" in item.line()


def test_other_repositories_and_languages_never_appear(container):
    add(container, "Web uses pnpm", scope="repo:github.com/acme/web", memory_type=MemoryType.TOOLING,
        key="javascript.package_manager", value="pnpm")
    add(container, "Go errors wrap with %w", scope="lang:go", memory_type=MemoryType.PATTERN, key="go.errors",
        value="w")
    add(container, "API uses uv", scope="repo:github.com/acme/api", memory_type=MemoryType.TOOLING,
        key="python.package_manager", value="uv")
    pack = container.context.compile(ContextRequest(task="install dependencies and handle errors", context=API))
    assert titles(pack) == ["API uses uv"]


def test_repository_convention_overrides_general_habit(container):
    add(container, "Prefers pytest", scope="lang:python", memory_type=MemoryType.TOOLING,
        key="python.test_framework", value="pytest", origin=MemoryOrigin.EXPLICIT)
    add(container, "This repo uses unittest", scope="repo:github.com/acme/api", memory_type=MemoryType.TOOLING,
        key="python.test_framework", value="unittest", origin=MemoryOrigin.INFERRED)
    pack = container.context.compile(ContextRequest(task="write tests", context=API))
    assert titles(pack) == ["This repo uses unittest"]
    assert pack.overridden == 1
    elsewhere = container.context.compile(ContextRequest(task="write tests", context=WorkingContext.create(
        repo_id="github.com/acme/other", languages=["python"])))
    assert titles(elsewhere) == ["Prefers pytest"]


def test_languages_are_inferred_from_the_repository_overview(container):
    add(container, "api: stack overview", memory_type=MemoryType.ARCHITECTURE, scope="repo:github.com/acme/api",
        key="repo.overview", value="python", metadata={"languages": ["python"]})
    add(container, "Python: absolute imports", memory_type=MemoryType.CONVENTION, scope="lang:python",
        key="python.imports", value="absolute")
    pack = container.context.compile(ContextRequest(context=WorkingContext.create(repo_id="github.com/acme/api")))
    assert pack.context.languages == ("python",)
    assert "Python: absolute imports" in titles(pack)


def test_low_confidence_memories_are_not_injected(container):
    add(container, "Maybe uses tabs", memory_type=MemoryType.CONVENTION, key="style.indent", value="tabs",
        confidence_score=0.3)
    assert container.context.compile(ContextRequest(context=API)).items == []


def test_budget_and_character_cap_are_enforced(container):
    for i in range(80):
        add(container, f"Rule number {i} about keeping functions small and readable", memory_type=MemoryType.RULE)
    small = container.context.compile(ContextRequest(context=API, max_tokens=200))
    assert small.estimated_tokens <= 230 and small.omitted > 0
    capped = container.context.compile(ContextRequest(context=API, max_tokens=10_000, max_chars=1200, wrap=True))
    assert len(capped.text) <= 1200
    assert capped.text.startswith("<mnemosyne-context>") and capped.text.rstrip().endswith("</mnemosyne-context>")


def test_pinned_memories_always_lead(container):
    for i in range(30):
        add(container, f"Convention {i}", memory_type=MemoryType.CONVENTION, key=f"c.{i}", value="x",
            importance_score=0.9)
    add(container, "Pinned: the deploy freeze is on", memory_type=MemoryType.FACT, pinned=True, importance_score=0.1)
    pack = container.context.compile(ContextRequest(context=API, max_tokens=120))
    assert "Pinned: the deploy freeze is on" in titles(pack)


@pytest.mark.parametrize("fmt", ["markdown", "xml", "json"])
def test_formats(container, fmt):
    add(container, "Never force-push", memory_type=MemoryType.RULE)
    pack = container.context.compile(ContextRequest(context=API, format=fmt))
    if fmt == "json":
        payload = json.loads(pack.text)
        assert payload["sections"]["rules"][0]["title"] == "Never force-push"
    elif fmt == "xml":
        assert '<memory type="rule"' in pack.text and "<rules>" in pack.text
    else:
        assert "### Rules\n- Never force-push" in pack.text


def test_serving_context_records_access(container):
    memory = add(container, "Never force-push", memory_type=MemoryType.RULE)
    container.context.compile(ContextRequest(context=API))
    assert container.memories.get_memory(memory.id, track_access=False).access_count == 1
    container.context.compile(ContextRequest(context=API, record_access=False))
    assert container.memories.get_memory(memory.id, track_access=False).access_count == 1


def test_profile_without_repository_excludes_repository_memories(container):
    add(container, "Never force-push", memory_type=MemoryType.RULE)
    add(container, "API uses uv", scope="repo:github.com/acme/api", memory_type=MemoryType.TOOLING,
        key="python.package_manager", value="uv")
    assert titles(container.context.profile()) == ["Never force-push"]


def test_end_to_end_context_coverage():
    """Regression guard on what actually reaches the model."""
    in_sample = evaluate_context(max_tokens=1500)
    assert in_sample.coverage_essential >= 0.95, in_sample.summary()
    assert in_sample.coverage_relevant >= 0.9, in_sample.summary()
    assert in_sample.noise_rate <= 0.02, in_sample.summary()
    assert in_sample.leakage == 0.0
    held_out = evaluate_context(max_tokens=1500, queries=HELDOUT_QUERIES)
    assert held_out.coverage_essential >= 0.85, held_out.summary()
    assert held_out.leakage == 0.0
