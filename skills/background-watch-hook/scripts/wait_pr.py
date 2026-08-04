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
    is_retryable_http_error,
    later_since,
    list_paginated,
    list_paginated_with_count,
    max_id,
    min_interval_for_unauthenticated,
    RETRY_EXIT_CODE,
    requests_per_poll,
    ResponseCache,
    squash,
)

CODEX_REVIEW_PASS_REACTION_USER = "chatgpt-codex-connector[bot]"
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
STATE_CURSOR_KEYS = (
    "review_cursor",
    "review_comment_cursor",
    "issue_comment_cursor",
    "reaction_cursor",
)
# A bot review lands as a burst of inline comments plus an envelope. Re-polling a
# few times while the burst is still arriving turns it into one Agent turn instead
# of one turn per fragment that happened to cross a poll boundary.
SETTLE_MAX_ROUNDS = 3


class StateFileError(RuntimeError):
    """The requested ``--state-file`` cannot do the job it was asked to do.

    Raised rather than warned about because a forever watch that keeps polling
    without usable cursors loses activity instead of reporting it, and does so
    silently: every fresh cycle re-baselines from the current PR.
    """


class StatePersistenceError(StateFileError):
    """The cursors an explicit ``--state-file`` promised could not be saved."""


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


def _is_codex_pass_reaction(reaction: dict[str, Any]) -> bool:
    author = ((reaction.get("user") or {}).get("login")) or ""
    content = str(reaction.get("content") or "")
    return author == CODEX_REVIEW_PASS_REACTION_USER and content == CODEX_REVIEW_PASS_REACTION_CONTENT


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


def _with_since(url: str, since: str | None) -> str:
    if not since:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}since={urllib.parse.quote(since)}"


def _fetch_state(
    repo: str,
    pr_number: int,
    token: str | None,
    *,
    cache: ResponseCache | None = None,
    review_comment_since: str | None = None,
    issue_comment_since: str | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], int]:
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
    review_comments, review_comment_requests = list_paginated_with_count(
        _with_since(f"{base_url}/pulls/{pr_number}/comments", review_comment_since),
        token,
        cache=cache,
    )
    issue_comments, issue_comment_requests = list_paginated_with_count(
        _with_since(f"{base_url}/issues/{pr_number}/comments", issue_comment_since),
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
    state: dict[str, list[dict[str, Any]]],
    review_cursor: int,
    review_comment_cursor: int,
    issue_comment_cursor: int,
    reaction_cursor: int,
    pr_status: str,
    event_limit: int,
    viewer_login: str | None = None,
    ignore_self_comments: bool = True,
    actionable_only: bool = False,
    ignored_authors: set[str] | None = None,
    ignore_patterns: list[re.Pattern[str]] | None = None,
) -> tuple[str | None, int, int, int, int, str]:
    ignored_authors = ignored_authors or set()
    ignore_patterns = ignore_patterns or []
    current_pr_status = _current_pr_status(state.get("pull_request"))
    new_reviews = filter_new(state["reviews"], review_cursor)
    new_review_comments = filter_new(state["review_comments"], review_comment_cursor)
    new_issue_comments = filter_new(state["issue_comments"], issue_comment_cursor)

    def _visible(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Cursors advance over everything new; only the rendering is filtered, so a
        # dropped item is dropped once and never re-examined on the next poll.
        kept = items if not ignore_self_comments else [
            item for item in items if not _is_self_authored_comment(item, viewer_login)
        ]
        return [
            item
            for item in kept
            if _keep_item(item, ignored_authors=ignored_authors, ignore_patterns=ignore_patterns)
        ]

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

    if not (new_reviews or new_review_comments or new_issue_comments or new_reactions or has_pr_status_event):
        return None, review_cursor, review_comment_cursor, issue_comment_cursor, reaction_cursor, pr_status

    next_review_cursor = max(review_cursor, max_id(new_reviews))
    next_review_comment_cursor = max(review_comment_cursor, max_id(new_review_comments))
    next_issue_comment_cursor = max(issue_comment_cursor, max_id(new_issue_comments))
    next_reaction_cursor = max(reaction_cursor, max_id(state["reactions"]))
    next_pr_status = current_pr_status

    render_pr_status_event = has_pr_status_event and (
        not actionable_only or current_pr_status in ACTIONABLE_PR_STATUSES
    )

    rendered_events: list[str] = []
    if render_pr_status_event and isinstance(state.get("pull_request"), dict):
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

    lines = [f"GitHub PR activity detected for {repo}#{pr_number}"]

    visible_limit = max(event_limit, 1)
    for entry in rendered_events[:visible_limit]:
        lines.append(entry)

    total_events = len(rendered_events)
    if total_events > visible_limit:
        lines.append(f"- {total_events - visible_limit} additional event(s) omitted")

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
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


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


def _owner_conflict(
    owner: tuple[Any, Any, Any] | None,
    *,
    repo: str,
    pr_number: int | None,
    watch_identity: str | None,
) -> str | None:
    """Why ``owner`` is somebody else's claim on the path, or ``None`` when it is ours.

    A state file written before identities existed carries none, and an absent
    identity cannot prove a conflict, so such a file is adopted rather than rejected.
    """

    if owner is None:
        return None
    saved_repo, saved_pr, saved_watch = owner
    if saved_repo != repo or saved_pr != pr_number:
        return f"belongs to {saved_repo}#{saved_pr}, not {repo}#{pr_number}"
    if saved_watch is not None and watch_identity is not None and saved_watch != watch_identity:
        return (
            f"belongs to another watch on {saved_repo}#{saved_pr} with different "
            "reporting filters"
        )
    return None


def _load_state_file(
    path: str | None,
    *,
    repo: str,
    pr_number: int | None,
    watch_identity: str | None = None,
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
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        print(f"Ignoring unusable state file {path}: {err}", file=sys.stderr)
        return {}

    if not isinstance(payload, dict) or payload.get("version") != STATE_FILE_VERSION:
        print(f"Ignoring state file {path}: unrecognised format", file=sys.stderr)
        return {}
    # Resuming from somebody else's cursors would skip the history they cover, and
    # carrying on would overwrite them on the first cursor advance -- so this is
    # terminal rather than a fresh baseline. A file left behind by another watch has
    # to be removed, or that watch given its own path, deliberately.
    conflict = _owner_conflict(
        (payload.get("repo"), payload.get("pr"), payload.get("watch")),
        repo=repo,
        pr_number=pr_number,
        watch_identity=watch_identity,
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


def _state_file_owner(target: Path) -> tuple[Any, Any, Any] | None:
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
    return payload.get("repo"), payload.get("pr"), payload.get("watch")


def _claim_state_file(
    target: Path,
    *,
    repo: str,
    pr_number: int | None,
    watch_identity: str | None = None,
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
                },
                stream,
            )
    except OSError as err:
        raise StatePersistenceError(f"Cannot write state file {target}: {err}") from err
    return True


def _verify_state_file_writable(
    path: str | None,
    *,
    repo: str,
    pr_number: int | None,
    watch_identity: str | None = None,
) -> None:
    """Claim the requested state file, and fail before the first poll if it is unusable.

    A forever watch only discovers a read-only parent directory when the cycle it
    spent minutes on tries to save its cursors, and by then the activity that
    cycle observed is already unrecoverable.

    A missing state file is created here holding nothing but this PR's ownership, so
    the path is owned before any polling starts rather than after the first cursor
    advance. It carries no cursors, so this cycle still baselines from the current PR
    exactly as it did before.

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
        exists = target.exists()
    except OSError as err:
        raise StatePersistenceError(f"Cannot write state file {path}: {err}") from err

    if not exists and _claim_state_file(
        target, repo=repo, pr_number=pr_number, watch_identity=watch_identity
    ):
        # Creating the real file in the real directory is the write probe, and the
        # rename in later cycles lands on a file this process owns.
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
    **fields: Any,
) -> None:
    if not path:
        return

    payload = {
        "version": STATE_FILE_VERSION,
        "repo": repo,
        "pr": pr_number,
        "watch": watch_identity,
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
            "re-baselining, so activity arriving between cycles is not lost, and the next fetch only "
            "asks GitHub for what is new."
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
    watch_identity = _watch_identity(args)
    _verify_state_file_writable(
        args.state_file, repo=args.repo, pr_number=args.pr, watch_identity=watch_identity
    )
    saved = _load_state_file(
        args.state_file, repo=args.repo, pr_number=args.pr, watch_identity=watch_identity
    )
    token_fingerprint = _token_fingerprint(token)
    viewer_login = None
    if not args.include_self_comments:
        # The stored login spares a /user request on every cycle of a forever watch,
        # but only while the token still belongs to the account it was resolved for.
        # A rotated or swapped credential would otherwise keep filtering out the old
        # account's comments and let the new account's own comments wake the Agent.
        if token_fingerprint is not None and _saved_str(saved, "token_fingerprint") == token_fingerprint:
            viewer_login = _saved_str(saved, "viewer_login")
        viewer_login = viewer_login or get_authenticated_login(token)
    ignored_authors = _normalize_authors(args.ignore_author)
    try:
        ignore_patterns = _compile_ignore_patterns(
            args.ignore_comment_pattern,
            actionable_only=args.actionable_only,
        )
    except re.error as err:
        print(f"Invalid --ignore-comment-pattern: {err}", file=sys.stderr)
        return 2
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
    settle_seconds = max(args.settle, 0.0)

    start = time.monotonic()

    # Resume only from a complete cursor set. A partial one would leave some
    # baseline to be derived from a `since`-narrowed fetch, which no longer
    # contains the PR's full history.
    resume_cursors = {key: _saved_int(saved, key) for key in STATE_CURSOR_KEYS}
    resumed = not args.catch_up and all(value is not None for value in resume_cursors.values())
    # An explicit --since-*-comment-id asks for a replay from that id. The saved
    # `since` timestamp is only ever a shortcut for the saved cursor, so keeping it
    # would narrow the fetch to comments newer than the last poll and hide exactly
    # the history the flag asked to see.
    review_comment_since = (
        _saved_str(saved, "review_comment_since")
        if resumed and args.since_review_comment_id is None
        else None
    )
    issue_comment_since = (
        _saved_str(saved, "issue_comment_since")
        if resumed and args.since_issue_comment_id is None
        else None
    )
    # --catch-up asks for existing activity to count as pending, and it already
    # overrides the saved cursors above. The new-PR cursor follows the same rule:
    # inheriting it would filter the fully fetched history right back down to what
    # the last cycle had already seen, which is the opposite of what was asked for.
    # An explicit --since-pr-id names a replay point and still wins.
    saved_pr_cursor = None if args.catch_up else _saved_int(saved, "pr_cursor")
    since_pr_id = args.since_pr_id if args.since_pr_id is not None else saved_pr_cursor

    try:
        if args.pr is not None:
            state, requests_per_poll_count = _fetch_state(
                args.repo,
                args.pr,
                token,
                cache=cache,
                review_comment_since=review_comment_since,
                issue_comment_since=issue_comment_since,
            )
        else:
            initial_pr_stop_after_id = None
            initial_pr_max_pages = None
            if since_pr_id is not None and not args.catch_up:
                initial_pr_stop_after_id = since_pr_id
            elif not args.catch_up:
                initial_pr_max_pages = 1
            state, requests_per_poll_count = _fetch_new_pr_state(
                args.repo,
                token,
                stop_after_id=initial_pr_stop_after_id,
                max_pages=initial_pr_max_pages,
                cache=cache,
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
        pr_status = (
            args.since_pr_status
            or (_saved_str(saved, "pr_status") if resumed else None)
            or _current_pr_status(state.get("pull_request"))
        )

        print(
            (
                "Watching GitHub PR %s#%s from cursors: review=%s review_comment=%s issue_comment=%s reaction=%s pr_status=%s catch_up=%s resumed=%s"
                % (
                    args.repo,
                    args.pr,
                    review_cursor,
                    review_comment_cursor,
                    issue_comment_cursor,
                    reaction_cursor,
                    pr_status,
                    args.catch_up,
                    resumed,
                )
            ),
            file=sys.stderr,
        )

        def _render(cursors: tuple[int, int, int, int, str]) -> tuple[str | None, int, int, int, int, str]:
            return _render_activity(
                repo=args.repo,
                pr_number=args.pr,
                state=state,
                review_cursor=cursors[0],
                review_comment_cursor=cursors[1],
                issue_comment_cursor=cursors[2],
                reaction_cursor=cursors[3],
                pr_status=cursors[4],
                event_limit=args.event_limit,
                viewer_login=viewer_login,
                ignore_self_comments=not args.include_self_comments,
                actionable_only=args.actionable_only,
                ignored_authors=ignored_authors,
                ignore_patterns=ignore_patterns,
            )

        def _advance_since() -> None:
            nonlocal review_comment_since, issue_comment_since
            review_comment_since = later_since(review_comment_since, state["review_comments"])
            issue_comment_since = later_since(issue_comment_since, state["issue_comments"])

        def _persist_pr_state() -> None:
            _write_state_file(
                args.state_file,
                repo=args.repo,
                pr_number=args.pr,
                watch_identity=watch_identity,
                review_cursor=review_cursor,
                review_comment_cursor=review_comment_cursor,
                issue_comment_cursor=issue_comment_cursor,
                reaction_cursor=reaction_cursor,
                pr_status=pr_status,
                review_comment_since=review_comment_since,
                issue_comment_since=issue_comment_since,
                viewer_login=viewer_login,
                token_fingerprint=token_fingerprint,
            )

        def _settle(
            first: tuple[str | None, int, int, int, int, str],
            pending: tuple[int, int, int, int, str],
        ) -> tuple[str | None, int, int, int, int, str]:
            """Re-poll while a batch is still landing so it costs one Agent turn."""

            nonlocal state
            if settle_seconds <= 0:
                return first

            best = first
            for _round in range(SETTLE_MAX_ROUNDS):
                time.sleep(settle_seconds)
                try:
                    state, _count = _fetch_state(
                        args.repo,
                        args.pr,
                        token,
                        cache=cache,
                        review_comment_since=review_comment_since,
                        issue_comment_since=issue_comment_since,
                    )
                except Exception as err:  # noqa: BLE001
                    print(
                        f"Settle re-poll failed; reporting the batch as first seen: {err}",
                        file=sys.stderr,
                    )
                    return best
                # Rendered from the same cursors as the first hit, so the result is a
                # superset rather than a second, partial report.
                candidate = _render(pending)
                if candidate[0] is None:
                    return best
                if candidate[1:] == best[1:]:
                    return candidate
                best = candidate
            return best

        pending_cursors = (
            review_cursor,
            review_comment_cursor,
            issue_comment_cursor,
            reaction_cursor,
            pr_status,
        )
        initial_result = _render(pending_cursors)
        if initial_result[0] is not None and not args.catch_up:
            initial_result = _settle(initial_result, pending_cursors)
        (
            initial_output,
            review_cursor,
            review_comment_cursor,
            issue_comment_cursor,
            reaction_cursor,
            pr_status,
        ) = initial_result
        _advance_since()
        # Persisted even with nothing to report: the baseline this cycle established
        # is exactly what the next cycle must resume from.
        _persist_pr_state()
        if initial_output is not None:
            _write_cursor_output(
                args.cursor_output,
                review_cursor=review_cursor,
                review_comment_cursor=review_comment_cursor,
                issue_comment_cursor=issue_comment_cursor,
                reaction_cursor=reaction_cursor,
                pr_status=pr_status,
            )
            print(initial_output)
            return 0
    else:
        pr_cursor = since_pr_id if since_pr_id is not None else (0 if args.catch_up else max_id(state["pull_requests"]))
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
        _write_state_file(
            args.state_file,
            repo=args.repo,
            pr_number=None,
            watch_identity=watch_identity,
            pr_cursor=pr_cursor,
        )
        if initial_output is not None:
            _write_new_pr_cursor_output(args.cursor_output, pr_cursor=pr_cursor)
            print(initial_output)
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

        try:
            if args.pr is not None:
                state, requests_per_poll_count = _fetch_state(
                    args.repo,
                    args.pr,
                    token,
                    cache=cache,
                    review_comment_since=review_comment_since,
                    issue_comment_since=issue_comment_since,
                )
            else:
                state, requests_per_poll_count = _fetch_new_pr_state(
                    args.repo,
                    token,
                    stop_after_id=pr_cursor if pr_cursor > 0 else None,
                    cache=cache,
                )
        except urllib.error.HTTPError as err:
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
            pending_cursors = (
                review_cursor,
                review_comment_cursor,
                issue_comment_cursor,
                reaction_cursor,
                pr_status,
            )
            result = _render(pending_cursors)
            if result[0] is not None:
                result = _settle(result, pending_cursors)
            (
                output,
                review_cursor,
                review_comment_cursor,
                issue_comment_cursor,
                reaction_cursor,
                pr_status,
            ) = result
            _advance_since()
            # Cursors also move when everything new was filtered out, and that
            # progress has to survive the cycle or the next one re-examines it.
            _persist_pr_state()
            if output is None:
                continue

            _write_cursor_output(
                args.cursor_output,
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
            _write_state_file(
                args.state_file,
                repo=args.repo,
                pr_number=None,
                watch_identity=watch_identity,
                pr_cursor=pr_cursor,
            )
            if output is None:
                continue
            _write_new_pr_cursor_output(args.cursor_output, pr_cursor=pr_cursor)

        print(cache.summary(), file=sys.stderr)
        print(output)
        return 0


def run_cli() -> int:
    """``main`` plus the terminal handling of an unusable ``--state-file``.

    Exit 1 and not the retryable 75: neither a directory that cannot be written
    to nor a path already owned by another PR starts working on the next cycle,
    and a forever watch retrying into one would poll indefinitely while losing
    the activity it saw.
    """

    try:
        return main()
    except StateFileError as err:
        print(
            f"{err}. Give this watch its own --state-file path (or fix this one), "
            "then recreate the watch.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
