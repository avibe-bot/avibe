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


@pytest.mark.parametrize("entrypoint", ["runtime", "cli", "web"])
def test_project_add_preserves_unconfigured_url(tmp_path, monkeypatch, runtime, entrypoint):
    from core.mcp_hub import scope
    from vibe import internal_client, mcp_api, mcp_cli

    root = _project(tmp_path / "repo", {})
    monkeypatch.setenv("AVIBE_SKILL_WORKING_DIR", str(root))
    url = "https://${HOST}/mcp"
    if entrypoint == "cli":
        assert mcp_cli.add_main(["--transport", "http", "remote", url]) == 0
    elif entrypoint == "web":
        async def request(operation, payload):
            payload.pop("project", None)
            status, body = await runtime.submit(operation, {**payload, "workingDirectory": str(root)})
            assert status == 200
            return json.loads(body)
        monkeypatch.setattr(internal_client, "mcp_request", request)
        token = mcp_api.project_context.set("test-project")
        try:
            assert asyncio.run(mcp_api.add({"text": json.dumps({"mcpServers": {"remote": {"url": url}}})}))["ok"]
        finally:
            mcp_api.project_context.reset(token)
    else:
        assert _run(runtime, "add", {"workingDirectory": str(root), "servers": [{"name": "remote", "server": {"url": url}}]})["ok"]
    assert json.loads((root / ".mcp.json").read_text())["mcpServers"]["remote"]["url"] == url
    key = _key(root, "remote")
    scope.approve(key)
    assert scope.load_server(key).state == "needs_config"
    assert scope.load_server(key).missing == ["HOST"]


@pytest.mark.parametrize("field,transport,initial,supplied,expected_file,expected_private", [
    ("headers", {"url": "https://example.invalid/mcp"}, {}, {"Authorization": "Bearer ${TOKEN}"}, {"Authorization": "Bearer ${TOKEN}"}, {}),
    ("env", {"command": "unused"}, {}, {"TOKEN": "private"}, {}, {"TOKEN": "private"}),
    ("headers", {"url": "https://example.invalid/mcp"}, {}, {"Authorization": "${TOKEN}", "X-Key": "private"}, {"Authorization": "${TOKEN}"}, {"X-Key": "private"}),
    ("env", {"command": "unused"}, {"KEY": "${TOKEN}", "KEEP": "untouched"}, {"KEY": "private"}, {}, {"KEY": "private", "KEEP": "untouched"}),
    ("env", {"command": "unused"}, {"KEY": "private", "KEEP": "${OTHER}"}, {"KEY": "${TOKEN}"}, {"KEY": "${TOKEN}", "KEEP": "${OTHER}"}, {}),
])
def test_project_add_splits_templates_from_concrete_values(tmp_path, runtime, field, transport, initial, supplied, expected_file, expected_private):
    from core.mcp_hub import scope

    root = _project(tmp_path / "repo", {})
    key = _key(root, "remote")
    if initial:
        scope.write_server(key, {**transport, field: initial})
    scope.write_server(key, {**transport, field: supplied})
    document = json.loads((root / ".mcp.json").read_text())["mcpServers"]["remote"]
    assert document.get(field, {}) == expected_file
    cache_path = repository.package_dir(key) / "secrets.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    assert cache.get(field, {}) == expected_private
    old_document = (root / ".mcp.json").read_bytes()
    old_cache = cache_path.read_bytes() if cache_path.exists() else None
    from core.mcp_hub.errors import McpHubError

    with pytest.raises(McpHubError):
        scope.write_server(key, {**transport, field: {"BAD\nKEY": "invalid"}})
    assert (root / ".mcp.json").read_bytes() == old_document
    assert (cache_path.read_bytes() if cache_path.exists() else None) == old_cache


# ---------------------------------------------------------------------------
# Global (Avibe-owned) servers share the placeholder model
# ---------------------------------------------------------------------------

SECRET = "tok-secret-9f3"
CONCRETE = "plain-secret-51a"


def _recording_clients(monkeypatch) -> list:
    opened = []
    enter_client = mcp_runtime._enter_client

    async def recording(definition, *args, **kwargs):
        opened.append(definition)
        return await enter_client(definition, *args, **kwargs)

    monkeypatch.setattr(mcp_runtime, "_enter_client", recording)
    return opened


def _stored_text(name: str) -> str:
    """Everything an MCP package publishes or exposes, except the private secrets sidecar."""

    directory = repository.package_dir(name)
    return "".join(path.read_text() for path in sorted(directory.rglob("*.json")) if path.name != "secrets.json")


@pytest.mark.parametrize(
    ("env", "missing", "file_env", "private_env"),
    [
        pytest.param({"TOKEN": "${TOKEN}"}, ["TOKEN"], {"TOKEN": "${TOKEN}"}, None, id="template"),
        pytest.param({"TOKEN": SECRET}, [], {}, {"TOKEN": SECRET}, id="concrete"),
        pytest.param(
            {"TOKEN": "${TOKEN}", "PLAIN": CONCRETE}, ["TOKEN"], {"TOKEN": "${TOKEN}"}, {"PLAIN": CONCRETE}, id="mixed"
        ),
    ],
)
@pytest.mark.parametrize("surface", ["runtime", "cli"])
def test_global_add_configure_call(tmp_path, monkeypatch, capsys, runtime, surface, env, missing, file_env, private_env):
    from vibe import mcp_cli

    opened = _recording_clients(monkeypatch)
    if surface == "cli":
        argv = ["--global", "echo"] + [f"--env={k}={v}" for k, v in env.items()] + ["--", ECHO["command"], *ECHO["args"]]
        assert mcp_cli.add_main(argv) == 0
        out = capsys.readouterr().out
        assert ("needs config: TOKEN" in out) == bool(missing)
    else:
        added = _run(runtime, "add", {"global": True, "servers": [{"name": "echo", "server": {**ECHO, "env": env}}]})
        assert added["ok"] and added["added"][0].get("state", "ok") == ("needs_config" if missing else "ok")

    directory = repository.package_dir("echo")
    assert json.loads((directory / "definition.json").read_text())["transport"]["env"] == file_env
    secrets_file = directory / "secrets.json"
    assert (json.loads(secrets_file.read_text()).get("env") if secrets_file.exists() else None) == private_env
    if secrets_file.exists():
        assert oct(secrets_file.stat().st_mode & 0o777) == "0o600"
        assert oct(directory.stat().st_mode & 0o777) == "0o700"

    row = next(item for item in _run(runtime, "list", {"global": True})["servers"] if item["name"] == "echo")
    call = {"name": "echo", "global": True, "tool": "echo", "arguments": {"text": "hi"}}
    if missing:
        assert row["state"] == "needs_config" and row["missingVariables"] == missing
        assert _run(runtime, "call", call)["error"]["type"] == "needs_config"
        assert opened == []  # an unresolved add never connects
        configured = _run(runtime, "configure", {"name": "echo", "global": True, "secrets": {"env": {**(private_env or {}), "TOKEN": SECRET}}})
        assert configured["toolCount"] == 7  # a supplied map replaces that map, as for project servers
    else:
        assert row["state"] == "ok" and "missingVariables" not in row
    assert _run(runtime, "call", call)["status"] == "ok"
    assert opened[-1]["transport"]["env"] == {**env, "TOKEN": SECRET}  # execution resolves privately

    published = repository.read_published("echo")["definition"]["transport"]["env"]
    assert published == file_env  # publication keeps templates
    exposed = json.dumps(_run(runtime, "list", {"global": True})) + json.dumps(_run(runtime, "detail", {"name": "echo"}))
    for secret in (SECRET, CONCRETE):
        assert secret not in exposed + _stored_text("echo")


@pytest.mark.parametrize(
    "server",
    [
        pytest.param({"command": "${BIN}"}, id="command"),
        pytest.param({**ECHO, "args": ["${ARG}"]}, id="args"),
        pytest.param({"url": "https://${HOST}/mcp", "headers": {"X-Bad": "${1BAD}"}}, id="bad-name"),
        pytest.param({"url": "https://${HOST}/mcp", "args": ["a"]}, id="bad-structure"),
    ],
)
def test_invalid_global_placeholders_write_nothing(runtime, server):
    from core.mcp_hub import scope

    with pytest.raises(Exception):
        scope.global_add_definition("bad", server)
    result = _run(runtime, "add", {"global": True, "servers": [{"name": "bad", "server": server}]})
    assert result.get("ok") is not True
    assert not repository.package_dir("bad").exists()


@pytest.mark.parametrize(
    ("raw", "secrets", "expected"),
    [
        pytest.param(
            {"url": "https://${HOST}/mcp", "headers": {"Authorization": "Bearer ${TOKEN}", "X-Team": CONCRETE}},
            {"env": {"HOST": "h.example", "TOKEN": SECRET}},
            {"url": "https://h.example/mcp", "headers": {"Authorization": f"Bearer {SECRET}", "X-Team": CONCRETE}},
            id="http-url-and-header",
        ),
        pytest.param({"url": "https://${HOST}/mcp"}, {}, None, id="http-missing"),
    ],
)
def test_global_http_templates_resolve_only_in_memory(runtime, raw, secrets, expected):
    from core.mcp_hub import scope

    definition = scope.global_add_definition("remote", raw)
    template, private, missing = scope.split_global(definition)
    assert template["transport"]["url"] == raw["url"]
    assert all("${" in value for value in template["transport"]["headers"].values())
    repository.write_definition(template)
    scope.write_global_secrets("remote", private)
    if secrets:
        scope.configure(scope.global_key("remote"), secrets)
    if expected is None:
        assert missing == ["HOST"]
        with pytest.raises(Exception, match="needs config: HOST"):
            scope.resolve_global("remote", template)
        return
    resolved = scope.resolve_global("remote", template)["transport"]
    assert {"url": resolved["url"], "headers": resolved["headers"]} == expected
    assert SECRET not in (repository.package_dir("remote") / "definition.json").read_text()


def test_released_global_definition_runs_unchanged(monkeypatch, runtime):
    """A definition.json written by a released Avibe (concrete values, no sidecar) is read as is."""

    released = {
        "schemaVersion": 1, "name": "echo",
        "transport": {"type": "stdio", "command": ECHO["command"], "args": ECHO["args"],
                      "env": {"TOKEN": SECRET}, "cwd": repository.default_cwd()},
    }
    directory = repository.package_dir("echo")
    directory.mkdir(parents=True)
    (directory / "definition.json").write_text(json.dumps(released))
    opened = _recording_clients(monkeypatch)

    assert _run(runtime, "refresh", {"name": "echo"})["toolCount"] == 7
    assert _run(runtime, "call", {"name": "echo", "tool": "echo", "arguments": {"text": "x"}})["status"] == "ok"
    assert repository.read_published("echo")["definition"] == released
    assert all(item["transport"] == released["transport"] for item in opened)
    assert not (directory / "secrets.json").exists()


def test_global_replace_keeps_unsupplied_keys_and_moves_supplied_ones(runtime):
    def add(env, replace=False):
        server = {"name": "echo", "server": {**ECHO, "env": env}}
        return _run(runtime, "add", {"global": True, "replace": replace, "servers": [server]})

    add({"A": "${A}", "B": CONCRETE, "C": "${C}"})
    _run(runtime, "configure", {"name": "echo", "global": True, "secrets": {"env": {"A": SECRET, "B": CONCRETE, "C": "c"}}})
    replaced = add({"B": "${B}", "A": "a-plain"}, replace=True)

    assert replaced["added"][0]["missingVariables"] == ["B"]
    directory = repository.package_dir("echo")
    assert json.loads((directory / "definition.json").read_text())["transport"]["env"] == {"B": "${B}", "C": "${C}"}
    assert json.loads((directory / "secrets.json").read_text())["env"] == {"A": "a-plain", "C": "c"}


def test_only_avibe_owned_global_servers_are_configurable(runtime):
    configure = _run(runtime, "configure", {"name": "native-only", "global": True, "secrets": {"env": {"A": "b"}}})
    assert configure["error"]["type"] == "usage"
