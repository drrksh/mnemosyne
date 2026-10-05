"""
Git history analysis: how *this developer* commits.

Only the developer's own commits are analysed (matched by email), so a
repository's history from other contributors does not get attributed to
them. The developer's share of recent commits is recorded on every
finding from the repository; cross-repository generalization uses it to
ignore repositories the developer barely touches.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from mnemosyne.domain.enums import EvidenceKind, MemoryType
from mnemosyne.learning.repo.findings import Finding, pct, proportion_confidence
from mnemosyne.learning.workspace import run_git

ANALYZER = "git-history"
_FIELD, _RECORD = "\x1f", "\x1e"
_CONVENTIONAL = re.compile(
    r"^(?P<type>feat|fix|docs|style|refactor|perf|test|tests|build|ci|chore|revert)(?P<scope>\([^)]+\))?(?P<bang>!)?:\s"
)
_GITMOJI = re.compile(r"^(?::[a-z0-9_+-]+:|[\U0001F300-\U0001FAFF☀-➿])")
_TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
_ISSUE_REF = re.compile(r"(?:^|\s)#\d+\b")
_PAST_TENSE = frozenset(
    "added fixed updated removed changed implemented refactored created improved moved renamed merged made "
    "deleted cleaned bumped upgraded converted replaced reverted wrote enabled disabled".split()
)
_SHORTSTAT = re.compile(r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?")
_BRANCH_PREFIX = re.compile(r"^([a-z]+)[/-]")


@dataclass
class Commit:
    sha: str
    email: str
    subject: str
    body: str
    lines_changed: int = 0


@dataclass
class GitHistory:
    author_email: Optional[str]
    total_commits: int = 0
    own_commits: list[Commit] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)

    @property
    def author_share(self) -> Optional[float]:
        if not self.author_email or not self.total_commits:
            return None
        return round(len(self.own_commits) / self.total_commits, 3)


def configured_email(root: Path) -> Optional[str]:
    output = run_git(root, "config", "user.email")
    return output.strip().lower() if output and output.strip() else None


def read_history(root: Path, *, author_email: Optional[str], max_commits: int = 500) -> Optional[GitHistory]:
    output = run_git(
        root, "log", f"-n{max_commits}", "--no-merges", "--shortstat",
        f"--format={_RECORD}%H{_FIELD}%ae{_FIELD}%s{_FIELD}%b{_FIELD}",
    )
    if output is None:
        return None
    email = (author_email or configured_email(root) or "").lower() or None
    history = GitHistory(author_email=email)
    for record in output.split(_RECORD)[1:]:
        parts = record.split(_FIELD)
        if len(parts) < 5:
            continue
        sha, commit_email, tail = parts[0], parts[1].lower(), parts[4]
        subject, body = parts[2].strip(), parts[3].strip()
        stat = _SHORTSTAT.search(tail)
        changed = (int(stat.group(2) or 0) + int(stat.group(3) or 0)) if stat else 0
        history.total_commits += 1
        if email is None or commit_email == email:
            history.own_commits.append(Commit(sha, commit_email, subject, body, changed))
    branches = run_git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    if branches:
        history.branches = [b.strip() for b in branches.splitlines() if b.strip()]
    return history


def _first_word(subject: str) -> str:
    match = _CONVENTIONAL.match(subject)
    rest = subject[match.end():] if match else subject
    words = rest.strip().split()
    return words[0].lower().strip(":,.") if words else ""


def findings_from(history: GitHistory) -> list[Finding]:
    out: list[Finding] = []
    commits = history.own_commits
    n = len(commits)
    if n < 8:
        return out
    subjects = [c.subject for c in commits]
    who = "your" if history.author_email else "the repository's"

    def add(**kw) -> None:
        out.append(Finding(analyzer=ANALYZER, evidence_kind=EvidenceKind.COMMIT, tags=["git", *kw.pop("tags", [])],
                           samples=n, **kw))

    conventional = [s for s in subjects if _CONVENTIONAL.match(s)]
    gitmoji = [s for s in subjects if _GITMOJI.match(s)]
    details = []
    first_words = [_first_word(s) for s in subjects]
    imperative = sum(1 for w in first_words if w and w not in _PAST_TENSE and not w.endswith("ing"))
    if imperative / n >= 0.8:
        details.append("imperative mood (\"add\", not \"added\")")
    stripped = [(_CONVENTIONAL.sub("", s) or s).strip() for s in subjects]
    lowercase = sum(1 for s in stripped if s[:1].islower())
    if lowercase / n >= 0.8:
        details.append("lowercase subject after the prefix")
    elif lowercase / n <= 0.2:
        details.append("capitalized subject")
    if sum(1 for s in subjects if s.endswith(".")) / n <= 0.1:
        details.append("no trailing period")
    lengths = sorted(len(s) for s in subjects)
    p90 = lengths[int(0.9 * (n - 1))]
    details.append(f"subjects kept under ~{max(50, ((p90 + 9) // 10) * 10)} characters (90th percentile {p90})")
    bodies = sum(1 for c in commits if c.body)
    if bodies / n >= 0.5:
        details.append(f"a body explaining the change in {pct(bodies, n)} of commits")
    detail_text = "; ".join(details)

    conf = proportion_confidence(len(conventional), n, min_total=8, min_bound=0.6)
    if conf:
        parsed = [m for s in conventional if (m := _CONVENTIONAL.match(s))]
        types = Counter(m.group("type") for m in parsed)
        scoped = sum(1 for m in parsed if m.group("scope"))
        scope_note = ", usually with a scope like feat(api):" if scoped / len(conventional) >= 0.5 else ""
        add(key="commit.style", value="conventional", title="Commit messages follow Conventional Commits",
            content=f"{who.capitalize()} commits use Conventional Commits ({pct(len(conventional), n)} of {n}) — "
                    f"types {', '.join(t for t, _ in types.most_common(6))}{scope_note}; {detail_text}.",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.6,
            excerpt="recent subjects: " + " | ".join(subjects[:4]))
    elif (conf := proportion_confidence(len(gitmoji), n, min_total=8, min_bound=0.6)):
        add(key="commit.style", value="gitmoji", title="Commit messages start with a gitmoji",
            content=f"{who.capitalize()} commit subjects start with an emoji ({pct(len(gitmoji), n)} of {n}); "
                    f"{detail_text}.",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.55,
            excerpt="recent subjects: " + " | ".join(subjects[:4]))
    elif len(conventional) / n <= 0.15 and n >= 15:
        add(key="commit.style", value="plain", title="Commit messages are plain descriptive sentences",
            content=f"{who.capitalize()} commits do not use a prefix convention ({pct(n - len(conventional), n)} "
                    f"of {n} without Conventional Commit types); {detail_text}.",
            memory_type=MemoryType.CONVENTION, confidence=0.8, importance=0.5,
            excerpt="recent subjects: " + " | ".join(subjects[:4]))

    tickets = sum(1 for s in subjects if _TICKET.search(s))
    issues = sum(1 for s in subjects if _ISSUE_REF.search(s))
    if (conf := proportion_confidence(tickets, n, min_total=8, min_bound=0.5)):
        example = next(_TICKET.search(s).group(0) for s in subjects if _TICKET.search(s))  # type: ignore[union-attr]
        add(key="commit.references", value="ticket-id", title="Commit subjects reference a ticket ID",
            content=f"Commit subjects include a tracker ticket such as {example} ({pct(tickets, n)} of commits).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.5, excerpt=f"e.g. {example}")
    elif (conf := proportion_confidence(issues, n, min_total=8, min_bound=0.5)):
        add(key="commit.references", value="issue-number", title="Commit subjects reference issue numbers",
            content=f"Commit subjects reference issues as #123 ({pct(issues, n)} of commits).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.45, excerpt="")

    sizes = [c.lines_changed for c in commits if c.lines_changed]
    if len(sizes) >= 10:
        median = statistics.median(sizes)
        if median <= 60:
            add(key="commit.size", value="small", title="Prefers small, focused commits",
                content=f"Commits are small: a median of {median:.0f} changed lines across {len(sizes)} commits.",
                memory_type=MemoryType.PREFERENCE, confidence=0.75, importance=0.4,
                excerpt=f"median lines changed: {median:.0f}")

    prefixes = Counter(m.group(1) for b in history.branches if (m := _BRANCH_PREFIX.match(b)))
    named = [b for b in history.branches if b not in ("main", "master", "develop", "trunk")]
    if len(named) >= 4 and prefixes:
        top, count = prefixes.most_common(1)[0]
        if count / len(named) >= 0.6:
            kinds = ", ".join(f"{p}/" for p, _ in prefixes.most_common(4))
            value = "type/description" if top in ("feat", "feature", "fix", "bugfix", "chore", "hotfix") else (
                f"{top}/description")
            add(key="git.branch_naming", value=value, title=f"Branch names follow {value}",
                content=f"Branches are named with a prefix ({kinds}) followed by a short description "
                        f"({count} of {len(named)} branches).",
                memory_type=MemoryType.CONVENTION, confidence=0.8, importance=0.4,
                excerpt="branches: " + ", ".join(named[:5]))
    return out
