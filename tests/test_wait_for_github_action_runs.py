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
        / "wait_action.py"
    )
    spec = importlib.util.spec_from_file_location("wait_action", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_select_latest_runs_by_workflow_matches_sha_branch_and_workflow() -> None:
    module = _load_module()
    runs = [
        {
            "id": 1,
            "name": "CI",
            "head_sha": "abc123",
            "head_branch": "main",
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-04-24T00:00:00Z",
        },
        {
            "id": 2,
            "name": "CI",
            "head_sha": "abc123",
            "head_branch": "main",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-04-24T00:01:00Z",
        },
        {
            "id": 3,
            "name": "Security Scan",
            "head_sha": "other",
            "head_branch": "main",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-04-24T00:02:00Z",
        },
    ]

    selected = module._select_latest_runs_by_workflow(
        runs,
        workflows=["CI", "Security Scan"],
        branch="main",
        head_sha="abc123",
    )

    assert selected["CI"]["id"] == 2
    assert selected["Security Scan"] is None


def test_render_actions_result_waits_for_missing_or_running_runs() -> None:
    module = _load_module()
    output, failed = module._render_actions_result(
        repo="cyhhao/sub2api",
        branch="main",
        head_sha="abc123",
        selected={
            "CI": {
                "id": 1,
                "name": "CI",
                "status": "in_progress",
                "conclusion": None,
            },
            "Security Scan": None,
        },
        success_conclusions={"success"},
    )

    assert output is None
    assert failed is False


def test_render_actions_result_reports_success() -> None:
    module = _load_module()
    output, failed = module._render_actions_result(
        repo="cyhhao/sub2api",
        branch="main",
        head_sha="abc123",
        selected={
            "CI": {
                "id": 1,
                "name": "CI",
                "status": "completed",
                "conclusion": "success",
                "html_url": "https://github.com/example/actions/runs/1",
            },
            "Security Scan": {
                "id": 2,
                "name": "Security Scan",
                "status": "completed",
                "conclusion": "skipped",
                "html_url": "https://github.com/example/actions/runs/2",
            },
        },
        success_conclusions={"success", "skipped"},
    )

    assert output is not None
    assert "GitHub Actions success" in output
    assert "CI: status=completed conclusion=success" in output
    assert "Security Scan: status=completed conclusion=skipped" in output
    assert failed is False


def test_render_actions_result_reports_failure_but_is_an_event() -> None:
    module = _load_module()
    output, failed = module._render_actions_result(
        repo="cyhhao/sub2api",
        branch="main",
        head_sha="abc123",
        selected={
            "CI": {
                "id": 1,
                "name": "CI",
                "status": "completed",
                "conclusion": "failure",
                "html_url": "https://github.com/example/actions/runs/1",
            }
        },
        success_conclusions={"success"},
    )

    assert output is not None
    assert "GitHub Actions failure" in output
    assert "Failed workflow(s): CI" in output
    assert failed is True


def test_main_waits_until_target_runs_complete() -> None:
    module = _load_module()
    calls = 0

    def _fake_fetch_workflow_runs(repo, token, *, branch=None, head_sha=None, max_pages=3):
        nonlocal calls
        calls += 1
        assert repo == "cyhhao/sub2api"
        assert branch == "main"
        assert head_sha == "abc123"
        if calls == 1:
            return (
                [
                    {
                        "id": 1,
                        "name": "CI",
                        "head_sha": "abc123",
                        "head_branch": "main",
                        "status": "in_progress",
                        "conclusion": None,
                    }
                ],
                1,
            )
        return (
            [
                {
                    "id": 1,
                    "name": "CI",
                    "head_sha": "abc123",
                    "head_branch": "main",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://github.com/example/actions/runs/1",
                }
            ],
            1,
        )

    stdout = io.StringIO()
    with (
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "_fetch_workflow_runs", side_effect=_fake_fetch_workflow_runs),
        patch.object(module.time, "sleep", return_value=None),
        patch("sys.argv", ["wait_action.py", "--repo", "cyhhao/sub2api", "--branch", "main", "--sha", "abc123", "--workflow", "CI", "--interval", "1"]),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    assert rc == 0
    assert calls == 2
    assert "GitHub Actions success" in stdout.getvalue()


def test_main_returns_zero_for_completed_failed_workflow() -> None:
    module = _load_module()

    def _fake_fetch_workflow_runs(repo, token, *, branch=None, head_sha=None, max_pages=3):
        return (
            [
                {
                    "id": 1,
                    "name": "CI",
                    "head_sha": "abc123",
                    "head_branch": "main",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/example/actions/runs/1",
                }
            ],
            1,
        )

    stdout = io.StringIO()
    with (
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "_fetch_workflow_runs", side_effect=_fake_fetch_workflow_runs),
        patch("sys.argv", ["wait_action.py", "--repo", "cyhhao/sub2api", "--branch", "main", "--sha", "abc123", "--workflow", "CI"]),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    assert rc == 0
    assert "GitHub Actions failure" in stdout.getvalue()


def test_main_requires_authentication_by_default() -> None:
    module = _load_module()

    with (
        patch.object(module, "get_token", return_value=None),
        patch("sys.argv", ["wait_action.py", "--repo", "cyhhao/sub2api", "--sha", "abc123", "--workflow", "CI"]),
    ):
        rc = module.main()

    assert rc == 2


def test_main_uses_real_request_count_for_unauthenticated_interval() -> None:
    module = _load_module()
    calls = 0
    sleep_intervals: list[float] = []

    def _fake_fetch_workflow_runs(repo, token, *, branch=None, head_sha=None, max_pages=3):
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                [
                    {
                        "id": 1,
                        "name": "CI",
                        "head_sha": "abc123",
                        "head_branch": "main",
                        "status": "in_progress",
                        "conclusion": None,
                    }
                ],
                3,
            )
        return (
            [
                {
                    "id": 1,
                    "name": "CI",
                    "head_sha": "abc123",
                    "head_branch": "main",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://github.com/example/actions/runs/1",
                }
            ],
            3,
        )

    def _fake_sleep(seconds):
        sleep_intervals.append(seconds)

    stdout = io.StringIO()
    with (
        patch.object(module, "get_token", return_value=None),
        patch.object(module, "_fetch_workflow_runs", side_effect=_fake_fetch_workflow_runs),
        patch.object(module, "min_interval_for_unauthenticated", return_value=240.0) as min_interval,
        patch.object(module.time, "sleep", side_effect=_fake_sleep),
        patch(
            "sys.argv",
            [
                "wait_action.py",
                "--repo",
                "cyhhao/sub2api",
                "--branch",
                "main",
                "--sha",
                "abc123",
                "--workflow",
                "CI",
                "--interval",
                "1",
                "--allow-unauthenticated",
            ],
        ),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    assert rc == 0
    min_interval.assert_any_call(3, bootstrap_requests=3)
    assert sleep_intervals == [240.0]
    assert "GitHub Actions success" in stdout.getvalue()


def test_main_retryable_startup_http_error_returns_retry_code() -> None:
    module = _load_module()
    err = urllib.error.HTTPError(
        url="https://api.github.com/repos/example/repo/actions/runs",
        code=503,
        msg="Service Unavailable",
        hdrs=None,
        fp=None,
    )

    with (
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "_fetch_workflow_runs", side_effect=err),
        patch("sys.argv", ["wait_action.py", "--repo", "cyhhao/sub2api", "--sha", "abc123", "--workflow", "CI"]),
    ):
        rc = module.main()

    assert rc == module.RETRY_EXIT_CODE


def test_main_unexpected_startup_error_fails_fast() -> None:
    module = _load_module()

    with (
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "_fetch_workflow_runs", side_effect=RuntimeError("bad payload")),
        patch("sys.argv", ["wait_action.py", "--repo", "cyhhao/sub2api", "--sha", "abc123", "--workflow", "CI"]),
    ):
        rc = module.main()

    assert rc == 1


def test_main_only_on_failure_skips_agent_turn_for_green_run() -> None:
    module = _load_module()

    def _fake_fetch_workflow_runs(repo, token, *, branch=None, head_sha=None, max_pages=3):
        return (
            [
                {
                    "id": 1,
                    "name": "CI",
                    "head_sha": "abc123",
                    "head_branch": "main",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://github.com/example/actions/runs/1",
                }
            ],
            1,
        )

    stdout = io.StringIO()
    with (
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "_fetch_workflow_runs", side_effect=_fake_fetch_workflow_runs),
        patch(
            "sys.argv",
            [
                "wait_action.py",
                "--repo",
                "cyhhao/sub2api",
                "--branch",
                "main",
                "--sha",
                "abc123",
                "--workflow",
                "CI",
                "--only-on-failure",
            ],
        ),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    assert rc == module.NO_EVENT_EXIT_CODE
    # Nothing on stdout means vibe watch builds no follow-up prompt.
    assert stdout.getvalue() == ""


def test_main_only_on_failure_still_reports_failed_run() -> None:
    module = _load_module()

    def _fake_fetch_workflow_runs(repo, token, *, branch=None, head_sha=None, max_pages=3):
        return (
            [
                {
                    "id": 1,
                    "name": "CI",
                    "head_sha": "abc123",
                    "head_branch": "main",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/example/actions/runs/1",
                }
            ],
            1,
        )

    stdout = io.StringIO()
    with (
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "_fetch_workflow_runs", side_effect=_fake_fetch_workflow_runs),
        patch(
            "sys.argv",
            [
                "wait_action.py",
                "--repo",
                "cyhhao/sub2api",
                "--sha",
                "abc123",
                "--workflow",
                "CI",
                "--only-on-failure",
            ],
        ),
        redirect_stdout(stdout),
    ):
        rc = module.main()

    assert rc == 0
    assert "GitHub Actions failure" in stdout.getvalue()
    assert "Failed workflow(s): CI" in stdout.getvalue()
