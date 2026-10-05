"""
Developer-domain vocabulary: tools, their categories, and concepts.

Two consumers depend on this one table:

* **Learning** maps a tool mention ("let's use pnpm", a `pnpm-lock.yaml`
  in a repo) onto a *slot key* such as `javascript.package_manager`.
  Because conversation extraction and repository analysis share this
  mapping, a preference stated in chat and a convention observed in code
  land on the same slot, which is what makes reinforcement and conflict
  resolution between the two possible.
* **Retrieval** expands text with *concepts*: "how should I write unit
  tests" and "Uses pytest with fixtures in conftest.py" share no words,
  but both carry the `testing` concept.

The table is plain data on purpose: extending Mnemosyne's knowledge of
a new tool is a one-line change, reviewable by anyone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Optional

from mnemosyne.core.text import stem, tokenize

#: Categories where a developer (or a repository) typically uses exactly
#: one tool at a time. Tools in these categories produce slot keys.
EXCLUSIVE_CATEGORIES: frozenset[str] = frozenset(
    {
        "test_framework",
        "e2e_framework",
        "formatter",
        "linter",
        "type_checker",
        "package_manager",
        "web_framework",
        "frontend_framework",
        "meta_framework",
        "orm",
        "bundler",
        "styling",
        "state_management",
        "validation",
        "task_queue",
        "build_tool",
        "error_handling",
        "ci",
    }
)

CATEGORY_LABELS: dict[str, str] = {
    "test_framework": "test framework",
    "e2e_framework": "end-to-end test framework",
    "formatter": "code formatter",
    "linter": "linter",
    "type_checker": "type checker",
    "package_manager": "package manager",
    "web_framework": "web framework",
    "frontend_framework": "frontend framework",
    "meta_framework": "full-stack framework",
    "orm": "database access layer",
    "database": "database",
    "cache": "cache",
    "bundler": "bundler",
    "styling": "styling approach",
    "state_management": "state management",
    "validation": "validation library",
    "task_queue": "task queue",
    "build_tool": "build tool",
    "error_handling": "error-handling library",
    "ci": "CI provider",
    "infrastructure": "infrastructure tooling",
    "cloud": "cloud platform",
    "migrations": "migration tool",
    "data": "data/ML library",
    "async_runtime": "async runtime",
    "cli": "CLI framework",
    "editor": "editor",
}

CATEGORY_CONCEPTS: dict[str, tuple[str, ...]] = {
    "test_framework": ("testing",),
    "e2e_framework": ("testing", "frontend"),
    "formatter": ("formatting", "style"),
    "linter": ("linting", "style"),
    "type_checker": ("typing",),
    "package_manager": ("packaging",),
    "web_framework": ("web",),
    "frontend_framework": ("frontend",),
    "meta_framework": ("frontend", "web"),
    "orm": ("database",),
    "database": ("database",),
    "cache": ("database", "performance"),
    "bundler": ("build",),
    "styling": ("frontend", "styling"),
    "state_management": ("frontend",),
    "validation": ("validation",),
    "task_queue": ("concurrency", "infrastructure"),
    "build_tool": ("build", "packaging"),
    "error_handling": ("errors",),
    "ci": ("cicd",),
    "infrastructure": ("infrastructure",),
    "cloud": ("infrastructure",),
    "migrations": ("database",),
    "data": ("data",),
    "async_runtime": ("concurrency",),
    "cli": ("cli",),
    "editor": ("editor",),
}


@dataclass(frozen=True)
class Tool:
    name: str
    category: str
    #: Ecosystem the slot key is namespaced under ("python", "javascript"
    #: ...). `None` for cross-ecosystem tools (databases, CI providers).
    ecosystem: Optional[str] = None
    #: Lowercase phrases that refer to this tool in prose.
    aliases: tuple[str, ...] = ()
    #: Package names that indicate the tool in a dependency manifest.
    packages: tuple[str, ...] = ()
    display: str = ""
    extra_concepts: tuple[str, ...] = field(default=())

    @property
    def label(self) -> str:
        return self.display or self.name

    @property
    def slot_key(self) -> Optional[str]:
        if self.category not in EXCLUSIVE_CATEGORIES:
            return None
        return f"{self.ecosystem}.{self.category}" if self.ecosystem else self.category


def _t(name: str, category: str, ecosystem: Optional[str], *aliases: str, packages: Iterable[str] = (),
       display: str = "", concepts: Iterable[str] = ()) -> Tool:
    return Tool(
        name=name,
        category=category,
        ecosystem=ecosystem,
        aliases=tuple({name, *aliases}),
        packages=tuple(packages),
        display=display,
        extra_concepts=tuple(concepts),
    )


PY, JS, GO, RS, RB, JVM, PHP, NET = "python", "javascript", "go", "rust", "ruby", "jvm", "php", "dotnet"

TOOLS: tuple[Tool, ...] = (
    # ---------------------------------------------------------------- Python
    _t("pytest", "test_framework", PY, "py.test", packages=["pytest"]),
    _t("unittest", "test_framework", PY, packages=[]),
    _t("black", "formatter", PY, packages=["black"], display="Black"),
    _t("ruff-format", "formatter", PY, "ruff format", display="ruff format"),
    _t("autopep8", "formatter", PY, packages=["autopep8"]),
    _t("yapf", "formatter", PY, packages=["yapf"]),
    _t("ruff", "linter", PY, packages=["ruff"]),
    _t("flake8", "linter", PY, packages=["flake8"]),
    _t("pylint", "linter", PY, packages=["pylint"]),
    _t("mypy", "type_checker", PY, packages=["mypy"]),
    _t("pyright", "type_checker", PY, "basedpyright", packages=["pyright", "basedpyright"]),
    _t("pip", "package_manager", PY, "pip-tools", "requirements.txt"),
    _t("poetry", "package_manager", PY, packages=["poetry"], display="Poetry"),
    _t("uv", "package_manager", PY, display="uv"),
    _t("pipenv", "package_manager", PY, packages=["pipenv"]),
    _t("conda", "package_manager", PY, "mamba", "micromamba"),
    _t("pdm", "package_manager", PY, packages=["pdm"]),
    _t("hatch", "package_manager", PY, packages=["hatch", "hatchling"]),
    _t("django", "web_framework", PY, packages=["django"], display="Django"),
    _t("flask", "web_framework", PY, packages=["flask"], display="Flask"),
    _t("fastapi", "web_framework", PY, packages=["fastapi"], display="FastAPI"),
    _t("aiohttp", "web_framework", PY, packages=["aiohttp"]),
    _t("litestar", "web_framework", PY, packages=["litestar"], display="Litestar"),
    _t("sqlalchemy", "orm", PY, packages=["sqlalchemy"], display="SQLAlchemy"),
    _t("sqlmodel", "orm", PY, packages=["sqlmodel"], display="SQLModel"),
    _t("peewee", "orm", PY, packages=["peewee"]),
    _t("tortoise-orm", "orm", PY, "tortoise", packages=["tortoise-orm"]),
    _t("django-orm", "orm", PY, "django orm"),
    _t("pydantic", "validation", PY, packages=["pydantic"], display="Pydantic"),
    _t("marshmallow", "validation", PY, packages=["marshmallow"]),
    _t("attrs", "validation", PY, packages=["attrs"]),
    _t("celery", "task_queue", PY, packages=["celery"], display="Celery"),
    _t("rq", "task_queue", PY, "python-rq", packages=["rq"]),
    _t("dramatiq", "task_queue", PY, packages=["dramatiq"]),
    _t("alembic", "migrations", PY, packages=["alembic"], display="Alembic"),
    _t("pandas", "data", PY, packages=["pandas"]),
    _t("numpy", "data", PY, packages=["numpy"], display="NumPy"),
    _t("polars", "data", PY, packages=["polars"]),
    _t("pytorch", "data", PY, "torch", packages=["torch"], display="PyTorch"),
    _t("tensorflow", "data", PY, packages=["tensorflow"], display="TensorFlow"),
    _t("scikit-learn", "data", PY, "sklearn", packages=["scikit-learn"]),
    _t("click", "cli", PY, packages=["click"]),
    _t("typer", "cli", PY, packages=["typer"]),
    _t("hypothesis", "test_library", PY, packages=["hypothesis"], concepts=["testing"]),
    _t("structlog", "logging", PY, packages=["structlog"], concepts=["logging"]),
    _t("loguru", "logging", PY, packages=["loguru"], concepts=["logging"]),
    _t("httpx", "http_client", PY, packages=["httpx"], concepts=["web"]),
    _t("requests", "http_client", PY, packages=["requests"], concepts=["web"]),
    # ----------------------------------------------------- JavaScript / TS
    _t("jest", "test_framework", JS, packages=["jest"], display="Jest"),
    _t("vitest", "test_framework", JS, packages=["vitest"], display="Vitest"),
    _t("mocha", "test_framework", JS, packages=["mocha"], display="Mocha"),
    _t("jasmine", "test_framework", JS, packages=["jasmine"]),
    _t("ava", "test_framework", JS, packages=["ava"]),
    _t("node-test", "test_framework", JS, "node:test", "node --test"),
    _t("playwright", "e2e_framework", JS, packages=["@playwright/test", "playwright"], display="Playwright"),
    _t("cypress", "e2e_framework", JS, packages=["cypress"], display="Cypress"),
    _t("prettier", "formatter", JS, packages=["prettier"], display="Prettier"),
    _t("biome", "linter", JS, "biomejs", packages=["@biomejs/biome"], display="Biome"),
    _t("eslint", "linter", JS, packages=["eslint"], display="ESLint"),
    _t("oxlint", "linter", JS, packages=["oxlint"]),
    _t("npm", "package_manager", JS),
    _t("yarn", "package_manager", JS, display="Yarn"),
    _t("pnpm", "package_manager", JS),
    _t("bun", "package_manager", JS, display="Bun"),
    _t("react", "frontend_framework", JS, "reactjs", "react.js", packages=["react"], display="React"),
    _t("vue", "frontend_framework", JS, "vuejs", "vue.js", packages=["vue"], display="Vue"),
    _t("svelte", "frontend_framework", JS, packages=["svelte"], display="Svelte"),
    _t("angular", "frontend_framework", JS, packages=["@angular/core"], display="Angular"),
    _t("solid", "frontend_framework", JS, "solidjs", packages=["solid-js"], display="SolidJS"),
    _t("preact", "frontend_framework", JS, packages=["preact"], display="Preact"),
    _t("next.js", "meta_framework", JS, "nextjs", "next", packages=["next"], display="Next.js"),
    _t("nuxt", "meta_framework", JS, "nuxtjs", packages=["nuxt"], display="Nuxt"),
    _t("sveltekit", "meta_framework", JS, "svelte kit", packages=["@sveltejs/kit"], display="SvelteKit"),
    _t("remix", "meta_framework", JS, packages=["@remix-run/react"], display="Remix"),
    _t("astro", "meta_framework", JS, packages=["astro"], display="Astro"),
    _t("express", "web_framework", JS, "expressjs", "express.js", packages=["express"], display="Express"),
    _t("fastify", "web_framework", JS, packages=["fastify"], display="Fastify"),
    _t("koa", "web_framework", JS, packages=["koa"], display="Koa"),
    _t("nestjs", "web_framework", JS, "nest.js", packages=["@nestjs/core"], display="NestJS"),
    _t("hono", "web_framework", JS, packages=["hono"], display="Hono"),
    _t("prisma", "orm", JS, packages=["prisma", "@prisma/client"], display="Prisma"),
    _t("drizzle", "orm", JS, "drizzle-orm", packages=["drizzle-orm"], display="Drizzle"),
    _t("typeorm", "orm", JS, packages=["typeorm"], display="TypeORM"),
    _t("sequelize", "orm", JS, packages=["sequelize"], display="Sequelize"),
    _t("mongoose", "orm", JS, packages=["mongoose"], display="Mongoose"),
    _t("knex", "orm", JS, packages=["knex"]),
    _t("tailwind", "styling", JS, "tailwindcss", packages=["tailwindcss"], display="Tailwind CSS"),
    _t("styled-components", "styling", JS, packages=["styled-components"]),
    _t("emotion", "styling", JS, packages=["@emotion/react"]),
    _t("sass", "styling", JS, "scss", packages=["sass"]),
    _t("css-modules", "styling", JS, "css modules"),
    _t("vite", "bundler", JS, packages=["vite"], display="Vite"),
    _t("webpack", "bundler", JS, packages=["webpack"]),
    _t("esbuild", "bundler", JS, packages=["esbuild"]),
    _t("rollup", "bundler", JS, packages=["rollup"]),
    _t("turbopack", "bundler", JS),
    _t("zod", "validation", JS, packages=["zod"], display="Zod"),
    _t("yup", "validation", JS, packages=["yup"]),
    _t("redux", "state_management", JS, "redux toolkit", packages=["redux", "@reduxjs/toolkit"], display="Redux"),
    _t("zustand", "state_management", JS, packages=["zustand"], display="Zustand"),
    _t("mobx", "state_management", JS, packages=["mobx"], display="MobX"),
    _t("jotai", "state_management", JS, packages=["jotai"]),
    _t("tanstack-query", "data_fetching", JS, "react query", "react-query", packages=["@tanstack/react-query"],
       concepts=["frontend"]),
    _t("typescript", "language", JS, "tsc", packages=["typescript"], display="TypeScript", concepts=["typing"]),
    # ------------------------------------------------------------------ Go
    _t("go-test", "test_framework", GO, "go test", "testing package", display="go test"),
    _t("testify", "test_library", GO, concepts=["testing"]),
    _t("gofmt", "formatter", GO, display="gofmt"),
    _t("gofumpt", "formatter", GO),
    _t("golangci-lint", "linter", GO, "golangci"),
    _t("gin", "web_framework", GO, display="Gin"),
    _t("echo", "web_framework", GO, display="Echo"),
    _t("fiber", "web_framework", GO, display="Fiber"),
    _t("chi", "web_framework", GO, display="chi"),
    _t("gorm", "orm", GO, display="GORM"),
    _t("sqlc", "orm", GO),
    _t("ent", "orm", GO),
    _t("cobra", "cli", GO, display="Cobra"),
    # ---------------------------------------------------------------- Rust
    _t("cargo", "package_manager", RS, display="Cargo"),
    _t("rustfmt", "formatter", RS),
    _t("clippy", "linter", RS, display="Clippy"),
    _t("tokio", "async_runtime", RS, packages=["tokio"], display="Tokio"),
    _t("async-std", "async_runtime", RS, packages=["async-std"]),
    _t("axum", "web_framework", RS, packages=["axum"], display="Axum"),
    _t("actix-web", "web_framework", RS, "actix", packages=["actix-web"], display="Actix Web"),
    _t("rocket", "web_framework", RS, packages=["rocket"], display="Rocket"),
    _t("anyhow", "error_handling", RS, packages=["anyhow"]),
    _t("thiserror", "error_handling", RS, packages=["thiserror"]),
    _t("diesel", "orm", RS, packages=["diesel"], display="Diesel"),
    _t("sea-orm", "orm", RS, packages=["sea-orm"], display="SeaORM"),
    _t("serde", "serialization", RS, packages=["serde"], concepts=["data"]),
    # ---------------------------------------------------------------- Ruby
    _t("rails", "web_framework", RB, "ruby on rails", packages=["rails"], display="Rails"),
    _t("sinatra", "web_framework", RB, packages=["sinatra"], display="Sinatra"),
    _t("rspec", "test_framework", RB, packages=["rspec"], display="RSpec"),
    _t("minitest", "test_framework", RB, packages=["minitest"], display="Minitest"),
    _t("rubocop", "linter", RB, packages=["rubocop"], display="RuboCop"),
    _t("bundler", "package_manager", RB, display="Bundler"),
    # ----------------------------------------------------------------- JVM
    _t("spring", "web_framework", JVM, "spring boot", "springboot", display="Spring Boot"),
    _t("junit", "test_framework", JVM, "junit5", display="JUnit"),
    _t("testng", "test_framework", JVM, display="TestNG"),
    _t("maven", "build_tool", JVM, "mvn", display="Maven"),
    _t("gradle", "build_tool", JVM, display="Gradle"),
    _t("hibernate", "orm", JVM, "jpa", display="Hibernate"),
    # ----------------------------------------------------------------- PHP
    _t("laravel", "web_framework", PHP, display="Laravel"),
    _t("symfony", "web_framework", PHP, display="Symfony"),
    _t("phpunit", "test_framework", PHP, display="PHPUnit"),
    _t("composer", "package_manager", PHP, display="Composer"),
    # ---------------------------------------------------------------- .NET
    _t("aspnet", "web_framework", NET, "asp.net", "asp.net core", display="ASP.NET Core"),
    _t("xunit", "test_framework", NET, display="xUnit"),
    _t("nunit", "test_framework", NET, display="NUnit"),
    _t("entity-framework", "orm", NET, "entity framework", "ef core", display="Entity Framework"),
    # ------------------------------------------------- Data stores (generic)
    _t("postgres", "database", None, "postgresql", "psql", "pg", packages=["psycopg", "psycopg2", "psycopg2-binary",
       "asyncpg", "pg"], display="PostgreSQL"),
    _t("mysql", "database", None, "mariadb", packages=["mysqlclient", "pymysql", "mysql2"], display="MySQL"),
    _t("sqlite", "database", None, "sqlite3", packages=["better-sqlite3", "sqlite3"], display="SQLite"),
    _t("mongodb", "database", None, "mongo", packages=["pymongo", "mongodb", "motor"], display="MongoDB"),
    _t("dynamodb", "database", None, display="DynamoDB"),
    _t("elasticsearch", "database", None, "opensearch", packages=["elasticsearch"], display="Elasticsearch"),
    _t("redis", "cache", None, "valkey", packages=["redis", "ioredis"], display="Redis"),
    _t("memcached", "cache", None),
    # ------------------------------------------------- Delivery & infra
    _t("github-actions", "ci", None, "github actions", "gh actions", display="GitHub Actions"),
    _t("gitlab-ci", "ci", None, "gitlab ci", display="GitLab CI"),
    _t("circleci", "ci", None, "circle ci", display="CircleCI"),
    _t("jenkins", "ci", None, display="Jenkins"),
    _t("docker", "infrastructure", None, "dockerfile", "docker compose", "docker-compose", display="Docker"),
    _t("kubernetes", "infrastructure", None, "k8s", "kubectl", "helm", display="Kubernetes"),
    _t("terraform", "infrastructure", None, "opentofu", display="Terraform"),
    _t("aws", "cloud", None, "amazon web services", display="AWS"),
    _t("gcp", "cloud", None, "google cloud", display="Google Cloud"),
    _t("azure", "cloud", None, display="Azure"),
    _t("vercel", "cloud", None, display="Vercel"),
    _t("fly.io", "cloud", None, "flyio", display="Fly.io"),
    _t("pre-commit", "infrastructure", None, "precommit", display="pre-commit", concepts=["linting", "git"]),
    # -------------------------------------------------------------- Editors
    _t("neovim", "editor", None, "nvim", "vim", display="Neovim"),
    _t("vscode", "editor", None, "vs code", "visual studio code", display="VS Code"),
    _t("jetbrains", "editor", None, "intellij", "pycharm", "webstorm", "goland", display="JetBrains IDEs"),
    _t("emacs", "editor", None, display="Emacs"),
)

#: Concept -> trigger words (raw; stemmed at load time). Concepts let
#: retrieval connect text that shares meaning but not vocabulary.
CONCEPT_TRIGGERS: dict[str, tuple[str, ...]] = {
    "testing": ("test", "tests", "testing", "spec", "specs", "tdd", "fixture", "fixtures", "mock", "mocks",
                "mocking", "stub", "assert", "assertion", "coverage", "unit test", "unit tests", "integration test",
                "integration tests", "e2e", "end-to-end",
                "conftest", "parametrize", "snapshot", "flaky", "regression", "testcase"),
    "formatting": ("format", "formatting", "formatter", "indent", "indentation", "whitespace", "quote", "quotes",
                   "semicolon", "semicolons", "line-length", "wrap", "tabs", "spaces", "trailing", "comma"),
    "linting": ("lint", "linter", "linting", "warning", "warnings", "static", "analysis"),
    "style": ("style", "convention", "conventions", "idiom", "idiomatic", "readable", "readability"),
    "typing": ("type", "types", "typing", "typed", "annotation", "annotations", "annotated", "hint", "hints",
               "generic", "generics", "strict", "interface", "interfaces", "any", "unknown"),
    "packaging": ("package", "packages", "dependency", "dependencies", "install", "installs", "lockfile",
                  "virtualenv", "venv", "registry", "publish", "monorepo", "workspace"),
    "database": ("database", "db", "sql", "query", "queries", "schema", "migration", "migrations", "table",
                 "tables", "transaction", "transactions", "orm", "persistence", "nosql", "column", "columns"),
    "git": ("git", "commit", "commits", "branch", "branches", "merge", "rebase", "pr", "pull", "push", "squash",
            "changelog", "tag", "diverged", "upstream", "force-push", "history", "cherry-pick"),
    "cicd": ("ci", "cd", "pipeline", "pipelines", "deploy", "deployment", "release", "releases", "workflow",
             "workflows", "artifact"),
    "infrastructure": ("infra", "infrastructure", "container", "containers", "cluster", "cloud", "hosting",
                       "provision", "staging", "production"),
    "errors": ("error", "errors", "exception", "exceptions", "raise", "throw", "catch", "failure", "fail",
               "bug", "bugs", "crash", "traceback", "panic", "retry", "retries", "fallback", "timeout", "timeouts"),
    "logging": ("log", "logs", "logging", "logger", "print", "trace", "tracing", "metric", "metrics",
                "monitoring", "observability", "telemetry"),
    "web": ("api", "apis", "rest", "http", "endpoint", "endpoints", "route", "routes", "router", "request",
            "requests", "response", "responses", "graphql", "grpc", "webhook", "webhooks", "middleware"),
    "concurrency": ("async", "await", "asyncio", "thread", "threads", "threading", "concurrency", "concurrent",
                    "parallel", "promise", "promises", "goroutine", "goroutines", "race", "worker", "workers"),
    "frontend": ("frontend", "ui", "ux", "component", "components", "jsx", "tsx", "css", "html", "dom",
                 "browser", "render", "rendering", "hook", "hooks", "props", "page", "pages", "hydration"),
    "styling": ("css", "styles", "styling", "theme", "theming", "layout", "responsive"),
    "security": ("api key", "access token", "secret key", "private key", "security", "secure", "auth",
                 "authentication", "authorization", "token", "tokens", "secret",
                 "secrets", "password", "passwords", "encrypt", "encryption", "permission", "permissions",
                 "vulnerability", "xss", "csrf", "injection", "sanitize", "credential", "credentials", "api-key",
                 "apikey", "admin", "admins", "role", "roles", "rbac", "oauth", "jwt"),
    "performance": ("performance", "perf", "fast", "slow", "latency", "optimize", "optimization", "cache",
                    "caching", "profiling", "benchmark", "throughput", "efficient", "n+1"),
    "documentation": ("doc", "docs", "docstring", "docstrings", "documentation", "readme", "comment",
                      "comments", "jsdoc"),
    "naming": ("name", "names", "naming", "rename", "camelcase", "snake_case", "pascalcase", "kebab-case",
               "identifier", "identifiers", "variable", "variables"),
    "architecture": ("architecture", "layer", "layers", "layered", "monolith", "microservice", "microservices",
                     "hexagonal", "dependency-injection", "coupling", "business"),
    "validation": ("validate", "validation", "validator", "schema", "parse", "parsing", "sanitize"),
    "build": ("build", "bundle", "bundler", "compile", "compiler", "transpile", "minify"),
    "data": ("data", "dataframe", "dataset", "ml", "training", "tensor", "array", "analytics"),
    "cli": ("cli", "command", "commands", "subcommand", "subcommands", "terminal", "shell", "flag", "flags", "argv"),
    "communication": ("answer", "answers", "explain", "explanation", "explanations", "verbose", "terse",
                      "concise", "brief", "tone", "emoji", "emojis"),
    "review": ("review", "reviews", "reviewer", "reviewing", "approve", "approval", "nit", "nits"),
    "editor": ("editor", "ide", "keybinding", "keybindings", "plugin", "plugins"),
    "datetime": ("time", "timestamp", "timestamps", "date", "dates", "datetime", "datetimes", "timezone",
                 "timezones", "utc", "tz", "clock", "duration", "calendar", "iso8601", "epoch"),
    "decision": ("should", "choose", "choice", "option", "options", "alternative", "alternatives", "tradeoff",
                 "tradeoffs", "trade-off", "trade-offs", "pros", "cons", "versus", "vs", "compare", "comparison",
                 "decide", "decided", "decision", "recommend", "recommendation"),
    "i18n": ("translate", "translation", "translations", "translated", "i18n", "l10n", "locale", "locales",
             "localization", "localize", "internationalization", "intl"),
    "money": ("money", "monetary", "currency", "currencies", "amount", "amounts", "price", "prices", "pricing",
              "payment", "payments", "refund", "refunds", "charge", "charges", "cents"),
}


# ----------------------------------------------------------------------
# Indexes (built once at import)
# ----------------------------------------------------------------------
def _build_alias_index() -> dict[str, Tool]:
    index: dict[str, Tool] = {}
    for tool in TOOLS:
        for alias in tool.aliases:
            index.setdefault(alias.lower(), tool)
    return index


def _build_package_index() -> dict[str, Tool]:
    index: dict[str, Tool] = {}
    for tool in TOOLS:
        for package in tool.packages:
            index.setdefault(package.lower(), tool)
    return index


def _build_concept_index() -> dict[str, frozenset[str]]:
    index: dict[str, set[str]] = {}
    for concept, words in CONCEPT_TRIGGERS.items():
        for word in words:
            if " " in word:
                # Phrase trigger: indexed as a bigram of stemmed tokens.
                parts = tokenize(word, drop_directives=False, keep_stopwords=True)
                index.setdefault("_".join(parts), set()).add(concept)
                continue
            # Only the word's own (first) token: "line-length" names the
            # concept as a whole; its parts ("line") are too generic.
            for token in tokenize(word, drop_directives=False, keep_stopwords=True)[:1] or [stem(word.lower())]:
                index.setdefault(token, set()).add(concept)
    for tool in TOOLS:
        concepts = set(CATEGORY_CONCEPTS.get(tool.category, ())) | set(tool.extra_concepts)
        if not concepts:
            continue
        for alias in tool.aliases:
            for token in tokenize(alias, drop_directives=False, keep_stopwords=True)[:1]:
                index.setdefault(token, set()).update(concepts)
    return {token: frozenset(concepts) for token, concepts in index.items()}


ALIAS_INDEX: dict[str, Tool] = _build_alias_index()
PACKAGE_INDEX: dict[str, Tool] = _build_package_index()
CONCEPT_INDEX: dict[str, frozenset[str]] = _build_concept_index()
TOOLS_BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


def tool_for_alias(text: str) -> Optional[Tool]:
    return ALIAS_INDEX.get(text.strip().lower())


def tool_for_package(package: str) -> Optional[Tool]:
    return PACKAGE_INDEX.get(package.strip().lower())


@lru_cache(maxsize=65536)
def concepts_for_token(token: str) -> frozenset[str]:
    return CONCEPT_INDEX.get(token, frozenset())


#: Concepts that name an *activity* — the kind of work being done. They
#: decide which conventions apply ("write tests" -> testing conventions),
#: so they weigh more than *domain* concepts describing the object of the
#: work ("...for the refund service" -> money).
ACTIVITY_CONCEPTS: frozenset[str] = frozenset(
    {
        "testing", "formatting", "linting", "style", "typing", "packaging", "git", "cicd", "errors", "logging",
        "documentation", "naming", "review", "i18n", "security", "performance", "validation", "build",
        "decision", "communication",
    }
)
DOMAIN_CONCEPT_WEIGHT = 0.5


def concept_weight(concept: str) -> float:
    return 1.0 if concept in ACTIVITY_CONCEPTS else DOMAIN_CONCEPT_WEIGHT


def concepts_for_text(text: str) -> list[str]:
    """Concepts evoked by free text. Function words are kept ("should we
    ...", "X versus Y") and identifiers are not split (`payments-api`
    names a project, not the money concept)."""
    return concepts_for_tokens(tokenize(text, drop_directives=False, keep_stopwords=True, split_identifiers=False))


def concepts_for_tokens(tokens: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    previous: Optional[str] = None
    for token in tokens:
        for concept in concepts_for_token(token):
            seen.setdefault(concept, None)
        if previous is not None:
            for concept in concepts_for_token(f"{previous}_{token}"):
                seen.setdefault(concept, None)
        previous = token
    return list(seen)


def vocabulary_fingerprint() -> str:
    """Changes whenever anything that shapes tokenization or concepts
    changes. Embedders built on this vocabulary fold it into their
    model id, so stored vectors are recomputed instead of silently
    mixing two feature spaces."""
    import hashlib

    from mnemosyne.core import text as text_module

    material = repr(
        (
            sorted((k, v) for k, v in CONCEPT_TRIGGERS.items()),
            sorted((t.name, t.category, t.aliases) for t in TOOLS),
            sorted(text_module.LEMMAS.items()),
            sorted(text_module.STOPWORDS),
            sorted(text_module.DIRECTIVE_WORDS),
            [text_module.stem(w) for w in ("translated", "services", "queries", "matches", "caching")],
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]


#: Aliases that are also ordinary English (or very common) words. Free
#: text scanning ignores them; they still match when they are the direct
#: object of a directive ("use black"), see `match_tool_prefix`.
AMBIGUOUS_ALIASES: frozenset[str] = frozenset(
    """
    next solid echo chi ent fiber gin rocket express emotion rollup bun click requests
    attrs rq uv pip spring remix astro hatch black mocha jasmine ava koa jest yarn cargo
    composer poetry rails vim tag ruff-format pg mongo serde sass scss knex zod yup jotai
    vite ruff rspec tortoise conda mamba aws azure gcp redis docker helm
    """.split()
)

_ALIASES_LONGEST_FIRST: tuple[str, ...] = tuple(sorted(ALIAS_INDEX, key=len, reverse=True))
_SCAN_PUNCT_RE = re.compile(r"[,;!?()\[\]{}\"'`*]")
_SCAN_TRAILING_RE = re.compile(r"[.:](?=\s|$)")


def _scan_normalize(text: str) -> str:
    lowered = _SCAN_PUNCT_RE.sub(" ", text.lower())
    lowered = _SCAN_TRAILING_RE.sub(" ", lowered)
    return " " + " ".join(lowered.split()) + " "


def match_tool_prefix(phrase: str) -> Optional[Tool]:
    """The tool named at the *start* of `phrase` ("pnpm for everything" ->
    pnpm), preferring the longest alias. Ambiguous aliases are accepted
    here because the caller has already established a directive context."""
    normalized = _scan_normalize(phrase)
    for alias in _ALIASES_LONGEST_FIRST:
        if normalized.startswith(f" {alias} "):
            return ALIAS_INDEX[alias]
    return None


def find_tools_in_text(text: str, *, include_ambiguous: bool = False) -> list[Tool]:
    """Every known tool mentioned in `text`, longest alias first so that
    "ruff format" is preferred over "ruff" when both match."""
    lowered = _scan_normalize(text)
    found: list[Tool] = []
    taken: list[tuple[int, int]] = []
    for alias in _ALIASES_LONGEST_FIRST:
        if not include_ambiguous and alias in AMBIGUOUS_ALIASES:
            continue
        needle = f" {alias} "
        start = lowered.find(needle)
        while start != -1:
            span = (start + 1, start + len(needle) - 1)
            if not any(a < span[1] and span[0] < b for a, b in taken):
                taken.append(span)
                tool = ALIAS_INDEX[alias]
                if tool not in found:
                    found.append(tool)
                break
            start = lowered.find(needle, start + 1)
    return found


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category.replace("_", " "))
