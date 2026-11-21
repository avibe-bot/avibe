"""Fail closed on runner routing, not on guesses about job `if` expressions.

All workflows must use hosted runners except the dispatch-only channel signer.
This deliberately stronger contract covers PRs, reusable calls and workflow_run
chains without implementing GitHub's expression language or trusting path/branch
filters. Unknown runner expressions require review, not a silent exemption.
"""

from copy import deepcopy
import json
from pathlib import Path
import re

import pytest
import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
HOSTED = {
    "ubuntu-latest", "ubuntu-22.04", "ubuntu-24.04", "ubuntu-24.04-arm",
    "macos-latest", "macos-13", "macos-14", "macos-15", "macos-15-intel",
    "macos-26", "macos-26-intel", "xcode-27", "xcode-27-xlarge", "windows-latest", "windows-2022",
    "windows-2025", "windows-11-arm",
}
SIGNER = ("model-hub-engine-channel.yml", "sign")


def repository_workflows():
    return {
        path.name: yaml.safe_load(path.read_text())
        for path in sorted(WORKFLOWS.iterdir()) if path.suffix in {".yml", ".yaml"}
    }


def runner_errors(workflows):
    errors = []
    visited = set()

    def triggers(workflow):
        value = workflow.get("on", workflow.get(True, {}))
        return {value} if isinstance(value, str) else set(value)

    def resolve_json(value, inputs):
        if not isinstance(value, str):
            return value
        match = re.fullmatch(r"\$\{\{\s*fromJSON\(inputs\.(\w+)\)\s*\}\}", value)
        assert match, f"unresolved matrix: {value}"
        return json.loads(inputs[match[1]])

    def labels(value, job, inputs):
        if isinstance(value, list):
            return [label for item in value for label in labels(item, job, inputs)]
        # Runner groups are not proof of a GitHub-hosted allocation.
        assert isinstance(value, str), f"unreviewed runner/group: {value}"
        if "${{" not in value:
            return [value]
        match = re.fullmatch(r"\$\{\{\s*matrix\.(\w+)\s*\}\}", value)
        if match:
            key = match[1]
            matrix = resolve_json(job.get("strategy", {}).get("matrix", {}), inputs)
            candidates = list(matrix.get(key, []))
            assert not isinstance(matrix.get(key), str), "dynamic runner axis"
            include = resolve_json(matrix.get("include", []), inputs)
            candidates.extend(row[key] for row in include if key in row)
            assert candidates, f"missing runner axis: {key}"
            return [label for item in candidates for label in labels(item, {}, inputs)]
        # A literal lookup's values bound the possible runner even if its key
        # comes from an input. An unknown key cannot allocate a runner.
        match = re.fullmatch(r"\$\{\{\s*fromJSON\('([^']+)'\)\[matrix\.\w+\]\s*\}\}", value)
        assert match, f"unresolved runner: {value}"
        return [label for item in json.loads(match[1]).values() for label in labels(item, {}, inputs)]

    def visit(name, inputs, chain):
        assert name not in chain, f"recursive workflow call: {chain} -> {name}"
        visited.add(name)
        workflow = workflows[name]
        events = triggers(workflow)
        for job_id, job in workflow["jobs"].items():
            location = f"{name}/{job_id}"
            try:
                if (name, job_id) == SIGNER:
                    assert events == {"workflow_dispatch"}, "signer must remain dispatch-only"
                    assert not chain, "signer must not be reusable"
                    continue
                if "uses" in job:
                    target = job["uses"]
                    assert target.startswith("./.github/workflows/"), f"unreviewed external workflow: {target}"
                    child = target.removeprefix("./.github/workflows/")
                    assert "workflow_call" in triggers(workflows[child]), f"not reusable: {child}"
                    visit(child, job.get("with", {}), [*chain, name])
                else:
                    selected = labels(job.get("runs-on"), job, inputs)
                    assert selected and all(label in HOSTED for label in selected), f"non-hosted runners: {selected}"
            except (AssertionError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"{location}: {exc}")

    # Treat every event as untrusted for runner routing, including every
    # workflow_run listener, so adding a new chain cannot bypass the guard.
    for name, workflow in workflows.items():
        if triggers(workflow) - {"workflow_call"}:
            visit(name, {}, [])
    for name in workflows.keys() - visited:
        # Orphan reusables are checked too; unresolved inputs fail closed.
        visit(name, {}, [])
    return errors


def test_repository_runner_routing_keeps_fork_code_off_self_hosted():
    # Before the fix npm-wrapper checked out and executed PR code on a
    # persistent runner. Existing CI-contract tests checked routing, not hosts.
    assert runner_errors(repository_workflows()) == []


@pytest.mark.parametrize("route", [
    "direct", "array", "group", "matrix-axis", "matrix-include", "matrix-input",
    "nested-call", "workflow-run", "unknown-expression", "signer-pr", "signer-call",
])
def test_runner_guard_rejects_untrusted_routes(route):
    workflows = {
        "pr.yml": {"on": "pull_request", "jobs": {"test": {"runs-on": "ubuntu-latest"}}},
    }
    job = workflows["pr.yml"]["jobs"]["test"]
    if route == "direct":
        job["runs-on"] = "self-hosted"
    elif route == "array":
        job["runs-on"] = ["self-hosted", "Linux", "X64"]
    elif route == "group":
        job["runs-on"] = {"group": "private", "labels": "linux"}
    elif route in {"matrix-axis", "matrix-include", "matrix-input"}:
        job["runs-on"] = "${{ matrix.os }}"
        matrix = {"os": ["ubuntu-latest", "self-hosted"]} if route == "matrix-axis" else {
            "os": ["ubuntu-latest"], "include": [{"os": "self-hosted"}],
        }
        job["strategy"] = {"matrix": matrix}
        if route == "matrix-input":
            matrix["include"] = "${{ fromJSON(inputs.platforms) }}"
            workflows["child.yaml"] = {"on": "workflow_call", "jobs": {"test": deepcopy(job)}}
            workflows["pr.yml"]["jobs"]["test"] = {
                "uses": "./.github/workflows/child.yaml",
                "with": {"platforms": '[{"os":"self-hosted"}]'},
            }
    elif route == "nested-call":
        job.clear()
        job["uses"] = "./.github/workflows/child.yml"
        workflows["child.yml"] = {"on": "workflow_call", "jobs": {"call": {"uses": "./.github/workflows/leaf.yml"}}}
        workflows["leaf.yml"] = {"on": "workflow_call", "jobs": {"run": {"runs-on": "self-hosted"}}}
    elif route == "workflow-run":
        workflows["follow.yml"] = {
            "on": {"workflow_run": {"workflows": ["pr"], "types": ["completed"]}},
            "jobs": {"run": {"runs-on": "self-hosted"}},
        }
    elif route == "unknown-expression":
        job["runs-on"] = "${{ vars.RUNNER }}"
    else:
        workflows[SIGNER[0]] = {"on": {"workflow_dispatch": None}, "jobs": {SIGNER[1]: {"runs-on": "self-hosted"}}}
        if route == "signer-pr":
            workflows[SIGNER[0]]["on"]["pull_request"] = None
        else:
            workflows[SIGNER[0]]["on"]["workflow_call"] = None
            job.clear()
            job["uses"] = f"./.github/workflows/{SIGNER[0]}"
    assert runner_errors(workflows), route


def test_runner_guard_accepts_hosted_reusable_inputs_and_dispatch_only_signer():
    workflows = {
        "pr.yml": {"on": ["pull_request", "push"], "jobs": {"call": {
            "uses": "./.github/workflows/child.yaml", "with": {"platforms": '[{"os":"ubuntu-latest"}]'},
        }}},
        "child.yaml": {"on": "workflow_call", "jobs": {"test": {
            "runs-on": "${{ matrix.os }}", "strategy": {"matrix": {"include": "${{ fromJSON(inputs.platforms) }}"}},
        }}},
        SIGNER[0]: {"on": "workflow_dispatch", "jobs": {SIGNER[1]: {"runs-on": "self-hosted"}}},
    }
    assert runner_errors(workflows) == []
