"""Behavior evidence for the repo-local PR waiter transaction boundary."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "wait_pr.py"
SPEC = importlib.util.spec_from_file_location("repo_local_wait_pr", SCRIPT)
assert SPEC and SPEC.loader
wait_pr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = wait_pr
SPEC.loader.exec_module(wait_pr)


def _pr_state(
    *,
    reviews=None,
    review_comments=None,
    issue_comments=None,
    reactions=None,
    review_threads=None,
    head="head",
):
    return {
        "pull_request": {
            "number": 1213,
            "state": "open",
            "draft": False,
            "head": {"sha": head},
            "html_url": "https://github.com/avibe-bot/avibe/pull/1213",
        },
        "reviews": list(reviews or []),
        "review_comments": list(review_comments or []),
        "issue_comments": list(issue_comments or []),
        "reactions": list(reactions or []),
        "review_threads": list(review_threads or []),
    }


def _seeded_state(path: Path, *, review_fingerprints=None, head="head"):
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": "avibe-bot/avibe",
                "pr": 1213,
                "watch": None,
                "owner": None,
                "review_cursor": 7,
                "review_comment_cursor": 10,
                "issue_comment_cursor": 10,
                "reaction_cursor": 0,
                "pr_status": "open",
                "head_sha": head,
                "review_fingerprints": review_fingerprints or {},
                "review_comment_fingerprints": {},
                "issue_comment_fingerprints": {},
                "review_thread_states": {},
            }
        )
    )


def _include_self_watch_identity():
    return wait_pr._watch_identity(
        wait_pr._build_parser().parse_args(
            ["--repo", "avibe-bot/avibe", "--pr", "1213", "--include-self-comments"]
        )
    )


def test_token_precedence_matches_github_cli(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "preferred")
    monkeypatch.setenv("GITHUB_TOKEN", "stale")

    assert wait_pr.get_token() == "preferred"


def test_settle_keeps_one_review_baseline_for_every_candidate(monkeypatch, tmp_path, capsys):
    old_review = {
        "id": 7,
        "state": "APPROVED",
        "updated_at": "2026-08-10T00:00:00Z",
        "submitted_at": "2026-08-09T23:00:00Z",
        "body": "",
        "user": {"login": "reviewer"},
        "html_url": "https://example.invalid/review/7",
    }
    changed_review = {**old_review, "updated_at": "2026-08-10T00:01:00Z", "state": "DISMISSED"}
    initial = _pr_state(reviews=[changed_review])
    settled = _pr_state(
        reviews=[changed_review],
        issue_comments=[
            {
                "id": 11,
                "body": "The review was dismissed.",
                "user": {"login": "reviewer"},
                "html_url": "https://example.invalid/comment/11",
            }
        ],
    )
    state_file = tmp_path / "state.json"
    _seeded_state(state_file, review_fingerprints={"7": wait_pr._review_fingerprint(old_review)})
    calls = iter([initial, settled, settled])
    monkeypatch.setattr(wait_pr, "get_token", lambda: "token")
    monkeypatch.setattr(wait_pr, "_fetch_state", lambda *args, **kwargs: (next(calls), 1))
    monkeypatch.setattr(wait_pr.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(wait_pr, "REQUEST_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--repo",
            "avibe-bot/avibe",
            "--pr",
            "1213",
            "--include-self-comments",
            "--state-file",
            str(state_file),
            "--settle",
            "0.001",
            "--timeout",
            "30",
            "--event-limit",
            "20",
        ],
    )

    assert wait_pr.main() == 0
    output = capsys.readouterr().out
    assert "review #7" in output
    assert "issue_comment #11" in output


def test_undelivered_staged_report_replays_payload_without_github(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": "avibe-bot/avibe",
                "pr": 1213,
                "watch": "watch-1",
                "owner": "watch-1",
                "review_cursor": 1,
                "pending": {
                    "delivered_after": "delivery-1",
                    "output": "persisted report",
                    "cursors": {"review_cursor": 2},
                },
            }
        )
    )

    saved = json.loads(state_file.read_text())
    resolved, replay = wait_pr._resolve_staged_state(
        str(state_file),
        saved,
        delivery="delivery-1",
        repo="avibe-bot/avibe",
        pr_number=1213,
        watch_identity=None,
        watch_id="watch-1",
    )
    assert resolved["pending"]["output"] == "persisted report"
    assert replay == "persisted report"
    assert json.loads(state_file.read_text())["pending"]["output"] == "persisted report"

    resolved, replay = wait_pr._resolve_staged_state(
        str(state_file),
        json.loads(state_file.read_text()),
        delivery="delivery-2",
        repo="avibe-bot/avibe",
        pr_number=1213,
        watch_identity=None,
        watch_id="watch-1",
    )
    assert replay is None
    assert resolved["review_cursor"] == 2
    assert "pending" not in json.loads(state_file.read_text())


def test_pending_report_replays_before_authentication_preflight(monkeypatch, tmp_path, capsys):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": "avibe-bot/avibe",
                "pr": 1213,
                "watch": _include_self_watch_identity(),
                "owner": "watch-1",
                "pending": {
                    "delivered_after": None,
                    "output": "persisted report",
                    "cursors": {"review_cursor": 2},
                },
            }
        )
    )
    monkeypatch.setenv("AVIBE_WATCH_ID", "watch-1")
    monkeypatch.delenv(wait_pr.LAST_DELIVERY_ENV, raising=False)
    monkeypatch.setattr(wait_pr, "get_token", lambda: None)
    monkeypatch.setattr(
        wait_pr,
        "_verify_state_file_writable",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--repo",
            "avibe-bot/avibe",
            "--pr",
            "1213",
            "--include-self-comments",
            "--state-file",
            str(state_file),
        ],
    )

    assert wait_pr.main() == 0
    assert "persisted report" in capsys.readouterr().out


def test_viewer_failure_does_not_claim_state_file(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    monkeypatch.delenv("AVIBE_WATCH_ID", raising=False)
    monkeypatch.setattr(wait_pr, "get_token", lambda: "token")
    monkeypatch.setattr(
        wait_pr,
        "resolve_authenticated_login",
        lambda _token: (_ for _ in ()).throw(RuntimeError("viewer unavailable")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--repo",
            "avibe-bot/avibe",
            "--pr",
            "1213",
            "--state-file",
            str(state_file),
            "--timeout",
            "1",
        ],
    )

    assert wait_pr.main() == 1
    assert not state_file.exists()


def test_cursorless_state_allows_same_watch_to_change_filters(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": "avibe-bot/avibe",
                "pr": 1213,
                "watch": "old-filter-identity",
                "owner": "watch-1",
            }
        )
    )

    saved = wait_pr._load_state_file(
        str(state_file),
        repo="avibe-bot/avibe",
        pr_number=1213,
        watch_identity="new-filter-identity",
        watch_id="watch-1",
    )
    assert saved["owner"] == "watch-1"


def test_resume_requires_all_persisted_activity_baselines(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": "avibe-bot/avibe",
                "pr": 1213,
                "watch": _include_self_watch_identity(),
                "owner": "watch-1",
                "review_cursor": 1,
                "review_comment_cursor": 2,
                "issue_comment_cursor": 3,
                "reaction_cursor": 4,
                "pr_status": "open",
            }
        )
    )
    monkeypatch.setenv("AVIBE_WATCH_ID", "watch-1")
    monkeypatch.setattr(wait_pr, "get_token", lambda: "token")
    monkeypatch.setattr(wait_pr, "_fetch_state", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--repo",
            "avibe-bot/avibe",
            "--pr",
            "1213",
            "--include-self-comments",
            "--state-file",
            str(state_file),
        ],
    )

    assert wait_pr.main() == 2
    missing = wait_pr._missing_pr_baselines(json.loads(state_file.read_text()))
    assert set(missing) == {
        "head_sha",
        "review_fingerprints",
        "review_comment_fingerprints",
        "issue_comment_fingerprints",
        "review_thread_states",
    }


def test_cursor_covered_comment_edits_are_reported():
    old_review_comment = {
        "id": 5,
        "updated_at": "2026-08-10T00:00:00Z",
        "body": "old inline request",
        "path": "wait_pr.py",
        "user": {"login": "reviewer"},
        "html_url": "https://example.invalid/review-comment/5",
    }
    old_issue_comment = {
        "id": 6,
        "updated_at": "2026-08-10T00:00:00Z",
        "body": "old conversation request",
        "user": {"login": "reviewer"},
        "html_url": "https://example.invalid/issue-comment/6",
    }
    changed_review_comment = {
        **old_review_comment,
        "updated_at": "2026-08-10T00:01:00Z",
        "body": "new inline request",
    }
    changed_issue_comment = {
        **old_issue_comment,
        "updated_at": "2026-08-10T00:01:00Z",
        "body": "new conversation request",
    }

    result = wait_pr._render_activity(
        repo="avibe-bot/avibe",
        pr_number=1213,
        state=_pr_state(
            review_comments=[changed_review_comment],
            issue_comments=[changed_issue_comment],
        ),
        review_cursor=0,
        review_comment_cursor=5,
        issue_comment_cursor=6,
        reaction_cursor=0,
        pr_status="open",
        head_sha="head",
        event_limit=10,
        ignore_self_comments=False,
        review_comment_fingerprints={"5": wait_pr._comment_fingerprint(old_review_comment)},
        issue_comment_fingerprints={"6": wait_pr._comment_fingerprint(old_issue_comment)},
    )

    assert "review_comment #5" in result[0]
    assert "issue_comment #6" in result[0]


def test_event_limit_never_omits_codex_pass_reaction():
    issue_comments = [
        {
            "id": comment_id,
            "body": f"comment {comment_id}",
            "user": {"login": "reviewer"},
            "html_url": f"https://example.invalid/comment/{comment_id}",
        }
        for comment_id in (1, 2)
    ]
    reaction = {
        "id": 9,
        "content": "+1",
        "created_at": "2026-08-10T00:00:00Z",
        "user": {"login": "chatgpt-codex-connector[bot]"},
    }

    result = wait_pr._render_activity(
        repo="avibe-bot/avibe",
        pr_number=1213,
        state=_pr_state(issue_comments=issue_comments, reactions=[reaction]),
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        head_sha="head",
        event_limit=1,
        ignore_self_comments=False,
    )

    assert "pr_reaction #9" in result[0]
    assert "2 additional event(s) omitted" in result[0]


def test_codex_pass_reaction_accepts_both_api_login_forms_only():
    for login in ("chatgpt-codex-connector", "chatgpt-codex-connector[bot]"):
        assert wait_pr._is_codex_pass_reaction(
            {"content": "+1", "user": {"login": login}}
        )

    assert not wait_pr._is_codex_pass_reaction(
        {"content": "+1", "user": {"login": "another-reviewer"}}
    )


def test_review_thread_state_changes_are_paginated_and_reported(monkeypatch):
    cursors = []

    def _graphql(_query, variables, _token):
        cursors.append(variables["endCursor"])
        if variables["endCursor"] is None:
            nodes = [{"id": "thread-1", "isResolved": True}]
            page_info = {"hasNextPage": True, "endCursor": "cursor-1"}
        else:
            nodes = [{"id": "thread-2", "isResolved": False}]
            page_info = {"hasNextPage": False, "endCursor": "cursor-2"}
        return {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {"nodes": nodes, "pageInfo": page_info},
                }
            }
        }

    monkeypatch.setattr(wait_pr, "github_graphql", _graphql)
    threads, request_count = wait_pr._fetch_review_threads("avibe-bot/avibe", 1213, "token")

    assert request_count == 2
    assert cursors == [None, "cursor-1"]
    assert [thread["id"] for thread in threads] == ["thread-1", "thread-2"]

    result = wait_pr._render_activity(
        repo="avibe-bot/avibe",
        pr_number=1213,
        state=_pr_state(review_threads=threads),
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        head_sha="head",
        event_limit=1,
        ignore_self_comments=False,
        review_thread_states={"thread-1": False, "thread-2": False},
    )

    assert "review_thread thread-1 unresolved -> resolved" in result[0]


def test_head_change_is_reported_as_pr_activity():
    result = wait_pr._render_activity(
        repo="avibe-bot/avibe",
        pr_number=1213,
        state=_pr_state(head="new-head"),
        review_cursor=0,
        review_comment_cursor=0,
        issue_comment_cursor=0,
        reaction_cursor=0,
        pr_status="open",
        head_sha="old-head",
        event_limit=10,
        ignore_self_comments=False,
    )

    assert result[0] is not None
    assert "pr_head #1213 old-head -> new-head" in result[0]
    assert result[-1] == "new-head"
