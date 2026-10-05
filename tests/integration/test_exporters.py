"""Rule-file export: managed blocks, audience filtering, idempotency."""
from __future__ import annotations

import pytest
from tests.learning.conftest import git

from mnemosyne.domain.dto import NewMemoryData
from mnemosyne.domain.enums import MemoryType
from mnemosyne.integrations.exporters import BEGIN_PREFIX, END, export, remove_block, upsert_block


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "api"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='api'\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "remote", "add", "origin", "git@github.com:acme/api.git")
    return root


@pytest.fixture
def seeded(container):
    container.memories.create_memory(NewMemoryData(
        title="Money as integer cents", content="Store amounts in minor units.", memory_type=MemoryType.RULE,
        scope="repo:github.com/acme/api", importance_score=0.9))
    container.memories.create_memory(NewMemoryData(
        title="Keep answers terse", memory_type=MemoryType.PREFERENCE, key="communication.verbosity", value="terse"))
    return container


def test_block_is_appended_and_replaced_without_touching_the_rest():
    original = "# Project\n\nHand-written guidance.\n"
    first = upsert_block(original, f"{BEGIN_PREFIX} x -->\nv1\n{END}")
    assert first.startswith("# Project\n\nHand-written guidance.\n\n<!-- mnemosyne:begin")
    second = upsert_block(first + "\n## Later hand edits\n", f"{BEGIN_PREFIX} x -->\nv2\n{END}")
    assert "v1" not in second and "v2" in second and "## Later hand edits" in second
    assert remove_block(second).startswith("# Project") and BEGIN_PREFIX not in remove_block(second)


def test_repo_audience_exports_only_repository_knowledge(seeded, repo):
    [result] = export(seeded, repo, targets=["claude"])
    text = (repo / "CLAUDE.md").read_text()
    assert result.action == "created"
    assert "Money as integer cents — Store amounts in minor units" in text
    assert "terse" not in text  # personal preferences stay out of committed files


def test_personal_audience_includes_personal_preferences(seeded, repo):
    export(seeded, repo, targets=["agents"], audience="personal")
    assert "Keep answers terse" in (repo / "AGENTS.md").read_text()


def test_export_is_idempotent_and_dry_run_writes_nothing(seeded, repo):
    assert export(seeded, repo, targets=["copilot"], dry_run=True)[0].action == "created"
    assert not (repo / ".github" / "copilot-instructions.md").exists()
    export(seeded, repo, targets=["copilot"])
    assert export(seeded, repo, targets=["copilot"])[0].action == "unchanged"


def test_cursor_rule_file_has_frontmatter(seeded, repo):
    export(seeded, repo, targets=["cursor"])
    text = (repo / ".cursor" / "rules" / "mnemosyne.mdc").read_text()
    assert text.startswith("---\ndescription:") and "alwaysApply: true" in text


def test_unknown_target_is_rejected(seeded, repo):
    with pytest.raises(ValueError):
        export(seeded, repo, targets=["notepad"])
