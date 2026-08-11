from __future__ import annotations

import io
import importlib.util
import json
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest


def _load_module():
    script_path = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "background-watch-hook"
        / "scripts"
        / "wait_pr.py"
    )
    spec = importlib.util.spec_from_file_location("wait_pr", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    module.github_graphql = lambda *_args, **_kwargs: {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
    }
    return module


def test_render_activity_includes_codex_pr_body_reaction() -> None:
    module = _load_module()
    state = {
        "pull_request": {"number": 153, "state": "open", "draft": False},
        "reviews": [],
        "review_comments": [],
        "issue_comments": [],
        "reactions": [
            {
                "id": 123,
                "content": "+1",
                "created_at": "2026-04-02T13:05:42Z",
                "user": {"login": "chatgpt-codex-connector[bot]"},
            }
        ],
    }

    output, review_cursor, review_comment_cursor, issue_comment_cursor, reaction_cursor, pr_status = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
    )

    assert output is not None
    assert "pr_reaction #123" in output
    assert "chatgpt-codex-connector[bot]" in output
    assert reaction_cursor == 123
    assert review_cursor == 0
    assert review_comment_cursor == 0
    assert issue_comment_cursor == 0
    assert pr_status == "open"


def test_render_activity_accepts_api_codex_login_and_keeps_reaction_outside_limit() -> None:
    module = _load_module()
    state = {
        "pull_request": {"number": 153, "state": "open", "draft": False},
        "reviews": [],
        "review_comments": [
            {
                "id": 501,
                "body": "Fix this",
                "path": "core/watches.py",
                "html_url": "https://github.com/example/repo/pull/153#discussion_r501",
                "user": {"login": "reviewer"},
            }
        ],
        "issue_comments": [],
        "reactions": [
            {
                "id": 601,
                "content": "+1",
                "created_at": "2026-04-02T13:05:42Z",
                "user": {"login": "chatgpt-codex-connector"},
            }
        ],
    }

    output, *_rest = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=1,
    )

    assert output is not None
    assert "review_comment #501" in output
    assert "pr_reaction #601" in output
    assert "additional event(s) omitted" not in output


def test_render_activity_reports_a_changed_pr_head() -> None:
    module = _load_module()
    state = {
        "pull_request": {
            "number": 153,
            "state": "open",
            "draft": False,
            "head": {"sha": "new-head"},
            "html_url": "https://github.com/example/repo/pull/153",
        },
        "reviews": [],
        "review_comments": [],
        "issue_comments": [],
        "reactions": [],
    }

    output, *_rest = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        previous_head_sha="old-head",
    )

    assert output is not None
    assert "pr_head #153 old-head -> new-head" in output


def test_render_activity_reports_an_edited_existing_comment() -> None:
    module = _load_module()
    comment = {
        "id": 501,
        "body": "Initial text",
        "path": "core/watches.py",
        "created_at": "2026-04-02T13:05:42Z",
        "updated_at": "2026-04-02T13:05:42Z",
        "html_url": "https://github.com/example/repo/pull/153#discussion_r501",
        "user": {"login": "reviewer"},
    }
    state = _pr_state(review_comments=[comment])
    fingerprints: dict[str, str] = {}

    first = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        review_comment_fingerprints=fingerprints,
    )
    assert first[0] is not None

    unchanged = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=first[1],
        review_comment_cursor=first[2],
        issue_comment_cursor=first[3],
        reaction_cursor=first[4],
        pr_status=first[5],
        event_limit=8,
        review_comment_fingerprints=fingerprints,
    )
    assert unchanged[0] is None

    edited = dict(comment, body="Corrected text", updated_at="2026-04-02T13:10:42Z")
    changed = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=_pr_state(review_comments=[edited]),
        review_cursor=unchanged[1],
        review_comment_cursor=unchanged[2],
        issue_comment_cursor=unchanged[3],
        reaction_cursor=unchanged[4],
        pr_status=unchanged[5],
        event_limit=8,
        review_comment_fingerprints=fingerprints,
    )
    assert changed[0] is not None
    assert "Corrected text" in changed[0]


def test_render_activity_ignores_non_codex_or_non_plus_one_reactions() -> None:
    module = _load_module()
    state = {
        "pull_request": {"number": 153, "state": "open", "draft": False},
        "reviews": [],
        "review_comments": [],
        "issue_comments": [],
        "reactions": [
            {
                "id": 124,
                "content": "heart",
                "created_at": "2026-04-02T13:05:42Z",
                "user": {"login": "chatgpt-codex-connector[bot]"},
            },
            {
                "id": 125,
                "content": "+1",
                "created_at": "2026-04-02T13:05:42Z",
                "user": {"login": "someone-else"},
            },
        ],
    }

    output, *_rest = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
    )

    assert output is None


def test_render_activity_ignores_self_authored_issue_comment_but_advances_cursor() -> None:
    module = _load_module()
    state = {
        "pull_request": {"number": 153, "state": "open", "draft": False},
        "reviews": [],
        "review_comments": [],
        "issue_comments": [
            {
                "id": 126,
                "body": "  @CoDeX ReViEw  ",
                "html_url": "https://github.com/example/repo/pull/1#issuecomment-126",
                "user": {"login": "someone"},
            }
        ],
        "reactions": [],
    }

    output, review_cursor, review_comment_cursor, issue_comment_cursor, reaction_cursor, pr_status = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        viewer_login="someone",
    )

    assert output is None
    assert review_cursor == 0
    assert review_comment_cursor == 0
    assert issue_comment_cursor == 126
    assert reaction_cursor == 0
    assert pr_status == "open"


def test_render_activity_ignores_self_authored_review_but_advances_cursor() -> None:
    module = _load_module()
    state = {
        "pull_request": {"number": 153, "state": "open", "draft": False},
        "reviews": [
            {
                "id": 125,
                "state": "COMMENTED",
                "body": "Looks good",
                "html_url": "https://github.com/example/repo/pull/1#pullrequestreview-125",
                "user": {"login": "someone"},
            }
        ],
        "review_comments": [],
        "issue_comments": [],
        "reactions": [],
    }

    output, review_cursor, review_comment_cursor, issue_comment_cursor, reaction_cursor, pr_status = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        viewer_login="someone",
    )

    assert output is None
    assert review_cursor == 125
    assert review_comment_cursor == 0
    assert issue_comment_cursor == 0
    assert reaction_cursor == 0
    assert pr_status == "open"


def test_render_activity_includes_self_authored_comment_when_disabled() -> None:
    module = _load_module()
    state = {
        "pull_request": {"number": 153, "state": "open", "draft": False},
        "reviews": [],
        "review_comments": [],
        "issue_comments": [
            {
                "id": 127,
                "body": "@codex review",
                "html_url": "https://github.com/example/repo/pull/1#issuecomment-127",
                "user": {"login": "someone"},
            }
        ],
        "reactions": [],
    }

    output, *_rest = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        viewer_login="someone",
        ignore_self_comments=False,
    )

    assert output is not None
    assert "issue_comment #127" in output


def test_render_activity_includes_self_authored_review_when_disabled() -> None:
    module = _load_module()
    state = {
        "pull_request": {"number": 153, "state": "open", "draft": False},
        "reviews": [
            {
                "id": 128,
                "state": "COMMENTED",
                "body": "Looks good",
                "html_url": "https://github.com/example/repo/pull/1#pullrequestreview-128",
                "user": {"login": "someone"},
            }
        ],
        "review_comments": [],
        "issue_comments": [],
        "reactions": [],
    }

    output, *_rest = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        viewer_login="someone",
        ignore_self_comments=False,
    )

    assert output is not None
    assert "review #128" in output


def test_render_activity_includes_pr_status_change() -> None:
    module = _load_module()
    state = {
        "pull_request": {
            "number": 153,
            "state": "closed",
            "draft": False,
            "merged_at": "2026-04-03T12:45:56Z",
            "html_url": "https://github.com/example/repo/pull/153",
        },
        "reviews": [],
        "review_comments": [],
        "issue_comments": [],
        "reactions": [],
    }

    output, review_cursor, review_comment_cursor, issue_comment_cursor, reaction_cursor, pr_status = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
    )

    assert output is not None
    assert "pr_status #153 open -> merged" in output
    assert "Pull request was merged." in output
    assert review_cursor == 0
    assert review_comment_cursor == 0
    assert issue_comment_cursor == 0
    assert reaction_cursor == 0
    assert pr_status == "merged"


def test_render_activity_reports_open_to_draft_transition() -> None:
    module = _load_module()
    state = {
        "pull_request": {
            "number": 153,
            "state": "open",
            "draft": True,
            "html_url": "https://github.com/example/repo/pull/153",
        },
        "reviews": [],
        "review_comments": [],
        "issue_comments": [],
        "reactions": [],
    }

    output, *_rest, pr_status = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
    )

    assert output is not None
    assert "pr_status #153 open -> draft" in output
    assert "Pull request was converted to draft." in output
    assert pr_status == "draft"


def test_render_activity_reports_draft_to_open_transition() -> None:
    module = _load_module()
    state = {
        "pull_request": {
            "number": 153,
            "state": "open",
            "draft": False,
            "html_url": "https://github.com/example/repo/pull/153",
        },
        "reviews": [],
        "review_comments": [],
        "issue_comments": [],
        "reactions": [],
    }

    output, *_rest, pr_status = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="draft",
        event_limit=8,
    )

    assert output is not None
    assert "pr_status #153 draft -> open" in output
    assert "Pull request is ready for review." in output
    assert pr_status == "open"


def test_render_activity_reports_closed_to_open_transition() -> None:
    module = _load_module()
    state = {
        "pull_request": {
            "number": 153,
            "state": "open",
            "draft": False,
            "html_url": "https://github.com/example/repo/pull/153",
        },
        "reviews": [],
        "review_comments": [],
        "issue_comments": [],
        "reactions": [],
    }

    output, *_rest, pr_status = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="closed",
        event_limit=8,
    )

    assert output is not None
    assert "pr_status #153 closed -> open" in output
    assert "Pull request was reopened." in output
    assert pr_status == "open"


def test_render_activity_prioritizes_closed_over_draft() -> None:
    module = _load_module()
    state = {
        "pull_request": {
            "number": 153,
            "state": "closed",
            "draft": True,
            "html_url": "https://github.com/example/repo/pull/153",
        },
        "reviews": [],
        "review_comments": [],
        "issue_comments": [],
        "reactions": [],
    }

    output, *_rest, pr_status = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
    )

    assert output is not None
    assert "pr_status #153 open -> closed" in output
    assert "Pull request was closed without merge." in output
    assert pr_status == "closed"


def test_render_activity_skips_unchanged_pr_status() -> None:
    module = _load_module()
    state = {
        "pull_request": {
            "number": 153,
            "state": "open",
            "draft": False,
            "html_url": "https://github.com/example/repo/pull/153",
        },
        "reviews": [],
        "review_comments": [],
        "issue_comments": [],
        "reactions": [],
    }

    output, review_cursor, review_comment_cursor, issue_comment_cursor, reaction_cursor, pr_status = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
    )

    assert output is None
    assert review_cursor == 0
    assert review_comment_cursor == 0
    assert issue_comment_cursor == 0
    assert reaction_cursor == 0
    assert pr_status == "open"


def test_render_new_pull_requests_includes_new_prs() -> None:
    module = _load_module()
    state = {
        "pull_requests": [
            {
                "id": 401,
                "number": 157,
                "title": "feat: add codex subagent routing",
                "state": "open",
                "html_url": "https://github.com/example/repo/pull/157",
                "user": {"login": "cyhhao"},
            }
        ]
    }

    output, pr_cursor = module._render_new_pull_requests(
        repo="avibe-bot/avibe",
        state=state,
        pr_cursor=0,
        event_limit=8,
    )

    assert output is not None
    assert "pull_request #157" in output
    assert pr_cursor == 401


def test_fetch_new_pr_state_stops_after_cursor() -> None:
    module = _load_module()
    responses = [
        [
            {"id": 410, "number": 2, "title": "Newest", "state": "open"},
            {"id": 405, "number": 1, "title": "Known", "state": "open"},
        ]
    ]

    def _fake_list_paginated_with_count(base_url, token, *, stop_after_id=None, max_pages=None, cache=None):
        assert "pulls?state=all" in base_url
        assert stop_after_id == 405
        assert max_pages is None
        return responses[0], 1

    with patch.object(module, "list_paginated_with_count", side_effect=_fake_list_paginated_with_count):
        state, request_count = module._fetch_new_pr_state(
            "avibe-bot/avibe",
            token="token",
            stop_after_id=405,
        )

    assert state["pull_requests"][0]["id"] == 410
    assert request_count == 1


def test_main_uses_since_pr_cursor_for_initial_new_pr_fetch() -> None:
    module = _load_module()
    calls: list[int | None] = []

    def _fake_fetch_new_pr_state(repo, token, *, stop_after_id=None, max_pages=None, cache=None):
        calls.append((stop_after_id, max_pages))
        return (
            {
                "pull_requests": [
                    {
                        "id": 410,
                        "number": 158,
                        "title": "New PR",
                        "state": "open",
                        "html_url": "https://github.com/example/repo/pull/158",
                        "user": {"login": "cyhhao"},
                    }
                ]
            },
            1,
        )

    stdout = io.StringIO()
    with (
        patch.object(module, "_fetch_new_pr_state", side_effect=_fake_fetch_new_pr_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--new-prs",
                "--since-pr-id",
                "405",
            ],
        ),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    assert rc == 0
    assert calls == [(405, None)]
    assert "pull_request #158" in stdout.getvalue()


def test_main_bootstraps_new_pr_watch_from_first_page_only() -> None:
    module = _load_module()
    calls: list[tuple[int | None, int | None]] = []

    def _fake_fetch_new_pr_state(repo, token, *, stop_after_id=None, max_pages=None, cache=None):
        calls.append((stop_after_id, max_pages))
        if len(calls) == 1:
            return ({"pull_requests": []}, 1)
        return (
            {
                "pull_requests": [
                    {
                        "id": 410,
                        "number": 158,
                        "title": "New PR",
                        "state": "open",
                        "html_url": "https://github.com/example/repo/pull/158",
                        "user": {"login": "cyhhao"},
                    }
                ]
            },
            1,
        )

    stdout = io.StringIO()
    with (
        patch.object(module, "_fetch_new_pr_state", side_effect=_fake_fetch_new_pr_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None),
        patch("sys.argv", ["wait_pr.py", "--repo", "avibe-bot/avibe", "--new-prs", "--interval", "1"]),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    assert rc == 0
    assert calls == [(None, 1), (None, None)]
    assert "pull_request #158" in stdout.getvalue()


def test_main_detects_pr_status_change_during_polling() -> None:
    module = _load_module()
    fetch_calls = 0

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        if fetch_calls == 1:
            return (
                {
                    "pull_request": {
                        "number": 153,
                        "state": "open",
                        "draft": False,
                        "html_url": "https://github.com/example/repo/pull/153",
                    },
                    "reviews": [],
                    "review_comments": [],
                    "issue_comments": [],
                    "reactions": [],
                },
                1,
            )
        return (
            {
                "pull_request": {
                    "number": 153,
                    "state": "closed",
                    "draft": False,
                    "merged_at": "2026-04-03T12:45:56Z",
                    "html_url": "https://github.com/example/repo/pull/153",
                },
                "reviews": [],
                "review_comments": [],
                "issue_comments": [],
                "reactions": [],
            },
            1,
        )

    stdout = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None),
        patch("sys.argv", ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--interval", "1"]),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    assert rc == 0
    assert fetch_calls == 2
    assert "pr_status #153 open -> merged" in stdout.getvalue()


def test_main_reduces_unauthenticated_new_pr_interval_after_bootstrap() -> None:
    module = _load_module()
    fetch_calls: list[int | None] = []
    sleep_calls: list[float] = []

    def _fake_fetch_new_pr_state(repo, token, *, stop_after_id=None, max_pages=None, cache=None):
        fetch_calls.append((stop_after_id, max_pages))
        if len(fetch_calls) == 1:
            return ({"pull_requests": []}, 50)
        if len(fetch_calls) == 2:
            return ({"pull_requests": []}, 1)
        return (
            {
                "pull_requests": [
                    {
                        "id": 410,
                        "number": 158,
                        "title": "New PR",
                        "state": "open",
                        "html_url": "https://github.com/example/repo/pull/158",
                        "user": {"login": "cyhhao"},
                    }
                ]
            },
            1,
        )

    def _fake_min_interval(requests_per_poll, *, bootstrap_requests=0):
        if bootstrap_requests:
            return 3600.0
        return 60.0

    stdout = io.StringIO()
    with (
        patch.object(module, "_fetch_new_pr_state", side_effect=_fake_fetch_new_pr_state),
        patch.object(module, "get_token", return_value=None),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module, "min_interval_for_unauthenticated", side_effect=_fake_min_interval),
        patch.object(module.time, "sleep", side_effect=lambda seconds: sleep_calls.append(seconds)),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--new-prs",
                "--allow-unauthenticated",
                "--interval",
                "1",
            ],
        ),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    assert rc == 0
    assert sleep_calls == [3600.0, 60.0]
    assert fetch_calls == [(None, 1), (None, None), (None, None)]
    assert "pull_request #158" in stdout.getvalue()


def test_main_retries_retryable_initial_pr_http_error_inside_one_shot() -> None:
    module = _load_module()
    state = _pr_state()
    state["issue_comments"] = [
        {
            "id": 501,
            "body": "Review result",
            "html_url": "https://github.com/example/repo/pull/153#issuecomment-501",
            "user": {"login": "reviewer"},
        }
    ]
    stderr = io.StringIO()
    err = urllib.error.HTTPError("https://api.github.com/example", 503, "Service Unavailable", hdrs=None, fp=None)

    with (
        patch.object(module, "_fetch_state", side_effect=[err, err, (state, 1)]) as fetch,
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None) as sleep,
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--catch-up"],
        ),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 0
    assert fetch.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1.0, 2.0]
    assert "Transient initial GitHub PR state request failure" in stderr.getvalue()


def test_main_returns_terminal_exit_code_for_non_retryable_initial_pr_http_error() -> None:
    module = _load_module()
    stderr = io.StringIO()
    err = urllib.error.HTTPError("https://api.github.com/example", 404, "Not Found", hdrs=None, fp=None)

    with (
        patch.object(module, "_fetch_state", side_effect=err),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch("sys.argv", ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153"]),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 1
    assert "Failed to fetch initial PR state: GitHub HTTP 404 Not Found" in stderr.getvalue()


def test_main_stops_after_bounded_initial_pr_network_retries() -> None:
    module = _load_module()
    stderr = io.StringIO()
    err = urllib.error.URLError("temporary network failure")

    with (
        patch.object(module, "_fetch_state", side_effect=err) as fetch,
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None),
        patch("sys.argv", ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153"]),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 75
    assert fetch.call_count == 3
    assert "failed after 3 attempts" in stderr.getvalue()


def test_main_recovers_from_retryable_pr_polling_failure() -> None:
    module = _load_module()
    updated = _pr_state()
    updated["issue_comments"] = [
        {
            "id": 501,
            "body": "New review result",
            "html_url": "https://github.com/example/repo/pull/153#issuecomment-501",
            "user": {"login": "reviewer"},
        }
    ]
    error = urllib.error.URLError("temporary network failure")
    stdout = io.StringIO()

    with (
        patch.object(module, "_fetch_state", side_effect=[(_pr_state(), 1), error, (updated, 1)]) as fetch,
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--interval", "1"],
        ),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    assert rc == 0
    assert fetch.call_count == 3
    assert "issue_comment #501" in stdout.getvalue()


def test_main_stops_on_a_terminal_polling_http_error() -> None:
    module = _load_module()
    error = urllib.error.HTTPError(
        url="https://api.github.com/repos/example/repo/pulls/153",
        code=404,
        msg="Not Found",
        hdrs=None,
        fp=None,
    )
    stderr = io.StringIO()

    with (
        patch.object(module, "_fetch_state", side_effect=[(_pr_state(), 1), error]),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--interval", "1"],
        ),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 1
    assert "GitHub polling failed: GitHub HTTP 404 Not Found" in stderr.getvalue()


def test_new_pr_seed_does_not_resolve_viewer_login(tmp_path: Path) -> None:
    module = _load_module()
    state_file = tmp_path / "new-prs.json"

    with (
        patch.object(module, "_fetch_new_pr_state", return_value=({"pull_requests": []}, 1)),
        patch.object(module, "get_token", return_value="app-token"),
        patch.object(
            module,
            "get_authenticated_login",
            side_effect=AssertionError("new-PR mode must not call /user"),
        ),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--new-prs",
                "--state-file",
                str(state_file),
                "--seed-state",
            ],
        ),
    ):
        rc = module.main()

    assert rc == 0
    assert state_file.is_file()


def test_main_fails_closed_when_authenticated_login_cannot_be_resolved() -> None:
    module = _load_module()
    stderr = io.StringIO()

    with (
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value=None),
        patch.object(module, "_fetch_state", side_effect=AssertionError("must not poll")),
        patch("sys.argv", ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153"]),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 1
    assert "refusing to poll" in stderr.getvalue()


def test_main_retries_transient_authenticated_login_failure() -> None:
    module = _load_module()
    state = _pr_state(
        issue_comments=[
            {
                "id": 501,
                "body": "Review result",
                "html_url": "https://github.com/example/repo/pull/153#issuecomment-501",
                "user": {"login": "reviewer"},
            }
        ]
    )

    with (
        patch.object(module, "get_token", return_value="token"),
        patch.object(
            module,
            "get_authenticated_login",
            side_effect=[TimeoutError("temporary viewer timeout"), "tester"],
        ) as lookup,
        patch.object(module, "_fetch_state", return_value=(state, 1)),
        patch.object(module.time, "sleep", return_value=None) as sleep,
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--catch-up"],
        ),
    ):
        rc = module.main()

    assert rc == 0
    assert lookup.call_count == 2
    assert [call.args[0] for call in sleep.call_args_list] == [1.0]


def test_main_stops_after_bounded_viewer_lookup_retries_with_retryable_exit_code() -> None:
    module = _load_module()
    stderr = io.StringIO()

    with (
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", side_effect=TimeoutError("temporary viewer timeout")) as lookup,
        patch.object(module, "_fetch_state", side_effect=AssertionError("must not poll before viewer login")),
        patch.object(module.time, "sleep", return_value=None),
        patch("sys.argv", ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153"]),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 75
    assert lookup.call_count == 3
    assert "GitHub viewer lookup failed" in stderr.getvalue()


def test_render_activity_actionable_only_drops_bot_trigger_comment_but_advances_cursor() -> None:
    module = _load_module()
    state = {
        "pull_request": {"number": 153, "state": "open", "draft": False},
        "reviews": [],
        "review_comments": [],
        "issue_comments": [
            {
                "id": 301,
                "body": "@codex review",
                "html_url": "https://github.com/example/repo/pull/1#issuecomment-301",
                "user": {"login": "teammate"},
            }
        ],
        "reactions": [],
    }

    output, _review_cursor, _review_comment_cursor, issue_comment_cursor, _reaction_cursor, _pr_status = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        actionable_only=True,
        ignore_patterns=module._compile_ignore_patterns(None, actionable_only=True),
    )

    assert output is None
    # Dropped once, never re-examined.
    assert issue_comment_cursor == 301


@pytest.mark.parametrize(
    "body",
    [
        "@author fix the timeout handling",
        "@alice review the migration assumptions",
        "@bob merge after the release freeze",
    ],
)
def test_render_activity_actionable_only_keeps_a_human_request_that_opens_with_a_command_word(body: str) -> None:
    """A mention plus a command word is only noise when that is the whole comment.

    Suppressing one of these still advances the cursor, so the request would not be
    delayed — it would be lost, and the review-fix loop would never answer it.
    """
    module = _load_module()
    state = {
        "pull_request": {"number": 153, "state": "open", "draft": False},
        "reviews": [],
        "review_comments": [],
        "issue_comments": [
            {
                "id": 301,
                "body": body,
                "html_url": "https://github.com/example/repo/pull/1#issuecomment-301",
                "user": {"login": "teammate"},
            }
        ],
        "reactions": [],
    }

    output, _review_cursor, _review_comment_cursor, issue_comment_cursor, _reaction_cursor, _pr_status = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        actionable_only=True,
        ignore_patterns=module._compile_ignore_patterns(None, actionable_only=True),
    )

    assert output is not None
    assert body in output
    assert issue_comment_cursor == 301


def test_render_activity_actionable_only_keeps_bot_trigger_comment_when_disabled() -> None:
    module = _load_module()
    state = {
        "pull_request": {"number": 153, "state": "open", "draft": False},
        "reviews": [],
        "review_comments": [],
        "issue_comments": [
            {
                "id": 301,
                "body": "@codex review",
                "html_url": "https://github.com/example/repo/pull/1#issuecomment-301",
                "user": {"login": "teammate"},
            }
        ],
        "reactions": [],
    }

    output, *_rest = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
    )

    assert output is not None
    assert "issue_comment #301" in output


def test_render_activity_actionable_only_drops_bodyless_commented_review() -> None:
    module = _load_module()
    state = {
        "pull_request": {"number": 153, "state": "open", "draft": False},
        "reviews": [
            {
                "id": 401,
                "state": "COMMENTED",
                "body": "",
                "html_url": "https://github.com/example/repo/pull/1#pullrequestreview-401",
                "user": {"login": "chatgpt-codex-connector[bot]"},
            }
        ],
        "review_comments": [],
        "issue_comments": [],
        "reactions": [],
    }

    output, review_cursor, *_rest = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        actionable_only=True,
        ignore_patterns=module._compile_ignore_patterns(None, actionable_only=True),
    )

    assert output is None
    assert review_cursor == 401


def test_render_activity_actionable_only_keeps_inline_comments_and_verdicts() -> None:
    module = _load_module()
    state = {
        "pull_request": {"number": 153, "state": "open", "draft": False},
        "reviews": [
            {
                "id": 402,
                "state": "CHANGES_REQUESTED",
                "body": "",
                "html_url": "https://github.com/example/repo/pull/1#pullrequestreview-402",
                "user": {"login": "chatgpt-codex-connector[bot]"},
            }
        ],
        "review_comments": [
            {
                "id": 501,
                "path": "core/watches.py",
                "body": "This drops the cursor advance.",
                "html_url": "https://github.com/example/repo/pull/1#discussion_r501",
                "user": {"login": "chatgpt-codex-connector[bot]"},
            }
        ],
        "issue_comments": [],
        "reactions": [],
    }

    output, *_rest = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        actionable_only=True,
        ignore_patterns=module._compile_ignore_patterns(None, actionable_only=True),
    )

    assert output is not None
    assert "review #402" in output
    assert "review_comment #501" in output


def test_render_activity_actionable_only_keeps_codex_pass_reaction() -> None:
    module = _load_module()
    state = {
        "pull_request": {"number": 153, "state": "open", "draft": False},
        "reviews": [],
        "review_comments": [],
        "issue_comments": [],
        "reactions": [
            {
                "id": 601,
                "content": "+1",
                "created_at": "2026-04-02T13:05:42Z",
                "user": {"login": "chatgpt-codex-connector[bot]"},
            }
        ],
    }

    output, *_rest = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        actionable_only=True,
        ignore_patterns=module._compile_ignore_patterns(None, actionable_only=True),
    )

    assert output is not None
    assert "pr_reaction #601" in output


def test_render_activity_actionable_only_drops_draft_toggle_but_keeps_merge() -> None:
    module = _load_module()
    ignore_patterns = module._compile_ignore_patterns(None, actionable_only=True)
    draft_state = {
        "pull_request": {"number": 153, "state": "open", "draft": True},
        "reviews": [],
        "review_comments": [],
        "issue_comments": [],
        "reactions": [],
    }

    output, _rc, _rcc, _icc, _reaction_cursor, pr_status = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=draft_state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        actionable_only=True,
        ignore_patterns=ignore_patterns,
    )

    assert output is None
    # The status still moves forward, so the transition is not re-detected.
    assert pr_status == "draft"

    merged_state = {
        "pull_request": {
            "number": 153,
            "state": "closed",
            "merged_at": "2026-04-02T14:00:00Z",
            "html_url": "https://github.com/example/repo/pull/153",
        },
        "reviews": [],
        "review_comments": [],
        "issue_comments": [],
        "reactions": [],
    }

    output, *_rest = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=merged_state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        actionable_only=True,
        ignore_patterns=ignore_patterns,
    )

    assert output is not None
    assert "pr_status #153 open -> merged" in output


def test_render_activity_ignores_configured_author_but_advances_cursor() -> None:
    module = _load_module()
    state = {
        "pull_request": {"number": 153, "state": "open", "draft": False},
        "reviews": [],
        "review_comments": [],
        "issue_comments": [
            {
                "id": 701,
                "body": "Looks good to me, shipping soon.",
                "html_url": "https://github.com/example/repo/pull/1#issuecomment-701",
                "user": {"login": "NoisyBot"},
            }
        ],
        "reactions": [],
    }

    output, _rc, _rcc, issue_comment_cursor, *_rest = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        ignored_authors=module._normalize_authors(["noisybot"]),
    )

    assert output is None
    assert issue_comment_cursor == 701


def test_render_activity_ignores_custom_comment_pattern() -> None:
    module = _load_module()
    state = {
        "pull_request": {"number": 153, "state": "open", "draft": False},
        "reviews": [],
        "review_comments": [],
        "issue_comments": [
            {
                "id": 801,
                "body": "Deployed to staging: build 42",
                "html_url": "https://github.com/example/repo/pull/1#issuecomment-801",
                "user": {"login": "teammate"},
            }
        ],
        "reactions": [],
    }

    output, _rc, _rcc, issue_comment_cursor, *_rest = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=state,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        ignore_patterns=module._compile_ignore_patterns(
            [r"^deployed to staging"], actionable_only=False
        ),
    )

    assert output is None
    assert issue_comment_cursor == 801


def test_main_rejects_invalid_ignore_comment_pattern() -> None:
    module = _load_module()

    with (
        patch.object(module, "get_token", return_value="token"),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--ignore-comment-pattern",
                "([unclosed",
            ],
        ),
    ):
        rc = module.main()

    assert rc == 2


def _pr_state(
    *,
    reviews=None,
    review_comments=None,
    issue_comments=None,
    reactions=None,
    review_threads=None,
    pr_state="open",
):
    return {
        "pull_request": {
            "number": 153,
            "state": pr_state,
            "draft": False,
            "html_url": "https://github.com/example/repo/pull/153",
        },
        "reviews": reviews or [],
        "review_comments": review_comments or [],
        "issue_comments": issue_comments or [],
        "reactions": reactions or [],
        "review_threads": review_threads or [],
    }


def _review_comment(comment_id: int, *, body="Fix this", created_at="2026-08-04T10:00:00Z"):
    return {
        "id": comment_id,
        "body": body,
        "path": "core/watches.py",
        "created_at": created_at,
        "updated_at": created_at,
        "html_url": f"https://github.com/example/repo/pull/153#discussion_r{comment_id}",
        "user": {"login": "chatgpt-codex-connector[bot]"},
    }


def _complete_pr_baseline_fields(module, state=None) -> dict[str, object]:
    baseline = state or _pr_state()
    raw_threads = baseline.get("review_threads")
    return {
        "head_sha": module._current_pr_head_sha(baseline.get("pull_request")) or "unknown",
        "review_fingerprints": module._fingerprint_map(baseline["reviews"]),
        "review_comment_fingerprints": module._fingerprint_map(baseline["review_comments"]),
        "issue_comment_fingerprints": module._fingerprint_map(baseline["issue_comments"]),
        "review_thread_states": module._review_thread_state_map(
            raw_threads if isinstance(raw_threads, list) else []
        ),
        "snapshot": module._normalized_pr_snapshot(
            baseline,
            ignore_self_comments=False,
        ),
    }


def test_fetch_state_narrows_comments_and_filters_reactions_server_side() -> None:
    module = _load_module()
    urls: list[str] = []

    def _fake_list_paginated_with_count(base_url, token, *, stop_after_id=None, max_pages=None, cache=None):
        urls.append(base_url)
        return [], 1

    with (
        patch.object(module, "list_paginated_with_count", side_effect=_fake_list_paginated_with_count),
        patch.object(module, "github_get", return_value={"number": 153, "state": "open"}),
    ):
        module._fetch_state(
            "avibe-bot/avibe",
            153,
            "token",
            review_comment_since="2026-08-04T06:47:10Z",
            issue_comment_since="2026-08-04T06:47:10Z",
        )

    reviews_url = next(url for url in urls if url.endswith("/reviews"))
    review_comments_url = next(url for url in urls if "/pulls/153/comments" in url)
    issue_comments_url = next(url for url in urls if "/issues/153/comments" in url)
    reactions_url = next(url for url in urls if "/reactions" in url)

    # The reviews endpoint supports neither `since` nor a newest-first order, so it
    # must stay unfiltered and lean on revalidation instead.
    assert "since=" not in reviews_url
    assert "since=" not in review_comments_url
    assert "since=" not in issue_comments_url
    # Only the Codex pass reaction is ever reported, so the rest never travel.
    assert "content=%2B1" in reactions_url


def test_fetch_state_omits_since_when_no_cursor_is_known() -> None:
    module = _load_module()
    urls: list[str] = []

    def _fake_list_paginated_with_count(base_url, token, *, stop_after_id=None, max_pages=None, cache=None):
        urls.append(base_url)
        return [], 1

    with (
        patch.object(module, "list_paginated_with_count", side_effect=_fake_list_paginated_with_count),
        patch.object(module, "github_get", return_value={"number": 153, "state": "open"}),
    ):
        module._fetch_state("avibe-bot/avibe", 153, "token")

    assert all("since=" not in url for url in urls)


def test_fetch_state_shares_one_cache_across_every_request() -> None:
    module = _load_module()
    caches: list[object] = []
    sentinel = object()

    def _fake_list_paginated_with_count(base_url, token, *, stop_after_id=None, max_pages=None, cache=None):
        caches.append(cache)
        return [], 1

    with (
        patch.object(module, "list_paginated_with_count", side_effect=_fake_list_paginated_with_count),
        patch.object(module, "github_get", return_value={"number": 153, "state": "open"}) as fake_get,
    ):
        module._fetch_state("avibe-bot/avibe", 153, "token", cache=sentinel)

    assert caches == [sentinel, sentinel, sentinel, sentinel]
    assert fake_get.call_args.kwargs["cache"] is sentinel


def test_fetch_state_paginates_review_threads_as_part_of_the_pr_snapshot() -> None:
    module = _load_module()
    graphql_pages = iter(
        [
            {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [{"id": "thread-1", "isResolved": False}],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        }
                    }
                }
            },
            {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [{"id": "thread-2", "isResolved": True}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            },
        ]
    )
    with (
        patch.object(module, "github_get", return_value={"number": 153, "state": "open"}),
        patch.object(module, "list_paginated_with_count", return_value=([], 1)),
        patch.object(module, "github_graphql", side_effect=lambda *_args, **_kwargs: next(graphql_pages)),
    ):
        state, request_count = module._fetch_state("avibe-bot/avibe", 153, "token")

    assert state["review_threads"] == [
        {"id": "thread-1", "isResolved": False},
        {"id": "thread-2", "isResolved": True},
    ]
    assert request_count == 7


def test_fresh_pr_watch_baselines_existing_review_threads() -> None:
    module = _load_module()
    state = _pr_state(review_threads=[{"id": "thread-1", "isResolved": False}])
    stdout = io.StringIO()

    with (
        patch.object(module, "_fetch_state", return_value=(state, 1)) as fetch,
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "monotonic", side_effect=[0.0, 1.0]),
        patch.object(module.time, "sleep", side_effect=AssertionError("must not wait")),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--timeout",
                "0.5",
            ],
        ),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    assert rc == 124
    assert fetch.call_count == 1
    assert stdout.getvalue() == ""


def test_render_activity_reports_review_edit_and_thread_transition() -> None:
    module = _load_module()
    old_review = {
        "id": 7,
        "state": "COMMENTED",
        "body": "old",
        "commit_id": "old-head",
        "user": {"login": "reviewer"},
    }
    baseline = _pr_state(reviews=[old_review], review_threads=[{"id": "thread-1", "isResolved": False}])
    current = _pr_state(
        reviews=[{**old_review, "body": "edited", "commit_id": "new-head"}],
        review_threads=[{"id": "thread-1", "isResolved": True}],
    )
    output, *_rest = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=current,
        review_cursor=7,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        snapshot=module._normalized_pr_snapshot(baseline, ignore_self_comments=False),
        review_fingerprints={"7": module._item_fingerprint(old_review)},
        review_thread_states={"thread-1": False},
        ignore_self_comments=False,
    )

    assert output is not None
    assert "review #7" in output
    assert "edited" in output
    assert "review_thread thread-1 unresolved -> resolved" in output


def test_snapshot_gate_advances_past_filtered_activity_without_waking() -> None:
    module = _load_module()
    baseline = _pr_state()
    current = _pr_state(
        issue_comments=[
            {
                "id": 9,
                "body": "@codex review",
                "user": {"login": "maintainer"},
            }
        ]
    )

    result = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=current,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        snapshot=module._normalized_pr_snapshot(
            baseline,
            viewer_login="maintainer",
            ignore_self_comments=True,
        ),
        viewer_login="maintainer",
        ignore_self_comments=True,
    )

    assert result[0] is None
    assert result[3] == 9


def test_review_thread_status_remains_visible_when_its_comment_author_is_filtered() -> None:
    module = _load_module()
    baseline = _pr_state()
    current = _pr_state(
        review_comments=[
            {
                **_review_comment(501),
                "user": {"login": "dependabot[bot]"},
            }
        ],
        review_threads=[{"id": "thread-1", "isResolved": False}],
    )

    output, *_rest = module._render_activity(
        repo="avibe-bot/avibe",
        pr_number=153,
        state=current,
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        event_limit=8,
        snapshot=module._normalized_pr_snapshot(
            baseline,
            ignore_self_comments=False,
            ignored_authors={"dependabot[bot]"},
        ),
        review_thread_states={},
        ignore_self_comments=False,
        ignored_authors={"dependabot[bot]"},
    )

    assert output is not None
    assert "review_thread thread-1 absent -> unresolved" in output
    assert "review_comment #501" not in output


def test_pending_report_payload_is_replayable_without_remote_state(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": 153,
                "watch": "filters",
                "owner": "watch-1",
                "pending": {
                    "delivered_after": "delivery-1",
                    "output": "persisted report",
                    "cursors": {"review_cursor": 2},
                },
            }
        )
    )
    saved = module._load_state_file(
        str(state_file),
        repo="avibe-bot/avibe",
        pr_number=153,
        watch_identity="filters",
        watch_id="watch-1",
    )

    resolved = module._resolve_staged_state(
        str(state_file),
        saved,
        delivery="delivery-1",
        repo="avibe-bot/avibe",
        pr_number=153,
        watch_identity="filters",
        watch_id="watch-1",
    )

    assert resolved == saved
    assert module._staged_replay_output(saved, "delivery-1") == "persisted report"


def test_main_replays_pending_output_before_auth_or_remote_preflight(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    _managed_state(
        module,
        state_file,
        "wat_9",
        **{
            module.STAGED_KEY: {
                "delivered_after": "delivery-1",
                "output": "persisted report",
                "cursors": {"review_cursor": 2},
            }
        },
    )

    stdout = io.StringIO()
    with (
        patch.dict(
            "os.environ",
            {module.WATCH_ID_ENV: "wat_9", module.LAST_DELIVERY_ENV: "delivery-1"},
            clear=False,
        ),
        patch.object(module, "get_token", side_effect=AssertionError("must not authenticate")),
        patch.object(module, "_fetch_state", side_effect=AssertionError("must not poll")),
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--state-file", str(state_file)],
        ),
        redirect_stdout(stdout),
        patch("sys.stderr", io.StringIO()),
    ):
        rc = module.main()

    assert rc == 0
    assert stdout.getvalue() == "persisted report\n"


def test_main_settle_window_reports_a_batched_review_as_one_event() -> None:
    module = _load_module()
    fetches = 0

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        nonlocal fetches
        fetches += 1
        if fetches == 1:
            return _pr_state(), 1
        if fetches == 2:
            # First fragment of the batch is visible.
            return _pr_state(review_comments=[_review_comment(501)]), 1
        # The rest of the batch lands while the waiter is settling.
        return (
            _pr_state(review_comments=[_review_comment(501), _review_comment(502), _review_comment(503)]),
            1,
        )

    stdout = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--interval",
                "1",
                "--settle",
                "5",
            ],
        ),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    output = stdout.getvalue()
    assert rc == 0
    # One report, one Agent turn, all three comments in it.
    assert output.count("GitHub PR activity detected") == 1
    assert "review_comment #501" in output
    assert "review_comment #502" in output
    assert "review_comment #503" in output


def test_main_settle_window_stops_once_the_batch_is_stable() -> None:
    module = _load_module()
    fetches = 0

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        nonlocal fetches
        fetches += 1
        if fetches == 1:
            return _pr_state(), 1
        return _pr_state(review_comments=[_review_comment(501)]), 1

    stdout = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--interval", "1", "--settle", "5"],
        ),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    assert rc == 0
    # Bootstrap, the poll that found it, and a single confirming re-poll: a quiet
    # batch must not burn all the settle rounds.
    assert fetches == 3
    assert "review_comment #501" in stdout.getvalue()


def test_main_settle_window_survives_a_failed_re_poll() -> None:
    module = _load_module()
    fetches = 0

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        nonlocal fetches
        fetches += 1
        if fetches == 1:
            return _pr_state(), 1
        if fetches == 2:
            return _pr_state(review_comments=[_review_comment(501)]), 1
        raise urllib.error.URLError("network went away mid-settle")

    stdout = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--interval", "1", "--settle", "5"],
        ),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    # A broken re-poll must not lose the event that was already detected.
    assert rc == 0
    assert "review_comment #501" in stdout.getvalue()


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("settle timed out"),
        ConnectionError("connection reset"),
        OSError("socket unavailable"),
    ],
)
def test_main_settle_preserves_detected_event_for_every_transient_error(error) -> None:
    module = _load_module()
    fetches = 0

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        nonlocal fetches
        fetches += 1
        if fetches == 1:
            return _pr_state(), 1
        if fetches == 2:
            return _pr_state(review_comments=[_review_comment(501)]), 1
        raise error

    stdout = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--interval",
                "1",
                "--settle",
                "5",
            ],
        ),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    assert rc == 0
    assert "review_comment #501" in stdout.getvalue()


def test_main_writes_the_state_file_even_with_nothing_to_report(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "cursors" / "pr-153.json"

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        return _pr_state(review_comments=[_review_comment(501)]), 1

    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="qiqi"),
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--timeout",
                "0.0001",
                "--interval",
                "1",
                "--state-file",
                str(state_file),
            ],
        ),
        patch("sys.stderr", io.StringIO()),
    ):
        rc = module.main()

    assert rc == 124
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    # The baseline this cycle established is what the next cycle must resume from.
    assert saved["review_comment_cursor"] == 501
    assert saved["repo"] == "avibe-bot/avibe"
    assert saved["pr"] == 153
    assert saved["viewer_login"] == "qiqi"
    assert saved["token_fingerprint"] == module._token_fingerprint("token")
    assert saved["review_comment_since"] == "2026-08-04T09:59:58Z"


def test_seed_state_persists_a_complete_pr_baseline_and_exits(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    state = _pr_state(
        reviews=[
            {
                "id": 7,
                "body": "reviewed",
                "state": "COMMENTED",
                "submitted_at": "2026-08-04T10:00:00Z",
                "commit_id": "head-1",
                "user": {"login": "reviewer"},
            }
        ],
        review_comments=[_review_comment(501)],
        review_threads=[{"id": "thread-1", "isResolved": False}],
    )
    state["pull_request"]["head"] = {"sha": "head-1"}

    stderr = io.StringIO()
    with (
        patch.object(module, "_fetch_state", return_value=(state, 7)),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", side_effect=AssertionError("must not wait")),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--state-file",
                str(state_file),
                "--seed-state",
            ],
        ),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 0
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["review_cursor"] == 7
    assert saved["review_comment_cursor"] == 501
    assert saved["head_sha"] == "head-1"
    assert saved["review_fingerprints"]["7"]
    assert saved["review_comment_fingerprints"]["501"]
    assert saved["review_thread_states"] == {"thread-1": False}
    assert saved["snapshot"]
    assert "Seeded GitHub PR baseline" in stderr.getvalue()


def test_manual_resume_rejects_complete_cursors_without_snapshot_baselines(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "legacy-pr-153.json"
    raw = json.dumps(
        {
            "version": module.STATE_FILE_VERSION,
            "repo": "avibe-bot/avibe",
            "pr": 153,
            "review_cursor": 0,
            "review_comment_cursor": 400,
            "issue_comment_cursor": 0,
            "reaction_cursor": 0,
            "pr_status": "open",
            "viewer_login": "qiqi",
            "token_fingerprint": module._token_fingerprint("token"),
        }
    )
    state_file.write_text(raw, encoding="utf-8")
    stderr = io.StringIO()

    with (
        patch.object(module, "_fetch_state", return_value=(_pr_state(), 1)) as fetch,
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login") as lookup,
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--state-file",
                str(state_file),
            ],
        ),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 2
    assert fetch.call_count == 1
    lookup.assert_not_called()
    assert "lacks required baseline" in stderr.getvalue()
    assert state_file.read_text(encoding="utf-8") == raw


def test_invalid_remote_target_does_not_claim_the_state_file(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-999.json"

    with (
        patch.object(module, "_fetch_state", side_effect=RuntimeError("pull request not found")),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "999", "--state-file", str(state_file)],
        ),
        patch("sys.stderr", io.StringIO()),
    ):
        rc = module.main()

    assert rc == 1
    assert not state_file.exists()


def test_main_resumes_from_the_state_file_instead_of_re_baselining(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    state_file.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": 153,
                "review_cursor": 0,
                "review_comment_cursor": 400,
                "issue_comment_cursor": 0,
                "reaction_cursor": 0,
                "pr_status": "open",
                "review_comment_since": "2026-08-04T09:00:00Z",
                "issue_comment_since": "2026-08-04T09:00:00Z",
                "viewer_login": "qiqi",
                "token_fingerprint": module._token_fingerprint("token"),
                **_complete_pr_baseline_fields(module),
            }
        ),
        encoding="utf-8",
    )
    since_values: list[str | None] = []

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        since_values.append(kwargs.get("review_comment_since"))
        return _pr_state(review_comments=[_review_comment(501)]), 1

    stdout = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login") as fake_login,
        patch("sys.argv", ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--state-file", str(state_file)]),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    assert rc == 0
    # A comment that arrived between cycles is reported instead of being swallowed
    # by a fresh baseline.
    assert "review_comment #501" in stdout.getvalue()
    # The stored `since` narrows the very first fetch of the new cycle.
    assert since_values == ["2026-08-04T09:00:00Z"]
    # The stored login spares a /user request per cycle.
    fake_login.assert_not_called()


def test_main_re_resolves_the_login_when_the_token_changed(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    state_file.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": 153,
                "review_cursor": 0,
                "review_comment_cursor": 400,
                "issue_comment_cursor": 0,
                "reaction_cursor": 0,
                "pr_status": "open",
                "viewer_login": "someone-else",
                "token_fingerprint": module._token_fingerprint("old-token"),
                **_complete_pr_baseline_fields(module),
            }
        ),
        encoding="utf-8",
    )

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        return _pr_state(review_comments=[_review_comment(501)]), 1

    stdout = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="new-token"),
        patch.object(
            module, "get_authenticated_login", return_value="chatgpt-codex-connector[bot]"
        ) as fake_login,
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--timeout",
                "0.0001",
                "--interval",
                "1",
                "--state-file",
                str(state_file),
            ],
        ),
        redirect_stdout(stdout),
        patch("sys.stderr", io.StringIO()),
    ):
        rc = module.main()

    # The cached login belonged to the previous credential. Reusing it would let the
    # new account's own comments wake the Agent, so the login is resolved again and
    # it is the fresh login that filters this cycle.
    fake_login.assert_called_once_with("new-token", raise_on_error=True)
    assert rc == 124
    assert stdout.getvalue() == ""
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["viewer_login"] == "chatgpt-codex-connector[bot]"
    assert saved["token_fingerprint"] == module._token_fingerprint("new-token")


def test_state_file_never_stores_the_token_itself(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-153.json"

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        return _pr_state(), 1

    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="ghp_super_secret"),
        patch.object(module, "get_authenticated_login", return_value="qiqi"),
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--timeout",
                "0.0001",
                "--interval",
                "1",
                "--state-file",
                str(state_file),
            ],
        ),
        patch("sys.stderr", io.StringIO()),
    ):
        module.main()

    raw = state_file.read_text(encoding="utf-8")
    # The fingerprint identifies the credential; it must never carry it.
    assert "ghp_super_secret" not in raw
    assert json.loads(raw)["token_fingerprint"] == module._token_fingerprint("ghp_super_secret")


def test_main_replays_from_an_explicit_cursor_without_a_saved_since(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    state_file.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": 153,
                "review_cursor": 0,
                "review_comment_cursor": 500,
                "issue_comment_cursor": 0,
                "reaction_cursor": 0,
                "pr_status": "open",
                "review_comment_since": "2026-08-04T09:00:00Z",
                "issue_comment_since": "2026-08-04T09:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    since_values: list[tuple[str | None, str | None]] = []

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        since_values.append((kwargs.get("review_comment_since"), kwargs.get("issue_comment_since")))
        return _pr_state(review_comments=[_review_comment(450)]), 1

    stdout = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--state-file",
                str(state_file),
                "--since-review-comment-id",
                "400",
            ],
        ),
        redirect_stdout(stdout),
        patch("sys.stderr", io.StringIO()),
    ):
        rc = module.main()

    # An explicit cursor asks for a replay. The saved `since` would have filtered out
    # comment #450 server-side and the replay would have returned nothing. The stream
    # with no explicit cursor keeps its cheap incremental `since`.
    assert since_values == [(None, "2026-08-04T09:00:00Z")]
    assert rc == 0
    assert "review_comment #450" in stdout.getvalue()


def test_main_refuses_a_state_file_belonging_to_another_pr(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-999.json"
    foreign = json.dumps(
        {
            "version": module.STATE_FILE_VERSION,
            "repo": "avibe-bot/avibe",
            "pr": 999,
            "review_cursor": 0,
            "review_comment_cursor": 400,
            "issue_comment_cursor": 0,
            "reaction_cursor": 0,
        }
    )
    state_file.write_text(foreign, encoding="utf-8")

    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=AssertionError("must not poll")),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--timeout",
                "0.0001",
                "--interval",
                "1",
                "--state-file",
                str(state_file),
            ],
        ),
        redirect_stdout(stdout),
        patch("sys.stderr", stderr),
    ):
        rc = module.run_cli()

    # Two watches sharing one path is a setup mistake, not something to paper over:
    # adopting the foreign cursors would skip this PR's history, and overwriting them
    # would blind the other watch. Stop instead, and leave the other file intact.
    assert rc == 1
    assert stdout.getvalue() == ""
    assert "belongs to avibe-bot/avibe#999" in stderr.getvalue()
    assert state_file.read_text(encoding="utf-8") == foreign


def test_main_ignores_a_partial_state_file(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    state_file.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": 153,
                "review_comment_cursor": 400,
                "review_comment_since": "2026-08-04T09:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    since_values: list[str | None] = []

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        since_values.append(kwargs.get("review_comment_since"))
        return _pr_state(review_comments=[_review_comment(501)]), 1

    stdout = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--timeout",
                "0.0001",
                "--interval",
                "1",
                "--state-file",
                str(state_file),
            ],
        ),
        redirect_stdout(stdout),
        patch("sys.stderr", io.StringIO()),
    ):
        rc = module.main()

    # An incomplete cursor set cannot be combined with a narrowed fetch: the
    # missing baselines would have to come from a partial history.
    assert rc == 124
    assert since_values == [None]


def test_main_refuses_to_poll_past_an_unreadable_state_file(tmp_path) -> None:
    """A corrupt state file is terminal, because its cursors are unknown.

    Warning and re-baselining looks like recovery and is a silent loss: the file DID
    hold a cursor, and everything that arrived between it and the fresh snapshot is
    skipped -- then the only evidence of how far the watch had got is overwritten. A
    forever watch would do that on every cycle. Stopping instead puts the choice with
    whoever can make it.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    state_file.write_text("{ this is not json", encoding="utf-8")

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        return _pr_state(), 1

    stderr = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--timeout",
                "0.0001",
                "--interval",
                "1",
                "--state-file",
                str(state_file),
            ],
        ),
        patch("sys.stderr", stderr),
    ):
        rc = module.run_cli()

    assert rc == 1
    assert "is corrupt" in stderr.getvalue()
    # Left exactly as found: the operator decides, and the bytes are the evidence.
    assert state_file.read_text(encoding="utf-8") == "{ this is not json"


def test_main_refuses_a_state_file_it_does_not_recognise(tmp_path) -> None:
    """Valid JSON of the wrong shape or version is unusable for the same reason.

    Its cursors may mean something else, or nothing this waiter can read, so resuming
    from them and overwriting them are both guesses about how far the watch had got.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    original = json.dumps({"version": module.STATE_FILE_VERSION + 1, "review_comment_cursor": 500})
    state_file.write_text(original, encoding="utf-8")

    stderr = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=AssertionError("must not poll")),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--state-file", str(state_file)],
        ),
        patch("sys.stderr", stderr),
    ):
        rc = module.run_cli()

    assert rc == 1
    assert "not in a recognised format" in stderr.getvalue()
    assert state_file.read_text(encoding="utf-8") == original


def test_main_starts_over_from_an_empty_state_file(tmp_path) -> None:
    """A zero-byte file is an interrupted claim, not corruption.

    The claim creates the path exclusively and then writes it, so a cycle killed in
    between leaves nothing behind. No cursor was ever recorded there, so there is
    none to lose and refusing would strand the watch on a file only it created.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    state_file.write_text("", encoding="utf-8")

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        return _pr_state(review_comments=[_review_comment(501)]), 1

    stderr = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--timeout",
                "0.0001",
                "--interval",
                "1",
                "--state-file",
                str(state_file),
            ],
        ),
        patch("sys.stderr", stderr),
    ):
        rc = module.run_cli()

    assert rc == 124
    assert json.loads(state_file.read_text(encoding="utf-8"))["review_comment_cursor"] == 501


def test_main_refuses_to_poll_when_the_state_file_cannot_be_written(tmp_path) -> None:
    """An unwritable ``--state-file`` is terminal, and terminal BEFORE the first poll.

    Warning and continuing left a forever watch polling without the cursors it was
    asked to keep: every fresh cycle re-baselines from the current PR, so activity
    that arrived between cycles is silently dropped — the exact loss the flag
    exists to prevent. Discovering that only when the cycle tries to save has
    already cost the activity that cycle observed.
    """
    module = _load_module()
    read_only = tmp_path / "read-only"
    read_only.mkdir()
    read_only.chmod(0o500)
    state_file = read_only / "pr-153.json"

    stderr = io.StringIO()
    try:
        with (
            patch.object(module, "_fetch_state", side_effect=AssertionError("must not poll")),
            patch.object(module, "get_token", return_value="token"),
            patch.object(module, "get_authenticated_login", return_value="tester"),
            patch.object(module.time, "sleep", return_value=None),
            patch(
                "sys.argv",
                ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--state-file", str(state_file)],
            ),
            patch("sys.stderr", stderr),
        ):
            rc = module.run_cli()
    finally:
        read_only.chmod(0o700)

    # 1, not the retryable 75: a read-only directory does not start working next cycle.
    assert rc == 1
    assert "Cannot write state file" in stderr.getvalue()
    assert not state_file.exists()


def test_main_refuses_to_poll_when_the_state_file_cannot_be_replaced(tmp_path) -> None:
    """The preflight has to probe the replace, not just the parent directory.

    A target that is a directory — a stale path, a mistyped mount — accepts new
    siblings all day and fails only at ``os.replace``. Probing creation alone let a
    fresh forever cycle establish a baseline, poll, and then lose it, which is
    exactly what the fail-before-poll promise rules out.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    state_file.mkdir()

    stderr = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=AssertionError("must not poll")),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--state-file", str(state_file)],
        ),
        patch("sys.stderr", stderr),
    ):
        rc = module.run_cli()

    assert rc == 1
    assert "Cannot write state file" in stderr.getvalue()
    # No scratch file left behind in the directory the probe failed on.
    assert list(state_file.parent.glob(".pr-153.json.*")) == []


def test_state_file_preflight_leaves_saved_cursors_untouched(tmp_path) -> None:
    """The probe rewrites an existing state file with its own bytes, not with junk.

    Probing the replace means writing to the real path, so a watch resuming from
    good cursors has to come out the other side with exactly those cursors.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    saved = json.dumps(
        {
            "version": module.STATE_FILE_VERSION,
            "repo": "avibe-bot/avibe",
            "pr": 153,
            "review_cursor": 0,
            "review_comment_cursor": 400,
            "issue_comment_cursor": 0,
            "reaction_cursor": 0,
        }
    )
    state_file.write_text(saved, encoding="utf-8")

    module._verify_state_file_writable(str(state_file), repo="avibe-bot/avibe", pr_number=153)

    assert state_file.read_text(encoding="utf-8") == saved
    assert list(tmp_path.glob(".pr-153.json.*")) == []


def test_state_file_preflight_claims_a_missing_path_before_polling(tmp_path) -> None:
    """A missing state file is owned from the start, and carries no cursors.

    Claiming before the first poll is what stops two watches that start together
    from both seeing an unowned path; writing only ownership is what keeps this
    cycle's baseline identical to the no-state-file case.
    """
    module = _load_module()
    state_file = tmp_path / "cursors" / "pr-153.json"

    module._verify_state_file_writable(
        str(state_file), repo="avibe-bot/avibe", pr_number=153, watch_identity="abc123"
    )

    claim = {
        "version": module.STATE_FILE_VERSION,
        "repo": "avibe-bot/avibe",
        "pr": 153,
        "watch": "abc123",
        "owner": None,
    }
    assert json.loads(state_file.read_text(encoding="utf-8")) == claim
    # No cursors, so a resume is not attempted and the cycle baselines as before.
    assert (
        module._load_state_file(
            str(state_file), repo="avibe-bot/avibe", pr_number=153, watch_identity="abc123"
        )
        == claim
    )
    assert list(state_file.parent.glob(".pr-153.json.*")) == []


def test_state_file_claim_is_visible_to_a_second_watch(tmp_path) -> None:
    """The loser of the creation race must be turned away, not left sharing the path."""
    module = _load_module()
    state_file = tmp_path / "pr-153.json"

    assert module._claim_state_file(state_file, repo="avibe-bot/avibe", pr_number=153) is True
    # A concurrent waiter for another PR reaches its own claim a moment later.
    assert module._claim_state_file(state_file, repo="avibe-bot/avibe", pr_number=158) is False
    with pytest.raises(module.StateFileOwnershipError):
        module._load_state_file(str(state_file), repo="avibe-bot/avibe", pr_number=158)


def test_watch_identity_separates_report_filters_but_not_pacing() -> None:
    """Two watches on one PR are the same owner only if they report the same things.

    Pacing options are deliberately outside the identity: a watch differing only in
    interval or settle window sees the same activity, so sharing cursors with it
    loses nothing, while a differing filter set does lose events.
    """
    module = _load_module()

    def _identity(*extra: str) -> str:
        with patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", *extra],
        ):
            return module._watch_identity(module._build_parser().parse_args())

    baseline = _identity("--interval", "60")
    assert _identity("--interval", "5", "--settle", "20", "--timeout", "0") == baseline
    assert _identity("--actionable-only") != baseline
    assert _identity("--include-self-comments") != baseline
    assert _identity("--ignore-author", "dependabot[bot]") != baseline
    assert _identity("--ignore-comment-pattern", "^nit") != baseline
    # Order and duplication are not identity: the same filter set has to hash alike
    # across cycles however the command happened to be written.
    assert _identity(
        "--ignore-author",
        "Renovate[bot]",
        "--ignore-author",
        "dependabot[bot]",
        "--ignore-author",
        "dependabot[bot]",
    ) == _identity("--ignore-author", "dependabot[bot]", "--ignore-author", "renovate[bot]")


def test_load_state_file_rejects_another_watch_on_the_same_pr(tmp_path) -> None:
    """Same PR, different filters: the filtered watch would advance past the other's events."""
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    state_file.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": 153,
                "watch": "actionable",
                "review_comment_cursor": 400,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(module.StateFileOwnershipError) as err:
        module._load_state_file(
            str(state_file), repo="avibe-bot/avibe", pr_number=153, watch_identity="everything"
        )

    assert "different reporting filters" in str(err.value)


def test_load_state_file_adopts_a_file_written_before_identities_existed(tmp_path) -> None:
    """An absent identity cannot prove a conflict, so it must not invent one.

    Rejecting it would strand a watch on cursors it wrote itself under an older
    build of this waiter.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    saved = {
        "version": module.STATE_FILE_VERSION,
        "repo": "avibe-bot/avibe",
        "pr": 153,
        "review_comment_cursor": 400,
    }
    state_file.write_text(json.dumps(saved), encoding="utf-8")

    assert (
        module._load_state_file(
            str(state_file), repo="avibe-bot/avibe", pr_number=153, watch_identity="everything"
        )
        == saved
    )


def test_write_state_file_refuses_a_path_another_watch_now_owns(tmp_path) -> None:
    """Ownership is verified on every replacement, not only at startup.

    A waiter that lost the creation race by microseconds has already passed the
    preflight, so the replace itself is the last place to notice -- and overwriting
    the winner's cursors would make it re-baseline and skip real activity.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    other = json.dumps(
        {
            "version": module.STATE_FILE_VERSION,
            "repo": "avibe-bot/avibe",
            "pr": 158,
            "review_comment_cursor": 400,
        }
    )
    state_file.write_text(other, encoding="utf-8")

    with pytest.raises(module.StateFileOwnershipError):
        module._write_state_file(
            str(state_file),
            repo="avibe-bot/avibe",
            pr_number=153,
            review_comment_cursor=999,
        )

    assert state_file.read_text(encoding="utf-8") == other


def test_write_state_file_refuses_a_sibling_watch_on_the_same_pr(tmp_path) -> None:
    """The write guard covers the same-PR case too, not just a foreign PR."""
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    other = json.dumps(
        {
            "version": module.STATE_FILE_VERSION,
            "repo": "avibe-bot/avibe",
            "pr": 153,
            "watch": "actionable",
            "review_comment_cursor": 400,
        }
    )
    state_file.write_text(other, encoding="utf-8")

    with pytest.raises(module.StateFileOwnershipError):
        module._write_state_file(
            str(state_file),
            repo="avibe-bot/avibe",
            pr_number=153,
            watch_identity="everything",
            review_comment_cursor=999,
        )

    assert state_file.read_text(encoding="utf-8") == other


def _new_pr(pr_id: int, number: int) -> dict[str, object]:
    return {
        "id": pr_id,
        "number": number,
        "title": f"PR {number}",
        "state": "open",
        "html_url": f"https://github.com/example/repo/pull/{number}",
        "user": {"login": "cyhhao"},
    }


def _run_new_pr_catch_up(module, state_file, *extra: str) -> str:
    def _fake_fetch_new_pr_state(repo, token, *, stop_after_id=None, max_pages=None, cache=None):
        return {"pull_requests": [_new_pr(400, 157), _new_pr(410, 158)]}, 1

    stdout = io.StringIO()
    with (
        patch.object(module, "_fetch_new_pr_state", side_effect=_fake_fetch_new_pr_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--new-prs",
                "--catch-up",
                "--timeout",
                "0.0001",
                "--interval",
                "1",
                "--state-file",
                str(state_file),
                *extra,
            ],
        ),
        redirect_stdout(stdout),
        patch("sys.stderr", io.StringIO()),
    ):
        rc = module.main()

    assert rc == 0
    return stdout.getvalue()


def test_main_new_prs_catch_up_ignores_the_saved_cursor(tmp_path) -> None:
    """--catch-up means "report what is already there", saved cursor or not.

    The PR-activity path already lets --catch-up override saved cursors; inheriting
    the saved new-PR cursor filtered the freshly fetched history back down to what
    the previous cycle had seen, so the flag reported nothing.
    """
    module = _load_module()
    state_file = tmp_path / "new-prs.json"
    state_file.write_text(
        json.dumps({"version": module.STATE_FILE_VERSION, "repo": "avibe-bot/avibe", "pr": None, "pr_cursor": 410}),
        encoding="utf-8",
    )

    output = _run_new_pr_catch_up(module, state_file)

    assert "pull_request #157" in output
    assert "pull_request #158" in output


def test_main_new_prs_catch_up_still_honours_an_explicit_cursor(tmp_path) -> None:
    """Only an explicitly supplied cursor narrows a catch-up."""
    module = _load_module()
    state_file = tmp_path / "new-prs.json"
    state_file.write_text(
        json.dumps({"version": module.STATE_FILE_VERSION, "repo": "avibe-bot/avibe", "pr": None, "pr_cursor": 300}),
        encoding="utf-8",
    )

    output = _run_new_pr_catch_up(module, state_file, "--since-pr-id", "400")

    assert "pull_request #157" not in output
    assert "pull_request #158" in output


def test_main_stops_when_persisting_advanced_cursors_fails(tmp_path) -> None:
    """The same rule once polling is under way: losing cursors stops the watch."""
    module = _load_module()
    state_file = tmp_path / "pr-153.json"

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        return _pr_state(review_comments=[_review_comment(501)]), 1

    def _replace_then_break(src, dst):
        # The preflight claims the missing file with an exclusive create, so every
        # replace here belongs to a cursor write: the disk goes away while the watch
        # is already polling, which is the case this test is about.
        raise OSError("disk went away")

    stderr = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.os, "replace", side_effect=_replace_then_break),
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--timeout",
                "0.0001",
                "--interval",
                "1",
                "--state-file",
                str(state_file),
            ],
        ),
        patch("sys.stderr", stderr),
    ):
        rc = module.run_cli()

    assert rc == 1
    assert "Could not write state file" in stderr.getvalue()


def test_main_state_file_round_trips_new_pr_cursor(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "new-prs.json"

    def _fake_fetch_new_pr_state(repo, token, *, stop_after_id=None, max_pages=None, cache=None):
        return (
            {
                "pull_requests": [
                    {
                        "id": 410,
                        "number": 158,
                        "title": "New PR",
                        "state": "open",
                        "html_url": "https://github.com/example/repo/pull/158",
                        "user": {"login": "cyhhao"},
                    }
                ]
            },
            1,
        )

    stdout = io.StringIO()
    with (
        patch.object(module, "_fetch_new_pr_state", side_effect=_fake_fetch_new_pr_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep", return_value=None),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--new-prs",
                "--timeout",
                "0.0001",
                "--interval",
                "1",
                "--state-file",
                str(state_file),
            ],
        ),
        redirect_stdout(stdout),
        patch("sys.stderr", io.StringIO()),
    ):
        rc = module.main()

    assert rc == 124
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["pr_cursor"] == 410
    assert saved["pr"] is None


def test_main_rejects_a_bad_ignore_pattern_without_claiming_the_state_file(tmp_path) -> None:
    """Argument validation happens before any state is claimed.

    Claiming first left the file owned by a watch identity derived from the very
    pattern that was rejected, so the corrected re-run was refused as a different
    watch's state until the file was deleted by hand.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    stderr = io.StringIO()

    with (
        patch.object(module, "get_token", return_value="token"),
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--ignore-comment-pattern",
                "[unclosed",
                "--state-file",
                str(state_file),
            ],
        ),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 2
    assert "Invalid --ignore-comment-pattern" in stderr.getvalue()
    assert not state_file.exists()


def test_main_rejects_missing_auth_without_claiming_the_state_file(tmp_path) -> None:
    """The same rule for the auth precondition."""
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    stderr = io.StringIO()

    with (
        patch.object(module, "get_token", return_value=None),
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--state-file", str(state_file)],
        ),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 2
    assert "GitHub authentication is required" in stderr.getvalue()
    assert not state_file.exists()


def test_state_file_records_the_managed_watch_that_owns_it() -> None:
    """Two identically configured watches still cannot share one state file.

    The filter digest is equal for both, so only the managed watch id tells them
    apart. It is read from the environment `vibe watch` sets for the cycle.
    """
    module = _load_module()

    with patch.dict("os.environ", {module.WATCH_ID_ENV: "wat_123"}, clear=False):
        assert module._managed_watch_id() == "wat_123"
    with patch.dict("os.environ", {module.WATCH_ID_ENV: "  "}, clear=False):
        assert module._managed_watch_id() is None


def test_load_state_file_rejects_a_sibling_watch_with_the_same_filters(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    state_file.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": 153,
                "watch": "abc123",
                "owner": "wat_first",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(module.StateFileOwnershipError) as excinfo:
        module._load_state_file(
            str(state_file),
            repo="avibe-bot/avibe",
            pr_number=153,
            watch_identity="abc123",
            watch_id="wat_second",
        )

    assert "belongs to watch wat_first" in str(excinfo.value)
    # A manual run has no watch id either, and cannot prove it is ``wat_first`` -- so
    # it is refused too. Adopting it would strip the owner on the next write and leave
    # the watch resuming from cursors covering activity it never delivered.
    with pytest.raises(module.StateFileOwnershipError) as manual:
        module._load_state_file(
            str(state_file), repo="avibe-bot/avibe", pr_number=153, watch_identity="abc123"
        )
    assert "belongs to watch wat_first" in str(manual.value)


def test_main_skips_the_settle_window_that_would_outlast_the_timeout(tmp_path) -> None:
    """A settle window is never worth losing the report to a timeout kill.

    `vibe watch` terminates the waiter at its deadline, so re-polling past that
    point throws away the batch that was already worth an Agent turn.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    state_file.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": 153,
                "review_cursor": 0,
                "review_comment_cursor": 500,
                "issue_comment_cursor": 0,
                "reaction_cursor": 0,
                "pr_status": "open",
                "viewer_login": "qiqi",
                "token_fingerprint": module._token_fingerprint("token"),
                **_complete_pr_baseline_fields(module),
            }
        ),
        encoding="utf-8",
    )
    fetches: list[object] = []

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        fetches.append(kwargs)
        return _pr_state(review_comments=[_review_comment(501)]), 1

    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep") as fake_sleep,
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--settle",
                "30",
                "--timeout",
                "5",
                "--state-file",
                str(state_file),
            ],
        ),
        redirect_stdout(stdout),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 0
    assert "review_comment #501" in stdout.getvalue()
    assert "Settle window plus its re-poll would outlast --timeout" in stderr.getvalue()
    # No settle sleep, and no settle re-poll: one fetch is the whole run.
    fake_sleep.assert_not_called()
    assert len(fetches) == 1


def test_main_skips_the_settle_window_when_only_its_sleep_would_fit(tmp_path) -> None:
    """The sleep is only half the settle cost; the re-poll behind it needs room too.

    A supervisor and waiter that share a deadline would otherwise be killed mid-fetch,
    with the already-detected batch still unreported.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    state_file.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": 153,
                "review_cursor": 0,
                "review_comment_cursor": 500,
                "issue_comment_cursor": 0,
                "reaction_cursor": 0,
                "pr_status": "open",
                "viewer_login": "qiqi",
                "token_fingerprint": module._token_fingerprint("token"),
                **_complete_pr_baseline_fields(module),
            }
        ),
        encoding="utf-8",
    )
    fetches: list[object] = []

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        fetches.append(kwargs)
        return _pr_state(review_comments=[_review_comment(501)]), 1

    stdout = io.StringIO()
    stderr = io.StringIO()
    # A 5s settle fits in the 20s left, but the request budget behind it does not.
    assert module.REQUEST_TIMEOUT_SECONDS > 5
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "sleep") as fake_sleep,
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--settle",
                "5",
                "--timeout",
                "20",
                "--state-file",
                str(state_file),
            ],
        ),
        redirect_stdout(stdout),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 0
    assert "review_comment #501" in stdout.getvalue()
    fake_sleep.assert_not_called()
    assert len(fetches) == 1


def _stdout_at_persist(module, stdout: io.StringIO) -> list[str]:
    """Record what had already been written to stdout each time state was saved."""

    snapshots: list[str] = []
    real_write = module._write_state_file

    def _spy(*args, **kwargs):
        snapshots.append(stdout.getvalue())
        return real_write(*args, **kwargs)

    module._write_state_file = _spy
    return snapshots


def test_main_commits_cursors_only_after_the_event_is_reported(tmp_path) -> None:
    """A manual run's reported event moves its cursors after the report, not before.

    `vibe watch` builds the follow-up from a completed process, so a kill between
    the two orderings has opposite costs: commit-then-report loses the event for
    good, because the saved cursors have moved past it and no follow-up carried it,
    while report-then-commit costs at most one repeated report next cycle.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    state_file.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": 153,
                "review_cursor": 0,
                "review_comment_cursor": 500,
                "issue_comment_cursor": 0,
                "reaction_cursor": 0,
                "pr_status": "open",
                "viewer_login": "qiqi",
                "token_fingerprint": module._token_fingerprint("token"),
                **_complete_pr_baseline_fields(module),
            }
        ),
        encoding="utf-8",
    )

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        return _pr_state(review_comments=[_review_comment(501)]), 1

    stdout = io.StringIO()
    snapshots = _stdout_at_persist(module, stdout)
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--state-file", str(state_file)],
        ),
        redirect_stdout(stdout),
        patch("sys.stderr", io.StringIO()),
    ):
        rc = module.main()

    assert rc == 0
    assert "review_comment #501" in stdout.getvalue()
    # One save, and the report was already out when it happened.
    assert len(snapshots) == 1
    assert "review_comment #501" in snapshots[0]
    assert json.loads(state_file.read_text(encoding="utf-8"))["review_comment_cursor"] == 501


def test_main_new_prs_commits_the_cursor_only_after_the_event_is_reported(tmp_path) -> None:
    """The new-PR path has the same ordering."""
    module = _load_module()
    state_file = tmp_path / "new-prs.json"
    state_file.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": None,
                "pr_cursor": 400,
            }
        ),
        encoding="utf-8",
    )

    def _fake_fetch_new_pr_state(repo, token, *, stop_after_id=None, max_pages=None, cache=None):
        return {"pull_requests": [_new_pr(400, 157), _new_pr(410, 158)]}, 1

    stdout = io.StringIO()
    snapshots = _stdout_at_persist(module, stdout)
    with (
        patch.object(module, "_fetch_new_pr_state", side_effect=_fake_fetch_new_pr_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--new-prs", "--state-file", str(state_file)],
        ),
        redirect_stdout(stdout),
        patch("sys.stderr", io.StringIO()),
    ):
        rc = module.main()

    assert rc == 0
    assert "pull_request #158" in stdout.getvalue()
    assert len(snapshots) == 1
    assert "pull_request #158" in snapshots[0]
    assert json.loads(state_file.read_text(encoding="utf-8"))["pr_cursor"] == 410


def _managed_state(module, path, watch_id: str | None, **fields) -> None:
    """A state file already owned by ``watch_id``, so a managed run resumes from it."""

    baseline = _pr_state()
    path.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": 153,
                "watch": None,
                "owner": watch_id,
                "review_cursor": 0,
                "review_comment_cursor": 500,
                "issue_comment_cursor": 0,
                "reaction_cursor": 0,
                "pr_status": "open",
                "head_sha": "unknown",
                "review_fingerprints": {},
                "review_comment_fingerprints": {},
                "issue_comment_fingerprints": {},
                "review_thread_states": {},
                "snapshot": module._normalized_pr_snapshot(
                    baseline,
                    ignore_self_comments=False,
                ),
                **fields,
            }
        ),
        encoding="utf-8",
    )


def _run_managed(module, state_file, fetch, *, delivery: str = ""):
    """One managed cycle over ``state_file``, told when this watch last delivered."""

    env = {module.WATCH_ID_ENV: "wat_9", module.LAST_DELIVERY_ENV: delivery}
    stdout = io.StringIO()
    with (
        patch.dict("os.environ", env, clear=False),
        patch.object(module, "_fetch_state", side_effect=fetch),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--state-file", str(state_file)],
        ),
        redirect_stdout(stdout),
        patch("sys.stderr", io.StringIO()),
    ):
        rc = module.main()
    return rc, stdout.getvalue(), json.loads(state_file.read_text(encoding="utf-8"))


def test_a_managed_run_stages_the_cursors_that_cover_its_report(tmp_path) -> None:
    """Under `vibe watch` the reported event's cursors are staged, not committed.

    Flushing stdout is not delivery: the supervisor reads the report only after the
    process exits, so this process cannot know its report survived. Committing at
    report time therefore drops the event whenever the service dies in between --
    the saved cursors have moved past it and no follow-up ever carried it. So the
    committed cursors stay put and the advanced ones wait under ``STAGED_KEY``,
    written down BEFORE the report leaves, for the next cycle to resolve.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    _managed_state(module, state_file, "wat_9")

    def _fetch(repo, pr_number, token, **kwargs):
        return _pr_state(review_comments=[_review_comment(501)]), 1

    rc, stdout, payload = _run_managed(module, state_file, _fetch, delivery="2026-08-04T10:00:00+00:00")

    assert rc == 0
    assert "review_comment #501" in stdout
    # Still pointing before the event that was just reported.
    assert payload["review_comment_cursor"] == 500
    assert payload[module.STAGED_KEY]["cursors"]["review_comment_cursor"] == 501
    assert payload[module.STAGED_KEY]["output"] == stdout.strip()
    # Stamped with the delivery this cycle started from, which is what a later cycle
    # compares against to learn whether this report was queued.
    assert payload[module.STAGED_KEY]["delivered_after"] == "2026-08-04T10:00:00+00:00"


def test_a_managed_run_promotes_the_staged_cursors_once_the_report_was_delivered(tmp_path) -> None:
    """A delivery stamp that has moved since staging means the report was queued.

    The comparison is what makes this survive a restart, and makes a ``once`` watch
    resumed long after its single report promote rather than replay it: the stamp is
    still on the watch, so "was it delivered" is answerable at any later time.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    _managed_state(
        module,
        state_file,
        "wat_9",
        **{
            module.STAGED_KEY: {
                "delivered_after": "2026-08-04T10:00:00+00:00",
                "cursors": {"review_comment_cursor": 501, "review_cursor": 0},
            }
        },
    )

    saved = module._load_state_file(
        str(state_file), repo="avibe-bot/avibe", pr_number=153, watch_identity=None, watch_id="wat_9"
    )
    with patch("sys.stderr", io.StringIO()):
        resolved = module._resolve_staged_state(
            str(state_file),
            saved,
            delivery="2026-08-04T10:05:00+00:00",
            repo="avibe-bot/avibe",
            pr_number=153,
            watch_identity=None,
            watch_id="wat_9",
        )

    assert resolved["review_comment_cursor"] == 501
    assert module.STAGED_KEY not in resolved
    # Written down before any polling, so the promotion is decided exactly once.
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["review_comment_cursor"] == 501
    assert module.STAGED_KEY not in payload


def test_a_managed_run_reports_the_event_again_when_it_was_never_delivered(tmp_path) -> None:
    """An unchanged delivery stamp means the report was never queued: replay it.

    One repeated Agent turn is the cost of the staged cursors being dropped; the
    alternative -- promoting them anyway -- is an event nobody ever hears about.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    _managed_state(
        module,
        state_file,
        "wat_9",
        **{
            module.STAGED_KEY: {
                "delivered_after": "2026-08-04T10:00:00+00:00",
                "cursors": {"review_comment_cursor": 501, "review_cursor": 0},
            }
        },
    )

    def _fetch(repo, pr_number, token, **kwargs):
        return _pr_state(review_comments=[_review_comment(501)]), 1

    rc, stdout, payload = _run_managed(module, state_file, _fetch, delivery="2026-08-04T10:00:00+00:00")

    assert rc == 0
    assert "review_comment #501" in stdout
    assert payload["review_comment_cursor"] == 500
    assert payload[module.STAGED_KEY]["cursors"]["review_comment_cursor"] == 501


def test_a_manual_run_stages_nothing_because_printing_is_its_delivery(tmp_path) -> None:
    """No watch id, no next cycle: staged cursors would be promoted by nobody.

    The file is deliberately ownerless: a manual run is refused a state file that
    names a watch, so an owned one would fail the preflight before this test could
    observe what it stages.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    _managed_state(module, state_file, None)

    def _fetch(repo, pr_number, token, **kwargs):
        return _pr_state(review_comments=[_review_comment(501)]), 1

    stdout = io.StringIO()
    with (
        patch.dict("os.environ", {module.WATCH_ID_ENV: "", module.LAST_DELIVERY_ENV: ""}, clear=False),
        patch.object(module, "_fetch_state", side_effect=_fetch),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--state-file", str(state_file)],
        ),
        redirect_stdout(stdout),
        patch("sys.stderr", io.StringIO()),
    ):
        rc = module.main()

    assert rc == 0
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["review_comment_cursor"] == 501
    assert module.STAGED_KEY not in payload


def _ownerless_state(module, path, **fields) -> None:
    path.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": 153,
                "review_comment_cursor": 500,
                **fields,
            }
        ),
        encoding="utf-8",
    )


def test_preflight_adopts_an_ownerless_state_file_for_a_managed_watch(tmp_path) -> None:
    """An absent owner fits every managed watch, so it must not stay absent.

    A file left by a manual run would otherwise be adopted by two managed watches at
    once; each would poll and then overwrite the other's cursors, skipping the
    activity in between for good.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    _ownerless_state(module, state_file)

    module._verify_state_file_writable(
        str(state_file),
        repo="avibe-bot/avibe",
        pr_number=153,
        watch_identity="abc123",
        watch_id="wat_first",
    )

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["owner"] == "wat_first"
    assert saved["watch"] == "abc123"
    # Adoption takes the name, not the cursors: a resumed watch keeps its baseline.
    assert saved["review_comment_cursor"] == 500

    # The second managed watch now finds a claim instead of an open path: the preflight
    # leaves it alone and the load refuses it.
    module._verify_state_file_writable(
        str(state_file),
        repo="avibe-bot/avibe",
        pr_number=153,
        watch_identity="abc123",
        watch_id="wat_second",
    )
    assert json.loads(state_file.read_text(encoding="utf-8"))["owner"] == "wat_first"
    with pytest.raises(module.StateFileOwnershipError):
        module._load_state_file(
            str(state_file),
            repo="avibe-bot/avibe",
            pr_number=153,
            watch_identity="abc123",
            watch_id="wat_second",
        )


def test_preflight_leaves_an_ownerless_state_file_alone_for_a_manual_run(tmp_path) -> None:
    """Only a managed watch has a name to claim the path with.

    A manual run must not stamp itself on a watch's file, or the watch's next cycle
    would be refused its own state.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    _ownerless_state(module, state_file)

    module._verify_state_file_writable(
        str(state_file), repo="avibe-bot/avibe", pr_number=153, watch_identity="abc123"
    )

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved.get("owner") is None
    assert saved["review_comment_cursor"] == 500


def test_preflight_does_not_adopt_another_watchs_state_file(tmp_path) -> None:
    """A path that already carries somebody else's claim is left untouched."""
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    _ownerless_state(module, state_file, watch="abc123", owner="wat_first")

    module._verify_state_file_writable(
        str(state_file),
        repo="avibe-bot/avibe",
        pr_number=153,
        watch_identity="abc123",
        watch_id="wat_second",
    )

    assert json.loads(state_file.read_text(encoding="utf-8"))["owner"] == "wat_first"
    with pytest.raises(module.StateFileOwnershipError):
        module._load_state_file(
            str(state_file),
            repo="avibe-bot/avibe",
            pr_number=153,
            watch_identity="abc123",
            watch_id="wat_second",
        )


def test_load_state_file_refuses_a_managed_watchs_file_for_a_manual_run(tmp_path) -> None:
    """A manual run cannot prove it is the watch that owns the path, so it is refused.

    Adopting it instead is silent data loss, not sharing: the manual run reports to a
    terminal, then ``_write_state_file`` stamps ``owner: null`` over the claim while
    advancing the cursors. The watch adopts its own file back as ownerless and resumes
    past activity its Agent follow-up was never sent.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    _ownerless_state(module, state_file, watch="abc123", owner="wat_first")

    with pytest.raises(module.StateFileOwnershipError):
        module._load_state_file(
            str(state_file),
            repo="avibe-bot/avibe",
            pr_number=153,
            watch_identity="abc123",
            watch_id=None,
        )

    # The preflight leaves the claim exactly as it found it.
    module._verify_state_file_writable(
        str(state_file), repo="avibe-bot/avibe", pr_number=153, watch_identity="abc123"
    )
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["owner"] == "wat_first"
    assert saved["review_comment_cursor"] == 500


def test_preflight_claims_an_empty_state_file_for_a_managed_watch(tmp_path) -> None:
    """An interrupted claim must be finished, not rewritten empty.

    A zero-byte file carries no cursors, so the load treats it as a fresh baseline --
    but it also names no owner. Left that way, two managed watches pointed at the path
    both clear the preflight and poll, and one overwrites the other's cursors.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    state_file.write_text("", encoding="utf-8")

    module._verify_state_file_writable(
        str(state_file),
        repo="avibe-bot/avibe",
        pr_number=153,
        watch_identity="abc123",
        watch_id="wat_first",
    )

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["owner"] == "wat_first"
    assert saved["watch"] == "abc123"
    assert saved["repo"] == "avibe-bot/avibe"
    assert saved["pr"] == 153
    # No cursors were invented: this cycle still baselines from the current PR.
    assert "review_comment_cursor" not in saved

    # The second managed watch now loses on the claim instead of sharing the path.
    module._verify_state_file_writable(
        str(state_file),
        repo="avibe-bot/avibe",
        pr_number=153,
        watch_identity="abc123",
        watch_id="wat_second",
    )
    assert json.loads(state_file.read_text(encoding="utf-8"))["owner"] == "wat_first"
    with pytest.raises(module.StateFileOwnershipError):
        module._load_state_file(
            str(state_file),
            repo="avibe-bot/avibe",
            pr_number=153,
            watch_identity="abc123",
            watch_id="wat_second",
        )


def test_preflight_does_not_rewrite_another_watchs_state_file(tmp_path) -> None:
    """A foreign file is left unprobed, not merely unadopted.

    The probe is an ``os.replace`` of bytes read a moment earlier, and the owning
    watch's own cursor writes do NOT take the preflight lock -- so probing its path can
    land stale bytes over a cursor it has just advanced, or over a ``pending`` handoff
    it is midway through, making it replay or lose activity.

    The assertion is on ``os.replace`` itself rather than on the resulting bytes: the
    probe rewrites the file with the bytes it already held, so content alone cannot
    tell a skipped write from a completed round trip.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    _ownerless_state(module, state_file, watch="abc123", owner="wat_first")
    before = state_file.read_bytes()

    replaced = []
    real_replace = module.os.replace

    def _spy(src, dst, *args, **kwargs):
        replaced.append((src, dst))
        return real_replace(src, dst, *args, **kwargs)

    # Both shapes of foreign file: another watch on this PR, and another PR entirely.
    with patch.object(module.os, "replace", _spy):
        module._verify_state_file_writable(
            str(state_file),
            repo="avibe-bot/avibe",
            pr_number=153,
            watch_identity="abc123",
            watch_id="wat_second",
        )
        module._verify_state_file_writable(
            str(state_file), repo="avibe-bot/avibe", pr_number=999, watch_identity="abc123"
        )

    assert replaced == [], f"the preflight wrote to a foreign state file: {replaced}"
    assert state_file.read_bytes() == before

    # Refusal still happens -- it just happens at the load, before any poll.
    with pytest.raises(module.StateFileOwnershipError):
        module._load_state_file(
            str(state_file),
            repo="avibe-bot/avibe",
            pr_number=153,
            watch_identity="abc123",
            watch_id="wat_second",
        )


def test_state_file_lock_serializes_the_ownership_decision(tmp_path) -> None:
    """The lock is held on a sidecar, not on the file that gets replaced.

    A lock on the state file's own inode would be released into thin air by the
    first atomic replace, leaving the second half of the decision unguarded.
    """
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    _ownerless_state(module, state_file)
    lock_file = tmp_path / "pr-153.json.lock"

    with module._state_file_lock(state_file):
        assert lock_file.exists()
        held = lock_file.stat().st_ino

    module._verify_state_file_writable(
        str(state_file),
        repo="avibe-bot/avibe",
        pr_number=153,
        watch_identity="abc123",
        watch_id="wat_first",
    )

    # The state file was replaced; the lock's identity survived it.
    assert lock_file.stat().st_ino == held
