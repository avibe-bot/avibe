"""How much of ``lark-oapi`` a process with the Feishu adapter imports.

The SDK's package ``__init__`` imports every generated API: about 10,600
modules, two seconds of start-up and 180 MiB for an adapter that uses its
long-connection client alone. ``feishu_sdk_ws`` loads that client on its own.
This test runs the adapter through a real connect, event, acknowledgement and
stop in a fresh interpreter and counts what was imported, so that a stray
``import lark_oapi`` anywhere on the way, or an SDK release the loader no
longer fits, shows up here and not in users' memory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The client and what it imports come to 62 modules with lark-oapi 1.7.5.
SDK_MODULE_CEILING = 120


def test_the_adapter_runs_on_the_sdk_client_alone():
    environment = {**os.environ, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    done = subprocess.run(
        [sys.executable, "-m", "tests.feishu_footprint_driver"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    report = json.loads(done.stdout.strip().splitlines()[-1])

    # It worked: the event arrived, Feishu was answered, the adapter stopped.
    assert report["received_chat"] == "oc_chat"
    assert report["acknowledged"] == {"code": 200}
    assert report["adapter_stopped"] is True
    # Four threads that asked for the client at once, one from inside a running loop, each got it whole.
    assert report["concurrent_loads_complete"] == [True] * 4
    # And it did so without the API tree.
    assert report["sdk_api_modules"] == []
    assert report["sdk_package_loaded"] is False
    assert 0 < report["sdk_modules"] <= SDK_MODULE_CEILING
    assert "importing the whole SDK instead" not in done.stderr
