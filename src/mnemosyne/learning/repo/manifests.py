"""
Manifest and configuration analysis: the stack, the tooling, and how to
run things.

Configuration files are the strongest evidence a repository offers — a
`uv.lock` is not an inference, it is a fact — so findings here carry high
confidence and `EvidenceKind.CONFIG`. Tool names are mapped through the
shared vocabulary (core/vocabulary.py), so a `pnpm-lock.yaml` here and
"we use pnpm" in a conversation land on the same slot.
"""
from __future__ import annotations

import json
import re
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from mnemosyne.core.vocabulary import TOOLS_BY_NAME, Tool, category_label, tool_for_package
from mnemosyne.domain.enums import EvidenceKind, MemoryType
from mnemosyne.learning.repo.files import FileInventory, read_text
from mnemosyne.learning.repo.findings import Finding

ANALYZER = "manifests"

_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_GEMFILE_GEM = re.compile(r"""^\s*gem\s+['"]([\w-]+)['"]""", re.M)
_GO_REQUIRE = re.compile(r"^\s*(?:require\s+)?([\w.\-]+/[\w.\-/]+)\s+v[\d.]+", re.M)
_GO_VERSION = re.compile(r"^go\s+(\d+\.\d+)", re.M)
_MAKE_TARGET = re.compile(r"^([A-Za-z][\w-]*):(?!=)", re.M)
_JSONC_COMMENT = re.compile(r"(?<![:\"])//.*?$|/\*[\s\S]*?\*/", re.M)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")

#: Go module path -> vocabulary tool name.
GO_MODULES = {
    "github.com/gin-gonic/gin": "gin",
    "github.com/labstack/echo": "echo",
    "github.com/gofiber/fiber": "fiber",
    "github.com/go-chi/chi": "chi",
    "gorm.io/gorm": "gorm",
    "entgo.io/ent": "ent",
    "github.com/spf13/cobra": "cobra",
    "github.com/stretchr/testify": "testify",
    "github.com/jackc/pgx": "postgres",
    "github.com/lib/pq": "postgres",
    "github.com/redis/go-redis": "redis",
    "github.com/go-redis/redis": "redis",
}

COMMAND_SCRIPTS = ("test", "test:unit", "test:e2e", "lint", "format", "fmt", "typecheck", "type-check", "check",
                   "build", "dev", "start", "serve", "migrate")


@dataclass
class StackSummary:
    languages: dict[str, int] = field(default_factory=dict)
    tools: dict[str, Tool] = field(default_factory=dict)
    tool_sources: dict[str, str] = field(default_factory=dict)
    data_stores: list[str] = field(default_factory=list)
    commands: dict[str, str] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)
    pre_commit_hooks: list[str] = field(default_factory=list)


def _jsonc(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        cleaned = _TRAILING_COMMA.sub(r"\1", _JSONC_COMMENT.sub("", text))
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


def _toml(root: Path, name: str) -> dict[str, Any]:
    text = read_text(root, name)
    if not text:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}


def _dep_name(spec: str) -> str:
    match = _REQUIREMENT_NAME.match(spec)
    return match.group(1).lower().replace("_", "-") if match else ""


class ManifestAnalyzer:
    def __init__(self, inventory: FileInventory) -> None:
        self.inv = inventory
        self.root = inventory.root
        self.stack = StackSummary()
        self.findings: list[Finding] = []
        self._files = set(inventory.all_files)

    # ------------------------------------------------------------------
    def _exists(self, *names: str) -> Optional[str]:
        for name in names:
            if name in self._files or (self.root / name).exists():
                return name
        return None

    def _glob(self, pattern: str) -> list[str]:
        return sorted(str(p.relative_to(self.root)) for p in self.root.glob(pattern))

    def _tool(self, name: str, source: str) -> None:
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            return
        key = tool.slot_key or f"tool:{tool.name}"
        # First evidence wins: callers check the most authoritative sources first.
        if key not in self.stack.tools:
            self.stack.tools[key] = tool
            self.stack.tool_sources[key] = source

    def _from_packages(self, packages: list[str], source: str) -> None:
        for package in packages:
            tool = tool_for_package(package)
            if tool is None:
                continue
            if tool.category in ("database", "cache"):
                if tool.label not in self.stack.data_stores:
                    self.stack.data_stores.append(tool.label)
            else:
                self._tool(tool.name, source)

    # ------------------------------------------------------------------
    def run(self) -> StackSummary:
        self._python()
        self._javascript()
        self._go()
        self._rust()
        self._ruby()
        self._ci_and_infra()
        self._make_and_just()
        self._editorconfig()
        self._emit()
        return self.stack

    def _python(self) -> None:
        pyproject = _toml(self.root, "pyproject.toml")
        tool = pyproject.get("tool", {})
        project = pyproject.get("project", {})
        deps: list[str] = [_dep_name(d) for d in project.get("dependencies", [])]
        for group in project.get("optional-dependencies", {}).values():
            deps.extend(_dep_name(d) for d in group)
        for group in pyproject.get("dependency-groups", {}).values():
            deps.extend(_dep_name(d) for d in group if isinstance(d, str))
        poetry = tool.get("poetry", {})
        deps.extend(name.lower() for name in poetry.get("dependencies", {}))
        for group in poetry.get("group", {}).values():
            deps.extend(name.lower() for name in group.get("dependencies", {}))
        for req in self._glob("requirements*.txt") + self._glob("requirements/*.txt"):
            deps.extend(_dep_name(line) for line in read_text(self.root, req).splitlines() if line.strip()
                        and not line.lstrip().startswith(("#", "-")))

        # Package manager: lockfiles are decisive.
        if self._exists("uv.lock"):
            self._tool("uv", "uv.lock")
        elif self._exists("poetry.lock") or poetry:
            self._tool("poetry", "poetry.lock" if self._exists("poetry.lock") else "pyproject.toml [tool.poetry]")
        elif self._exists("pdm.lock"):
            self._tool("pdm", "pdm.lock")
        elif self._exists("Pipfile.lock", "Pipfile"):
            self._tool("pipenv", "Pipfile")
        elif self._exists("environment.yml", "environment.yaml"):
            self._tool("conda", "environment.yml")
        elif self._glob("requirements*.txt"):
            self._tool("pip", "requirements.txt")

        if "ruff" in tool or self._exists("ruff.toml", ".ruff.toml"):
            self._tool("ruff", "ruff configuration")
            if "format" in tool.get("ruff", {}):
                self._tool("ruff-format", "pyproject.toml [tool.ruff.format]")
        if "black" in tool:
            self._tool("black", "pyproject.toml [tool.black]")
        if "mypy" in tool or self._exists("mypy.ini", ".mypy.ini"):
            self._tool("mypy", "mypy configuration")
            if tool.get("mypy", {}).get("strict"):
                self.findings.append(Finding(
                    ANALYZER, "Python: mypy runs in strict mode",
                    "Type checking uses mypy with strict = true; new code must type-check under --strict.",
                    MemoryType.CONVENTION, 0.95, 0.6, key="python.type_strictness", value="strict",
                    evidence_kind=EvidenceKind.CONFIG, excerpt="pyproject.toml: [tool.mypy] strict = true",
                    tags=["python", "typing"]))
        if "pyright" in tool or self._exists("pyrightconfig.json"):
            self._tool("pyright", "pyright configuration")
        if "pytest" in tool.get("pytest", {}) or "pytest" in tool or self._exists("pytest.ini", "conftest.py",
                                                                                   "tests/conftest.py"):
            self._tool("pytest", "pytest configuration")
        for name in ("flake8", "pylint"):
            if name in tool or (self._exists(f".{name}", f".{name}rc", "setup.cfg") and name in read_text(
                self.root, "setup.cfg"
            )):
                self._tool(name, f"{name} configuration")
        if self._exists(".pre-commit-config.yaml"):
            text = read_text(self.root, ".pre-commit-config.yaml")
            hooks = re.findall(r"^\s*-\s*id:\s*([\w.-]+)", text, re.M)
            self.stack.pre_commit_hooks = list(dict.fromkeys(hooks))
            if "ruff-format" in hooks:
                self._tool("ruff-format", ".pre-commit-config.yaml")
            if "black" in hooks:
                self._tool("black", ".pre-commit-config.yaml")
        self._from_packages(deps, "Python dependencies")

        line_length = (
            tool.get("ruff", {}).get("line-length")
            or tool.get("black", {}).get("line-length")
            or self._setup_cfg_value("flake8", "max-line-length")
        )
        if line_length:
            self.findings.append(Finding(
                ANALYZER, f"Python: maximum line length {line_length}",
                f"Lines are limited to {line_length} characters (configured in the project's lint/format settings).",
                MemoryType.CONVENTION, 0.95, 0.4, key="python.line_length", value=str(line_length),
                evidence_kind=EvidenceKind.CONFIG, excerpt=f"configured line length: {line_length}",
                tags=["python", "formatting"]))
        requires = project.get("requires-python") or (poetry.get("dependencies", {}) or {}).get("python")
        version_file = read_text(self.root, ".python-version").strip()
        if requires or version_file:
            self.stack.versions["python"] = str(requires or version_file)

    def _setup_cfg_value(self, section: str, key: str) -> Optional[str]:
        text = read_text(self.root, "setup.cfg") or read_text(self.root, ".flake8")
        match = re.search(rf"^\[{section}\][^\[]*?^{key}\s*=\s*(\d+)", text, re.M | re.S)
        return match.group(1) if match else None

    def _javascript(self) -> None:
        package = _jsonc(read_text(self.root, "package.json")) if self._exists("package.json") else None
        if not isinstance(package, dict):
            return
        manager = None
        declared = str(package.get("packageManager", ""))
        for name in ("pnpm", "yarn", "bun", "npm"):
            if declared.startswith(name):
                manager = (name, "package.json packageManager")
        if manager is None:
            for lockfile, name in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"),
                                   ("bun.lock", "bun"), ("package-lock.json", "npm")):
                if self._exists(lockfile):
                    manager = (name, lockfile)
                    break
        if manager:
            self._tool(manager[0], manager[1])
        deps = [*package.get("dependencies", {}), *package.get("devDependencies", {})]
        self._from_packages(deps, "package.json dependencies")
        if self._exists("biome.json", "biome.jsonc"):
            self._tool("biome", "biome.json")
        if self._exists(".prettierrc", ".prettierrc.json", ".prettierrc.js", "prettier.config.js",
                        ".prettierrc.yaml", ".prettierrc.cjs") or "prettier" in package:
            self._tool("prettier", "prettier configuration")
        if self._exists("eslint.config.js", "eslint.config.mjs", ".eslintrc", ".eslintrc.js", ".eslintrc.json",
                        ".eslintrc.cjs", "eslint.config.ts"):
            self._tool("eslint", "eslint configuration")

        runner = manager[0] if manager else "npm"
        prefix = {"npm": "npm run", "yarn": "yarn", "pnpm": "pnpm", "bun": "bun run"}[runner]
        scripts = package.get("scripts", {}) or {}
        for name in COMMAND_SCRIPTS:
            if name in scripts:
                command = f"{prefix} {name}" if not (name == "test" and runner in ("npm", "pnpm", "yarn")) else (
                    f"{runner} test")
                self.stack.commands.setdefault(name.replace(":", " "), command)

        tsconfig = _jsonc(read_text(self.root, "tsconfig.json")) if self._exists("tsconfig.json") else None
        if isinstance(tsconfig, dict):
            options = tsconfig.get("compilerOptions", {}) or {}
            if options.get("strict") is True:
                self.findings.append(Finding(
                    ANALYZER, "TypeScript: strict mode is on",
                    "tsconfig.json enables \"strict\": true — code must satisfy strict null checks and no "
                    "implicit any.",
                    MemoryType.CONVENTION, 0.95, 0.6, key="typescript.strict", value="true",
                    evidence_kind=EvidenceKind.CONFIG, excerpt="tsconfig.json: compilerOptions.strict = true",
                    tags=["typescript", "typing"]))
        engines = package.get("engines", {}) or {}
        nvmrc = read_text(self.root, ".nvmrc").strip()
        if engines.get("node") or nvmrc:
            self.stack.versions["node"] = str(engines.get("node") or nvmrc)

    def _go(self) -> None:
        text = read_text(self.root, "go.mod")
        if not text:
            return
        self._tool("go-test", "go.mod")
        for module in _GO_REQUIRE.findall(text):
            for prefix, name in GO_MODULES.items():
                if module.startswith(prefix):
                    tool = TOOLS_BY_NAME[name]
                    if tool.category in ("database", "cache"):
                        if tool.label not in self.stack.data_stores:
                            self.stack.data_stores.append(tool.label)
                    else:
                        self._tool(name, "go.mod")
        version = _GO_VERSION.search(text)
        if version:
            self.stack.versions["go"] = version.group(1)
        if self._exists(".golangci.yml", ".golangci.yaml", ".golangci.toml"):
            self._tool("golangci-lint", ".golangci.yml")
        self.stack.commands.setdefault("test", "go test ./...")

    def _rust(self) -> None:
        cargo = _toml(self.root, "Cargo.toml")
        if not cargo:
            return
        self._tool("cargo", "Cargo.toml")
        deps = [*cargo.get("dependencies", {}), *cargo.get("dev-dependencies", {})]
        self._from_packages(deps, "Cargo.toml dependencies")
        if self._exists("rustfmt.toml", ".rustfmt.toml"):
            self._tool("rustfmt", "rustfmt.toml")
        self.stack.commands.setdefault("test", "cargo test")
        edition = cargo.get("package", {}).get("edition")
        if edition:
            self.stack.versions["rust edition"] = str(edition)

    def _ruby(self) -> None:
        text = read_text(self.root, "Gemfile")
        if not text:
            return
        self._tool("bundler", "Gemfile")
        self._from_packages(_GEMFILE_GEM.findall(text), "Gemfile")
        if self._exists(".rubocop.yml"):
            self._tool("rubocop", ".rubocop.yml")

    def _ci_and_infra(self) -> None:
        workflows = self._glob(".github/workflows/*.yml") + self._glob(".github/workflows/*.yaml")
        if workflows:
            self._tool("github-actions", workflows[0])
        elif self._exists(".gitlab-ci.yml"):
            self._tool("gitlab-ci", ".gitlab-ci.yml")
        elif self._exists(".circleci/config.yml"):
            self._tool("circleci", ".circleci/config.yml")
        elif self._exists("Jenkinsfile"):
            self._tool("jenkins", "Jenkinsfile")
        if self._exists("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml"):
            self._tool("docker", "Dockerfile / compose file")
        if self._glob("*.tf") or self._glob("infra/**/*.tf"):
            self._tool("terraform", "*.tf")
        if self._glob("**/Chart.yaml") or self._glob("k8s/*.yaml"):
            self._tool("kubernetes", "Kubernetes manifests")

    def _make_and_just(self) -> None:
        for filename, runner in (("Makefile", "make"), ("justfile", "just"), ("Justfile", "just")):
            text = read_text(self.root, filename)
            if not text:
                continue
            for target in _MAKE_TARGET.findall(text):
                normalized = {"fmt": "format", "check": "lint", "typecheck": "typecheck", "run": "dev"}.get(target,
                                                                                                          target)
                if normalized in ("test", "lint", "format", "typecheck", "build", "dev", "migrate", "install"):
                    self.stack.commands.setdefault(normalized, f"{runner} {target}")

    def _editorconfig(self) -> None:
        text = read_text(self.root, ".editorconfig")
        if not text:
            return
        sections: dict[str, dict[str, str]] = defaultdict(dict)
        current = None
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1]
            elif current and "=" in line and not line.startswith(("#", ";")):
                k, _, v = line.partition("=")
                sections[current][k.strip().lower()] = v.strip().lower()
        default = sections.get("*", {})
        style = default.get("indent_style")
        size = default.get("indent_size")
        if style:
            value = "tabs" if style == "tab" else f"{size or '?'} spaces"
            self.findings.append(Finding(
                ANALYZER, f"Indent with {value} (.editorconfig)",
                f".editorconfig sets indent_style={style}" + (f", indent_size={size}" if size else "") +
                " for all files.",
                MemoryType.CONVENTION, 0.95, 0.3, key="style.indent", value=value,
                evidence_kind=EvidenceKind.CONFIG, excerpt=".editorconfig [*]", tags=["formatting"]))

    # ------------------------------------------------------------------
    def _emit(self) -> None:
        for key, tool in sorted(self.stack.tools.items()):
            if not tool.slot_key:
                continue
            label = category_label(tool.category)
            ecosystem = f"{tool.ecosystem.capitalize()} " if tool.ecosystem and tool.ecosystem != "jvm" else ""
            source = self.stack.tool_sources[key]
            importance = 0.7 if tool.category in ("test_framework", "package_manager", "web_framework",
                                                  "frontend_framework", "meta_framework") else 0.55
            self.findings.append(Finding(
                ANALYZER, f"{ecosystem}{label}: {tool.label}",
                f"The project uses {tool.label} as its {label} (evidence: {source}).",
                MemoryType.TOOLING, 0.9 if "lock" in source or "configuration" in source else 0.85, importance,
                key=tool.slot_key, value=tool.name, evidence_kind=EvidenceKind.CONFIG, excerpt=f"found in {source}",
                tags=[t for t in (tool.ecosystem, tool.category) if t]))
        if self.stack.commands:
            order = ["install", "dev", "build", "test", "test unit", "test e2e", "lint", "format", "typecheck",
                     "migrate", "start", "serve"]
            items = sorted(self.stack.commands.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)
            lines = "; ".join(f"{name}: `{command}`" for name, command in items)
            self.findings.append(Finding(
                ANALYZER, "How to run things in this repository",
                f"Commands — {lines}.",
                MemoryType.PROCEDURE, 0.9, 0.7, key="repo.commands", value=lines[:480],
                evidence_kind=EvidenceKind.CONFIG, excerpt="package.json scripts / Makefile / justfile",
                tags=["commands"]))
        for language, version in sorted(self.stack.versions.items()):
            self.findings.append(Finding(
                ANALYZER, f"{language.capitalize()} version: {version}",
                f"The project targets {language} {version}; do not use language features newer than that.",
                MemoryType.FACT, 0.95, 0.55, key=f"repo.{language.replace(' ', '_')}_version", value=version,
                evidence_kind=EvidenceKind.CONFIG, excerpt=f"{language} {version}", tags=[language]))
        if self.stack.pre_commit_hooks:
            hooks = ", ".join(self.stack.pre_commit_hooks[:12])
            self.findings.append(Finding(
                ANALYZER, "pre-commit hooks run on every commit",
                f"pre-commit runs: {hooks}. Code must pass them before committing.",
                MemoryType.TOOLING, 0.9, 0.45, key="repo.pre_commit", value=hooks[:480],
                evidence_kind=EvidenceKind.CONFIG, excerpt=".pre-commit-config.yaml", tags=["git", "linting"]))
