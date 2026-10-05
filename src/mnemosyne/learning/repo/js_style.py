"""
JavaScript / TypeScript style analysis.

Line-level heuristics over sampled files with comments and string
contents blanked out first, so a semicolon inside a string or a `var`
in a comment does not count. Less exact than parsing, but every finding
still requires a large, lopsided sample before it is reported.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from mnemosyne.domain.enums import MemoryType
from mnemosyne.learning.repo.findings import Finding, majority, pct, proportion_confidence

ANALYZER = "js-style"

_BLOCK_COMMENT = re.compile(r"/\*[\s\S]*?\*/")
_LINE_COMMENT = re.compile(r"(?<![:\\])//.*$", re.M)
_STRING = re.compile(r"(\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`)")
_IMPORT_QUOTE = re.compile(r"""^\s*(?:import\s[^'"]*?from\s+|import\s+|export\s[^'"]*?from\s+)(['"])""", re.M)
_REQUIRE = re.compile(r"\brequire\(\s*['\"]")
_ESM = re.compile(r"^\s*(?:import\s|export\s)", re.M)
_DECL = re.compile(r"\b(const|let|var)\s+[\w{\[]")
_ARROW_EXPORT = re.compile(
    r"^\s*(?:export\s+)?const\s+\w+\s*(?::[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|\w+)\s*(?::[^=]+)?=>", re.M
)
_FUNCTION_DECL = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*\w+\s*[<(]", re.M)
_AWAIT = re.compile(r"\bawait\b")
_THEN = re.compile(r"\.then\(")
_INTERFACE = re.compile(r"^\s*(?:export\s+)?interface\s+\w+", re.M)
_TYPE_ALIAS = re.compile(r"^\s*(?:export\s+)?type\s+\w+(?:<[^>]*>)?\s*=", re.M)
_ANY = re.compile(r"(?::\s*any\b|\bas\s+any\b|<any>|any\[\])")
_DEFAULT_EXPORT = re.compile(r"^\s*export\s+default\b", re.M)
_NAMED_EXPORT = re.compile(r"^\s*export\s+(?:const|let|function|class|type|interface|enum|async\s+function|\{)", re.M)
_CLASS_COMPONENT = re.compile(r"class\s+\w+\s+extends\s+(?:React\.)?(?:Pure)?Component\b")
_HOOK_CALL = re.compile(r"\buse[A-Z]\w*\(")
_JSX_RETURN = re.compile(r"return\s*\(?\s*<[A-Za-z>]")
_STATEMENT_END = re.compile(r"[\w)\]'\"`]\s*;?\s*$")


@dataclass
class JsStats:
    files: int = 0
    ts_files: int = 0
    semicolons: Counter[str] = field(default_factory=Counter)
    quotes: Counter[str] = field(default_factory=Counter)
    indent: Counter[str] = field(default_factory=Counter)
    trailing_commas: Counter[str] = field(default_factory=Counter)
    declarations: Counter[str] = field(default_factory=Counter)
    function_style: Counter[str] = field(default_factory=Counter)
    async_style: Counter[str] = field(default_factory=Counter)
    type_style: Counter[str] = field(default_factory=Counter)
    any_uses: int = 0
    ts_lines: int = 0
    exports: Counter[str] = field(default_factory=Counter)
    modules: Counter[str] = field(default_factory=Counter)
    components: Counter[str] = field(default_factory=Counter)


def _blank_strings(code: str) -> str:
    return _STRING.sub(lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2) + m.group(0)[-1], code)


def analyze_file(path: str, source: str, stats: JsStats) -> None:
    is_ts = PurePosixPath(path).suffix in (".ts", ".tsx", ".mts", ".cts")
    stats.files += 1
    stats.ts_files += 1 if is_ts else 0

    for match in _IMPORT_QUOTE.finditer(source):
        stats.quotes["single" if match.group(1) == "'" else "double"] += 1

    code = _BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), source)
    code = _LINE_COMMENT.sub("", code)
    code = _blank_strings(code)
    lines = code.splitlines()

    previous_indent = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if line.startswith("\t"):
            stats.indent["tabs"] += 1
        else:
            indent = len(line) - len(line.lstrip(" "))
            if indent > previous_indent and indent - previous_indent in (2, 4):
                stats.indent[f"{indent - previous_indent} spaces"] += 1
            previous_indent = indent
        if _STATEMENT_END.search(stripped) and not stripped.startswith(("import", "export {", "}", "//", "*", "@")):
            if stripped.endswith((",", "{", "(", "[", "=>", ":", "?", "&&", "||", "+", "-")):
                continue
            if stripped.startswith(("if ", "for ", "while ", "else", "case ", "default:", "<", "/>")):
                continue
            stats.semicolons["always" if stripped.endswith(";") else "never"] += 1
        if stripped in ("}", "]", ")", "});", "})", "];", "),") and index > 0:
            prev = lines[index - 1].strip()
            if prev and not prev.endswith(("{", "[", "(")):
                stats.trailing_commas["trailing" if prev.endswith(",") else "none"] += 1

    for match in _DECL.finditer(code):
        stats.declarations[match.group(1)] += 1
    stats.function_style["arrow functions"] += len(_ARROW_EXPORT.findall(code))
    stats.function_style["function declarations"] += len(_FUNCTION_DECL.findall(code))
    stats.async_style["async/await"] += len(_AWAIT.findall(code))
    stats.async_style[".then() chains"] += len(_THEN.findall(code))
    if is_ts:
        stats.type_style["interfaces"] += len(_INTERFACE.findall(code))
        stats.type_style["type aliases"] += len(_TYPE_ALIAS.findall(code))
        stats.any_uses += len(_ANY.findall(code))
        stats.ts_lines += len(lines)
    if _DEFAULT_EXPORT.search(code):
        stats.exports["default"] += 1
    elif _NAMED_EXPORT.search(code):
        stats.exports["named"] += 1
    if _REQUIRE.search(source):
        stats.modules["CommonJS (require)"] += 1
    elif _ESM.search(code):
        stats.modules["ES modules (import/export)"] += 1
    if PurePosixPath(path).suffix in (".tsx", ".jsx"):
        if _CLASS_COMPONENT.search(code):
            stats.components["class components"] += 1
        elif _JSX_RETURN.search(code) or _HOOK_CALL.search(code):
            stats.components["function components"] += 1


def findings_from(stats: JsStats) -> list[Finding]:
    out: list[Finding] = []
    if stats.files == 0:
        return out
    lang = "TypeScript" if stats.ts_files >= stats.files / 2 else "JavaScript"

    def add(**kw) -> None:
        out.append(Finding(analyzer=ANALYZER, tags=["javascript", *kw.pop("tags", [])], **kw))

    label, count, total = majority(stats.semicolons)
    conf = proportion_confidence(count, total, min_total=60, min_bound=0.8)
    if label and conf:
        text = "end statements with semicolons" if label == "always" else "omit semicolons"
        add(key="javascript.semicolons", value=label, title=f"{lang}: {text}",
            content=f"Statements {text} ({pct(count, total)} of {total} statements).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.45, samples=total,
            excerpt=f"semicolons: {dict(stats.semicolons)}", tags=["formatting"])

    label, count, total = majority(stats.quotes)
    conf = proportion_confidence(count, total, min_total=20)
    if label and conf:
        add(key="javascript.quotes", value=label, title=f"{lang}: {label} quotes",
            content=f"Strings and import paths use {label} quotes ({pct(count, total)} of {total} imports).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.4, samples=total,
            excerpt=f"import quotes: {dict(stats.quotes)}", tags=["formatting"])

    label, count, total = majority(stats.indent)
    conf = proportion_confidence(count, total, min_total=60)
    if label and conf:
        add(key="javascript.indent", value=label, title=f"{lang}: indent with {label}",
            content=f"Code is indented with {label} ({pct(count, total)} of {total} indentation steps).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.3, samples=total,
            excerpt=f"indentation: {dict(stats.indent)}", tags=["formatting"])

    label, count, total = majority(stats.trailing_commas)
    conf = proportion_confidence(count, total, min_total=30, min_bound=0.75)
    if label == "trailing" and conf:
        add(key="javascript.trailing_commas", value="multiline",
            title=f"{lang}: trailing commas in multi-line literals",
            content=f"Multi-line arrays, objects and argument lists end with a trailing comma ({pct(count, total)}).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.25, samples=total,
            excerpt=f"trailing commas: {dict(stats.trailing_commas)}", tags=["formatting"])

    total_decl = sum(stats.declarations.values())
    if total_decl >= 30 and stats.declarations["var"] == 0:
        add(key="javascript.declarations", value="const-let", title=f"{lang}: const/let only, never var",
            content=f"Variables are declared with const ({pct(stats.declarations['const'], total_decl)}) or let; "
                    "var is never used.",
            memory_type=MemoryType.CONVENTION, confidence=0.9, importance=0.4, samples=total_decl,
            excerpt=f"declarations: {dict(stats.declarations)}")

    label, count, total = majority(stats.function_style)
    conf = proportion_confidence(count, total, min_total=15, min_bound=0.7)
    if label and conf:
        add(key="javascript.function_style", value=label, title=f"{lang}: top-level functions as {label}",
            content=f"Top-level functions are written as {label} ({pct(count, total)} of {total}).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.4, samples=total,
            excerpt=f"function styles: {dict(stats.function_style)}")

    label, count, total = majority(stats.async_style)
    conf = proportion_confidence(count, total, min_total=15, min_bound=0.75)
    if label == "async/await" and conf:
        add(key="javascript.async", value="async-await", title=f"{lang}: async/await over .then() chains",
            content=f"Asynchronous code uses async/await ({pct(count, total)} of {total} async sites).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.45, samples=total,
            excerpt=f"async styles: {dict(stats.async_style)}", tags=["concurrency"])

    label, count, total = majority(stats.type_style)
    conf = proportion_confidence(count, total, min_total=12, min_bound=0.65)
    if label and conf:
        add(key="typescript.type_declarations", value=label, title=f"TypeScript: prefer {label}",
            content=f"Object types are declared as {label} ({pct(count, total)} of {total} declarations).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.4, samples=total,
            excerpt=f"type declarations: {dict(stats.type_style)}", tags=["typing", "typescript"])

    if stats.ts_lines >= 1500:
        per_kloc = 1000 * stats.any_uses / stats.ts_lines
        if per_kloc < 0.5:
            add(key="typescript.any", value="avoid", title="TypeScript: avoid `any`",
                content=f"`any` is essentially absent ({stats.any_uses} uses in {stats.ts_lines} lines); use precise "
                        "types or `unknown` and narrow.",
                memory_type=MemoryType.CONVENTION, confidence=0.85, importance=0.55, samples=stats.ts_lines,
                excerpt=f"any uses: {stats.any_uses} in {stats.ts_lines} lines", tags=["typing", "typescript"])

    label, count, total = majority(stats.exports)
    conf = proportion_confidence(count, total, min_total=12, min_bound=0.75)
    if label and conf:
        add(key="javascript.exports", value=label, title=f"{lang}: {label} exports",
            content=f"Modules use {label} exports ({pct(count, total)} of {total} modules).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.45, samples=total,
            excerpt=f"exports: {dict(stats.exports)}")

    label, count, total = majority(stats.modules)
    conf = proportion_confidence(count, total, min_total=12, min_bound=0.8)
    if label and conf:
        add(key="javascript.modules", value="esm" if label.startswith("ES") else "commonjs",
            title=f"{lang}: {label}", content=f"Modules are written as {label} ({pct(count, total)} of {total}).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.4, samples=total,
            excerpt=f"module systems: {dict(stats.modules)}")

    label, count, total = majority(stats.components)
    conf = proportion_confidence(count, total, min_total=6, min_bound=0.7)
    if label and conf:
        add(key="react.components", value="function" if label.startswith("function") else "class",
            title=f"React: {label}" + (" with hooks" if label.startswith("function") else ""),
            content=f"React components are {label} ({pct(count, total)} of {total} component files).",
            memory_type=MemoryType.PATTERN, confidence=conf, importance=0.55, samples=total,
            excerpt=f"components: {dict(stats.components)}", tags=["react", "frontend"])
    return out
