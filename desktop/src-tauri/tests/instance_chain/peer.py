"""Bootstrap a second real Runtime and pair it through the combined producer."""

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import uuid

import requests


def start(root, producer, mappings):
    root.mkdir()
    environment = {key: os.environ[key] for key in ("PATH", "PYTHONPATH", "DESKTOP_R1_HEAD", "DESKTOP_RUNTIME_UI")}
    for key in ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        path = root / key.lower()
        path.mkdir()
        environment[key] = str(path)
    environment["DESKTOP_RUNTIME_PEER"] = json.dumps({"producer": producer, "mappings": str(mappings)})
    file = Path(__file__).with_name("test_runtime_peer.py")
    process = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-p", "tests.conftest", "--rootdir", str(Path.cwd()), str(file), "-qs"],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    ready = queue.Queue()
    log = (root / "peer.log").open("w")

    def collect():
        for line in process.stdout:
            log.write(line)
            log.flush()
            try:
                value = json.loads(line)
                if value.get("event") == "peer_ready":
                    ready.put(value)
            except (ValueError, AttributeError):
                pass

    threading.Thread(target=collect, daemon=True).start()
    try:
        local = ready.get(timeout=45)["local"]
        # Establish a separate real B1 jar in this same combined B1/B2 server.
        session = requests.Session()
        session.trust_env = False
        credentials = session.post(
            producer["provider"] + "/auth/v1/verify", json={"token": "123456"}, timeout=10
        ).json()
        account = session.post(
            producer["base"] + "/api/desktop/session",
            json={key: credentials[key] for key in ("access_token", "refresh_token")},
            headers={"Origin": "https://avibe.bot", "Host": "avibe.bot"},
            timeout=10,
        )
        assert account.status_code == 200, account.text
        cookie = "; ".join(f"{cookie.name}={cookie.value}" for cookie in session.cookies)
        client = requests.Session()
        client.trust_env = False
        descriptor = client.get(local + "/api/desktop/local-instance", timeout=10).json()
        csrf = client.get(local + "/api/csrf-token", timeout=10).json()["csrf_token"]
        headers = {"Origin": local, "X-Vibe-CSRF-Token": csrf}
        prepared = client.post(
            local + "/api/desktop/local-instance/prepare",
            json={
                "account_id": account.json()["account"]["id"],
                "expected_binding_revision": descriptor["binding_revision"],
            },
            headers=headers,
            timeout=10,
        )
        assert prepared.status_code == 200, prepared.text
        prepared = prepared.json()
        descriptor = prepared["local"]
        grant = session.post(
            producer["base"] + "/api/desktop/instances/ensure-local",
            json={
                "installation_id": descriptor["installation_id"],
                "operation_id": prepared["operation_id"],
                "device_name": descriptor["device_name"],
                "local_instance_id": None,
            },
            headers={"Origin": "https://avibe.bot", "Host": "avibe.bot", "Cookie": cookie},
            timeout=20,
        )
        assert grant.status_code == 200, grant.text
        grant = grant.json()
        paired = client.post(
            local + "/api/remote-access/vibe-cloud/pair",
            json={
                "pairing_key": grant["pairing"]["pairing_key"],
                "backend_url": "https://avibe.bot",
                "device_name": descriptor["device_name"],
                "desktop_enrollment": {
                    "installation_id": descriptor["installation_id"],
                    "operation_id": prepared["operation_id"],
                    "account_id": account.json()["account"]["id"],
                    "expected_binding_revision": descriptor["binding_revision"],
                },
            },
            headers=headers,
            timeout=20,
        )
        assert paired.status_code == 200, paired.text
        return process, log, grant["instance"]
    except BaseException:
        process.terminate()
        process.wait(timeout=10)
        log.close()
        raise
