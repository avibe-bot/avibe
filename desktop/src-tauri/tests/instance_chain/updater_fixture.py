"""Existing signed updater test assets served only on a mapped fixture socket."""

import http.server
import json
from pathlib import Path
import platform
import threading


def start(root: Path):
    target = "aarch64-apple-darwin" if platform.machine() == "arm64" else "x86_64-apple-darwin"
    fixtures = Path(__file__).resolve().parents[3] / "runtime-host/tests/fixtures/updater"
    raw = (fixtures / f"stable-{target}.json").read_bytes()
    manifest = json.loads(raw)
    name = f"desktop-update-{target}.json"
    artifact = next(iter(manifest["platforms"].values()))
    artifact_name = artifact["url"].rsplit("/", 1)[-1]
    index = {
        "schema_version": 1,
        "repository": "avibe-bot/avibe",
        "releases": [{
            "tag": manifest["tag"], "prerelease": False, "commit": manifest["source_sha"],
            "assets": [{"name": value} for value in (name, name + ".sig", artifact_name)],
        }],
    }
    prefix = "/releases/" + manifest["tag"] + "/"
    files = {
        "/index/releases.json": json.dumps(index).encode(),
        prefix + name: raw,
        prefix + name + ".sig": (fixtures / f"stable-{target}.json.sig").read_bytes(),
        prefix + artifact_name: (fixtures / "artifact.bin").read_bytes(),
    }
    requested = threading.Event()
    released = threading.Event()
    calls = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            calls.append(self.path)
            with (root / "updater-http.log").open("a") as log:
                log.write(self.path + "\n")
            if self.path not in files:
                self.send_error(404)
                return
            if self.path == prefix + artifact_name:
                requested.set()
                if not released.wait(timeout=25):
                    self.send_error(504)
                    return
            body = files[self.path]
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return {
        "server": server, "base": f"http://127.0.0.1:{server.server_port}",
        "requested": requested, "release": released, "calls": calls,
    }
