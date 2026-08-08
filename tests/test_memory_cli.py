"""CLI schema contracts for ``vibe memory``."""

from __future__ import annotations

import json

import pytest

from core.caller_context import AVIBE_SESSION_ID_ENV
from vibe import cli, internal_client


def test_memory_search_json_is_a_presentation_of_the_uds_response(monkeypatch, capsys) -> None:
    args = cli.build_parser().parse_args(["memory", "search", "find this", "--limit", "3", "--json"])
    calls: list[tuple[str, int]] = []

    def search(query: str, limit: int, **_kwargs):
        calls.append((query, limit))
        return {"status_code": 200, "body": {"status": "ok", "items": [{"kind": "fact", "text": "result"}]}}

    monkeypatch.setattr(internal_client, "memory_search_sync", search)

    assert cli.cmd_memory(args) == 0
    assert calls == [("find this", 3)]
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 1,
        "ok": True,
        "kind": "memory_search",
        "result": {"status": "ok", "items": [{"kind": "fact", "text": "result"}]},
    }


def test_memory_status_json_returns_a_closed_service_down_code(monkeypatch, capsys) -> None:
    args = cli.build_parser().parse_args(["memory", "status", "--json"])

    def unavailable(**_kwargs):
        raise internal_client.InternalServerUnavailable("socket unavailable")

    monkeypatch.setattr(internal_client, "memory_status_sync", unavailable)

    assert cli.cmd_memory(args) == 1
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 1,
        "ok": False,
        "kind": "memory_status",
        "code": "memory_sidecar_unavailable",
        "error": "memory_sidecar_unavailable",
    }


def test_memory_cli_passes_agent_session_to_the_internal_boundary(monkeypatch, capsys) -> None:
    args = cli.build_parser().parse_args(["memory", "status", "--json"])
    calls: list[dict[str, str | None]] = []
    monkeypatch.setenv(AVIBE_SESSION_ID_ENV, "ses-admin")

    def status(**kwargs):
        calls.append(kwargs)
        return {
            "status_code": 200,
            "body": {
                "status": "ok",
                "source": {"status": "available", "observed_at": "2026-08-08T12:00:00Z", "reason": None},
                "health": None,
            },
        }

    monkeypatch.setattr(internal_client, "memory_status_sync", status)

    assert cli.cmd_memory(args) == 0
    assert calls == [{"caller_session_id": "ses-admin"}]
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_memory_cli_rejects_out_of_range_search_without_transport(monkeypatch, capsys) -> None:
    args = cli.build_parser().parse_args(["memory", "search", "query", "--limit", "21", "--json"])

    def transport_must_not_run(*_args, **_kwargs):
        raise AssertionError("invalid CLI input reached the UDS")

    monkeypatch.setattr(internal_client, "memory_search_sync", transport_must_not_run)

    assert cli.cmd_memory(args) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "memory_invalid_input"


def test_memory_cli_human_output_uses_configured_i18n(monkeypatch, capsys) -> None:
    args = cli.build_parser().parse_args(["memory", "status"])
    monkeypatch.setattr(cli, "_memory_cli_language", lambda: "zh")
    monkeypatch.setattr(
        internal_client,
        "memory_status_sync",
        lambda **_kwargs: {
            "status_code": 200,
            "body": {
                "status": "ok",
                "source": {
                    "status": "stale",
                    "observed_at": "2026-08-08T12:00:00Z",
                    "reason": "memory_sidecar_unavailable",
                },
                "health": {
                    "status": "ok",
                    "version": "1.2.3",
                    "capabilities": {},
                    "disabled_features": [],
                    "cascade": {},
                    "recorder": {},
                },
            },
        },
    )

    assert cli.cmd_memory(args) == 0
    assert capsys.readouterr().out.splitlines() == [
        "记忆来源：数据已过期",
        "EverOS 1.2.3：正常",
        "来源原因：记忆 sidecar 不可用",
    ]


def test_memory_cli_human_status_uses_localized_fallbacks_for_unknown_tokens(
    monkeypatch,
    capsys,
) -> None:
    args = cli.build_parser().parse_args(["memory", "status"])
    monkeypatch.setattr(cli, "_memory_cli_language", lambda: "zh")
    monkeypatch.setattr(
        internal_client,
        "memory_status_sync",
        lambda **_kwargs: {
            "status_code": 200,
            "body": {
                "status": "ok",
                "source": {"status": "future_state", "reason": "future_reason"},
                "health": {"status": "future_health"},
            },
        },
    )

    assert cli.cmd_memory(args) == 0
    output = capsys.readouterr().out.splitlines()
    assert output == [
        "记忆来源：未知",
        "EverOS 未知版本：未知",
        "来源原因：未知原因",
    ]
    assert "future_" not in "\n".join(output)


def test_memory_cli_locale_read_failure_keeps_closed_service_down_error(monkeypatch, capsys) -> None:
    args = cli.build_parser().parse_args(["memory", "status"])

    def fail_config_path():
        raise RuntimeError("source checkout migration guard")

    def unavailable(**_kwargs):
        raise internal_client.InternalServerUnavailable("socket unavailable")

    monkeypatch.setattr(cli.paths, "get_config_path", fail_config_path)
    monkeypatch.setattr(internal_client, "memory_status_sync", unavailable)

    assert cli.cmd_memory(args) == 1
    assert capsys.readouterr().err.strip() == "Memory status failed: memory_sidecar_unavailable"


@pytest.mark.parametrize("outcome", ["accepted", "duplicate"])
def test_memory_remember_exits_zero_only_for_queued_outcomes(
    monkeypatch,
    capsys,
    outcome,
) -> None:
    args = cli.build_parser().parse_args(["memory", "remember", "keep this", "--json"])
    monkeypatch.setattr(
        internal_client,
        "memory_remember_sync",
        lambda text, **_kwargs: {"status_code": 200, "body": {"status": outcome}},
    )

    assert cli.cmd_memory(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["result"]["status"] == outcome


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"status": "skipped", "reason": "memory_disabled"}, "memory_disabled"),
        ({"status": "skipped", "reason": "memory_queue_full"}, "memory_queue_full"),
        ({"status": "skipped", "reason": "memory_low_disk_space"}, "memory_low_disk_space"),
        ({"status": "failed", "error": "memory_store_unavailable"}, "memory_store_unavailable"),
    ],
)
def test_memory_remember_nonqueued_outcomes_exit_nonzero(monkeypatch, capsys, body, expected) -> None:
    args = cli.build_parser().parse_args(["memory", "remember", "keep this", "--json"])
    monkeypatch.setattr(
        internal_client,
        "memory_remember_sync",
        lambda text, **_kwargs: {"status_code": 200, "body": body},
    )

    assert cli.cmd_memory(args) == 1
    assert json.loads(capsys.readouterr().out)["code"] == expected


def test_memory_remember_rejects_over_limit_text_before_transport(monkeypatch, capsys) -> None:
    args = cli.build_parser().parse_args(["memory", "remember", "x" * 4_001, "--json"])
    monkeypatch.setattr(
        internal_client,
        "memory_remember_sync",
        lambda *_args, **_kwargs: pytest.fail("invalid input reached the controller"),
    )

    assert cli.cmd_memory(args) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "memory_invalid_input"
