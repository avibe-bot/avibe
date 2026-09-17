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


def test_combined_ci_mode_supports_pinned_or_dynamic_current_head() -> None:
    module = _load_module()

    pinned = module._build_parser().parse_args(
        ["--repo", "avibe-bot/avibe", "--pr", "153", "--sha", "abc123", "--workflow", "CI"]
    )
    assert module._validate_ci_args(pinned) is None

    dynamic = module._build_parser().parse_args(
        ["--repo", "avibe-bot/avibe", "--pr", "153", "--workflow", "CI"]
    )
    assert module._validate_ci_args(dynamic) is None

    missing_workflow = module._build_parser().parse_args(
        ["--repo", "avibe-bot/avibe", "--pr", "153", "--branch", "feature"]
    )
    assert "--workflow" in (module._validate_ci_args(missing_workflow) or "")

    new_pr_mode = module._build_parser().parse_args(
        ["--repo", "avibe-bot/avibe", "--new-prs", "--sha", "abc123", "--workflow", "CI"]
    )
    assert "--new-prs" in (module._validate_ci_args(new_pr_mode) or "")


def test_pr_only_watch_identity_keeps_the_legacy_material() -> None:
    module = _load_module()
    args = module._build_parser().parse_args(
        ["--repo", "avibe-bot/avibe", "--pr", "153", "--actionable-only"]
    )
    expected_material = json.dumps(
        {
            "mode": "pr",
            "actionable_only": True,
            "include_self_comments": False,
            "ignore_authors": [],
            "ignore_comment_patterns": [],
        },
        sort_keys=True,
    )
    expected = module.hashlib.sha256(
        f"wait_pr/{module.STATE_FILE_VERSION}/{expected_material}".encode()
    ).hexdigest()[:16]

    assert module._watch_identity(args) == expected


def test_combined_watch_identity_includes_ci_contract() -> None:
    module = _load_module()
    pr_only = module._build_parser().parse_args(
        ["--repo", "avibe-bot/avibe", "--pr", "153", "--actionable-only"]
    )
    combined = module._build_parser().parse_args(
        [
            "--repo",
            "avibe-bot/avibe",
            "--pr",
            "153",
            "--actionable-only",
            "--sha",
            "abc123",
            "--workflow",
            "CI",
        ]
    )

    assert module._watch_identity(pr_only) != module._watch_identity(combined)


def test_combined_watch_identity_includes_actions_page_limit() -> None:
    module = _load_module()

    def _identity(max_pages: str) -> str:
        return module._watch_identity(
            module._build_parser().parse_args(
                [
                    "--repo",
                    "avibe-bot/avibe",
                    "--pr",
                    "153",
                    "--sha",
                    "abc123",
                    "--workflow",
                    "CI",
                    "--max-pages",
                    max_pages,
                ]
            )
        )

    assert _identity("1") != _identity("3")


def test_combined_watch_migrates_identity_without_actions_page_limit(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-153-combined.json"
    args = module._build_parser().parse_args(
        ["--repo", "avibe-bot/avibe", "--pr", "153", "--sha", "abc123", "--workflow", "CI"]
    )
    legacy_identity = module._watch_identity(args, include_ci_max_pages=False)
    state_file.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": 153,
                "watch": legacy_identity,
                "owner": "wat_123",
                "head_sha": "abc123",
                "actions": {"CI": []},
            }
        ),
        encoding="utf-8",
    )

    aliases = module._legacy_watch_identity_aliases(args, str(state_file))
    assert legacy_identity in aliases


def test_combined_watch_migrates_the_legacy_sha_identity(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-153-combined.json"
    args = module._build_parser().parse_args(
        [
            "--repo",
            "avibe-bot/avibe",
            "--pr",
            "153",
            "--sha",
            "new-sha",
            "--branch",
            "feature",
            "--workflow",
            "CI",
        ]
    )
    legacy_identity = module._watch_identity(args, legacy_ci_sha="old-sha", include_ci_max_pages=False)
    state_file.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": 153,
                "watch": legacy_identity,
                "owner": "wat_123",
                "head_sha": "old-sha",
                "actions": {"CI": []},
            }
        ),
        encoding="utf-8",
    )

    identity = module._watch_identity(args)
    aliases = module._legacy_watch_identity_aliases(args, str(state_file))
    saved = module._load_state_file(
        str(state_file),
        repo="avibe-bot/avibe",
        pr_number=153,
        watch_identity=identity,
        watch_id="wat_123",
        watch_identity_aliases=aliases,
    )

    assert saved["watch"] == legacy_identity
    assert legacy_identity in aliases
    module._write_state_file(
        str(state_file),
        repo="avibe-bot/avibe",
        pr_number=153,
        watch_identity=identity,
        watch_id="wat_123",
        watch_identity_aliases=aliases,
        head_sha="new-sha",
        actions={"CI": []},
    )
    assert json.loads(state_file.read_text(encoding="utf-8"))["watch"] == identity


def test_fresh_combined_baseline_rejects_a_stale_sha(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-153-combined.json"
    state = _pr_state()
    state["pull_request"]["head"] = {"sha": "new-sha"}

    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch.object(module, "_fetch_state", return_value=(state, 1)),
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
                "--sha",
                "old-sha",
                "--workflow",
                "CI",
                "--state-file",
                str(state_file),
            ],
        ),
        redirect_stdout(stdout),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 2
    assert "does not match --sha old-sha" in stderr.getvalue()
    assert not state_file.exists()


def test_resumed_combined_watch_rejects_a_stale_sha(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-153-combined.json"
    baseline = _pr_state()
    baseline["pull_request"]["head"] = {"sha": "new-sha"}
    state_file.write_text(
        json.dumps(
            {
                "version": module.STATE_FILE_VERSION,
                "repo": "avibe-bot/avibe",
                "pr": 153,
                "review_cursor": 0,
                "review_comment_cursor": 0,
                "issue_comment_cursor": 0,
                "reaction_cursor": 0,
                "pr_status": "open",
                "review_comment_since": "2026-08-04T09:00:00Z",
                "issue_comment_since": "2026-08-04T09:00:00Z",
                "viewer_login": "tester",
                "token_fingerprint": module._token_fingerprint("token"),
                **_complete_pr_baseline_fields(module, baseline),
                "actions": {"CI": []},
            }
        ),
        encoding="utf-8",
    )
    original_state = state_file.read_text(encoding="utf-8")

    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch.object(module, "_fetch_state", return_value=(baseline, 1)),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login") as fake_login,
        patch(
            "sys.argv",
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--sha",
                "old-sha",
                "--workflow",
                "CI",
                "--state-file",
                str(state_file),
            ],
        ),
        redirect_stdout(stdout),
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == 2
    assert "does not match --sha old-sha" in stderr.getvalue()
    assert stdout.getvalue() == ""
    assert state_file.read_text(encoding="utf-8") == original_state
    fake_login.assert_not_called()


def test_combined_pr_waiter_reports_ci_completion(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-153-combined.json"
    fetch_calls = 0

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        assert kwargs["ci_sha"] == "abc123"
        assert kwargs["ci_workflows"] == ["CI"]
        action = {
            "id": 7,
            "name": "CI",
            "head_sha": "abc123",
            "head_branch": "feature",
            "status": "in_progress" if fetch_calls == 1 else "completed",
            "conclusion": None if fetch_calls == 1 else "success",
            "html_url": "https://github.com/example/actions/runs/7",
        }
        state = _pr_state()
        state["pull_request"]["head"] = {"sha": "abc123"}
        state["actions"] = [action]
        return state, 2

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
                "--sha",
                "abc123",
                "--branch",
                "feature",
                "--workflow",
                "CI",
                "--state-file",
                str(state_file),
                "--interval",
                "1",
            ],
        ),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    assert rc == 0
    assert fetch_calls == 2
    assert "GitHub Actions success for avibe-bot/avibe@abc123 on feature" in stdout.getvalue()
    assert "CI: status=completed conclusion=success" in stdout.getvalue()


def test_dynamic_combined_waiter_follows_the_current_pr_head(tmp_path) -> None:
    module = _load_module()
    state_file = tmp_path / "pr-153-combined.json"
    fetch_calls = 0

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        assert kwargs["ci_sha"] is None
        assert kwargs["ci_workflows"] == ["CI"]
        head_sha = "old-head" if fetch_calls == 1 else "new-head"
        state = _pr_state()
        state["pull_request"]["head"] = {"sha": head_sha}
        state["actions"] = [
            {
                "id": fetch_calls,
                "name": "CI",
                "head_sha": head_sha,
                "head_branch": "feature",
                "status": "in_progress" if fetch_calls == 1 else "completed",
                "conclusion": None if fetch_calls == 1 else "success",
                "html_url": f"https://github.com/example/actions/runs/{fetch_calls}",
            }
        ]
        return state, 2

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
                "--workflow",
                "CI",
                "--state-file",
                str(state_file),
                "--interval",
                "1",
            ],
        ),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    output = stdout.getvalue()
    assert rc == 0
    assert fetch_calls == 2
    assert "pr_head #153 old-head -> new-head" in output
    assert "GitHub Actions success for avibe-bot/avibe@new-head" in output


def test_combined_settle_does_not_report_a_stale_ci_success_after_a_rerun_starts() -> None:
    module = _load_module()
    fetch_calls = 0

    def _fake_fetch_state(repo, pr_number, token, **kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        state = _pr_state()
        state["pull_request"]["head"] = {"sha": "abc123"}
        if fetch_calls == 1:
            action = {
                "id": 7,
                "name": "CI",
                "head_sha": "abc123",
                "head_branch": "feature",
                "status": "completed",
                "conclusion": "success",
                "run_attempt": 1,
            }
        elif fetch_calls == 2:
            action = {
                "id": 7,
                "name": "CI",
                "head_sha": "abc123",
                "head_branch": "feature",
                "status": "completed",
                "conclusion": "success",
                "run_attempt": 2,
            }
            state["review_comments"] = [_review_comment(501)]
        else:
            action = {
                "id": 7,
                "name": "CI",
                "head_sha": "abc123",
                "head_branch": "feature",
                "status": "in_progress",
                "conclusion": None,
                "run_attempt": 2,
            }
            state["review_comments"] = [_review_comment(501)]
        state["actions"] = [action]
        return state, 2

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
                "--sha",
                "abc123",
                "--branch",
                "feature",
                "--workflow",
                "CI",
                "--settle",
                "1",
                "--timeout",
                "0",
            ],
        ),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    output = stdout.getvalue()
    assert rc == 0
    assert fetch_calls == 5
    assert "review_comment #501" in output
    assert "GitHub Actions success" not in output


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


def test_fetch_state_includes_combined_actions_and_request_count() -> None:
    module = _load_module()
    with (
        patch.object(module, "list_paginated_with_count", return_value=([], 1)),
        patch.object(
            module,
            "github_get",
            side_effect=[
                {"number": 153, "state": "open", "head": {"sha": "abc123"}},
                {"number": 153, "state": "open", "head": {"sha": "abc123"}},
            ],
        ) as fetch_pr,
        patch.object(module, "_fetch_review_threads", return_value=([], 1)),
        patch.object(
            module,
            "fetch_workflow_runs",
            return_value=(
                [{"id": 7, "name": "CI", "head_sha": "abc123", "status": "in_progress"}],
                2,
            ),
        ) as fetch_actions,
    ):
        state, request_count = module._fetch_state(
            "avibe-bot/avibe",
            153,
            "token",
            ci_sha="abc123",
            ci_branch="feature",
            ci_workflows=["CI"],
            ci_max_pages=4,
        )

    fetch_actions.assert_called_once_with(
        "avibe-bot/avibe",
        "token",
        branch="feature",
        head_sha="abc123",
        max_pages=4,
        cache=fetch_actions.call_args.kwargs["cache"],
    )
    assert state["actions"][0]["id"] == 7
    assert request_count == 2 + 5 + 2
    assert fetch_pr.call_count == 2


def test_fetch_state_uses_the_observed_pr_head_for_dynamic_ci() -> None:
    module = _load_module()
    with (
        patch.object(module, "list_paginated_with_count", return_value=([], 1)),
        patch.object(
            module,
            "github_get",
            side_effect=[
                {"number": 153, "state": "open", "head": {"sha": "current-head"}},
                {"number": 153, "state": "open", "head": {"sha": "current-head"}},
            ],
        ),
        patch.object(module, "_fetch_review_threads", return_value=([], 1)),
        patch.object(module, "fetch_workflow_runs", return_value=([], 1)) as fetch_actions,
    ):
        module._fetch_state(
            "avibe-bot/avibe",
            153,
            "token",
            ci_branch="feature",
            ci_workflows=["CI"],
        )

    fetch_actions.assert_called_once_with(
        "avibe-bot/avibe",
        "token",
        branch="feature",
        head_sha="current-head",
        max_pages=3,
        cache=fetch_actions.call_args.kwargs["cache"],
    )


def test_fetch_state_rejects_dynamic_ci_when_pr_head_is_missing() -> None:
    module = _load_module()
    with (
        patch.object(module, "list_paginated_with_count", return_value=([], 1)),
        patch.object(module, "github_get", return_value={"number": 153, "state": "open"}),
        patch.object(module, "_fetch_review_threads", return_value=([], 1)),
        patch.object(module, "fetch_workflow_runs") as fetch_actions,
        pytest.raises(
            RuntimeError,
            match="GitHub PR response has no head SHA for current-head CI monitoring",
        ),
    ):
        module._fetch_state(
            "avibe-bot/avibe",
            153,
            "token",
            ci_branch="feature",
            ci_workflows=["CI"],
        )

    fetch_actions.assert_not_called()


def test_fetch_state_discards_actions_when_pr_moves_during_fetch() -> None:
    module = _load_module()
    with (
        patch.object(module, "list_paginated_with_count", return_value=([], 1)),
        patch.object(module, "_fetch_review_threads", return_value=([], 1)),
        patch.object(
            module,
            "github_get",
            side_effect=[
                {"number": 153, "state": "open", "head": {"sha": "abc123"}},
                {"number": 153, "state": "open", "head": {"sha": "new-sha"}},
            ],
        ),
        patch.object(
            module,
            "fetch_workflow_runs",
            return_value=([{"id": 7, "name": "CI", "head_sha": "abc123", "status": "completed"}], 1),
        ),
    ):
        state, _request_count = module._fetch_state(
            "avibe-bot/avibe",
            153,
            "token",
            ci_sha="abc123",
            ci_workflows=["CI"],
        )

    assert state["pull_request"]["head"]["sha"] == "new-sha"
    assert state["actions"] == []


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


def _run_managed(module, state_file, fetch, *, delivery: str = "", extra_args=()):
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
            [
                "wait_pr.py",
                "--repo",
                "avibe-bot/avibe",
                "--pr",
                "153",
                "--state-file",
                str(state_file),
                *extra_args,
            ],
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


def test_dynamic_combined_cycles_report_activity_that_lands_during_the_follow_up(tmp_path) -> None:
    """A forever re-arm keeps one baseline across CI and the later review.

    The first cycle exits after CI succeeds. The review lands while its Agent
    follow-up is running, before the next cycle starts. Reusing the same durable
    state must compare that review with the pre-review snapshot instead of
    baselining it away.
    """

    module = _load_module()
    state_file = tmp_path / "pr-153-combined.json"
    baseline = _pr_state()
    baseline["pull_request"]["head"] = {"sha": "head-1"}
    in_progress = {
        "id": 7,
        "name": "CI",
        "head_sha": "head-1",
        "head_branch": "feature",
        "status": "in_progress",
        "conclusion": None,
    }
    _managed_state(
        module,
        state_file,
        "wat_9",
        **_complete_pr_baseline_fields(module, baseline),
        actions=module.normalize_selected_runs({"CI": [in_progress]}),
    )

    succeeded = {
        **in_progress,
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://github.com/example/actions/runs/7",
    }

    def _fetch_ci(repo, pr_number, token, **kwargs):
        state = _pr_state()
        state["pull_request"]["head"] = {"sha": "head-1"}
        state["actions"] = [succeeded]
        return state, 2

    rc, first_output, first_payload = _run_managed(
        module,
        state_file,
        _fetch_ci,
        delivery="delivery-1",
        extra_args=("--workflow", "CI"),
    )
    assert rc == 0
    assert "GitHub Actions success" in first_output
    assert module.STAGED_KEY in first_payload

    def _fetch_review(repo, pr_number, token, **kwargs):
        state = _pr_state(
            issue_comments=[
                {
                    "id": 601,
                    "body": (
                        "Codex Review: Didn't find any major issues.\n\n"
                        "**Reviewed commit:** `head-1`"
                    ),
                    "user": {"login": "chatgpt-codex-connector[bot]"},
                    "html_url": "https://github.com/example/repo/pull/153#issuecomment-601",
                }
            ]
        )
        state["pull_request"]["head"] = {"sha": "head-1"}
        state["actions"] = [succeeded]
        return state, 2

    rc, second_output, second_payload = _run_managed(
        module,
        state_file,
        _fetch_review,
        delivery="delivery-2",
        extra_args=("--workflow", "CI"),
    )

    assert rc == 0
    assert "issue_comment #601" in second_output
    assert "Didn't find any major issues" in second_output
    assert second_payload["issue_comment_cursor"] == 0
    assert second_payload[module.STAGED_KEY]["cursors"]["issue_comment_cursor"] == 601


def _ci_run(run_id=7, *, attempt=1, workflow="CI", head="head-1", status="completed", conclusion="success"):
    return {
        "id": run_id,
        "name": workflow,
        "head_sha": head,
        "head_branch": "feature",
        "status": status,
        "conclusion": conclusion,
        "run_attempt": attempt,
        "html_url": f"https://github.com/example/actions/runs/{run_id}",
    }


def _ci_state(*runs, head="head-1", comment=False):
    state = _pr_state(review_comments=[_review_comment(501)] if comment else [])
    state["pull_request"]["head"] = {"sha": head}
    state["actions"] = list(runs)
    return state


def _seed_ci_state(module, path, *runs, workflows=("CI",), head="head-1", owner="wat_9"):
    state = _ci_state(*runs, head=head)
    _managed_state(
        module,
        path,
        owner,
        **_complete_pr_baseline_fields(module, state),
        actions=module.normalize_selected_runs(
            module.select_matching_runs(list(runs), workflows=list(workflows), branch="feature", head_sha=head)
        ),
    )


def _ci_cycle(module, path, states, *, delivery="1", workflows=("CI",), settle=0, sha=None, extra_args=()):
    """Use real cursor IO in tmp_path, fake GitHub, and a bounded virtual clock."""
    clock = 0.0
    polls = iter(states)
    latest = states[-1]

    def sleep(seconds):
        nonlocal clock
        clock += seconds

    def fetch(*args, **kwargs):
        nonlocal latest
        latest = next(polls, latest)
        return latest, 2

    workflow_args = tuple(arg for workflow in workflows for arg in ("--workflow", workflow))
    ci_args = ("--branch", "feature", *workflow_args) if workflows else ()
    sha_args = ("--sha", sha) if sha is not None else ()
    with (
        patch.object(module.time, "monotonic", side_effect=lambda: clock),
        patch.object(module.time, "sleep", side_effect=sleep),
    ):
        return _run_managed(
            module,
            path,
            fetch,
            delivery=delivery,
            extra_args=(
                *ci_args, *sha_args, "--interval", "1", "--timeout", "100",
                "--settle", str(settle), *extra_args,
            ),
        )


@pytest.mark.parametrize("intermediate", ["missing", "nonterminal", "older-attempt", "partial"])
def test_combined_ci_does_not_reannounce_observed_results_after_snapshot_regression(tmp_path, intermediate):
    module = _load_module()
    path = tmp_path / "ci.json"
    first = _ci_run(attempt=2, conclusion="failure")
    second = _ci_run(8)
    _seed_ci_state(module, path, first, second)
    regressed = {
        "missing": _ci_state(),
        "nonterminal": _ci_state(_ci_run(attempt=2, status="in_progress", conclusion=None), second),
        "older-attempt": _ci_state(_ci_run(attempt=1, conclusion="failure"), second),
        "partial": _ci_state(first),
    }[intermediate]

    rc, output, _ = _ci_cycle(
        module, path, [_ci_state(first, second), regressed, _ci_state(first, second)]
    )

    assert rc == 124
    assert output == ""


def test_combined_ci_remembers_a_result_across_pr_only_delivery_and_restart(tmp_path):
    module = _load_module()
    path = tmp_path / "ci.json"
    run = _ci_run()
    _seed_ci_state(module, path, run)

    rc, output, _ = _ci_cycle(module, path, [_ci_state(comment=True)])
    assert rc == 0
    assert "review_comment #501" in output
    assert "GitHub Actions" not in output

    rc, output, _ = _ci_cycle(module, path, [_ci_state(run, comment=True)], delivery="2")
    assert rc == 124
    assert output == ""


@pytest.mark.parametrize("new_result", ["rerun", "new-run", "new-head", "conclusion"])
def test_combined_ci_still_reports_new_terminal_results_and_acknowledges_once(tmp_path, new_result):
    module = _load_module()
    path = tmp_path / "ci.json"
    old = _ci_run()
    _seed_ci_state(module, path, old)
    new = {
        "rerun": _ci_run(attempt=2),
        "new-run": _ci_run(8),
        "new-head": _ci_run(9, head="head-2"),
        "conclusion": _ci_run(conclusion="failure"),
    }[new_result]
    state = _ci_state(old, new) if new_result == "new-run" else _ci_state(new, head=new["head_sha"])

    rc, first_output, payload = _ci_cycle(module, path, [state])
    assert rc == 0
    assert "GitHub Actions" in first_output
    assert payload[module.STAGED_KEY]["output"] == first_output.strip()
    assert payload["actions"] == module.normalize_selected_runs({"CI": [old]})

    # Losing the acknowledgement must replay even when the API is unavailable.
    def unavailable(*args, **kwargs):
        raise AssertionError("replay must not reach GitHub")

    rc, replay, _ = _run_managed(
        module, path, unavailable, delivery="1", extra_args=("--branch", "feature", "--workflow", "CI")
    )
    assert rc == 0
    assert replay == first_output

    rc, output, _ = _ci_cycle(module, path, [state], delivery="2")
    assert rc == 124
    assert output == ""


def test_combined_ci_settle_drops_a_candidate_that_returns_to_the_reported_baseline(tmp_path):
    module = _load_module()
    path = tmp_path / "ci.json"
    old = _ci_run()
    _seed_ci_state(module, path, old)

    rc, output, _ = _ci_cycle(
        module, path, [_ci_state(old), _ci_state(_ci_run(attempt=2)), _ci_state(old)], settle=1
    )

    assert rc == 124
    assert output == ""


def test_combined_ci_waits_for_all_workflows_without_reannouncing_a_restored_one(tmp_path):
    module = _load_module()
    path = tmp_path / "ci.json"
    ci = _ci_run()
    security = _ci_run(8, workflow="Security Scan")
    workflows = ("CI", "Security Scan")
    _seed_ci_state(module, path, ci, security, workflows=workflows)

    rc, output, _ = _ci_cycle(
        module, path, [_ci_state(ci), _ci_state(ci, security)], workflows=workflows
    )
    assert rc == 124
    assert output == ""

    pending = _ci_run(attempt=2, status="in_progress", conclusion=None)
    completed = _ci_run(attempt=2)
    rc, output, _ = _ci_cycle(
        module, path,
        [_ci_state(pending, security), _ci_state(completed), _ci_state(completed, security)],
        workflows=workflows,
    )
    assert rc == 0
    assert "CI: status=completed" in output
    assert "Security Scan: status=completed" in output


@pytest.mark.parametrize("with_comment", [False, True])
def test_combined_ci_settle_does_not_commit_a_terminal_subset_of_its_candidate(tmp_path, with_comment):
    module = _load_module()
    path = tmp_path / "ci.json"
    old = _ci_run()
    failed = _ci_run(8, conclusion="failure")
    succeeded = _ci_run(9)
    _seed_ci_state(module, path, old)
    candidate = _ci_state(old, failed, succeeded, comment=with_comment)
    partial = _ci_state(old, succeeded, comment=with_comment)

    rc, output, payload = _ci_cycle(
        module, path, [_ci_state(old), candidate, partial], settle=1
    )
    assert rc == (0 if with_comment else 124)
    assert "GitHub Actions" not in output
    fields = payload[module.STAGED_KEY]["cursors"] if with_comment else payload
    assert fields["actions"] == module.normalize_selected_runs({"CI": [old]})

    # Remember the missing failed run across a restart, including a PR-only ack.
    rc, output, _ = _ci_cycle(module, path, [partial], delivery="2")
    assert rc == 124
    assert output == ""

    # The candidate was withdrawn, so the full result must still be delivered.
    rc, output, payload = _ci_cycle(module, path, [candidate], delivery="2")
    assert rc == 0
    assert "GitHub Actions failure" in output
    assert "actions/runs/8" in output
    assert payload[module.STAGED_KEY]["cursors"]["actions"] == module.normalize_selected_runs(
        {"CI": [old, failed, succeeded]}
    )


def test_combined_ci_run_order_and_timestamp_changes_are_not_events(tmp_path):
    module = _load_module()
    path = tmp_path / "ci.json"
    first = _ci_run()
    second = _ci_run(8)
    _seed_ci_state(module, path, first, second)
    first["run_started_at"] = "2026-01-02T00:00:00Z"
    second["run_started_at"] = "2026-01-01T00:00:00Z"

    rc, output, _ = _ci_cycle(module, path, [_ci_state(second, first)])
    assert rc == 124
    assert output == ""


def test_combined_ci_new_head_does_not_inherit_old_known_runs_or_lose_completion(tmp_path):
    module = _load_module()
    path = tmp_path / "ci.json"
    _seed_ci_state(module, path, _ci_run(conclusion="failure"))
    pending = _ci_run(9, head="head-2", status="in_progress", conclusion=None)
    rc, output, _ = _ci_cycle(module, path, [_ci_state(pending, head="head-2")])
    assert rc == 0
    assert "pr_head" in output
    assert "GitHub Actions" not in output

    completed = _ci_run(9, head="head-2")
    rc, output, _ = _ci_cycle(module, path, [_ci_state(completed, head="head-2")], delivery="2")
    assert rc == 0
    assert "GitHub Actions success" in output
    assert "actions/runs/7" not in output


def test_combined_ci_settle_fetch_failure_retains_the_report_and_its_snapshot(tmp_path):
    module = _load_module()
    path = tmp_path / "ci.json"
    old, new = _ci_run(), _ci_run(attempt=2)
    _seed_ci_state(module, path, old)
    error = urllib.error.HTTPError("https://api.github.com/example", 503, "Unavailable", hdrs=None, fp=None)
    polls = iter([(_ci_state(new), 2), error])

    def fetch(*args, **kwargs):
        value = next(polls)
        if isinstance(value, Exception):
            raise value
        return value

    with patch.object(module.time, "sleep", return_value=None):
        rc, output, payload = _run_managed(
            module, path, fetch, delivery="1",
            extra_args=("--branch", "feature", "--workflow", "CI", "--settle", "1", "--timeout", "0"),
        )
    assert rc == 0
    assert "GitHub Actions success" in output
    assert payload["actions"] == module.normalize_selected_runs({"CI": [old]})
    assert payload[module.STAGED_KEY]["cursors"]["actions"] == module.normalize_selected_runs({"CI": [new]})


def test_combined_ci_pinned_sha_case_does_not_start_a_new_epoch(tmp_path):
    module = _load_module()
    path = tmp_path / "ci.json"
    run = _ci_run(head="abc123")
    _seed_ci_state(module, path, run, head="abc123")

    rc, output, _ = _ci_cycle(module, path, [_ci_state(run, head="abc123")], sha="ABC123")
    assert rc == 124
    assert output == ""


@pytest.mark.parametrize("with_comment", [False, True])
def test_explicit_pr_replay_does_not_replay_unrelated_completed_ci(with_comment):
    module = _load_module()
    state = _ci_state(_ci_run(), comment=with_comment)
    stdout = io.StringIO()
    with (
        patch.dict("os.environ", {module.WATCH_ID_ENV: "", module.LAST_DELIVERY_ENV: ""}, clear=False),
        patch.object(module, "_fetch_state", return_value=(state, 2)),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch.object(module.time, "monotonic", side_effect=[0, 2]),
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--workflow", "CI",
             "--since-review-comment-id", "0", "--timeout", "1"],
        ),
        redirect_stdout(stdout),
        patch("sys.stderr", io.StringIO()),
    ):
        rc = module.main()
    assert rc == (0 if with_comment else 124)
    assert "GitHub Actions" not in stdout.getvalue()
    assert ("review_comment #501" in stdout.getvalue()) == with_comment


@pytest.mark.parametrize("mode", ["catch-up", "pr-replay", "monitor"])
@pytest.mark.parametrize("inventory_present", [False, True])
@pytest.mark.parametrize("stale_kind", ["missing-run", "higher-attempt"])
@pytest.mark.parametrize("initial_terminal", [False, True])
def test_explicit_replay_resets_ci_inventory_but_ordinary_resume_retains_it(
    tmp_path, mode, inventory_present, stale_kind, initial_terminal,
):
    module = _load_module()
    path = tmp_path / "ci.json"
    old = _ci_run()
    stale = (
        [old, _ci_run(8, conclusion="failure")]
        if stale_kind == "missing-run" else [_ci_run(attempt=9, conclusion="failure")]
    )
    _seed_ci_state(module, path, *([old] if inventory_present else stale))
    if inventory_present:
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved[module.ACTIONS_OBSERVED_KEY] = module.normalize_selected_runs({"CI": stale})
        path.write_text(json.dumps(saved), encoding="utf-8")

    current = old if initial_terminal else _ci_run(status="in_progress", conclusion=None)
    state = _ci_state(current, comment=True)
    flags = {
        "catch-up": ("--catch-up",),
        "pr-replay": ("--since-review-comment-id", "0"),
        "monitor": (),
    }[mode]
    rc, first_output, payload = _ci_cycle(module, path, [state], extra_args=flags)
    assert rc == 0
    assert "review_comment #501" in first_output
    assert ("GitHub Actions success" in first_output) == (mode == "catch-up" and initial_terminal)
    expected_inventory = module._observe_actions(
        module.normalize_selected_runs({"CI": stale}) if mode == "monitor" else {},
        module.normalize_selected_runs({"CI": [current]}),
        "head-1",
    )
    assert payload[module.STAGED_KEY]["cursors"][module.ACTIONS_OBSERVED_KEY] == expected_inventory

    # Explicit reset flags must not supersede an undelivered transaction.
    def unavailable(*args, **kwargs):
        raise AssertionError("pending replay must not reach GitHub")

    rc, replay, _ = _run_managed(
        module, path, unavailable, delivery="1",
        extra_args=("--branch", "feature", "--workflow", "CI", *flags),
    )
    assert rc == 0
    assert replay == first_output

    # A normal restart adopts the reset inventory and remains quiet on the
    # initial state. A genuinely new CI result then reports exactly once.
    rc, output, _ = _ci_cycle(module, path, [state], delivery="2")
    assert rc == 124
    assert output == ""
    complete = _ci_state(old, _ci_run(9), comment=True)
    rc, output, _ = _ci_cycle(module, path, [complete], delivery="2")
    assert rc == (124 if mode == "monitor" else 0)
    assert ("GitHub Actions success" in output) == (mode != "monitor")
    assert "review_comment #501" not in output
    rc, output, _ = _ci_cycle(module, path, [complete], delivery="3")
    assert rc == 124
    assert output == ""


@pytest.mark.parametrize(
    "flags",
    [
        ("--since-review-id", "0"),
        ("--since-review-comment-id", "0"),
        ("--since-issue-comment-id", "0"),
        ("--since-reaction-id", "0"),
        ("--since-pr-status", "open"),
        ("--catch-up",),
    ],
)
def test_explicit_replay_with_empty_ci_baselines_quietly_then_tracks_new_observations(tmp_path, flags):
    module = _load_module()
    path = tmp_path / "ci.json"
    _seed_ci_state(module, path, _ci_run(conclusion="failure"))
    rc, output, payload = _ci_cycle(module, path, [_ci_state()], extra_args=flags)
    assert rc == 124
    assert output == ""
    assert payload["actions"] == {"CI": []}
    assert payload[module.ACTIONS_OBSERVED_KEY] == {"CI": []}

    succeeded, pending = _ci_run(9), _ci_run(10, status="in_progress", conclusion=None)
    rc, output, _ = _ci_cycle(module, path, [_ci_state(succeeded, pending), _ci_state(succeeded)])
    assert rc == 124
    assert output == ""
    # Reset is an initialization boundary, not permission to forget runs
    # observed afterwards, including across a normal restart.
    rc, output, _ = _ci_cycle(module, path, [_ci_state(succeeded)])
    assert rc == 124
    assert output == ""
    complete = _ci_state(succeeded, _ci_run(10))
    rc, output, _ = _ci_cycle(module, path, [complete])
    assert rc == 0
    assert "GitHub Actions success" in output
    rc, output, _ = _ci_cycle(module, path, [complete], delivery="2")
    assert rc == 124
    assert output == ""


@pytest.mark.parametrize("head", ["head-1", "head-2"])
def test_explicit_seed_replaces_an_ownerless_actions_baseline_with_current_state(tmp_path, head):
    module = _load_module()
    path = tmp_path / "ci.json"
    _seed_ci_state(module, path, _ci_run(conclusion="failure"), owner=None)
    current = _ci_run(8, head=head)
    state = _ci_state(current, head=head)
    with (
        patch.dict("os.environ", {module.WATCH_ID_ENV: "", module.LAST_DELIVERY_ENV: ""}, clear=False),
        patch.object(module, "_fetch_state", return_value=(state, 2)),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch(
            "sys.argv",
            ["wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--branch", "feature",
             "--workflow", "CI", "--state-file", str(path), "--seed-state"],
        ),
        patch("sys.stdout", io.StringIO()),
        patch("sys.stderr", io.StringIO()),
    ):
        assert module.main() == 0

    saved = json.loads(path.read_text(encoding="utf-8"))
    expected = module.normalize_selected_runs({"CI": [current]})
    assert saved["actions"] == expected
    assert saved[module.ACTIONS_OBSERVED_KEY] == expected
    rc, output, _ = _ci_cycle(module, path, [state])
    assert rc == 124
    assert output == ""
    rc, output, _ = _ci_cycle(module, path, [_ci_state(current, _ci_run(9, head=head), head=head)])
    assert rc == 0
    assert "GitHub Actions success" in output


@pytest.mark.parametrize("with_ci", [False, True])
def test_settle_preserves_detected_pr_activity_when_later_polls_omit_it(tmp_path, with_ci):
    module = _load_module()
    path = tmp_path / "pr.json"
    old = _ci_run()
    _seed_ci_state(module, path, old)
    first = _ci_state(_ci_run(attempt=2), comment=True)
    quiet = _ci_state(old)
    calls = 0
    clock = 0.0

    def sleep(seconds):
        nonlocal clock
        clock += seconds

    def fetch(*args, **kwargs):
        nonlocal calls
        calls += 1
        return (first if calls == 1 else quiet), 2

    ci_args = ("--branch", "feature", "--workflow", "CI") if with_ci else ()
    with (
        patch.object(module.time, "sleep", side_effect=sleep),
        patch.object(module.time, "monotonic", side_effect=lambda: clock),
    ):
        rc, output, payload = _run_managed(
            module, path, fetch, delivery="1",
            extra_args=(*ci_args, "--settle", "1", "--timeout", "100"),
        )
    assert rc == 0
    assert "review_comment #501" in output
    assert "GitHub Actions" not in output
    assert payload[module.STAGED_KEY]["output"] == output.strip()
    if with_ci:
        assert payload[module.STAGED_KEY]["cursors"]["actions"] == module.normalize_selected_runs({"CI": [old]})


@pytest.mark.parametrize("during_loop", [False, True])
@pytest.mark.parametrize("ci_mode", ["off", "stable", "complete", "withdraw"])
def test_settled_pr_report_keeps_its_snapshot_through_ack_and_restart(tmp_path, during_loop, ci_mode):
    module = _load_module()
    path = tmp_path / "pr.json"
    old = _ci_run()
    _seed_ci_state(module, path, old)
    first_run = {
        "off": old,
        "stable": old,
        "complete": _ci_run(attempt=2, status="in_progress", conclusion=None),
        "withdraw": _ci_run(attempt=2),
    }[ci_mode]
    final_run = _ci_run(attempt=2) if ci_mode == "complete" else old
    first = _ci_state(first_run, comment=True)
    quiet = _ci_state(final_run)
    workflows = () if ci_mode == "off" else ("CI",)
    states = [_ci_state(old), first, quiet] if during_loop else [first, quiet]

    rc, output, payload = _ci_cycle(module, path, states, workflows=workflows, settle=1)
    assert rc == 0
    assert "review_comment #501" in output
    assert ("GitHub Actions" in output) == (ci_mode == "complete")
    staged = payload[module.STAGED_KEY]["cursors"]
    assert staged["snapshot"]["review_comments"] == module._normalized_pr_snapshot(first)["review_comments"]
    assert staged["review_comment_fingerprints"] == module._fingerprint_map(first["review_comments"])

    def unavailable(*args, **kwargs):
        raise AssertionError("unacknowledged output must replay without polling")

    ci_args = ("--branch", "feature", "--workflow", "CI") if workflows else ()
    rc, replay, _ = _run_managed(module, path, unavailable, delivery="1", extra_args=ci_args)
    assert rc == 0
    assert replay == output

    restored = _ci_state(final_run, comment=True)
    rc, output, _ = _ci_cycle(module, path, [restored], delivery="2", workflows=workflows)
    assert rc == 124
    assert output == ""

    if ci_mode == "withdraw":
        restored["actions"] = [_ci_run(attempt=2)]
        rc, output, _ = _ci_cycle(module, path, [restored], delivery="2", workflows=workflows)
        assert rc == 0
        assert "GitHub Actions success" in output
        assert "GitHub PR activity" not in output
        rc, output, _ = _ci_cycle(module, path, [restored], delivery="ci-ack", workflows=workflows)
        assert rc == 124
        assert output == ""

    restored["review_comments"][0]["body"] = "A genuine edit after acknowledgement"
    rc, output, _ = _ci_cycle(module, path, [restored], delivery="2", workflows=workflows)
    assert rc == 0
    assert "review_comment #501" in output
    rc, output, _ = _ci_cycle(module, path, [restored], delivery="3", workflows=workflows)
    assert rc == 124
    assert output == ""

    restored["review_comments"] = []
    restored["review_threads"] = [{"id": "thread-1", "isResolved": False}]
    rc, output, _ = _ci_cycle(module, path, [restored], delivery="3", workflows=workflows)
    assert rc == 0
    assert "review_thread thread-1" in output
    assert "501" not in json.loads(path.read_text())[module.STAGED_KEY]["cursors"]["snapshot"]["review_comments"]
    rc, output, _ = _ci_cycle(module, path, [restored], delivery="4", workflows=workflows)
    assert rc == 124
    assert output == ""


@pytest.mark.parametrize("during_loop", [False, True])
def test_settled_pr_fallback_does_not_attribute_current_ci_to_a_transient_head(tmp_path, during_loop):
    module = _load_module()
    path = tmp_path / "pr.json"
    old = _ci_run()
    _seed_ci_state(module, path, old)
    transient = _ci_state(_ci_run(9, head="head-2"), head="head-2", comment=True)
    quiet = _ci_state(old)
    states = [_ci_state(old), transient, quiet] if during_loop else [transient, quiet]
    rc, output, payload = _ci_cycle(module, path, states, settle=1)
    assert rc == 0
    assert "head-2" in output
    assert "GitHub Actions" not in output
    staged = payload[module.STAGED_KEY]["cursors"]
    assert payload["head_sha"] == "head-1"
    assert staged["head_sha"] == staged["snapshot"]["pull_request"]["head_sha"] == "head-2"
    assert staged["actions"] == {"CI": []}
    assert staged["actions_observed"]["CI"][0]["head_sha"] == "head-1"
    rc, output, _ = _ci_cycle(module, path, [quiet], delivery="2")
    assert rc == 0
    assert "head-2 -> head-1" in output
    assert "GitHub Actions success" in output
    rc, output, _ = _ci_cycle(module, path, [quiet], delivery="3")
    assert rc == 124
    assert output == ""


@pytest.mark.parametrize("during_loop", [False, True])
def test_settled_pr_snapshot_survives_a_failed_repoll_and_ack(tmp_path, during_loop):
    module = _load_module()
    path = tmp_path / "pr.json"
    run = _ci_run()
    _seed_ci_state(module, path, run)
    first = _ci_state(run, comment=True)
    failure = urllib.error.URLError("synthetic settle failure")
    states = [_ci_state(run), first, failure] if during_loop else [first, failure]
    clock = 0.0

    def sleep(seconds):
        nonlocal clock
        clock += seconds

    def fetch(*args, **kwargs):
        item = states.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, 2

    with (
        patch.object(module.time, "monotonic", side_effect=lambda: clock),
        patch.object(module.time, "sleep", side_effect=sleep),
    ):
        rc, output, payload = _run_managed(
            module, path, fetch, delivery="1",
            extra_args=("--workflow", "CI", "--branch", "feature", "--settle", "1", "--timeout", "100"),
        )
    assert rc == 0
    assert "review_comment #501" in output
    assert "501" in payload[module.STAGED_KEY]["cursors"]["snapshot"]["review_comments"]
    rc, output, _ = _ci_cycle(module, path, [first], delivery="2")
    assert rc == 124
    assert output == ""


@pytest.mark.parametrize("location", ["committed", "pending"])
@pytest.mark.parametrize("key", ["actions", "actions_observed"])
@pytest.mark.parametrize("bad_value", [
    {"CI": [None]}, {"CI": ["invalid"]}, {"CI": [{"id": []}]},
    {"CI": "not-a-list"}, None, [], {"CI": [{"id": 7, "run_attempt": []}]},
])
def test_malformed_saved_actions_fail_closed_without_overwriting_history(tmp_path, key, bad_value, location):
    module = _load_module()
    path = tmp_path / "pr.json"
    _seed_ci_state(module, path, _ci_run())
    saved = json.loads(path.read_text(encoding="utf-8"))
    if location == "pending":
        saved[module.STAGED_KEY] = {
            "delivered_after": "0",
            "output": "an earlier report",
            "cursors": {key: bad_value},
        }
    else:
        saved[key] = bad_value
    path.write_text(json.dumps(saved), encoding="utf-8")
    original = path.read_bytes()
    stderr = io.StringIO()
    with (
        patch.dict("os.environ", {module.WATCH_ID_ENV: "wat_9", module.LAST_DELIVERY_ENV: "1"}),
        patch.object(module, "_fetch_state", return_value=(_ci_state(_ci_run()), 2)),
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "get_authenticated_login", return_value="tester"),
        patch("sys.argv", [
            "wait_pr.py", "--repo", "avibe-bot/avibe", "--pr", "153", "--workflow", "CI",
            "--branch", "feature", "--state-file", str(path), "--timeout", "0.001",
        ]),
        patch("sys.stderr", stderr),
    ):
        assert module.run_cli() == 1
    assert key in stderr.getvalue()
    assert "malformed" in stderr.getvalue()
    assert path.read_bytes() == original


@pytest.mark.parametrize("attempt", ["missing", None])
def test_valid_legacy_actions_and_partial_pending_remain_compatible(tmp_path, attempt):
    module = _load_module()
    path = tmp_path / "pr.json"
    run = _ci_run(attempt=attempt)
    if attempt == "missing":
        run.pop("run_attempt")
    _seed_ci_state(module, path, run)
    saved = json.loads(path.read_text())
    if attempt == "missing":
        saved["actions"]["CI"][0].pop("run_attempt")
    saved[module.STAGED_KEY] = {
        "delivered_after": "0",
        "output": "legacy partial transaction",
        "cursors": {"review_comment_cursor": 500},
    }
    path.write_text(json.dumps(saved))
    rc, output, _ = _ci_cycle(module, path, [_ci_state(run)], delivery="1")
    assert rc == 124
    assert output == ""


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
