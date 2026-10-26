"""Schema -> flag contracts for the shared ``vibe mcp`` runner (plan §4.2).

Exact keys, no aliases or inserted defaults; one input mode per call; schemas the
validator cannot handle publish as JSON-input-only instead of failing generation.
"""

import pytest

from core.mcp_hub.commands import build_command, parse_tool_argv, validate_arguments
from core.mcp_hub.errors import McpHubError

TOOL = {
    "name": "search",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "ratio": {"type": "number"},
            "exact": {"type": "boolean"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "filter": {"type": "object"},
            "maybe": {"type": ["string", "null"]},
            "choice": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            "bad key": {"type": "string"},
            "help": {"type": "string"},
            "camelCase": {"type": "string"},
        },
        "required": ["query"],
    },
}


def test_flag_table():
    command = build_command(TOOL)
    assert {flag["key"]: flag["kind"] for flag in command["flags"]} == {
        "query": "string", "limit": "integer", "ratio": "number", "exact": "boolean",
        "tags": "array", "filter": "object", "camelCase": "string",
    }
    assert sorted(command["jsonOnlyKeys"]) == ["bad key", "choice", "help", "maybe"]
    assert command["validated"] is True


def test_flags_parse_exact_keys_and_strict_values():
    command = build_command(TOOL)
    arguments = parse_tool_argv(command, [
        "--query", "async Python", "--limit=5", "--ratio", "0.5", "--exact", "false",
        "--tags", '["a"]', "--filter", "{}", "--camelCase", "x",
    ])
    assert arguments == {"query": "async Python", "limit": 5, "ratio": 0.5, "exact": False,
                         "tags": ["a"], "filter": {}, "camelCase": "x"}
    assert parse_tool_argv(command, []) == {}


@pytest.mark.parametrize(
    "argv",
    [
        ["--limit", "5.5"],
        ["--limit", "true"],
        ["--ratio", "NaNx"],
        ["--exact", "yes"],
        ["--tags", "{}"],
        ["--camel-case", "x"],
        ["--query", "a", "--query", "b"],
        ["--query", "a", "--args-json", "{}"],
        ["--args-json", "{}", "--args-json", "{}"],
        ["--args-json", "[]"],
        ["positional"],
    ],
)
def test_usage_errors(argv):
    with pytest.raises(McpHubError) as caught:
        parse_tool_argv(build_command(TOOL), argv)
    assert caught.value.usage


def test_json_input_modes_reach_flagless_keys():
    command = build_command(TOOL)
    assert parse_tool_argv(command, ["--args-json", '{"choice": 3, "help": "x"}']) == {"choice": 3, "help": "x"}
    assert parse_tool_argv(command, ["--args-stdin"], read_stdin=lambda: '{"maybe": null}') == {"maybe": None}
    assert parse_tool_argv(command, ["--args-file", "p"], read_file=lambda path: '{"query": "q"}') == {"query": "q"}


def test_validation_uses_the_published_schema():
    validate_arguments(TOOL, {"query": "q", "choice": 1})
    with pytest.raises(McpHubError) as caught:
        validate_arguments(TOOL, {"limit": 1})
    assert caught.value.type == "invalid_arguments"


def test_unhandleable_schema_is_json_only_and_unvalidated():
    remote = {"name": "remote", "inputSchema": {"$ref": "https://example.invalid/schema.json"}}
    nested = {
        "name": "nested",
        "inputSchema": {
            "type": "object",
            "properties": {"filter": {"$ref": "https://example.invalid/filter.json"}},
        },
    }
    invalid = {"name": "invalid", "inputSchema": {"type": "object", "properties": {"a": {"type": 5}}}}
    for tool in (remote, nested, invalid):
        command = build_command(tool)
        assert command["validated"] is False and command["flags"] == []
        validate_arguments(tool, {"anything": 1})  # not validated, not a failure
