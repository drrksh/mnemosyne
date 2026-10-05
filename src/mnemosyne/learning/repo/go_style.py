"""Go idioms: error wrapping, table-driven tests, assertion libraries and
context propagation."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from mnemosyne.domain.enums import MemoryType
from mnemosyne.learning.repo.findings import Finding, majority, pct, proportion_confidence

ANALYZER = "go-style"

_ERRORF_WRAP = re.compile(r'fmt\.Errorf\("[^"]*%w')
_ERRORF_PLAIN = re.compile(r'fmt\.Errorf\("(?:(?!%w)[^"])*"')
_PKG_ERRORS = re.compile(r"\berrors\.(?:Wrap|Wrapf|WithMessage)\(")
_TABLE_TEST = re.compile(r"(?:tests|cases|testCases|tt|tcs)\s*:?=\s*\[\]struct\s*{")
_TEST_FUNC = re.compile(r"^func Test\w+\(t \*testing\.T\)", re.M)
_TESTIFY = re.compile(r'"github\.com/stretchr/testify/(?:assert|require)"')
_FUNC_SIG = re.compile(r"^func (?:\([^)]*\)\s*)?\w+\(([^)]*)\)", re.M)


@dataclass
class GoStats:
    files: int = 0
    test_files: int = 0
    error_style: Counter[str] = field(default_factory=Counter)
    test_functions: int = 0
    table_tests: int = 0
    testify_files: int = 0
    ctx_first: int = 0
    ctx_elsewhere: int = 0


def analyze_file(path: str, source: str, stats: GoStats) -> None:
    stats.files += 1
    is_test = path.endswith("_test.go")
    if is_test:
        stats.test_files += 1
        stats.test_functions += len(_TEST_FUNC.findall(source))
        stats.table_tests += len(_TABLE_TEST.findall(source))
        if _TESTIFY.search(source):
            stats.testify_files += 1
        return
    stats.error_style["fmt.Errorf with %w"] += len(_ERRORF_WRAP.findall(source))
    stats.error_style["fmt.Errorf without %w"] += len(_ERRORF_PLAIN.findall(source))
    stats.error_style["pkg/errors Wrap"] += len(_PKG_ERRORS.findall(source))
    for match in _FUNC_SIG.finditer(source):
        params = match.group(1)
        if "context.Context" in params:
            if params.strip().split(",")[0].strip().endswith("context.Context"):
                stats.ctx_first += 1
            else:
                stats.ctx_elsewhere += 1


def findings_from(stats: GoStats) -> list[Finding]:
    out: list[Finding] = []
    if stats.files == 0:
        return out

    def add(**kw) -> None:
        out.append(Finding(analyzer=ANALYZER, tags=["go", *kw.pop("tags", [])], **kw))

    label, count, total = majority(stats.error_style)
    conf = proportion_confidence(count, total, min_total=10)
    if label and conf and label != "fmt.Errorf without %w":
        add(key="go.error_wrapping", value="fmt-w" if label.startswith("fmt") else "pkg-errors",
            title=f"Go: wrap errors with {label}",
            content=f"Errors are wrapped with {label} ({pct(count, total)} of {total} error constructions), "
                    "preserving the cause for errors.Is / errors.As.",
            memory_type=MemoryType.PATTERN, confidence=conf, importance=0.55, samples=total,
            excerpt=f"error styles: {dict(stats.error_style)}", tags=["errors"])

    if stats.test_functions >= 6 and stats.table_tests / stats.test_functions >= 0.4:
        add(key="go.test_style", value="table-driven", title="Go: table-driven tests",
            content=f"Tests are table-driven ({stats.table_tests} tables across {stats.test_functions} test "
                    "functions), usually with t.Run subtests.",
            memory_type=MemoryType.PATTERN, confidence=0.85, importance=0.5, samples=stats.test_functions,
            excerpt=f"table tests {stats.table_tests}/{stats.test_functions}", tags=["testing"])

    if stats.test_files >= 4:
        uses_testify = stats.testify_files / stats.test_files >= 0.5
        add(key="go.test_assertions", value="testify" if uses_testify else "stdlib",
            title="Go tests assert with testify" if uses_testify else "Go tests use the standard library only",
            content=(f"{stats.testify_files} of {stats.test_files} test files import testify assert/require."
                     if uses_testify else f"Only {stats.testify_files} of {stats.test_files} test files use testify; "
                                          "assertions are plain `if ... { t.Errorf(...) }`."),
            memory_type=MemoryType.TOOLING, confidence=0.8, importance=0.45, samples=stats.test_files,
            excerpt=f"testify files {stats.testify_files}/{stats.test_files}", tags=["testing"])

    total = stats.ctx_first + stats.ctx_elsewhere
    conf = proportion_confidence(stats.ctx_first, total, min_total=8, min_bound=0.75)
    if conf:
        add(key="go.context", value="first-parameter", title="Go: context.Context is the first parameter",
            content=f"Functions taking a context accept it as the first parameter ({pct(stats.ctx_first, total)} of "
                    f"{total}).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.45, samples=total,
            excerpt=f"ctx first {stats.ctx_first}, elsewhere {stats.ctx_elsewhere}", tags=["concurrency"])
    return out
