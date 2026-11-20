"""How much of ``lark-oapi`` a process with the Feishu adapter imports, and what that costs anybody else.

The SDK's package ``__init__`` imports every generated API: about 10,600
modules, two seconds of start-up and 180 MiB for an adapter that uses its
long-connection client alone. ``feishu_sdk_ws`` loads that client on its own by
deferring the package's ``__init__``. Each scenario runs in a fresh interpreter
(``tests/feishu_footprint_driver.py``), because what is imported is a fact
about a whole process:

- the adapter, run through a real connect, event, acknowledgement and stop,
  must not have imported the API tree: a stray ``import lark_oapi`` on the way,
  or an SDK release the loader no longer fits, shows up here and not in users'
  memory;
- code that imports the SDK the ordinary way, before, during or after, must
  get the whole SDK and never a stand-in.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]

# The client and what it imports come to 64 modules with lark-oapi 1.7.5.
SDK_MODULE_CEILING = 120
# The whole SDK is about 10,600.
WHOLE_SDK_FLOOR = 5000


def _run(scenario: str) -> Dict[str, Any]:
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
    assert "importing the whole SDK instead" not in done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_adapter_runs_on_the_sdk_client_alone():
    report = _run("adapter")

    # It worked: the event arrived, Feishu was answered, the adapter stopped.
    assert report["received_chat"] == "oc_chat"
    assert report["acknowledged"] == {"code": 200}
    assert report["adapter_stopped"] is True
    # And it did so without the API tree.
    assert report["sdk_api_modules"] == []
    assert 0 < report["sdk_modules"] <= SDK_MODULE_CEILING


def test_an_ordinary_import_at_the_same_moment_gets_the_whole_sdk():
    report = _run("beside-an-ordinary-import")

    assert report.pop("sdk_modules") > WHOLE_SDK_FLOOR
    assert report == {
        "client_alone_has_client": True,
        "ordinary_import_has_client": True,
        "ordinary_import_builds_a_dispatcher": "EventDispatcherHandler",
        "package_knows_its_children": True,
        "dispatcher_is_the_real_class": True,
        "client_module_is_the_package_s": True,
    }


def test_an_ordinary_import_afterwards_gets_the_whole_sdk_when_it_uses_it():
    report = _run("alone-then-ordinary")

    assert report["sdk_modules_after_the_client_alone"] <= SDK_MODULE_CEILING
    # Importing a submodule costs that submodule; using the package costs the SDK.
    assert report["package_knows_core"] is True
    assert report["sdk_modules_after_a_submodule"] <= SDK_MODULE_CEILING
    assert report["has_client"] is True
    assert report["sdk_modules_after_using_the_package"] > WHOLE_SDK_FLOOR
    assert report["client_module_is_the_package_s"] is True
