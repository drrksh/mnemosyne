"""Scope parsing, working-context affinity, tokenization, and vocabulary."""
from __future__ import annotations

import pytest

from mnemosyne.core.text import fingerprint, normalize_value, stem, tokenize, truncate
from mnemosyne.core.vocabulary import find_tools_in_text, match_tool_prefix, tool_for_package
from mnemosyne.domain.exceptions import ValidationError
from mnemosyne.domain.scope import Scope, ScopeKind, canonical_scope
from mnemosyne.domain.working_context import WorkingContext


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "global"),
        ("", "global"),
        ("global", "global"),
        ("lang:Python", "lang:python"),
        ("language:py", "lang:python"),
        ("lang:TS", "lang:typescript"),
        ("repo:github.com/acme/api", "repo:github.com/acme/api"),
        ("repository:gitlab.com/a/b/", "repo:gitlab.com/a/b"),
    ],
)
def test_scope_canonicalization(raw, expected):
    assert canonical_scope(raw) == expected


@pytest.mark.parametrize("raw", ["python", "team:x", "lang:", "repo:", "lang:has space", "repo:bad path"])
def test_invalid_scopes_are_rejected(raw):
    with pytest.raises(ValidationError):
        Scope.parse(raw)


def test_scope_specificity_orders_repo_over_language_over_global():
    assert Scope.parse("repo:x/y").specificity > Scope.parse("lang:go").specificity > Scope.global_().specificity
    assert Scope.parse("repo:x/y").kind == ScopeKind.REPOSITORY


def test_working_context_affinity():
    ctx = WorkingContext.create(repo_id="github.com/acme/api", languages=["py"])
    assert ctx.scope_affinity("repo:github.com/acme/api") == 1.0
    assert ctx.scope_affinity("lang:python") == 0.85
    assert ctx.scope_affinity("global") == 0.6
    assert ctx.scope_affinity("repo:github.com/other/repo") is None
    assert ctx.scope_affinity("lang:go") is None
    assert ctx.applicable_scopes() == ["global", "lang:python", "repo:github.com/acme/api"]


def test_empty_working_context_applies_everything_but_prefers_general_knowledge():
    ctx = WorkingContext()
    assert ctx.scope_affinity("global") > ctx.scope_affinity("lang:python") > ctx.scope_affinity("repo:a/b") > 0


def test_tokenizer_splits_identifiers_and_drops_noise():
    assert tokenize("getUserById") == ["getuserbyid", "user", "id"]
    assert {stem("snake"), stem("case"), stem("name")} <= set(tokenize("snake_case_name"))
    assert tokenize("Don't use mutable default args") == [stem("mutable"), stem("default"), "argument"]
    assert tokenize("PostgreSQL") == tokenize("postgres") == tokenize("pg")


def test_stemming_is_consistent_for_common_forms():
    assert stem("testing") == stem("tests") == "test"
    assert stem("dependencies") == "dependency"
    assert stem("string") == "string"
    assert stem("status") == "status"


def test_fingerprint_ignores_case_whitespace_and_punctuation():
    assert fingerprint("Use pytest!", "Body.") == fingerprint("use   PYTEST", "body")
    assert fingerprint("Use pytest", "a") != fingerprint("Use unittest", "a")


def test_normalize_value_and_truncate():
    assert normalize_value(' "Pytest". ') == "pytest"
    assert truncate("one two three four five", 12) == "one two…"
    assert truncate("short", 10) == "short"


def test_tool_detection_skips_ambiguous_words_in_free_text():
    found = [t.name for t in find_tools_in_text("Our stack: Next.js, Tailwind, Prisma, PostgreSQL. I wore black.")]
    assert found == ["postgres", "tailwind", "next.js", "prisma"] or set(found) == {
        "postgres", "tailwind", "next.js", "prisma"
    }
    assert "black" not in found


def test_tool_prefix_matching_resolves_slot_keys():
    assert match_tool_prefix("black for formatting").slot_key == "python.formatter"
    assert match_tool_prefix("ruff format please").name == "ruff-format"
    assert match_tool_prefix("pnpm.").slot_key == "javascript.package_manager"
    assert match_tool_prefix("my own thing") is None
    assert tool_for_package("@playwright/test").slot_key == "javascript.e2e_framework"
