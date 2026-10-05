"""The MCP server's tools, called the way an MCP client calls them."""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from mnemosyne.mcp_server import INSTRUCTIONS, build_server


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "payments"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", "https://github.com/acme/payments"], check=True)
    (path / "pyproject.toml").write_text("[project]\nname='payments'\n[tool.ruff]\nline-length = 100\n")
    return path


@pytest.fixture
def server(local_backend, test_settings, repo):
    return build_server(test_settings, backend_factory=lambda: local_backend, default_cwd=str(repo))


def call(server, tool: str, **arguments) -> str:
    content, _ = asyncio.run(server.call_tool(tool, arguments))
    return "\n".join(block.text for block in content)


def test_server_advertises_tools_and_instructions(server):
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == {"get_context", "recall", "remember", "feedback", "forget", "profile", "learn_repository"}
    assert tools["recall"].annotations.readOnlyHint is True
    assert tools["forget"].annotations.destructiveHint is True
    assert "get_context" in INSTRUCTIONS and server.instructions == INSTRUCTIONS


def test_remember_recall_feedback_forget(server):
    out = call(server, "remember", statement="Use pnpm, never npm")
    assert out.startswith("Remembered: Prefers pnpm over npm")
    assert call(server, "remember", statement="Use pnpm, never npm").startswith("Already remembered")
    assert call(server, "remember", statement="use yarn instead of npm").startswith("Updated: ")

    out = call(server, "remember", statement="Amounts are integer cents", kind="fact", applies_to="this_repository")
    assert "repo:github.com/acme/payments" in out
    memory_id = out.rsplit("id ", 1)[1].rstrip(").")

    recalled = call(server, "recall", query="how are amounts stored")
    assert "Amounts are integer cents" in recalled and memory_id in recalled
    assert "confidence now" in call(server, "feedback", memory_id=memory_id, signal="helpful")
    assert call(server, "forget", memory_id=memory_id) == "Archived: Amounts are integer cents."
    assert "Amounts are integer cents" not in call(server, "recall", query="how are amounts stored")


def test_remember_scope_validation(server):
    assert call(server, "remember", statement="x y z", applies_to="language").startswith("Not stored")
    assert "lang:python" in call(server, "remember", statement="Prefer dataclasses for records",
                                 applies_to="language", language="python")


def test_learn_repository_then_context_and_profile(server):
    out = call(server, "learn_repository")
    assert out.startswith("github.com/acme/payments:") and "learned" in out
    context = call(server, "get_context", task="add a module")
    assert "maximum line length 100" in context
    assert "maximum line length 100" in call(server, "profile")
    resource = asyncio.run(server.read_resource("mnemosyne://profile"))
    assert "maximum line length 100" in next(iter(resource)).content


def test_empty_memory_answers_plainly(server):
    assert "no stored context" in call(server, "get_context", task="anything")
    assert call(server, "recall", query="anything") == "No relevant memories."


def test_tool_errors_surface_as_tool_errors(server):
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        asyncio.run(server.call_tool("feedback", {"memory_id": "not-a-uuid", "signal": "helpful"}))
