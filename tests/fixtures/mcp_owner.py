"""Child test fixture: serve the real control IPC app with only the MCP runtime.

Run with a hermetic ``AVIBE_HOME``; the CLI then reaches this owner exactly as it
reaches the Controller.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.internal_server import serve  # noqa: E402
from core.mcp_hub.runtime import McpRuntime  # noqa: E402


async def main() -> None:
    controller = SimpleNamespace(mcp_runtime=McpRuntime())
    try:
        await serve(controller)
    finally:
        controller.mcp_runtime.stop()


if __name__ == "__main__":
    asyncio.run(main())
