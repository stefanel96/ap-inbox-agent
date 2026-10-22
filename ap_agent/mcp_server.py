"""MCP server: exposes the read-only inbox tools to Hermes (or any MCP client) over stdio.

Run (normally started by Hermes): python -m ap_agent.mcp_server
"""
import functools
import json

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import tools

server = MCPServer(
    "ap_inbox",
    instructions="Read-only tools over a processed accounts payable inbox. Cite message ids; report unsure, "
                 "missing and unreadable items; never invent numbers.",
)
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _register(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return json.dumps(fn(*args, **kwargs), default=str, ensure_ascii=False)
        except Exception as e:  # report the error to the agent instead of hiding it
            return json.dumps({"error": f"{type(e).__name__}: {e}"})
    server.add_tool(wrapper, name=fn.__name__, description=fn.__doc__, annotations=READ_ONLY)


for _fn in tools.TOOLS:
    _register(_fn)


if __name__ == "__main__":
    server.run("stdio")
