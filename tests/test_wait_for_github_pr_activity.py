from __future__ import annotations

import io
import importlib.util
import json
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


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
        patch.object(module, "get_authenticated_login", return_value=None),
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
        patch.object(module, "get_authenticated_login", return_value=None),
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
        patch.object(module, "get_authenticated_login", return_value=None),
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
        patch.object(module, "get_authenticated_login", return_value=None),
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


def test_main_returns_retry_exit_code_for_retryable_initial_pr_http_error() -> None:
    module = _load_module()
    stderr = io.StringIO()
    err = urllib.error.HTTPError("https://api.github.com/example", 503, "Service Unavailable", hdrs=None, fp=None)

    with (
        patch.object(module, "_fetch_state", side_effect=err),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value=None),
        patch("sys.argv", ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153"]),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 75
    assert "GitHub API error: 503 Service Unavailable" in stderr.getvalue()


def test_main_returns_terminal_exit_code_for_non_retryable_initial_pr_http_error() -> None:
    module = _load_module()
    stderr = io.StringIO()
    err = urllib.error.HTTPError("https://api.github.com/example", 404, "Not Found", hdrs=None, fp=None)

    with (
        patch.object(module, "_fetch_state", side_effect=err),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value=None),
        patch("sys.argv", ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153"]),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 1
    assert "GitHub API error: 404 Not Found" in stderr.getvalue()


def test_main_returns_retry_exit_code_for_initial_pr_network_error() -> None:
    module = _load_module()
    stderr = io.StringIO()
    err = urllib.error.URLError("temporary network failure")

    with (
        patch.object(module, "_fetch_state", side_effect=err),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value=None),
        patch("sys.argv", ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153"]),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 75
    assert "GitHub network error: temporary network failure" in stderr.getvalue()


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
    assert "since=2026-08-04T06%3A47%3A10Z" in review_comments_url
    assert "since=2026-08-04T06%3A47%3A10Z" in issue_comments_url
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
        patch.object(module, "get_authenticated_login", return_value=None),
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
        patch.object(module, "get_authenticated_login", return_value=None),
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
        patch.object(module, "get_authenticated_login", return_value=None),
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
    assert saved["review_comment_since"] == "2026-08-04T09:59:58Z"


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


def test_main_ignores_a_state_file_belonging_to_another_pr(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-999.json"
    state_file.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": 999,
                "review_cursor": 0,
                "review_comment_cursor": 400,
                "issue_comment_cursor": 0,
                "reaction_cursor": 0,
            }
        ),
        encoding="utf-8",
    )

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        return _pr_state(review_comments=[_review_comment(501)]), 1

    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value=None),
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
        rc = module.main()

    # Foreign cursors would silently skip this PR's real history, so it baselines.
    assert rc == 124
    assert stdout.getvalue() == ""
    assert "belongs to avibe-bot/avibe#999" in stderr.getvalue()


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
        patch.object(module, "get_authenticated_login", return_value=None),
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


def test_main_ignores_an_unreadable_state_file(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-153.json"
    state_file.write_text("{ this is not json", encoding="utf-8")

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        return _pr_state(), 1

    stderr = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value=None),
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
        rc = module.main()

    assert rc == 124
    assert "Ignoring unusable state file" in stderr.getvalue()
    # A corrupt file is replaced by a usable one rather than breaking every cycle.
    assert json.loads(state_file.read_text(encoding="utf-8"))["repo"] == "avibe-bot/avibe"


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
            patch.object(module, "get_authenticated_login", return_value=None),
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


def test_main_stops_when_persisting_advanced_cursors_fails(tmp_path) -> None:
    """The same rule once polling is under way: losing cursors stops the watch."""
    module = _load_module()
    state_file = tmp_path / "pr-153.json"

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        return _pr_state(review_comments=[_review_comment(501)]), 1

    stderr = io.StringIO()
    with (
        patch.object(module, "_fetch_state", side_effect=_fake_fetch_state),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value=None),
        patch.object(module.os, "replace", side_effect=OSError("disk went away")),
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
        patch.object(module, "get_authenticated_login", return_value=None),
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
