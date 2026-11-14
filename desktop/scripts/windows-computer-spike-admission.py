"""Fixed-origin, read-only stable-release admission. No token enters GUI work."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise RuntimeError("GitHub redirect refused")


def main():
    token = os.environ.pop("GH_TOKEN")
    if os.environ.get("GITHUB_REPOSITORY") != "avibe-bot/avibe":
        raise RuntimeError("Repository mismatch")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if head != os.environ["ACCEPTANCE_SOURCE_SHA"]:
        raise RuntimeError("Source SHA mismatch")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    observed = []
    for workflow in ("publish.yml", "release_ai.yml", "desktop-release.yml"):
        # The tag filter bounds all histories to the one stable release.
        url = f"https://api.github.com/repos/avibe-bot/avibe/actions/workflows/{workflow}/runs?branch=v3.2.6&per_page=100"
        request = Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        with opener.open(request, timeout=30) as response:
            data = json.load(response)
        runs = data["workflow_runs"]
        if data["total_count"] > 100:
            raise RuntimeError("Stable inventory truncated")
        observed += [{"workflow": workflow, "id": r["id"], "status": r["status"]} for r in runs]
        if any(r["status"] != "completed" for r in runs):
            raise RuntimeError("Stable release active; yielding")
    out = Path(os.environ["RUNNER_TEMP"]) / "computer-use-spike/evidence"
    out.mkdir(parents=True, exist_ok=True)
    (out / "admission.json").write_text(json.dumps({"source": head, "runs": observed}), encoding="utf-8")


if __name__ == "__main__":
    main()
