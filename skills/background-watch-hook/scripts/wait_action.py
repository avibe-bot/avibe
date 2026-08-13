#!/usr/bin/env python3
"""Wait until standalone GitHub Actions workflow runs finish for a commit."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _github_wait_common import (  # noqa: E402
    NO_EVENT_EXIT_CODE,
    NO_EVENT_MARKER,
    get_token,
    github_request,
    min_interval_for_unauthenticated,
    no_event,
    retry_initial_request,
)

from _github_actions_wait import (  # noqa: E402
    DEFAULT_SUCCESS_CONCLUSIONS,
    TERMINAL_STATUS,
    fetch_workflow_runs as _fetch_workflow_runs,
    normalize_selected_runs as _normalize_selected_runs,
    render_actions_result as _render_actions_result,
    select_matching_runs as _select_latest_runs_by_workflow,
)


def _write_cursor_output(path: str | None, *, selected: dict[str, list[dict[str, Any]]]) -> None:
    if not path:
        return

    payload = {
        workflow: [
            {
                "id": run.get("id"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "run_attempt": run.get("run_attempt"),
                "html_url": run.get("html_url"),
            }
            for run in runs
        ]
        for workflow, runs in selected.items()
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
    parser.add_argument(
        "--only-on-failure",
        action="store_true",
        help=(
            "Do not wake the Agent when every watched workflow succeeded; exit with the "
            f"no-event code {NO_EVENT_EXIT_CODE} and the '{NO_EVENT_MARKER}' marker instead. "
            "Failures still exit 0 with the full report."
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
    selected: dict[str, list[dict[str, Any]]] = {workflow: [] for workflow in args.workflow}
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

        def _fetch_runs() -> tuple[list[dict[str, Any]], int]:
            return _fetch_workflow_runs(
                args.repo,
                token,
                branch=args.branch,
                head_sha=args.sha,
                max_pages=args.max_pages,
            )

        request_result = (
            retry_initial_request(
                _fetch_runs,
                description="initial GitHub Actions request",
                unauthenticated=token is None,
            )
            if first_poll
            else github_request(_fetch_runs, unauthenticated=token is None)
        )
        if request_result.error is not None:
            print(str(request_result.error), file=sys.stderr)
            if first_poll or not request_result.error.retryable:
                return 1
            print(
                "Retryable GitHub request failure during polling; continuing in this watch",
                file=sys.stderr,
            )
            runs = []
            request_count = 0
        else:
            if request_result.value is None:
                print("GitHub request completed without a result", file=sys.stderr)
                return 1
            runs, request_count = request_result.value

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
            output, has_failed_workflow = _render_actions_result(
                repo=args.repo,
                branch=args.branch,
                head_sha=args.sha,
                selected=selected,
                success_conclusions=success_conclusions,
            )
            if output is not None:
                _write_cursor_output(args.cursor_output, selected=selected)
                if args.only_on_failure and not has_failed_workflow:
                    # The interesting outcome is a broken build. Reporting a green one
                    # costs a full Agent turn to say "nothing to do", so keep the
                    # summary on stderr where it stays inspectable via the watch log.
                    print(output, file=sys.stderr)
                    return no_event(
                        "All watched workflows succeeded; exiting without an Agent follow-up "
                        "because --only-on-failure is set."
                    )
                print(output)
                return 0

        missing = [workflow for workflow, runs in selected.items() if not runs]
        running = [
            workflow
            for workflow, runs in selected.items()
            if any(str(run.get("status") or "") != TERMINAL_STATUS for run in runs)
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
