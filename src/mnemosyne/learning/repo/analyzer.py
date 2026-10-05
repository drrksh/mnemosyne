"""
Repository analysis orchestrator.

Runs every analyzer over one repository, reconciles overlapping claims
(two analyzers reporting the same slot), adds a repository overview, and
converts findings into `CandidateMemory` objects scoped to the
repository. Evidence references embed the commit SHA, so re-analysing an
unchanged repository is a no-op and analysing a new commit refreshes
each belief with a fresh measurement.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from mnemosyne.core.vocabulary import category_label
from mnemosyne.domain.enums import EvidenceKind, MemoryOrigin, MemoryType
from mnemosyne.domain.learning import CandidateMemory, EvidenceDraft
from mnemosyne.domain.scope import Scope
from mnemosyne.learning.repo import git_history, go_style, js_style, python_style
from mnemosyne.learning.repo.files import FileInventory, build_inventory, count_lines, read_text
from mnemosyne.learning.repo.findings import Finding
from mnemosyne.learning.repo.manifests import ManifestAnalyzer, StackSummary
from mnemosyne.learning.workspace import RepoIdentity, detect_repo

logger = logging.getLogger("mnemosyne.learning.repo")

#: When two analyzers claim the same slot, prefer evidence from how code is
#: actually written over what a manifest declares (a project can depend on
#: pytest while writing unittest-style tests).
_ANALYZER_PRIORITY = {
    python_style.ANALYZER: 3,
    js_style.ANALYZER: 3,
    go_style.ANALYZER: 3,
    git_history.ANALYZER: 3,
    "manifests": 2,
}


@dataclass(frozen=True)
class AnalysisOptions:
    max_files: int = 4000
    max_file_bytes: int = 400_000
    max_files_per_language: int = 400
    max_commits: int = 500
    author_email: Optional[str] = None


@dataclass
class RepoAnalysis:
    identity: RepoIdentity
    languages: dict[str, int] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    stack: StackSummary = field(default_factory=StackSummary)
    files_scanned: int = 0
    author_email: Optional[str] = None
    author_share: Optional[float] = None
    duration_ms: float = 0.0

    @property
    def scope(self) -> str:
        return str(Scope.repository(self.identity.repo_id))

    @property
    def primary_languages(self) -> list[str]:
        total = sum(self.languages.values()) or 1
        return [lang for lang, lines in sorted(self.languages.items(), key=lambda kv: -kv[1]) if lines / total >= 0.05]

    def to_candidates(self) -> list[CandidateMemory]:
        revision = (self.identity.head_sha or "worktree")[:12]
        candidates = []
        for finding in self.findings:
            metadata = {
                "analyzer": finding.analyzer,
                "samples": finding.samples,
                "languages": self.primary_languages,
                **finding.metadata,
            }
            if self.author_share is not None:
                metadata["author_share"] = self.author_share
            candidates.append(
                CandidateMemory(
                    # No "in this repository" prefix: the scope already says where
                    # it applies, and generalized copies reuse the title verbatim.
                    title=finding.title,
                    content=finding.content,
                    memory_type=finding.memory_type,
                    origin=MemoryOrigin.INFERRED,
                    confidence=finding.confidence,
                    importance=finding.importance,
                    scope=self.scope,
                    key=finding.key,
                    value=finding.value,
                    tags=sorted(set(finding.tags)),
                    source=f"analyzer:{finding.analyzer}",
                    metadata=metadata,
                    evidence=[
                        EvidenceDraft(
                            kind=finding.evidence_kind,
                            source_ref=f"repo:{self.identity.repo_id}@{revision}:{finding.analyzer}:{finding.key}",
                            excerpt=finding.excerpt or finding.content,
                        )
                    ],
                )
            )
        return candidates


class RepositoryAnalyzer:
    def __init__(self, options: Optional[AnalysisOptions] = None) -> None:
        self.options = options or AnalysisOptions()

    def analyze(self, path: Path | str) -> RepoAnalysis:
        started = time.perf_counter()
        path = Path(path).expanduser().resolve()
        identity = detect_repo(path) or RepoIdentity(
            root=path, repo_id=f"local/{path.name or 'workspace'}", name=path.name or "workspace"
        )
        inventory = build_inventory(
            identity.root, max_files=self.options.max_files, max_file_bytes=self.options.max_file_bytes
        )
        analysis = RepoAnalysis(identity=identity, files_scanned=len(inventory.all_files))
        analysis.languages = {
            language: count_lines(identity.root, inventory.sample(language, self.options.max_files_per_language))
            for language in inventory.by_language
        }

        manifest = ManifestAnalyzer(inventory)
        analysis.stack = manifest.run()
        findings = list(manifest.findings)
        findings.extend(self._python(inventory))
        findings.extend(self._javascript(inventory))
        findings.extend(self._go(inventory))

        history = git_history.read_history(
            identity.root, author_email=self.options.author_email, max_commits=self.options.max_commits
        )
        if history is not None:
            analysis.author_email = history.author_email
            analysis.author_share = history.author_share
            findings.extend(git_history.findings_from(history))

        analysis.findings = self._reconcile(findings)
        overview = self._overview(analysis)
        if overview is not None:
            analysis.findings.insert(0, overview)
        analysis.duration_ms = round((time.perf_counter() - started) * 1000, 1)
        logger.info(
            "repository_analyzed",
            extra={"repo": identity.repo_id, "findings": len(analysis.findings), "ms": analysis.duration_ms},
        )
        return analysis

    # ------------------------------------------------------------------
    def _python(self, inventory: FileInventory) -> list[Finding]:
        paths = inventory.sample("python", self.options.max_files_per_language)
        if not paths:
            return []
        stats = python_style.PythonStats()
        for relative in paths:
            python_style.analyze_file(relative, read_text(inventory.root, relative), stats)
        return python_style.findings_from(stats)

    def _javascript(self, inventory: FileInventory) -> list[Finding]:
        paths = inventory.sample("typescript", self.options.max_files_per_language) + inventory.sample(
            "javascript", self.options.max_files_per_language
        )
        if not paths:
            return []
        stats = js_style.JsStats()
        for relative in paths:
            js_style.analyze_file(relative, read_text(inventory.root, relative), stats)
        return js_style.findings_from(stats)

    def _go(self, inventory: FileInventory) -> list[Finding]:
        paths = inventory.sample("go", self.options.max_files_per_language)
        if not paths:
            return []
        stats = go_style.GoStats()
        for relative in paths:
            go_style.analyze_file(relative, read_text(inventory.root, relative), stats)
        return go_style.findings_from(stats)

    @staticmethod
    def _reconcile(findings: list[Finding]) -> list[Finding]:
        """One finding per slot. Agreeing claims merge their evidence;
        disagreeing claims keep the higher-priority analyzer's view."""
        by_key: dict[str, Finding] = {}
        unkeyed: list[Finding] = []
        for finding in findings:
            if not finding.key:
                unkeyed.append(finding)
                continue
            current = by_key.get(finding.key)
            if current is None:
                by_key[finding.key] = finding
                continue
            same = (current.value or "").lower() == (finding.value or "").lower()
            rank = lambda f: (_ANALYZER_PRIORITY.get(f.analyzer, 1), f.confidence)  # noqa: E731
            winner, other = (finding, current) if rank(finding) > rank(current) else (current, finding)
            if same:
                winner.confidence = min(0.97, max(winner.confidence, other.confidence) + 0.02)
                winner.excerpt = f"{winner.excerpt}; also {other.excerpt}".strip("; ")
                winner.content = f"{winner.content} Confirmed by {other.analyzer.replace('-', ' ')}."
            by_key[finding.key] = winner
        return [*by_key.values(), *unkeyed]

    @staticmethod
    def _overview(analysis: RepoAnalysis) -> Optional[Finding]:
        languages = analysis.languages
        total = sum(languages.values())
        if not total and not analysis.stack.tools:
            return None
        lang_text = ", ".join(
            f"{lang.capitalize()} ({100 * lines / total:.0f}%)"
            for lang, lines in sorted(languages.items(), key=lambda kv: -kv[1])
            if total and lines / total >= 0.03
        )
        groups: Counter[str] = Counter()
        tools_by_category: dict[str, list[str]] = {}
        for tool in analysis.stack.tools.values():
            tools_by_category.setdefault(tool.category, []).append(tool.label)
            groups[tool.category] += 1
        ordered = ["meta_framework", "frontend_framework", "web_framework", "orm", "validation", "task_queue",
                   "migrations", "styling", "state_management", "test_framework", "e2e_framework", "linter",
                   "formatter", "type_checker", "package_manager", "bundler", "ci", "infrastructure", "cloud"]
        parts = [f"{category_label(c)}: {', '.join(sorted(tools_by_category[c]))}" for c in ordered
                 if c in tools_by_category]
        if analysis.stack.data_stores:
            parts.append(f"data stores: {', '.join(analysis.stack.data_stores)}")
        content = f"{analysis.identity.name} is written in {lang_text or 'unknown languages'}."
        if parts:
            content += " Stack — " + "; ".join(parts) + "."
        signature = "|".join(
            sorted(analysis.primary_languages) + sorted(t.name for t in analysis.stack.tools.values())
            + sorted(analysis.stack.data_stores)
        )
        return Finding(
            analyzer="overview",
            title=f"{analysis.identity.name}: stack overview",
            content=content,
            memory_type=MemoryType.ARCHITECTURE,
            confidence=0.9,
            importance=0.7,
            key="repo.overview",
            value=signature[:480],
            evidence_kind=EvidenceKind.CONFIG,
            excerpt=content[:400],
            tags=["overview", *analysis.primary_languages],
            metadata={"languages": analysis.primary_languages},
        )
