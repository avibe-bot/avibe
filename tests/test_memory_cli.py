"""CLI schema contracts for ``vibe memory``."""

from __future__ import annotations

import builtins
import json
import shlex
from pathlib import Path

import pytest

from core.caller_context import AVIBE_SESSION_ID_ENV
from avibe_memory.types import MAX_AGENTIC_TIMEOUT_SECONDS, RecallPolicy
from core.prompt_registry import prompt_text
from vibe import cli, internal_client


_MEMORY_PROMPT = prompt_text("memory-context-prompt")


_MEMORY_HELP_BY_LANGUAGE = {
    "en": {
        "top": ("Use local Memory through the running controller",),
        "memory": (
            "Show Memory status",
            "Show the Memory profile",
            "List processed Memory episodes",
            "Search local Memory",
            "Submit personal context for best-effort capture",
        ),
        "status": ("Print machine-readable output",),
        "profile": ("Print machine-readable output",),
        "list": (
            "Page number (1-based)",
            "Episodes per page (1-100)",
            "Print machine-readable output",
        ),
        "search": (
            "Search query",
            "Maximum results (1-100)",
            "Recall mode (default: hybrid)",
            "Print machine-readable output",
        ),
        "remember": ("Text to remember", "Print machine-readable output"),
    },
    "zh": {
        "top": ("通过运行中的控制器使用本地记忆",),
        "memory": (
            "显示记忆状态",
            "显示记忆档案",
            "列出已处理的记忆片段",
            "搜索本地记忆",
            "提交个人信息供记忆系统尽力处理",
        ),
        "status": ("输出机器可读格式",),
        "profile": ("输出机器可读格式",),
        "list": ("页码（从 1 开始）", "每页记忆片段数（1-100）", "输出机器可读格式"),
        "search": ("搜索内容", "最大结果数（1-100）", "召回模式（默认：hybrid）", "输出机器可读格式"),
        "remember": ("要记住的文本", "输出机器可读格式"),
    },
}


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("arguments,section", [(["--help"], "top"), (["memory", "--help"], "memory")])
def test_memory_help_uses_configured_i18n(language, arguments, section, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_memory_cli_language", lambda: language)
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as raised:
        parser.parse_args(arguments)

    assert raised.value.code == 0
    output = capsys.readouterr().out
    for expected in _MEMORY_HELP_BY_LANGUAGE[language][section]:
        assert expected in output


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("command", ["status", "profile", "list", "search", "remember"])
def test_memory_subcommand_help_uses_configured_i18n(language, command, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_memory_cli_language", lambda: language)
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["memory", command, "--help"])

    assert raised.value.code == 0
    output = capsys.readouterr().out
    for expected in _MEMORY_HELP_BY_LANGUAGE[language][command]:
        assert expected in output


def test_memory_help_copy_is_not_hardcoded_in_cli_module() -> None:
    source = Path(cli.__file__).read_text(encoding="utf-8")

    for help_by_command in _MEMORY_HELP_BY_LANGUAGE["en"].values():
        for text in help_by_command:
            assert text not in source


def test_memory_search_json_is_a_presentation_of_the_uds_response(monkeypatch, capsys) -> None:
    args = cli.build_parser().parse_args(["memory", "search", "find this", "--limit", "3", "--json"])
    calls: list[tuple[str, int, str]] = []

    def search(query: str, limit: int, **kwargs):
        calls.append((query, limit, kwargs["mode"]))
        return {"status_code": 200, "body": {"status": "ok", "items": [{"kind": "fact", "text": "result"}]}}

    monkeypatch.setattr(internal_client, "memory_search_sync", search)

    assert cli.cmd_memory(args) == 0
    assert calls == [("find this", 3, "hybrid")]
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 1,
        "ok": True,
        "kind": "memory_search",
        "result": {"status": "ok", "items": [{"kind": "fact", "text": "result"}]},
    }


def test_memory_list_json_preserves_opaque_entry_id_and_page_contract(
    monkeypatch,
    capsys,
) -> None:
    """Scenario: MEMORY-LIST-001 and MEMORY-LIST-004."""

    args = cli.build_parser().parse_args(
        ["memory", "list", "--project", "notes", "--page", "2", "--limit", "5", "--json"]
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setenv(AVIBE_SESSION_ID_ENV, "ses-memory-list")

    def list_sync(**kwargs):
        calls.append(kwargs)
        return {
            "status_code": 200,
            "body": {
                "status": "ok",
                "items": [
                    {
                        "id": "opaque-entry-id",
                        "kind": "episode",
                        "subject": "Subject",
                        "summary": "Summary",
                        "body": "Body",
                        "timestamp": "2026-08-14T12:00:00Z",
                        "project": "notes",
                    }
                ],
                "page": 2,
                "page_size": 5,
                "count": 1,
                "total_count": 6,
                "warnings": [],
            },
        }

    monkeypatch.setattr(internal_client, "memory_list_sync", list_sync)

    assert cli.cmd_memory(args) == 0
    assert calls == [
        {
            "page": 2,
            "limit": 5,
            "project": "notes",
            "caller_session_id": "ses-memory-list",
        }
    ]
    output = json.loads(capsys.readouterr().out)
    assert output["kind"] == "memory_list"
    assert output["result"]["items"][0]["id"] == "opaque-entry-id"
    assert output["result"]["page"] == 2


@pytest.mark.parametrize(
    ("language", "warning"),
    [
        (
            "en",
            "Memory listing reached the provider ordering limit. Later pages may be incomplete.",
        ),
        ("zh", "记忆列表已达到服务排序上限，后续页面可能不完整。"),
    ],
)
def test_memory_list_human_surfaces_truncation_warning(
    language,
    warning,
    monkeypatch,
    capsys,
) -> None:
    args = cli.build_parser().parse_args(["memory", "list"])
    monkeypatch.setattr(cli, "_memory_cli_language", lambda: language)
    monkeypatch.setattr(
        internal_client,
        "memory_list_sync",
        lambda **_kwargs: {
            "status_code": 200,
            "body": {
                "status": "ok",
                "items": [
                    {
                        "id": "opaque-entry-id",
                        "kind": "episode",
                        "subject": "Subject",
                        "summary": "",
                        "body": "Body",
                        "timestamp": "2026-08-14T12:00:00Z",
                        "project": "default",
                    }
                ],
                "warnings": ["memory_list_truncated"],
            },
        },
    )

    assert cli.cmd_memory(args) == 0
    captured = capsys.readouterr()
    assert warning in captured.err
    assert "2026-08-14T12:00:00Z Subject" in captured.out


@pytest.mark.parametrize("operation", ["search", "profile"])
@pytest.mark.parametrize(
    ("language", "warning"),
    [
        (
            "en",
            "Some user, Agent, or project Memory sources could not be loaded. Results may be incomplete.",
        ),
        ("zh", "部分用户记忆、Agent 记忆或项目记忆来源未能加载，结果可能不完整。"),
    ],
)
def test_memory_read_human_surfaces_partial_warning(
    operation,
    language,
    warning,
    capsys,
) -> None:
    cli._print_memory_cli_human(
        operation,
        {
            "status": "ok",
            "items": [{"kind": "fact", "text": "Visible result", "date": None}],
            "warnings": ["memory_search_partial"],
        },
        language=language,
    )

    captured = capsys.readouterr()
    assert warning in captured.err
    assert "Visible result" in captured.out


def test_memory_list_parser_defaults_match_everos_page_semantics() -> None:
    args = cli.build_parser().parse_args(["memory", "list"])

    assert args.memory_command == "list"
    assert args.project is None
    assert args.page == 1
    assert args.limit == 20
    assert args.json is False


@pytest.mark.parametrize("filename", ["CLI.md", "CLI_ZH.md"])
def test_checked_in_memory_cli_references_match_everos_limits(filename: str) -> None:
    reference = (Path(__file__).parents[1] / "docs" / filename).read_text()
    memory_section = reference.split("### `vibe memory`", 1)[1].split("\n### ", 1)[0]

    assert memory_section.count("[--limit 1..100]") == 2
    assert "[--limit 1..20]" not in memory_section


@pytest.mark.parametrize(
    "arguments",
    [
        ["memory", "list", "--page", "0", "--json"],
        ["memory", "list", "--limit", "0", "--json"],
        ["memory", "list", "--limit", "101", "--json"],
    ],
)
def test_memory_list_rejects_invalid_bounds_before_transport(
    arguments,
    monkeypatch,
    capsys,
) -> None:
    args = cli.build_parser().parse_args(arguments)

    def transport_must_not_run(**_kwargs):
        raise AssertionError("invalid list input reached the controller")

    monkeypatch.setattr(internal_client, "memory_list_sync", transport_must_not_run)

    assert cli.cmd_memory(args) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "memory_invalid_input"


def test_memory_list_accepts_everos_max_page_size(monkeypatch, capsys) -> None:
    args = cli.build_parser().parse_args(
        ["memory", "list", "--limit", "100", "--json"]
    )
    calls: list[int] = []

    def list_sync(**kwargs):
        calls.append(kwargs["limit"])
        return {"status_code": 200, "body": {"status": "ok", "items": []}}

    monkeypatch.setattr(internal_client, "memory_list_sync", list_sync)

    assert cli.cmd_memory(args) == 0
    assert calls == [100]
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_memory_list_all_rejection_comes_from_controller(monkeypatch, capsys) -> None:
    """Scenario: MEMORY-LIST-003."""

    args = cli.build_parser().parse_args(
        ["memory", "list", "--project", "all", "--json"]
    )
    calls: list[str | None] = []

    def list_sync(**kwargs):
        calls.append(kwargs["project"])
        return {
            "status_code": 400,
            "body": {"status": "failed", "error": "memory_invalid_input"},
        }

    monkeypatch.setattr(internal_client, "memory_list_sync", list_sync)

    assert cli.cmd_memory(args) == 1
    assert calls == ["all"]
    assert json.loads(capsys.readouterr().out)["code"] == "memory_invalid_input"


def test_memory_list_is_not_added_to_the_injected_personal_memory_prompt() -> None:
    assert "vibe memory list" not in _MEMORY_PROMPT


def test_injected_agentic_memory_example_is_accepted_by_the_live_parser() -> None:
    example = 'vibe memory search "<query>" --mode agentic --json'
    assert f"`{example}`" in _MEMORY_PROMPT

    args = cli.build_parser().parse_args(shlex.split(example)[1:])

    assert args.memory_command == "search"
    assert args.query == "<query>"
    assert args.mode == "agentic"
    assert args.limit == 8


def test_agentic_cli_builds_bounded_internal_recall_policy(
    monkeypatch,
    capsys,
) -> None:
    """Scenario: MEMORY-SEARCH-007."""

    calls: list[tuple[str, str, dict[str, object]]] = []

    def request_sync(method, path, *, payload, **_kwargs):
        calls.append((method, path, payload))
        return {
            "status_code": 200,
            "body": {"status": "ok", "items": [], "warnings": []},
        }

    monkeypatch.setattr(internal_client, "_memory_request_sync", request_sync)
    args = cli.build_parser().parse_args(
        ["memory", "search", "connect the clues", "--mode", "agentic", "--json"]
    )

    assert cli.cmd_memory(args) == 0
    assert len(calls) == 1
    method, path, payload = calls[0]
    assert method == "POST"
    assert path == "/internal/memory/search"
    assert payload["query"] == "connect the clues"
    policy = RecallPolicy.from_payload(payload["policy"])
    assert policy == RecallPolicy(
        mode="agentic",
        max_results=8,
        include_profile=True,
        include_current_session=False,
        timeout_seconds=MAX_AGENTIC_TIMEOUT_SECONDS,
        max_model_calls=2,
        cost_budget_tokens=32_000,
    )
    assert json.loads(capsys.readouterr().out)["ok"] is True


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


def test_memory_status_does_not_import_optional_implementation(monkeypatch, capsys) -> None:
    args = cli.build_parser().parse_args(["memory", "status", "--json"])
    original_import = builtins.__import__

    def core_only_import(name, *args, **kwargs):
        if name == "avibe_memory" or name.startswith("avibe_memory."):
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", core_only_import)
    monkeypatch.setattr(
        internal_client,
        "memory_status_sync",
        lambda **_kwargs: {
            "status_code": 503,
            "body": {
                "status": "failed",
                "error": "memory_implementation_unavailable",
            },
        },
    )

    assert cli.cmd_memory(args) == 1
    assert json.loads(capsys.readouterr().out)["code"] == (
        "memory_implementation_unavailable"
    )


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
    args = cli.build_parser().parse_args(["memory", "search", "query", "--limit", "101", "--json"])

    def transport_must_not_run(*_args, **_kwargs):
        raise AssertionError("invalid CLI input reached the UDS")

    monkeypatch.setattr(internal_client, "memory_search_sync", transport_must_not_run)

    assert cli.cmd_memory(args) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "memory_invalid_input"


def test_memory_cli_accepts_everos_max_results_and_large_query(
    monkeypatch,
    capsys,
) -> None:
    query = "find this detail " * 700
    args = cli.build_parser().parse_args(
        ["memory", "search", query, "--limit", "100", "--json"]
    )
    calls: list[tuple[str, int]] = []

    def search_sync(text, limit, **_kwargs):
        calls.append((text, limit))
        return {"status_code": 200, "body": {"status": "ok", "items": []}}

    monkeypatch.setattr(internal_client, "memory_search_sync", search_sync)

    assert len(query.encode()) > 8 * 1024
    assert cli.cmd_memory(args) == 0
    assert calls == [(query.strip(), 100)]
    assert json.loads(capsys.readouterr().out)["ok"] is True


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
                "state": "degraded",
                "reason": "memory_sidecar_unavailable",
                "health": {
                    "status": "ok",
                    "version": "1.2.3",
                    "capabilities": {},
                    "disabled_features": [],
                    "cascade": {},
                    "recorder": {},
                },
                "attachment_capture": {"status": "ready"},
            },
        },
    )

    assert cli.cmd_memory(args) == 0
    assert capsys.readouterr().out.splitlines() == [
        "记忆：已降级",
        "EverOS 1.2.3：正常",
        "IM 附件捕获：可用",
        "来源原因：记忆 sidecar 不可用",
    ]


def test_memory_cli_human_labels_origins_and_preserves_legacy_items(capsys) -> None:
    cli._print_memory_cli_human(
        "search",
        {
            "items": [
                {"kind": "fact", "text": "Direct", "date": "2026-08-21", "origin": "user"},
                {"kind": "fact", "text": "Recorded", "date": None, "origin": "agent"},
                {"kind": "fact", "text": "Shared", "date": None, "origin": "both"},
                {"kind": "fact", "text": "Legacy", "date": None},
            ]
        },
        language="en",
    )

    assert capsys.readouterr().out.splitlines() == [
        "[User memory] 2026-08-21 Direct",
        "[Agent memory] Recorded",
        "[User + Agent] Shared",
        "Legacy",
    ]


def test_memory_prompt_explains_owner_labels_and_profile_separation() -> None:
    assert "label `origin` as `user`, `agent`, or `both`" in _MEMORY_PROMPT
    assert "never merge them into one attributed profile" in _MEMORY_PROMPT


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
                "state": "future_state",
                "reason": "future_reason",
                "health": {"status": "future_health"},
            },
        },
    )

    assert cli.cmd_memory(args) == 0
    output = capsys.readouterr().out.splitlines()
    assert output == [
        "记忆：未知",
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
    assert capsys.readouterr().err.strip() == "Memory status failed: Memory sidecar unavailable"


@pytest.mark.parametrize(
    ("code", "label"),
    [
        ("memory_implementation_unavailable", "Memory implementation unavailable"),
        ("memory_implementation_incompatible", "Memory implementation is incompatible"),
    ],
)
def test_memory_cli_implementation_failure_localizes_human_error_without_changing_json_code(
    monkeypatch,
    capsys,
    code,
    label,
) -> None:
    args = cli.build_parser().parse_args(["memory", "status"])
    monkeypatch.setattr(cli, "_memory_cli_language", lambda: "en")
    monkeypatch.setattr(
        internal_client,
        "memory_status_sync",
        lambda **_kwargs: {
            "status_code": 503,
            "body": {"status": "failed", "error": code},
        },
    )

    assert cli.cmd_memory(args) == 1
    error = capsys.readouterr().err.strip()
    assert label in error
    assert code not in error


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
    ("language", "expected"),
    [
        (
            "en",
            "Memory request acknowledged for best-effort processing; persistence is not guaranteed.",
        ),
        ("zh", "记忆请求已确认；系统将尽力处理，但不保证持久保存。"),
    ],
)
@pytest.mark.parametrize("outcome", ["accepted", "duplicate"])
def test_memory_remember_human_output_never_confirms_persistence(
    monkeypatch,
    capsys,
    language,
    expected,
    outcome,
) -> None:
    args = cli.build_parser().parse_args(["memory", "remember", "keep this"])
    monkeypatch.setattr(cli, "_memory_cli_language", lambda: language)
    monkeypatch.setattr(
        internal_client,
        "memory_remember_sync",
        lambda text, **_kwargs: {"status_code": 200, "body": {"status": outcome}},
    )

    assert cli.cmd_memory(args) == 0
    assert capsys.readouterr().out.strip() == expected


def test_top_level_memory_guides_disclose_best_effort_process_local_delivery() -> None:
    docs_root = Path(__file__).parents[1] / "docs"
    guides = {
        path
        for path in docs_root.glob("*.md")
        if "vibe memory" in path.read_text(encoding="utf-8")
    }

    assert guides
    for guide in guides:
        text = guide.read_text(encoding="utf-8")
        if guide.stem.endswith("_ZH"):
            assert "尽力" in text
            assert "进程内" in text
            assert "不保证" in text
        else:
            lowered = text.lower()
            assert "best-effort" in lowered
            assert "process-local" in lowered
            assert "not guarantee" in lowered


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


def test_memory_remember_accepts_text_over_legacy_cli_limit(monkeypatch, capsys) -> None:
    text = "x" * 4_001
    args = cli.build_parser().parse_args(["memory", "remember", text, "--json"])
    calls: list[str] = []

    def remember_sync(value, **_kwargs):
        calls.append(value)
        return {"status_code": 200, "body": {"status": "accepted"}}

    monkeypatch.setattr(internal_client, "memory_remember_sync", remember_sync)

    assert cli.cmd_memory(args) == 0
    assert calls == [text]
    assert json.loads(capsys.readouterr().out)["ok"] is True
