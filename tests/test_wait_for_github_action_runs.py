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


def test_select_runs_by_workflow_keeps_every_matching_rerun() -> None:
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

    assert [run["id"] for run in selected["CI"]] == [1, 2]
    assert selected["Security Scan"] == []


def test_render_actions_result_waits_for_missing_or_running_runs() -> None:
    module = _load_module()
    output, failed = module._render_actions_result(
        repo="cyhhao/sub2api",
        branch="main",
        head_sha="abc123",
        selected={
            "CI": [
                {
                    "id": 1,
                    "name": "CI",
                    "status": "in_progress",
                    "conclusion": None,
                }
            ],
            "Security Scan": [],
        },
        success_conclusions={"success"},
    )

    assert output is None
    assert failed is False


def test_normalize_selected_runs_distinguishes_rerun_attempts() -> None:
    module = _load_module()
    first = module._normalize_selected_runs(
        {
            "CI": [
                {
                    "id": 1,
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": "abc123",
                    "head_branch": "main",
                    "run_attempt": 1,
                }
            ]
        }
    )
    rerun = module._normalize_selected_runs(
        {
            "CI": [
                {
                    "id": 1,
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": "abc123",
                    "head_branch": "main",
                    "run_attempt": 2,
                }
            ]
        }
    )

    assert first != rerun
    assert first["CI"][0]["run_attempt"] == 1


def test_render_actions_result_reports_success() -> None:
    module = _load_module()
    output, failed = module._render_actions_result(
        repo="cyhhao/sub2api",
        branch="main",
        head_sha="abc123",
        selected={
            "CI": [
                {
                    "id": 1,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://github.com/example/actions/runs/1",
                }
            ],
            "Security Scan": [
                {
                    "id": 2,
                    "name": "Security Scan",
                    "status": "completed",
                    "conclusion": "skipped",
                    "html_url": "https://github.com/example/actions/runs/2",
                }
            ],
        },
        success_conclusions={"success", "skipped"},
    )

    assert output is not None
    assert "GitHub Actions success" in output
    assert "CI: status=completed conclusion=success" in output
    assert "Security Scan: status=completed conclusion=skipped" in output
    assert failed is False


def test_render_actions_result_keeps_an_older_failed_rerun_visible() -> None:
    module = _load_module()
    output, failed = module._render_actions_result(
        repo="cyhhao/sub2api",
        branch="main",
        head_sha="abc123",
        selected={
            "CI": [
                {
                    "id": 1,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/example/actions/runs/1",
                },
                {
                    "id": 2,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://github.com/example/actions/runs/2",
                },
            ]
        },
        success_conclusions={"success"},
    )

    assert output is not None
    assert "GitHub Actions failure" in output
    assert "actions/runs/1" in output
    assert "actions/runs/2" in output
    assert failed is True


def test_render_actions_result_reports_failure_but_is_an_event() -> None:
    module = _load_module()
    output, failed = module._render_actions_result(
        repo="cyhhao/sub2api",
        branch="main",
        head_sha="abc123",
        selected={
            "CI": [
                {
                    "id": 1,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/example/actions/runs/1",
                }
            ]
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


def test_main_retries_retryable_startup_http_error_inside_one_shot() -> None:
    module = _load_module()
    err = urllib.error.HTTPError(
        url="https://api.github.com/repos/example/repo/actions/runs",
        code=503,
        msg="Service Unavailable",
        hdrs=None,
        fp=None,
    )
    completed = [
        {
            "id": 1,
            "name": "CI",
            "head_sha": "abc123",
            "status": "completed",
            "conclusion": "success",
        }
    ]

    with (
        patch.object(module, "get_token", return_value="token"),
        patch.object(
            module,
            "_fetch_workflow_runs",
            side_effect=[err, err, (completed, 1)],
        ) as fetch,
        patch.object(module.time, "sleep", return_value=None) as sleep,
        patch("sys.argv", ["wait_action.py", "--repo", "cyhhao/sub2api", "--sha", "abc123", "--workflow", "CI"]),
    ):
        rc = module.main()

    assert rc == 0
    assert fetch.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1.0, 2.0]


def test_main_unexpected_startup_error_fails_fast() -> None:
    module = _load_module()

    with (
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "_fetch_workflow_runs", side_effect=RuntimeError("bad payload")),
        patch("sys.argv", ["wait_action.py", "--repo", "cyhhao/sub2api", "--sha", "abc123", "--workflow", "CI"]),
    ):
        rc = module.main()

    assert rc == 1


def test_main_stops_on_a_terminal_polling_http_error() -> None:
    module = _load_module()
    error = urllib.error.HTTPError(
        url="https://api.github.com/repos/example/repo/actions/runs",
        code=404,
        msg="Not Found",
        hdrs=None,
        fp=None,
    )

    runs = [
        {
            "id": 1,
            "name": "CI",
            "head_sha": "abc123",
            "head_branch": "main",
            "status": "in_progress",
            "conclusion": None,
        }
    ]
    with (
        patch.object(module, "get_token", return_value="token"),
        patch.object(module, "_fetch_workflow_runs", side_effect=[(runs, 1), error]),
        patch.object(module.time, "sleep", return_value=None),
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
            ],
        ),
    ):
        rc = module.main()

    assert rc == 1


def test_main_recovers_from_retryable_actions_polling_failure() -> None:
    module = _load_module()
    running = [
        {
            "id": 1,
            "name": "CI",
            "head_sha": "abc123",
            "status": "in_progress",
            "conclusion": None,
        }
    ]
    completed = [
        {
            "id": 1,
            "name": "CI",
            "head_sha": "abc123",
            "status": "completed",
            "conclusion": "success",
        }
    ]
    error = urllib.error.URLError("temporary network failure")

    with (
        patch.object(module, "get_token", return_value="token"),
        patch.object(
            module,
            "_fetch_workflow_runs",
            side_effect=[(running, 1), error, (completed, 1)],
        ) as fetch,
        patch.object(module.time, "sleep", return_value=None) as sleep,
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
                "--interval",
                "1",
            ],
        ),
    ):
        rc = module.main()

    assert rc == 0
    assert fetch.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1.0, 1.0]


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
    stderr = io.StringIO()
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
        patch("sys.stderr", stderr),
    ):
        rc = module.main()

    assert rc == module.NO_EVENT_EXIT_CODE
    # Nothing on stdout means vibe watch builds no follow-up prompt.
    assert stdout.getvalue() == ""
    # The code alone is sysexits EX_USAGE; the marker is what makes it a quiet cycle
    # rather than a failure, so the waiter has to print it.
    assert module.NO_EVENT_MARKER in stderr.getvalue()
    assert "All watched workflows succeeded" in stderr.getvalue()


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
