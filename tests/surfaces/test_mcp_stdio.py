"""`mnemosyne mcp` over real stdio, driven by the official MCP client the
way Claude Code or Cursor drives it."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.slow


def test_mcp_server_over_stdio(tmp_path, _isolated_home):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    repo = tmp_path / "svc"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/acme/svc"], check=True)
    env = {**os.environ, "MNEMOSYNE_HOME": str(_isolated_home), "MNEMOSYNE_HOOK_DAEMON": "0"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "mnemosyne.cli", "mcp"], env=env, cwd=str(repo))

    async def scenario() -> None:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            init = await asyncio.wait_for(session.initialize(), 30)
            assert init.serverInfo.name == "mnemosyne" and "get_context" in (init.instructions or "")
            tools = {t.name for t in (await session.list_tools()).tools}
            assert {"get_context", "recall", "remember", "feedback", "forget"} <= tools

            remembered = await session.call_tool("remember", {"statement": "Use pnpm, never npm"})
            assert not remembered.isError and "Prefers pnpm over npm" in remembered.content[0].text
            here = await session.call_tool("remember", {"statement": "Amounts are integer cents", "kind": "fact",
                                                        "applies_to": "this_repository"})
            assert "repo:github.com/acme/svc" in here.content[0].text  # cwd of the server process

            recalled = await session.call_tool("recall", {"query": "how are amounts stored"})
            assert "Amounts are integer cents" in recalled.content[0].text
            bad = await session.call_tool("feedback", {"memory_id": "nope", "signal": "helpful"})
            assert bad.isError

    asyncio.run(scenario())
