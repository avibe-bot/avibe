"""MCP Hub definition parsing: the page JSON and ``vibe mcp-add`` share one shape.

Contract (docs/plans/mcp-hub-mvp.md §4.3, §5): both entry points normalize to the
same ``definition.json``; unsupported transports, options, placeholders and
credential-bearing URLs fail with a field-specific usage error; parsing never
connects.
"""

import json

import pytest

from core.mcp_hub.definition import (
    masked_definition,
    merge_preserved_secrets,
    parse_add_argv,
    parse_provider_json,
)
from core.mcp_hub.errors import McpHubError


@pytest.mark.parametrize(
    ("argv", "server"),
    [
        (
            ["--transport", "http", "docs", "https://example.invalid/mcp", "--header", "Accept-Language: en"],
            {"type": "http", "url": "https://example.invalid/mcp", "headers": {"Accept-Language": "en"}},
        ),
        (
            ["playwright", "--", "npx", "-y", "@playwright/mcp@latest", "--isolated"],
            {"command": "npx", "args": ["-y", "@playwright/mcp@latest", "--isolated"]},
        ),
        (
            ["local-tools", "--env", "MODE=readonly", "B=2", "--", "python", "server.py"],
            {"command": "python", "args": ["server.py"], "env": {"MODE": "readonly", "B": "2"}},
        ),
        (
            ["--cwd", "/srv/tools", "tools", "--", "/bin/tool", "--flag=x"],
            {"command": "/bin/tool", "args": ["--flag=x"], "cwd": "/srv/tools"},
        ),
    ],
)
def test_add_argv_and_page_json_produce_identical_definitions(argv, server):
    from_cli, replace = parse_add_argv(argv)
    name = from_cli[0]["name"]
    from_page = parse_provider_json(json.dumps({"mcpServers": {name: server}}))
    assert replace is False
    assert from_cli == from_page


def test_provider_type_spellings_and_ignored_enabled():
    servers = {
        f"s{index}": {"type": kind, "url": "http://127.0.0.1:1/mcp", "enabled": False}
        for index, kind in enumerate(["http", "streamable-http", "streamable_http"])
    }
    servers["plain"] = {"url": "http://127.0.0.1:1/mcp"}
    servers["local"] = {"type": "stdio", "command": "srv"}
    parsed = {item["name"]: item["transport"]["type"] for item in parse_provider_json(json.dumps({"mcpServers": servers}))}
    assert parsed == {"local": "stdio", "plain": "streamable_http", "s0": "streamable_http",
                      "s1": "streamable_http", "s2": "streamable_http"}


@pytest.mark.parametrize(
    ("server", "error_type", "field"),
    [
        ({"type": "sse", "url": "http://127.0.0.1:1/sse"}, "unsupported_transport_sse", "type"),
        ({"command": "x", "url": "http://127.0.0.1:1/"}, "invalid_definition", "url"),
        ({"url": "https://user:pw@example.invalid/mcp"}, "invalid_definition", "url"),
        ({"url": "https://example.invalid/mcp", "headers": {"Authorization": "Bearer ${TOKEN}"}},
         "invalid_definition", "headers.Authorization"),
        ({"command": "x", "env": {"K": "${HOME:-/tmp}"}}, "invalid_definition", "env.K"),
        ({"command": "x", "cwd": "relative/dir"}, "invalid_definition", "cwd"),
        ({"url": "https://example.invalid/mcp", "args": ["a"]}, "invalid_definition", "args"),
    ],
)
def test_page_rejects_unsupported_input_with_field(server, error_type, field):
    with pytest.raises(McpHubError) as caught:
        parse_provider_json(json.dumps({"mcpServers": {"bad": server}}))
    assert (caught.value.type, caught.value.field, caught.value.usage) == (error_type, field, True)


def test_json_batch_keeps_valid_neighbors_when_one_entry_is_invalid():
    parsed = parse_provider_json(json.dumps({"mcpServers": {
        "ok": {"command": "srv"},
        "bad": {"type": "sse", "url": "http://127.0.0.1:1/sse"},
    }}))
    assert [item["name"] for item in parsed] == ["ok"]
    assert parsed.failures[0]["name"] == "bad"


@pytest.mark.parametrize("name", ["hidden.json", ".hidden.json"])
def test_hidden_registry_name_is_reserved(name):
    with pytest.raises(McpHubError) as caught:
        parse_add_argv([name, "--", "srv"])
    assert caught.value.type == "invalid_name"


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "a\x00b"])
def test_invalid_names_are_rejected(name):
    with pytest.raises(McpHubError) as caught:
        parse_add_argv([name, "--", "srv"])
    assert caught.value.type == "invalid_name"


@pytest.mark.parametrize("name", ["iphone_use", "node_repl", "Upper", "1abc", "con"])
def test_native_style_names_are_accepted(name):
    definitions, _replace = parse_add_argv([name, "--", "srv"])
    assert definitions[0]["name"] == name


@pytest.mark.parametrize(
    "argv",
    [
        ["--scope", "user", "x", "--", "srv"],
        ["--transport", "sse", "x", "http://127.0.0.1:1/sse"],
        ["--client-id", "abc", "--transport", "http", "x", "http://127.0.0.1:1/"],
    ],
)
def test_add_rejects_scope_sse_and_oauth_options(argv):
    with pytest.raises(McpHubError) as caught:
        parse_add_argv(argv)
    assert caught.value.usage


def test_json_stdin_after_command_separator_is_a_server_argument():
    definitions, _replace = parse_add_argv(["local", "--", "server", "--json-stdin"])
    assert definitions[0]["transport"]["command"] == "server"
    assert definitions[0]["transport"]["args"] == ["--json-stdin"]


def test_json_stdin_and_replace():
    text = json.dumps({"mcpServers": {"b": {"command": "srv"}, "a": {"url": "http://127.0.0.1:1/mcp"}}})
    definitions, replace = parse_add_argv(["--replace", "--json-stdin", text])
    assert [item["name"] for item in definitions] == ["a", "b"] and replace is True


def test_masking_hides_values_and_empty_submission_keeps_stored_secret():
    stored = parse_provider_json(json.dumps({"mcpServers": {"docs": {
        "url": "https://example.invalid/mcp", "headers": {"Authorization": "Bearer real", "X-Keep": "v"}}}}))[0]
    masked = masked_definition(stored)
    assert masked["transport"]["headers"] == {"Authorization": "", "X-Keep": ""}
    assert masked["transport"]["has_headers"] == {"Authorization": True, "X-Keep": True}
    assert "real" not in json.dumps(masked)

    # The page resubmits the whole map: blank keeps, a new value replaces, omission deletes.
    submitted = parse_provider_json(json.dumps({"mcpServers": {"docs": {
        "url": "https://example.invalid/mcp", "headers": {"Authorization": "", "X-New": "n"}}}}))[0]
    merged = merge_preserved_secrets(submitted, stored)
    assert merged["transport"]["headers"] == {"Authorization": "Bearer real", "X-New": "n"}
