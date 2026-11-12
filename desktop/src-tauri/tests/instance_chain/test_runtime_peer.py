"""An isolated second R1 Runtime process used only by the native chain."""

import json
import os
from pathlib import Path
import sys

import pytest
from tests.test_desktop_local_instance import desktop_host as desktop_host
from tests.test_pairing_recovery import pairing_host as pairing_host
from vibe import desktop_local_instance
import runtime as runtime_fixture


def test_runtime_peer_server(desktop_host, monkeypatch):
    if "DESKTOP_RUNTIME_PEER" not in os.environ:
        pytest.skip("explicit native chain process only")
    config = json.loads(os.environ["DESKTOP_RUNTIME_PEER"])
    monkeypatch.setattr(desktop_local_instance.socket, "gethostname", lambda: "工作室 Studio")
    server, local = runtime_fixture.start(monkeypatch, config["producer"], Path(config["mappings"]))
    print(json.dumps({"event": "peer_ready", "local": local}), flush=True)
    try:
        for line in sys.stdin:
            if line.strip() == "stop":
                break
    finally:
        server.shutdown()
