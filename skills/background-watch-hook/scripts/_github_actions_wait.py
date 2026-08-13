"""Shared GitHub Actions polling helpers for the bundled waiters."""

from __future__ import annotations

import urllib.parse
from typing import Any

from _github_wait_common import github_get

DEFAULT_SUCCESS_CONCLUSIONS = {"success", "skipped", "neutral"}
TERMINAL_STATUS = "completed"


def fetch_workflow_runs(
    repo: str,
    token: str | None,
    *,
    branch: str | None = None,
    head_sha: str | None = None,
    max_pages: int = 3,
    cache: Any = None,
) -> tuple[list[dict[str, Any]], int]:
    encoded_repo = urllib.parse.quote(repo, safe="/")
    query: dict[str, str | int] = {"per_page": 100}
    if branch:
        query["branch"] = branch
    if head_sha:
        query["head_sha"] = head_sha

    runs: list[dict[str, Any]] = []
    request_count = 0
    for page in range(1, max(max_pages, 1) + 1):
        query["page"] = page
        url = f"https://api.github.com/repos/{encoded_repo}/actions/runs?{urllib.parse.urlencode(query)}"
        payload = github_get(url, token, cache=cache)
        request_count += 1
        if not isinstance(payload, dict):
            raise RuntimeError(f"Expected a JSON object from {url}")
        page_runs = payload.get("workflow_runs")
        if not isinstance(page_runs, list):
            raise RuntimeError(f"Expected workflow_runs list from {url}")
        runs.extend(run for run in page_runs if isinstance(run, dict))
        if len(page_runs) < 100:
            break
    return runs, request_count


def workflow_name(run: dict[str, Any]) -> str:
    return str(run.get("name") or run.get("workflowName") or "")


def _run_sort_key(run: dict[str, Any]) -> tuple[str, int]:
    timestamp = str(run.get("run_started_at") or run.get("created_at") or "")
    run_id = int(run["id"]) if isinstance(run.get("id"), int) else 0
    return timestamp, run_id


def select_matching_runs(
    runs: list[dict[str, Any]],
    *,
    workflows: list[str],
    branch: str | None,
    head_sha: str,
) -> dict[str, list[dict[str, Any]]]:
    """Return every distinct matching run, including older reruns."""

    workflow_set = set(workflows)
    result: dict[str, list[dict[str, Any]]] = {workflow: [] for workflow in workflows}
    seen_ids: dict[str, set[int]] = {workflow: set() for workflow in workflows}
    normalized_sha = head_sha.casefold()

    for run in sorted(runs, key=_run_sort_key):
        name = workflow_name(run)
        if name not in workflow_set:
            continue
        if str(run.get("head_sha") or "").casefold() != normalized_sha:
            continue
        if branch and str(run.get("head_branch") or "") != branch:
            continue
        run_id = run.get("id")
        if isinstance(run_id, int) and run_id in seen_ids[name]:
            continue
        if isinstance(run_id, int):
            seen_ids[name].add(run_id)
        result[name].append(run)

    return result


def normalize_selected_runs(selected: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Keep only stable gate state so timestamps do not create false events."""

    return {
        workflow: [
            {
                "id": run.get("id"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "head_sha": run.get("head_sha"),
                "head_branch": run.get("head_branch"),
            }
            for run in runs
        ]
        for workflow, runs in selected.items()
    }


def format_run(run: dict[str, Any]) -> str:
    name = workflow_name(run) or "unknown"
    status = str(run.get("status") or "unknown")
    conclusion = str(run.get("conclusion") or "none")
    url = str(run.get("html_url") or run.get("url") or "")
    title = str(run.get("display_title") or "")
    details = f" - {title}" if title else ""
    return f"- {name}: status={status} conclusion={conclusion}{details}\n  {url}"


def render_actions_result(
    *,
    repo: str,
    branch: str | None,
    head_sha: str,
    selected: dict[str, list[dict[str, Any]]],
    success_conclusions: set[str],
) -> tuple[str | None, bool]:
    missing = [workflow for workflow, runs in selected.items() if not runs]
    running = [
        workflow
        for workflow, runs in selected.items()
        if any(str(run.get("status") or "") != TERMINAL_STATUS for run in runs)
    ]
    if missing or running:
        return None, False

    failed = [
        workflow
        for workflow, runs in selected.items()
        if any(str(run.get("conclusion") or "") not in success_conclusions for run in runs)
    ]
    result = "failure" if failed else "success"
    short_sha = head_sha[:12]
    branch_label = f" on {branch}" if branch else ""
    lines = [f"GitHub Actions {result} for {repo}@{short_sha}{branch_label}"]
    for workflow in selected:
        for run in selected[workflow]:
            lines.append(format_run(run))
    if failed:
        lines.append(f"Failed workflow(s): {', '.join(failed)}")
    return "\n".join(lines), bool(failed)
