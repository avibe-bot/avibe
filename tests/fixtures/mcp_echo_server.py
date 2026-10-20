"""Loopback MCP test server: ``python mcp_echo_server.py [stdio|http PORT]``."""

import sys

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


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "http":
        server.run("streamable-http", host="127.0.0.1", port=int(sys.argv[2]))
    else:
        server.run("stdio")
