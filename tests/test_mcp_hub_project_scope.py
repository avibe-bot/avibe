"""MCP Hub project scope contracts (docs/plans/mcp-hub-project-scope.md).

``core.mcp_hub.scope`` is imported inside the tests that exercise it, so the
runtime, CLI and prompt contracts also run against code that predates it.
"""

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

from core.mcp_hub import repository
from core.mcp_hub import runtime as mcp_runtime
from core.mcp_hub.runtime import McpRuntime
from tests.ui_server_test_helpers import save_config

FIXTURE = str(Path(__file__).parent / "fixtures" / "mcp_echo_server.py")
ECHO = {"command": sys.executable, "args": [FIXTURE]}


def _run(runtime, operation, payload):
    status, body = asyncio.run(runtime.submit(operation, payload))
    assert status == 200
    return json.loads(body)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from vibe import internal_client, mcp_cli

    save_config(tmp_path)
    owner = McpRuntime()
    monkeypatch.setattr(internal_client, "mcp_request_sync", lambda operation, payload, **_: _run(owner, operation, payload))
    monkeypatch.setattr(mcp_cli, "_admit", lambda _floor: None)
    yield owner
    owner.stop()


@pytest.fixture
def global_echo(runtime):
    assert _run(runtime, "add", {"servers": [{"name": "echo", "server": ECHO}]})["ok"]
    return runtime


def _project(path: Path, servers: dict, **extra) -> Path:
    (path / ".git").mkdir(parents=True)
    (path / "src").mkdir()
    (path / ".mcp.json").write_text(json.dumps({"mcpServers": servers, **extra}))
    return path


def _key(root: Path, name: str):
    from core.mcp_hub import scope

    return scope.ScopedName(scope.project_scope(root), name)


# ---------------------------------------------------------------------------
# D1: one project root with managed Skills
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("git", "cwd", "base", "expected"),
    [
        pytest.param("a", "a/b", ".", ".", id="project_base"),
        pytest.param("a", "a/b", None, "a", id="git"),
        pytest.param(None, "a/b", None, "a/b", id="plain-cwd"),
    ],
)
def test_project_root_is_the_managed_skills_root(tmp_path, git, cwd, base, expected):
    from core.managed_skills import _project_directories
    from core.mcp_hub import scope

    (tmp_path / cwd).mkdir(parents=True)
    if git:
        (tmp_path / git / ".git").mkdir()
    project_base = tmp_path / base if base else None
    resolved = scope.project_scope(tmp_path / cwd, project_base, require_config=False)
    assert resolved.project_root == (tmp_path / expected).resolve()
    assert resolved.project_root == _project_directories(tmp_path / cwd, project_base=project_base)[-1]
    assert resolved.key.startswith("project:dir-")
    assert scope.project_scope(tmp_path / cwd, project_base) is None  # no .mcp.json at that root


def test_project_id_names_the_cache_for_project_folders(tmp_path):
    from storage import projects_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state

    root = _project(tmp_path / "repo", {"echo": ECHO})
    ensure_sqlite_state()
    with create_sqlite_engine().begin() as conn:
        project_id = projects_service.create_project(conn, str(root))["id"]
    assert _key(root / "src", "echo").scope.key == f"project:{project_id}"


# ---------------------------------------------------------------------------
# D3/D4: effective definition, placeholders and approval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "secrets", "expected"),
    [
        pytest.param(
            {"url": "https://h.example/mcp", "headers": {"X-Token": "raw", "x-team": "repo"}},
            {"headers": {"x-token": "tok-secret"}},
            {"state": "ok", "headers": {"x-token": "tok-secret", "x-team": "repo"}},
            id="cached-header-overrides-case-insensitively",
        ),
        pytest.param(
            {"url": "https://${HOST}/mcp", "headers": {"Authorization": "Bearer ${TOKEN}"}},
            {"env": {"HOST": "h.example", "UNUSED": "tok-secret"}},
            {"state": "needs_config", "missing": ["TOKEN"], "placeholders": ["HOST", "TOKEN"]},
            id="missing-variable-needs-config",
        ),
        pytest.param(
            {"url": "https://${HOST}/mcp", "headers": {"Authorization": "Bearer ${TOKEN}"}},
            {"env": {"HOST": "h.example", "TOKEN": "tok-secret"}},
            {"state": "ok", "url": "https://h.example/mcp", "headers": {"Authorization": "Bearer tok-secret"}},
            id="one-namespace-fills-url-and-headers",
        ),
        pytest.param({"command": "${BIN}"}, {}, {"error": "invalid_definition"}, id="placeholder-in-command"),
        pytest.param({**ECHO, "args": ["${SCRIPT}"]}, {}, {"error": "invalid_definition"}, id="placeholder-in-args"),
        pytest.param(
            {"url": "${MCP_URL}"}, {}, {"state": "needs_config", "missing": ["MCP_URL"]}, id="whole-url-missing"
        ),
        pytest.param(
            {"url": "${SCHEME}://example.invalid/mcp"}, {}, {"state": "needs_config", "missing": ["SCHEME"]},
            id="url-scheme-missing",
        ),
        pytest.param({"url": "https://${HOST}/mcp"}, {}, {"state": "needs_config", "missing": ["HOST"]}, id="url-host-missing"),
        pytest.param(
            {"url": "${MCP_URL}"}, {"env": {"MCP_URL": "https://h.example/mcp"}},
            {"state": "ok", "url": "https://h.example/mcp", "headers": {}}, id="whole-url-resolved",
        ),
        pytest.param(
            {"url": "${MCP_URL}"}, {"env": {"MCP_URL": "http://[oops"}}, {"error": "invalid_definition"},
            id="resolved-url-invalid",
        ),
        pytest.param(
            {"url": "${MCP_URL}", "headers": 42}, {}, {"error": "invalid_definition"}, id="bad-field-beside-missing-url"
        ),
        pytest.param(
            {"url": "${MCP_URL}", "type": []}, {}, {"error": "invalid_definition"}, id="bad-type-beside-missing-url"
        ),
        pytest.param(
            {"url": 42, "headers": {"A": "${T}"}}, {}, {"error": "invalid_definition"}, id="non-string-url-beside-missing"
        ),
    ],
)
def test_effective_definition(tmp_path, raw, secrets, expected):
    from core.mcp_hub import scope
    from core.mcp_hub.errors import McpHubError

    root = _project(tmp_path / "repo", {"srv": raw})
    key = _key(root, "srv")
    stored = repository.package_dir(key) / "secrets.json"
    if "error" in expected:
        with pytest.raises(McpHubError) as caught:
            scope.configure(key, secrets)
        assert caught.value.type == expected["error"]
        assert not stored.exists()
        return
    scope.approve(key)
    scope.configure(key, secrets)
    server = scope.load_server(key)
    assert server.state == expected["state"]
    assert server.missing == expected.get("missing", [])
    assert json.loads(stored.read_text()) == secrets  # stand-ins are never stored
    if server.state == "needs_config":  # nor returned or published
        assert server.definition is None
        assert not (repository.package_dir(key) / "current.json").exists()
    if "placeholders" in expected:
        assert server.placeholders == expected["placeholders"]
    if "headers" in expected:
        assert server.definition["transport"]["headers"] == expected["headers"]
        assert server.definition["transport"]["url"] == expected.get("url", raw["url"])
    summary = server.summary()
    assert summary["url"] == raw["url"] and "tok-secret" not in json.dumps(summary)
    assert oct((repository.package_dir(key) / "secrets.json").stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param("rotate-secret", "ok", id="rotating-a-secret-keeps-approval"),
        pytest.param("edit-raw", "pending_approval", id="editing-the-entry-needs-reapproval"),
    ],
)
def test_approval_binds_the_raw_entry(tmp_path, change, expected):
    from core.mcp_hub import scope

    raw = {"url": "https://${HOST}/mcp"}
    root = _project(tmp_path / "repo", {"remote": raw})
    key = _key(root, "remote")
    assert scope.load_server(key).state == "pending_approval"
    scope.approve(key)
    scope.configure(key, {"env": {"HOST": "one.example"}})
    if change == "rotate-secret":
        scope.configure(key, {"env": {"HOST": "two.example"}})
    else:
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"remote": {"url": "https://${HOST}/v2"}}}))
    assert scope.load_server(key).state == expected


# ---------------------------------------------------------------------------
# Runtime: visibility, shadowing and isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["call", "refresh"])
@pytest.mark.parametrize(
    ("entry", "error"),
    [
        pytest.param(ECHO, "pending_approval", id="unapproved"),
        pytest.param({"command": "${BIN}"}, "not_found", id="invalid-entry-still-shadows"),
        pytest.param({"url": "${MCP_URL}"}, "needs_config", id="approved-url-stand-in-never-connects"),
    ],
)
def test_blocked_project_server_never_opens_a_transport(tmp_path, monkeypatch, global_echo, operation, entry, error):
    root = _project(tmp_path / "repo", {"echo": entry})
    opened = []
    enter_client = mcp_runtime._enter_client

    async def recording(definition, *args, **kwargs):
        opened.append(definition["name"])
        return await enter_client(definition, *args, **kwargs)

    monkeypatch.setattr(mcp_runtime, "_enter_client", recording)
    payload = {"name": "echo", "workingDirectory": str(root / "src"), "sessionId": "s"}
    if error == "needs_config":
        assert _run(global_echo, "approve", payload)["state"] == "needs_config"
    if operation == "call":
        payload.update(tool="echo", arguments={"text": "x"})
    result = _run(global_echo, operation, payload)
    assert result["error"]["type"] == error
    assert opened == []
    assert global_echo._connections == {}


def test_project_and_global_runtime_identities_stay_isolated(tmp_path, global_echo):
    runtime = global_echo
    root = _project(tmp_path / "repo", {"echo": {**ECHO, "env": {"T": "${T}"}}})
    payload = {"name": "echo", "workingDirectory": str(root)}
    assert _run(runtime, "approve", payload)["state"] == "needs_config"
    assert _run(runtime, "configure", {**payload, "secrets": {"env": {"T": "x"}}})["toolCount"] == 7

    for item in ({**payload, "sessionId": "s"}, {"name": "echo", "sessionId": "s"}):
        assert _run(runtime, "call", {**item, "tool": "echo", "arguments": {"text": "ok"}})["status"] == "ok"
    assert {key[0].scope.key for key in runtime._connections} == {"global", _key(root, "echo").scope.key}

    edited = {"echo": {**ECHO, "args": [FIXTURE, "stdio"]}}
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": edited}))
    stale = _run(runtime, "call", {**payload, "tool": "echo", "arguments": {"text": "x"}})
    assert stale["error"]["type"] == "pending_approval"


def test_project_add_writes_back_atomically_and_keeps_secrets_private(tmp_path, runtime):
    from storage import projects_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state

    root = _project(tmp_path / "repo", {"keep": {"command": "x", "timeout": 3}}, team="kept")
    data = json.loads((root / ".mcp.json").read_text())
    team = {"custom": True, "cwd": str(root), "env": {"MODE": "team", "KEY": "${KEY}"}}
    data["mcpServers"]["echo"] = {"command": "old", "args": ["x"], **team}
    (root / ".mcp.json").write_text(json.dumps(data))
    ensure_sqlite_state()
    with create_sqlite_engine().begin() as conn:
        project_id = projects_service.create_project(conn, str(root))["id"]
    server = {**ECHO, "env": {"TOKEN": "s3cret"}}
    result = _run(runtime, "add", {"project": project_id, "replace": True, "servers": [{"name": "echo", "server": server}]})
    assert result["added"][0]["state"] == "pending_approval"
    written = json.loads((root / ".mcp.json").read_text())
    assert written["team"] == "kept" and written["mcpServers"]["keep"] == {"command": "x", "timeout": 3}
    assert written["mcpServers"]["echo"] == {**ECHO, **team}
    assert "s3cret" not in (root / ".mcp.json").read_text()
    team_file = (root / ".mcp.json").read_bytes()
    scope_payload = {"project": project_id, "name": "echo"}
    _run(runtime, "approve", scope_payload)
    assert _run(runtime, "configure", {**scope_payload, "secrets": {"env": {"KEY": "k"}}})["toolCount"] == 7
    assert (root / ".mcp.json").read_bytes() == team_file

    removed = _run(runtime, "remove", {"project": project_id, "name": "echo"})
    assert removed["kind"] == "project"
    assert "echo" not in json.loads((root / ".mcp.json").read_text())["mcpServers"]


def test_concurrent_writes_to_one_project_keep_every_server(tmp_path, monkeypatch):
    """Per-server locks do not cover the shared ``.mcp.json``; both checks pass, then both replace."""
    import threading

    from core.mcp_hub import scope

    root = _project(tmp_path / "repo", {})
    both_checked = threading.Barrier(2, timeout=1)
    write_atomic = scope.write_atomic

    def checked_then_wait(path, text, *, before_replace, **kwargs):
        def check():
            before_replace()
            try:
                both_checked.wait()
            except threading.BrokenBarrierError:
                pass

        write_atomic(path, text, before_replace=check, **kwargs)

    monkeypatch.setattr(scope, "write_atomic", checked_then_wait)
    errors = []

    def write(name):
        try:
            scope.write_server(_key(root, name), ECHO, replace=False)
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(name,)) for name in ("alpha", "beta")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(json.loads((root / ".mcp.json").read_text())["mcpServers"]) == ["alpha", "beta"]


def test_a_project_without_a_folder_is_a_typed_error(runtime, monkeypatch):
    from core.mcp_hub import scope

    monkeypatch.setattr(scope, "_project_workdir", lambda _project_id: "")
    for operation, payload in (("list", {}), ("add", {"servers": [{"name": "echo", "server": ECHO}]})):
        status, body = asyncio.run(runtime.submit(operation, {"project": "proj_nofolder", **payload}))
        assert status == 200  # a typed error; the HTTP layer maps ``usage`` to 400
        assert json.loads(body)["error"]["type"] == "usage"
    assert repository.list_names() == []


@pytest.mark.parametrize(
    ("entry", "persisted", "rejected"),
    [
        pytest.param({**ECHO, "env": {"T": "${T}"}}, None, {"env": {"BAD KEY": "fake"}}, id="stdio-bad-key"),
        pytest.param({**ECHO, "env": {"T": "${T}"}}, None, {"env": None}, id="stdio-null-env"),
        pytest.param({"url": "https://${T}/${P}"}, None, {"headers": None}, id="http-null-headers"),
        pytest.param({"url": "https://${T}/${P}"}, {"headers": None}, None, id="repair-persisted-null"),
    ],
)
def test_rejected_configure_keeps_the_cache_and_allows_correction(tmp_path, runtime, entry, persisted, rejected):
    root = _project(tmp_path / "repo", {"echo": entry})
    payload = {"name": "echo", "workingDirectory": str(root)}
    assert _run(runtime, "approve", payload)["state"] == "needs_config"
    assert _run(runtime, "configure", {**payload, "secrets": {"env": {"OTHER": "1"}}})["state"] == "needs_config"
    secrets = repository.package_dir(_key(root, "echo")) / "secrets.json"
    if persisted is not None:
        secrets.write_text(json.dumps(persisted))
        assert [row["name"] for row in _run(runtime, "list", {"workingDirectory": str(root)})["servers"]] == []
    before = secrets.read_bytes()

    if rejected is not None:
        result = _run(runtime, "configure", {**payload, "secrets": rejected})
        assert secrets.read_bytes() == before
        assert result["error"]["type"] == "invalid_definition"
    corrected = _run(runtime, "configure", {**payload, "secrets": {"env": {"T": "x"}}})
    if "command" in entry:
        assert corrected["toolCount"] == 7
    else:  # ${P} keeps it offline; the repaired file is what matters
        assert corrected["state"] == "needs_config"
        assert json.loads(secrets.read_text()) == {"env": {"T": "x"}}


def _snapshot_file(root: Path) -> Path:
    directory = repository.package_dir(_key(root, "echo"))
    return directory / "current.json"


def _corrupt_current(root: Path) -> Path:
    current = _snapshot_file(root)
    current.write_text("{corrupt")
    return current


def _published(file: str, change):
    """Rewrite one file of the active revision of the project's ``echo``."""

    def corrupt(root: Path) -> Path:
        current = _snapshot_file(root)
        path = current.parent / "revisions" / json.loads(current.read_text())["revision"] / file
        path.write_text(json.dumps(change(json.loads(path.read_text()))))
        return path

    return corrupt


def _manifest(**fields):
    return _published("manifest.json", lambda manifest: {**manifest, **fields})


def _first(file: str, change):
    return _published(file, lambda items: [change(items[0]), *items[1:]])


def _flags(**fields):
    return _published(
        "commands.json",
        lambda commands: [{**command, "flags": [{**flag, **fields} for flag in command["flags"]]} for command in commands],
    )


def _entry(**fields):
    def corrupt(root: Path) -> Path:
        document = root / ".mcp.json"
        data = json.loads(document.read_text())
        data["mcpServers"]["echo"] = fields
        document.write_text(json.dumps(data))
        return document

    return corrupt


@pytest.mark.parametrize(
    ("corrupt", "error"),
    [
        pytest.param(_corrupt_current, "invalid_package", id="corrupt-current-json"),
        pytest.param(_published("tools.json", lambda tools: [None]), "invalid_package", id="null-published-tool"),
        pytest.param(_manifest(description=42), "invalid_package", id="manifest-description-not-text"),
        pytest.param(_manifest(auth=42), "invalid_package", id="manifest-auth-not-text"),
        pytest.param(_manifest(generatedAt=42), "invalid_package", id="manifest-generated-at-not-text"),
        pytest.param(_manifest(diagnostics="x"), "invalid_package", id="manifest-diagnostics-not-list"),
        pytest.param(_manifest(serverInfo=42), "invalid_package", id="manifest-server-info-not-object"),
        pytest.param(_manifest(serverInfo={"version": 1}), "invalid_package", id="manifest-version-not-text"),
        pytest.param(_first("tools.json", lambda tool: {**tool, "description": 42}), "invalid_package", id="tool-description"),
        pytest.param(
            _first("commands.json", lambda command: {k: v for k, v in command.items() if k != "description"}),
            "invalid_package", id="command-without-description",
        ),
        pytest.param(_first("commands.json", lambda command: {**command, "validated": "yes"}), "invalid_package",
                     id="command-validated-not-bool"),
        pytest.param(_first("commands.json", lambda command: {**command, "flags": None}), "invalid_package",
                     id="command-flags-not-list"),
        pytest.param(_first("commands.json", lambda command: {**command, "jsonOnlyKeys": [1]}), "invalid_package",
                     id="command-json-only-keys-not-text"),
        pytest.param(_flags(kind=[]), "invalid_package", id="flag-kind-not-text"),
        pytest.param(_flags(kind="date"), "invalid_package", id="flag-kind-unknown"),
        pytest.param(_flags(required="no"), "invalid_package", id="flag-required-not-bool"),
        pytest.param(
            _entry(command="echo", args=42, env={"TOKEN": "${TOKEN}"}), "invalid_definition",
            id="malformed-entry-with-missing-secret",
        ),
        pytest.param(_entry(**ECHO, type=[]), "invalid_definition", id="non-string-type"),
    ],
)
def test_a_corrupt_project_server_hides_only_that_server(tmp_path, monkeypatch, capsys, global_echo, corrupt, error):
    """One owner per persisted input turns bad data into a per-entry error, never a catalog failure."""

    from core.system_prompt_injection import build_system_prompt_blocks
    from vibe import mcp_cli

    assert _run(global_echo, "add", {"servers": [{"name": "other", "server": ECHO}]})["ok"]
    root = _project(tmp_path / "repo", {"echo": ECHO, "sibling": ECHO})
    for name in ("echo", "sibling"):
        assert _run(global_echo, "approve", {"name": name, "workingDirectory": str(root)})["toolCount"] == 7
    corrupted = corrupt(root)
    before = corrupted.read_bytes()
    from core.mcp_hub import scope
    from core.mcp_hub.errors import McpHubError

    with pytest.raises(McpHubError) as caught:
        scope.load_server(_key(root, "echo")).summary()
    assert caught.value.type == error

    listed = _run(global_echo, "list", {"workingDirectory": str(root)})["servers"]
    project = _key(root, "sibling").scope.key
    assert sorted((row["name"], row["scope"]) for row in listed) == [("other", "global"), ("sibling", project)]
    prompt = "".join(block.text for block in build_system_prompt_blocks(backend="claude", skills_cwd=str(root)))
    monkeypatch.setenv("AVIBE_SKILL_WORKING_DIR", str(root))
    capsys.readouterr()
    assert mcp_cli.main(["--list"]) == 0
    cli = capsys.readouterr().out
    for text in (prompt, cli):
        assert sorted(re.findall(r"^- (echo|other|sibling)\b", text, flags=re.MULTILINE)) == ["other", "sibling"]
    assert corrupted.read_bytes() == before


def test_a_named_call_reads_only_its_own_package(tmp_path, monkeypatch, global_echo):
    from core.mcp_hub import native

    assert _run(global_echo, "add", {"servers": [{"name": "other", "server": ECHO}]})["ok"]
    root = _project(tmp_path / "repo", {"mine": ECHO, "sibling": ECHO})
    for name in ("mine", "sibling"):
        _run(global_echo, "approve", {"name": name, "workingDirectory": str(root)})
    read = []
    for attribute in ("_load_revision", "read_published"):
        original = getattr(repository, attribute)
        monkeypatch.setattr(
            repository, attribute, lambda name, *args, _f=original: read.append(str(name)) or _f(name, *args)
        )
    native_reads = []
    read_all = native.read_all
    monkeypatch.setattr(native, "read_all", lambda *args: native_reads.append(1) or read_all(*args))

    for name, cwd in (("echo", tmp_path), ("mine", root)):
        read.clear()
        call = {"name": name, "workingDirectory": str(cwd), "sessionId": "s", "tool": "echo", "arguments": {"text": "x"}}
        assert _run(global_echo, "call", call)["status"] == "ok"
        assert set(read) == {name}
    native_reads.clear()
    assert len(_run(global_echo, "list", {"workingDirectory": str(root)})["servers"]) == 4
    assert len(native_reads) == 1


def test_reads_never_touch_the_project_cache(tmp_path, global_echo):
    from config import paths
    from core.system_prompt_injection import build_system_prompt_blocks

    root = _project(tmp_path / "repo", {"echo": {**ECHO, "env": {"T": "${T}"}}})
    payload = {"name": "echo", "workingDirectory": str(root)}
    _run(global_echo, "approve", payload)
    assert _run(global_echo, "configure", {**payload, "secrets": {"env": {"T": "tok"}}})["toolCount"] == 7
    home = paths.get_vibe_remote_dir()

    def state():
        """Every MCP Hub file: global packages and the project cache with its secrets."""

        files = [*(home / "mcp").rglob("*"), *(home / "project_mcp").rglob("*")]
        return {path: path.read_bytes() for path in sorted(files) if path.is_file()}

    before = state()
    original = (root / ".mcp.json").read_text()
    edited = json.dumps({"mcpServers": {"echo": {**ECHO, "args": [FIXTURE, "edited"]}}})
    for content in (None, json.dumps({"mcpServers": {}}), edited):
        if content is None:
            (root / ".mcp.json").unlink()
        else:
            (root / ".mcp.json").write_text(content)
        _run(global_echo, "list", payload)
        _run(global_echo, "detail", payload)
        _run(global_echo, "call", {**payload, "sessionId": "s", "tool": "echo", "arguments": {"text": "x"}})
        build_system_prompt_blocks(backend="claude", skills_cwd=str(root))
        (root / ".mcp.json").write_text(original)
        assert state() == before
    assert _run(global_echo, "call", {**payload, "sessionId": "s", "tool": "echo", "arguments": {"text": "x"}})["status"] == "ok"


# ---------------------------------------------------------------------------
# Prompt: the index shows only ready servers the Session can see
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cwd", "shown"),
    [
        pytest.param("mine/src", set(), id="pending-project-entry-shadows-global"),
        pytest.param("other/src", {"echo"}, id="other-project-and-pending-hidden"),
        pytest.param("plain", {"echo"}, id="no-project-global-only"),
    ],
)
def test_prompt_index_lists_visible_ready_servers(tmp_path, global_echo, cwd, shown):
    from core.system_prompt_injection import build_system_prompt_blocks

    _project(tmp_path / "mine", {"echo": ECHO})
    _project(tmp_path / "other", {"secret-tool": ECHO})
    (tmp_path / "plain").mkdir()
    blocks = build_system_prompt_blocks(backend="claude", skills_cwd=str(tmp_path / cwd))
    text = "".join(block.text for block in blocks)
    listed = set(re.findall(r"^- (echo|secret-tool)\b", text, flags=re.MULTILINE))
    assert listed == shown


# ---------------------------------------------------------------------------
# CLI: the Controller resolves the visible set from the caller's scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "code", "expected"),
    [
        pytest.param(["--list", "--format", "json"], 0, '"servers": []', id="list-hides-shadowed-global"),
        pytest.param(["echo", "echo", "--text", "hi"], 1, "does not exist", id="call-outside-visible-set"),
        pytest.param(["--global", "--list", "--format", "json"], 0, '"name": "echo"', id="global-list"),
        pytest.param(["--global", "echo", "echo", "--text", "hi"], 0, "hi", id="global-reaches-shadowed-server"),
    ],
)
def test_cli_resolves_the_visible_set(tmp_path, monkeypatch, capsys, global_echo, argv, code, expected):
    from vibe import mcp_cli

    root = _project(tmp_path / "repo", {"echo": {"command": "${BIN}"}})
    monkeypatch.setenv("AVIBE_SKILL_WORKING_DIR", str(root / "src"))
    capsys.readouterr()
    assert mcp_cli.main(argv) == code
    captured = capsys.readouterr()
    assert expected in captured.out + captured.err


@pytest.mark.parametrize(
    ("argv", "code", "expected"),
    [
        pytest.param(["--list"], 0, "- echo: pending approval — run vibe mcp echo --approve", id="pending-list"),
        pytest.param(
            ["echo", "--list"],
            1,
            "error: MCP server echo: pending approval.\nhint: run vibe mcp echo --approve",
            id="pending-help",
        ),
        pytest.param(
            ["remote", "echo", "--text", "x"],
            1,
            "error: MCP server remote: needs config: HOST.\nhint: run vibe mcp remote --configure",
            id="needs-config-call",
        ),
    ],
)
def test_cli_shows_how_to_unblock_a_project_server(tmp_path, monkeypatch, capsys, runtime, argv, code, expected):
    from vibe import mcp_cli

    root = _project(tmp_path / "repo", {"echo": ECHO, "remote": {"url": "https://${HOST}/mcp"}})
    _run(runtime, "approve", {"name": "remote", "workingDirectory": str(root)})
    monkeypatch.setenv("AVIBE_SKILL_WORKING_DIR", str(root))
    capsys.readouterr()
    assert mcp_cli.main(argv) == code
    captured = capsys.readouterr()
    assert expected in captured.out + captured.err


@pytest.mark.parametrize(
    ("has_config", "flags", "target"),
    [
        pytest.param(True, [], "project", id="project-config-receives-entry"),
        pytest.param(False, [], "global", id="no-project-config-stays-global"),
        pytest.param(True, ["--global"], "global", id="global-flag-forces-global"),
    ],
)
def test_cli_add_targets_the_derived_project(tmp_path, monkeypatch, runtime, has_config, flags, target):
    from vibe import mcp_cli

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    if has_config:
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setenv("AVIBE_SKILL_WORKING_DIR", str(root))
    assert mcp_cli.add_main([*flags, "echo", "--", sys.executable, FIXTURE]) == 0
    in_project = has_config and "echo" in json.loads((root / ".mcp.json").read_text())["mcpServers"]
    in_global = repository.read_avibe_definition("echo") is not None
    assert (in_project, in_global) == (target == "project", target == "global")


def test_cli_sends_skill_scope_inputs(monkeypatch, tmp_path):
    from vibe import internal_client, mcp_cli

    sent = []
    monkeypatch.setenv("AVIBE_SKILL_WORKING_DIR", str(tmp_path))
    monkeypatch.setenv("AVIBE_SKILL_PROJECT_BASE", str(tmp_path))
    monkeypatch.setenv("AVIBE_SESSION_ID", "session")
    monkeypatch.setattr(mcp_cli, "_admit", lambda _floor: None)
    reply = {"ok": True, "servers": []}
    monkeypatch.setattr(internal_client, "mcp_request_sync", lambda op, payload, **_: sent.append((op, payload)) or reply)
    assert mcp_cli.main(["--global", "--list"]) == 0
    scope_inputs = {"workingDirectory": str(tmp_path), "projectBase": str(tmp_path), "sessionId": "session"}
    assert sent == [("list", {**scope_inputs, "global": True})]


# ---------------------------------------------------------------------------
# Owner-found polish: formatting, CLI confirmations, empty-dir cleanup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        pytest.param(
            '{"mcpServers":{"echo":{"command":"x"}}}',
            '{"mcpServers":{"echo":{"command":"x"}, "liner": {"command": "y"}}}',
            id="compact-file-stays-compact",
        ),
        pytest.param(
            '{\n    "mcpServers": {\n        "echo": {\n            "command": "x"\n        }\n    }\n}\n',
            '{\n    "mcpServers": {\n        "echo": {\n            "command": "x"\n        },\n        "liner": {\n            "command": "y"\n        }\n    }\n}\n',
            id="four-space-indent-kept",
        ),
        pytest.param(
            '{\n\t"mcpServers": {\n\t\t"echo": {\n\t\t\t"command": "x"\n\t\t}\n\t}\n}\n',
            '{\n\t"mcpServers": {\n\t\t"echo": {\n\t\t\t"command": "x"\n\t\t},\n\t\t"liner": {\n\t\t\t"command": "y"\n\t\t}\n\t}\n}\n',
            id="tab-indent-kept",
        ),
    ],
)
def test_write_server_preserves_the_projects_json_formatting(tmp_path, original, expected):
    from core.mcp_hub import scope

    root = _project(tmp_path / "repo", {})
    (root / ".mcp.json").write_text(original)
    key = scope.ScopedName(scope.project_scope(root), "liner")
    scope.write_server(key, {"command": "y"})
    assert (root / ".mcp.json").read_text() == expected


def test_write_server_replaces_only_the_one_entry(tmp_path):
    from core.mcp_hub import scope

    root = _project(tmp_path / "repo", {"echo": {"command": "old"}})
    scope.write_server(scope.ScopedName(scope.project_scope(root), "echo"), {"command": "new"}, replace=True)
    text = (root / ".mcp.json").read_text()
    assert json.loads(text)["mcpServers"] == {"echo": {"command": "new"}}
    assert text == '{"mcpServers": {"echo": {"command": "new"}}}'


def test_remove_server_deletes_the_empty_project_cache_dir(tmp_path):
    from core.mcp_hub import scope

    root = _project(tmp_path / "repo", {"echo": ECHO})
    key = scope.ScopedName(scope.project_scope(root), "echo")
    scope.approve(key)
    scope.remove_server(key)
    assert not key.scope.root.exists()


def test_cli_approve_prints_a_confirmation(tmp_path, monkeypatch, capsys, runtime):
    from vibe import mcp_cli

    root = _project(tmp_path / "repo", {"remote": {"url": "https://${HOST}/mcp"}})
    monkeypatch.setenv("AVIBE_SKILL_WORKING_DIR", str(root))
    capsys.readouterr()
    assert mcp_cli.main(["remote", "--approve"]) == 0
    assert "Approved remote" in capsys.readouterr().out


def test_project_add_and_replace_preserve_oauth_options(tmp_path, runtime):
    root = _project(tmp_path / "repo", {})
    for client_id, scopes in [("first-client", "read"), ("replacement-client", "read write")]:
        oauth = {"clientId": client_id, "scopes": scopes}
        result = _run(runtime, "add", {
            "workingDirectory": str(root),
            "replace": True,
            "servers": [{"name": "remote", "server": {"url": "https://example.invalid/mcp", "oauth": oauth}}],
        })
        assert result["ok"]
        written = json.loads((root / ".mcp.json").read_text())
        assert written["mcpServers"]["remote"]["oauth"] == oauth
