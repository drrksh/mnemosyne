"""`mnemosyne demo` and `mnemosyne eval` — see it work, and measure it."""
from __future__ import annotations

import click

from mnemosyne.cli.common import bold, dim, echo_json

DEMO_TASKS = (
    ("github.com/acme/payments-api", ("python",), "add an endpoint to refund a payment"),
    ("github.com/acme/web-dashboard", ("typescript",), "write tests for the invoice table component"),
    ("github.com/acme/ledger-cli", ("go",), "how should errors be handled here?"),
)


@click.command()
@click.option("--max-tokens", default=900, show_default=True)
def demo(max_tokens: int) -> None:
    """Brief an assistant on three tasks for a sample developer (in-memory; touches nothing).

    The sample developer has 100+ memories across four repositories. Note
    how each briefing holds only what applies to *that* repository and task.
    """
    from mnemosyne.config import Settings
    from mnemosyne.container import Mnemosyne
    from mnemosyne.domain.working_context import WorkingContext
    from mnemosyne.evaluation.harness import seed
    from mnemosyne.services.context_service import ContextRequest

    settings = Settings(database_url="sqlite:///:memory:", app_env="testing", log_level="WARNING")
    with Mnemosyne(settings, schema="create") as container:
        count = len(seed(container))
        click.echo(dim(f"Sample developer: {count} memories across 4 repositories.\n"))
        for repo, languages, task in DEMO_TASKS:
            pack = container.context.compile(ContextRequest(
                task=task, context=WorkingContext.create(repo_id=repo, languages=list(languages)),
                max_tokens=max_tokens, record_access=False,
            ))
            click.echo(bold(f"▶ {repo}: “{task}”"))
            click.echo(pack.text)
            click.echo(dim(f"~{pack.estimated_tokens} tokens · {len(pack.items)} of {count} memories · "
                           f"{pack.took_ms:.0f} ms\n"))


@click.command(name="eval")
@click.option("--variant", "variants", multiple=True, help="Retrieval variants to compare (default: all).")
@click.option("--heldout", is_flag=True, help="Use the held-out query set.")
@click.option("--context", "context_budget", type=int, default=None,
              help="Evaluate compiled context at this token budget instead of ranking.")
@click.option("--json", "as_json", is_flag=True)
def evaluate(variants: tuple[str, ...], heldout: bool, context_budget: int | None, as_json: bool) -> None:
    """Measure retrieval quality on the labeled evaluation persona."""
    from mnemosyne.evaluation import harness
    from mnemosyne.evaluation.persona import HELDOUT_QUERIES, QUERIES

    queries = HELDOUT_QUERIES if heldout else QUERIES
    if context_budget:
        result = harness.evaluate_context(max_tokens=context_budget, queries=queries)
        if as_json:
            echo_json(result.summary())
        else:
            for key, value in result.summary().items():
                click.echo(f"{key:<20} {value}")
        return
    reports = [harness.evaluate(v, queries=queries) for v in (variants or tuple(harness.VARIANTS))]
    if as_json:
        echo_json({r.variant: r.summary() for r in reports})
    else:
        click.echo(harness.format_report(reports))
