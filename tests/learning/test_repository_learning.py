"""Repository analysis against real git repositories with known conventions."""
from __future__ import annotations

from mnemosyne.domain.dto import SearchCriteria
from mnemosyne.domain.enums import MemoryOrigin, MemoryStatus
from mnemosyne.domain.learning import IngestAction
from mnemosyne.learning.repo.analyzer import AnalysisOptions, RepositoryAnalyzer
from mnemosyne.learning.workspace import detect_repo, detect_working_context
from tests.learning.conftest import git


def findings_by_key(analysis):
    return {f.key: f for f in analysis.findings if f.key}


def test_repository_identity_uses_normalized_remote(python_repo):
    identity = detect_repo(python_repo / "app")
    assert identity.repo_id == "github.com/acme/payments"
    assert identity.root == python_repo
    assert identity.branch == "main"
    assert len(identity.head_sha) == 40


def test_working_context_detects_languages(ts_repo, python_repo):
    ts = detect_working_context(ts_repo)
    assert ts.repo_id == "github.com/acme/dashboard"
    assert {"typescript", "javascript"} <= set(ts.languages)
    assert detect_working_context(python_repo, file_path="app/invoice.py").languages[0] == "python"


def test_python_project_conventions_and_tooling(python_repo):
    analysis = RepositoryAnalyzer(AnalysisOptions(author_email="dev@example.com")).analyze(python_repo)
    found = findings_by_key(analysis)
    expected = {
        "python.package_manager": "uv",
        "python.linter": "ruff",
        "python.type_checker": "mypy",
        "python.type_strictness": "strict",
        "python.test_framework": "pytest",
        "python.web_framework": "fastapi",
        "python.orm": "sqlalchemy",
        "python.line_length": "100",
        "python.indent": "4 spaces",
        "python.quotes": "double",
        "python.docstring_style": "google",
        "python.data_containers": "dataclasses",
        "python.union_syntax": "pep604",
        "python.generic_syntax": "builtin",
        "commit.style": "conventional",
        "ci": "github-actions",
        "repo.python_version": ">=3.12",
    }
    for key, value in expected.items():
        assert key in found, f"missing {key}; got {sorted(found)}"
        assert found[key].value == value, (key, found[key].value)
    assert "PostgreSQL" in analysis.stack.data_stores
    assert "make test" in found["repo.commands"].content
    overview = found["repo.overview"]
    assert "FastAPI" in overview.content and "Python" in overview.content


def test_commit_analysis_only_counts_the_developers_commits(python_repo):
    analysis = RepositoryAnalyzer(AnalysisOptions(author_email="dev@example.com")).analyze(python_repo)
    commit = findings_by_key(analysis)["commit.style"]
    assert "11 of 11" in commit.content or "100% of 11" in commit.content
    assert analysis.author_share == round(11 / 14, 3)
    teammate_view = RepositoryAnalyzer(AnalysisOptions(author_email="teammate@example.com")).analyze(python_repo)
    assert "commit.style" not in findings_by_key(teammate_view)  # too few commits to judge


def test_typescript_project_conventions(ts_repo):
    found = findings_by_key(RepositoryAnalyzer().analyze(ts_repo))
    assert found["javascript.package_manager"].value == "pnpm"
    assert found["javascript.test_framework"].value == "vitest"
    assert found["javascript.e2e_framework"].value == "playwright"
    assert found["javascript.meta_framework"].value == "next.js"
    assert found["typescript.strict"].value == "true"  # parsed despite JSONC comments and trailing commas
    assert found["javascript.quotes"].value == "single"
    assert found["react.components"].value == "function"
    assert "pnpm test" in found["repo.commands"].content


def test_learning_a_repository_is_idempotent_per_commit(container, python_repo):
    first = container.learning.learn_repository(str(python_repo), author_email="dev@example.com")
    assert first.report.counts["created"] >= 15
    second = container.learning.learn_repository(str(python_repo), author_email="dev@example.com")
    assert second.report.counts["created"] == 0
    assert second.report.counts["skipped"] == len(second.report.outcomes)


def test_a_new_commit_refreshes_and_a_changed_convention_supersedes(container, python_repo):
    container.learning.learn_repository(str(python_repo), author_email="dev@example.com")
    (python_repo / "poetry.lock").write_text("")
    (python_repo / "uv.lock").unlink()
    git(python_repo, "add", "-A")
    git(python_repo, "commit", "-q", "-m", "chore: switch to poetry")
    report = container.learning.learn_repository(str(python_repo), author_email="dev@example.com").report

    changed = {o.key: o for o in report.outcomes if o.action == IngestAction.SUPERSEDED}
    assert changed["python.package_manager"].previous_value == "uv"
    assert changed["python.package_manager"].new_value == "poetry"
    assert report.counts["refreshed"] > 5

    old = container.memories.get_memory(changed["python.package_manager"].replaced_memory_id, track_access=False)
    assert old.status == MemoryStatus.ARCHIVED


def test_dry_run_stores_nothing(container, python_repo):
    result = container.learning.learn_repository(str(python_repo), dry_run=True)
    assert result.report is None and result.analysis.findings
    assert container.memories.search_memories(SearchCriteria()).total == 0


def test_repository_memories_are_scoped_and_inferred(container, python_repo):
    container.learning.learn_repository(str(python_repo), author_email="dev@example.com", generalize=False)
    result = container.memories.search_memories(SearchCriteria(page_size=200))
    assert result.total > 0
    assert {m.scope for m in result.items} == {"repo:github.com/acme/payments"}
    assert {m.origin for m in result.items} == {MemoryOrigin.INFERRED}
    assert all(m.metadata.get("author_share") is not None for m in result.items)


def test_cross_repository_generalization(container, make_python_repo):
    for name in ("alpha", "beta", "gamma"):
        container.learning.learn_repository(str(make_python_repo(name, "pytest")), author_email="dev@example.com")
    general = container.memories.search_memories(SearchCriteria(scopes=["lang:python"], keys=["python.test_framework"]))
    assert general.total == 1
    memory = general.items[0]
    assert memory.value == "pytest"
    assert memory.origin == MemoryOrigin.SYSTEM
    assert sorted(memory.metadata["generalized_from"]) == [
        "github.com/acme/alpha", "github.com/acme/beta", "github.com/acme/gamma"
    ]


def test_generalization_does_not_override_an_explicit_preference(container, make_python_repo):
    from mnemosyne.domain.dto import NewMemoryData

    container.memories.create_memory(NewMemoryData(title="I prefer unittest", scope="lang:python",
                                                   key="python.test_framework", value="unittest"))
    for name in ("alpha", "beta"):
        container.learning.learn_repository(str(make_python_repo(name, "pytest")), author_email="dev@example.com")
    general = container.memories.search_memories(SearchCriteria(scopes=["lang:python"], keys=["python.test_framework"]))
    assert [m.value for m in general.items] == ["unittest"]


def test_analysis_works_without_git(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    for i in range(12):
        (root / f"mod_{i}.py").write_text(f'def function_{i}(value: int) -> int:\n    return value + {i}\n')
    analysis = RepositoryAnalyzer().analyze(root)
    assert analysis.identity.repo_id.startswith("local/")
    assert analysis.languages["python"] > 0
