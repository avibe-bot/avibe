"""Guard contracts absent from the pipeline's workflow-wiring tests.

Real, offline resolvers must reject stale locks without changing them; a tool
that writes despite its check flags must also fail the guard. Schema/workflow
errors must fail semantically, not just parse as JSON/YAML. All fixtures and
package-manager caches live under tmp_path; no dependency installation/builds.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts import ci_manifest_guard as guard


def put(root: Path, name: str, content: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=root, text=True, capture_output=True, timeout=30, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    run(tmp_path, "git", "init", "-q")
    return tmp_path


@pytest.fixture
def locked_repo(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for executable in ("npm", "uv", "cargo"):
        if not shutil.which(executable):
            pytest.skip(f"offline resolver contract requires {executable}")
        try:
            run(repo, executable, "--version")
        except (OSError, subprocess.CalledProcessError):
            # The suite isolates HOME. A rustup shim can exist without a usable
            # toolchain there; CI puts the actual toolchain bin directory on
            # PATH before pytest, rather than allowing rustup to install one.
            pytest.skip(f"offline resolver contract requires runnable {executable} in the isolated test home")
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_PYTHON", sys.executable)
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    monkeypatch.setenv("UV_CACHE_DIR", str(repo / "cache/uv"))
    monkeypatch.setenv("CARGO_HOME", str(repo / "cache/cargo"))
    monkeypatch.setenv("CARGO_NET_OFFLINE", "true")
    monkeypatch.setenv("npm_config_cache", str(repo / "cache/npm"))
    monkeypatch.setenv("npm_config_offline", "true")
    monkeypatch.setenv("npm_config_userconfig", str(repo / "empty.npmrc"))
    put(repo, "empty.npmrc", "")
    put(repo, "pyproject.toml", '[project]\nname = "guard-fixture"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n')
    put(repo, "desktop/Cargo.toml", '[workspace]\nmembers = ["runtime-host"]\nresolver = "2"\n')
    put(
        repo,
        "desktop/runtime-host/Cargo.toml",
        '[package]\nname = "guard-fixture"\nversion = "0.1.0"\nedition = "2021"\n',
    )
    put(
        repo,
        "desktop/runtime-host/src/lib.rs",
        '// metadata must not compile this crate\ncompile_error!("no build");\n',
    )
    manifest = {"name": "guard-fixture", "version": "1.0.0", "scripts": {"postinstall": "touch SCRIPT_RAN"}}
    for directory in ("ui", "desktop"):
        put(repo, f"{directory}/package.json", json.dumps(manifest))
        run(repo / directory, "npm", "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund")
    put(repo, "local-dep/package.json", '{"name":"local-dep","version":"1.0.0"}')
    put(
        repo,
        "npm/avibe/package.json",
        json.dumps(
            {
                "name": "@avibe/cli",
                "version": "1.2.3",
                "bin": {"avibe": "bin/avibe.js", "vibe": "bin/avibe.js"},
            }
        ),
    )
    put(repo, "npm/avibe/bin/avibe.js", "#!/usr/bin/env node\n")
    run(repo, "uv", "lock", "--offline")
    run(repo / "desktop", "cargo", "generate-lockfile", "--offline")
    run(repo, "git", "add", "pyproject.toml", "uv.lock", "desktop", "ui", "npm", "local-dep")
    return repo


def test_real_resolvers_accept_fresh_locks_without_builds_or_scripts(locked_repo: Path) -> None:
    before = guard.snapshot(locked_repo, set(guard.LOCKFILES))
    assert guard.main(["--root", str(locked_repo), "--locks-only"]) == 0
    assert guard.snapshot(locked_repo, set(guard.LOCKFILES)) == before
    assert not (locked_repo / "desktop/target").exists()
    for directory in ("ui", "desktop"):
        assert not (locked_repo / directory / "SCRIPT_RAN").exists()
        assert not (locked_repo / directory / "node_modules").exists()
    assert not (locked_repo / "npm/avibe/package-lock.json").exists()


@pytest.mark.parametrize("ecosystem", ["uv", "cargo", "ui", "desktop"])
def test_real_resolvers_reject_stale_locks_without_rewriting(locked_repo: Path, ecosystem: str) -> None:
    if ecosystem == "uv":
        path = locked_repo / "pyproject.toml"
        path.write_text(path.read_text().replace(">=3.10", ">=3.11"))
    elif ecosystem == "cargo":
        path = locked_repo / "desktop/runtime-host/Cargo.toml"
        path.write_text(path.read_text().replace("0.1.0", "0.2.0"))
        # The tempting faster command really misses this mismatch.
        run(locked_repo / "desktop", "cargo", "metadata", "--locked", "--format-version", "1", "--no-deps")
    else:
        path = locked_repo / ecosystem / "package.json"
        manifest = json.loads(path.read_text())
        manifest["dependencies"] = {"local-dep": "file:../local-dep"}
        path.write_text(json.dumps(manifest))
    before = guard.snapshot(locked_repo, set(guard.LOCKFILES))
    with pytest.raises(guard.GuardError) as error:
        guard.check_locks(locked_repo)
    message = str(error.value)
    assert {"uv": "uv lock --check", "cargo": "cargo metadata --locked"}.get(ecosystem, "npm ci") in message
    assert "exited" in message
    assert guard.snapshot(locked_repo, set(guard.LOCKFILES)) == before


@pytest.mark.parametrize("missing", guard.LOCKFILES)
def test_missing_lock_fails_before_running_a_resolver(repo: Path, missing: str) -> None:
    for name in (*guard.MANIFESTS, *guard.LOCKFILES):
        if name != missing:
            put(repo, name, "not parsed: preflight must fail first")
    with pytest.raises(guard.GuardError, match=f"missing required manifest/lock: {missing}"):
        guard.check_locks(repo)
    assert not (repo / missing).exists()


@pytest.mark.parametrize("exit_failure", [False, True])
def test_unexpected_tool_lock_write_is_reported_even_on_failure(
    locked_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_failure: bool,
) -> None:
    original = guard.command

    def writes_lock(args: list[str], cwd: Path, timeout: float) -> str:
        if args[0] == "git":
            return original(args, cwd, timeout)
        if args[0] == "uv":
            (cwd / "uv.lock").write_text("unexpected write")
            if exit_failure:
                raise guard.GuardError("resolver also failed")
        return ""

    monkeypatch.setattr(guard, "command", writes_lock)
    with pytest.raises(guard.GuardError, match="check modified manifests/locks.*uv.lock"):
        guard.check_locks(locked_repo)
    assert (locked_repo / "uv.lock").read_text() == "unexpected write"  # report, never silently repair


@pytest.mark.parametrize("mutation", ["dependencies", "missing-bin", "escape-bin", "lock"])
def test_dependency_free_launcher_declaration_is_checked(locked_repo: Path, mutation: str) -> None:
    path = locked_repo / "npm/avibe/package.json"
    manifest = json.loads(path.read_text())
    if mutation == "dependencies":
        manifest["dependencies"] = {"example": "1"}
    elif mutation == "missing-bin":
        manifest["bin"]["vibe"] = "missing.js"
    elif mutation == "escape-bin":
        manifest["bin"]["vibe"] = "../../local-dep/package.json"
    else:
        put(locked_repo, "npm/avibe/package-lock.json", "{}")
    path.write_text(json.dumps(manifest))
    with pytest.raises(guard.GuardError, match="npm/avibe"):
        guard.check_npm_entrypoint(locked_repo)


@pytest.mark.parametrize(
    "bad_schema",
    [
        '{"type":',
        '{"$schema":"http://json-schema.org/draft-07/schema#","type":"not-a-type"}',
        '{"$schema":"http://json-schema.org/draft-07/schema#","required":"not-an-array"}',
        '{"$schema":"https://example.invalid/unknown-dialect","type":"object"}',
        '{"$schema":"http://json-schema.org/draft-07/schema#","type":"object","type":"string"}',
    ],
)
def test_schema_validation_rejects_malformed_tracked_schemas(repo: Path, bad_schema: str) -> None:
    path = put(
        repo, "contracts/example.schema.json", '{"$schema":"http://json-schema.org/draft-07/schema#","type":"object"}'
    )
    run(repo, "git", "add", ".")
    assert guard.main(["--root", str(repo), "--schemas-only"]) == 0
    path.write_text(bad_schema)
    assert guard.main(["--root", str(repo), "--schemas-only"]) == 1


def test_combined_schema_and_companion_data_must_agree(repo: Path) -> None:
    """Separate data/schema edits can be valid JSON but incompatible together."""
    schema = put(repo, "contracts/example.schema.json", json.dumps({
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object", "required": ["version"],
        "properties": {"version": {"type": "integer"}},
    }))
    data = put(repo, "contracts/example.json", '{"version": 1}')
    run(repo, "git", "add", ".")
    guard.check_schemas(repo)
    data.write_text('{"version": "wrong"}')
    with pytest.raises(guard.GuardError, match="not of type"):
        guard.check_schemas(repo)
    data.write_text('{"version": 1}')
    definition = json.loads(schema.read_text())
    definition["required"].append("new_required_field")
    schema.write_text(json.dumps(definition))
    with pytest.raises(guard.GuardError, match="required property"):
        guard.check_schemas(repo)


def test_untracked_scratch_schema_is_not_part_of_the_contract(repo: Path) -> None:
    put(
        repo, "contracts/real.schema.json", '{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object"}'
    )
    run(repo, "git", "add", ".")
    put(repo, "scratch/invalid.schema.json", "invalid")
    guard.check_schemas(repo)


@pytest.mark.parametrize("workflow_name", ["lint.yml", "desktop-native-update-acceptance.yml"])
@pytest.mark.parametrize(
    "bad_workflow",
    [
        "on: [push\njobs: {}\n",
        "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n        run: echo invalid\n",
        "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ${{ missing.context }}\n",
        "on: workflow_dispatch\njobs:\n  test:\n    runs-on: ubuntu-latest\n    env:\n      ACCEPTANCE_ROOT: ${{ runner.temp }}/acceptance\n    steps:\n      - run: echo valid\n",
    ],
)
def test_real_actionlint_rejects_workflow_syntax_and_semantics(
    repo: Path, bad_workflow: str, workflow_name: str,
) -> None:
    if not shutil.which("actionlint"):
        pytest.skip(f"workflow validation requires actionlint {guard.ACTIONLINT_VERSION}")
    valid_workflow = "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo valid\n"
    # Keep a known workflow present: an omitted acceptance workflow must fail
    # because its invalid expression escaped semantic validation, not because
    # the guard found no workflows to check.
    put(repo, ".github/workflows/lint.yml", valid_workflow)
    path = put(repo, f".github/workflows/{workflow_name}", valid_workflow)
    run(repo, "git", "add", ".")
    assert guard.main(["--root", str(repo), "--workflows-only"]) == 0
    path.write_text(bad_workflow)
    assert guard.main(["--root", str(repo), "--workflows-only"]) == 1


@pytest.mark.parametrize(
    "name,invalid",
    [
        ("data/manifest.json", '{"broken":}'),
        (".github/workflows/unrelated.yml", "jobs: [broken"),
        ("data/settings.yaml", "duplicate: first\nduplicate: second\n"),
    ],
)
def test_all_tracked_json_yaml_are_parsed(repo: Path, name: str, invalid: str) -> None:
    put(repo, name, invalid)
    run(repo, "git", "add", ".")
    with pytest.raises(guard.GuardError):
        guard.check_serialized_files(repo)


@pytest.mark.parametrize(
    "queue,cancel,valid",
    [
        ("max", "false", True),
        ("single", "true", True),
        ("max", "true", False),
        ("many", "false", False),
        ("[max]", "false", False),
        ("max", "${{ github.ref == 'refs/heads/main' }}", False),
    ],
)
def test_publish_queue_compatibility_is_narrow_and_validated(
    repo: Path,
    queue: str,
    cancel: str,
    valid: bool,
) -> None:
    if not shutil.which("actionlint"):
        pytest.skip(f"workflow validation requires actionlint {guard.ACTIONLINT_VERSION}")
    source_config = Path(guard.__file__).resolve().parents[1] / ".github/actionlint.yaml"
    put(repo, ".github/actionlint.yaml", source_config.read_text())
    workflow = (
        "on: push\njobs:\n  release:\n    runs-on: ubuntu-latest\n"
        "    concurrency:\n      group: release\n"
        f"      queue: {queue}\n      cancel-in-progress: {cancel}\n"
        "    steps:\n      - run: echo valid\n"
    )
    path = put(repo, ".github/workflows/publish.yml", workflow)
    run(repo, "git", "add", ".")
    assert guard.main(["--root", str(repo), "--workflows-only"]) == (0 if valid else 1)
    if valid:
        # The config must not hide another unexpected concurrency field.
        path.write_text(workflow.replace("      queue:", "      typo: ignored?\n      queue:"))
        assert guard.main(["--root", str(repo), "--workflows-only"]) == 1
        # Nor should its exact queue exception leak to another workflow.
        path.unlink()
        put(repo, ".github/workflows/lint.yml", workflow)
        run(repo, "git", "add", "-A")
        assert guard.main(["--root", str(repo), "--workflows-only"]) == 1


def test_missing_actionlint_is_not_silently_skipped(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put(repo, ".github/workflows/lint.yml", "on: push\n")
    run(repo, "git", "add", ".")
    # Git remains real; only the unavailable validator is substituted.
    original = guard.command

    def unavailable(args: list[str], cwd: Path, timeout: float) -> str:
        if args[0] == "actionlint":
            raise guard.GuardError("actionlint: executable not found")
        return original(args, cwd, timeout)

    monkeypatch.setattr(guard, "command", unavailable)
    assert guard.main(["--root", str(repo), "--workflows-only"]) == 1
