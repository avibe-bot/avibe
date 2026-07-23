"""CLI schema contracts for ``vibe memory``."""

from __future__ import annotations

import json

from vibe import cli, internal_client


def test_memory_search_json_is_a_presentation_of_the_uds_response(monkeypatch, capsys) -> None:
    args = cli.build_parser().parse_args(["memory", "search", "find this", "--limit", "3", "--json"])
    calls: list[tuple[str, int]] = []

    def search(query: str, limit: int):
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

    def unavailable():
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
        lambda: {
            "status_code": 200,
            "body": {
                "state": "degraded",
                "pending": 1,
                "processing": 0,
                "awaiting_receipt": 2,
                "succeeded": 3,
                "receipt_unknown": 4,
                "distill_failed": 5,
                "dead": 6,
                "missed": 7,
                "processing_fault_kind": "credential",
            },
        },
    )

    assert cli.cmd_memory(args) == 0
    assert capsys.readouterr().out.splitlines() == [
        "记忆状态：degraded",
        "处理中：3；成功：3；结果未知：4；失败：5；已放弃：6；已跳过：7",
        "记忆引擎无法调用已配置的模型接口，请检查 API Key 余额/权限。",
    ]


def test_memory_cli_locale_read_failure_keeps_closed_service_down_error(monkeypatch, capsys) -> None:
    args = cli.build_parser().parse_args(["memory", "status"])

    def fail_config_path():
        raise RuntimeError("source checkout migration guard")

    def unavailable():
        raise internal_client.InternalServerUnavailable("socket unavailable")

    monkeypatch.setattr(cli.paths, "get_config_path", fail_config_path)
    monkeypatch.setattr(internal_client, "memory_status_sync", unavailable)

    assert cli.cmd_memory(args) == 1
    assert capsys.readouterr().err.strip() == "Memory status failed: memory_sidecar_unavailable"
