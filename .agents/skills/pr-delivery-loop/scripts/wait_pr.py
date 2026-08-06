#!/usr/bin/env python3
"""Wait until a GitHub pull request or repository receives new PR activity."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _github_wait_common import (  # noqa: E402
    filter_new,
    get_authenticated_login,
    get_token,
    github_get,
    is_rate_limit_http_error,
    is_retryable_http_error,
    list_paginated,
    list_paginated_with_count,
    max_id,
    min_interval_for_unauthenticated,
    RETRY_EXIT_CODE,
    requests_per_poll,
    squash,
)

CODEX_REVIEW_PASS_REACTION_USERS = frozenset(
    {"chatgpt-codex-connector", "chatgpt-codex-connector[bot]"}
)
CODEX_REVIEW_PASS_REACTION_CONTENT = "+1"


def _format_review(review: dict[str, Any]) -> str:
    review_id = review.get("id")
    author = ((review.get("user") or {}).get("login")) or "unknown"
    state = str(review.get("state") or "commented").lower()
    body = squash(review.get("body") or state)
    url = review.get("html_url") or ""
    return f"- review #{review_id} by {author} ({state})\n  {body}\n  {url}"


def _format_review_comment(comment: dict[str, Any]) -> str:
    comment_id = comment.get("id")
    author = ((comment.get("user") or {}).get("login")) or "unknown"
    path = comment.get("path") or "unknown-path"
    body = squash(comment.get("body"))
    url = comment.get("html_url") or ""
    return f"- review_comment #{comment_id} by {author} on {path}\n  {body}\n  {url}"


def _format_issue_comment(comment: dict[str, Any]) -> str:
    comment_id = comment.get("id")
    author = ((comment.get("user") or {}).get("login")) or "unknown"
    body = squash(comment.get("body"))
    url = comment.get("html_url") or ""
    return f"- issue_comment #{comment_id} by {author}\n  {body}\n  {url}"


def _is_self_authored_comment(comment: dict[str, Any], viewer_login: str | None) -> bool:
    if not viewer_login:
        return False
    author = ((comment.get("user") or {}).get("login")) or ""
    return str(author).casefold() == viewer_login.casefold()


def _is_codex_pass_reaction(reaction: dict[str, Any]) -> bool:
    author = ((reaction.get("user") or {}).get("login")) or ""
    content = str(reaction.get("content") or "")
    return author.casefold() in CODEX_REVIEW_PASS_REACTION_USERS and content == CODEX_REVIEW_PASS_REACTION_CONTENT


def _format_reaction(reaction: dict[str, Any]) -> str:
    reaction_id = reaction.get("id")
    author = ((reaction.get("user") or {}).get("login")) or "unknown"
    content = str(reaction.get("content") or "")
    created_at = str(reaction.get("created_at") or "")
    return (
        f"- pr_reaction #{reaction_id} by {author} ({content})\n"
        f"  Codex review completed without comments and reacted on the PR body at {created_at}."
    )


def _current_pr_status(pr: dict[str, Any] | None) -> str:
    if not isinstance(pr, dict):
        return "unknown"
    if pr.get("merged_at"):
        return "merged"
    state = str(pr.get("state") or "").lower()
    if state == "closed":
        return "closed"
    if pr.get("draft") is True:
        return "draft"
    if state == "open":
        return state
    return state or "unknown"


def _describe_pr_status_change(previous_status: str, current_status: str) -> str:
    if previous_status == "draft" and current_status == "open":
        return "Pull request is ready for review."
    if previous_status == "open" and current_status == "draft":
        return "Pull request was converted to draft."
    if current_status == "merged":
        return "Pull request was merged."
    if current_status == "closed":
        return "Pull request was closed without merge."
    if current_status == "open":
        return "Pull request was reopened."
    return f"Pull request status changed from {previous_status} to {current_status}."


def _format_pr_status_event(pr: dict[str, Any], previous_status: str, current_status: str) -> str:
    pr_number = pr.get("number")
    url = pr.get("html_url") or ""
    return (
        f"- pr_status #{pr_number} {previous_status} -> {current_status}\n"
        f"  {_describe_pr_status_change(previous_status, current_status)}\n"
        f"  {url}"
    )


def _format_pull_request(pr: dict[str, Any]) -> str:
    pr_number = pr.get("number")
    author = ((pr.get("user") or {}).get("login")) or "unknown"
    state = str(pr.get("state") or "open").lower()
    title = squash(pr.get("title") or "")
    url = pr.get("html_url") or ""
    return f"- pull_request #{pr_number} by {author} ({state})\n  {title}\n  {url}"


def _fetch_state(repo: str, pr_number: int, token: str | None) -> tuple[dict[str, list[dict[str, Any]]], int]:
    encoded_repo = urllib.parse.quote(repo, safe="/")
    pull_request = github_get(
        f"https://api.github.com/repos/{encoded_repo}/pulls/{pr_number}",
        token,
    )
    reviews, review_requests = list_paginated_with_count(
        f"https://api.github.com/repos/{encoded_repo}/pulls/{pr_number}/reviews",
        token,
    )
    review_comments, review_comment_requests = list_paginated_with_count(
        f"https://api.github.com/repos/{encoded_repo}/pulls/{pr_number}/comments",
        token,
    )
    issue_comments, issue_comment_requests = list_paginated_with_count(
        f"https://api.github.com/repos/{encoded_repo}/issues/{pr_number}/comments",
        token,
    )
    reactions, reaction_requests = list_paginated_with_count(
        f"https://api.github.com/repos/{encoded_repo}/issues/{pr_number}/reactions",
        token,
    )
    return (
        {
            "pull_request": pull_request,
            "reviews": reviews,
            "review_comments": review_comments,
            "issue_comments": issue_comments,
            "reactions": reactions,
        },
        1 + review_requests + review_comment_requests + issue_comment_requests + reaction_requests,
    )


def _fetch_new_pr_state(
    repo: str,
    token: str | None,
    *,
    stop_after_id: int | None = None,
    max_pages: int | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    encoded_repo = urllib.parse.quote(repo, safe="/")
    pull_requests, request_count = list_paginated_with_count(
        f"https://api.github.com/repos/{encoded_repo}/pulls?state=all&sort=created&direction=desc",
        token,
        stop_after_id=stop_after_id,
        max_pages=max_pages,
    )
    return {"pull_requests": pull_requests}, request_count


def _render_activity(
    *,
    repo: str,
    pr_number: int,
    state: dict[str, list[dict[str, Any]]],
    review_cursor: int,
    review_comment_cursor: int,
    issue_comment_cursor: int,
    reaction_cursor: int,
    pr_status: str,
    event_limit: int,
    viewer_login: str | None = None,
    ignore_self_comments: bool = True,
) -> tuple[str | None, int, int, int, int, str]:
    current_pr_status = _current_pr_status(state.get("pull_request"))
    new_reviews = filter_new(state["reviews"], review_cursor)
    new_review_comments = filter_new(state["review_comments"], review_comment_cursor)
    new_issue_comments = filter_new(state["issue_comments"], issue_comment_cursor)
    visible_reviews = (
        [review for review in new_reviews if not _is_self_authored_comment(review, viewer_login)]
        if ignore_self_comments
        else new_reviews
    )
    visible_review_comments = (
        [comment for comment in new_review_comments if not _is_self_authored_comment(comment, viewer_login)]
        if ignore_self_comments
        else new_review_comments
    )
    visible_issue_comments = (
        [comment for comment in new_issue_comments if not _is_self_authored_comment(comment, viewer_login)]
        if ignore_self_comments
        else new_issue_comments
    )
    new_reactions = [
        reaction
        for reaction in filter_new(state["reactions"], reaction_cursor)
        if _is_codex_pass_reaction(reaction)
    ]
    has_pr_status_event = current_pr_status != pr_status

    if not (new_reviews or new_review_comments or new_issue_comments or new_reactions or has_pr_status_event):
        return None, review_cursor, review_comment_cursor, issue_comment_cursor, reaction_cursor, pr_status

    next_review_cursor = max(review_cursor, max_id(new_reviews))
    next_review_comment_cursor = max(review_comment_cursor, max_id(new_review_comments))
    next_issue_comment_cursor = max(issue_comment_cursor, max_id(new_issue_comments))
    next_reaction_cursor = max(reaction_cursor, max_id(state["reactions"]))
    next_pr_status = current_pr_status

    rendered_events: list[str] = []
    if has_pr_status_event and isinstance(state.get("pull_request"), dict):
        rendered_events.append(_format_pr_status_event(state["pull_request"], pr_status, current_pr_status))
    rendered_events.extend(_format_review(review) for review in visible_reviews)
    rendered_events.extend(_format_review_comment(comment) for comment in visible_review_comments)
    rendered_events.extend(_format_issue_comment(comment) for comment in visible_issue_comments)
    rendered_events.extend(_format_reaction(reaction) for reaction in new_reactions)

    if not rendered_events:
        return (
            None,
            next_review_cursor,
            next_review_comment_cursor,
            next_issue_comment_cursor,
            next_reaction_cursor,
            next_pr_status,
        )

    visible_limit = max(event_limit, 1)
    reaction_events = rendered_events[-len(new_reactions) :] if new_reactions else []
    non_reaction_events = rendered_events[: len(rendered_events) - len(reaction_events)]
    non_reaction_limit = max(visible_limit - len(reaction_events), 0)
    displayed_events = non_reaction_events[:non_reaction_limit] + reaction_events
    omitted_events = len(non_reaction_events) - len(displayed_events[:non_reaction_limit])

    lines = [f"GitHub PR activity detected for {repo}#{pr_number}"]
    for entry in displayed_events:
        lines.append(entry)

    if omitted_events:
        lines.append(f"- {omitted_events} additional event(s) omitted")

    return (
        "\n".join(lines),
        next_review_cursor,
        next_review_comment_cursor,
        next_issue_comment_cursor,
        next_reaction_cursor,
        next_pr_status,
    )


def _render_new_pull_requests(
    *,
    repo: str,
    state: dict[str, list[dict[str, Any]]],
    pr_cursor: int,
    event_limit: int,
) -> tuple[str | None, int]:
    new_pull_requests = filter_new(state["pull_requests"], pr_cursor)
    if not new_pull_requests:
        return None, pr_cursor

    next_pr_cursor = max(pr_cursor, max_id(new_pull_requests))
    lines = [f"GitHub new pull request activity detected for {repo}"]
    rendered_events = [_format_pull_request(pr) for pr in new_pull_requests]

    visible_limit = max(event_limit, 1)
    for entry in rendered_events[:visible_limit]:
        lines.append(entry)

    total_events = len(rendered_events)
    if total_events > visible_limit:
        lines.append(f"- {total_events - visible_limit} additional event(s) omitted")

    return "\n".join(lines), next_pr_cursor


def _write_cursor_output(
    path: str | None,
    *,
    review_cursor: int,
    review_comment_cursor: int,
    issue_comment_cursor: int,
    reaction_cursor: int,
    pr_status: str,
) -> None:
    if not path:
        return

    payload = {
        "review_cursor": review_cursor,
        "review_comment_cursor": review_comment_cursor,
        "issue_comment_cursor": issue_comment_cursor,
        "reaction_cursor": reaction_cursor,
        "pr_status": pr_status,
    }
    _write_staged_json(path, payload)


def _read_cursor_input(path: str | None) -> dict[str, Any]:
    if not path:
        return {}

    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("cursor file must contain a JSON object")
    return payload


def _staged_cursor_path(path: str) -> str:
    return f"{path}.pending"


def _write_staged_json(path: str, payload: dict[str, Any]) -> None:
    """Stage progress so a follow-up can acknowledge delivery before promotion."""
    _write_json_atomically(_staged_cursor_path(path), payload)


def _write_json_atomically(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    prefix = f".{os.path.basename(path)}."
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory, prefix=prefix, delete=False
    ) as handle:
        json.dump(payload, handle)
        temporary_path = handle.name
    os.replace(temporary_path, path)


def _promote_staged_cursor(path: str | None) -> None:
    if not path:
        return
    staged_path = _staged_cursor_path(path)
    if os.path.exists(staged_path):
        os.replace(staged_path, path)


def _write_new_pr_cursor_output(path: str | None, *, pr_cursor: int) -> None:
    if not path:
        return

    _write_staged_json(path, {"pr_cursor": pr_cursor})


def _settle_activity(
    *,
    repo: str,
    pr_number: int,
    token: str | None,
    initial_output: str,
    state: dict[str, list[dict[str, Any]]],
    review_cursor: int,
    review_comment_cursor: int,
    issue_comment_cursor: int,
    reaction_cursor: int,
    pr_status: str,
    event_limit: int,
    viewer_login: str | None,
    ignore_self_comments: bool,
) -> tuple[str, int, int, int, int, str]:
    """Collect a short burst of review envelope and inline-comment writes."""
    outputs = [initial_output]
    for _ in range(2):
        time.sleep(2)
        try:
            state, _request_count = _fetch_state(repo, pr_number, token)
        except urllib.error.HTTPError as err:
            print(f"GitHub API error during review settling: {err.code} {err.reason}", file=sys.stderr)
            break
        except Exception as err:  # noqa: BLE001
            print(f"Review settling failed: {err}", file=sys.stderr)
            break

        output, review_cursor, review_comment_cursor, issue_comment_cursor, reaction_cursor, pr_status = (
            _render_activity(
                repo=repo,
                pr_number=pr_number,
                state=state,
                review_cursor=review_cursor,
                review_comment_cursor=review_comment_cursor,
                issue_comment_cursor=issue_comment_cursor,
                reaction_cursor=reaction_cursor,
                pr_status=pr_status,
                event_limit=event_limit,
                viewer_login=viewer_login,
                ignore_self_comments=ignore_self_comments,
            )
        )
        if output is not None:
            outputs.append(output)

    return "\n\n".join(outputs), review_cursor, review_comment_cursor, issue_comment_cursor, reaction_cursor, pr_status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub repo in owner/name form")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pr", type=int, help="Pull request number")
    mode.add_argument("--new-prs", action="store_true", help="Watch for new pull requests in the repository")
    parser.add_argument("--interval", type=float, default=45.0, help="Polling interval in seconds")
    parser.add_argument(
        "--timeout",
        type=float,
        default=21600.0,
        help="Overall timeout in seconds; default 21600 (6 hours), 0 means forever",
    )
    parser.add_argument("--since-review-id", type=int, default=None, help="Existing review cursor")
    parser.add_argument("--since-review-comment-id", type=int, default=None, help="Existing review comment cursor")
    parser.add_argument("--since-issue-comment-id", type=int, default=None, help="Existing PR conversation comment cursor")
    parser.add_argument("--since-reaction-id", type=int, default=None, help="Existing PR-body reaction cursor")
    parser.add_argument("--since-pr-status", help=argparse.SUPPRESS)
    parser.add_argument("--since-pr-id", type=int, default=None, help="Existing repository pull request cursor")
    parser.add_argument(
        "--cursor-output",
        help="Write consumed activity cursors here when the waiter detects an event",
    )
    parser.add_argument(
        "--ack-cursor-file",
        help="Promote the previous staged cursor after its event was delivered and handled",
    )
    parser.add_argument(
        "--cursor-file",
        help="Read baseline cursors, or write them when --snapshot-cursors is set",
    )
    parser.add_argument(
        "--snapshot-cursors",
        action="store_true",
        help="Write the current PR activity cursors and exit",
    )
    parser.add_argument("--event-limit", type=int, default=8, help="Maximum number of new events to include in stdout")
    parser.add_argument(
        "--include-self-comments",
        action="store_true",
        help="Include reviews and comments authored by the current authenticated GitHub user",
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
    if args.snapshot_cursors and args.new_prs:
        parser.error("--snapshot-cursors requires --pr")

    try:
        _promote_staged_cursor(args.ack_cursor_file)
    except OSError as err:
        print(f"Failed to acknowledge staged cursor: {err}", file=sys.stderr)
        return 1

    token = get_token()
    viewer_login = (
        get_authenticated_login(token) if args.pr is not None and not args.include_self_comments else None
    )
    if args.pr is not None and not args.include_self_comments and viewer_login is None:
        print(
            "Unable to resolve the authenticated GitHub viewer identity; "
            "use a token that can read /user or pass --include-self-comments explicitly.",
            file=sys.stderr,
        )
        return 2
    if token is None and not args.allow_unauthenticated:
        print(
            (
                "GitHub authentication is required for reliable polling. "
                "Set GH_TOKEN/GITHUB_TOKEN, run 'gh auth login', or pass "
                "--allow-unauthenticated for a throttled best-effort run."
            ),
            file=sys.stderr,
        )
        return 2

    base_interval = max(args.interval, 1.0)
    effective_interval = base_interval

    start = time.monotonic()

    try:
        if args.pr is not None:
            state, requests_per_poll_count = _fetch_state(args.repo, args.pr, token)
        else:
            initial_pr_stop_after_id = None
            initial_pr_max_pages = None
            if args.since_pr_id is not None and not args.catch_up:
                initial_pr_stop_after_id = args.since_pr_id
            elif not args.catch_up:
                initial_pr_max_pages = 1
            state, requests_per_poll_count = _fetch_new_pr_state(
                args.repo,
                token,
                stop_after_id=initial_pr_stop_after_id,
                max_pages=initial_pr_max_pages,
            )
    except urllib.error.HTTPError as err:
        print(f"GitHub API error: {err.code} {err.reason}", file=sys.stderr)
        return RETRY_EXIT_CODE if is_retryable_http_error(err) else 1
    except urllib.error.URLError as err:
        print(f"GitHub network error: {err.reason}", file=sys.stderr)
        return RETRY_EXIT_CODE
    except Exception as err:  # noqa: BLE001
        print(f"Failed to fetch initial PR state: {err}", file=sys.stderr)
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

    if args.pr is not None:
        if args.snapshot_cursors:
            cursor_path = args.cursor_file or args.cursor_output
            if not cursor_path:
                parser.error("--snapshot-cursors requires --cursor-file")
            snapshot = {
                "review_cursor": max_id(state["reviews"]),
                "review_comment_cursor": max_id(state["review_comments"]),
                "issue_comment_cursor": max_id(state["issue_comments"]),
                "reaction_cursor": max_id(state["reactions"]),
                "pr_status": _current_pr_status(state.get("pull_request")),
            }
            _write_json_atomically(cursor_path, snapshot)
            print(json.dumps(snapshot))
            return 0

        try:
            cursor_input = _read_cursor_input(args.cursor_file)
        except (OSError, ValueError, json.JSONDecodeError) as err:
            print(f"Failed to read cursor file: {err}", file=sys.stderr)
            return 1

        review_cursor = (
            args.since_review_id
            if args.since_review_id is not None
            else (
                int(cursor_input["review_cursor"])
                if "review_cursor" in cursor_input
                else (0 if args.catch_up else max_id(state["reviews"]))
            )
        )
        review_comment_cursor = (
            args.since_review_comment_id
            if args.since_review_comment_id is not None
            else (
                int(cursor_input["review_comment_cursor"])
                if "review_comment_cursor" in cursor_input
                else (0 if args.catch_up else max_id(state["review_comments"]))
            )
        )
        issue_comment_cursor = (
            args.since_issue_comment_id
            if args.since_issue_comment_id is not None
            else (
                int(cursor_input["issue_comment_cursor"])
                if "issue_comment_cursor" in cursor_input
                else (0 if args.catch_up else max_id(state["issue_comments"]))
            )
        )
        reaction_cursor = (
            args.since_reaction_id
            if args.since_reaction_id is not None
            else (
                int(cursor_input["reaction_cursor"])
                if "reaction_cursor" in cursor_input
                else (0 if args.catch_up else max_id(state["reactions"]))
            )
        )
        pr_status = (
            args.since_pr_status
            or str(cursor_input.get("pr_status") or "")
            or _current_pr_status(state.get("pull_request"))
        )

        print(
            (
                "Watching GitHub PR %s#%s from cursors: review=%s review_comment=%s issue_comment=%s reaction=%s pr_status=%s catch_up=%s"
                % (
                    args.repo,
                    args.pr,
                    review_cursor,
                    review_comment_cursor,
                    issue_comment_cursor,
                    reaction_cursor,
                    pr_status,
                    args.catch_up,
                )
            ),
            file=sys.stderr,
        )

        initial_output, review_cursor, review_comment_cursor, issue_comment_cursor, reaction_cursor, pr_status = _render_activity(
            repo=args.repo,
            pr_number=args.pr,
            state=state,
            review_cursor=review_cursor,
            review_comment_cursor=review_comment_cursor,
            issue_comment_cursor=issue_comment_cursor,
            reaction_cursor=reaction_cursor,
            pr_status=pr_status,
            event_limit=args.event_limit,
            viewer_login=viewer_login,
            ignore_self_comments=not args.include_self_comments,
        )
        if initial_output is not None:
            (
                initial_output,
                review_cursor,
                review_comment_cursor,
                issue_comment_cursor,
                reaction_cursor,
                pr_status,
            ) = _settle_activity(
                repo=args.repo,
                pr_number=args.pr,
                token=token,
                initial_output=initial_output,
                state=state,
                review_cursor=review_cursor,
                review_comment_cursor=review_comment_cursor,
                issue_comment_cursor=issue_comment_cursor,
                reaction_cursor=reaction_cursor,
                pr_status=pr_status,
                event_limit=args.event_limit,
                viewer_login=viewer_login,
                ignore_self_comments=not args.include_self_comments,
            )
            _write_cursor_output(
                args.cursor_output or args.cursor_file,
                review_cursor=review_cursor,
                review_comment_cursor=review_comment_cursor,
                issue_comment_cursor=issue_comment_cursor,
                reaction_cursor=reaction_cursor,
                pr_status=pr_status,
            )
            print(initial_output)
            return 0
    else:
        try:
            cursor_input = _read_cursor_input(args.cursor_file)
        except (OSError, ValueError, json.JSONDecodeError) as err:
            print(f"Failed to read cursor file: {err}", file=sys.stderr)
            return 1
        pr_cursor = (
            args.since_pr_id
            if args.since_pr_id is not None
            else (
                int(cursor_input["pr_cursor"])
                if "pr_cursor" in cursor_input
                else (0 if args.catch_up else max_id(state["pull_requests"]))
            )
        )
        print(
            f"Watching GitHub new PRs in {args.repo} from cursor: pr={pr_cursor} catch_up={args.catch_up}",
            file=sys.stderr,
        )
        initial_output, pr_cursor = _render_new_pull_requests(
            repo=args.repo,
            state=state,
            pr_cursor=pr_cursor,
            event_limit=args.event_limit,
        )
        if initial_output is not None:
            _write_new_pr_cursor_output(args.cursor_output or args.cursor_file, pr_cursor=pr_cursor)
            print(initial_output)
            return 0

    while True:
        sleep_seconds = effective_interval
        if args.timeout > 0:
            remaining_timeout = args.timeout - (time.monotonic() - start)
            if remaining_timeout <= 0:
                print("Timed out while waiting for GitHub PR activity", file=sys.stderr)
                return 124
            sleep_seconds = min(sleep_seconds, remaining_timeout)

        time.sleep(sleep_seconds)

        try:
            if args.pr is not None:
                state, requests_per_poll_count = _fetch_state(args.repo, args.pr, token)
            else:
                state, requests_per_poll_count = _fetch_new_pr_state(
                    args.repo,
                    token,
                    stop_after_id=pr_cursor if pr_cursor > 0 else None,
                )
        except urllib.error.HTTPError as err:
            if not is_retryable_http_error(err):
                print(f"GitHub API error during polling: {err.code} {err.reason}", file=sys.stderr)
                return 1
            if token is None and (err.code == 429 or is_rate_limit_http_error(err)):
                print(
                    (
                        "GitHub unauthenticated polling hit a rate limit. "
                        "Authenticate with 'gh auth login' or GH_TOKEN/GITHUB_TOKEN."
                    ),
                    file=sys.stderr,
                )
                return RETRY_EXIT_CODE
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

        if args.pr is not None:
            output, review_cursor, review_comment_cursor, issue_comment_cursor, reaction_cursor, pr_status = _render_activity(
                repo=args.repo,
                pr_number=args.pr,
                state=state,
                review_cursor=review_cursor,
                review_comment_cursor=review_comment_cursor,
                issue_comment_cursor=issue_comment_cursor,
                reaction_cursor=reaction_cursor,
                pr_status=pr_status,
                event_limit=args.event_limit,
                viewer_login=viewer_login,
                ignore_self_comments=not args.include_self_comments,
            )
            if output is None:
                continue

            (
                output,
                review_cursor,
                review_comment_cursor,
                issue_comment_cursor,
                reaction_cursor,
                pr_status,
            ) = _settle_activity(
                repo=args.repo,
                pr_number=args.pr,
                token=token,
                initial_output=output,
                state=state,
                review_cursor=review_cursor,
                review_comment_cursor=review_comment_cursor,
                issue_comment_cursor=issue_comment_cursor,
                reaction_cursor=reaction_cursor,
                pr_status=pr_status,
                event_limit=args.event_limit,
                viewer_login=viewer_login,
                ignore_self_comments=not args.include_self_comments,
            )

            _write_cursor_output(
                args.cursor_output or args.cursor_file,
                review_cursor=review_cursor,
                review_comment_cursor=review_comment_cursor,
                issue_comment_cursor=issue_comment_cursor,
                reaction_cursor=reaction_cursor,
                pr_status=pr_status,
            )
        else:
            output, pr_cursor = _render_new_pull_requests(
                repo=args.repo,
                state=state,
                pr_cursor=pr_cursor,
                event_limit=args.event_limit,
            )
            if output is None:
                continue
            _write_new_pr_cursor_output(args.cursor_output or args.cursor_file, pr_cursor=pr_cursor)

        print(output)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
