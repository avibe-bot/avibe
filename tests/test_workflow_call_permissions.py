"""Check the real local reusable-workflow call graph before GitHub can reject it.

GITHUB_TOKEN permissions can only stay level or decrease across workflow calls.
Job permissions replace workflow defaults; an explicit map makes omitted scopes
`none`. Conditions do not exempt a called job from startup validation.
https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
LEVELS = {"none": 0, "read": 1, "write": 2}


def permission_errors(workflows: dict, entry: str) -> list[str]:
    """Resolve explicit token scopes through local calls, including skipped jobs.

    This checks permission compatibility, not every part of GitHub's compiler.
    The entry must declare its defaults so repository settings are not guessed.
    """
    scopes = {
        scope
        for workflow in workflows.values()
        for owner in [workflow, *workflow.get("jobs", {}).values()]
        if isinstance(owner.get("permissions"), dict)
        for scope in owner["permissions"]
    }
    errors = []

    def effective(value, inherited):
        if value is None:
            return dict(inherited)
        if value in ("read-all", "write-all"):
            return dict.fromkeys(scopes, LEVELS[value.removesuffix("-all")])
        assert isinstance(value, dict), f"Unsupported permissions: {value!r}"
        return {scope: LEVELS[value.get(scope, "none")] for scope in scopes}

    def check(requested, ceiling, path):
        if ceiling is not None:
            for scope in sorted(scopes):
                if requested[scope] > ceiling[scope]:
                    requested_level = next(key for key, value in LEVELS.items() if value == requested[scope])
                    allowed_level = next(key for key, value in LEVELS.items() if value == ceiling[scope])
                    errors.append(f"{path}: {scope}:{requested_level} exceeds caller {scope}:{allowed_level}")

    def visit(name, ceiling, chain):
        assert name not in chain, f"Recursive workflow call: {chain} -> {name}"
        workflow = workflows[name]
        assert ceiling is not None or "permissions" in workflow, "Entry permissions must be explicit"
        defaults = effective(workflow.get("permissions"), ceiling or dict.fromkeys(scopes, 0))
        check(defaults, ceiling, f"{name} workflow")
        for job_id, job in workflow["jobs"].items():
            permissions = effective(job.get("permissions"), defaults)
            check(permissions, ceiling, f"{name} / {job_id}")
            target = job.get("uses", "")
            if target.startswith(("./.github/workflows/", "$/.github/workflows/")):
                child = target.rsplit("/", 1)[1]
                triggers = workflows[child].get("on", workflows[child].get(True, {}))
                assert "workflow_call" in triggers, f"{name} / {job_id} calls non-reusable {child}"
                visit(child, permissions, [*chain, name])

    visit(entry, None, [])
    return errors


def repository_workflows():
    return {path.name: yaml.safe_load(path.read_text()) for path in WORKFLOWS.glob("*.yml")}


def test_release_reusable_workflow_permissions_fit_the_actual_caller():
    # #3206 had dispatch-only jobs in a reusable file. Their false `if` did not
    # prevent a release startup failure: actions:read exceeded actions:none.
    workflows = repository_workflows()
    callers = {
        name
        for name, workflow in workflows.items()
        if any(job.get("uses", "").startswith(("./.github/workflows/", "$/.github/workflows/"))
               for job in workflow["jobs"].values())
    }
    assert "desktop-release.yml" in callers
    for caller in sorted(callers):
        assert permission_errors(workflows, caller) == []


def test_even_a_disabled_nested_job_cannot_escalate_release_permissions():
    workflows = repository_workflows()
    caller = workflows["desktop-release.yml"]["jobs"]["desktop-packages"]
    child = caller["uses"].rsplit("/", 1)[1]
    workflows[child]["jobs"]["disabled-signing"] = {
        "if": "${{ false }}",
        "permissions": {"contents": "read", "actions": "read"},
        "runs-on": "ubuntu-latest",
        "steps": [{"run": "true"}],
    }
    assert permission_errors(workflows, "desktop-release.yml") == []


@pytest.mark.parametrize(("caller_job", "child_permissions", "grandchild_permissions", "expected"), [
    ({}, None, {"contents": "read"}, []),
    ({}, {}, {"contents": "read"}, ["leaf.yml / run: contents:read exceeds caller contents:none"]),
    ({"permissions": {"contents": "read"}}, None, {"contents": "write"},
     ["leaf.yml / run: contents:write exceeds caller contents:read"]),
    ({"permissions": {}}, None, {"contents": "read"},
     ["leaf.yml / run: contents:read exceeds caller contents:none"]),
    ({"permissions": {"actions": "read"}}, None, {"actions": "read"}, []),
    ({"permissions": "read-all"}, None, {"actions": "read"}, []),
    ({"permissions": "read-all"}, None, {"contents": "write"},
     ["leaf.yml / run: contents:write exceeds caller contents:read"]),
])
def test_permission_resolution_inherits_or_replaces_defaults_across_each_call(
    caller_job, child_permissions, grandchild_permissions, expected
):
    # Parse YAML fixtures rather than depending on text positions or job names.
    workflows = {
        "root.yml": yaml.safe_load("""
permissions:
  contents: write
jobs:
  call:
    uses: ./.github/workflows/child.yml
"""),
        "child.yml": yaml.safe_load("""
on: {workflow_call: null}
jobs:
  call:
    uses: ./.github/workflows/leaf.yml
"""),
        "leaf.yml": yaml.safe_load("""
on: {workflow_call: null}
jobs:
  run:
    runs-on: ubuntu-latest
    steps: [{run: 'true'}]
"""),
    }
    workflows["root.yml"]["jobs"]["call"].update(deepcopy(caller_job))
    if child_permissions is not None:
        workflows["child.yml"]["permissions"] = child_permissions
    workflows["leaf.yml"]["jobs"]["run"]["permissions"] = grandchild_permissions
    assert permission_errors(workflows, "root.yml") == expected
