"""
A realistic developer persona used for retrieval evaluation and demos.

"Sam" is a senior backend engineer: Python/FastAPI day to day, some
TypeScript/React on the dashboard, a small Go CLI, and one legacy
Python 3.8 service with different conventions. The memories below are
the kind Mnemosyne accumulates over months — preferences, conventions,
tooling, patterns, incidents, decisions, procedures — plus a deliberate
haystack of true-but-irrelevant memories that retrieval must ignore.

Queries are phrased the way developers actually talk to an assistant
("write unit tests for the refund service"), each with graded relevance
labels: 3 = the answer must use this, 2 = clearly relevant, 1 = useful
background. Labels were written before tuning and are not adjusted to
fit the system's output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from mnemosyne.domain.enums import MemoryType as T

PAYMENTS = "github.com/acme/payments-api"
DASHBOARD = "github.com/acme/web-dashboard"
LEDGER = "github.com/acme/ledger-cli"
LEGACY = "github.com/acme/legacy-billing"


@dataclass(frozen=True)
class PersonaMemory:
    label: str
    memory_type: T
    title: str
    content: str
    scope: str = "global"
    key: Optional[str] = None
    value: Optional[str] = None
    importance: float = 0.6
    confidence: float = 0.9
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PersonaQuery:
    text: str
    relevant: dict[str, int]
    repo: Optional[str] = None
    languages: tuple[str, ...] = ()
    #: Labels that must NOT appear in the top results (scope leakage).
    forbidden: tuple[str, ...] = field(default_factory=tuple)


M = PersonaMemory

MEMORIES: tuple[PersonaMemory, ...] = (
    # ------------------------------------------------------------ global
    M("g_terse", T.PREFERENCE, "Keep explanations terse",
      "Sam prefers short, direct answers: lead with the code change, skip the preamble, no emojis.", importance=0.8),
    M("g_no_emoji", T.RULE, "No emojis in code, commit messages or docs",
      "Emojis are never used in source code, commit messages, PR descriptions or documentation."),
    M("g_commits_conv", T.CONVENTION, "Commit messages follow Conventional Commits",
      "Commit messages use Conventional Commits (feat:, fix:, chore:, refactor:) with an imperative subject line of "
      "at most 72 characters and a body explaining why.", key="commit.style", value="conventional", importance=0.7),
    M("g_small_prs", T.PREFERENCE, "Small, focused pull requests",
      "Prefers pull requests under about 400 changed lines that address one concern each; split refactors from "
      "behaviour changes."),
    M("g_tests_first", T.PREFERENCE, "Regression test before the bug fix",
      "When fixing a bug, first write a failing test that reproduces it, then fix the code."),
    M("g_no_force_push", T.RULE, "Never force-push to main or shared branches",
      "Force-pushing rewrites shared history; on a diverged shared branch merge or rebase locally instead, and only "
      "force-push personal branches with --force-with-lease.", importance=0.85),
    M("g_review_style", T.PREFERENCE, "Code review style",
      "When reviewing code, give specific line-level suggestions with the reasoning behind each one, flag correctness "
      "issues before style nits."),
    M("g_editor", T.TOOLING, "Uses Neovim",
      "Edits code in Neovim with LSP; do not suggest VS Code specific steps or extensions.", key="editor", value="neovim"),
    M("g_os", T.FACT, "Works on macOS with Homebrew",
      "Development machine is a MacBook (Apple Silicon); install system tools with Homebrew (brew install)."),
    M("g_skill_py", T.SKILL, "Expert Python developer",
      "Ten years of Python; skip basics and explain only non-obvious library behaviour.", importance=0.7),
    M("g_skill_go", T.SKILL, "Intermediate Go developer",
      "Comfortable with Go syntax; appreciates explanations of idioms like error wrapping and context propagation."),
    M("g_skill_rust", T.SKILL, "New to Rust",
      "Just started learning Rust: explain ownership, borrowing and lifetimes when they come up.", importance=0.7),
    M("g_explain_tradeoffs", T.PREFERENCE, "Design proposals list trade-offs",
      "When proposing an architecture or technology choice, list the trade-offs of each option and end with a clear "
      "recommendation."),
    M("g_docs", T.CONVENTION, "Docstrings explain why, not what",
      "Public functions and classes get docstrings that explain intent and constraints rather than restating the code."),
    M("g_no_print", T.ANTI_PATTERN, "No print() debugging in committed code",
      "Never leave print() statements in committed code; use the module logger at debug level instead."),
    M("g_branch_naming", T.CONVENTION, "Branch naming",
      "Branches are named type/short-description, e.g. feat/partial-refunds or fix/webhook-ordering.",
      key="git.branch_naming", value="type/short-description"),
    M("g_secret", T.RULE, "Never hardcode secrets",
      "Secrets, API keys and tokens never go into code, tests or logs; read them from environment variables through "
      "the settings object.", importance=0.9),
    M("g_timezones", T.CONVENTION, "Timezone-aware UTC timestamps",
      "All timestamps are timezone-aware UTC datetimes; never create naive datetimes, convert at the edges."),
    # ------------------------------------------------------------ python
    M("py_pytest", T.TOOLING, "Tests use pytest",
      "Python tests are written with pytest using fixtures in conftest.py; no unittest.TestCase classes.",
      scope="lang:python", key="python.test_framework", value="pytest", importance=0.75),
    M("py_parametrize", T.PATTERN, "Table-driven tests with parametrize",
      "Uses pytest.mark.parametrize for table-driven test cases instead of loops inside a test.", scope="lang:python"),
    M("py_ruff", T.TOOLING, "Lint with ruff",
      "Python linting with ruff (rules E, F, I, B, UP); fix findings rather than adding noqa.", scope="lang:python",
      key="python.linter", value="ruff"),
    M("py_formatter", T.TOOLING, "Format with ruff format",
      "Code formatting with ruff format at line length 100.", scope="lang:python", key="python.formatter",
      value="ruff-format"),
    M("py_mypy", T.TOOLING, "mypy in strict mode",
      "Type checking with mypy --strict; every function is fully annotated.", scope="lang:python",
      key="python.type_checker", value="mypy"),
    M("py_uv", T.TOOLING, "Dependencies managed with uv",
      "Python dependencies are managed with uv: uv add, uv sync, uv run; the uv.lock lockfile is committed.",
      scope="lang:python", key="python.package_manager", value="uv"),
    M("py_typing_style", T.CONVENTION, "Modern type hint syntax",
      "Use built-in generics (list[str]) and X | None unions instead of typing.List and Optional.", scope="lang:python"),
    M("py_dataclasses", T.PATTERN, "Frozen dataclasses for domain objects",
      "Domain objects are frozen dataclasses with slots; Pydantic models only at API boundaries.", scope="lang:python"),
    M("py_errors", T.PATTERN, "Custom exception hierarchy",
      "Each service defines a custom exception hierarchy rooted in one base error; re-raise with raise ... from exc to "
      "keep the cause, and map errors to responses in one place.", scope="lang:python"),
    M("py_logging", T.CONVENTION, "Structured logging",
      "Use logging.getLogger(__name__) with structured extra fields; no f-strings inside log calls.",
      scope="lang:python"),
    M("py_pathlib", T.CONVENTION, "pathlib over os.path",
      "Work with filesystem paths through pathlib.Path, never os.path string juggling.", scope="lang:python"),
    M("py_async", T.PATTERN, "Async HTTP with httpx",
      "Async code makes HTTP calls with httpx.AsyncClient and explicit timeouts; never call requests inside async "
      "functions.", scope="lang:python"),
    M("py_imports", T.CONVENTION, "Absolute imports only",
      "Python modules use absolute imports; no relative imports.", scope="lang:python"),
    M("py_mutable_default", T.ANTI_PATTERN, "No mutable default arguments",
      "Never use a list or dict as a default argument; default to None and create inside.", scope="lang:python"),
    M("py_naming", T.CONVENTION, "Python naming conventions",
      "snake_case for functions, variables and modules; PascalCase for classes; UPPER_CASE for constants.",
      scope="lang:python", key="python.naming", value="snake_case"),
    M("py_docstring_style", T.CONVENTION, "Google-style docstrings",
      "Docstrings follow the Google style with Args:, Returns: and Raises: sections.", scope="lang:python",
      key="python.docstring_style", value="google"),
    M("py_fixtures_factories", T.PATTERN, "Factory fixtures for test data",
      "Test data comes from factory fixtures like make_invoice(**overrides) rather than shared module-level objects.",
      scope="lang:python"),
    M("py_no_mock_db", T.PREFERENCE, "Integration tests use a real database",
      "Integration tests run against a real PostgreSQL in Docker (testcontainers) instead of mocking the session.",
      scope="lang:python"),
    # -------------------------------------------------------- typescript
    M("ts_pnpm", T.TOOLING, "Use pnpm",
      "JavaScript and TypeScript packages are installed with pnpm (pnpm add, pnpm install); never npm install.",
      scope="lang:typescript", key="javascript.package_manager", value="pnpm"),
    M("ts_vitest", T.TOOLING, "Unit tests with Vitest",
      "Frontend unit tests use Vitest with @testing-library/react.", scope="lang:typescript",
      key="javascript.test_framework", value="vitest"),
    M("ts_strict", T.CONVENTION, "TypeScript strict, no any",
      "TypeScript runs in strict mode; never use any — use unknown and narrow it.", scope="lang:typescript"),
    M("ts_function_components", T.PATTERN, "Function components with hooks",
      "React components are function components using hooks; no class components.", scope="lang:typescript"),
    M("ts_named_exports", T.CONVENTION, "Named exports only",
      "Modules use named exports; no default exports.", scope="lang:typescript"),
    M("ts_tailwind", T.TOOLING, "Styling with Tailwind",
      "Components are styled with Tailwind utility classes; no CSS-in-JS.", scope="lang:typescript",
      key="javascript.styling", value="tailwind"),
    M("ts_query", T.PATTERN, "Server state with TanStack Query",
      "Fetch and cache server data with TanStack Query hooks; don't copy server data into global state.",
      scope="lang:typescript"),
    M("ts_zod", T.PATTERN, "Validate API responses with zod",
      "Responses from the backend are parsed with zod schemas at the boundary before use.", scope="lang:typescript"),
    M("ts_eslint", T.TOOLING, "ESLint and Prettier",
      "Linting with ESLint (typescript-eslint) and formatting with Prettier: single quotes, no semicolons.",
      scope="lang:typescript", key="javascript.linter", value="eslint"),
    M("ts_types_over_interfaces", T.CONVENTION, "Type aliases over interfaces",
      "Prefer type aliases; use interfaces only for public contracts meant to be extended.", scope="lang:typescript"),
    # ---------------------------------------------------------------- go
    M("go_errors", T.PATTERN, "Wrap Go errors with %w",
      "Wrap errors with fmt.Errorf(\"doing x: %w\", err) and inspect them with errors.Is and errors.As.",
      scope="lang:go"),
    M("go_table_tests", T.PATTERN, "Table-driven Go tests",
      "Go tests are table-driven with t.Run subtests per case.", scope="lang:go"),
    M("go_context", T.CONVENTION, "context.Context first",
      "Functions that do I/O take ctx context.Context as their first parameter.", scope="lang:go"),
    M("go_lint", T.TOOLING, "golangci-lint and gofumpt",
      "Go code is linted with golangci-lint and formatted with gofumpt.", scope="lang:go", key="go.linter",
      value="golangci-lint"),
    # ------------------------------------------------------ payments-api
    M("pa_overview", T.ARCHITECTURE, "payments-api stack",
      "payments-api is a FastAPI service on SQLAlchemy 2.0 and PostgreSQL 16 with Alembic migrations and Celery "
      "workers on Redis.", scope=f"repo:{PAYMENTS}", importance=0.75),
    M("pa_layers", T.ARCHITECTURE, "Layering: routers, services, repositories",
      "Routers only parse requests; business logic lives in services; repositories own all SQL.",
      scope=f"repo:{PAYMENTS}", importance=0.7),
    M("pa_money", T.RULE, "Money as integer minor units",
      "Monetary amounts are stored as integers in minor units (cents) together with an ISO currency code; never "
      "floats or decimals in the database.", scope=f"repo:{PAYMENTS}", importance=0.9),
    M("pa_idempotency", T.DECISION, "Idempotency keys on POST endpoints",
      "Every POST endpoint accepts an Idempotency-Key header stored for 24 hours so client retries are safe; decided "
      "after the double-charge incident.", scope=f"repo:{PAYMENTS}", importance=0.8),
    M("pa_stripe", T.FACT, "Stripe is the payment provider",
      "Card payments go through Stripe; webhook signatures are verified with STRIPE_WEBHOOK_SECRET.",
      scope=f"repo:{PAYMENTS}"),
    M("pa_stripe_order", T.FAILURE, "Stripe webhooks arrive out of order",
      "Incident INC-412: Stripe webhook events were processed twice and out of order. Handlers must dedupe on event "
      "id and ignore events older than the stored state.", scope=f"repo:{PAYMENTS}", importance=0.8),
    M("pa_migrations", T.PROCEDURE, "Creating a database migration",
      "New schema changes: uv run alembic revision --autogenerate -m '...'; review the generated diff; never edit a "
      "migration that has been applied.", scope=f"repo:{PAYMENTS}"),
    M("pa_deploy", T.PROCEDURE, "Deploying payments-api",
      "Merging to main runs GitHub Actions, builds the image and Argo CD syncs it to staging; production deploys "
      "happen by pushing a release tag.", scope=f"repo:{PAYMENTS}"),
    M("pa_test_cmd", T.PROCEDURE, "Running the payments-api tests",
      "Run tests with uv run pytest -x -q; integration tests need docker compose up -d postgres first.",
      scope=f"repo:{PAYMENTS}"),
    M("pa_flaky", T.FAILURE, "Flaky refund concurrency test",
      "test_refund_concurrency was flaky in CI because tests shared database state; fixed by wrapping each test in a "
      "transaction that is rolled back.", scope=f"repo:{PAYMENTS}"),
    M("pa_n_plus_one", T.FAILURE, "N+1 queries on the invoices endpoint",
      "The /invoices list endpoint was slow because of an N+1 query; fixed with selectinload(Invoice.lines). Watch for "
      "this in list endpoints.", scope=f"repo:{PAYMENTS}"),
    M("pa_ci", T.TOOLING, "payments-api CI pipeline",
      "CI on GitHub Actions runs ruff, mypy and pytest with a 90% coverage gate.", scope=f"repo:{PAYMENTS}",
      key="ci", value="github-actions"),
    M("pa_auth", T.ARCHITECTURE, "Authorization with JWT scopes",
      "Requests carry OAuth2 JWTs from the identity service; endpoints check permissions with the require_scope "
      "dependency, e.g. require_scope('payments:admin').", scope=f"repo:{PAYMENTS}"),
    M("pa_feature_flags", T.DECISION, "Risky changes ship behind feature flags",
      "New or risky payment flows are released behind LaunchDarkly feature flags.", scope=f"repo:{PAYMENTS}"),
    # ---------------------------------------------------- legacy-billing
    M("lb_unittest", T.TOOLING, "legacy-billing uses unittest",
      "legacy-billing tests use unittest.TestCase classes; keep that style there for consistency.",
      scope=f"repo:{LEGACY}", key="python.test_framework", value="unittest"),
    M("lb_py38", T.FACT, "legacy-billing runs Python 3.8",
      "legacy-billing is pinned to Python 3.8: no match statements and no X | None union syntax.",
      scope=f"repo:{LEGACY}"),
    # ----------------------------------------------------- web-dashboard
    M("wd_overview", T.ARCHITECTURE, "web-dashboard stack",
      "web-dashboard is a Next.js 14 app router project with React 18, TypeScript, Tailwind and TanStack Query, "
      "deployed on Vercel.", scope=f"repo:{DASHBOARD}", importance=0.75),
    M("wd_api_client", T.PATTERN, "Use the generated API client",
      "All backend calls go through src/lib/api.ts, generated from the OpenAPI spec; never call fetch directly from "
      "components.", scope=f"repo:{DASHBOARD}"),
    M("wd_e2e", T.TOOLING, "End-to-end tests with Playwright",
      "Browser end-to-end tests live in e2e/ and use Playwright; run them with pnpm test:e2e.",
      scope=f"repo:{DASHBOARD}", key="javascript.e2e_framework", value="playwright"),
    M("wd_i18n", T.CONVENTION, "All UI copy goes through next-intl",
      "Every user-facing string is translated through next-intl's t() function; no hardcoded copy in components.",
      scope=f"repo:{DASHBOARD}"),
    M("wd_a11y", T.RULE, "Accessible names on interactive elements",
      "Every button, link and input needs an accessible name; Playwright runs axe accessibility checks.",
      scope=f"repo:{DASHBOARD}"),
    M("wd_hydration", T.FAILURE, "Hydration mismatch from Date.now()",
      "A hydration mismatch came from calling Date.now() during render; compute dates on the server or in useEffect.",
      scope=f"repo:{DASHBOARD}"),
    # -------------------------------------------------------- ledger-cli
    M("lc_cobra", T.TOOLING, "ledger-cli uses Cobra",
      "ledger-cli commands are built with Cobra and configuration is loaded with Viper.", scope=f"repo:{LEDGER}"),
    M("lc_release", T.PROCEDURE, "Releasing ledger-cli",
      "Releases are built with goreleaser when a version tag is pushed.", scope=f"repo:{LEDGER}"),
    # ------------------------------------------------- decisions & goals
    M("dec_postgres", T.DECISION, "PostgreSQL over MongoDB for payments",
      "Chose PostgreSQL rather than MongoDB for payment data because of transactional guarantees and relational "
      "reporting."),
    M("dec_monorepo", T.DECISION, "No monorepo for now",
      "Decided against merging services into a monorepo this year; the build tooling cost outweighed the benefits."),
    M("goal_celery_beat", T.GOAL, "Move reporting jobs to Celery beat",
      "Q4 goal: migrate the nightly reporting cron jobs to Celery beat schedules."),
    # ---------------------------------------------- haystack (distractors)
    M("n_meetings", T.PREFERENCE, "Meetings after 10am", "Prefers meetings scheduled after 10am.", importance=0.3),
    M("n_standup", T.FACT, "Team standup time", "Team standup is at 9:30 CET every weekday.", importance=0.3),
    M("n_oncall", T.PROCEDURE, "On-call handoff", "On-call handoff happens every Monday; update the runbook page.",
      importance=0.4),
    M("n_vacation", T.FACT, "Out of office over the holidays", "Out of office from December 20 to January 5.",
      importance=0.3),
    M("n_book", T.FACT, "Currently reading DDIA", "Reading Designing Data-Intensive Applications.", importance=0.2),
    M("n_1password", T.TOOLING, "1Password CLI for local secrets",
      "Uses the 1Password CLI (op) to inject secrets into the local shell.", importance=0.4),
    M("n_grafana", T.FACT, "Grafana dashboards", "Latency dashboards for payments live in Grafana under Payments/API.",
      importance=0.4),
    M("n_sentry", T.FACT, "Errors are tracked in Sentry", "Exceptions from services are reported to Sentry.",
      importance=0.4),
    M("n_pagerduty", T.FACT, "PagerDuty rotation", "Sam is on the payments PagerDuty rotation every fourth week.",
      importance=0.3),
    M("n_dark_mode", T.PREFERENCE, "Dark mode", "Prefers dark themes in tools and terminals.", importance=0.2),
    M("n_keyboard", T.FACT, "Keyboard layout", "Uses a split keyboard with a Colemak layout.", importance=0.2),
    M("n_coffee", T.FACT, "Coffee order", "Drinks flat whites.", importance=0.1),
    M("n_conference", T.GOAL, "Conference talk", "Wants to give a talk about idempotent payment APIs next year.",
      importance=0.3),
    M("n_mentoring", T.FACT, "Mentors a junior engineer", "Mentors Alex, a junior engineer on the team.",
      importance=0.3),
    M("n_slack", T.PREFERENCE, "Async communication", "Prefers async Slack threads over calls for small questions.",
      importance=0.3),
    M("n_timezone", T.FACT, "Lives in Berlin", "Based in Berlin (CET/CEST).", importance=0.3),
    M("n_hiring", T.FACT, "Hiring interviews", "Runs system design interviews on Thursdays.", importance=0.2),
    M("n_laptop", T.FACT, "Laptop specs", "MacBook Pro with 32 GB RAM.", importance=0.2),
    M("n_docker_mem", T.FACT, "Docker Desktop memory", "Docker Desktop is configured with 8 GB of memory.",
      importance=0.3),
    M("n_jira", T.FACT, "Ticket tracker", "Work is tracked in Jira project PAY.", importance=0.3),
    M("n_wiki", T.FACT, "Team wiki", "Architecture decision records live in the Confluence space PAYENG.",
      importance=0.3),
    M("n_offsite", T.FACT, "Team offsite", "Team offsite planned for March in Lisbon.", importance=0.1),
    M("n_learning_k8s", T.GOAL, "Learn more Kubernetes", "Wants to get the CKA certification.", importance=0.3),
    M("n_music", T.PREFERENCE, "Focus music", "Listens to ambient music while coding.", importance=0.1),
)

Q = PersonaQuery
PY = ("python",)
TS = ("typescript",)
GO = ("go",)

QUERIES: tuple[PersonaQuery, ...] = (
    Q("write unit tests for the refund service",
      {"py_pytest": 3, "py_fixtures_factories": 2, "py_parametrize": 2, "pa_test_cmd": 2, "py_no_mock_db": 1,
       "g_tests_first": 1}, repo=PAYMENTS, languages=PY, forbidden=("lb_unittest", "ts_vitest")),
    Q("add a new database table for invoices",
      {"pa_migrations": 3, "pa_overview": 2, "pa_money": 1, "pa_layers": 1}, repo=PAYMENTS, languages=PY),
    Q("how do I deploy this to production", {"pa_deploy": 3, "pa_ci": 1}, repo=PAYMENTS, languages=PY),
    Q("fix the stripe webhook handler that processes events twice",
      {"pa_stripe_order": 3, "pa_stripe": 2, "pa_idempotency": 2, "g_tests_first": 1}, repo=PAYMENTS, languages=PY),
    Q("write a commit message for these changes", {"g_commits_conv": 3, "g_no_emoji": 1}),
    Q("handle errors when the payment provider times out",
      {"py_errors": 3, "pa_stripe": 2, "py_async": 1, "py_logging": 1}, repo=PAYMENTS, languages=PY),
    Q("add logging to the reconciliation job", {"py_logging": 3, "g_no_print": 2}, repo=PAYMENTS, languages=PY),
    Q("set up linting and formatting for a new python service",
      {"py_ruff": 3, "py_formatter": 3, "py_mypy": 2, "py_uv": 1}, languages=PY),
    Q("install the project dependencies", {"py_uv": 3}, repo=PAYMENTS, languages=PY, forbidden=("ts_pnpm",)),
    Q("install the project dependencies", {"ts_pnpm": 3}, repo=DASHBOARD, languages=TS, forbidden=("py_uv",)),
    Q("create a new react component for the invoice table",
      {"ts_function_components": 3, "ts_named_exports": 2, "ts_tailwind": 2, "wd_i18n": 2, "ts_strict": 1,
       "wd_a11y": 1}, repo=DASHBOARD, languages=TS, forbidden=("pa_money", "pa_layers")),
    Q("fetch invoices from the backend api in the dashboard",
      {"wd_api_client": 3, "ts_query": 3, "ts_zod": 2}, repo=DASHBOARD, languages=TS),
    Q("write an end to end test for the login page", {"wd_e2e": 3, "wd_a11y": 1}, repo=DASHBOARD, languages=TS),
    Q("the page throws a hydration error", {"wd_hydration": 3}, repo=DASHBOARD, languages=TS),
    Q("add an amount field to the payment model", {"pa_money": 3, "pa_migrations": 2, "py_dataclasses": 1},
      repo=PAYMENTS, languages=PY),
    Q("the invoices endpoint is really slow", {"pa_n_plus_one": 3, "n_grafana": 1}, repo=PAYMENTS, languages=PY),
    Q("add type hints to this module", {"py_typing_style": 3, "py_mypy": 3}, languages=PY),
    Q("write a docstring for this function", {"py_docstring_style": 3, "g_docs": 2}, languages=PY),
    Q("explain how ownership and borrowing work in this rust code", {"g_skill_rust": 3}, languages=("rust",)),
    Q("add a new subcommand to the cli", {"lc_cobra": 3, "go_errors": 1, "go_context": 1}, repo=LEDGER, languages=GO),
    Q("write tests for the parser", {"go_table_tests": 3}, repo=LEDGER, languages=GO,
      forbidden=("py_pytest", "ts_vitest")),
    Q("return an error from the storage layer", {"go_errors": 3}, repo=LEDGER, languages=GO),
    Q("make this endpoint safe for clients to retry", {"pa_idempotency": 3}, repo=PAYMENTS, languages=PY),
    Q("restrict this route to admins", {"pa_auth": 3}, repo=PAYMENTS, languages=PY),
    Q("should we use mongodb for the new service", {"dec_postgres": 3, "g_explain_tradeoffs": 2}),
    Q("where should the business logic for refunds go", {"pa_layers": 3, "pa_overview": 1}, repo=PAYMENTS,
      languages=PY),
    Q("my branch diverged from origin, how do I push", {"g_no_force_push": 3, "g_branch_naming": 1}),
    Q("review this pull request", {"g_review_style": 3, "g_small_prs": 2}),
    Q("write tests for the invoice generator", {"lb_unittest": 3, "lb_py38": 1}, repo=LEGACY, languages=PY,
      forbidden=("py_pytest",)),
    Q("build the path to the config file", {"py_pathlib": 3}, languages=PY),
    Q("parse the timestamps in the bank export", {"g_timezones": 3}, repo=PAYMENTS, languages=PY),
    Q("store the api key for the new exchange rate provider", {"g_secret": 3, "n_1password": 1}, repo=PAYMENTS,
      languages=PY),
    Q("implement partial refunds",
      {"pa_money": 2, "pa_feature_flags": 2, "pa_idempotency": 2, "pa_layers": 1}, repo=PAYMENTS, languages=PY),
    Q("ci is failing on mypy", {"pa_ci": 3, "py_mypy": 2}, repo=PAYMENTS, languages=PY),
    Q("translate the settings page", {"wd_i18n": 3}, repo=DASHBOARD, languages=TS),
    Q("release a new version of the cli", {"lc_release": 3}, repo=LEDGER, languages=GO),
    Q("call the exchange rate api from async code", {"py_async": 3}, repo=PAYMENTS, languages=PY),
    Q("a test is flaky in ci", {"pa_flaky": 3, "pa_ci": 1}, repo=PAYMENTS, languages=PY),
    Q("how should I name this helper function", {"py_naming": 3}, languages=PY),
    Q("install postgres on my machine", {"g_os": 3, "n_docker_mem": 1}),
    Q("validate the json the server returns", {"ts_zod": 3}, repo=DASHBOARD, languages=TS),
)

#: Held-out queries, written before any tuning and never used to tune
#: weights or vocabulary. Report these separately: they are the honest
#: estimate of quality on phrasings the system was not shaped around.
HELDOUT_QUERIES: tuple[PersonaQuery, ...] = (
    Q("add pytest coverage for the payout scheduler",
      {"py_pytest": 3, "py_fixtures_factories": 2, "py_parametrize": 2, "pa_test_cmd": 2}, repo=PAYMENTS,
      languages=PY),
    Q("which package manager should I use to add zustand", {"ts_pnpm": 3}, repo=DASHBOARD, languages=TS,
      forbidden=("py_uv",)),
    Q("rename this variable to follow our conventions", {"py_naming": 3}, languages=PY),
    Q("the webhook endpoint received the same event again", {"pa_stripe_order": 3, "pa_idempotency": 2},
      repo=PAYMENTS, languages=PY),
    Q("bump the version and tag a release of ledger", {"lc_release": 3}, repo=LEDGER, languages=GO),
    Q("style the new modal dialog", {"ts_tailwind": 3, "wd_a11y": 1}, repo=DASHBOARD, languages=TS),
    Q("get rid of the any types in the chart component", {"ts_strict": 3}, repo=DASHBOARD, languages=TS),
    Q("how do we ship to staging", {"pa_deploy": 3}, repo=PAYMENTS, languages=PY),
    Q("convert the amount to dollars for display", {"pa_money": 3}, repo=PAYMENTS, languages=PY),
    Q("is it ok to use os.path.join here", {"py_pathlib": 3}, languages=PY),
    Q("TypeError because a default list argument is shared between calls", {"py_mutable_default": 3},
      languages=PY),
    Q("propagate cancellation through the http client", {"go_context": 3}, repo=LEDGER, languages=GO),
    Q("the dashboard calls fetch inside a component, is that fine", {"wd_api_client": 3}, repo=DASHBOARD,
      languages=TS),
    Q("squash my commits and write the message", {"g_commits_conv": 3, "g_no_force_push": 1}),
    Q("set up the dev environment on a new laptop", {"g_os": 3, "py_uv": 1, "n_1password": 1}),
)
