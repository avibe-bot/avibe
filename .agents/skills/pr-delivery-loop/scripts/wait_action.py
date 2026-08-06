#!/usr/bin/env python3
"""Wait until selected GitHub Actions workflow runs finish for a commit."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _github_wait_common import (  # noqa: E402
    RETRY_EXIT_CODE,
    get_token,
    github_get,
    is_retryable_http_error,
    min_interval_for_unauthenticated,
)

DEFAULT_SUCCESS_CONCLUSIONS = {"success", "skipped", "neutral"}
TERMINAL_STATUS = "completed"


def _fetch_workflow_runs(
    repo: str,
    token: str | None,
    *,
    branch: str | None = None,
    head_sha: str | None = None,
    max_pages: int = 3,
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
        payload = github_get(url, token)
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


def _workflow_name(run: dict[str, Any]) -> str:
    return str(run.get("name") or run.get("workflowName") or "")


def _run_sort_key(run: dict[str, Any]) -> tuple[str, int]:
    timestamp = str(run.get("run_started_at") or run.get("created_at") or "")
    run_id = int(run["id"]) if isinstance(run.get("id"), int) else 0
    return timestamp, run_id


def _select_latest_runs_by_workflow(
    runs: list[dict[str, Any]],
    *,
    workflows: list[str],
    branch: str | None,
    head_sha: str,
) -> dict[str, dict[str, Any] | None]:
    workflow_set = set(workflows)
    result: dict[str, dict[str, Any] | None] = {workflow: None for workflow in workflows}
    normalized_sha = head_sha.casefold()

    for run in sorted(runs, key=_run_sort_key):
        name = _workflow_name(run)
        if name not in workflow_set:
            continue
        if str(run.get("head_sha") or "").casefold() != normalized_sha:
            continue
        if branch and str(run.get("head_branch") or "") != branch:
            continue
        result[name] = run

    return result


def _format_run(run: dict[str, Any]) -> str:
    name = _workflow_name(run) or "unknown"
    status = str(run.get("status") or "unknown")
    conclusion = str(run.get("conclusion") or "none")
    url = str(run.get("html_url") or run.get("url") or "")
    title = str(run.get("display_title") or "")
    details = f" - {title}" if title else ""
    return f"- {name}: status={status} conclusion={conclusion}{details}\n  {url}"


def _render_actions_result(
    *,
    repo: str,
    branch: str | None,
    head_sha: str,
    selected: dict[str, dict[str, Any] | None],
    success_conclusions: set[str],
) -> tuple[str | None, bool]:
    missing = [workflow for workflow, run in selected.items() if run is None]
    running = [
        workflow
        for workflow, run in selected.items()
        if run is not None and str(run.get("status") or "") != TERMINAL_STATUS
    ]
    if missing or running:
        return None, False

    failed = [
        workflow
        for workflow, run in selected.items()
        if run is not None and str(run.get("conclusion") or "") not in success_conclusions
    ]
    result = "failure" if failed else "success"
    short_sha = head_sha[:12]
    branch_label = f" on {branch}" if branch else ""
    lines = [f"GitHub Actions {result} for {repo}@{short_sha}{branch_label}"]
    for workflow in selected:
        run = selected[workflow]
        if run is not None:
            lines.append(_format_run(run))
    if failed:
        lines.append(f"Failed workflow(s): {', '.join(failed)}")
    return "\n".join(lines), bool(failed)


def _write_cursor_output(path: str | None, *, selected: dict[str, dict[str, Any] | None]) -> None:
    if not path:
        return

    payload = {
        workflow: {
            "id": run.get("id"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "html_url": run.get("html_url"),
        }
        for workflow, run in selected.items()
        if run is not None
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _parse_success_conclusions(values: list[str] | None) -> set[str]:
    if not values:
        return set(DEFAULT_SUCCESS_CONCLUSIONS)
    result: set[str] = set()
    for value in values:
        result.update(item.strip() for item in value.split(",") if item.strip())
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub repo in owner/name form")
    parser.add_argument("--branch", help="Branch name to match, e.g. main")
    parser.add_argument("--sha", required=True, help="Exact head commit SHA to match")
    parser.add_argument("--workflow", action="append", required=True, help="Workflow name to wait for; repeatable")
    parser.add_argument("--interval", type=float, default=45.0, help="Polling interval in seconds")
    parser.add_argument(
        "--timeout",
        type=float,
        default=21600.0,
        help="Overall timeout in seconds; default 21600 (6 hours), 0 means forever",
    )
    parser.add_argument("--max-pages", type=int, default=3, help="Maximum Actions run-list pages to inspect per poll")
    parser.add_argument(
        "--success-conclusion",
        action="append",
        help=(
            "Conclusion treated as successful; repeatable or comma-separated. "
            "Defaults to success,skipped,neutral."
        ),
    )
    parser.add_argument("--cursor-output", help=argparse.SUPPRESS)
    parser.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        help="Allow polling without GitHub auth; the interval will be clamped to a safer minimum",
    )
    args = parser.parse_args()

    token = get_token()
    if token is None and not args.allow_unauthenticated:
        print(
            (
                "GitHub authentication is required for reliable polling. "
                "Set GITHUB_TOKEN/GH_TOKEN, run 'gh auth login', or pass "
                "--allow-unauthenticated for a throttled best-effort run."
            ),
            file=sys.stderr,
        )
        return 2

    base_interval = max(args.interval, 1.0)
    effective_interval = base_interval
    success_conclusions = _parse_success_conclusions(args.success_conclusion)
    start = time.monotonic()
    selected: dict[str, dict[str, Any] | None] = {workflow: None for workflow in args.workflow}
    first_successful_fetch = True

    print(
        (
            "Watching GitHub Actions for %s sha=%s branch=%s workflows=%s"
            % (args.repo, args.sha, args.branch or "-", ",".join(args.workflow))
        ),
        file=sys.stderr,
    )

    poll_attempt = 0
    while True:
        first_poll = poll_attempt == 0
        poll_attempt += 1
        try:
            runs, request_count = _fetch_workflow_runs(
                args.repo,
                token,
                branch=args.branch,
                head_sha=args.sha,
                max_pages=args.max_pages,
            )
        except urllib.error.HTTPError as err:
            if first_poll:
                print(f"GitHub API error: {err.code} {err.reason}", file=sys.stderr)
                return RETRY_EXIT_CODE if is_retryable_http_error(err) else 1
            if token is None and err.code in {403, 429}:
                print(
                    (
                        "GitHub unauthenticated polling hit a rate limit. "
                        "Authenticate with 'gh auth login' or GITHUB_TOKEN/GH_TOKEN."
                    ),
                    file=sys.stderr,
                )
                return 1
            print(f"GitHub API error during polling: {err.code} {err.reason}", file=sys.stderr)
            runs = []
        except urllib.error.URLError as err:
            print(f"GitHub network error: {err.reason}", file=sys.stderr)
            if first_poll:
                return RETRY_EXIT_CODE
            runs = []
        except Exception as err:  # noqa: BLE001
            print(f"Polling failed: {err}", file=sys.stderr)
            if first_poll:
                return 1
            runs = []
            request_count = 0

        if token is None and request_count > 0:
            bootstrap_requests = request_count if first_successful_fetch else 0
            unauthenticated_min = min_interval_for_unauthenticated(
                request_count,
                bootstrap_requests=bootstrap_requests,
            )
            target_interval = max(base_interval, unauthenticated_min)
            if target_interval != effective_interval:
                direction = "increasing" if target_interval > effective_interval else "reducing"
                print(
                    (
                        "GitHub unauthenticated polling uses %s request(s) per poll plus "
                        "%s bootstrap request(s); %s interval from %.1fs to %.1fs."
                    )
                    % (
                        request_count,
                        bootstrap_requests,
                        direction,
                        effective_interval,
                        target_interval,
                    ),
                    file=sys.stderr,
                )
                effective_interval = target_interval
            first_successful_fetch = False

        if runs:
            selected = _select_latest_runs_by_workflow(
                runs,
                workflows=args.workflow,
                branch=args.branch,
                head_sha=args.sha,
            )
            output, _has_failed_workflow = _render_actions_result(
                repo=args.repo,
                branch=args.branch,
                head_sha=args.sha,
                selected=selected,
                success_conclusions=success_conclusions,
            )
            if output is not None:
                _write_cursor_output(args.cursor_output, selected=selected)
                print(output)
                return 0

        missing = [workflow for workflow, run in selected.items() if run is None]
        running = [
            workflow
            for workflow, run in selected.items()
            if run is not None and str(run.get("status") or "") != TERMINAL_STATUS
        ]
        print(f"Waiting for GitHub Actions: missing={missing or '-'} running={running or '-'}", file=sys.stderr)

        sleep_seconds = effective_interval
        if args.timeout > 0:
            remaining_timeout = args.timeout - (time.monotonic() - start)
            if remaining_timeout <= 0:
                print("Timed out while waiting for GitHub Actions", file=sys.stderr)
                return 124
            sleep_seconds = min(sleep_seconds, remaining_timeout)

        time.sleep(sleep_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
