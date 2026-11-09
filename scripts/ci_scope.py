#!/usr/bin/env python3
"""One scope contract for CI producers, required gates, and scenario evidence.

PR always runs the cheap Linux checks; only proven unrelated paths omit heavy
jobs. MG_THIN checks the exact combination, not PR test results. MASTER_FULL
runs every producer. A skip is a declared disposition, never a passing test.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.ci_docs_only import Scope, classify, narrative_path, well_formed

MODES = ("PR", "MG_THIN", "MASTER_FULL")
FAST_JOBS = (
    "ruff", "memory-insight-contract", "show-router-integration",
    "build-linux-artifacts", "migration-release-guard", "npm-wrapper",
    "ui-checks", "manifest-consistency", "python-smoke",
)
PR_TEST_JOBS = ("unit-test-shards", "ui-tests", "scenario-evidence", "model-hub-hosted-wire")
HEAVY_JOBS = (
    "ui-browser-checks", "ios-shell", "desktop-shell", "install-upgrade-shards",
    "web-fetch-runtime", "web-fetch-macos", "windows-install-smoke-runners",
    "windows-agent", "windows-model-hub", "windows-control-ipc",
)
PRODUCERS = (*FAST_JOBS, *PR_TEST_JOBS, *HEAVY_JOBS)
GATES = {
    "unit-tests": (
        "unit-test-shards", "scenario-evidence", "ui-checks", "ui-tests",
        "ui-browser-checks", "model-hub-hosted-wire", "manifest-consistency",
        "python-smoke", "ios-shell", "desktop-shell",
    ),
    "install-upgrade-regression": ("install-upgrade-shards",),
    "windows-install-smoke": (
        "windows-install-smoke-runners", "web-fetch-runtime", "web-fetch-macos",
        "windows-agent", "windows-model-hub", "windows-control-ipc",
    ),
}
# Files outside these known roots are uncertain, never a reason to omit a job.
KNOWN_ROOTS = {
    "core", "modules", "vibe", "storage", "config", "tests", "ui", "ios",
    "desktop", "docs", "assets", "skills", "standards", "examples", ".agents",
}
KNOWN_FILES = {"main.py", "AGENTS.md", "VISION.md", "VISION_ZH.md", "SECURITY.md"}
# These can change dependency resolution, generated paths, or the runner itself.
SHARED_INPUTS = (".github/", "scripts/", "npm/", "patches/")
NATIVE_UI = ("ui/src/lib/native", "ui/src/lib/useNative", "ui/src/mobile/")
IOS_CONTRACTS = ("ios/", *NATIVE_UI, "vibe/data/", "vibe/message_types.json",
                 "assets/mascot/cloud-tuanzi-v2.png")
DESKTOP_CONTRACTS = ("desktop/", "ui/src/i18n/", *NATIVE_UI)
INSTALL_INPUTS = (
    "vibe/runtime", "vibe/install", "vibe/bootstrap", "vibe/upgrade",
    "vibe/cli.py", "vibe/commands/", "vibe/paths.py", "vibe/model_hub_runtime/",
    "vibe/show_runtime", "vibe/tmux_runtime", "vibe/git_runtime", "vibe/ripgrep_runtime",
    "vibe/cloudflared_runtime", "tests/test_install", "tests/test_upgrade",
    "tests/test_distribution", "tests/e2e/test_install", "tests/test_runtime",
    "desktop/", "skills/",
)
MAC_FETCH_INPUTS = (
    "core/web_fetch", "core/handlers/model_hub/", "vibe/model_hub_runtime/",
    "tests/test_model_hub_fetch", "tests/test_model_hub_local_fetch",
    "tests/test_desktop_runtime_bundle", "desktop/",
)


def common_input(path: str) -> bool:
    """Only explicitly known non-build paths may be ruled unrelated."""
    return (
        not well_formed(path)
        or path.startswith(SHARED_INPUTS)
        or (path.split("/", 1)[0] not in KNOWN_ROOTS and path not in KNOWN_FILES
            and not narrative_path(path))
        # UI build configuration/plugins can change output paths or native bundles.
        or (path.startswith("ui/") and not path.startswith(("ui/src/", "ui/e2e/", "ui/design/")))
    )


def heavy_routes(scope: Scope) -> dict[str, str]:
    if (not scope.paths or not scope.routable or any(common_input(p) for p in scope.paths)
            or scope.platform_reason.startswith("added name ")):
        return dict.fromkeys(HEAVY_JOBS, "run")

    def affected(prefixes: tuple[str, ...]) -> str:
        return "run" if any(p.startswith(prefixes) for p in scope.paths) else "skip"

    # Native platform checks cannot prove the behavior of a change only on
    # Linux. All Python/runtime roots are conservatively relevant on Windows;
    # ordinary UI, iOS, desktop and narrative changes do not invoke those tools.
    windows = affected(("core/", "modules/", "vibe/", "storage/", "config/", "tests/", "main.py", "skills/"))
    installer = affected(INSTALL_INPUTS)
    return {
        "ui-browser-checks": scope.browser,
        "ios-shell": affected(IOS_CONTRACTS),
        "desktop-shell": affected(DESKTOP_CONTRACTS),
        "install-upgrade-shards": installer,
        "web-fetch-runtime": scope.platform,
        "web-fetch-macos": affected(MAC_FETCH_INPUTS),
        "windows-install-smoke-runners": installer,
        "windows-agent": windows,
        "windows-model-hub": windows,
        "windows-control-ipc": windows,
    }


def make_plan(mode: str, scope: Scope | None = None) -> dict:
    if mode not in MODES:
        raise ValueError(f"Unknown CI mode: {mode!r}")
    jobs = dict.fromkeys(PRODUCERS, "run")
    if mode == "MG_THIN":
        jobs.update(dict.fromkeys((*PR_TEST_JOBS, *HEAVY_JOBS), "skip"))
    elif mode == "PR":
        if scope is None:
            raise ValueError("PR routing needs a complete change classification")
        jobs.update(heavy_routes(scope))
    return {"mode": mode, "jobs": jobs}


def validate_plan(plan: object) -> dict:
    if not isinstance(plan, dict) or set(plan) != {"mode", "jobs"} or plan["mode"] not in MODES:
        raise ValueError("Missing or invalid CI mode/plan")
    jobs = plan["jobs"]
    if not isinstance(jobs, dict) or set(jobs) != set(PRODUCERS) or set(jobs.values()) - {"run", "skip"}:
        raise ValueError("Missing, unknown or invalid producer disposition")
    if plan["mode"] != "PR":
        if plan != make_plan(plan["mode"]):
            raise ValueError(f"Producer dispositions do not match {plan['mode']}")
    elif any(jobs[job] != "run" for job in (*FAST_JOBS, *PR_TEST_JOBS)):
        raise ValueError("PR must run every cheap Linux check")
    return plan


def mode_for_event(event_name: str, ref: str) -> str:
    if event_name == "pull_request":
        return "PR"
    if event_name == "merge_group":
        return "MG_THIN"
    if event_name in {"push", "workflow_dispatch"} and ref == "refs/heads/master":
        return "MASTER_FULL"
    raise ValueError(f"Unsupported CI event/ref: {event_name}/{ref}")


def plan_from_needs(needs: dict, *, event_name: str, ref: str) -> dict:
    changes = needs.get("changes", {})
    if changes.get("result") != "success":
        raise ValueError("Change classification did not succeed")
    outputs = changes.get("outputs", {})
    plan = validate_plan(json.loads(outputs.get("plan", "")))
    mode = mode_for_event(event_name, ref)
    if outputs.get("mode") != mode or plan["mode"] != mode:
        raise ValueError("CI mode does not match the triggering event")
    return plan


def check_results(plan: dict, needs: dict, jobs: tuple[str, ...]) -> list[str]:
    validate_plan(plan)
    problems = []
    if set(jobs) - (set(PRODUCERS) | set(GATES)):
        raise ValueError("Unknown result producer")
    for job in jobs:
        expected = "success" if plan["jobs"].get(job, "run") == "run" else "skipped"
        result = needs.get(job, {}).get("result")
        if result != expected:
            problems.append(f"{job}: {result!r}; expected {expected} in {plan['mode']}")
    return problems


def check_gate(gate: str, needs: dict, *, event_name: str, ref: str) -> list[str]:
    plan = plan_from_needs(needs, event_name=event_name, ref=ref)
    if gate == "master-full-backstop":
        if plan["mode"] != "MASTER_FULL":
            raise ValueError("Only MASTER_FULL can certify the release backstop")
        jobs = (*PRODUCERS, *GATES)
    else:
        jobs = GATES[gate]
    if set(needs) != {"changes", *jobs}:
        raise ValueError("Gate dependencies are missing or unknown")
    return check_results(plan, needs, jobs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", choices=[*GATES, "master-full-backstop"])
    args = parser.parse_args()
    event_name, ref = os.environ.get("GITHUB_EVENT_NAME", ""), os.environ.get("GITHUB_REF", "")
    try:
        if args.gate:
            needs = json.loads(os.environ.get("NEEDS_JSON", ""))
            problems = check_gate(args.gate, needs, event_name=event_name, ref=ref)
            for problem in problems:
                print(f"::error::{problem}")
            if not problems:
                print(f"{args.gate}: every result matches the declared CI scope.")
            return int(bool(problems))
        mode = mode_for_event(event_name, ref)
        scope = None
        if mode != "MASTER_FULL":
            try:
                event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
            except (KeyError, OSError, ValueError):
                event = {}
            scope = classify(event_name, event, os.environ.get("GITHUB_SHA", ""), Path.cwd())
        plan = make_plan(mode, scope)
        serialized = json.dumps(plan, separators=(",", ":"))
        if output := os.environ.get("GITHUB_OUTPUT"):
            with Path(output).open("a", encoding="utf-8") as stream:
                stream.write(f"mode={mode}\nplan={serialized}\n")
        summary = f"## CI scope: {mode}\n\n" + "\n".join(f"- `{job}`: **{route}**" for job, route in plan["jobs"].items()) + "\n"
        if scope:
            summary += f"\n{scope.reason}\n"
        print(summary)
        if output := os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(output).open("a", encoding="utf-8") as stream:
                stream.write(summary)
        return 0
    except (KeyError, TypeError, ValueError) as exc:
        print(f"::error::Invalid CI scope: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
