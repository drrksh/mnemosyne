"""
Python style analysis with `ast` and `tokenize` — exact parsing, not
regular expressions, so a string that happens to contain `print(` or a
comment mentioning `os.path` never skews the counts.

Measures indentation, quote preference, naming, annotation coverage and
syntax, docstring coverage and style, string formatting, data-container
idioms, async usage, logging vs print, exception patterns, import style,
pathlib vs os.path, and test style.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from mnemosyne.domain.enums import MemoryType
from mnemosyne.learning.repo.findings import Finding, majority, pct, proportion_confidence

ANALYZER = "python-style"
_SNAKE = re.compile(r"^_{0,2}[a-z][a-z0-9_]*$")
_PASCAL = re.compile(r"^_?[A-Z][A-Za-z0-9]*$")
_STRING_PREFIX = re.compile(r"^[rRbBuUfF]{0,2}")


@dataclass
class PythonStats:
    files: int = 0
    test_files: int = 0
    indent: Counter[str] = field(default_factory=Counter)
    quotes: Counter[str] = field(default_factory=Counter)
    line_lengths: list[int] = field(default_factory=list)
    function_names: Counter[str] = field(default_factory=Counter)
    class_names: Counter[str] = field(default_factory=Counter)
    params_total: int = 0
    params_annotated: int = 0
    returns_total: int = 0
    returns_annotated: int = 0
    union_style: Counter[str] = field(default_factory=Counter)
    generic_style: Counter[str] = field(default_factory=Counter)
    future_annotations_files: int = 0
    files_with_annotations: int = 0
    public_defs: int = 0
    public_documented: int = 0
    docstring_style: Counter[str] = field(default_factory=Counter)
    formatting: Counter[str] = field(default_factory=Counter)
    data_containers: Counter[str] = field(default_factory=Counter)
    functions: int = 0
    async_functions: int = 0
    print_calls: int = 0
    logging_calls: int = 0
    custom_exceptions: int = 0
    raises_in_handlers: int = 0
    raises_from: int = 0
    handlers: int = 0
    bare_excepts: int = 0
    imports: Counter[str] = field(default_factory=Counter)
    path_style: Counter[str] = field(default_factory=Counter)
    test_style: Counter[str] = field(default_factory=Counter)
    pytest_fixtures: int = 0
    parametrize: int = 0
    test_locations: Counter[str] = field(default_factory=Counter)
    examples: dict[str, list[str]] = field(default_factory=dict)

    def example(self, key: str, path: str) -> None:
        bucket = self.examples.setdefault(key, [])
        if len(bucket) < 3 and path not in bucket:
            bucket.append(path)


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        p.name.startswith("test_")
        or p.name.endswith("_test.py")
        or p.name == "conftest.py"
        or any(part in ("tests", "test") for part in p.parts[:-1])
    )


def _annotation_styles(node: ast.AST, stats: PythonStats) -> None:
    for sub in ast.walk(node):
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
            stats.union_style["pep604"] += 1
        elif isinstance(sub, ast.Subscript):
            base = sub.value
            name = base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else ""
            if name in ("Optional", "Union"):
                stats.union_style["typing"] += 1
            elif name in ("List", "Dict", "Set", "Tuple", "FrozenSet", "Type"):
                stats.generic_style["typing"] += 1
            elif name in ("list", "dict", "set", "tuple", "frozenset", "type"):
                stats.generic_style["builtin"] += 1


def _docstring_style(doc: str) -> str | None:
    if re.search(r"^\s*(Args|Arguments|Returns|Raises|Yields):\s*$", doc, re.M):
        return "google"
    if re.search(r"^\s*(Parameters|Returns|Raises)\s*\n\s*-{3,}", doc, re.M):
        return "numpy"
    if re.search(r":param\s|:returns?:|:raises?\s", doc):
        return "rest"
    if re.search(r"@param\s|@return", doc):
        return "epytext"
    return None


def _scan_tokens(source: str, stats: PythonStats) -> None:
    indent_stack = [0]
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.INDENT:
                text = token.string
                if "\t" in text:
                    stats.indent["tabs"] += 1
                else:
                    delta = len(text) - indent_stack[-1]
                    if delta > 0:
                        stats.indent[f"{delta} spaces"] += 1
                indent_stack.append(len(text.expandtabs(8)))
            elif token.type == tokenize.DEDENT:
                if len(indent_stack) > 1:
                    indent_stack.pop()
            elif token.type == tokenize.STRING:
                body = _STRING_PREFIX.sub("", token.string)
                if body[:3] in ('"""', "'''"):
                    continue  # docstrings / multi-line strings follow their own convention
                quote = body[:1]
                inner = body[1:-1]
                if quote == '"' and "'" not in inner:
                    stats.quotes["double"] += 1
                elif quote == "'" and '"' not in inner:
                    stats.quotes["single"] += 1
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return


def analyze_file(path: str, source: str, stats: PythonStats) -> None:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return
    test_file = is_test_path(path)
    stats.files += 1
    if test_file:
        stats.test_files += 1
        top = PurePosixPath(path).parts[0] if len(PurePosixPath(path).parts) > 1 else ""
        stats.test_locations["tests directory" if top in ("tests", "test") else "next to code"] += 1
    _scan_tokens(source, stats)
    stats.line_lengths.extend(len(line) for line in source.splitlines() if line.strip())

    has_annotations = False
    has_future_annotations = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stats.functions += 1
            if isinstance(node, ast.AsyncFunctionDef):
                stats.async_functions += 1
            if not (node.name.startswith("__") and node.name.endswith("__")):
                stats.function_names["snake_case" if _SNAKE.match(node.name) else "other"] += 1
            if not test_file:
                args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                args = [a for a in args if a.arg not in ("self", "cls")]
                stats.params_total += len(args)
                stats.params_annotated += sum(1 for a in args if a.annotation is not None)
                if node.name != "__init__":
                    stats.returns_total += 1
                    stats.returns_annotated += 1 if node.returns is not None else 0
                for annotation in [a.annotation for a in args if a.annotation is not None] + (
                    [node.returns] if node.returns is not None else []
                ):
                    has_annotations = True
                    _annotation_styles(annotation, stats)
                if not node.name.startswith("_"):
                    stats.public_defs += 1
                    doc = ast.get_docstring(node)
                    if doc:
                        stats.public_documented += 1
                        style = _docstring_style(doc)
                        if style:
                            stats.docstring_style[style] += 1
            for decorator in node.decorator_list:
                text = ast.unparse(decorator)
                if "pytest.fixture" in text or text == "fixture":
                    stats.pytest_fixtures += 1
                if "parametrize" in text:
                    stats.parametrize += 1
        elif isinstance(node, ast.ClassDef):
            stats.class_names["PascalCase" if _PASCAL.match(node.name) else "other"] += 1
            bases = [ast.unparse(b) for b in node.bases]
            decorators = [ast.unparse(d) for d in node.decorator_list]
            if any(b.endswith(("Exception", "Error")) for b in bases):
                stats.custom_exceptions += 1
            if any("dataclass" in d for d in decorators):
                stats.data_containers["dataclasses"] += 1
            elif any(b.endswith("BaseModel") for b in bases):
                stats.data_containers["pydantic"] += 1
            elif any(d.startswith(("attr.s", "attrs.define", "define", "attr.define", "frozen")) for d in decorators):
                stats.data_containers["attrs"] += 1
            elif any(b.endswith("NamedTuple") for b in bases):
                stats.data_containers["namedtuple"] += 1
            elif any(b.endswith("TypedDict") for b in bases):
                stats.data_containers["typeddict"] += 1
            if test_file and any(b.endswith("TestCase") for b in bases):
                stats.test_style["unittest classes"] += 1
            if not test_file and not node.name.startswith("_"):
                stats.public_defs += 1
                if ast.get_docstring(node):
                    stats.public_documented += 1
        elif isinstance(node, ast.JoinedStr):
            stats.formatting["f-strings"] += 1
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "format" and isinstance(func.value, ast.Constant) and (
                isinstance(func.value.value, str)
            ):
                stats.formatting["str.format"] += 1
            if not test_file:
                if isinstance(func, ast.Name) and func.id == "print":
                    stats.print_calls += 1
                elif isinstance(func, ast.Attribute) and func.attr in (
                    "debug", "info", "warning", "error", "exception", "critical"
                ) and isinstance(func.value, ast.Name) and func.value.id in ("logger", "log", "logging", "LOGGER"):
                    stats.logging_calls += 1
            text = ast.unparse(func) if isinstance(func, ast.Attribute) else ""
            if text.startswith("os.path."):
                stats.path_style["os.path"] += 1
            elif isinstance(func, ast.Name) and func.id == "Path":
                stats.path_style["pathlib"] += 1
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Constant) and (
            isinstance(node.left.value, str)
        ):
            stats.formatting["% formatting"] += 1
        elif isinstance(node, ast.ExceptHandler):
            stats.handlers += 1
            if node.type is None:
                stats.bare_excepts += 1
            for sub in ast.walk(node):
                if isinstance(sub, ast.Raise) and sub.exc is not None:
                    stats.raises_in_handlers += 1
                    if sub.cause is not None:
                        stats.raises_from += 1
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            stats.imports["relative" if node.level else "absolute"] += 1
        elif isinstance(node, ast.Import):
            stats.imports["absolute"] += len(node.names)

    if test_file:
        module_tests = sum(
            1 for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")
        )
        stats.test_style["pytest functions"] += module_tests
    if has_annotations:
        stats.files_with_annotations += 1
        stats.future_annotations_files += 1 if has_future_annotations else 0


def findings_from(stats: PythonStats) -> list[Finding]:
    out: list[Finding] = []
    if stats.files == 0:
        return out

    def add(**kw) -> None:
        out.append(Finding(analyzer=ANALYZER, tags=["python", *kw.pop("tags", [])], **kw))

    label, count, total = majority(stats.indent)
    conf = proportion_confidence(count, total, min_total=30)
    if label and conf:
        add(key="python.indent", value=label, title=f"Python: indent with {label}",
            content=f"Python code is indented with {label} ({pct(count, total)} of {total} indented blocks).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.35, samples=total,
            excerpt=f"indentation deltas: {dict(stats.indent)}")

    label, count, total = majority(stats.quotes)
    conf = proportion_confidence(count, total, min_total=40)
    if label and conf:
        add(key="python.quotes", value=label, title=f"Python: {label} quotes for strings",
            content=f"Python string literals use {label} quotes ({pct(count, total)} of {total} single-line "
                    "strings where either quote would work).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.4, samples=total,
            excerpt=f"quote usage: {dict(stats.quotes)}")

    snake, total_fn = stats.function_names["snake_case"], sum(stats.function_names.values())
    pascal, total_cls = stats.class_names["PascalCase"], sum(stats.class_names.values())
    conf_fn = proportion_confidence(snake, total_fn, min_total=15, min_bound=0.8)
    conf_cls = proportion_confidence(pascal, total_cls, min_total=5, min_bound=0.75) if total_cls else 0.9
    if conf_fn and conf_cls:
        add(key="python.naming", value="pep8", title="Python naming follows PEP 8",
            content=f"Functions and variables are snake_case ({pct(snake, total_fn)} of {total_fn} functions) and "
                    f"classes PascalCase ({pct(pascal, total_cls)} of {total_cls} classes).",
            memory_type=MemoryType.CONVENTION, confidence=min(conf_fn, conf_cls), importance=0.4,
            samples=total_fn, excerpt=f"functions: {dict(stats.function_names)}, classes: {dict(stats.class_names)}",
            tags=["naming"])

    if stats.params_total + stats.returns_total >= 20:
        annotated = stats.params_annotated + stats.returns_annotated
        total = stats.params_total + stats.returns_total
        ratio = annotated / total
        if ratio >= 0.85:
            conf = proportion_confidence(annotated, total, min_total=20, min_bound=0.75)
            if conf:
                add(key="python.type_hints", value="full", title="Python: fully type-annotated code",
                    content=f"Function signatures are type-annotated ({pct(annotated, total)} of {total} parameters "
                            "and return types); new code should be annotated too.",
                    memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.65, samples=total,
                    excerpt=f"parameters {stats.params_annotated}/{stats.params_total}, "
                            f"returns {stats.returns_annotated}/{stats.returns_total}", tags=["typing"])
        elif ratio <= 0.15:
            conf = proportion_confidence(total - annotated, total, min_total=30, min_bound=0.75)
            if conf:
                add(key="python.type_hints", value="none", title="Python: code is not type-annotated",
                    content=f"Only {pct(annotated, total)} of {total} parameters/returns are annotated; this "
                            "codebase does not use type hints.",
                    memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.45, samples=total,
                    excerpt=f"annotated {annotated}/{total}", tags=["typing"])

    label, count, total = majority(stats.union_style)
    conf = proportion_confidence(count, total, min_total=8)
    if label and conf:
        text = "X | None (PEP 604)" if label == "pep604" else "typing.Optional / typing.Union"
        add(key="python.union_syntax", value=label, title=f"Python: optional types written as {text}",
            content=f"Optional and union annotations use {text} ({pct(count, total)} of {total}).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.45, samples=total,
            excerpt=f"union styles: {dict(stats.union_style)}", tags=["typing"])

    label, count, total = majority(stats.generic_style)
    conf = proportion_confidence(count, total, min_total=8)
    if label and conf:
        text = "built-in generics (list[str], dict[str, int])" if label == "builtin" else (
            "typing generics (List[str], Dict[str, int])")
        add(key="python.generic_syntax", value=label, title=f"Python: {text}",
            content=f"Generic annotations use {text} ({pct(count, total)} of {total}).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.4, samples=total,
            excerpt=f"generic styles: {dict(stats.generic_style)}", tags=["typing"])

    if stats.files_with_annotations >= 5:
        conf = proportion_confidence(stats.future_annotations_files, stats.files_with_annotations, min_total=5,
                                     min_bound=0.6)
        if conf:
            add(key="python.future_annotations", value="yes",
                title="Python: modules start with `from __future__ import annotations`",
                content=f"{pct(stats.future_annotations_files, stats.files_with_annotations)} of annotated modules "
                        "import annotations from __future__.",
                memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.3,
                samples=stats.files_with_annotations, excerpt="", tags=["typing"])

    label, count, total = majority(stats.docstring_style)
    conf = proportion_confidence(count, total, min_total=6)
    if label and conf:
        names = {"google": "Google style (Args:/Returns:/Raises:)", "numpy": "NumPy style (Parameters / ----------)",
                 "rest": "reStructuredText (:param x:)", "epytext": "Epytext (@param)"}
        add(key="python.docstring_style", value=label, title=f"Python docstrings: {names[label]}",
            content=f"Docstrings with structured sections use {names[label]} ({pct(count, total)} of {total}).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.45, samples=total,
            excerpt=f"docstring styles: {dict(stats.docstring_style)}", tags=["documentation"])

    if stats.public_defs >= 15:
        ratio = stats.public_documented / stats.public_defs
        if ratio >= 0.7:
            conf = proportion_confidence(stats.public_documented, stats.public_defs, min_total=15, min_bound=0.6)
            if conf:
                add(key="python.docstrings", value="public-apis",
                    title="Python: public functions and classes have docstrings",
                    content=f"{pct(stats.public_documented, stats.public_defs)} of {stats.public_defs} public "
                            "functions and classes are documented.",
                    memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.45,
                    samples=stats.public_defs, excerpt="", tags=["documentation"])

    label, count, total = majority(stats.formatting)
    conf = proportion_confidence(count, total, min_total=10)
    if label and conf:
        add(key="python.string_formatting", value=label, title=f"Python: {label} for string formatting",
            content=f"Strings are built with {label} ({pct(count, total)} of {total} formatted strings).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.35, samples=total,
            excerpt=f"formatting styles: {dict(stats.formatting)}")

    label, count, total = majority(stats.data_containers)
    conf = proportion_confidence(count, total, min_total=4, min_bound=0.5)
    if label and conf:
        add(key="python.data_containers", value=label, title=f"Python: data containers are {label}",
            content=f"Data-holding classes are defined with {label} ({count} of {total} data classes).",
            memory_type=MemoryType.PATTERN, confidence=conf, importance=0.5, samples=total,
            excerpt=f"data containers: {dict(stats.data_containers)}")

    if stats.functions >= 20 and stats.async_functions / stats.functions >= 0.3:
        add(key="python.async", value="async-first", title="Python: async-first codebase",
            content=f"{pct(stats.async_functions, stats.functions)} of functions are async; I/O code is expected to "
                    "be asynchronous.",
            memory_type=MemoryType.PATTERN, confidence=0.85, importance=0.5, samples=stats.functions,
            excerpt=f"async {stats.async_functions}/{stats.functions}", tags=["concurrency"])

    output = stats.print_calls + stats.logging_calls
    if stats.logging_calls >= 5:
        conf = proportion_confidence(stats.logging_calls, output, min_total=5, min_bound=0.7)
        if conf:
            add(key="python.logging", value="logging", title="Python: use logging, not print",
                content=f"Application code reports through loggers ({stats.logging_calls} logger calls vs "
                        f"{stats.print_calls} print calls outside tests).",
                memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.5, samples=output,
                excerpt=f"logger calls {stats.logging_calls}, print calls {stats.print_calls}", tags=["logging"])

    if stats.custom_exceptions >= 3:
        detail = ""
        if stats.raises_in_handlers >= 5:
            detail = f" Re-raised exceptions keep their cause with `raise ... from` " \
                     f"({pct(stats.raises_from, stats.raises_in_handlers)} of re-raises)."
        add(key="python.exceptions", value="custom-hierarchy", title="Python: custom exception classes",
            content=f"The codebase defines its own exception types ({stats.custom_exceptions} classes) rather than "
                    f"raising built-in exceptions directly.{detail}",
            memory_type=MemoryType.PATTERN, confidence=0.8, importance=0.5, samples=stats.custom_exceptions,
            excerpt=f"custom exception classes: {stats.custom_exceptions}", tags=["errors"])

    if stats.handlers >= 15 and stats.bare_excepts == 0:
        add(key="python.bare_except", value="never", title="Python: never use a bare `except:`",
            content=f"None of {stats.handlers} exception handlers is a bare `except:`; always catch specific "
                    "exception types.",
            memory_type=MemoryType.ANTI_PATTERN, confidence=0.85, importance=0.45, samples=stats.handlers,
            excerpt=f"handlers {stats.handlers}, bare 0", tags=["errors"])

    label, count, total = majority(stats.imports)
    conf = proportion_confidence(count, total, min_total=30, min_bound=0.85)
    if label and conf:
        add(key="python.imports", value=label, title=f"Python: {label} imports",
            content=f"Modules use {label} imports ({pct(count, total)} of {total} imports).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.35, samples=total,
            excerpt=f"imports: {dict(stats.imports)}")

    label, count, total = majority(stats.path_style)
    conf = proportion_confidence(count, total, min_total=8)
    if label and conf:
        text = "pathlib.Path" if label == "pathlib" else "os.path functions"
        add(key="python.paths", value=label, title=f"Python: filesystem paths with {text}",
            content=f"Path handling uses {text} ({pct(count, total)} of {total} path operations).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.4, samples=total,
            excerpt=f"path styles: {dict(stats.path_style)}")

    label, count, total = majority(stats.test_style)
    conf = proportion_confidence(count, total, min_total=8)
    if label and conf:
        framework = "pytest" if label == "pytest functions" else "unittest"
        extras = []
        if framework == "pytest" and stats.pytest_fixtures:
            extras.append(f"{stats.pytest_fixtures} fixtures")
        if stats.parametrize:
            extras.append(f"{stats.parametrize} parametrized tests")
        extra = f" ({', '.join(extras)})" if extras else ""
        add(key="python.test_framework", value=framework, title=f"Python tests use {framework}",
            content=f"Tests are written as {label} ({pct(count, total)} of {total} tests){extra}.",
            memory_type=MemoryType.TOOLING, confidence=conf, importance=0.7, samples=total,
            excerpt=f"test styles: {dict(stats.test_style)}", tags=["testing"])

    label, count, total = majority(stats.test_locations)
    conf = proportion_confidence(count, total, min_total=5)
    if label and conf:
        add(key="python.test_layout", value=label, title=f"Python tests live in a {label}" if label.startswith(
            "tests") else "Python tests live next to the code they test",
            content=f"Test modules are placed in a {label} ({count} of {total} test files).",
            memory_type=MemoryType.CONVENTION, confidence=conf, importance=0.4, samples=total, excerpt="",
            tags=["testing"])
    return out


def suggested_line_length(lengths: list[int]) -> int | None:
    if len(lengths) < 200:
        return None
    ordered = sorted(lengths)
    p99 = ordered[int(0.99 * (len(ordered) - 1))]
    for limit in (79, 88, 100, 120):
        if p99 <= limit:
            return limit
    return None
