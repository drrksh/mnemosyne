"""Go repositories: idioms from the code, tooling from go.mod."""
from __future__ import annotations

import pytest

from mnemosyne.learning.repo.analyzer import AnalysisOptions, RepositoryAnalyzer
from tests.learning.conftest import git

SERVICE = '''package {pkg}

import (
\t"context"
\t"fmt"
)

type Store interface {{
\tGet(ctx context.Context, id string) (string, error)
}}

func Load{name}(ctx context.Context, store Store, id string) (string, error) {{
\tvalue, err := store.Get(ctx, id)
\tif err != nil {{
\t\treturn "", fmt.Errorf("load {pkg} %s: %w", id, err)
\t}}
\treturn value, nil
}}

func Save{name}(ctx context.Context, store Store, id string) error {{
\tif _, err := store.Get(ctx, id); err != nil {{
\t\treturn fmt.Errorf("save {pkg}: %w", err)
\t}}
\treturn nil
}}

func Delete{name}(ctx context.Context, id string) error {{
\tif id == "" {{
\t\treturn fmt.Errorf("delete {pkg}: %w", errEmpty)
\t}}
\treturn nil
}}
'''

TABLE_TEST = '''package {pkg}

import (
\t"testing"

\t"github.com/stretchr/testify/require"
)

func TestLoad{name}(t *testing.T) {{
\ttests := []struct {{
\t\tname string
\t\tid   string
\t}}{{
\t\t{{"empty", ""}},
\t\t{{"present", "a"}},
\t}}
\tfor _, tt := range tests {{
\t\tt.Run(tt.name, func(t *testing.T) {{
\t\t\trequire.NotNil(t, tt)
\t\t}})
\t}}
}}

func TestSave{name}(t *testing.T) {{
\tcases := []struct {{
\t\tid string
\t}}{{{{"a"}}}}
\tfor _, c := range cases {{
\t\trequire.NotEmpty(t, c.id)
\t}}
}}
'''


@pytest.fixture
def go_repo(tmp_path):
    root = tmp_path / "ledger"
    root.mkdir()
    for pkg, name in [("invoice", "Invoice"), ("refund", "Refund"), ("payout", "Payout"), ("ledger", "Ledger")]:
        (root / pkg).mkdir()
        (root / pkg / f"{pkg}.go").write_text(SERVICE.format(pkg=pkg, name=name))
        (root / pkg / f"{pkg}_test.go").write_text(TABLE_TEST.format(pkg=pkg, name=name))
    (root / "go.mod").write_text(
        "module github.com/acme/ledger\n\ngo 1.22\n\nrequire (\n\tgithub.com/go-chi/chi/v5 v5.0.12\n"
        "\tgithub.com/jackc/pgx/v5 v5.5.0\n\tgithub.com/stretchr/testify v1.9.0\n)\n"
    )
    (root / ".golangci.yml").write_text("linters:\n  enable: [gofumpt]\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "remote", "add", "origin", "https://github.com/acme/ledger.git")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "feat: ledger service")
    return root


def test_go_idioms_and_tooling(go_repo):
    analysis = RepositoryAnalyzer(AnalysisOptions(author_email="dev@example.com")).analyze(go_repo)
    found = {f.key: f for f in analysis.findings if f.key}

    assert found["go.error_wrapping"].value == "fmt-w"
    assert found["go.test_style"].value == "table-driven"
    assert found["go.test_assertions"].value == "testify"
    assert found["go.context"].value == "first-parameter"
    assert analysis.languages.get("go", 0) > 0

    assert found["repo.go_version"].value == "1.22"
    assert "go test ./..." in found["repo.commands"].value
    overview = found["repo.overview"].content
    assert "web framework: chi" in overview and "golangci-lint" in overview and "PostgreSQL" in overview


def test_unwrapped_errors_are_not_learned_as_a_convention(go_repo):
    for path in go_repo.rglob("*.go"):
        if not path.name.endswith("_test.go"):
            path.write_text(path.read_text().replace(": %w", ": %v"))
    analysis = RepositoryAnalyzer(AnalysisOptions()).analyze(go_repo)
    assert "go.error_wrapping" not in {f.key for f in analysis.findings}
