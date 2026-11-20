"""How much of ``lark-oapi`` a process with the Feishu adapter imports, and what that costs anybody else.

The SDK's package ``__init__`` imports every generated API: about 10,600
modules, two seconds of start-up and 180 MiB for an adapter that uses its
long-connection client alone. ``feishu_sdk_ws`` loads a private copy of that
client instead. Each scenario runs in a fresh interpreter
(``tests/feishu_footprint_driver.py``), because what is imported is a fact
about a whole process:

- the adapter, run through a real connect, event, acknowledgement and stop,
  must not have imported the SDK under its own name, nor the API tree into its
  copy: a stray ``import lark_oapi`` on the way, or an SDK release the copy no
  longer fits, shows up here and not in users' memory;
- code that imports the SDK the ordinary way, at the same moment, gets the SDK,
  whole and its own.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

ROOT = Path(__file__).resolve().parents[1]

# The client and what it imports come to 65 modules with lark-oapi 1.7.5.
SDK_MODULE_CEILING = 120
# The whole SDK is about 10,600.
WHOLE_SDK_FLOOR = 5000


def _run(scenario: str) -> Tuple[Dict[str, Any], str]:
    environment = {**os.environ, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    done = subprocess.run(
        [sys.executable, "-m", "tests.feishu_footprint_driver", scenario],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1]), done.stderr


def test_the_adapter_runs_on_its_own_copy_of_the_sdk_client():
    report, stderr = _run("adapter")
    assert "importing the whole SDK instead" not in stderr

    # It worked: the event arrived, Feishu was answered, the adapter stopped.
    assert report["received_chat"] == "oc_chat"
    assert report["acknowledged"] == {"code": 200}
    assert report["adapter_stopped"] is True
    # And it did so without the SDK under its own name, and without the API tree.
    assert report["sdk_modules_under_its_own_name"] == 0
    assert report["api_modules_in_our_copy"] == []
    assert 0 < report["sdk_modules_in_our_copy"] <= SDK_MODULE_CEILING


def test_an_ordinary_import_at_the_same_moment_gets_the_sdk_whole_and_its_own():
    report, stderr = _run("beside-an-ordinary-import")
    assert "importing the whole SDK instead" not in stderr

    assert report.pop("sdk_modules_under_its_own_name") > WHOLE_SDK_FLOOR
    assert 0 < report.pop("sdk_modules_in_our_copy") <= SDK_MODULE_CEILING
    assert report == {
        "our_copy_has_client": True,
        "ordinary_import_has_client": True,
        "ordinary_import_builds_a_dispatcher": "EventDispatcherHandler",
        "both_finished": True,
        "the_sdk_s_client_module_is_its_own": True,
        "the_sdk_s_client_names_the_real_dispatcher": True,
    }


def test_an_install_the_copy_cannot_be_made_from_gets_the_whole_sdk_and_says_so():
    """A module without a source file cannot be renamed; loading it as it is would bring in the SDK unannounced."""
    report, _stderr = _run("without-source-files")

    assert report == {
        "module": "lark_oapi.ws.client",
        "warned": True,
        "has_client": True,
        "names_the_real_dispatcher": "lark_oapi.event.dispatcher_handler",
    }
