"""
MCP server: Mnemosyne as tools any MCP client (Claude Code, Cursor,
Windsurf, Claude Desktop...) can call.

Hooks push context into Claude Code automatically; MCP lets the
assistant *pull* — recall something specific mid-task, record a
preference the moment the developer states it, or correct a memory that
turned out to be wrong. Every tool goes through the configured backend,
so the same server works on the developer's local memory or against a
team deployment.

The server writes nothing to stdout except protocol messages.
"""
from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

if TYPE_CHECKING:  # pragma: no cover
    from mnemosyne.client.backend import Backend
    from mnemosyne.config import Settings

INSTRUCTIONS = """\
Mnemosyne is the developer's long-term engineering memory: their conventions, preferences, tooling, past \
decisions and lessons, learned from their code, git history and earlier sessions.

- Before starting non-trivial work, call `get_context` with the task, unless a <mnemosyne-context> block for \
this task is already in the conversation.
- Use `recall` to look up something specific (how they handle errors, which test framework, why a decision \
was made) instead of asking the developer to repeat themselves.
- When the developer states a lasting preference, convention or decision ("always...", "we use...", "don't..."), \
call `remember` with one self-contained sentence. Do not store task-specific instructions or secrets.
- When a memory turns out to be wrong or outdated, call `feedback`; when one was useful, `feedback` with \
"helpful". Explicit statements by the developer always override what Mnemosyne inferred.
"""

MemoryKind = Literal[
    "rule", "preference", "convention", "pattern", "anti_pattern", "tooling", "decision", "architecture",
    "procedure", "failure", "fact", "skill",
]
Signal = Literal["helpful", "not_helpful", "confirm", "wrong", "outdated"]


def _format_memory(memory: dict[str, Any], score: Optional[float] = None) -> str:
    meta = [memory["memory_type"], memory["scope"]]
    if memory.get("key"):
        meta.append(f"{memory['key']}={memory.get('value')}")
    meta.append(f"confidence {memory['confidence_score']:.2f}")
    if score is not None:
        meta.append(f"score {score:.2f}")
    line = f"- {memory['title']} ({'; '.join(meta)}; id {memory['id']})"
    content = (memory.get("content") or "").strip()
    if content and content != memory["title"]:
        line += "\n  " + content[:600].replace("\n", "\n  ")
    return line


class MnemosyneMCP(FastMCP[Any]):
    """FastMCP that owns the backend its tools use, and closes it."""

    def __init__(self, *, close_backend: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.close_backend = close_backend


def build_server(settings: Settings, backend_factory: Optional[Callable[[], Backend]] = None,
                 *, default_cwd: Optional[str] = None) -> MnemosyneMCP:
    from mnemosyne.client.backend import open_backend

    factory = backend_factory or (lambda: open_backend(settings))
    lock = threading.Lock()
    holder: dict[str, Backend] = {}

    def backend() -> Backend:
        with lock:
            if "b" not in holder:
                holder["b"] = factory()
            return holder["b"]

    def where(cwd: Optional[str]) -> str:
        return cwd or default_cwd or os.getcwd()

    def close() -> None:
        if "b" in holder:
            holder.pop("b").close()

    server = MnemosyneMCP(name="mnemosyne", instructions=INSTRUCTIONS, log_level="WARNING", close_backend=close)

    @server.tool(annotations=ToolAnnotations(title="Get context for a task", readOnlyHint=True, openWorldHint=False))
    def get_context(task: str, cwd: Optional[str] = None, file_path: Optional[str] = None,
                    max_tokens: int = 1200) -> str:
        """Briefing for a task: the developer's relevant conventions, preferences, decisions and lessons
        for this repository. `cwd` is the project directory (defaults to the server's working directory)."""
        result = backend().context(task=task, cwd=where(cwd), file_path=file_path,
                                   max_tokens=max(200, min(max_tokens, 6000)), include_profile=True)
        return result.text if result.items else "Mnemosyne has no stored context relevant to this task yet."

    @server.tool(annotations=ToolAnnotations(title="Recall memories", readOnlyHint=True, openWorldHint=False))
    def recall(query: str, cwd: Optional[str] = None, limit: int = 8) -> str:
        """Search the developer's memory. Results are ranked for the current repository and include ids
        for `feedback` and `forget`."""
        items = backend().recall(query, cwd=where(cwd), limit=max(1, min(limit, 25)))
        if not items:
            return "No relevant memories."
        return "\n".join(_format_memory(i["memory"], i["score"]) for i in items)

    @server.tool(annotations=ToolAnnotations(title="Remember", readOnlyHint=False, destructiveHint=False,
                                             idempotentHint=True, openWorldHint=False))
    def remember(statement: str, kind: Optional[MemoryKind] = None,
                 applies_to: Literal["auto", "everywhere", "this_repository", "language"] = "auto",
                 language: Optional[str] = None, cwd: Optional[str] = None) -> str:
        """Store something the developer wants remembered across sessions: one self-contained sentence,
        e.g. "Use pnpm, never npm" or "Money is always integer cents". A newer statement about the same
        setting replaces the older one. Never store secrets or one-off task instructions."""
        from mnemosyne.client.explicit import ALREADY_RECORDED, explicit_candidate
        from mnemosyne.domain.enums import MemoryType
        from mnemosyne.learning.workspace import detect_working_context

        context = detect_working_context(where(cwd))
        scope = None
        if applies_to == "everywhere":
            scope = "global"
        elif applies_to == "this_repository":
            if not context.repo_id:
                return "Not stored: no git repository detected for this directory; use applies_to='everywhere'."
            scope = f"repo:{context.repo_id}"
        elif applies_to == "language":
            if not language:
                return "Not stored: applies_to='language' needs `language` (e.g. 'python')."
            scope = f"lang:{language}"
        candidate = explicit_candidate(statement, context=context, memory_type=MemoryType(kind) if kind else None,
                                       scope=scope, source="mcp")
        report = backend().ingest([candidate], source="mcp:remember")
        outcome = report.outcomes[0] if report.outcomes else None
        if outcome is not None and outcome["action"] == "skipped" and outcome["reason"] == ALREADY_RECORDED:
            return f"Already remembered: {outcome['title']}."
        if outcome is None or outcome["action"] == "skipped":
            return f"Not stored: {outcome['reason'] if outcome else 'unknown reason'}."
        if outcome["action"] == "superseded":
            return (f"Updated: {outcome['title']} (replaces {outcome['key']}={outcome['previous_value']}; "
                    f"id {outcome['memory_id']}).")
        if outcome["action"] == "created":
            kind_scope = f"{candidate.memory_type.value}, {candidate.scope}"
            return f"Remembered: {outcome['title']} ({kind_scope}; id {outcome['memory_id']})."
        return f"Already known; reinforced: {outcome['title']} (id {outcome['memory_id']})."

    @server.tool(annotations=ToolAnnotations(title="Give feedback on a memory", readOnlyHint=False,
                                             destructiveHint=False, openWorldHint=False))
    def feedback(memory_id: str, signal: Signal, note: Optional[str] = None) -> str:
        """Tell Mnemosyne whether a memory helped. `wrong` or `outdated` retire it quickly; `confirm`
        makes an inferred memory authoritative."""
        memory = backend().feedback(memory_id, signal, note)
        return f"{memory['title']}: confidence now {memory['confidence_score']:.2f} ({memory['status']})."

    @server.tool(annotations=ToolAnnotations(title="Forget a memory", readOnlyHint=False, destructiveHint=True,
                                             idempotentHint=True, openWorldHint=False))
    def forget(memory_id: str) -> str:
        """Archive a memory the developer asked to forget (it stops being used; it can be restored)."""
        memory = backend().get(memory_id)
        backend().forget(memory_id)
        return f"Archived: {memory['title']}."

    @server.tool(annotations=ToolAnnotations(title="Developer profile", readOnlyHint=True, openWorldHint=False))
    def profile(cwd: Optional[str] = None) -> str:
        """The developer's standing profile for this repository: rules, preferences, conventions, tooling."""
        result = backend().context(task=None, cwd=where(cwd), max_tokens=3000, include_profile=True,
                                   record_access=False)
        return result.text if result.items else "No profile yet."

    @server.tool(annotations=ToolAnnotations(title="Learn a repository", readOnlyHint=False, destructiveHint=False,
                                             idempotentHint=True, openWorldHint=False))
    def learn_repository(path: Optional[str] = None) -> str:
        """Analyse a repository on this machine (code style, tooling, structure, the developer's commit
        habits) and learn its conventions. Re-running only records what changed."""
        result = backend().learn_repository(where(path))
        lines = [f"{result.repo_id}: {len(result.findings)} findings in {result.duration_ms:.0f} ms."]
        if result.report:
            lines.append(result.report.summary)
            lines += [f"- {o['title']}" for o in result.report.learned][:30]
        return "\n".join(lines)

    @server.resource("mnemosyne://profile", name="profile", mime_type="text/markdown",
                     description="The developer's profile for the server's working directory.")
    def profile_resource() -> str:
        result = backend().context(task=None, cwd=where(None), max_tokens=3000, include_profile=True,
                                   record_access=False)
        return result.text or "No profile yet."

    return server


def run_stdio(settings: Settings) -> None:  # pragma: no cover - exercised end-to-end
    server = build_server(settings)
    try:
        server.run("stdio")
    finally:
        server.close_backend()
