"""MCP frontend for broker-owned, chat-authenticated private connections."""
import os
import sys
import asyncio
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp.server.fastmcp import FastMCP
import connection_tools

# Tool validation may contain arguments; keep framework diagnostics from logging secrets.
logging.getLogger("mcp").setLevel(logging.CRITICAL)

mcp = FastMCP("AntigravityIntercom")
connection_tools.install(mcp)


def _start_background_listener():
    # Only the user-managed broker starts listeners.
    return


if __name__ == "__main__":
    # Keep original functions as the backend; replace only the STDIO tool table.
    # Signature/doc preservation retains existing MCP schemas and approval names.
    import asyncio
    import functools
    import inspect
    import broker_rpc

    context = broker_rpc.client_context()
    for tool in asyncio.run(mcp.list_tools()):
        original = getattr(connection_tools, tool.name)

        def make_proxy(fn, name):
            @functools.wraps(fn)
            def proxy(*args, **kwargs):
                bound = inspect.signature(fn).bind(*args, **kwargs)
                bound.apply_defaults()
                return broker_rpc.request("call", context=context, name=name, arguments=dict(bound.arguments))
            proxy.__signature__ = inspect.signature(fn)
            return proxy

        mcp.remove_tool(tool.name)
        mcp.add_tool(make_proxy(original, tool.name), name=tool.name,
                     description=tool.description, annotations=tool.annotations)
    mcp.run()

