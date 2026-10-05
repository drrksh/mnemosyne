"""Fixtures that build real git repositories with known conventions."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable

import pytest

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Dev",
    "GIT_AUTHOR_EMAIL": "dev@example.com",
    "GIT_COMMITTER_NAME": "Dev",
    "GIT_COMMITTER_EMAIL": "dev@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(root: Path, *args: str, email: str = "dev@example.com") -> str:
    env = {**GIT_ENV, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email}
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env).stdout


PY_MODULE = '''from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class {name}Error(Exception):
    """Raised when {lower} processing fails."""


@dataclass(frozen=True)
class {name}Record:
    """A {lower} record."""

    identifier: str
    amount: int


def load_{lower}(path: Path, limit: int | None = None) -> list[{name}Record]:
    """Load {lower} records from disk.

    Args:
        path: Where the records live.
        limit: Optional maximum number of records.

    Returns:
        The parsed records.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("could not read %s", path)
        raise {name}Error(f"cannot read {{path}}") from exc
    records = [{name}Record(identifier=line, amount=len(line)) for line in text.splitlines()]
    logger.info("loaded {lower} records")
    return records[:limit] if limit else records


def summarize_{lower}(records: list[{name}Record]) -> dict[str, int]:
    """Summarize records by identifier.

    Args:
        records: Records to summarize.

    Returns:
        Totals keyed by identifier.
    """
    totals: dict[str, int] = {{}}
    for record in records:
        totals[record.identifier] = totals.get(record.identifier, 0) + record.amount
    return totals
'''

PY_TEST = '''import pytest

from app.{lower} import load_{lower}, summarize_{lower}


@pytest.fixture
def sample_path(tmp_path):
    path = tmp_path / "data.txt"
    path.write_text("a\\nbb\\n")
    return path


def test_load_{lower}(sample_path):
    assert len(load_{lower}(sample_path)) == 2


@pytest.mark.parametrize("limit,expected", [(1, 1), (2, 2)])
def test_load_{lower}_limit(sample_path, limit, expected):
    assert len(load_{lower}(sample_path, limit)) == expected


def test_summarize_{lower}(sample_path):
    assert summarize_{lower}(load_{lower}(sample_path)) == {{"a": 1, "bb": 2}}
'''

TS_COMPONENT = '''import {{ useState }} from 'react'
import type {{ Invoice }} from './types'
import {{ formatMoney }} from './money'

type Props = {{
  invoices: Invoice[]
  title: string
}}

export const {name}Table = ({{ invoices, title }}: Props) => {{
  const [selected, setSelected] = useState<string | null>(null)
  const total = invoices.reduce((sum, invoice) => sum + invoice.amount, 0)
  return (
    <section>
      <h2>{{title}}</h2>
      <p>{{total}}</p>
      <button onClick={{() => setSelected(null)}}>clear</button>
    </section>
  )
}}

export const load{name} = async (url: string): Promise<Invoice[]> => {{
  const response = await fetch(url)
  const body = await response.json()
  return body.items
}}
'''


@pytest.fixture
def python_repo(tmp_path) -> Path:
    """A uv/ruff/mypy/pytest project with conventional commits."""
    root = tmp_path / "payments"
    (root / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "app" / "__init__.py").write_text("")
    names = ["Invoice", "Refund", "Payout", "Ledger", "Customer", "Charge", "Dispute", "Balance"]
    for name in names:
        (root / "app" / f"{name.lower()}.py").write_text(PY_MODULE.format(name=name, lower=name.lower()))
        (root / "tests" / f"test_{name.lower()}.py").write_text(PY_TEST.format(lower=name.lower()))
    (root / "tests" / "conftest.py").write_text("import pytest\n\n\n@pytest.fixture\ndef anything():\n    return 1\n")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "payments"\nrequires-python = ">=3.12"\ndependencies = ["fastapi", "sqlalchemy", '
        '"psycopg[binary]", "pydantic"]\n\n[tool.ruff]\nline-length = 100\n\n[tool.mypy]\nstrict = true\n\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )
    (root / "uv.lock").write_text("version = 1\n")
    (root / "Makefile").write_text("test:\n\tuv run pytest\nlint:\n\tuv run ruff check .\n")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("on: push\njobs: {}\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "remote", "add", "origin", "git@github.com:Acme/Payments.git")
    git(root, "config", "user.email", "dev@example.com")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "chore: initial project skeleton")
    subjects = ["feat(api): add invoice export", "fix: handle empty refunds", "refactor: split ledger module",
                "test: cover payout limits", "feat: add dispute webhooks", "fix(ledger): round balances",
                "docs: describe charge flow", "feat(api): paginate customers", "fix: retry stripe timeouts",
                "chore: bump dependencies"]
    for i, subject in enumerate(subjects):
        (root / "app" / f"extra_{i}.py").write_text(f'"""Extra module {i}."""\n\nVALUE: int = {i}\n')
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", subject)
    # A teammate's commits that must not be attributed to the developer.
    for i in range(3):
        (root / f"teammate_{i}.txt").write_text("x")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", f"Updated stuff {i}", email="teammate@example.com")
    return root


@pytest.fixture
def ts_repo(tmp_path) -> Path:
    root = tmp_path / "dashboard"
    (root / "src").mkdir(parents=True)
    for name in ["Invoice", "Refund", "Payout", "Ledger", "Customer", "Charge", "Dispute", "Balance"]:
        (root / "src" / f"{name}Table.tsx").write_text(TS_COMPONENT.format(name=name))
    (root / "src" / "types.ts").write_text("export type Invoice = {\n  id: string\n  amount: number\n}\n")
    (root / "package.json").write_text(json.dumps({
        "name": "dashboard",
        "packageManager": "pnpm@9.0.0",
        "scripts": {"test": "vitest run", "lint": "eslint .", "dev": "next dev", "build": "next build"},
        "dependencies": {"next": "14.0.0", "react": "18.0.0", "@tanstack/react-query": "5.0.0"},
        "devDependencies": {"typescript": "5.4.0", "vitest": "1.0.0", "eslint": "9.0.0", "prettier": "3.0.0",
                            "tailwindcss": "3.0.0", "@playwright/test": "1.40.0"},
    }))
    (root / "tsconfig.json").write_text('{\n  // comments are allowed\n  "compilerOptions": {"strict": true,},\n}\n')
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "remote", "add", "origin", "https://github.com/acme/dashboard.git")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "feat: initial dashboard")
    return root


@pytest.fixture
def make_python_repo(tmp_path) -> Callable[[str, str], Path]:
    """Small Python repos with a chosen test style, for generalization."""

    def factory(name: str, test_style: str = "pytest") -> Path:
        root = tmp_path / name
        (root / "pkg").mkdir(parents=True)
        (root / "tests").mkdir()
        for i in range(10):
            if test_style == "pytest":
                body = f"def test_case_{i}():\n    assert {i} == {i}\n"
            else:
                body = f"import unittest\n\n\nclass Case{i}(unittest.TestCase):\n    def test_it(self):\n" \
                       f"        self.assertEqual({i}, {i})\n"
            (root / "tests" / f"test_{i}.py").write_text(body)
        (root / "pkg" / "__init__.py").write_text("")
        git(root, "init", "-q", "-b", "main")
        git(root, "remote", "add", "origin", f"git@github.com:acme/{name}.git")
        git(root, "config", "user.email", "dev@example.com")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "feat: start")
        return root

    return factory
