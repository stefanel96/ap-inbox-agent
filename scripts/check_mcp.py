"""Smoke test: start the MCP server the way Hermes does, list its tools and call a few.

Run: python scripts/check_mcp.py
"""
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[1]


async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "ap_agent.mcp_server"], cwd=str(REPO))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print(f"{len(tools)} tools:", ", ".join(t.name for t in tools))
            for name, args in (("inbox_overview", {}), ("search_emails", {"chase_level": "final_notice", "limit": 3}),
                               ("run_sql", {"query": "DELETE FROM emails"}), ("cost_report", {})):
                res = await session.call_tool(name, args)
                text = res.content[0].text
                print(f"\n--- {name}({args}) -> {len(text)} chars")
                print(json.dumps(json.loads(text), indent=1)[:700])


if __name__ == "__main__":
    asyncio.run(main())
