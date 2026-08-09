#!/usr/bin/env python3
"""Wait until a GitHub pull request or repository receives new PR activity."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX only, which is every platform `vibe` runs the waiter on.
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None  # type: ignore[assignment]

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _github_wait_common import (  # noqa: E402
    filter_new,
    get_token,
    GitHubProtocolError,
    GitHubRequestResult,
    github_get,
    github_graphql,
    github_request,
    LAST_DELIVERY_ENV,
    list_paginated,
    list_paginated_with_count,
    max_id,
    min_interval_for_unauthenticated,
    REQUEST_TIMEOUT_SECONDS,
    requests_per_poll,
    resolve_authenticated_login,
    ResponseCache,
    retry_initial_request,
    squash,
    WATCH_ID_ENV,
)

CODEX_REVIEW_PASS_REACTION_USERS = frozenset(
    {"chatgpt-codex-connector", "chatgpt-codex-connector[bot]"}
)
CODEX_REVIEW_PASS_REACTION_CONTENT = "+1"

# Comments that only drive the review loop rather than report its result. On a
# busy PR these outnumber the real findings, and each one costs a full Agent turn.
# Anchored at BOTH ends: a trigger comment is only the command. "@codex review" is
# noise; "@author fix the timeout handling" is a person asking for work and must
# survive, because suppressing it still advances the cursor and the request would be
# lost for good rather than merely delayed.
DEFAULT_NOISE_COMMENT_PATTERNS = (
    r"^@[\w\[\]-]+\s+(review|fix|rebase|merge)\s*$",
    r"^/[\w-]+\s*$",
)
# Lifecycle transitions worth interrupting for. Draft toggles are usually the
# Agent's own doing, so they are noise in --actionable-only mode.
ACTIONABLE_PR_STATUSES = frozenset({"merged", "closed"})
STATE_FILE_VERSION = 1
# Cursors that cover a REPORTED event, held here instead of committed, next to the
# supervisor's last-delivery stamp as it read at report time. A waiter cannot see its
# own delivery -- the supervisor reads its output only after the process exits -- so
# committing them at report time loses the event whenever the service stops in
# between. A later cycle that reads a different stamp knows the report was delivered
# and promotes them; an unchanged stamp replays the report.
STAGED_KEY = "pending"
STATE_CURSOR_KEYS = (
    "review_cursor",
    "review_comment_cursor",
    "issue_comment_cursor",
    "reaction_cursor",
)
REVIEW_FINGERPRINTS_KEY = "review_fingerprints"
REVIEW_COMMENT_FINGERPRINTS_KEY = "review_comment_fingerprints"
ISSUE_COMMENT_FINGERPRINTS_KEY = "issue_comment_fingerprints"
REVIEW_THREAD_STATES_KEY = "review_thread_states"
PR_SNAPSHOT_KEY = "snapshot"
PR_FINGERPRINT_KEYS = (
    REVIEW_FINGERPRINTS_KEY,
    REVIEW_COMMENT_FINGERPRINTS_KEY,
    ISSUE_COMMENT_FINGERPRINTS_KEY,
)
# A bot review lands as a burst of inline comments plus an envelope. Re-polling a
# few times while the burst is still arriving turns it into one Agent turn instead
# of one turn per fragment that happened to cross a poll boundary.
SETTLE_MAX_ROUNDS = 3
REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $endCursor) {
        nodes { id isResolved }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


class StateFileError(RuntimeError):
    """The requested ``--state-file`` cannot do the job it was asked to do.

    Raised rather than warned about because a forever watch that keeps polling
    without usable cursors loses activity instead of reporting it, and does so
    silently: every fresh cycle re-baselines from the current PR.
    """


class StatePersistenceError(StateFileError):
    """The cursors an explicit ``--state-file`` promised could not be saved."""


class StateFileUnusableError(StateFileError):
    """An existing ``--state-file`` cannot be read, so its cursors are unknown.

    Not recoverable by starting over: re-baselining from the current PR skips
    everything that arrived after the last cursor this file did hold, and then
    overwrites the evidence. The corrupt file has to be removed deliberately.
    """


class StateFileOwnershipError(StateFileError):
    """The state file at this path belongs to a different PR, or a different watch.

    Two watches pointed at one file is a configuration error, not a state to
    recover from: each would read the other's cursors as absent, re-baseline, and
    then overwrite them, so both watches keep missing activity between cycles.
    """


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


def _compile_ignore_patterns(values: list[str] | None, *, actionable_only: bool) -> list[re.Pattern[str]]:
    patterns = list(DEFAULT_NOISE_COMMENT_PATTERNS) if actionable_only else []
    patterns.extend(values or [])
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


def _normalize_authors(values: list[str] | None) -> set[str]:
    return {value.strip().casefold() for value in (values or []) if value.strip()}


def _is_ignored_author(item: dict[str, Any], ignored_authors: set[str]) -> bool:
    if not ignored_authors:
        return False
    author = ((item.get("user") or {}).get("login")) or ""
    return str(author).casefold() in ignored_authors


def _matches_ignored_pattern(item: dict[str, Any], patterns: list[re.Pattern[str]]) -> bool:
    if not patterns:
        return False
    text = squash(item.get("body"), limit=1000)
    if not text:
        return False
    return any(pattern.search(text) for pattern in patterns)


def _is_actionable_review(review: dict[str, Any]) -> bool:
    """A review carries a verdict, or a body worth reading.

    A bodyless ``COMMENTED`` review is the envelope GitHub wraps around inline
    comments; those comments are reported on their own, so the envelope adds a
    turn and no information.
    """

    state = str(review.get("state") or "").lower()
    if state in {"changes_requested", "approved", "dismissed"}:
        return True
    return bool(squash(review.get("body")))


def _keep_item(
    item: dict[str, Any],
    *,
    ignored_authors: set[str],
    ignore_patterns: list[re.Pattern[str]],
) -> bool:
    return not _is_ignored_author(item, ignored_authors) and not _matches_ignored_pattern(item, ignore_patterns)


def _visible_activity_items(
    items: list[dict[str, Any]],
    *,
    viewer_login: str | None,
    ignore_self_comments: bool,
    ignored_authors: set[str],
    ignore_patterns: list[re.Pattern[str]],
) -> list[dict[str, Any]]:
    kept = (
        items
        if not ignore_self_comments
        else [item for item in items if not _is_self_authored_comment(item, viewer_login)]
    )
    return [
        item
        for item in kept
        if _keep_item(item, ignored_authors=ignored_authors, ignore_patterns=ignore_patterns)
    ]


def _is_codex_pass_reaction(reaction: dict[str, Any]) -> bool:
    author = ((reaction.get("user") or {}).get("login")) or ""
    content = str(reaction.get("content") or "")
    return author in CODEX_REVIEW_PASS_REACTION_USERS and content == CODEX_REVIEW_PASS_REACTION_CONTENT


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


def _current_pr_head_sha(pr: dict[str, Any] | None) -> str:
    if not isinstance(pr, dict):
        return "unknown"
    head = pr.get("head")
    if not isinstance(head, dict):
        return "unknown"
    sha = str(head.get("sha") or "").strip()
    return sha or "unknown"


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


def _format_pr_head_event(pr: dict[str, Any], previous_head: str, current_head: str) -> str:
    pr_number = pr.get("number")
    url = pr.get("html_url") or ""
    return (
        f"- pr_head #{pr_number} {previous_head} -> {current_head}\n"
        "  Pull request head changed; confirm or trigger Codex review for the new exact head.\n"
        f"  {url}"
    )


def _format_review_thread_event(
    pr: dict[str, Any],
    thread_id: str,
    previous_resolved: bool | None,
    current_resolved: bool | None,
) -> str:
    url = pr.get("html_url") or ""

    def _label(value: bool | None) -> str:
        if value is None:
            return "absent"
        return "resolved" if value else "unresolved"

    previous = _label(previous_resolved)
    current = _label(current_resolved)
    return (
        f"- review_thread {thread_id} {previous} -> {current}\n"
        "  Review thread state changed; re-evaluate unresolved threads across the entire PR.\n"
        f"  {url}"
    )


def _format_pull_request(pr: dict[str, Any]) -> str:
    pr_number = pr.get("number")
    author = ((pr.get("user") or {}).get("login")) or "unknown"
    state = str(pr.get("state") or "open").lower()
    title = squash(pr.get("title") or "")
    url = pr.get("html_url") or ""
    return f"- pull_request #{pr_number} by {author} ({state})\n  {title}\n  {url}"


def _fetch_review_threads(
    repo: str,
    pr_number: int,
    token: str | None,
) -> tuple[list[dict[str, Any]], int]:
    if token is None:
        return [], 0
    try:
        owner, repo_name = repo.split("/", 1)
    except ValueError as err:
        raise RuntimeError(f"Invalid repository name: {repo}") from err

    threads: list[dict[str, Any]] = []
    end_cursor: str | None = None
    request_count = 0
    while True:
        data = github_graphql(
            REVIEW_THREADS_QUERY,
            {
                "owner": owner,
                "repo": repo_name,
                "number": pr_number,
                "endCursor": end_cursor,
            },
            token,
        )
        request_count += 1
        repository = data.get("repository")
        pull_request = repository.get("pullRequest") if isinstance(repository, dict) else None
        connection = pull_request.get("reviewThreads") if isinstance(pull_request, dict) else None
        if not isinstance(connection, dict):
            raise GitHubProtocolError("GitHub GraphQL response has no reviewThreads connection")
        nodes = connection.get("nodes")
        if not isinstance(nodes, list):
            raise GitHubProtocolError("GitHub GraphQL reviewThreads response has no nodes list")
        threads.extend(node for node in nodes if isinstance(node, dict))
        page_info = connection.get("pageInfo")
        if not isinstance(page_info, dict) or page_info.get("hasNextPage") is not True:
            break
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise GitHubProtocolError("GitHub GraphQL reviewThreads page has no endCursor")
        end_cursor = next_cursor
    return threads, request_count


def _fetch_state(
    repo: str,
    pr_number: int,
    token: str | None,
    *,
    cache: ResponseCache | None = None,
) -> tuple[dict[str, Any], int]:
    encoded_repo = urllib.parse.quote(repo, safe="/")
    base_url = f"https://api.github.com/repos/{encoded_repo}"
    pull_request = github_get(f"{base_url}/pulls/{pr_number}", token, cache=cache)
    # The reviews endpoint ignores sort/direction (verified against api.github.com:
    # direction=desc returns the same ascending ids), so there is no way to page it
    # newest-first and no `since` support either. An unchanged review list stays
    # cheap by revalidating to 304 rather than by fetching less.
    reviews, review_requests = list_paginated_with_count(
        f"{base_url}/pulls/{pr_number}/reviews",
        token,
        cache=cache,
    )
    # Snapshot equality is the wake gate, so every mutable collection must be a
    # complete current view. A `since` slice cannot prove that an older item was
    # removed and would turn the normalized snapshot into an append-only cache.
    review_comments, review_comment_requests = list_paginated_with_count(
        f"{base_url}/pulls/{pr_number}/comments",
        token,
        cache=cache,
    )
    issue_comments, issue_comment_requests = list_paginated_with_count(
        f"{base_url}/issues/{pr_number}/comments",
        token,
        cache=cache,
    )
    # Only the Codex pass reaction is ever reported, and this endpoint filters by
    # content server-side, so every other reaction on the PR body stays home.
    reactions, reaction_requests = list_paginated_with_count(
        f"{base_url}/issues/{pr_number}/reactions"
        f"?content={urllib.parse.quote(CODEX_REVIEW_PASS_REACTION_CONTENT)}",
        token,
        cache=cache,
    )
    review_threads, review_thread_requests = _fetch_review_threads(repo, pr_number, token)
    return (
        {
            "pull_request": pull_request,
            "reviews": reviews,
            "review_comments": review_comments,
            "issue_comments": issue_comments,
            "reactions": reactions,
            "review_threads": review_threads,
        },
        (
            1
            + review_requests
            + review_comment_requests
            + issue_comment_requests
            + reaction_requests
            + review_thread_requests
        ),
    )


def _fetch_new_pr_state(
    repo: str,
    token: str | None,
    *,
    stop_after_id: int | None = None,
    max_pages: int | None = None,
    cache: ResponseCache | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    encoded_repo = urllib.parse.quote(repo, safe="/")
    pull_requests, request_count = list_paginated_with_count(
        f"https://api.github.com/repos/{encoded_repo}/pulls?state=all&sort=created&direction=desc",
        token,
        stop_after_id=stop_after_id,
        max_pages=max_pages,
        cache=cache,
    )
    return {"pull_requests": pull_requests}, request_count


def _render_activity(
    *,
    repo: str,
    pr_number: int,
    state: dict[str, Any],
    review_cursor: int,
    review_comment_cursor: int,
    issue_comment_cursor: int,
    reaction_cursor: int,
    pr_status: str,
    head_sha: str,
    snapshot: dict[str, Any],
    event_limit: int,
    viewer_login: str | None = None,
    ignore_self_comments: bool = True,
    actionable_only: bool = False,
    ignored_authors: set[str] | None = None,
    ignore_patterns: list[re.Pattern[str]] | None = None,
    review_fingerprints: dict[str, str] | None = None,
    review_comment_fingerprints: dict[str, str] | None = None,
    issue_comment_fingerprints: dict[str, str] | None = None,
    review_thread_states: dict[str, bool] | None = None,
    review_threads_available: bool = True,
) -> tuple[str | None, int, int, int, int, str, str, dict[str, Any]]:
    ignored_authors = ignored_authors or set()
    ignore_patterns = ignore_patterns or []
    current_snapshot = _normalized_pr_snapshot(
        state,
        viewer_login=viewer_login,
        ignore_self_comments=ignore_self_comments,
        actionable_only=actionable_only,
        ignored_authors=ignored_authors,
        ignore_patterns=ignore_patterns,
        committed_snapshot=snapshot,
        review_threads_available=review_threads_available,
    )
    current_pr_status = _current_pr_status(state.get("pull_request"))
    current_head_sha = _current_pr_head_sha(state.get("pull_request"))
    review_threads = state.get("review_threads")
    current_review_thread_states = _review_thread_state_map(
        review_threads if isinstance(review_threads, list) else []
    )
    review_thread_changes = _review_thread_state_changes(
        current_review_thread_states,
        review_thread_states or {},
    )
    new_reviews = _filter_review_changes(state["reviews"], review_cursor, review_fingerprints or {})
    new_review_comments = _filter_comment_changes(
        state["review_comments"],
        review_comment_cursor,
        review_comment_fingerprints or {},
    )
    new_issue_comments = _filter_comment_changes(
        state["issue_comments"],
        issue_comment_cursor,
        issue_comment_fingerprints or {},
    )

    def _visible(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _visible_activity_items(
            items,
            viewer_login=viewer_login,
            ignore_self_comments=ignore_self_comments,
            ignored_authors=ignored_authors,
            ignore_patterns=ignore_patterns,
        )

    visible_reviews = _visible(new_reviews)
    if actionable_only:
        visible_reviews = [review for review in visible_reviews if _is_actionable_review(review)]
    visible_review_comments = _visible(new_review_comments)
    visible_issue_comments = _visible(new_issue_comments)
    new_reactions = [
        reaction
        for reaction in filter_new(state["reactions"], reaction_cursor)
        if _is_codex_pass_reaction(reaction)
    ]
    has_pr_status_event = current_pr_status != pr_status
    has_head_event = current_head_sha != head_sha

    next_review_cursor = max(review_cursor, max_id(state["reviews"]))
    next_review_comment_cursor = max(review_comment_cursor, max_id(state["review_comments"]))
    next_issue_comment_cursor = max(issue_comment_cursor, max_id(state["issue_comments"]))
    next_reaction_cursor = max(reaction_cursor, max_id(state["reactions"]))
    next_pr_status = current_pr_status
    next_head_sha = current_head_sha

    # This is the only wake decision. Per-field comparisons below are descriptors:
    # they explain a changed canonical snapshot but cannot suppress one or create a
    # wake on their own.
    if current_snapshot == snapshot:
        return (
            None,
            next_review_cursor,
            next_review_comment_cursor,
            next_issue_comment_cursor,
            next_reaction_cursor,
            next_pr_status,
            next_head_sha,
            current_snapshot,
        )

    render_pr_status_event = has_pr_status_event and (
        not actionable_only or current_pr_status in ACTIONABLE_PR_STATUSES
    )

    required_events: list[str] = []
    if has_head_event and isinstance(state.get("pull_request"), dict):
        required_events.append(_format_pr_head_event(state["pull_request"], head_sha, current_head_sha))
    if render_pr_status_event and isinstance(state.get("pull_request"), dict):
        required_events.append(_format_pr_status_event(state["pull_request"], pr_status, current_pr_status))
    # A Codex +1 is durable pass evidence, not just another activity line. It must
    # survive a small --event-limit even when a large review batch lands with it.
    required_events.extend(_format_reaction(reaction) for reaction in new_reactions)
    if isinstance(state.get("pull_request"), dict):
        required_events.extend(
            _format_review_thread_event(state["pull_request"], *change)
            for change in review_thread_changes
        )
    optional_events = [_format_review(review) for review in visible_reviews]
    optional_events.extend(_format_review_comment(comment) for comment in visible_review_comments)
    optional_events.extend(_format_issue_comment(comment) for comment in visible_issue_comments)

    if not (required_events or optional_events):
        required_events.append(
            "- pr_snapshot changed\n"
            "  Gate-relevant PR state changed; re-evaluate the exact head, review verdict, "
            "and all unresolved threads."
        )

    lines = [f"GitHub PR activity detected for {repo}#{pr_number}"]

    visible_limit = max(event_limit, len(required_events), 1)
    visible_optional = optional_events[: max(0, visible_limit - len(required_events))]
    selected_events = [*required_events, *visible_optional]
    for entry in selected_events:
        lines.append(entry)

    total_events = len(required_events) + len(optional_events)
    if total_events > len(selected_events):
        lines.append(f"- {total_events - len(selected_events)} additional event(s) omitted")

    return (
        "\n".join(lines),
        next_review_cursor,
        next_review_comment_cursor,
        next_issue_comment_cursor,
        next_reaction_cursor,
        next_pr_status,
        next_head_sha,
        current_snapshot,
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
    head_sha: str,
) -> None:
    if not path:
        return

    payload = {
        "review_cursor": review_cursor,
        "review_comment_cursor": review_comment_cursor,
        "issue_comment_cursor": issue_comment_cursor,
        "reaction_cursor": reaction_cursor,
        "pr_status": pr_status,
        "head_sha": head_sha,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _review_fingerprint(review: dict[str, Any]) -> str:
    """Capture fields GitHub can update without allocating a new review id."""

    return "|".join(
        str(review.get(field) or "")
        for field in ("state", "updated_at", "submitted_at", "body")
    )


def _review_fingerprint_map(reviews: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(review["id"]): _review_fingerprint(review)
        for review in reviews
        if isinstance(review.get("id"), int) and not isinstance(review.get("id"), bool)
    }


def _comment_fingerprint(comment: dict[str, Any]) -> str:
    """Capture fields GitHub can edit without changing a comment id."""

    return "|".join(str(comment.get(field) or "") for field in ("updated_at", "body"))


def _comment_fingerprint_map(comments: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(comment["id"]): _comment_fingerprint(comment)
        for comment in comments
        if isinstance(comment.get("id"), int) and not isinstance(comment.get("id"), bool)
    }


def _review_thread_state_map(threads: list[dict[str, Any]]) -> dict[str, bool]:
    return {
        str(thread["id"]): bool(thread["isResolved"])
        for thread in threads
        if isinstance(thread.get("id"), str) and isinstance(thread.get("isResolved"), bool)
    }


def _normalized_item_map(
    items: list[dict[str, Any]],
    *,
    fields: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = item.get("id")
        if not isinstance(item_id, (int, str)) or isinstance(item_id, bool):
            continue
        author = ((item.get("user") or {}).get("login")) or ""
        normalized[str(item_id)] = {
            "author": str(author),
            **{field: item.get(field) for field in fields},
        }
    return normalized


def _normalized_pr_snapshot(
    state: dict[str, Any],
    *,
    viewer_login: str | None = None,
    ignore_self_comments: bool = True,
    actionable_only: bool = False,
    ignored_authors: set[str] | None = None,
    ignore_patterns: list[re.Pattern[str]] | None = None,
    committed_snapshot: dict[str, Any] | None = None,
    review_threads_available: bool = True,
) -> dict[str, Any]:
    """Return the complete gate-relevant PR snapshot without volatile fields."""

    ignored_authors = ignored_authors or set()
    ignore_patterns = ignore_patterns or []

    def _visible(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _visible_activity_items(
            items,
            viewer_login=viewer_login,
            ignore_self_comments=ignore_self_comments,
            ignored_authors=ignored_authors,
            ignore_patterns=ignore_patterns,
        )

    reviews = _visible(state["reviews"])
    if actionable_only:
        reviews = [review for review in reviews if _is_actionable_review(review)]
    review_comments = _visible(state["review_comments"])
    issue_comments = _visible(state["issue_comments"])
    reactions = [reaction for reaction in state["reactions"] if _is_codex_pass_reaction(reaction)]
    normalized_reactions = _normalized_item_map(reactions, fields=("content",))
    for reaction in normalized_reactions.values():
        reaction["author"] = "chatgpt-codex-connector"

    if review_threads_available:
        review_threads = _review_thread_state_map(state["review_threads"])
    else:
        saved_threads = (committed_snapshot or {}).get("review_threads")
        review_threads = saved_threads if isinstance(saved_threads, dict) else {}
    pr_status = _current_pr_status(state.get("pull_request"))
    if actionable_only and pr_status not in ACTIONABLE_PR_STATUSES:
        pr_status = "open"

    return {
        "pull_request": {
            "status": pr_status,
            "head_sha": _current_pr_head_sha(state.get("pull_request")),
        },
        "reviews": _normalized_item_map(
            reviews,
            fields=("state", "body", "commit_id"),
        ),
        "review_comments": _normalized_item_map(
            review_comments,
            fields=("body", "path"),
        ),
        "issue_comments": _normalized_item_map(issue_comments, fields=("body",)),
        "reactions": normalized_reactions,
        "review_threads": review_threads,
    }


def _review_thread_state_changes(
    current: dict[str, bool],
    baseline: dict[str, bool],
) -> list[tuple[str, bool | None, bool | None]]:
    return [
        (thread_id, baseline.get(thread_id), current.get(thread_id))
        for thread_id in sorted(current.keys() | baseline.keys())
        if baseline.get(thread_id) != current.get(thread_id)
    ]


def _filter_review_changes(
    reviews: list[dict[str, Any]],
    cursor: int,
    fingerprints: dict[str, str],
) -> list[dict[str, Any]]:
    """Return new reviews and updates to reviews already covered by the id cursor."""

    changed: list[dict[str, Any]] = []
    for review in reviews:
        review_id = review.get("id")
        if not isinstance(review_id, int) or isinstance(review_id, bool):
            continue
        saved_fingerprint = fingerprints.get(str(review_id))
        if review_id > cursor or (saved_fingerprint is not None and saved_fingerprint != _review_fingerprint(review)):
            changed.append(review)
    return changed


def _filter_comment_changes(
    comments: list[dict[str, Any]],
    cursor: int,
    fingerprints: dict[str, str],
) -> list[dict[str, Any]]:
    """Return new comments and edits to comments already covered by the id cursor."""

    changed: list[dict[str, Any]] = []
    for comment in comments:
        comment_id = comment.get("id")
        if not isinstance(comment_id, int) or isinstance(comment_id, bool):
            continue
        saved_fingerprint = fingerprints.get(str(comment_id))
        if comment_id > cursor or (
            saved_fingerprint is not None and saved_fingerprint != _comment_fingerprint(comment)
        ):
            changed.append(comment)
    return changed


def _write_new_pr_cursor_output(path: str | None, *, pr_cursor: int) -> None:
    if not path:
        return

    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"pr_cursor": pr_cursor}, handle)


def _watch_identity(args: argparse.Namespace) -> str:
    """A stable digest of the options that decide what this watch reports.

    Ownership by repo and PR alone cannot tell two watches on the same PR apart --
    one ``--actionable-only``, one meant to report everything -- and a shared state
    file then lets whichever polls first advance the cursors past an event the other
    would have reported. That event is gone for good rather than merely late, so the
    filters belong in the identity.

    Only the report-shaping options are in here. ``--interval``, ``--timeout`` and
    ``--settle`` change pacing, not which activity counts, and two watches differing
    only in those can share cursors without losing anything.
    """

    material = json.dumps(
        {
            "mode": "new-prs" if args.new_prs else "pr",
            "actionable_only": bool(args.actionable_only),
            "include_self_comments": bool(args.include_self_comments),
            "ignore_authors": sorted(_normalize_authors(args.ignore_author)),
            "ignore_comment_patterns": sorted(set(args.ignore_comment_pattern or [])),
        },
        sort_keys=True,
    )
    return hashlib.sha256(f"wait_pr/{STATE_FILE_VERSION}/{material}".encode()).hexdigest()[:16]


def _managed_watch_id() -> str | None:
    """The id of the ``vibe watch`` this waiter is a cycle of, when there is one.

    Two separately managed watches on the same PR with the same filters are
    indistinguishable by configuration alone, and sharing a state file means
    whichever polls first advances the cursors past events the other never
    reported. The supervisor hands the id down, so the owner can be exact. A manual
    run has none, which is not a conflict -- only proof of one is.
    """

    value = os.environ.get(WATCH_ID_ENV, "").strip()
    return value or None


def _owner_conflict(
    owner: tuple[Any, Any, Any, Any] | None,
    *,
    repo: str,
    pr_number: int | None,
    watch_identity: str | None,
    watch_id: str | None,
    allow_cursorless_watch_identity_change: bool = False,
) -> str | None:
    """Why ``owner`` is somebody else's claim on the path, or ``None`` when it is ours.

    A state file written before identities existed carries no owner, and an absent
    owner is compatible with every run -- that is the file a managed watch adopts.

    A file that DOES name an owner is the opposite: the burden is on this run to
    prove it is that owner, and a manual run cannot. Letting it through would be
    silent data loss rather than a mere sharing of the path -- the manual run polls,
    reports to a terminal nobody is reading, and ``_write_state_file`` stamps
    ``owner: null`` over the managed claim while advancing the cursors past that
    activity. The managed watch then adopts its own file back as ownerless and
    resumes from cursors covering events its Agent follow-up never received.
    """

    if owner is None:
        return None
    saved_repo, saved_pr, saved_watch, saved_owner = owner
    if saved_repo != repo or saved_pr != pr_number:
        return f"belongs to {saved_repo}#{saved_pr}, not {repo}#{pr_number}"
    if saved_watch is not None and watch_identity is not None and saved_watch != watch_identity:
        if allow_cursorless_watch_identity_change and saved_owner == watch_id:
            return None
        return (
            f"belongs to another watch on {saved_repo}#{saved_pr} with different "
            "reporting filters"
        )
    if saved_owner is not None and saved_owner != watch_id:
        if watch_id is None:
            return (
                f"belongs to watch {saved_owner}; this run is not managed by that "
                "watch. Use a different --state-file."
            )
        return f"belongs to watch {saved_owner}, not watch {watch_id}"
    return None


def _cursorless_state_file(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    return not any(
        key in payload
        for key in (
            *STATE_CURSOR_KEYS,
            "pr_cursor",
            "pr_status",
            "head_sha",
            *PR_FINGERPRINT_KEYS,
            REVIEW_THREAD_STATES_KEY,
            PR_SNAPSHOT_KEY,
            STAGED_KEY,
        )
    )


def _load_state_file(
    path: str | None,
    *,
    repo: str,
    pr_number: int | None,
    watch_identity: str | None = None,
    watch_id: str | None = None,
) -> dict[str, Any]:
    """Read cursors left behind by an earlier run of this same waiter.

    A `--forever` watch runs the waiter once per cycle, so without this the next
    cycle re-baselines from whatever the PR looks like at startup and silently
    swallows everything that arrived between the previous cycle's exit and that
    snapshot.
    """

    if not path:
        return {}

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise StateFileUnusableError(f"State file {path} cannot be read: {err}") from err

    # An empty file is the claim below caught between its exclusive create and its
    # first write, so no cursor was ever recorded in it and there is none to lose.
    # Anything else that will not parse HAD cursors, and how far they reached is now
    # unknown -- which is why this is terminal rather than a fresh baseline.
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except ValueError as err:
        raise StateFileUnusableError(f"State file {path} is corrupt: {err}") from err
    if not isinstance(payload, dict) or payload.get("version") != STATE_FILE_VERSION:
        raise StateFileUnusableError(f"State file {path} is not in a recognised format")
    # Resuming from somebody else's cursors would skip the history they cover, and
    # carrying on would overwrite them on the first cursor advance -- so this is
    # terminal rather than a fresh baseline. A file left behind by another watch has
    # to be removed, or that watch given its own path, deliberately.
    conflict = _owner_conflict(
        (payload.get("repo"), payload.get("pr"), payload.get("watch"), payload.get("owner")),
        repo=repo,
        pr_number=pr_number,
        watch_identity=watch_identity,
        watch_id=watch_id,
        allow_cursorless_watch_identity_change=_cursorless_state_file(Path(path)) if path else False,
    )
    if conflict is not None:
        raise StateFileOwnershipError(f"State file {path} {conflict}")
    return payload


def _state_file_scratch(target: Path) -> tuple[int, str]:
    """An exclusively created scratch file beside ``target``.

    ``mkstemp`` rather than a derived name such as ``<state-file>.tmp``: this runs
    in a directory of persisted user state, and a deterministic path both
    truncates whatever already occupies it and lets two waiters sharing a
    directory scribble over each other's half-written file.
    """

    return tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")


def _state_file_owner(target: Path) -> tuple[Any, Any, Any, Any] | None:
    """Which watch the file at ``target`` currently claims, or ``None`` if it says nothing.

    Missing and unusable files both answer ``None``, matching ``_load_state_file``:
    there is no owner to respect, so the caller may take the path.
    """

    try:
        with target.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != STATE_FILE_VERSION:
        return None
    return payload.get("repo"), payload.get("pr"), payload.get("watch"), payload.get("owner")


def _claim_state_file(
    target: Path,
    *,
    repo: str,
    pr_number: int | None,
    watch_identity: str | None = None,
    watch_id: str | None = None,
) -> bool:
    """Take a currently missing state file for this PR, atomically.

    ``O_EXCL`` is the whole point: two waiters starting together both see no file,
    and exactly one of them creates it. The loser reads the winner's claim and stops
    on the ownership check instead of quietly sharing the path and overwriting the
    winner's cursors on its next advance -- a silent gap in the review loop.

    Returns False when somebody else got there first; the caller then treats the path
    as pre-existing and lets ``_load_state_file`` judge the owner.
    """

    try:
        handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError as err:
        raise StatePersistenceError(f"Cannot write state file {target}: {err}") from err

    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "version": STATE_FILE_VERSION,
                    "repo": repo,
                    "pr": pr_number,
                    "watch": watch_identity,
                    "owner": watch_id,
                },
                stream,
            )
    except OSError as err:
        raise StatePersistenceError(f"Cannot write state file {target}: {err}") from err
    return True


@contextmanager
def _state_file_lock(target: Path) -> Iterator[None]:
    """Serialize the ownership decision on ``target`` across processes.

    Deciding who owns a state file is read-then-write, and two managed watches that
    interleave those halves both conclude the path is theirs. A sidecar lock file
    makes the decision one step. It is a separate file precisely because the state
    file itself is replaced rather than rewritten: a lock held on an inode that has
    since been renamed away guards nothing.

    Best effort by design. A filesystem without working locks, or a directory that
    refuses the lock file, must not stop the watch -- the write probe and the
    ownership re-check on every replace still stand.
    """

    if fcntl is None:
        yield
        return

    handle = None
    try:
        handle = os.open(target.with_name(f"{target.name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
    except OSError:
        if handle is not None:
            os.close(handle)
        yield
        return

    try:
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


def _adopt_state_file(
    target: Path,
    *,
    repo: str,
    pr_number: int | None,
    watch_id: str,
    watch_identity: str | None,
) -> bool:
    """Stamp this managed watch onto an ownerless state file, keeping its cursors.

    A file written before owners existed, or by a manual run, names no owner -- and an
    absent owner is compatible with every managed watch. Two of them would therefore
    both adopt the same path, poll, and overwrite each other's cursors, skipping
    activity for good. Claiming the name here, under the lock and before any polling,
    turns that into a conflict the loser sees while it can still be told about it.

    An EMPTY file is the same hazard wearing different clothes: it is a claim caught
    between its exclusive create and its first write, so ``_load_state_file`` reads it
    as a fresh baseline with no cursors to lose. Left as it is, it names no owner
    either, and two managed watches pointed at that path both clear this preflight and
    poll -- so finish the claim the interrupted run started rather than falling through
    to a byte-for-byte rewrite that preserves the emptiness and the ambiguity with it.

    Returns False when the file says nothing usable, leaving it to the caller's probe.
    """

    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return False

    if not raw.strip():
        payload: dict[str, Any] = {
            "version": STATE_FILE_VERSION,
            "repo": repo,
            "pr": pr_number,
        }
    else:
        try:
            payload = json.loads(raw)
        except ValueError:
            return False
        if not isinstance(payload, dict) or payload.get("version") != STATE_FILE_VERSION:
            return False

    payload["owner"] = watch_id
    if payload.get("watch") is None and watch_identity is not None:
        payload["watch"] = watch_identity

    scratch = None
    try:
        handle, scratch = _state_file_scratch(target)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        os.replace(scratch, target)
        scratch = None
    except OSError as err:
        raise StatePersistenceError(f"Cannot write state file {target}: {err}") from err
    finally:
        if scratch is not None:
            try:
                os.unlink(scratch)
            except OSError:
                pass
    return True


def _verify_state_file_writable(
    path: str | None,
    *,
    repo: str,
    pr_number: int | None,
    watch_identity: str | None = None,
    watch_id: str | None = None,
) -> None:
    """Claim the requested state file, and fail before the first poll if it is unusable.

    A forever watch only discovers a read-only parent directory when the cycle it
    spent minutes on tries to save its cursors, and by then the activity that
    cycle observed is already unrecoverable.

    A missing state file is created here holding nothing but this PR's ownership, so
    the path is owned before any polling starts rather than after the first cursor
    advance. A managed first cycle must then pass ``--catch-up`` or seed a complete
    cursor set before polling; it must never silently baseline from the current PR.

    An existing file that names no owner is adopted here too, cursors intact, because
    an absent owner is compatible with every managed watch and would otherwise let two
    of them share the path. Both steps happen under a lock on the path, so the
    read-then-write that decides ownership cannot interleave with another waiter's.

    A file that belongs to somebody else is left untouched and unprobed, and this
    returns without writing anything -- see the conflict branch below.

    For a file that already exists the probe is the whole write, ``os.replace``
    included, because that is the step persistence actually depends on and the step a
    sibling-creation check cannot speak for: a target that is a directory, or one in a
    sticky-bit directory owned by somebody else, accepts new siblings all day and
    still refuses the rename. It is rewritten with the bytes it already holds, so a
    watch resuming from good cursors keeps them either way.
    """

    if not path:
        return

    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise StatePersistenceError(f"Cannot write state file {path}: {err}") from err

    with _state_file_lock(target):
        try:
            exists = target.exists()
        except OSError as err:
            raise StatePersistenceError(f"Cannot write state file {path}: {err}") from err

        if not exists and _claim_state_file(
            target,
            repo=repo,
            pr_number=pr_number,
            watch_identity=watch_identity,
            watch_id=watch_id,
        ):
            # Creating the real file in the real directory is the write probe, and the
            # rename in later cycles lands on a file this process owns.
            return

        # Somebody else's file is not ours to touch AT ALL -- not to adopt, and not to
        # probe. The probe below is an ``os.replace`` of bytes read a moment earlier,
        # and the owner's own cursor writes do NOT take this lock, so probing a foreign
        # path can land stale bytes over a cursor it has just advanced or a ``pending``
        # handoff it is mid-way through, making the owning watch replay or lose
        # activity. Returning here costs nothing: ``_load_state_file`` refuses the same
        # file moments later, with the same conflict, before a single poll.
        conflict = _owner_conflict(
            _state_file_owner(target),
            repo=repo,
            pr_number=pr_number,
            watch_identity=watch_identity,
            watch_id=watch_id,
            allow_cursorless_watch_identity_change=watch_id is not None and _cursorless_state_file(target),
        )
        if conflict is not None:
            return

        # An ownerless file is adopted, not merely accepted: taking the name is what
        # makes a second managed watch on the same path fail instead of sharing it.
        if watch_id is not None and _adopt_state_file(
            target,
            repo=repo,
            pr_number=pr_number,
            watch_id=watch_id,
            watch_identity=watch_identity,
        ):
            # Rewriting the real file is the write probe, as the claim is above.
            return

        try:
            existing = target.read_bytes()
        except OSError as err:
            raise StatePersistenceError(f"Cannot write state file {path}: {err}") from err

        scratch = None
        try:
            handle, scratch = _state_file_scratch(target)
            with os.fdopen(handle, "wb") as stream:
                stream.write(existing)
            os.replace(scratch, target)
            scratch = None
        except OSError as err:
            raise StatePersistenceError(f"Cannot write state file {path}: {err}") from err
        finally:
            if scratch is not None:
                try:
                    os.unlink(scratch)
                except OSError:
                    pass


def _write_state_file(
    path: str | None,
    *,
    repo: str,
    pr_number: int | None,
    watch_identity: str | None = None,
    watch_id: str | None = None,
    **fields: Any,
) -> None:
    if not path:
        return

    payload = {
        "version": STATE_FILE_VERSION,
        "repo": repo,
        "pr": pr_number,
        "watch": watch_identity,
        "owner": watch_id,
        **fields,
    }
    target = Path(path)
    scratch = None
    # Re-checked on every replace, not just at startup: the claim can lose a race with
    # a waiter that created the file in the same instant, or a person can point another
    # watch at the same path mid-run. Clobbering that watch's cursors would make it
    # re-baseline and skip real activity, so hand the path back and stop instead.
    conflict = _owner_conflict(
        _state_file_owner(target),
        repo=repo,
        pr_number=pr_number,
        watch_identity=watch_identity,
        watch_id=watch_id,
        allow_cursorless_watch_identity_change=watch_id is not None and _cursorless_state_file(target),
    )
    if conflict is not None:
        raise StateFileOwnershipError(f"State file {path} now {conflict}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Written aside and moved into place: a cycle killed mid-write must not
        # leave a truncated file that the next cycle has to discard.
        handle, scratch = _state_file_scratch(target)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        os.replace(scratch, target)
        scratch = None
    except OSError as err:
        # Terminal, not a warning. The caller asked for cursors that survive the
        # process; continuing without them lets the next cycle re-baseline from the
        # current PR and silently drop everything that arrived in between, which is
        # the exact loss ``--state-file`` exists to prevent.
        raise StatePersistenceError(f"Could not write state file {path}: {err}") from err
    finally:
        # A failed write must not leave its scratch file behind in the state dir.
        if scratch is not None:
            try:
                os.unlink(scratch)
            except OSError:
                pass


def _token_fingerprint(token: str | None) -> str | None:
    """Which credentials a cached ``viewer_login`` was resolved under.

    A one-way digest, never the token itself: the state file lives on disk next to
    the cursors and has no business holding a credential. It exists so a resumed
    cycle can tell "same account, reuse the saved login" from "the token now
    belongs to someone else", which otherwise filtered the wrong author's
    comments out of the review loop.
    """

    if not token:
        return None
    return hashlib.sha256(f"wait_pr/{STATE_FILE_VERSION}/{token}".encode()).hexdigest()[:16]


def _saved_int(saved: dict[str, Any], key: str) -> int | None:
    value = saved.get(key)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _saved_str(saved: dict[str, Any], key: str) -> str | None:
    value = saved.get(key)
    return value if isinstance(value, str) and value else None


def _saved_fingerprints(saved: dict[str, Any], key: str) -> dict[str, str]:
    value = saved.get(key)
    if not isinstance(value, dict):
        return {}
    return {str(item_key): item for item_key, item in value.items() if isinstance(item, str)}


def _saved_review_thread_states(saved: dict[str, Any]) -> dict[str, bool]:
    value = saved.get(REVIEW_THREAD_STATES_KEY)
    if not isinstance(value, dict):
        return {}
    return {
        str(item_key): item
        for item_key, item in value.items()
        if isinstance(item, bool)
    }


def _saved_snapshot(saved: dict[str, Any]) -> dict[str, Any]:
    value = saved.get(PR_SNAPSHOT_KEY)
    return value if isinstance(value, dict) else {}


def _missing_pr_baselines(saved: dict[str, Any]) -> list[str]:
    missing = []
    if _saved_str(saved, "head_sha") is None:
        missing.append("head_sha")
    for key in PR_FINGERPRINT_KEYS:
        value = saved.get(key)
        if not isinstance(value, dict) or any(not isinstance(item, str) for item in value.values()):
            missing.append(key)
    thread_states = saved.get(REVIEW_THREAD_STATES_KEY)
    if not isinstance(thread_states, dict) or any(
        not isinstance(item, bool) for item in thread_states.values()
    ):
        missing.append(REVIEW_THREAD_STATES_KEY)
    if not isinstance(saved.get(PR_SNAPSHOT_KEY), dict):
        missing.append(PR_SNAPSHOT_KEY)
    return missing


def _last_delivery() -> str | None:
    """When the supervisor last queued a report from this watch, as it sees it.

    ``None`` for a manual run, which has no supervisor -- and needs none, because
    there printing the report IS delivering it and nothing is ever staged.
    """

    return os.environ.get(LAST_DELIVERY_ENV, "").strip() or None


def _staged_replay_output(saved: dict[str, Any], delivery: str | None) -> str | None:
    staged = saved.get(STAGED_KEY)
    if not isinstance(staged, dict):
        return None
    if not isinstance(staged.get("cursors"), dict):
        raise StateFileUnusableError("Pending waiter transaction has no usable cursor state")
    output = staged.get("output")
    if staged.get("delivered_after") == delivery and isinstance(output, str) and output:
        return output
    return None


def _resolve_staged_state(
    path: str | None,
    saved: dict[str, Any],
    *,
    delivery: str | None,
    repo: str,
    pr_number: int | None,
    watch_identity: str | None,
    watch_id: str | None,
) -> tuple[dict[str, Any], str | None]:
    """Promote an acknowledged transaction or replay its persisted output.

    Promoted when the supervisor's last-delivery stamp has moved since the report was
    staged: the report was queued, so polling may start after it. When the stamp is
    unchanged, return the stored rendered report without touching the pending block.
    Persisting both halves makes replay independent of mutable GitHub objects.
    """

    staged = saved.get(STAGED_KEY)
    if not isinstance(staged, dict):
        return saved, None
    cursors = staged.get("cursors")
    if not isinstance(cursors, dict):
        raise StateFileUnusableError("Pending waiter transaction has no usable cursor state")
    replay_output = _staged_replay_output(saved, delivery)
    delivered = staged.get("delivered_after") != delivery

    if replay_output is not None:
        print("An earlier report was never delivered; replaying its persisted output.", file=sys.stderr)
        return saved, replay_output

    resolved = {key: value for key, value in saved.items() if key != STAGED_KEY}
    if delivered:
        resolved.update(cursors)
    print(
        (
            "An earlier report was delivered; advancing past it."
            if delivered
            else "Legacy pending state has no report payload; reconstructing from committed cursors."
        ),
        file=sys.stderr,
    )
    fields = {
        key: value
        for key, value in resolved.items()
        if key not in {"version", "repo", "pr", "watch", "owner"}
    }
    _write_state_file(
        path,
        repo=repo,
        pr_number=pr_number,
        watch_identity=watch_identity,
        watch_id=watch_id,
        **fields,
    )
    return resolved, None


def _deliver(output: str) -> None:
    """Hand the report to the supervisor, and make sure it has left this process.

    The cursors covering a reported event are committed only after this returns, so
    the report must be out of our own buffers first.
    """

    print(output)
    sys.stdout.flush()


def _build_parser() -> argparse.ArgumentParser:
    """The waiter's CLI surface, separate from main() so it can be inspected directly."""

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
    parser.add_argument("--cursor-output", help=argparse.SUPPRESS)
    parser.add_argument(
        "--state-file",
        help=(
            "Path to a JSON file holding this watch's cursors between runs. Strongly recommended for "
            "--forever watches: each cycle resumes where the previous one stopped instead of "
            "re-baselining, so activity arriving between cycles is not lost and the next cycle can "
            "compare a complete ETag-revalidated gate snapshot."
        ),
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=0.0,
        help=(
            "Seconds to wait after the first new activity before reporting, re-polling until the set "
            f"stops growing (at most {SETTLE_MAX_ROUNDS} extra polls). A batched review that straddles "
            "a poll boundary then costs one Agent turn instead of one per fragment. 0 disables it."
        ),
    )
    parser.add_argument("--event-limit", type=int, default=8, help="Maximum number of new events to include in stdout")
    parser.add_argument(
        "--include-self-comments",
        action="store_true",
        help="Include reviews and comments authored by the current authenticated GitHub user",
    )
    parser.add_argument(
        "--actionable-only",
        action="store_true",
        help=(
            "Only wake the Agent for review activity that needs a response: reviews carrying a "
            "verdict or a body, inline review comments, the Codex pass reaction, and merged/closed "
            "transitions. Drops bodyless COMMENTED review envelopes, bot trigger comments such as "
            "'@codex review', and draft toggles."
        ),
    )
    parser.add_argument(
        "--ignore-author",
        action="append",
        help="GitHub login whose reviews and comments never trigger a follow-up; repeatable",
    )
    parser.add_argument(
        "--ignore-comment-pattern",
        action="append",
        help="Case-insensitive regex; matching review/comment bodies never trigger a follow-up. Repeatable",
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
    return parser


def main() -> int:
    args = _build_parser().parse_args()

    token = get_token()
    cache = ResponseCache()
    # Everything that can reject the arguments runs BEFORE any state is claimed. A
    # bad --ignore-comment-pattern used to leave a claimed state file behind on its
    # way to exit 2, and fixing the regex changes the watch identity, so the very
    # next run was refused as another watch's until the file was deleted by hand.
    ignored_authors = _normalize_authors(args.ignore_author)
    try:
        ignore_patterns = _compile_ignore_patterns(
            args.ignore_comment_pattern,
            actionable_only=args.actionable_only,
        )
    except re.error as err:
        print(f"Invalid --ignore-comment-pattern: {err}", file=sys.stderr)
        return 2
    watch_identity = _watch_identity(args)
    watch_id = _managed_watch_id()
    delivery_stamp = _last_delivery()
    # Only a managed run has a next cycle to promote staged cursors, and only it gets
    # told whether the report was queued. A manual run is one process whose stdout is
    # the delivery, so staging there would leave cursors nobody ever promotes.
    two_phase = watch_id is not None
    if two_phase and not args.state_file:
        print(
            "Managed PR watchers require an owner-specific --state-file; refusing to poll without durable cursors.",
            file=sys.stderr,
        )
        return 2
    # Read-only state may supply the cached viewer login, but no path is claimed
    # until every authentication precondition below has passed.
    saved = _load_state_file(
        args.state_file,
        repo=args.repo,
        pr_number=args.pr,
        watch_identity=watch_identity,
        watch_id=watch_id,
    )
    replay_output = _staged_replay_output(saved, delivery_stamp)
    if replay_output is not None:
        print(
            "An earlier report was never delivered; replaying it before GitHub polling preflight.",
            file=sys.stderr,
        )
        _deliver(replay_output)
        return 0
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
    if args.pr is not None and token is None and not args.include_self_comments:
        print(
            "Unauthenticated PR watches require --include-self-comments because viewer identity cannot be resolved.",
            file=sys.stderr,
        )
        return 2
    token_fingerprint = _token_fingerprint(token)
    base_interval = max(args.interval, 1.0)
    start = time.monotonic()
    viewer_login = None
    if args.pr is not None and not args.include_self_comments:
        # The stored login spares a /user request on every cycle of a forever watch,
        # but only while the token still belongs to the account it was resolved for.
        # A rotated or swapped credential would otherwise keep filtering out the old
        # account's comments and let the new account's own comments wake the Agent.
        if token_fingerprint is not None and _saved_str(saved, "token_fingerprint") == token_fingerprint:
            viewer_login = _saved_str(saved, "viewer_login")
        if viewer_login is None:
            viewer_result = retry_initial_request(
                lambda: resolve_authenticated_login(token),
                description="GitHub viewer lookup",
            )
            if viewer_result.error is not None:
                print(f"GitHub viewer lookup failed: {viewer_result.error}", file=sys.stderr)
                return 1
            viewer_login = viewer_result.value
        if not viewer_login:
            print(
                "GitHub viewer identity could not be resolved; pass --include-self-comments explicitly to continue.",
                file=sys.stderr,
            )
            return 1

    _verify_state_file_writable(
        args.state_file,
        repo=args.repo,
        pr_number=args.pr,
        watch_identity=watch_identity,
        watch_id=watch_id,
    )
    # Reload after the atomic claim/adoption so the cycle starts from the state
    # that actually owns the path, not the read-only preflight snapshot.
    saved = _load_state_file(
        args.state_file,
        repo=args.repo,
        pr_number=args.pr,
        watch_identity=watch_identity,
        watch_id=watch_id,
    )
    saved, replay_output = _resolve_staged_state(
        args.state_file,
        saved,
        delivery=delivery_stamp,
        repo=args.repo,
        pr_number=args.pr,
        watch_identity=watch_identity,
        watch_id=watch_id,
    )
    if replay_output is not None:
        _deliver(replay_output)
        return 0

    effective_interval = base_interval
    settle_seconds = max(args.settle, 0.0)

    # Resume only from a complete cursor set. A partial one would leave some
    # baseline to be derived from a `since`-narrowed fetch, which no longer
    # contains the PR's full history.
    resume_cursors = {key: _saved_int(saved, key) for key in STATE_CURSOR_KEYS}
    resumed = not args.catch_up and all(value is not None for value in resume_cursors.values())
    missing_baselines = _missing_pr_baselines(saved) if resumed else []
    if args.pr is not None and args.state_file and missing_baselines:
        print(
            (
                "Saved PR state lacks required baseline(s): %s; pass --catch-up "
                "to seed them explicitly before resuming."
            )
            % ", ".join(missing_baselines),
            file=sys.stderr,
        )
        return 2
    if two_phase and not args.catch_up:
        seeded = _saved_int(saved, "pr_cursor") is not None if args.new_prs else resumed
        if not seeded:
            print(
                "Managed first watch requires --catch-up or a pre-seeded complete state file; refusing to rebaseline.",
                file=sys.stderr,
            )
            return 2
    # --catch-up asks for existing activity to count as pending, and it already
    # overrides the saved cursors above. The new-PR cursor follows the same rule:
    # inheriting it would filter the fully fetched history right back down to what
    # the last cycle had already seen, which is the opposite of what was asked for.
    # An explicit --since-pr-id names a replay point and still wins.
    saved_pr_cursor = None if args.catch_up else _saved_int(saved, "pr_cursor")
    since_pr_id = args.since_pr_id if args.since_pr_id is not None else saved_pr_cursor

    if args.pr is not None:
        initial_request = retry_initial_request(
            lambda: _fetch_state(args.repo, args.pr, token, cache=cache),
            description="initial GitHub PR state request",
        )
    else:
        initial_pr_stop_after_id = None
        initial_pr_max_pages = None
        if since_pr_id is not None and not args.catch_up:
            initial_pr_stop_after_id = since_pr_id
        elif not args.catch_up:
            initial_pr_max_pages = 1
        initial_request = retry_initial_request(
            lambda: _fetch_new_pr_state(
                args.repo,
                token,
                stop_after_id=initial_pr_stop_after_id,
                max_pages=initial_pr_max_pages,
                cache=cache,
            ),
            description="initial GitHub pull-request list request",
        )
    if initial_request.error is not None:
        print(f"Failed to fetch initial PR state: {initial_request.error}", file=sys.stderr)
        return 1
    if initial_request.value is None:
        print("Initial GitHub request completed without a result", file=sys.stderr)
        return 1
    state, requests_per_poll_count = initial_request.value

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

        def _initial_cursor(flag_value: int | None, saved_key: str, items_key: str) -> int:
            if flag_value is not None:
                return flag_value
            if resumed:
                return resume_cursors[saved_key] or 0
            return 0 if args.catch_up else max_id(state[items_key])

        review_cursor = _initial_cursor(args.since_review_id, "review_cursor", "reviews")
        review_comment_cursor = _initial_cursor(
            args.since_review_comment_id, "review_comment_cursor", "review_comments"
        )
        issue_comment_cursor = _initial_cursor(
            args.since_issue_comment_id, "issue_comment_cursor", "issue_comments"
        )
        reaction_cursor = _initial_cursor(args.since_reaction_id, "reaction_cursor", "reactions")
        review_fingerprints = _saved_fingerprints(saved, REVIEW_FINGERPRINTS_KEY)
        review_comment_fingerprints = _saved_fingerprints(
            saved,
            REVIEW_COMMENT_FINGERPRINTS_KEY,
        )
        issue_comment_fingerprints = _saved_fingerprints(
            saved,
            ISSUE_COMMENT_FINGERPRINTS_KEY,
        )
        review_thread_states = _saved_review_thread_states(saved)
        pr_status = (
            args.since_pr_status
            or (_saved_str(saved, "pr_status") if resumed else None)
            or _current_pr_status(state.get("pull_request"))
        )
        head_sha = (
            (_saved_str(saved, "head_sha") if resumed else None)
            or _current_pr_head_sha(state.get("pull_request"))
        )
        explicit_replay = any(
            value is not None
            for value in (
                args.since_review_id,
                args.since_review_comment_id,
                args.since_issue_comment_id,
                args.since_reaction_id,
                args.since_pr_status,
            )
        )
        if resumed:
            snapshot = _saved_snapshot(saved)
        elif args.catch_up or explicit_replay:
            snapshot = {}
        else:
            snapshot = _normalized_pr_snapshot(
                state,
                viewer_login=viewer_login,
                ignore_self_comments=not args.include_self_comments,
                actionable_only=args.actionable_only,
                ignored_authors=ignored_authors,
                ignore_patterns=ignore_patterns,
                review_threads_available=token is not None,
            )

        print(
            (
                "Watching GitHub PR %s#%s from cursors: review=%s review_comment=%s issue_comment=%s reaction=%s pr_status=%s head=%s catch_up=%s resumed=%s"
                % (
                    args.repo,
                    args.pr,
                    review_cursor,
                    review_comment_cursor,
                    issue_comment_cursor,
                    reaction_cursor,
                    pr_status,
                    head_sha,
                    args.catch_up,
                    resumed,
                )
            ),
            file=sys.stderr,
        )

        def _render(
            cursors: tuple[int, int, int, int, str, str, dict[str, Any]],
        ) -> tuple[str | None, int, int, int, int, str, str, dict[str, Any]]:
            return _render_activity(
                repo=args.repo,
                pr_number=args.pr,
                state=state,
                review_cursor=cursors[0],
                review_comment_cursor=cursors[1],
                issue_comment_cursor=cursors[2],
                reaction_cursor=cursors[3],
                pr_status=cursors[4],
                head_sha=cursors[5],
                snapshot=cursors[6],
                event_limit=args.event_limit,
                viewer_login=viewer_login,
                ignore_self_comments=not args.include_self_comments,
                actionable_only=args.actionable_only,
                ignored_authors=ignored_authors,
                ignore_patterns=ignore_patterns,
                review_fingerprints=review_fingerprints,
                review_comment_fingerprints=review_comment_fingerprints,
                issue_comment_fingerprints=issue_comment_fingerprints,
                review_thread_states=review_thread_states,
                review_threads_available=token is not None,
            )

        def _pr_state_fields() -> dict[str, Any]:
            return {
                "review_cursor": review_cursor,
                "review_comment_cursor": review_comment_cursor,
                "issue_comment_cursor": issue_comment_cursor,
                "reaction_cursor": reaction_cursor,
                "pr_status": pr_status,
                "head_sha": head_sha,
                REVIEW_FINGERPRINTS_KEY: review_fingerprints,
                REVIEW_COMMENT_FINGERPRINTS_KEY: review_comment_fingerprints,
                ISSUE_COMMENT_FINGERPRINTS_KEY: issue_comment_fingerprints,
                REVIEW_THREAD_STATES_KEY: review_thread_states,
                PR_SNAPSHOT_KEY: snapshot,
                "viewer_login": viewer_login,
                "token_fingerprint": token_fingerprint,
            }

        def _persist_pr_state(
            *,
            previous: dict[str, Any] | None = None,
            output: str | None = None,
        ) -> None:
            fields = _pr_state_fields()
            if previous is not None:
                # Committed state stays where the reported event is still unseen; the
                # cursors past it wait under STAGED_KEY, stamped with the delivery this
                # waiter started from so a later cycle can tell whether it has moved.
                fields = {
                    **previous,
                    STAGED_KEY: {
                        "delivered_after": delivery_stamp,
                        "output": output,
                        "cursors": fields,
                    },
                }
            _write_state_file(
                args.state_file,
                repo=args.repo,
                pr_number=args.pr,
                watch_identity=watch_identity,
                watch_id=watch_id,
                **fields,
            )

        def _report_pr(output: str, previous: dict[str, Any]) -> None:
            """Hand one report to the supervisor, staging or committing to match.

            Under ``vibe watch`` the cursors covering the event are staged first, so a
            crash before the supervisor queues the follow-up replays the report instead
            of skipping it. A manual run has no next cycle to promote anything, and
            printing there is the delivery, so it commits straight after.
            """

            if two_phase:
                _persist_pr_state(previous=previous, output=output)
                _deliver(output)
                return
            _deliver(output)
            _persist_pr_state()

        def _settle(
            first: tuple[str | None, int, int, int, int, str, str, dict[str, Any]],
            pending: tuple[int, int, int, int, str, str, dict[str, Any]],
        ) -> GitHubRequestResult[
            tuple[str | None, int, int, int, int, str, str, dict[str, Any]]
        ]:
            """Re-poll while a batch is still landing so it costs one Agent turn."""

            nonlocal state
            if settle_seconds <= 0:
                return GitHubRequestResult(value=first)

            best = first
            for _round in range(SETTLE_MAX_ROUNDS):
                # The batch is already worth a turn, so waiting for the rest of it must
                # not push the waiter past its own deadline: `vibe watch` kills the
                # process on timeout and the report would be lost with it. The sleep is
                # only half the cost — the re-poll behind it is several sequential
                # requests, each free to block for REQUEST_TIMEOUT_SECONDS — so both
                # have to fit in what is left, not just the sleep.
                if args.timeout > 0:
                    remaining = args.timeout - (time.monotonic() - start)
                    repoll_budget = max(1, requests_per_poll_count) * REQUEST_TIMEOUT_SECONDS
                    if remaining <= settle_seconds + repoll_budget:
                        print(
                            "Settle window plus its re-poll would outlast --timeout; "
                            "reporting the batch seen so far.",
                            file=sys.stderr,
                        )
                        return GitHubRequestResult(value=best)
                time.sleep(settle_seconds)
                settle_request = github_request(
                    lambda: _fetch_state(
                        args.repo,
                        args.pr,
                        token,
                        cache=cache,
                    )
                )
                if settle_request.error is not None:
                    print(
                        f"Settle re-poll failed: {settle_request.error}",
                        file=sys.stderr,
                    )
                    if settle_request.error.retryable:
                        continue
                    return GitHubRequestResult(error=settle_request.error)
                if settle_request.value is None:
                    return GitHubRequestResult(
                        error=GitHubProtocolError(
                            "Settle GitHub request completed without a result"
                        )
                    )
                state, _count = settle_request.value
                # Rendered from the same cursors as the first hit, so the result is a
                # superset rather than a second, partial report.
                candidate = _render(pending)
                if candidate[0] is None:
                    return GitHubRequestResult(value=best)
                if candidate[1:] == best[1:]:
                    return GitHubRequestResult(value=candidate)
                best = candidate
            return GitHubRequestResult(value=best)

        pending_cursors = (
            review_cursor,
            review_comment_cursor,
            issue_comment_cursor,
            reaction_cursor,
            pr_status,
            head_sha,
            snapshot,
        )
        pre_event_fields = _pr_state_fields()
        initial_result = _render(pending_cursors)
        if initial_result[0] is not None and not args.catch_up:
            settle_result = _settle(initial_result, pending_cursors)
            if settle_result.error is not None:
                return 1
            if settle_result.value is None:
                print("Settle request completed without a result", file=sys.stderr)
                return 1
            initial_result = settle_result.value
        review_fingerprints = _review_fingerprint_map(state["reviews"])
        review_comment_fingerprints = _comment_fingerprint_map(state["review_comments"])
        issue_comment_fingerprints = _comment_fingerprint_map(state["issue_comments"])
        if token is not None:
            review_thread_states = _review_thread_state_map(state["review_threads"])
        (
            initial_output,
            review_cursor,
            review_comment_cursor,
            issue_comment_cursor,
            reaction_cursor,
            pr_status,
            head_sha,
            snapshot,
        ) = initial_result
        if initial_output is None:
            # Persisted even with nothing to report: the baseline this cycle
            # established is exactly what the next cycle must resume from.
            _persist_pr_state()
        else:
            _write_cursor_output(
                args.cursor_output,
                review_cursor=review_cursor,
                review_comment_cursor=review_comment_cursor,
                issue_comment_cursor=issue_comment_cursor,
                reaction_cursor=reaction_cursor,
                pr_status=pr_status,
                head_sha=head_sha,
            )
            _report_pr(initial_output, pre_event_fields)
            return 0
    else:
        pr_cursor = since_pr_id if since_pr_id is not None else (0 if args.catch_up else max_id(state["pull_requests"]))
        print(
            f"Watching GitHub new PRs in {args.repo} from cursor: pr={pr_cursor} catch_up={args.catch_up}",
            file=sys.stderr,
        )
        pre_event_pr_cursor = pr_cursor
        initial_output, pr_cursor = _render_new_pull_requests(
            repo=args.repo,
            state=state,
            pr_cursor=pr_cursor,
            event_limit=args.event_limit,
        )
        def _persist_new_pr_state(
            *,
            previous: int | None = None,
            output: str | None = None,
        ) -> None:
            fields: dict[str, Any] = {"pr_cursor": pr_cursor}
            if previous is not None:
                fields = {
                    "pr_cursor": previous,
                    STAGED_KEY: {
                        "delivered_after": delivery_stamp,
                        "output": output,
                        "cursors": {"pr_cursor": pr_cursor},
                    },
                }
            _write_state_file(
                args.state_file,
                repo=args.repo,
                pr_number=None,
                watch_identity=watch_identity,
                watch_id=watch_id,
                **fields,
            )

        def _report_new_pr(output: str, previous: int) -> None:
            """Same staging contract as ``_report_pr``, over the single new-PR cursor."""

            if two_phase:
                _persist_new_pr_state(previous=previous, output=output)
                _deliver(output)
                return
            _deliver(output)
            _persist_new_pr_state()

        if initial_output is None:
            _persist_new_pr_state()
        else:
            _write_new_pr_cursor_output(args.cursor_output, pr_cursor=pr_cursor)
            _report_new_pr(initial_output, pre_event_pr_cursor)
            return 0

    while True:
        sleep_seconds = effective_interval
        if args.timeout > 0:
            remaining_timeout = args.timeout - (time.monotonic() - start)
            if remaining_timeout <= 0:
                print("Timed out while waiting for GitHub PR activity", file=sys.stderr)
                print(cache.summary(), file=sys.stderr)
                return 124
            sleep_seconds = min(sleep_seconds, remaining_timeout)

        time.sleep(sleep_seconds)

        if args.pr is not None:
            poll_request = github_request(
                lambda: _fetch_state(
                    args.repo,
                    args.pr,
                    token,
                    cache=cache,
                )
            )
        else:
            poll_request = github_request(
                lambda: _fetch_new_pr_state(
                    args.repo,
                    token,
                    stop_after_id=pr_cursor if pr_cursor > 0 else None,
                    cache=cache,
                )
            )
        if poll_request.error is not None:
            print(f"GitHub polling failed: {poll_request.error}", file=sys.stderr)
            if poll_request.error.retryable:
                print(
                    "Retryable GitHub request failure; continuing in this watch",
                    file=sys.stderr,
                )
                continue
            return 1
        if poll_request.value is None:
            print("GitHub polling completed without a result", file=sys.stderr)
            return 1
        state, requests_per_poll_count = poll_request.value

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
            pending_cursors = (
                review_cursor,
                review_comment_cursor,
                issue_comment_cursor,
                reaction_cursor,
                pr_status,
                head_sha,
                snapshot,
            )
            pre_event_fields = _pr_state_fields()
            result = _render(pending_cursors)
            if result[0] is not None:
                settle_result = _settle(result, pending_cursors)
                if settle_result.error is not None:
                    return 1
                if settle_result.value is None:
                    print("Settle request completed without a result", file=sys.stderr)
                    return 1
                result = settle_result.value
            review_fingerprints = _review_fingerprint_map(state["reviews"])
            review_comment_fingerprints = _comment_fingerprint_map(state["review_comments"])
            issue_comment_fingerprints = _comment_fingerprint_map(state["issue_comments"])
            if token is not None:
                review_thread_states = _review_thread_state_map(state["review_threads"])
            (
                output,
                review_cursor,
                review_comment_cursor,
                issue_comment_cursor,
                reaction_cursor,
                pr_status,
                head_sha,
                snapshot,
            ) = result
            if output is None:
                # Cursors also move when everything new was filtered out, and that
                # progress has to survive the cycle or the next one re-examines it.
                # Nothing is being reported, so there is no delivery to wait for.
                _persist_pr_state()
                continue

            _write_cursor_output(
                args.cursor_output,
                review_cursor=review_cursor,
                review_comment_cursor=review_comment_cursor,
                issue_comment_cursor=issue_comment_cursor,
                reaction_cursor=reaction_cursor,
                pr_status=pr_status,
                head_sha=head_sha,
            )
            report = partial(_report_pr, previous=pre_event_fields)
        else:
            pre_event_pr_cursor = pr_cursor
            output, pr_cursor = _render_new_pull_requests(
                repo=args.repo,
                state=state,
                pr_cursor=pr_cursor,
                event_limit=args.event_limit,
            )
            if output is None:
                _persist_new_pr_state()
                continue
            _write_new_pr_cursor_output(args.cursor_output, pr_cursor=pr_cursor)
            report = partial(_report_new_pr, previous=pre_event_pr_cursor)

        print(cache.summary(), file=sys.stderr)
        report(output)
        return 0


def run_cli() -> int:
    """``main`` plus the terminal handling of an unusable ``--state-file``.

    Exit 1 and not the retryable 75: a directory that cannot be written to, a path
    already owned by another PR, and a file whose cursors cannot be read do not start
    working on the next cycle, and a forever watch retrying into one would poll
    indefinitely while losing the activity it saw.
    """

    try:
        return main()
    except StateFileError as err:
        print(
            f"{err}. Give this watch its own --state-file path (or fix or remove "
            "this one), then recreate the watch.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
