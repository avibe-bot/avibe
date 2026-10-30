"""Loopback MCP test server: ``python mcp_echo_server.py [stdio|http PORT]``."""

import os
import sys
import time

from mcp.server.mcpserver import MCPServer

server = MCPServer("avibe-echo", version="0.1.0")


@server.tool(description="Echo text back, optionally repeated.")
def echo(text: str, repeat: int = 1) -> str:
    return text * repeat


@server.tool(description="Add two numbers.")
def add(a: float, b: float) -> dict:
    return {"sum": a + b}


@server.tool(description="Always fails.")
def fail(reason: str) -> str:
    raise ValueError(reason)


@server.tool(description="Reject with a JSON-RPC error.")
def reject() -> str:
    from mcp import MCPError

    raise MCPError(-32602, "rejected by server")


@server.tool(description="Return a tool error containing a session phrase.")
def session_text() -> str:
    from mcp import MCPError

    raise MCPError(-32602, "browser session not found")


@server.tool(description="Exit the server process mid-call.")
def crash() -> str:
    os._exit(1)


@server.tool(description="Sleep for some seconds.")
def nap(seconds: float) -> str:
    time.sleep(seconds)
    return "awake"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "http":
        server.run("streamable-http", host="127.0.0.1", port=int(sys.argv[2]))
    else:
        server.run("stdio")
