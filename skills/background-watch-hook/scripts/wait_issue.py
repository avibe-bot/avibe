#!/usr/bin/env python3
"""Wait until a GitHub repository or issue receives new issue activity."""

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
    AUTHENTICATION_HINT,
    filter_new,
    get_authenticated_login,
    get_token,
    is_retryable_http_error,
    list_paginated_with_count,
    max_id,
    min_interval_for_unauthenticated,
    RETRY_EXIT_CODE,
    requests_per_poll,
    squash,
)


def _is_pull_request_issue(item: dict[str, Any]) -> bool:
    return isinstance(item.get("pull_request"), dict)


def _format_issue(issue: dict[str, Any]) -> str:
    issue_number = issue.get("number")
    author = ((issue.get("user") or {}).get("login")) or "unknown"
    state = str(issue.get("state") or "open").lower()
    title = squash(issue.get("title") or "")
    url = issue.get("html_url") or ""
    return f"- issue #{issue_number} by {author} ({state})\n  {title}\n  {url}"


def _format_issue_comment(issue_number: int, comment: dict[str, Any]) -> str:
    comment_id = comment.get("id")
    author = ((comment.get("user") or {}).get("login")) or "unknown"
    body = squash(comment.get("body"))
    url = comment.get("html_url") or ""
    return f"- issue_comment #{comment_id} by {author} on issue #{issue_number}\n  {body}\n  {url}"


def _is_self_authored_comment(comment: dict[str, Any], viewer_login: str | None) -> bool:
    if not viewer_login:
        return False
    author = ((comment.get("user") or {}).get("login")) or ""
    return str(author).casefold() == viewer_login.casefold()


def _fetch_new_issue_state(
    repo: str,
    token: str | None,
    *,
    stop_after_id: int | None = None,
    max_pages: int | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    encoded_repo = urllib.parse.quote(repo, safe="/")
    raw_issues, request_count = list_paginated_with_count(
        f"https://api.github.com/repos/{encoded_repo}/issues?state=all&sort=created&direction=desc",
        token,
        stop_after_id=stop_after_id,
        max_pages=max_pages,
    )
    issues = [item for item in raw_issues if not _is_pull_request_issue(item)]
    return {"issues": issues, "raw_issue_cursor": max_id(raw_issues)}, request_count


def _fetch_issue_comment_state(
    repo: str,
    issue_number: int,
    token: str | None,
    *,
    stop_after_id: int | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    encoded_repo = urllib.parse.quote(repo, safe="/")
    comments, request_count = list_paginated_with_count(
        f"https://api.github.com/repos/{encoded_repo}/issues/{issue_number}/comments?sort=created&direction=desc",
        token,
        stop_after_id=stop_after_id,
    )
    return {"issue_comments": comments}, request_count


def _render_new_issues(
    *,
    repo: str,
    state: dict[str, list[dict[str, Any]]],
    issue_cursor: int,
    event_limit: int,
) -> tuple[str | None, int]:
    new_issues = filter_new(state["issues"], issue_cursor)
    if not new_issues:
        return None, issue_cursor

    next_issue_cursor = max(issue_cursor, max_id(new_issues))
    lines = [f"GitHub new issue activity detected for {repo}"]
    rendered_events = [_format_issue(issue) for issue in new_issues]
    visible_limit = max(event_limit, 1)
    for entry in rendered_events[:visible_limit]:
        lines.append(entry)
    if len(rendered_events) > visible_limit:
        lines.append(f"- {len(rendered_events) - visible_limit} additional event(s) omitted")
    return "\n".join(lines), next_issue_cursor


def _render_issue_comments(
    *,
    repo: str,
    issue_number: int,
    state: dict[str, list[dict[str, Any]]],
    issue_comment_cursor: int,
    event_limit: int,
    viewer_login: str | None = None,
    ignore_self_comments: bool = True,
) -> tuple[str | None, int]:
    new_comments = filter_new(state["issue_comments"], issue_comment_cursor)
    if not new_comments:
        return None, issue_comment_cursor

    visible_comments = (
        [comment for comment in new_comments if not _is_self_authored_comment(comment, viewer_login)]
        if ignore_self_comments
        else new_comments
    )

    next_issue_comment_cursor = max(issue_comment_cursor, max_id(new_comments))
    rendered_events = [_format_issue_comment(issue_number, comment) for comment in visible_comments]
    if not rendered_events:
        return None, next_issue_comment_cursor

    lines = [f"GitHub issue activity detected for {repo}#{issue_number}"]
    visible_limit = max(event_limit, 1)
    for entry in rendered_events[:visible_limit]:
        lines.append(entry)
    if len(rendered_events) > visible_limit:
        lines.append(f"- {len(rendered_events) - visible_limit} additional event(s) omitted")
    return "\n".join(lines), next_issue_comment_cursor


def _write_cursor_output(
    path: str | None,
    *,
    issue_cursor: int | None = None,
    raw_issue_cursor: int | None = None,
    issue_comment_cursor: int | None = None,
) -> None:
    if not path:
        return

    payload: dict[str, int] = {}
    if issue_cursor is not None:
        payload["issue_cursor"] = issue_cursor
    if raw_issue_cursor is not None:
        payload["raw_issue_cursor"] = raw_issue_cursor
    if issue_comment_cursor is not None:
        payload["issue_comment_cursor"] = issue_comment_cursor
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub repo in owner/name form")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--issue", type=int, help="Issue number")
    mode.add_argument("--new-issues", action="store_true", help="Watch for new issues in the repository")
    parser.add_argument("--interval", type=float, default=45.0, help="Polling interval in seconds")
    parser.add_argument(
        "--timeout",
        type=float,
        default=21600.0,
        help="Overall timeout in seconds; default 21600 (6 hours), 0 means forever",
    )
    parser.add_argument("--since-issue-id", type=int, default=None, help="Existing repository issue cursor")
    parser.add_argument("--since-raw-issue-id", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--since-issue-comment-id", type=int, default=None, help="Existing issue comment cursor")
    parser.add_argument("--cursor-output", help=argparse.SUPPRESS)
    parser.add_argument("--event-limit", type=int, default=8, help="Maximum number of new events to include in stdout")
    parser.add_argument(
        "--include-self-comments",
        action="store_true",
        help="Include comments authored by the current authenticated GitHub user",
    )
    parser.add_argument(
        "--catch-up",
        action="store_true",
        help="Treat current existing activity as pending when no explicit cursor is provided",
    )
    parser.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        help="Allow polling without GitHub auth; the interval will be clamped to a safer minimum",
    )
    args = parser.parse_args()

    token = get_token()
    viewer_login = None if args.include_self_comments else get_authenticated_login(token)
    if token is None and not args.allow_unauthenticated:
        print(
            (
                "GitHub authentication is required for reliable polling. "
                f"{AUTHENTICATION_HINT} "
                "Or pass --allow-unauthenticated for a throttled best-effort run."
            ),
            file=sys.stderr,
        )
        return 2

    base_interval = max(args.interval, 1.0)
    effective_interval = base_interval
    start = time.monotonic()

    try:
        if args.issue is not None:
            initial_issue_comment_stop_after_id = None
            if args.since_issue_comment_id is not None and not args.catch_up:
                initial_issue_comment_stop_after_id = args.since_issue_comment_id
            state, requests_per_poll_count = _fetch_issue_comment_state(
                args.repo,
                args.issue,
                token,
                stop_after_id=initial_issue_comment_stop_after_id,
            )
        else:
            initial_raw_issue_stop_after_id = None
            initial_raw_issue_max_pages = None
            if args.since_raw_issue_id is not None and not args.catch_up:
                initial_raw_issue_stop_after_id = args.since_raw_issue_id
            elif args.since_issue_id is not None and not args.catch_up:
                initial_raw_issue_stop_after_id = args.since_issue_id
            elif not args.catch_up:
                initial_raw_issue_max_pages = 1
            state, requests_per_poll_count = _fetch_new_issue_state(
                args.repo,
                token,
                stop_after_id=initial_raw_issue_stop_after_id,
                max_pages=initial_raw_issue_max_pages,
            )
    except urllib.error.HTTPError as err:
        print(f"GitHub API error: {err.code} {err.reason}", file=sys.stderr)
        return RETRY_EXIT_CODE if is_retryable_http_error(err) else 1
    except urllib.error.URLError as err:
        print(f"GitHub network error: {err.reason}", file=sys.stderr)
        return RETRY_EXIT_CODE
    except Exception as err:  # noqa: BLE001
        print(f"Failed to fetch initial issue state: {err}", file=sys.stderr)
        return 1

    if token is None:
        bootstrap_requests = requests_per_poll_count
        unauthenticated_min = min_interval_for_unauthenticated(
            requests_per_poll_count,
            bootstrap_requests=bootstrap_requests,
        )
        startup_interval = max(base_interval, unauthenticated_min)
        if effective_interval < startup_interval:
            print(
                (
                    "No GitHub token detected; clamping polling interval from %.1fs to %.1fs "
                    "for %s request(s) per poll plus %s bootstrap request(s) to avoid "
                    "unauthenticated rate-limit lockout."
                )
                % (effective_interval, startup_interval, requests_per_poll_count, bootstrap_requests),
                file=sys.stderr,
            )
            effective_interval = startup_interval
    else:
        bootstrap_requests = 0

    if args.issue is not None:
        issue_comment_cursor = (
            args.since_issue_comment_id
            if args.since_issue_comment_id is not None
            else (0 if args.catch_up else max_id(state["issue_comments"]))
        )
        print(
            (
                "Watching GitHub issue %s#%s comments from cursor: issue_comment=%s catch_up=%s"
                % (args.repo, args.issue, issue_comment_cursor, args.catch_up)
            ),
            file=sys.stderr,
        )
        initial_output, issue_comment_cursor = _render_issue_comments(
            repo=args.repo,
            issue_number=args.issue,
            state=state,
            issue_comment_cursor=issue_comment_cursor,
            event_limit=args.event_limit,
            viewer_login=viewer_login,
            ignore_self_comments=not args.include_self_comments,
        )
        if initial_output is not None:
            _write_cursor_output(args.cursor_output, issue_comment_cursor=issue_comment_cursor)
            print(initial_output)
            return 0
    else:
        issue_cursor = args.since_issue_id if args.since_issue_id is not None else (0 if args.catch_up else max_id(state["issues"]))
        raw_issue_cursor = (
            args.since_raw_issue_id
            if args.since_raw_issue_id is not None
            else (0 if args.catch_up else state["raw_issue_cursor"])
        )
        print(
            (
                f"Watching GitHub new issues in {args.repo} from cursor: issue={issue_cursor} "
                f"raw_issue={raw_issue_cursor} catch_up={args.catch_up}"
            ),
            file=sys.stderr,
        )
        initial_output, issue_cursor = _render_new_issues(
            repo=args.repo,
            state=state,
            issue_cursor=issue_cursor,
            event_limit=args.event_limit,
        )
        raw_issue_cursor = max(raw_issue_cursor, state["raw_issue_cursor"])
        if initial_output is not None:
            _write_cursor_output(
                args.cursor_output,
                issue_cursor=issue_cursor,
                raw_issue_cursor=raw_issue_cursor,
            )
            print(initial_output)
            return 0

    while True:
        sleep_seconds = effective_interval
        if args.timeout > 0:
            remaining_timeout = args.timeout - (time.monotonic() - start)
            if remaining_timeout <= 0:
                print("Timed out while waiting for GitHub issue activity", file=sys.stderr)
                return 124
            sleep_seconds = min(sleep_seconds, remaining_timeout)

        time.sleep(sleep_seconds)

        try:
            if args.issue is not None:
                state, requests_per_poll_count = _fetch_issue_comment_state(
                    args.repo,
                    args.issue,
                    token,
                    stop_after_id=issue_comment_cursor if issue_comment_cursor > 0 else None,
                )
            else:
                state, requests_per_poll_count = _fetch_new_issue_state(
                    args.repo,
                    token,
                    stop_after_id=raw_issue_cursor if raw_issue_cursor > 0 else None,
                )
        except urllib.error.HTTPError as err:
            if token is None and err.code in {403, 429}:
                print(
                    (
                        f"GitHub unauthenticated polling hit a rate limit. {AUTHENTICATION_HINT}"
                    ),
                    file=sys.stderr,
                )
                return 1
            print(f"GitHub API error during polling: {err.code} {err.reason}", file=sys.stderr)
            continue
        except Exception as err:  # noqa: BLE001
            print(f"Polling failed: {err}", file=sys.stderr)
            continue

        if token is None:
            unauthenticated_min = min_interval_for_unauthenticated(requests_per_poll_count)
            target_interval = max(base_interval, unauthenticated_min)
            if target_interval != effective_interval:
                if target_interval > effective_interval:
                    print(
                        (
                            "GitHub unauthenticated polling now needs %.1fs minimum for %s request(s) "
                            "per poll; increasing interval."
                        )
                        % (target_interval, requests_per_poll_count),
                        file=sys.stderr,
                    )
                else:
                    print(
                        (
                            "GitHub unauthenticated polling now needs only %.1fs minimum for %s request(s) "
                            "per poll; reducing interval."
                        )
                        % (target_interval, requests_per_poll_count),
                        file=sys.stderr,
                    )
                effective_interval = target_interval

        if args.issue is not None:
            output, issue_comment_cursor = _render_issue_comments(
                repo=args.repo,
                issue_number=args.issue,
                state=state,
                issue_comment_cursor=issue_comment_cursor,
                event_limit=args.event_limit,
                viewer_login=viewer_login,
                ignore_self_comments=not args.include_self_comments,
            )
            if output is None:
                continue
            _write_cursor_output(args.cursor_output, issue_comment_cursor=issue_comment_cursor)
        else:
            raw_issue_cursor = max(raw_issue_cursor, state["raw_issue_cursor"])
            output, issue_cursor = _render_new_issues(
                repo=args.repo,
                state=state,
                issue_cursor=issue_cursor,
                event_limit=args.event_limit,
            )
            if output is None:
                continue
            _write_cursor_output(
                args.cursor_output,
                issue_cursor=issue_cursor,
                raw_issue_cursor=raw_issue_cursor,
            )

        print(output)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
