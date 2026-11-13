"""Read-only public API admission. No token and no actions:read permission.

Any active Desktop Stage 2 run, unexpected response or rate limit blocks this
acceptance. This observation is not a lock against a later release dispatch.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

ACTIVE = ("in_progress", "queued", "requested", "waiting", "pending")
REPOSITORY = "avibe-bot/avibe"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise RuntimeError("GitHub API redirect refused")


def check() -> dict:
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise RuntimeError("Only the public product repository is admitted")
    source = os.environ["ACCEPTANCE_SOURCE_SHA"]
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if actual != source:
        raise RuntimeError("Checkout is not the exact event source")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    name = os.environ["GITHUB_EVENT_NAME"]
    if name == "pull_request":
        pr = event["pull_request"]
        if not (event["action"] == "labeled" and event["label"]["name"] == "native-update-acceptance"
                and pr["base"]["ref"] == "master" and pr["head"]["repo"]["full_name"] == REPOSITORY
                and pr["head"]["sha"] == source):
            raise RuntimeError("PR label admission mismatch")
    elif name != "workflow_dispatch":
        raise RuntimeError("Unsupported acceptance trigger")
    evidence = {
        "repository": REPOSITORY, "event": name, "source_sha": source,
        "event_sha": os.environ["ACCEPTANCE_EVENT_SHA"],
        "workflow_sha": os.environ["ACCEPTANCE_WORKFLOW_SHA"],
        "workflow_ref": os.environ["ACCEPTANCE_WORKFLOW_REF"],
        "observed_at": datetime.now(timezone.utc).isoformat(), "active_counts": {},
    }
    opener = build_opener(ProxyHandler({}), NoRedirect())
    for status in ACTIVE:
        url = (f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/"
               f"desktop-release.yml/runs?status={status}&per_page=1")
        request = Request(url, headers={"Accept": "application/vnd.github+json",
                                        "User-Agent": "Avibe-native-acceptance-admission",
                                        "X-GitHub-Api-Version": "2022-11-28"})
        with opener.open(request, timeout=20) as response:
            body = json.loads(response.read(2_000_000))
        count = body.get("total_count")
        runs = body.get("workflow_runs")
        if type(count) is not int or count < 0 or not isinstance(runs, list):
            raise RuntimeError("Unreadable Stage 2 status")
        evidence["active_counts"][status] = count
        if count or runs:
            raise RuntimeError(f"Desktop Stage 2 active: {status} ({count}); no acceptance started")
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = {"ok": True, **check()}
    except Exception as error:
        args.output.write_text(json.dumps({"ok": False, "error": str(error)}, indent=2) + "\n", encoding="utf-8")
        raise
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
