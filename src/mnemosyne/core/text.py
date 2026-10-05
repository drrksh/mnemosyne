"""
Text normalization and code-aware tokenization.

Shared by deduplication (fingerprints), lexical search (BM25), and the
hashing embedder, so all three agree on what a "word" is. Developer
text is unusual: identifiers like `getUserById`, `snake_case_name`,
`ruff-format`, `node.js`, or `c++` carry meaning in their parts, so the
tokenizer emits both the whole identifier and its components.

Stemming is deliberately light (a handful of suffix rules plus a small
lemma map of developer vocabulary). A full Porter stemmer would buy a
little recall at the cost of surprising collisions ("string" -> "str");
since the same function runs on both queries and documents, consistency
matters more than linguistic precision.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from functools import lru_cache

_RAW_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+#.\-/]*")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z0-9])|[A-Z]?[a-z0-9]+|[A-Z]+")
_SEPARATORS_RE = re.compile(r"[_\-./]+")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s+#]")

STOPWORDS: frozenset[str] = frozenset(
    """
    a about above after again against all am an and any are as at be because been before
    being below between both but by can could did do does doing down during each few for
    from further had has have having he her here hers herself him himself his how i if in
    into is it its itself just let me more most my myself no nor not of off on once only or
    other our ours ourselves out over own same she should so some such than that the their
    theirs them themselves then there these they this those through to too under until up
    very was we were what when where which while who whom why will with would you your
    yours yourself yourselves also etc via per e.g i.e ok okay please thing things stuff
    really lot lots get got make made want like need may might must shall us don doesn didn
    isn aren wasn weren won shouldn couldn wouldn hasn haven ll ve re
    """.split()
)

#: Single-character tokens are almost always contraction debris ("don't"
#: -> "don", "t"). These are the exceptions that name real languages.
_MEANINGFUL_SINGLE_CHARS = frozenset("cr")

#: Words that carry *polarity or force* ("always", "never") but little
#: topical meaning. They matter to the learning pipeline (which reads raw
#: text) but only add noise to retrieval, so retrieval drops them.
DIRECTIVE_WORDS: frozenset[str] = frozenset(
    """
    always never use using used prefer prefers preferred instead dont don't avoid remember
    note keep should shouldn't must mustn't
    """.split()
)

#: Canonical forms for common developer vocabulary. Applied after
#: lowercasing and before suffix stripping.
LEMMAS: dict[str, str] = {
    "caching": "cache",
    "cached": "cache",
    "caches": "cache",
    "naming": "name",
    "named": "name",
    "names": "name",
    "typing": "type",
    "typed": "type",
    "types": "type",
    "configuration": "config",
    "configurations": "config",
    "configured": "config",
    "configure": "config",
    "configs": "config",
    "cfg": "config",
    "dependencies": "dependency",
    "deps": "dependency",
    "dep": "dependency",
    "repos": "repo",
    "repository": "repo",
    "repositories": "repo",
    "databases": "database",
    "db": "database",
    "dbs": "database",
    "docs": "doc",
    "documentation": "doc",
    "documented": "doc",
    "documenting": "doc",
    "docstrings": "docstring",
    "env": "environment",
    "envs": "environment",
    "authentication": "auth",
    "authenticate": "auth",
    "authenticated": "auth",
    "authorization": "authz",
    "tests": "test",
    "testing": "test",
    "tested": "test",
    "specs": "spec",
    "exceptions": "exception",
    "errors": "error",
    "functions": "function",
    "func": "function",
    "funcs": "function",
    "fn": "function",
    "def": "function",
    "args": "argument",
    "arg": "argument",
    "arguments": "argument",
    "params": "parameter",
    "param": "parameter",
    "parameters": "parameter",
    "migrations": "migration",
    "commits": "commit",
    "committed": "commit",
    "committing": "commit",
    "logging": "log",
    "logs": "log",
    "logged": "log",
    "logger": "log",
    "loggers": "log",
    "formatting": "format",
    "formatted": "format",
    "formatter": "format",
    "formatters": "format",
    "linting": "lint",
    "linter": "lint",
    "linters": "lint",
    "deploying": "deploy",
    "deployment": "deploy",
    "deployments": "deploy",
    "deployed": "deploy",
    "indentation": "indent",
    "indented": "indent",
    "indenting": "indent",
    "async": "async",
    "asynchronous": "async",
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "ts": "typescript",
    "golang": "go",
    "k8s": "kubernetes",
    "postgresql": "postgres",
    "pg": "postgres",
}

_DOUBLE_CONSONANT_EXCEPTIONS = frozenset("lsz")


@lru_cache(maxsize=65536)
def stem(word: str) -> str:
    """Light, deterministic suffix stripping for English developer prose."""
    w = LEMMAS.get(word, word)
    if w != word or len(w) <= 3 or not w.isalpha():
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("sses"):
        return w[:-2]
    if w.endswith("ing") and len(w) - 3 >= 4:
        return _undouble(w[:-3])
    if w.endswith("ed") and len(w) - 2 >= 4:
        return _undouble(w[:-2])
    if w.endswith("s") and not w.endswith(("ss", "us", "is", "ous")) and len(w) > 3:
        return w[:-1]
    return w


def _undouble(stem_: str) -> str:
    last = stem_[-1:] if stem_ else ""
    if len(stem_) >= 3 and last == stem_[-2] and last not in _DOUBLE_CONSONANT_EXCEPTIONS and last not in "aeiou":
        return stem_[:-1]
    return stem_


@lru_cache(maxsize=65536)
def split_identifier(token: str) -> tuple[str, ...]:
    """`getUserById` -> (get, user, by, id); `snake_case` -> (snake, case)."""
    parts: list[str] = []
    for chunk in _SEPARATORS_RE.split(token):
        if not chunk:
            continue
        parts.extend(m.group(0) for m in _CAMEL_RE.finditer(chunk))
    return tuple(p.lower() for p in parts if p)


def _keep(word: str, stemmed: str, drop_directives: bool) -> bool:
    if len(word) == 1 and word not in _MEANINGFUL_SINGLE_CHARS:
        return False
    if word in STOPWORDS or stemmed in STOPWORDS:
        return False
    return not (drop_directives and (word in DIRECTIVE_WORDS or stemmed in DIRECTIVE_WORDS))


@lru_cache(maxsize=131072)
def _analyze_raw_token(raw: str, drop_directives: bool) -> tuple[str, ...]:
    whole = raw.lower().strip(".-/")
    if not whole:
        return ()
    out: list[str] = []
    stemmed_whole = stem(whole)
    if _keep(whole, stemmed_whole, drop_directives):
        out.append(stemmed_whole)
    # Known vocabulary ("PostgreSQL") is a word, not an identifier to split.
    if whole in LEMMAS:
        return tuple(out)
    parts = split_identifier(raw)
    if len(parts) > 1:
        for part in parts:
            stemmed = stem(part)
            if len(part) < 2 or not _keep(part, stemmed, drop_directives):
                continue
            if stemmed not in out:
                out.append(stemmed)
    return tuple(out)


def tokenize(text: str, *, drop_directives: bool = True) -> list[str]:
    """Retrieval tokenizer: lowercase, identifier-splitting, stopword
    removal, light stemming. Order is preserved (bigrams depend on it)."""
    if not text:
        return []
    tokens: list[str] = []
    for match in _RAW_TOKEN_RE.finditer(text):
        tokens.extend(_analyze_raw_token(match.group(0), drop_directives))
    return tokens


def normalize_whitespace(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def normalize_for_fingerprint(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = _PUNCT_RE.sub(" ", text)
    return normalize_whitespace(text)


def fingerprint(*parts: str) -> str:
    """Stable content hash used for exact-duplicate detection. Case,
    punctuation and whitespace differences do not change it."""
    normalized = "\n".join(normalize_for_fingerprint(p) for p in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def normalize_value(value: str | None) -> str:
    """Normalize a slot value so `"Pytest"`, `pytest.` and ` pytest ` match."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip().lower()
    text = text.strip("\"'`.,;: ")
    return normalize_whitespace(text)


def truncate(text: str, max_chars: int, *, ellipsis: str = "…") -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    cut = text[: max(0, max_chars - len(ellipsis))]
    space = cut.rfind(" ")
    if space >= max_chars * 0.5:
        cut = cut[:space]
    return cut.rstrip() + ellipsis


def short_hash(text: str, length: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]
