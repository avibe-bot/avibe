from __future__ import annotations

import io
import importlib.util
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

    def _fake_list_paginated_with_count(base_url, token, *, stop_after_id=None, max_pages=None):
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

    def _fake_fetch_new_pr_state(repo, token, *, stop_after_id=None, max_pages=None):
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

    def _fake_fetch_new_pr_state(repo, token, *, stop_after_id=None, max_pages=None):
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

    def _fake_fetch_state(repo, pr_number, token):
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

    def _fake_fetch_new_pr_state(repo, token, *, stop_after_id=None, max_pages=None):
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
