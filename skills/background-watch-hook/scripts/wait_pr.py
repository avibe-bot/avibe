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
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Iterator, NamedTuple

try:  # POSIX only, which is every platform `vibe` runs the waiter on.
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None  # type: ignore[assignment]

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _github_wait_common import (  # noqa: E402
    filter_new,
    get_authenticated_login,
    get_token,
    github_get,
    github_graphql,
    github_request,
    InitialRequestRetriesExhausted,
    LAST_DELIVERY_ENV,
    later_since,
    list_paginated,
    list_paginated_with_count,
    max_id,
    min_interval_for_unauthenticated,
    REQUEST_TIMEOUT_SECONDS,
    RETRY_EXIT_CODE,
    retry_initial_request,
    requests_per_poll,
    ResponseCache,
    squash,
    WATCH_ID_ENV,
)
from _github_actions_wait import (  # noqa: E402
    DEFAULT_SUCCESS_CONCLUSIONS,
    fetch_workflow_runs,
    normalize_selected_runs,
    render_actions_result,
    select_matching_runs,
)

CODEX_REVIEW_PASS_REACTION_USERS = {
    "chatgpt-codex-connector",
    "chatgpt-codex-connector[bot]",
}
CODEX_REVIEW_PASS_REACTION_CONTENT = "+1"
CODEX_STANDARD_SUMMARY_FOOTER = (
    "<details> <summary>ℹ️ About Codex in GitHub</summary>",
    "<br/>",
    "[Your team has set up Codex to review pull requests in this repo](https://chatgpt.com/codex/cloud/settings/general).",
    "Codex reviews are triggered when you",
    "- Open a pull request for review",
    "- Mark a draft as ready",
    "- Comment \"@codex review\" or \"@codex security review\".",
    "Codex reacts with 👀 while any review is running, comments if it has suggestions, and reacts with 👍 once all reviews finish with no findings.",
    "</details>",
)
# The clean-pass receipt Codex posts as a PR conversation comment. Its verdict line
# ends with a rotating sign-off (":+1:", "Chef's kiss.", "Keep them coming!"), and
# its details footer differs from the summary comment's.
CODEX_RECEIPT_MARKER = "<!-- codex-review-receipt:v1 -->"
CODEX_RECEIPT_FOOTER = (
    "<details> <summary>ℹ️ About Codex in GitHub</summary>",
    "<br/>",
    "[Your team has set up Codex to review pull requests in this repo](https://chatgpt.com/codex/cloud/settings/general). Reviews are triggered when you",
    "- Open a pull request for review",
    "- Mark a draft as ready",
    "- Comment \"@codex review\".",
    "If Codex has suggestions, it will comment; otherwise it will react with 👍.",
    "Codex can also answer questions or update the PR. Try commenting \"@codex address that feedback\".",
    "</details>",
)
CODEX_SIGN_OFF_MAX_CHARS = 80

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
ACTIONS_SNAPSHOT_KEY = "actions"
ACTIONS_OBSERVED_KEY = "actions_observed"
SUCCESS_BATCH_KEY = "success_batch"
# The last Codex review stall this watch reported (``_codex_review_stall``), and when
# this watch first saw the current head, which no stall of that head may predate.
REVIEW_STALL_KEY = "review_stall_reported"
REVIEW_STALL_HEAD_KEY = "review_stall_head"
CODEX_SUMMARY_MARKER = "<!-- codex-pull-request-review-summary -->"
CODEX_REVIEW_REQUEST = "@codex review"
# Codex reviews almost always conclude within minutes: over 68 reviews on
# avibe-bot/avibe the median was 3.5 min, p90 15 min and p95 39 min, while a stuck one
# can sit at "running" for hours. Past this, the review is treated as stalled.
DEFAULT_REVIEW_STALL_MINUTES = 30.0
_CODEX_REVIEW_IN_PROGRESS = frozenset({"queued", "pending", "running", "in_progress"})


class PRReport(NamedTuple):
    """One delivery and the observations its pending transaction will commit."""

    output: str | None
    cursors: tuple[int, int, int, int, str]
    pr_state: dict[str, Any]
    actions: dict[str, Any] | None = None
    success_batch: dict[str, Any] | None = None
    failed_actions: bool = False


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


def _is_codex_item(item: dict[str, Any]) -> bool:
    author = item.get("author") or ((item.get("user") or {}).get("login"))
    return author in CODEX_REVIEW_PASS_REACTION_USERS


def _has_codex_problem_text(body: str) -> bool:
    return bool(re.search(
        r"\b(errors?|failed|failures?|something went wrong|changes requested|found issues|needs? changes|P[0-3])\b",
        body, re.IGNORECASE,
    ))


def _codex_summary_cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    cells = stripped.split("|")
    return cells if len(cells) == 6 else None


def _is_codex_summary_table_line(line: str) -> bool:
    return _codex_summary_cells(line) is not None


def _is_codex_summary_separator(line: str) -> bool:
    cells = _codex_summary_cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells[1:-1])


def _is_codex_summary_review_row(line: str) -> bool:
    cells = _codex_summary_cells(line)
    if cells is None:
        return False
    label = re.sub(r"^[^\w]+", "", cells[1].strip()).replace("*", "").strip()
    return (
        label in {"Code Review", "Security Review"}
        and bool(cells[2].strip())
        and re.fullmatch(r"`[0-9a-f]{7,40}`", cells[3].strip(), re.IGNORECASE) is not None
        and bool(cells[4].strip())
    )


def _is_codex_summary_state_line(line: str) -> bool:
    match = re.fullmatch(r"<!-- codex-review-state:v1 (.+) -->", line.strip())
    if not match:
        return False
    try:
        state = json.loads(match.group(1))
    except json.JSONDecodeError:
        return False
    return isinstance(state, dict)


def _is_codex_summary_shape(body: str) -> bool:
    """Require the recognized progress table to be the whole known summary shape."""

    lines = body.splitlines()
    table_start = next(
        (
            index for index, line in enumerate(lines)
            if _is_codex_summary_table_line(line)
            and (cells := line.strip().split("|"))[1].strip() == "Review"
            and cells[2].strip() == "Status"
        ),
        None,
    )
    if table_start is None:
        return False

    allowed_prefix = {
        "<!-- codex-pull-request-review-summary -->",
        "## Codex Review Summary",
        "This comment shows the latest Codex review activity on this pull request.",
    }
    if any(
        line.strip()
        and line.strip() not in allowed_prefix
        and not _is_codex_summary_state_line(line)
        for line in lines[:table_start]
    ):
        return False

    table_end = table_start
    while table_end + 1 < len(lines) and _is_codex_summary_table_line(lines[table_end + 1]):
        table_end += 1
    table_lines = lines[table_start:table_end + 1]
    if (
        len(table_lines) < 3
        or not _is_codex_summary_separator(table_lines[1])
        or not all(_is_codex_summary_review_row(line) for line in table_lines[2:])
    ):
        return False
    suffix = [line.strip() for line in lines[table_end + 1:] if line.strip()]
    if not suffix:
        return True
    standard_footer = "\n".join(CODEX_STANDARD_SUMMARY_FOOTER)
    return "\n".join(suffix) in {
        standard_footer,
        standard_footer.replace(
            "\nCodex reviews are triggered when you", " Reviews are triggered when you", 1,
        ),
    }


def _is_codex_sign_off(text: str) -> bool:
    """A short plain-text sign-off, never markup or anything that reads as a problem."""

    return (
        len(text) <= CODEX_SIGN_OFF_MAX_CHARS
        and not _has_codex_problem_text(text)
        and re.search(r"[`\[\]<>#|]|https?:", text) is None
    )


def _codex_clean_pass_body_sha(body: str) -> str | None:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if lines and lines[0] == CODEX_RECEIPT_MARKER:
        lines = lines[1:]
    verdict = re.fullmatch(
        r"Codex Review:\s*Didn['’]t find any major issues\.?(?:\s+(.+))?",
        lines[0] if lines else "", re.IGNORECASE,
    )
    if not verdict or (verdict.group(1) is not None and not _is_codex_sign_off(verdict.group(1))):
        return None
    if len(lines) == 1:
        return ""
    if len(lines) != 2 and tuple(lines[2:]) != CODEX_RECEIPT_FOOTER:
        return None
    match = re.fullmatch(
        r"\*{0,2}Reviewed commit\*{0,2}:\*{0,2}\s*`([0-9a-f]{7,40})`",
        lines[1], re.IGNORECASE,
    )
    return match.group(1) if match else None


def _is_codex_clean_pass(item: dict[str, Any], head_sha: str) -> bool:
    """Recognize a success shape, not a substitute for the Agent's review gate."""

    if not head_sha or not _is_codex_item(item):
        return False
    if str(item.get("state") or "").lower() in {"changes_requested", "dismissed"}:
        return False
    body_sha = _codex_clean_pass_body_sha(str(item.get("body") or ""))
    if body_sha is None:
        return False
    item_sha = item.get("commit_id")
    item_sha = item_sha if isinstance(item_sha, str) else ""
    if item_sha and body_sha and not (
        item_sha.casefold().startswith(body_sha.casefold())
        or body_sha.casefold().startswith(item_sha.casefold())
    ):
        return False
    reviewed = item_sha or body_sha
    return len(reviewed) >= 7 and head_sha.casefold().startswith(reviewed.casefold())


def _awaits_review_judgment(state: dict[str, Any], batch: dict[str, Any]) -> bool:
    """The latest Codex verdict is neither a recognized pass nor a recognized problem."""

    outcome = batch.get("outcome")
    if batch.get("review") is not None or not outcome or outcome["kind"] not in {"reviews", "issue_comments"}:
        return False
    item = next((item for item in state[outcome["kind"]] if item.get("id") == outcome["id"]), None)
    return (
        item is not None
        and str(item.get("state") or "").lower() not in {"changes_requested", "dismissed"}
        and not _has_codex_problem_text(str(item.get("body") or ""))
    )


def _is_codex_progress(item: dict[str, Any]) -> bool:
    body = str(item.get("body") or "")
    if (
        not _is_codex_item(item)
        or "<!-- codex-pull-request-review-summary -->" not in body
        or _has_codex_problem_text(body)
        or not _is_codex_summary_shape(body)
    ):
        return False
    statuses = [
        cells[2].strip() for line in body.splitlines()
        if len(cells := line.strip().split("|")) >= 5 and cells[0] == ""
        and cells[2].strip() != "Status" and cells[2].strip(": -")
    ]
    return bool(statuses) and all(
        re.fullmatch(
            r"[\W_]*(queued|pending|running|in progress|completed)[\W_]*"
            r"(?:since\s+)?(?:<relative-time\b[^>]*>[^<]*</relative-time>\s*)?",
            status, re.IGNORECASE,
        )
        for status in statuses
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _github_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _codex_summary_code_review(body: str) -> dict[str, Any] | None:
    """The ``code_review`` object of a Codex summary's machine-readable state line."""

    for line in body.splitlines():
        match = re.fullmatch(r"<!-- codex-review-state:v1 (.+) -->", line.strip())
        if not match:
            continue
        try:
            state = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
        code_review = state.get("code_review") if isinstance(state, dict) else None
        return code_review if isinstance(code_review, dict) else None
    return None


def _codex_review_stall(
    state: dict[str, Any],
    *,
    repo: str,
    stall_seconds: float,
    now: datetime,
    head_seen_at: datetime,
) -> tuple[str, str] | None:
    """A Codex review of the current head that should have concluded by now: (key, report).

    Three shapes, measured from when they began:

    - Codex's summary says the review of the current head is running: since the
      summary's last update, or a later ``@codex review`` request, which restarts it;
    - a ``@codex review`` request has no answer (the summary was not touched since):
      since that request, but never before this watch first saw the current head
      (``head_seen_at``), so a request made for an earlier head does not count;
    - the current head was never picked up (no summary of it, nothing requested since
      the push): since ``head_seen_at``. A draft is not expected to be reviewed.

    ``key`` changes every further ``stall_seconds`` the stall lasts, so a still-stuck
    review is reported again rather than once and forgotten. ``None`` once the review
    concludes or the PR is not open.
    """

    pr = state.get("pull_request")
    head = _current_pr_head_sha(pr)
    status = _current_pr_status(pr)
    if stall_seconds <= 0 or not head or status not in {"open", "draft"}:
        return None
    summary_at: datetime | None = None
    code_review: dict[str, Any] = {}
    request_at: datetime | None = None
    for comment in state.get("issue_comments") or []:
        body = str(comment.get("body") or "")
        stamp = _github_time(comment.get("updated_at") or comment.get("created_at"))
        if stamp is None:
            continue
        if _is_codex_item(comment):
            parsed = _codex_summary_code_review(body) if CODEX_SUMMARY_MARKER in body else None
            if parsed is not None and (summary_at is None or stamp > summary_at):
                summary_at, code_review = stamp, parsed
        elif CODEX_REVIEW_REQUEST in body.lower():
            created = _github_time(comment.get("created_at")) or stamp
            request_at = created if request_at is None or created > request_at else request_at
    summary_head = str(code_review.get("commit_sha") or "").casefold() == head.casefold()
    reviewing_head = summary_head and str(code_review.get("status") or "").casefold() in _CODEX_REVIEW_IN_PROGRESS
    if reviewing_head and summary_at is not None:
        since = max(summary_at, request_at) if request_at is not None else summary_at
        shape = "has shown the review of this head as running"
    elif request_at is not None and (summary_at is None or request_at > summary_at):
        since = max(request_at, head_seen_at)
        shape = "has not answered the last `@codex review` request"
    elif not summary_head and status == "open":
        since = head_seen_at
        shape = "has not started reviewing this head"
    else:
        return None
    elapsed = (now - since).total_seconds()
    if elapsed < stall_seconds:
        return None
    rounds = int(elapsed // stall_seconds)
    number = (pr or {}).get("number")
    report = (
        f"Codex review stalled for {repo}#{number}@{head[:12]}\n"
        f"- Codex {shape} for {int(elapsed // 60)} min (since {since.strftime('%Y-%m-%dT%H:%M:%SZ')}).\n"
        "  Re-request it with `@codex review` and verify that Codex reacts to that comment.\n"
        f"  {(pr or {}).get('html_url') or ''}".rstrip()
    )
    return f"{head.casefold()}:{since.isoformat()}:{rounds}", report


def _quiet_success_snapshot(value: dict[str, Any], *, defer_reactions: bool) -> dict[str, Any]:
    """Remove only recognized success/progress payloads from the wake comparison."""

    result = dict(value)
    head = str((value.get("pull_request") or {}).get("head_sha") or "")
    for kind in ("reviews", "issue_comments"):
        result[kind] = {
            key: item for key, item in value.get(kind, {}).items()
            if not (_is_codex_clean_pass(item, head) or _is_codex_progress(item))
        }
    if defer_reactions:
        result["reactions"] = {}
    return result


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


def _current_pr_head_sha(pr: dict[str, Any] | None) -> str:
    if not isinstance(pr, dict):
        return ""
    head = pr.get("head")
    if not isinstance(head, dict):
        return ""
    value = head.get("sha")
    return value if isinstance(value, str) else ""


def _format_pr_head_event(pr: dict[str, Any], previous_sha: str, current_sha: str) -> str:
    pr_number = pr.get("number")
    url = pr.get("html_url") or ""
    return (
        f"- pr_head #{pr_number} {previous_sha[:12]} -> {current_sha[:12]}\n"
        "  Pull request head changed; start a fresh exact-head review cycle.\n"
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

    return (
        f"- review_thread {thread_id} {_label(previous_resolved)} -> {_label(current_resolved)}\n"
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


def _item_fingerprint(item: dict[str, Any]) -> str:
    mutable = {
        key: item.get(key)
        for key in (
            "id",
            "body",
            "state",
            "submitted_at",
            "commit_id",
            "path",
            "line",
            "original_line",
            "side",
            "start_line",
            "start_side",
            "user",
            "updated_at",
        )
    }
    encoded = json.dumps(mutable, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _changed_items(
    items: list[dict[str, Any]],
    since_id: int,
    fingerprints: dict[str, str],
) -> list[dict[str, Any]]:
    """Return new or edited items while advancing fingerprints for this fetch."""

    changed: dict[int, dict[str, Any]] = {}
    for item in items:
        item_id = item.get("id")
        if not isinstance(item_id, int):
            continue
        key = str(item_id)
        fingerprint = _item_fingerprint(item)
        # A legacy cursor has no content baseline. Seed it silently; otherwise every
        # pre-existing item would look edited on the first run after the migration.
        previous = fingerprints.get(key)
        if item_id > since_id or (previous is not None and previous != fingerprint):
            changed[item_id] = item
        fingerprints[key] = fingerprint
    return sorted(
        changed.values(),
        key=lambda item: (str(item.get("updated_at") or item.get("created_at") or ""), int(item["id"])),
    )


def _fingerprint_map(items: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(item["id"]): _item_fingerprint(item)
        for item in items
        if isinstance(item.get("id"), int) and not isinstance(item.get("id"), bool)
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
    """Return complete gate-relevant state without volatile timestamps."""

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
        raw_threads = state.get("review_threads")
        review_threads = _review_thread_state_map(raw_threads if isinstance(raw_threads, list) else [])
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
        "reviews": _normalized_item_map(reviews, fields=("state", "body", "commit_id")),
        "review_comments": _normalized_item_map(review_comments, fields=("body", "path")),
        "issue_comments": _normalized_item_map(issue_comments, fields=("body",)),
        "reactions": normalized_reactions,
        "review_threads": review_threads,
    }


def _review_observation(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "reviews": ("state", "body", "commit_id"),
        "issue_comments": ("body",),
        "review_comments": ("body", "path"),
        "reactions": ("content",),
    }[kind]
    normalized = _normalized_item_map([item], fields=fields)
    if _is_codex_item(item):
        for value in normalized.values():
            value["author"] = "chatgpt-codex-connector"
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return {
        "kind": kind, "id": item["id"],
        "fingerprint": hashlib.sha256(encoded.encode()).hexdigest(),
        "order": [str(item.get("updated_at") or item.get("submitted_at") or item.get("created_at") or ""),
                  int(item["id"])],
    }


def _select_review_outcome(
    outcomes: list[tuple[dict[str, Any], bool]],
) -> tuple[dict[str, Any], bool]:
    # IDs break equivalent ties, never conflicting updates within one clock tick.
    return max(
        outcomes,
        key=lambda pair: (pair[0]["order"][0], not pair[1], pair[0]["id"]),
    )


def _should_adopt_review_outcome(
    outcome: dict[str, Any],
    clean: bool,
    last_outcome: dict[str, Any] | None,
    batch: dict[str, Any],
) -> bool:
    if last_outcome is None:
        return True
    timestamp, last_timestamp = outcome["order"][0], last_outcome["order"][0]
    if timestamp != last_timestamp:
        return timestamp > last_timestamp
    if outcome["kind"] == last_outcome["kind"] and outcome["id"] == last_outcome["id"]:
        return outcome["fingerprint"] != last_outcome["fingerprint"]
    return not clean or batch.get("review") is not None


def _coalesced_review_state(
    state: dict[str, Any],
    previous: dict[str, Any] | None,
    baseline: dict[str, Any] | None,
    *,
    reaction_cursor: int = 0,
    initialize: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Advance monotonic observations, independently of notification progress.

    Retain the latest bot outcome even when its response row disappears. Retain
    reaction IDs across head epochs so an old reaction cannot become a new pass.
    """

    head = _current_pr_head_sha(state.get("pull_request"))
    previous = previous or {}
    same_head = str(previous.get("head_sha") or "").casefold() == head.casefold()
    reaction_floor = max(
        reaction_cursor, previous.get("reaction_seen", 0),
        max((int(key) for key in (baseline or {}).get("reactions", {})), default=0),
    )
    batch = dict(previous) if same_head else {
        "head_sha": head, "review": None, "outcome": None, "notified": None,
    }
    batch["reaction_seen"] = max(reaction_floor, max_id(state["reactions"]))
    seen = dict(batch.get("review_seen") or {})
    batch["review_seen"] = seen
    evidence = batch.get("review")
    revoked = False
    if evidence:
        item = next((item for item in state[evidence["kind"]] if item.get("id") == evidence["id"]), None)
        if item is None or _review_observation(evidence["kind"], item)["fingerprint"] != evidence["fingerprint"]:
            batch.update(review=None, notified=None)
            revoked = True

    outcomes: list[tuple[dict[str, Any], bool]] = []
    for kind, fields in (
        ("reviews", ("state", "body", "commit_id")),
        ("issue_comments", ("body",)),
        ("review_comments", ("body", "path")),
    ):
        old_items = (baseline or {}).get(kind, {})
        for item in state[kind]:
            if not _is_codex_item(item) or _is_codex_progress(item):
                continue
            if kind == "reviews" and not _is_actionable_review(item):
                continue
            normalized = _normalized_item_map([item], fields=fields)
            observation = _review_observation(kind, item)
            key = f"{kind}/{item['id']}"
            previous_fingerprint = seen.get(key)
            changed = (
                previous_fingerprint != observation["fingerprint"] if previous_fingerprint is not None
                else old_items.get(str(item.get("id"))) != normalized.get(str(item.get("id")))
            )
            seen[key] = observation["fingerprint"]
            # A replay scans old rows without renewing already-seen verdicts.
            if changed or (initialize and previous_fingerprint is None):
                clean = kind != "review_comments" and _is_codex_clean_pass(item, head)
                outcomes.append((observation, clean))

    if not initialize and same_head:
        outcomes.extend(
            (_review_observation("reactions", item), True) for item in state["reactions"]
            if _is_codex_pass_reaction(item) and int(item["id"]) > reaction_floor
        )
    if outcomes:
        outcome, clean = _select_review_outcome(outcomes)
        last_outcome = batch.get("outcome")
        if _should_adopt_review_outcome(outcome, clean, last_outcome, batch):
            batch["outcome"] = outcome
            batch["review"] = outcome if clean else None
            if not clean:
                batch["notified"] = None
    return batch, revoked and batch.get("review") is None


def _review_thread_state_changes(
    current: dict[str, bool],
    baseline: dict[str, bool],
) -> list[tuple[str, bool | None, bool | None]]:
    return [
        (thread_id, baseline.get(thread_id), current.get(thread_id))
        for thread_id in sorted(current.keys() | baseline.keys())
        if baseline.get(thread_id) != current.get(thread_id)
    ]


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
            raise RuntimeError("GitHub GraphQL response has no reviewThreads connection")
        nodes = connection.get("nodes")
        if not isinstance(nodes, list):
            raise RuntimeError("GitHub GraphQL reviewThreads response has no nodes list")
        threads.extend(node for node in nodes if isinstance(node, dict))
        page_info = connection.get("pageInfo")
        if not isinstance(page_info, dict) or page_info.get("hasNextPage") is not True:
            break
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise RuntimeError("GitHub GraphQL reviewThreads page has no endCursor")
        end_cursor = next_cursor
    return threads, request_count


def _fetch_state(
    repo: str,
    pr_number: int,
    token: str | None,
    *,
    cache: ResponseCache | None = None,
    review_comment_since: str | None = None,
    issue_comment_since: str | None = None,
    ci_sha: str | None = None,
    ci_branch: str | None = None,
    ci_workflows: list[str] | None = None,
    ci_max_pages: int = 3,
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
    # PR state is a mutable snapshot: complete collections are required to detect
    # edits and removals. The legacy since arguments remain accepted so existing
    # callers do not break, but they intentionally do not narrow these requests.
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
    actions: list[dict[str, Any]] = []
    action_requests = 0
    selected_ci_sha = ci_sha or (_current_pr_head_sha(pull_request) if ci_workflows else None)
    if ci_workflows and not selected_ci_sha:
        raise RuntimeError(
            "GitHub PR response has no head SHA for current-head CI monitoring"
        )
    if selected_ci_sha and ci_workflows:
        actions, action_requests = fetch_workflow_runs(
            repo,
            token,
            branch=ci_branch,
            head_sha=selected_ci_sha,
            max_pages=ci_max_pages,
            cache=cache,
        )
        # Actions are fetched after several independent PR requests. Re-read the
        # mutable PR object after that window so a push cannot pair an old head with
        # a terminal result from the exact SHA supplied by the caller.
        pull_request = github_get(f"{base_url}/pulls/{pr_number}", token, cache=cache)
        requests_after_actions = 1
    else:
        requests_after_actions = 0
    if (
        selected_ci_sha
        and ci_workflows
        and _current_pr_head_sha(pull_request).casefold() != selected_ci_sha.casefold()
    ):
        # The PR moved while this snapshot was assembled. The caller will report the
        # head transition; never render Actions fetched for the previous head.
        actions = []
    return (
        {
            "pull_request": pull_request,
            "reviews": reviews,
            "review_comments": review_comments,
            "issue_comments": issue_comments,
            "reactions": reactions,
            "review_threads": review_threads,
            "actions": actions,
        },
        (
            1
            + review_requests
            + review_comment_requests
            + issue_comment_requests
            + reaction_requests
            + review_thread_requests
            + action_requests
            + requests_after_actions
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
    state: dict[str, list[dict[str, Any]]],
    review_cursor: int,
    review_comment_cursor: int,
    issue_comment_cursor: int,
    reaction_cursor: int,
    pr_status: str,
    event_limit: int,
    previous_head_sha: str | None = None,
    snapshot: dict[str, Any] | None = None,
    review_fingerprints: dict[str, str] | None = None,
    review_comment_fingerprints: dict[str, str] | None = None,
    issue_comment_fingerprints: dict[str, str] | None = None,
    review_thread_states: dict[str, bool] | None = None,
    review_threads_available: bool = True,
    viewer_login: str | None = None,
    ignore_self_comments: bool = True,
    actionable_only: bool = False,
    ignored_authors: set[str] | None = None,
    ignore_patterns: list[re.Pattern[str]] | None = None,
    coalesce_success: bool = False,
) -> tuple[str | None, int, int, int, int, str]:
    ignored_authors = ignored_authors or set()
    ignore_patterns = ignore_patterns or []
    review_fingerprints = {} if review_fingerprints is None else review_fingerprints
    review_comment_fingerprints = (
        {} if review_comment_fingerprints is None else review_comment_fingerprints
    )
    issue_comment_fingerprints = (
        {} if issue_comment_fingerprints is None else issue_comment_fingerprints
    )
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
    new_reviews = _changed_items(state["reviews"], review_cursor, review_fingerprints)
    new_review_comments = _changed_items(
        state["review_comments"], review_comment_cursor, review_comment_fingerprints
    )
    new_issue_comments = _changed_items(
        state["issue_comments"], issue_comment_cursor, issue_comment_fingerprints
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
    has_head_event = bool(previous_head_sha and current_head_sha and previous_head_sha != current_head_sha)
    raw_threads = state.get("review_threads")
    current_thread_states = (
        _review_thread_state_map(raw_threads if isinstance(raw_threads, list) else [])
        if review_threads_available
        else dict(review_thread_states or {})
    )
    thread_changes = _review_thread_state_changes(current_thread_states, review_thread_states or {})
    next_review_cursor = max(review_cursor, max_id(state["reviews"]))
    next_review_comment_cursor = max(review_comment_cursor, max_id(state["review_comments"]))
    next_issue_comment_cursor = max(issue_comment_cursor, max_id(state["issue_comments"]))
    next_reaction_cursor = max(reaction_cursor, max_id(state["reactions"]))
    next_pr_status = current_pr_status

    if coalesce_success:
        def quiet(item: dict[str, Any]) -> bool:
            return _is_codex_clean_pass(item, current_head_sha) or _is_codex_progress(item)

        visible_reviews = [item for item in visible_reviews if not quiet(item)]
        visible_issue_comments = [item for item in visible_issue_comments if not quiet(item)]
        defer_reactions = not has_head_event and snapshot is not None
        if defer_reactions:
            new_reactions = []
        current_snapshot = _quiet_success_snapshot(current_snapshot, defer_reactions=defer_reactions)
        if snapshot is not None:
            snapshot = _quiet_success_snapshot(snapshot, defer_reactions=defer_reactions)
    snapshot_changed = snapshot is not None and current_snapshot != snapshot
    if snapshot is not None and not snapshot_changed:
        return (
            None,
            next_review_cursor,
            next_review_comment_cursor,
            next_issue_comment_cursor,
            next_reaction_cursor,
            next_pr_status,
        )
    if snapshot is None and not (
        new_reviews
        or new_review_comments
        or new_issue_comments
        or new_reactions
        or has_pr_status_event
        or has_head_event
        or thread_changes
    ):
        return None, review_cursor, review_comment_cursor, issue_comment_cursor, reaction_cursor, pr_status

    render_pr_status_event = has_pr_status_event and (
        not actionable_only or current_pr_status in ACTIONABLE_PR_STATUSES
    )

    required_events: list[str] = []
    if has_head_event and isinstance(state.get("pull_request"), dict):
        required_events.append(_format_pr_head_event(state["pull_request"], previous_head_sha, current_head_sha))
    if render_pr_status_event and isinstance(state.get("pull_request"), dict):
        required_events.append(_format_pr_status_event(state["pull_request"], pr_status, current_pr_status))
    if isinstance(state.get("pull_request"), dict):
        required_events.extend(
            _format_review_thread_event(state["pull_request"], *change)
            for change in thread_changes
        )
    optional_events = [_format_review(review) for review in visible_reviews]
    optional_events.extend(_format_review_comment(comment) for comment in visible_review_comments)
    optional_events.extend(_format_issue_comment(comment) for comment in visible_issue_comments)

    if not required_events and not optional_events and snapshot_changed:
        required_events.append(
            "- pr_snapshot changed\n"
            "  Gate-relevant PR state changed; re-evaluate the exact head, review verdict, "
            "and all unresolved threads."
        )
    if not required_events and not optional_events and not new_reactions:
        return (
            None,
            next_review_cursor,
            next_review_comment_cursor,
            next_issue_comment_cursor,
            next_reaction_cursor,
            next_pr_status,
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
    # A Codex +1 is durable pass evidence, not ordinary overflow. Always append it
    # after the bounded optional batch so a busy review cannot hide the gate signal.
    lines.extend(_format_reaction(reaction) for reaction in new_reactions)

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


def _startup_failure_exit_code(error: Any) -> int:
    """How a failure *before* the first poll should end this run.

    Nothing has been observed yet, so no activity is lost by ending the run early.
    A transient failure that merely outlasted the bounded startup retries -- a
    truncated body on a large PR, a blip in the network -- must therefore exit
    ``RETRY_EXIT_CODE`` so a managed retry-capable watch can re-arm itself instead
    of dying and leaving the PR unwatched until somebody notices. Genuinely
    terminal failures (a bad token, a PR that does not exist) still exit 1:
    retrying those would poll forever without ever succeeding.
    """

    return RETRY_EXIT_CODE if isinstance(error, InitialRequestRetriesExhausted) else 1


def _watch_identity(
    args: argparse.Namespace,
    *,
    legacy_ci_sha: str | None = None,
    include_ci_max_pages: bool = True,
) -> str:
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

    material_fields = {
        "mode": "new-prs" if args.new_prs else "pr",
        "actionable_only": bool(args.actionable_only),
        "include_self_comments": bool(args.include_self_comments),
        "ignore_authors": sorted(_normalize_authors(args.ignore_author)),
        "ignore_comment_patterns": sorted(set(args.ignore_comment_pattern or [])),
    }
    # Keep the v0.14 PR-only identity stable. Adding CI fields to that hash would
    # make every existing PR-only state file look owned by a different watch after
    # upgrading, even though its report contract has not changed.
    if _ci_enabled(args):
        material_fields.update(
            {
                "ci_branch": args.branch,
                "ci_workflows": list(args.workflow or []),
                "ci_success_conclusions": sorted(
                    _parse_success_conclusions(args.success_conclusion)
                ),
            }
        )
        if include_ci_max_pages:
            material_fields["ci_max_pages"] = args.max_pages
        if legacy_ci_sha is not None:
            material_fields["ci_sha"] = legacy_ci_sha
    if args.coalesce_success:
        material_fields["coalesce_success"] = True
    # Only a non-default stall window is material, so watches seeded before the stall
    # report existed keep their identity.
    if getattr(args, "review_stall_minutes", DEFAULT_REVIEW_STALL_MINUTES) != DEFAULT_REVIEW_STALL_MINUTES:
        material_fields["review_stall_minutes"] = args.review_stall_minutes
    material = json.dumps(material_fields, sort_keys=True)
    return hashlib.sha256(f"wait_pr/{STATE_FILE_VERSION}/{material}".encode()).hexdigest()[:16]


def _legacy_watch_identity_aliases(
    args: argparse.Namespace,
    path: str | None,
) -> set[str]:
    """Return released combined identities that can be migrated at this path.

    Before combined waiting was stabilized, the CI target SHA was part of the watch
    identity. A head change therefore made an otherwise identical watch look foreign.
    Only hashes derived from this path's own recorded PR/Actions head are accepted;
    this is a narrow migration, not a general identity bypass.
    """

    if not path or not _ci_enabled(args):
        return set()
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(payload, dict):
        return set()
    if payload.get("version") != STATE_FILE_VERSION:
        return set()
    if payload.get("repo") != args.repo or payload.get("pr") != args.pr:
        return set()

    candidate_shas: set[str] = set()
    for value in (args.sha, payload.get("head_sha")):
        if isinstance(value, str) and value:
            candidate_shas.add(value)
    actions = payload.get(ACTIONS_SNAPSHOT_KEY)
    if isinstance(actions, dict):
        for runs in actions.values():
            if not isinstance(runs, list):
                continue
            for run in runs:
                if isinstance(run, dict):
                    value = run.get("head_sha")
                    if isinstance(value, str) and value:
                        candidate_shas.add(value)
    aliases = {
        _watch_identity(args, legacy_ci_sha=sha, include_ci_max_pages=False)
        for sha in candidate_shas
    }
    # The released combined identity omitted both the target SHA and page limit.
    # Accept that exact old shape once so adding coverage cannot strand cursors.
    aliases.add(_watch_identity(args, include_ci_max_pages=False))
    return aliases


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
    watch_identity_aliases: set[str] | None = None,
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
    if (
        saved_watch is not None
        and watch_identity is not None
        and saved_watch != watch_identity
        and saved_watch not in (watch_identity_aliases or set())
    ):
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


def _load_state_file(
    path: str | None,
    *,
    repo: str,
    pr_number: int | None,
    watch_identity: str | None = None,
    watch_id: str | None = None,
    watch_identity_aliases: set[str] | None = None,
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
        watch_identity_aliases=watch_identity_aliases,
    )
    if conflict is not None:
        raise StateFileOwnershipError(f"State file {path} {conflict}")
    _validate_saved_actions(payload)
    _validate_saved_success_batch(payload)
    pending = payload.get(STAGED_KEY)
    if isinstance(pending, dict) and isinstance(pending.get("cursors"), dict):
        # Legacy pending transactions may contain only some cursor fields.
        # Validate their effective state before acknowledgement can write it.
        _validate_saved_actions({**payload, **pending["cursors"]})
        _validate_saved_success_batch({**payload, **pending["cursors"]})
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
    watch_identity_aliases: set[str] | None = None,
) -> None:
    """Claim the requested state file, and fail before the first poll if it is unusable.

    A forever watch only discovers a read-only parent directory when the cycle it
    spent minutes on tries to save its cursors, and by then the activity that
    cycle observed is already unrecoverable.

    A missing state file is created here holding nothing but this PR's ownership, so
    the path is owned before any polling starts rather than after the first cursor
    advance. It carries no cursors, so this cycle still baselines from the current PR
    exactly as it did before.

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
            watch_identity_aliases=watch_identity_aliases,
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
    watch_identity_aliases: set[str] | None = None,
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
        watch_identity_aliases=watch_identity_aliases,
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


def _check_state_file_path(path: str | None) -> None:
    """Reject an unusable path without claiming it before remote validation."""

    if not path:
        return
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not target.is_file():
            raise StatePersistenceError(f"Cannot write state file {path}: target is not a regular file")
        if target.parent.stat().st_mode & 0o222 == 0:
            raise StatePersistenceError(f"Cannot write state file {path}: parent directory is read-only")
    except StatePersistenceError:
        raise
    except OSError as err:
        raise StatePersistenceError(f"Cannot write state file {path}: {err}") from err


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
    return {
        str(item_id): fingerprint
        for item_id, fingerprint in value.items()
        if isinstance(fingerprint, str) and fingerprint
    }


def _saved_review_thread_states(saved: dict[str, Any]) -> dict[str, bool]:
    value = saved.get(REVIEW_THREAD_STATES_KEY)
    if not isinstance(value, dict):
        return {}
    return {str(item_id): state for item_id, state in value.items() if isinstance(state, bool)}


def _saved_snapshot(saved: dict[str, Any]) -> dict[str, Any]:
    value = saved.get(PR_SNAPSHOT_KEY)
    return value if isinstance(value, dict) else {}


def _saved_actions_snapshot(saved: dict[str, Any], key: str = ACTIONS_SNAPSHOT_KEY) -> dict[str, Any]:
    return saved.get(key, {})


def _validate_saved_actions(saved: dict[str, Any]) -> None:
    """Reject corrupt history, never filter it into an apparently empty gate."""

    for key in (ACTIONS_SNAPSHOT_KEY, ACTIONS_OBSERVED_KEY):
        if key not in saved:
            continue
        value = saved[key]
        if not isinstance(value, dict):
            raise StateFileUnusableError(f"Saved {key} is malformed: expected a workflow mapping")
        for workflow, runs in value.items():
            if not isinstance(workflow, str) or not isinstance(runs, list):
                raise StateFileUnusableError(f"Saved {key} is malformed: expected workflow run lists")
            for run in runs:
                if not isinstance(run, dict):
                    raise StateFileUnusableError(f"Saved {key} is malformed: expected run objects")
                run_id = run.get("id")
                attempt = run.get("run_attempt")
                if (
                    not isinstance(run_id, (int, str))
                    or isinstance(run_id, bool)
                    or (isinstance(run_id, str) and not run_id)
                    or any(
                        run.get(field) is not None and not isinstance(run[field], str)
                        for field in ("status", "conclusion", "head_sha", "head_branch")
                    )
                    or (
                        attempt is not None
                        and (not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1)
                    )
                ):
                    raise StateFileUnusableError(f"Saved {key} is malformed: invalid run comparison fields")
    if ACTIONS_SNAPSHOT_KEY in saved and ACTIONS_OBSERVED_KEY in saved:
        if set(saved[ACTIONS_SNAPSHOT_KEY]) != set(saved[ACTIONS_OBSERVED_KEY]):
            raise StateFileUnusableError("Saved actions_observed is malformed: workflow inventory differs from actions")


def _validate_saved_success_batch(saved: dict[str, Any]) -> None:
    if SUCCESS_BATCH_KEY not in saved:
        return
    value = saved[SUCCESS_BATCH_KEY]
    if not isinstance(value, dict) or not isinstance(value.get("head_sha"), str):
        raise StateFileUnusableError("Saved success_batch is malformed: expected head-bound state")
    notified = value.get("notified")
    if notified is not None and not isinstance(notified, str):
        raise StateFileUnusableError("Saved success_batch is malformed: invalid notification signature")
    reaction_seen = value.get("reaction_seen", 0)
    if not isinstance(reaction_seen, int) or isinstance(reaction_seen, bool) or reaction_seen < 0:
        raise StateFileUnusableError("Saved success_batch is malformed: invalid reaction watermark")
    threads = value.get("thread_states", {})
    if not isinstance(threads, dict) or any(not isinstance(key, str) or not isinstance(flag, bool) for key, flag in threads.items()):
        raise StateFileUnusableError("Saved success_batch is malformed: invalid observed thread states")
    seen = value.get("review_seen", {})
    if not isinstance(seen, dict) or any(not isinstance(key, str) or not isinstance(fp, str) for key, fp in seen.items()):
        raise StateFileUnusableError("Saved success_batch is malformed: invalid semantic observation history")
    for key in ("review", "outcome"):
        review = value.get(key)
        if review is None:
            continue
        if (
            not isinstance(review, dict)
            or review.get("kind") not in {"reviews", "issue_comments", "review_comments", "reactions"}
            or not isinstance(review.get("id"), (int, str))
            or isinstance(review.get("id"), bool)
            or not isinstance(review.get("fingerprint"), str)
            or not isinstance(review.get("order"), list)
            or len(review["order"]) != 2
            or not isinstance(review["order"][0], str)
            or not isinstance(review["order"][1], int)
        ):
            raise StateFileUnusableError("Saved success_batch is malformed: invalid review evidence")


def _combined_success_signature(selected: dict[str, Any], batch: dict[str, Any]) -> str:
    review = batch["review"]
    material = {
        "head": str(batch["head_sha"]).casefold(),
        "review": {key: review[key] for key in ("kind", "id", "fingerprint")},
        "actions": normalize_selected_runs(selected),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _actions_snapshot_regressed(current: dict[str, Any], previous: dict[str, Any]) -> bool:
    """An incomplete inventory or older attempt cannot establish a new verdict."""

    for workflow, previous_runs in previous.items():
        current_runs = {run.get("id"): run for run in current.get(workflow, [])}
        for previous_run in previous_runs:
            current_run = current_runs.get(previous_run.get("id"))
            if current_run is None:
                return True
            previous_attempt = previous_run.get("run_attempt")
            current_attempt = current_run.get("run_attempt")
            if isinstance(previous_attempt, int) and (
                not isinstance(current_attempt, int) or current_attempt < previous_attempt
            ):
                return True
    return False


def _observe_actions(
    previous: dict[str, Any], current: dict[str, Any], head_sha: str, *, preserve_terminal: bool = False,
) -> dict[str, Any]:
    """Remember known run IDs/attempts without treating observation as delivery."""

    observed = {}
    for workflow, runs in current.items():
        known = {
            run.get("id"): run
            for run in previous.get(workflow, [])
            if str(run.get("head_sha") or "").casefold() == head_sha.casefold()
        }
        for run in runs:
            old_attempt = known.get(run.get("id"), {}).get("run_attempt")
            new_attempt = run.get("run_attempt")
            if isinstance(old_attempt, int) and (
                not isinstance(new_attempt, int) or new_attempt < old_attempt
            ):
                continue
            if (
                preserve_terminal and old_attempt == new_attempt
                and known.get(run.get("id"), {}).get("status") == "completed"
                and run.get("status") != "completed"
            ):
                continue
            known[run.get("id")] = run
        observed[workflow] = list(known.values())
    return normalize_selected_runs(observed)


def _without_older_attempts(
    selected: dict[str, list[dict[str, Any]]], observed: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Missing inventory does not hide a new failure; an older attempt does."""

    result = {}
    for workflow, runs in selected.items():
        known = {run["id"]: run for run in observed.get(workflow, [])}
        result[workflow] = [
            run for run in runs
            if not isinstance(attempt := known.get(run.get("id"), {}).get("run_attempt"), int)
            or (isinstance(run.get("run_attempt"), int) and run["run_attempt"] >= attempt)
        ]
    return result


def _ci_enabled(args: argparse.Namespace) -> bool:
    return bool(args.sha or args.branch or args.workflow or args.success_conclusion)


def _validate_ci_args(args: argparse.Namespace) -> str | None:
    provided = _ci_enabled(args)
    if args.coalesce_success and not args.workflow:
        return "--coalesce-success requires --pr and at least one --workflow"
    if not provided:
        return None
    if args.pr is None:
        return "CI monitoring requires --pr; --new-prs cannot be combined with --sha or --workflow"
    if not args.workflow:
        return "CI monitoring requires at least one --workflow"
    if args.max_pages < 1:
        return "--max-pages must be at least 1"
    if len(set(args.workflow)) != len(args.workflow):
        return "--workflow values must be unique"
    return None


def _parse_success_conclusions(values: list[str] | None) -> set[str]:
    if not values:
        return set(DEFAULT_SUCCESS_CONCLUSIONS)
    result: set[str] = set()
    for value in values:
        result.update(item.strip() for item in value.split(",") if item.strip())
    return result


def _missing_pr_baselines(saved: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if _saved_str(saved, "head_sha") is None:
        missing.append("head_sha")
    for key in PR_FINGERPRINT_KEYS:
        value = saved.get(key)
        if not isinstance(value, dict) or any(not isinstance(item, str) for item in value.values()):
            missing.append(key)
    thread_states = saved.get(REVIEW_THREAD_STATES_KEY)
    if not isinstance(thread_states, dict) or any(not isinstance(item, bool) for item in thread_states.values()):
        missing.append(REVIEW_THREAD_STATES_KEY)
    if not isinstance(saved.get(PR_SNAPSHOT_KEY), dict):
        missing.append(PR_SNAPSHOT_KEY)
    return missing


def _missing_actions_baselines(saved: dict[str, Any], workflows: list[str]) -> list[str]:
    value = saved.get(ACTIONS_SNAPSHOT_KEY)
    if not isinstance(value, dict) or set(value) != set(workflows):
        return [ACTIONS_SNAPSHOT_KEY]
    if any(not isinstance(runs, list) for runs in value.values()):
        return [ACTIONS_SNAPSHOT_KEY]
    return []


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
    watch_identity_aliases: set[str] | None = None,
    return_replay: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], str | None]:
    """Promote acknowledged state or replay its persisted event payload.

    Promoted when the supervisor's last-delivery stamp has moved since the report was
    staged: the report was queued, so polling may start after it. Dropped when the
    stamp is unchanged, which replays the report -- one repeated Agent turn, against
    an event lost for good. Comparing stamps rather than consuming a one-shot ack is
    what makes this survive a restart, and makes a ``once`` watch resumed long after
    its one report promote instead of replaying. Either way the decision is written
    down before polling, so it is taken once.
    """

    staged = saved.get(STAGED_KEY)
    if not isinstance(staged, dict):
        return (saved, None) if return_replay else saved
    cursors = staged.get("cursors")
    if not isinstance(cursors, dict):
        raise StateFileUnusableError("Pending waiter transaction has no usable cursor state")
    replay_output = _staged_replay_output(saved, delivery)
    delivered = staged.get("delivered_after") != delivery

    if replay_output is not None:
        print("An earlier report was never delivered; replaying its persisted output.", file=sys.stderr)
        return (saved, replay_output) if return_replay else saved

    resolved = {key: value for key, value in saved.items() if key != STAGED_KEY}
    if delivered:
        resolved.update(cursors)
    _validate_saved_actions(resolved)
    _validate_saved_success_batch(resolved)
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
        watch_identity_aliases=watch_identity_aliases,
        **fields,
    )
    return (resolved, None) if return_replay else resolved


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
        help=(
            "GitHub login whose review/comment payloads never trigger a follow-up; repeatable. "
            "Independent review-thread status transitions remain visible"
        ),
    )
    parser.add_argument(
        "--ignore-comment-pattern",
        action="append",
        help=(
            "Case-insensitive regex; matching review/comment payloads never trigger a follow-up. "
            "Independent review-thread status transitions remain visible. Repeatable"
        ),
    )
    parser.add_argument(
        "--catch-up",
        action="store_true",
        help="Treat current existing activity as pending when no explicit cursor is provided",
    )
    parser.add_argument(
        "--seed-state",
        action="store_true",
        help="Write a complete current baseline to --state-file and exit without waiting",
    )
    parser.add_argument(
        "--sha",
        help=(
            "Optional exact PR head SHA whose Actions runs should be monitored together with PR "
            "activity. Omit it to follow the PR's current head across watch cycles"
        ),
    )
    parser.add_argument(
        "--branch",
        help="Optional Actions head branch constraint used with --sha and --workflow",
    )
    parser.add_argument(
        "--workflow",
        action="append",
        help=(
            "Actions workflow name to monitor at --sha, or at the PR's current head when --sha is "
            "omitted; repeatable and enables combined CI mode"
        ),
    )
    parser.add_argument(
        "--coalesce-success",
        action="store_true",
        help=(
            "In combined PR/CI mode, defer recognized Codex and CI successes until both are "
            "present at the same head; findings, errors, and unfamiliar activity still report"
        ),
    )
    parser.add_argument(
        "--review-stall-minutes",
        type=float,
        default=DEFAULT_REVIEW_STALL_MINUTES,
        help=(
            "PR mode: report when the current head's Codex review has stayed running, or a "
            "`@codex review` request unanswered, this many minutes, and again each further "
            "interval while it stays stuck; 0 disables. Default 30."
        ),
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=3,
        help="Maximum Actions run-list pages to inspect per poll in combined CI mode",
    )
    parser.add_argument(
        "--success-conclusion",
        action="append",
        help=(
            "Actions conclusion treated as successful in combined CI mode; repeatable or comma-separated. "
            "Defaults to success,skipped,neutral."
        ),
    )
    parser.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        help="Allow polling without GitHub auth; the interval will be clamped to a safer minimum",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()

    ci_error = _validate_ci_args(args)
    if ci_error is not None:
        print(ci_error, file=sys.stderr)
        return 2
    if args.review_stall_minutes < 0:
        print("--review-stall-minutes must be 0 (off) or positive", file=sys.stderr)
        return 2
    ci_enabled = _ci_enabled(args)
    success_conclusions = _parse_success_conclusions(args.success_conclusion)

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
    if args.seed_state and (not args.state_file or args.catch_up):
        print("--seed-state requires --state-file and cannot be combined with --catch-up", file=sys.stderr)
        return 2

    watch_identity = _watch_identity(args)
    watch_identity_aliases = _legacy_watch_identity_aliases(args, args.state_file)
    watch_id = _managed_watch_id()
    delivery_stamp = _last_delivery()
    two_phase = watch_id is not None
    if args.seed_state and two_phase:
        print("--seed-state is a pre-watch command and cannot run as a managed watch", file=sys.stderr)
        return 2
    if two_phase and not args.state_file:
        print(
            "Managed PR watchers require an owner-specific --state-file; refusing to poll without durable state.",
            file=sys.stderr,
        )
        return 2
    _check_state_file_path(args.state_file)
    saved = _load_state_file(
        args.state_file,
        repo=args.repo,
        pr_number=args.pr,
        watch_identity=watch_identity,
        watch_id=watch_id,
        watch_identity_aliases=watch_identity_aliases,
    )
    replay_output = _staged_replay_output(saved, delivery_stamp)
    if replay_output is not None:
        print(
            "An earlier report was never delivered; replaying it before GitHub preflight.",
            file=sys.stderr,
        )
        _deliver(replay_output)
        return 0
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

    token_fingerprint = _token_fingerprint(token)
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
                lambda: get_authenticated_login(token, raise_on_error=True),
                description="GitHub viewer lookup",
                unauthenticated=token is None,
            )
            if viewer_result.error is not None:
                print(f"GitHub viewer lookup failed: {viewer_result.error}", file=sys.stderr)
                return _startup_failure_exit_code(viewer_result.error)
            viewer_login = viewer_result.value
        if token is not None and not viewer_login:
            print(
                "Could not resolve the authenticated GitHub login; refusing to poll while self-comment filtering is enabled.",
                file=sys.stderr,
            )
            return 1

    base_interval = max(args.interval, 1.0)
    effective_interval = base_interval
    settle_seconds = max(args.settle, 0.0)

    start = time.monotonic()

    saved_pr_cursor = None if args.catch_up else _saved_int(saved, "pr_cursor")
    since_pr_id = args.since_pr_id if args.since_pr_id is not None else saved_pr_cursor
    initial_resume = all(_saved_int(saved, key) is not None for key in STATE_CURSOR_KEYS)
    initial_review_comment_since = (
        _saved_str(saved, "review_comment_since")
        if initial_resume and args.since_review_comment_id is None
        else None
    )
    initial_issue_comment_since = (
        _saved_str(saved, "issue_comment_since")
        if initial_resume and args.since_issue_comment_id is None
        else None
    )

    def _fetch_initial_state() -> tuple[dict[str, Any], int]:
        if args.pr is not None:
            return _fetch_state(
                args.repo,
                args.pr,
                token,
                cache=cache,
                review_comment_since=initial_review_comment_since,
                issue_comment_since=initial_issue_comment_since,
                ci_sha=args.sha if ci_enabled else None,
                ci_branch=args.branch if ci_enabled else None,
                ci_workflows=args.workflow if ci_enabled else None,
                ci_max_pages=args.max_pages,
            )
        initial_pr_stop_after_id = None
        initial_pr_max_pages = None
        if since_pr_id is not None and not args.catch_up:
            initial_pr_stop_after_id = since_pr_id
        elif not args.catch_up:
            initial_pr_max_pages = 1
        return _fetch_new_pr_state(
            args.repo,
            token,
            stop_after_id=initial_pr_stop_after_id,
            max_pages=initial_pr_max_pages,
            cache=cache,
        )

    initial_request = retry_initial_request(
        _fetch_initial_state,
        description="initial GitHub PR state request",
        unauthenticated=token is None,
    )
    if initial_request.error is not None:
        print(f"Failed to fetch initial PR state: {initial_request.error}", file=sys.stderr)
        return _startup_failure_exit_code(initial_request.error)
    if initial_request.value is None:
        print("Initial GitHub PR state request completed without a result", file=sys.stderr)
        return 1
    selected_actions: dict[str, list[dict[str, Any]]] = {}
    state, requests_per_poll_count = initial_request.value
    if args.pr is not None and ci_enabled and args.sha:
        observed_head_sha = _current_pr_head_sha(state.get("pull_request"))
        if observed_head_sha.casefold() != args.sha.casefold():
            print(
                "Refusing to start a combined watch: PR head "
                f"{observed_head_sha} does not match --sha {args.sha}. "
                "Refresh the SHA and retry.",
                file=sys.stderr,
            )
            return 2
    if ci_enabled:
        selected_ci_sha = args.sha or _current_pr_head_sha(state.get("pull_request"))
        selected_actions = select_matching_runs(
            state.get("actions", []),
            workflows=args.workflow or [],
            branch=args.branch,
            head_sha=selected_ci_sha,
        )

    # The remote target is now proven valid. Only now may this run create or adopt
    # its state path; a typo or inaccessible PR must leave no ownership claim.
    _verify_state_file_writable(
        args.state_file,
        repo=args.repo,
        pr_number=args.pr,
        watch_identity=watch_identity,
        watch_id=watch_id,
        watch_identity_aliases=watch_identity_aliases,
    )
    saved = _load_state_file(
        args.state_file,
        repo=args.repo,
        pr_number=args.pr,
        watch_identity=watch_identity,
        watch_id=watch_id,
        watch_identity_aliases=watch_identity_aliases,
    )
    saved, replay_output = _resolve_staged_state(
        args.state_file,
        saved,
        delivery=delivery_stamp,
        repo=args.repo,
        pr_number=args.pr,
        watch_identity=watch_identity,
        watch_id=watch_id,
        watch_identity_aliases=watch_identity_aliases,
        return_replay=True,
    )
    if replay_output is not None:
        _deliver(replay_output)
        return 0

    resume_cursors = {key: _saved_int(saved, key) for key in STATE_CURSOR_KEYS}
    resumed = not args.catch_up and all(value is not None for value in resume_cursors.values())
    explicit_replay = args.pr is not None and any(
        value is not None
        for value in (
            args.since_review_id,
            args.since_review_comment_id,
            args.since_issue_comment_id,
            args.since_reaction_id,
            args.since_pr_status,
        )
    )
    missing_baselines = _missing_pr_baselines(saved) if resumed and args.pr is not None else []
    if ci_enabled and resumed and args.pr is not None:
        missing_baselines.extend(_missing_actions_baselines(saved, args.workflow or []))
    if missing_baselines and not explicit_replay:
        print(
            "Saved PR state lacks required baseline(s): %s; use --catch-up, or remove "
            "and reseed this legacy state file before resuming."
            % ", ".join(missing_baselines),
            file=sys.stderr,
        )
        return 2
    if two_phase and not args.catch_up:
        seeded = _saved_int(saved, "pr_cursor") is not None if args.new_prs else resumed
        if not seeded:
            print(
                "Managed first watch requires a state file seeded before the watched action.",
                file=sys.stderr,
            )
            return 2

    saved_pr_cursor = None if args.catch_up else _saved_int(saved, "pr_cursor")
    since_pr_id = args.since_pr_id if args.since_pr_id is not None else saved_pr_cursor
    tracked_head_sha = _saved_str(saved, "head_sha")
    # A state file from before the stall report has none, which reads as nothing
    # reported yet: a review already stuck then is reported on the first cycle.
    reported_stall = (_saved_str(saved, REVIEW_STALL_KEY) or "") if resumed else ""
    saved_stall_head = saved.get(REVIEW_STALL_HEAD_KEY) if resumed else None
    stall_head: dict[str, str] = (
        {"sha": str(saved_stall_head["sha"]), "at": str(saved_stall_head["at"])}
        if isinstance(saved_stall_head, dict)
        and isinstance(saved_stall_head.get("sha"), str)
        and _github_time(saved_stall_head.get("at")) is not None
        else {}
    )
    review_fingerprints = _saved_fingerprints(saved, REVIEW_FINGERPRINTS_KEY)
    review_comment_fingerprints = _saved_fingerprints(saved, REVIEW_COMMENT_FINGERPRINTS_KEY)
    issue_comment_fingerprints = _saved_fingerprints(saved, ISSUE_COMMENT_FINGERPRINTS_KEY)
    review_thread_states = _saved_review_thread_states(saved)
    latest_review_thread_states = dict((saved.get(SUCCESS_BATCH_KEY) or {}).get("thread_states", review_thread_states))
    # Normal monitoring always compares one complete normalized snapshot. Numeric
    # cursors, fingerprints, and thread maps only explain that change in the report;
    # they never decide independently whether to wake. Explicit replay/catch-up is
    # the deliberate exception: there the requested cursor streams are the contract.
    snapshot = None if args.catch_up or explicit_replay else (_saved_snapshot(saved) if resumed else None)
    if not ci_enabled:
        actions_snapshot = None
    elif args.catch_up:
        actions_snapshot = None
    elif explicit_replay:
        # Explicit cursor replay is a PR activity request, not a request to
        # replay an already observed terminal Actions result.
        actions_snapshot = normalize_selected_runs(selected_actions)
    elif resumed:
        actions_snapshot = normalize_selected_runs(_saved_actions_snapshot(saved))
    else:
        # Explicit PR cursor replay does not replay an unrelated already-terminal
        # Actions result. A fresh normal watch still needs the current CI snapshot
        # as its baseline to avoid waking on the seed itself.
        actions_snapshot = normalize_selected_runs(selected_actions)
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

    def _pr_observation_snapshot(pr_state: dict[str, Any]) -> dict[str, Any]:
        return _normalized_pr_snapshot(
            pr_state, viewer_login=viewer_login,
            ignore_self_comments=not args.include_self_comments,
            actionable_only=args.actionable_only, ignored_authors=ignored_authors,
            ignore_patterns=ignore_patterns,
            committed_snapshot={"review_threads": dict(
                latest_review_thread_states if args.coalesce_success else review_thread_states
            )},
            review_threads_available=token is not None,
        )

    observed_head_sha = _current_pr_head_sha(state.get("pull_request"))

    if args.pr is not None and not resumed and not args.catch_up and not explicit_replay:
        tracked_head_sha = observed_head_sha
        review_fingerprints = _fingerprint_map(state["reviews"])
        review_comment_fingerprints = _fingerprint_map(state["review_comments"])
        issue_comment_fingerprints = _fingerprint_map(state["issue_comments"])
        if token is not None:
            raw_threads = state.get("review_threads")
            review_thread_states = _review_thread_state_map(
                raw_threads if isinstance(raw_threads, list) else []
            )
        snapshot = _pr_observation_snapshot(state)
        actions_snapshot = normalize_selected_runs(selected_actions) if ci_enabled else None

    # Explicit PR replay baselines CI at the current head, independently of the
    # historical PR cursor. Explicit seed likewise adopts the current snapshot.
    if ci_enabled and args.seed_state:
        actions_snapshot = normalize_selected_runs(selected_actions)
    actions_head_sha = observed_head_sha if explicit_replay or args.seed_state else tracked_head_sha
    actions_observed = (
        _observe_actions(
            # Only a normal resume inherits old inventory. Replaying or
            # establishing a fresh baseline must not retain omitted old runs.
            {} if not resumed or args.seed_state or explicit_replay else (
                _saved_actions_snapshot(saved, ACTIONS_OBSERVED_KEY)
                if ACTIONS_OBSERVED_KEY in saved else _saved_actions_snapshot(saved)
            ),
            normalize_selected_runs(selected_actions),
            args.sha or observed_head_sha,
            preserve_terminal=args.coalesce_success,
        )
        if ci_enabled else {}
    )

    def _visible_success_state() -> dict[str, Any]:
        visible = dict(state)
        for kind in ("reviews", "review_comments", "issue_comments"):
            visible[kind] = _visible_activity_items(
                state[kind], viewer_login=viewer_login,
                ignore_self_comments=not args.include_self_comments,
                ignored_authors=ignored_authors, ignore_patterns=ignore_patterns,
            )
        return visible

    success_batch = (
        saved.get(SUCCESS_BATCH_KEY) or {
            "head_sha": tracked_head_sha or observed_head_sha, "review": None,
            "outcome": None, "notified": None, "reaction_seen": _saved_int(saved, "reaction_cursor") or 0,
        }
    ) if args.coalesce_success else None
    if args.coalesce_success and token is not None:
        latest_review_thread_states = _review_thread_state_map(state.get("review_threads") or [])
    if args.coalesce_success and (args.seed_state or not resumed or explicit_replay):
        # Deliberate replay must not rebuild monotonic review history from the
        # current response: omitted rows can otherwise resurrect an older pass
        # over a newer invalidating outcome. A seed is an explicit new baseline.
        prior_success_batch = success_batch if (args.catch_up or explicit_replay) else None
        success_batch, _ = _coalesced_review_state(
            _visible_success_state(), prior_success_batch, snapshot, initialize=True,
            reaction_cursor=_saved_int(saved, "reaction_cursor") or 0,
        )
        _baseline_output, baseline_failed = render_actions_result(
            repo=args.repo, branch=args.branch, head_sha=args.sha or observed_head_sha,
            selected=selected_actions, success_conclusions=success_conclusions,
        )
        if (
            not args.catch_up and success_batch["review"] and _baseline_output and not baseline_failed
            and all(_pr_observation_snapshot(state)["review_threads"].values())
        ):
            success_batch["notified"] = _combined_success_signature(selected_actions, success_batch)
    if args.coalesce_success:
        success_batch = {**success_batch, "thread_states": dict(latest_review_thread_states)}
    success_observed = success_batch
    success_observation_snapshot = snapshot
    success_observation_state = None
    success_revoked = False

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

        def _active_ci_head_sha() -> str:
            return args.sha or _current_pr_head_sha(state.get("pull_request"))

        def _same_actions_head() -> bool:
            return actions_head_sha is not None and actions_head_sha.casefold() == _active_ci_head_sha().casefold()

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

        def _observe_success_state() -> None:
            nonlocal success_observed, success_observation_snapshot, success_observation_state, success_revoked
            nonlocal latest_review_thread_states
            if not args.coalesce_success or state is success_observation_state:
                return
            visible = _visible_success_state()
            if token is not None:
                latest_review_thread_states = _review_thread_state_map(visible.get("review_threads") or [])
            success_observed, revoked = _coalesced_review_state(
                visible, success_observed, success_observation_snapshot, reaction_cursor=reaction_cursor,
            )
            success_observed["thread_states"] = dict(latest_review_thread_states)
            success_revoked = success_revoked or revoked
            success_observation_snapshot = _pr_observation_snapshot(visible)
            success_observation_state = state

        def _active_actions() -> dict[str, list[dict[str, Any]]]:
            if not args.coalesce_success:
                return selected_actions
            active = _without_older_attempts(selected_actions, actions_observed)
            for workflow, observed_runs in actions_observed.items():
                by_id = {run["id"]: run for run in active.get(workflow, [])}
                for run in observed_runs:
                    if run.get("status") == "completed" and run.get("conclusion") not in success_conclusions:
                        payload = by_id.get(run["id"], {})
                        by_id[run["id"]] = {
                            **payload, **run, "name": workflow,
                            "html_url": payload.get("html_url") or f"https://github.com/{args.repo}/actions/runs/{run['id']}",
                        }
                active[workflow] = list(by_id.values())
            return active

        def _render(cursors: tuple[int, int, int, int, str]) -> PRReport:
            activity = _render_activity(
                repo=args.repo,
                pr_number=args.pr,
                state=state,
                review_cursor=cursors[0],
                review_comment_cursor=cursors[1],
                issue_comment_cursor=cursors[2],
                reaction_cursor=cursors[3],
                pr_status=cursors[4],
                event_limit=args.event_limit,
                previous_head_sha=tracked_head_sha,
                snapshot=snapshot,
                review_fingerprints=dict(review_fingerprints),
                review_comment_fingerprints=dict(review_comment_fingerprints),
                issue_comment_fingerprints=dict(issue_comment_fingerprints),
                review_thread_states=review_thread_states,
                review_threads_available=token is not None,
                viewer_login=viewer_login,
                ignore_self_comments=not args.include_self_comments,
                actionable_only=args.actionable_only,
                ignored_authors=ignored_authors,
                ignore_patterns=ignore_patterns,
                coalesce_success=args.coalesce_success and not (args.catch_up or explicit_replay),
            )
            return PRReport(activity[0], activity[1:], state)

        def _render_combined(
            cursors: tuple[int, int, int, int, str],
            *,
            fallback_pr: PRReport | None = None,
        ) -> PRReport:
            pr_result = _render(cursors)
            if pr_result.output is None and fallback_pr is not None and fallback_pr.output is not None:
                pr_result = fallback_pr
            if not ci_enabled:
                return pr_result

            _observe_success_state()
            if args.coalesce_success:
                batch = dict(success_observed)
                # Observation history is independently head-tagged. Preserve a
                # fresh latest-head verdict even when the PR report retains an
                # older head event; the CI guard below still forbids mixing them.
                pr_result = pr_result._replace(success_batch=batch)

            # A transient PR head can itself be retained as evidence. Never
            # attach another head's CI verdict to that observation's baseline.
            if _current_pr_head_sha(pr_result.pr_state.get("pull_request")).casefold() != (
                _current_pr_head_sha(state.get("pull_request")).casefold()
            ):
                return pr_result

            if args.coalesce_success:
                if success_revoked and batch.get("review") is None and pr_result.output is None:
                    pr_result = pr_result._replace(output=(
                        f"Codex review evidence changed or was removed for {args.repo}#{args.pr}; "
                        "re-evaluate the current head."
                    ))
                ci_output, failed = render_actions_result(
                    repo=args.repo, branch=args.branch, head_sha=_active_ci_head_sha(),
                    selected=_active_actions(), success_conclusions=success_conclusions,
                    fail_fast=True,
                )
                unresolved = not all(success_observation_snapshot["review_threads"].values())
                if failed or unresolved:
                    batch["notified"] = None
                elif (
                    ci_output and batch.get("review")
                    and str(batch["head_sha"]).casefold() == _active_ci_head_sha().casefold()
                    and not _actions_snapshot_regressed(normalize_selected_runs(selected_actions), actions_observed)
                ):
                    signature = _combined_success_signature(selected_actions, batch)
                    if batch.get("notified") != signature:
                        evidence = batch["review"]
                        item = next(item for item in state[evidence["kind"]] if item.get("id") == evidence["id"])
                        formatter = {
                            "reviews": _format_review, "issue_comments": _format_issue_comment,
                            "reactions": _format_reaction,
                        }[evidence["kind"]]
                        output = (
                            f"CI + Codex review success candidate for {args.repo}#{args.pr}"
                            f"@{_active_ci_head_sha()[:12]}; verify all delivery gates before close-out.\n"
                            f"{formatter(item)}\n{ci_output}"
                        )
                        if pr_result.output:
                            output = f"{pr_result.output}\n{output}"
                        batch["notified"] = signature
                        return pr_result._replace(
                            output=output, actions=normalize_selected_runs(selected_actions),
                        )
                # A green CI result alone must not advance its delivery baseline.
                # The review evidence above is still adopted on quiet/problem polls.
                # A reported verdict the waiter could not classify is the Agent's to
                # judge, so the green result that judgment may be waiting on is reported.
                if not failed and not (
                    ci_output and not unresolved
                    and str(batch["head_sha"]).casefold() == _active_ci_head_sha().casefold()
                    and _awaits_review_judgment(state, batch)
                ):
                    return pr_result

            actions_output = _render_new_actions()
            if actions_output is None:
                return pr_result
            reported_actions = normalize_selected_runs(_active_actions())
            output = actions_output if pr_result.output is None else f"{pr_result.output}\n{actions_output}"
            return pr_result._replace(
                output=output, actions=reported_actions, failed_actions=args.coalesce_success and failed,
            )

        def _render_new_actions() -> str | None:
            active_actions = _active_actions()
            current_actions = normalize_selected_runs(active_actions)
            # A push starts a new CI epoch even if it returns to an earlier SHA.
            previous = actions_snapshot if _same_actions_head() else None
            if previous is not None and current_actions == previous:
                return None
            output, failed = render_actions_result(
                repo=args.repo,
                branch=args.branch,
                head_sha=_active_ci_head_sha(),
                selected=active_actions,
                success_conclusions=success_conclusions,
                fail_fast=args.coalesce_success,
            )
            if _actions_snapshot_regressed(current_actions, actions_observed) and not (args.coalesce_success and failed):
                return None
            if args.coalesce_success and failed and previous is not None:
                # Other workflows finishing must not reannounce the same failure.
                known = {
                    (workflow, run["id"]): run for workflow, runs in previous.items() for run in runs
                }
                failures = [
                    (workflow, run) for workflow, runs in current_actions.items() for run in runs
                    if run.get("status") == "completed" and run.get("conclusion") not in success_conclusions
                ]
                if all(known.get((workflow, run["id"])) == run for workflow, run in failures):
                    return None
            return output

        def _advance_actions_baseline(report: PRReport) -> None:
            nonlocal actions_snapshot, actions_head_sha
            if not ci_enabled:
                return
            # Only a reported terminal result advances the notification baseline.
            # Quiet polls and PR-only reports must not erase an acknowledged CI
            # result. The existing pending transaction covers this state too.
            report_head = args.sha or _current_pr_head_sha(report.pr_state.get("pull_request"))
            if report.actions is not None:
                actions_snapshot = (
                    _observe_actions(actions_snapshot or {}, report.actions, report_head, preserve_terminal=True)
                    if args.coalesce_success else report.actions
                )
            elif actions_head_sha is None or actions_head_sha.casefold() != report_head.casefold():
                actions_snapshot = {workflow: [] for workflow in args.workflow}
            actions_head_sha = report_head

        def _refresh_actions() -> None:
            nonlocal selected_actions, actions_observed
            if ci_enabled:
                selected_actions = select_matching_runs(
                    state.get("actions", []),
                    workflows=args.workflow or [],
                    branch=args.branch,
                    head_sha=_active_ci_head_sha(),
                )
                actions_observed = _observe_actions(
                    actions_observed, normalize_selected_runs(selected_actions), _active_ci_head_sha(),
                    preserve_terminal=args.coalesce_success,
                )
            _observe_success_state()

        def _actions_waiting_for_terminal_result() -> bool:
            if not ci_enabled:
                return False
            current_actions = normalize_selected_runs(selected_actions)
            previous = actions_snapshot if _same_actions_head() else None
            actions_output, failed = render_actions_result(
                repo=args.repo,
                branch=args.branch,
                head_sha=_active_ci_head_sha(),
                selected=_active_actions(),
                success_conclusions=success_conclusions,
                fail_fast=args.coalesce_success,
            )
            if args.coalesce_success and failed:
                return False
            if _actions_snapshot_regressed(current_actions, actions_observed):
                return True
            if previous is not None and current_actions == previous:
                return False
            return actions_output is None

        def _with_review_stall(output: str | None) -> str | None:
            """Add a Codex review stall not yet reported to this cycle's output.

            Called after the cycle's ``pre_event_fields`` were taken, so a staged report
            keeps the previous stall key committed until its delivery is confirmed.
            """
            nonlocal reported_stall, stall_head
            now = _utc_now()
            head = _current_pr_head_sha(state.get("pull_request"))
            if head and stall_head.get("sha", "").casefold() != head.casefold():
                stall_head = {"sha": head, "at": now.isoformat()}
            if args.seed_state or args.catch_up or explicit_replay or not head:
                return output
            stall = _codex_review_stall(
                state,
                repo=args.repo,
                stall_seconds=args.review_stall_minutes * 60,
                now=now,
                head_seen_at=_github_time(stall_head["at"]) or now,
            )
            if stall is None or stall[0] == reported_stall:
                return output
            reported_stall = stall[0]
            return stall[1] if output is None else f"{output}\n\n{stall[1]}"

        def _adopt_report(report: PRReport) -> None:
            nonlocal review_cursor, review_comment_cursor, issue_comment_cursor, reaction_cursor, pr_status
            nonlocal observed_head_sha, snapshot, success_batch
            nonlocal review_fingerprints, review_comment_fingerprints, issue_comment_fingerprints, review_thread_states
            nonlocal review_comment_since, issue_comment_since
            review_cursor, review_comment_cursor, issue_comment_cursor, reaction_cursor, pr_status = report.cursors
            pr_state = report.pr_state
            observed_head_sha = _current_pr_head_sha(pr_state.get("pull_request"))
            review_comment_since = later_since(review_comment_since, pr_state["review_comments"])
            issue_comment_since = later_since(issue_comment_since, pr_state["issue_comments"])
            review_fingerprints = _fingerprint_map(pr_state["reviews"])
            review_comment_fingerprints = _fingerprint_map(pr_state["review_comments"])
            issue_comment_fingerprints = _fingerprint_map(pr_state["issue_comments"])
            if token is not None:
                raw_threads = pr_state.get("review_threads")
                review_thread_states = _review_thread_state_map(
                    raw_threads if isinstance(raw_threads, list) else []
                )
            snapshot = _pr_observation_snapshot(pr_state)
            _advance_actions_baseline(report)
            if report.success_batch is not None:
                success_batch = report.success_batch

        def _pr_state_fields() -> dict[str, Any]:
            fields = {
                "review_cursor": review_cursor,
                "review_comment_cursor": review_comment_cursor,
                "issue_comment_cursor": issue_comment_cursor,
                "reaction_cursor": reaction_cursor,
                "pr_status": pr_status,
                "review_comment_since": review_comment_since,
                "issue_comment_since": issue_comment_since,
                "viewer_login": viewer_login,
                "token_fingerprint": token_fingerprint,
                "head_sha": observed_head_sha,
                REVIEW_FINGERPRINTS_KEY: dict(review_fingerprints),
                REVIEW_COMMENT_FINGERPRINTS_KEY: dict(review_comment_fingerprints),
                ISSUE_COMMENT_FINGERPRINTS_KEY: dict(issue_comment_fingerprints),
                REVIEW_THREAD_STATES_KEY: dict(review_thread_states),
                PR_SNAPSHOT_KEY: snapshot or {},
                REVIEW_STALL_KEY: reported_stall,
                REVIEW_STALL_HEAD_KEY: dict(stall_head),
            }
            if ci_enabled:
                fields[ACTIONS_SNAPSHOT_KEY] = actions_snapshot or {workflow: [] for workflow in args.workflow}
                fields[ACTIONS_OBSERVED_KEY] = actions_observed
            if args.coalesce_success:
                fields[SUCCESS_BATCH_KEY] = success_batch
            return fields

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
                watch_identity_aliases=watch_identity_aliases,
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
            first: PRReport,
            pending: tuple[int, int, int, int, str],
        ) -> PRReport:
            """Re-poll while a batch is still landing so it costs one Agent turn."""

            nonlocal state
            if settle_seconds <= 0 or first.failed_actions:
                return first

            best = first
            best_pr = _render(pending)

            def _fallback() -> PRReport:
                # Revalidate against the latest successful poll, not a transient
                # earlier candidate that may now equal the delivered baseline.
                # PR evidence remains at-least-once even if a later collection
                # omits it; only the provisional CI verdict can be withdrawn.
                return _render_combined(pending, fallback_pr=best_pr)

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
                        return _fallback()
                time.sleep(settle_seconds)
                settle_request = github_request(
                    lambda: _fetch_state(
                        args.repo,
                        args.pr,
                        token,
                        cache=cache,
                        ci_sha=args.sha if ci_enabled else None,
                        ci_branch=args.branch if ci_enabled else None,
                        ci_workflows=args.workflow if ci_enabled else None,
                        ci_max_pages=args.max_pages,
                    ),
                    unauthenticated=token is None,
                )
                if settle_request.error is not None:
                    # Settling only coalesces a batch. Once an event is known, no
                    # enrichment failure may suppress it; the follow-up re-fetches
                    # live state and can handle a terminal API problem explicitly.
                    print(
                        f"Settle re-poll failed: {settle_request.error}; reporting the batch seen so far.",
                        file=sys.stderr,
                    )
                    return _fallback()
                if settle_request.value is None:
                    print(
                        "Settle re-poll returned no state; reporting the batch seen so far.",
                        file=sys.stderr,
                    )
                    return _fallback()
                state, _count = settle_request.value
                _refresh_actions()
                pr_candidate = _render(pending)
                if pr_candidate.output is not None:
                    best_pr = pr_candidate
                if _actions_waiting_for_terminal_result():
                    continue
                # Rendered from the same cursors as the first hit, so the result is a
                # superset rather than a second, partial report.
                candidate = _fallback()
                if candidate.failed_actions:
                    return candidate
                if candidate.output is None:
                    return candidate
                if candidate.cursors == best.cursors:
                    return candidate
                best = candidate
            return _fallback()

        pending_cursors = (
            review_cursor,
            review_comment_cursor,
            issue_comment_cursor,
            reaction_cursor,
            pr_status,
        )
        # Until a report is adopted, the committed PR head must stay paired
        # with the saved snapshot, even if the first fetch already saw a push.
        observed_head_sha = tracked_head_sha or observed_head_sha
        pre_event_fields = _pr_state_fields()
        initial_result = (
            PRReport(None, pending_cursors, state)
            if args.seed_state
            else _render_combined(pending_cursors)
        )
        if initial_result.output is not None and not args.catch_up:
            initial_result = _settle(initial_result, pending_cursors)
        initial_output = initial_result.output
        _adopt_report(initial_result)
        initial_output = _with_review_stall(initial_output)
        if initial_output is None:
            # Persisted even with nothing to report: the baseline this cycle
            # established is exactly what the next cycle must resume from.
            _persist_pr_state()
            tracked_head_sha = observed_head_sha
            if args.seed_state:
                print(f"Seeded GitHub PR baseline in {args.state_file}", file=sys.stderr)
                return 0
        else:
            _write_cursor_output(
                args.cursor_output,
                review_cursor=review_cursor,
                review_comment_cursor=review_comment_cursor,
                issue_comment_cursor=issue_comment_cursor,
                reaction_cursor=reaction_cursor,
                pr_status=pr_status,
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
            if args.seed_state:
                print(f"Seeded GitHub repository PR baseline in {args.state_file}", file=sys.stderr)
                return 0
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
                    ci_sha=args.sha if ci_enabled else None,
                    ci_branch=args.branch if ci_enabled else None,
                    ci_workflows=args.workflow if ci_enabled else None,
                    ci_max_pages=args.max_pages,
                ),
                unauthenticated=token is None,
            )
        else:
            poll_request = github_request(
                lambda: _fetch_new_pr_state(
                    args.repo,
                    token,
                    stop_after_id=pr_cursor if pr_cursor > 0 else None,
                    cache=cache,
                ),
                unauthenticated=token is None,
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
            _refresh_actions()
            pending_cursors = (
                review_cursor,
                review_comment_cursor,
                issue_comment_cursor,
                reaction_cursor,
                pr_status,
            )
            pre_event_fields = _pr_state_fields()
            result = _render_combined(pending_cursors)
            if result.output is not None:
                result = _settle(result, pending_cursors)
            output = result.output
            _adopt_report(result)
            output = _with_review_stall(output)
            if output is None:
                # Cursors also move when everything new was filtered out, and that
                # progress has to survive the cycle or the next one re-examines it.
                # Nothing is being reported, so there is no delivery to wait for.
                _persist_pr_state()
                tracked_head_sha = observed_head_sha
                continue

            _write_cursor_output(
                args.cursor_output,
                review_cursor=review_cursor,
                review_comment_cursor=review_comment_cursor,
                issue_comment_cursor=issue_comment_cursor,
                reaction_cursor=reaction_cursor,
                pr_status=pr_status,
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
