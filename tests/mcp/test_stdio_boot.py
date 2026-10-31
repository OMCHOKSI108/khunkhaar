"""Boot every KHUNKHAAR MCP server over stdio and exercise it through the real MCP protocol.

The in-process suites call tool functions directly; this suite proves the same servers start
as subprocesses, complete the MCP handshake, advertise their tools, and reject invalid input
with a structured error - exactly what OpenCode does at launch.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.mcp

ROOT = Path(__file__).resolve().parents[2]

SERVERS = {
    "khunkhaar-browser": (ROOT / "mcp-servers" / "browser" / "server.py",
                          {"browser_open", "browser_navigate", "browser_a11y_scan", "browser_close"}),
    "khunkhaar-api": (ROOT / "mcp-servers" / "api" / "server.py",
                      {"api_request", "api_policy", "api_discover_openapi"}),
    "khunkhaar-database": (ROOT / "mcp-servers" / "database" / "server.py",
                           {"db_profiles", "db_query", "db_describe_table", "db_diff"}),
    "khunkhaar-filesystem": (ROOT / "mcp-servers" / "filesystem" / "server.py",
                             {"fs_read", "fs_list", "fs_storage_report"}),
}


def _params(path: Path) -> StdioServerParameters:
    env = {**os.environ, "KHUNKHAAR_ROOT": str(ROOT)}
    env.setdefault("PATH", os.environ.get("PATH", ""))
    return StdioServerParameters(command=sys.executable, args=[str(path)], env=env, cwd=str(ROOT))


@pytest.mark.parametrize("name", sorted(SERVERS))
async def test_server_boots_lists_tools_and_rejects_invalid_input(name):
    path, expected = SERVERS[name]

    async def flow() -> None:
        async with stdio_client(_params(path)) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.server_info.name == name, init.server_info

                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert expected <= names, f"{name} missing {expected - names}"

                for tool in listed.tools:
                    assert tool.description, f"{tool.name} has no description"
                    assert tool.input_schema.get("type") == "object", tool.name

                # Missing required arguments must come back as a tool error, not a crash.
                strict = [t.name for t in listed.tools if t.input_schema.get("required")]
                assert strict, f"{name} exposes no tool with required arguments"
                bad = await session.call_tool(strict[0], {})
                assert bad.is_error, f"{strict[0]} accepted empty arguments: {bad}"

                # A tool with no required arguments must still answer after the bad call.
                easy = next(t.name for t in listed.tools if not t.input_schema.get("required"))
                good = await session.call_tool(easy, {})
                assert not good.is_error, f"{easy} failed on the smoke call: {good}"

    await asyncio.wait_for(flow(), timeout=120)
