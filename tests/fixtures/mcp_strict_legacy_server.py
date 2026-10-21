"""Stdio MCP server that, like strict pre-2026 servers (rmcp), exits on ``server/discover``.

Proxies every other line to ``mcp_echo_server.py``.
"""

import json
import subprocess
import sys
import threading
from pathlib import Path

child = subprocess.Popen(
    [sys.executable, str(Path(__file__).with_name("mcp_echo_server.py"))],
    stdin=subprocess.PIPE,
    stdout=sys.stdout.buffer,
)


def _pump() -> None:
    for line in sys.stdin.buffer:
        try:
            method = json.loads(line).get("method")
        except ValueError:
            method = None
        if method == "server/discover":
            sys.stderr.write("expect initialized request, but received server/discover\n")
            child.kill()
            sys.exit(1)
        child.stdin.write(line)
        child.stdin.flush()
    child.stdin.close()


thread = threading.Thread(target=_pump, daemon=True)
thread.start()
thread.join()
sys.exit(child.wait())
