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


def _pr_state(*, reviews=None, issue_comments=None, head="head"):
    return {
        "pull_request": {
            "number": 1213,
            "state": "open",
            "draft": False,
            "head": {"sha": head},
            "html_url": "https://github.com/avibe-bot/avibe/pull/1213",
        },
        "reviews": list(reviews or []),
        "review_comments": [],
        "issue_comments": list(issue_comments or []),
        "reactions": [],
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
            }
        )
    )


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
