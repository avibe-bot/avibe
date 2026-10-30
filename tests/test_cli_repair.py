"""Real child-shell repair boundaries; no user CLI/package manager is changed."""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from vibe import api, cli_paths
from tests.test_cli_health import fixture_env
from vibe.cli_health import cli_version, probe_cli
from vibe.cli_repair import identify_repair_source, native_bootstrap_env


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def native_case(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("the vendor native OpenCode installer is POSIX")
    home = tmp_path / "用户 HOME"
    home.mkdir()
    target = home / ".opencode/bin/opencode"
    old_calls = tmp_path / "old-calls"
    network = tmp_path / "network"
    persist = []
    tools = tmp_path / "tools"
    tools.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PYTHONPATH", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
        or key.startswith(("AVIBE_TEST_", "VIBE_TEST_"))
    }
    env.update(HOME=str(home), PATH=os.pathsep.join((str(tools), str(target.parent), "/usr/bin", "/bin")))
    env.update(
        AVIBE_HOME=str(home / ".avibe"),
        CODEX_HOME=str(home / ".codex"),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(api, "_command_env_for", lambda _path: dict(env))
    monkeypatch.setattr(api, "_configured_agent_cli_path", lambda name: str(target))
    monkeypatch.setattr(api, "_configured_backend_language", lambda: "en")
    monkeypatch.setattr(api, "is_desktop_backend_path", lambda _path: False)
    monkeypatch.setattr(api, "is_private_desktop_runtime_path", lambda _path: False)
    monkeypatch.setattr(api, "is_desktop_managed_runtime", lambda: False)
    monkeypatch.setattr(api, "_persist_agent_cli_path", lambda name, path: persist.append((name, path)))
    observations = []

    def observe(backend, path, *, env):
        health = asyncio.run(probe_cli(backend, path, env=env, timeout_seconds=3))
        observations.append(health.payload())
        return health

    monkeypatch.setattr(api, "probe_cli_sync", observe)
    script = tmp_path / "vendor-boundary.sh"
    # The actual vendor boundary (opencode.ai/install, inspected 2026-10-12):
    # command -v -> --version -> same-version exit before downloading. Keep
    # this executable boundary, not a Python mock of its claimed outcome.
    script.write_text("""set -euo pipefail
specific_version=1.2.15
if command -v opencode >/dev/null 2>&1; then
  opencode_path=$(which opencode)
  installed_version=$(opencode --version 2>/dev/null || echo "")
  if [[ "$installed_version" == "$specific_version" ]]; then exit 0; fi
fi
mkdir -p "$HOME/.opencode/bin"
printf '#!/bin/sh\\nprintf "1.2.15\\\\n"\\n' > "$HOME/.opencode/bin/opencode"
chmod 755 "$HOME/.opencode/bin/opencode"
""")
    curl = executable(
        tools / "curl",
        f'from pathlib import Path\nPath({str(network)!r}).write_text("download")\nprint(Path({str(script)!r}).read_text())\n',
    )

    def resolve(name, **_):
        if name == "bash":
            return "/bin/bash"
        if name == "curl":
            return str(curl)
        return str(target) if name in ("opencode", str(target)) and target.exists() else None

    monkeypatch.setattr(api, "resolve_cli_path", resolve)
    return dict(
        home=home,
        target=target,
        old=old_calls,
        network=network,
        env=env,
        script=script,
        persist=persist,
        tools=tools,
        observations=observations,
    )


def old_cli(case, *, hang=False, misleading=False):
    body = f'from pathlib import Path\nimport time,sys\nwith Path({str(case["old"])!r}).open("a") as log:log.write("version probe\\n")\n'
    body += "time.sleep(60)\n" if hang else f"print({'1.2.15' if misleading else 'broken'!r})\nsys.exit(1)\n"
    return executable(case["target"], body)


@pytest.mark.parametrize("hang,misleading", [(False, False), (True, False), (False, True)])
def test_native_repair_never_uses_bad_cli_in_bootstrap(native_case, hang, misleading):
    c = native_case
    old_cli(c, hang=hang, misleading=misleading)
    original_env = dict(c["env"])
    result = api.install_agent("opencode")
    assert result["ok"] is True, result
    assert c["old"].exists(), c["observations"]
    assert c["old"].read_text().splitlines() == ["version probe"]  # initial bounded detection only
    assert c["network"].exists()
    assert c["persist"] == [("opencode", str(c["target"]))]
    assert c["env"] == original_env
    assert subprocess.check_output([str(c["target"]), "--version"], env=c["env"], text=True).strip() == "1.2.15"


@pytest.mark.parametrize("alias", [True, False])
def test_remaining_resolution_refuses_before_network_or_other_cli(native_case, alias):
    c = native_case
    old_cli(c)
    other = c["home"] / "other-bin"
    other.mkdir()
    marker = c["home"] / "other-invoked"
    if alias:
        (other / "opencode").symlink_to(c["target"])
    else:
        executable(other / "opencode", f'from pathlib import Path\nPath({str(marker)!r}).touch()\nprint("1.2.15")\n')
    c["env"]["PATH"] = str(other) + os.pathsep + c["env"]["PATH"]
    result = api.install_agent("opencode")
    assert result["ok"] is False and result["code"] == "repair_path_conflict"
    assert c["old"].exists(), c["observations"]
    assert c["old"].read_text().splitlines() == ["version probe"]
    assert not marker.exists() and not c["network"].exists() and c["persist"] == []


def test_negative_control_vendor_skips_bad_target_for_other_matching_version(native_case):
    c = native_case
    old_cli(c)
    other = c["home"] / "other-bin"
    other.mkdir()
    marker = c["home"] / "other-invoked"
    executable(other / "opencode", f'from pathlib import Path\nPath({str(marker)!r}).touch()\nprint("1.2.15")\n')
    c["env"]["PATH"] = str(other) + os.pathsep + c["env"]["PATH"]
    before = c["target"].read_bytes()
    subprocess.run(["/bin/bash", str(c["script"])], env=c["env"], check=True, timeout=5)
    assert marker.exists() and c["target"].read_bytes() == before
    marker.unlink()
    result = api.install_agent("opencode")
    assert result["code"] == "repair_path_conflict" and not marker.exists() and not c["network"].exists()


def test_startup_hooks_and_imported_functions_cannot_restore_bad_cli(native_case):
    c = native_case
    old_cli(c)
    marker = c["home"] / "hook-ran"
    hook = c["home"] / "hook.sh"
    hook.write_text(f"touch {shlex.quote(str(marker))}\nexport PATH={shlex.quote(str(c['target'].parent))}:$PATH\n")
    c["env"].update(BASH_ENV=str(hook), ENV=str(hook))
    c["env"]["BASH_FUNC_opencode%%"] = f"() {{ touch {shlex.quote(str(marker))}; echo 1.2.15; }}"
    result = api.install_agent("opencode")
    assert result["ok"] and not marker.exists()
    assert c["old"].exists(), c["observations"]
    assert c["old"].read_text().splitlines() == ["version probe"]
    assert c["env"]["BASH_ENV"] == str(hook)  # no process/global environment mutation


@pytest.mark.parametrize("outcome", ["unchanged", "missing", "timeout"])
def test_exit_zero_is_not_repair_success_or_permission_to_switch_path(native_case, outcome):
    c = native_case
    old_cli(c)
    other = c["home"] / "other"
    other.mkdir()
    marker = c["home"] / "other-read"
    executable(other / "opencode", f'from pathlib import Path\nPath({str(marker)!r}).touch()\nprint("1.2.15")\n')
    c["env"]["PATH"] = str(other) + os.pathsep + c["env"]["PATH"]
    if outcome == "timeout":
        old_cli(c, hang=True)
    code = f"from pathlib import Path; Path({str(c['target'])!r}).unlink()" if outcome == "missing" else "pass"
    result = api._run_install_command(
        "opencode",
        [sys.executable, "-c", code],
        lambda s: s,
        mode="repair",
        env=c["env"],
        expected_path=str(c["target"]),
    )
    assert result["ok"] is False and result["code"] == "cli_readback_failed"
    assert c["persist"] == [] and not marker.exists()


@pytest.mark.parametrize(
    "backend,package",
    [("claude", "@anthropic-ai/claude-code"), ("opencode", "opencode-ai"), ("codex", "@openai/codex")],
)
def test_npm_provenance_preserves_global_prefix(tmp_path, backend, package):
    prefix = tmp_path / "npm-prefix"
    pkg = prefix / "lib/node_modules" / package
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"name": package}))
    target = executable(pkg / "cli.js", 'print("broken")\n')
    alias = prefix / "bin" / backend
    alias.parent.mkdir()
    alias.symlink_to(target)
    source = identify_repair_source(backend, str(alias), home=tmp_path)
    assert (
        source.kind == "npm"
        and source.prefix == prefix
        and source.package == package
        and source.destination == str(alias)
    )


@pytest.mark.parametrize(
    "backend,package,version_output",
    [
        ("claude", "@anthropic-ai/claude-code", "2.1.34 (Claude Code)"),
        ("opencode", "opencode-ai", "1.2.15"),
    ],
)
# A repair can pin the release an unattended upgrade replaced (core.update_checker).
@pytest.mark.parametrize("repair_version", [None, "1.0.0"])
def test_broken_npm_cli_is_repaired_by_its_prefix_not_self_update(
    native_case, monkeypatch, backend, package, version_output, repair_version
):
    c = native_case
    prefix = c["home"] / "owned npm prefix"
    pkg = prefix / "lib/node_modules" / package
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"name": package}))
    old = c["home"] / "npm-old-invoked"
    target = executable(
        pkg / "cli.js",
        f'from pathlib import Path\nimport sys\nwith Path({str(old)!r}).open("a") as f:f.write(sys.argv[1]+"\\n")\nsys.exit(1)\n',
    )
    alias = prefix / "bin" / backend
    alias.parent.mkdir()
    alias.symlink_to(target)
    calls = c["home"] / "npm-args.json"
    replacement = "#!" + sys.executable + "\nprint(" + repr(version_output) + ")\n"
    npm = executable(
        c["tools"] / "npm",
        f"import sys,json\nfrom pathlib import Path\nPath({str(calls)!r}).write_text(json.dumps(sys.argv[1:]))\nPath({str(target)!r}).write_text({replacement!r})\n",
    )
    monkeypatch.setattr(api, "_configured_agent_cli_path", lambda _name: str(alias))
    monkeypatch.setattr(api, "resolve_cli_path", lambda name, **_: str(npm) if name == "npm" else str(alias))
    result = api.install_agent(backend, version=repair_version)
    assert result["ok"], result
    spec = f"{package}@{repair_version}" if repair_version else package
    assert json.loads(calls.read_text()) == ["install", "--global", "--prefix", str(prefix), spec]
    assert old.read_text().splitlines() == ["--version"]
    assert c["persist"] == [(backend, str(alias))]
    assert result["detection"]["status"] == "ok" and result["detection"]["path"] == str(alias)


@pytest.mark.parametrize("configured_bare", [False, True])
@pytest.mark.parametrize("outcome", ["missing", "permission", "broken", "healthy"])
def test_healthy_upgrade_certifies_only_the_selected_executable(native_case, monkeypatch, configured_bare, outcome):
    c = native_case
    selected = c["target"]
    other_marker = c["home"] / "unrelated-probed"
    other = executable(
        c["home"] / "other/bin/opencode",
        f'from pathlib import Path\nPath({str(other_marker)!r}).touch()\nprint("1.1.0")\n',
    )
    replacement = "#!" + sys.executable + '\nprint("1.2.16")\n'
    operation = {
        "missing": "Path(__file__).unlink()",
        "permission": "Path(__file__).chmod(0o644)",
        "broken": "Path(__file__).write_text(" + repr("#!" + sys.executable + '\nprint("Node.js v22.18.0")\n') + ")",
        "healthy": "Path(__file__).write_text(" + repr(replacement) + ")",
    }[outcome]
    executable(
        selected,
        'import sys\nfrom pathlib import Path\nif sys.argv[1] == "--version":\n print("1.2.15")\nelif sys.argv[1] == "upgrade":\n '
        + operation
        + "\n",
    )
    configured = "opencode" if configured_bare else str(selected)
    monkeypatch.setattr(api, "_configured_agent_cli_path", lambda _name: configured)
    # Exercise the real resolver, including basename fallback, but restrict all
    # discovered candidates to this test's two owned installations.
    monkeypatch.setattr(
        api,
        "resolve_cli_path",
        lambda binary, **_: api._resolve_cli_path(
            binary,
            candidate_paths=lambda _name, **_kw: [selected, other],
            include_npm_global=False,
            include_desktop=False,
        ),
    )
    result = api.install_agent("opencode")
    assert not other_marker.exists()
    if outcome == "healthy":
        assert result["ok"] and result["path"] == str(selected)
        assert result["detection"]["version"] == "1.2.16"
        assert c["persist"] == [("opencode", str(selected))]
    else:
        assert not result["ok"] and result["code"] == "cli_readback_failed"
        assert c["persist"] == []
        if outcome in {"missing", "permission"}:
            # Negative witness: the old post-install resolver WOULD choose the
            # unrelated installation, so this case can expose the prior bug.
            assert api.resolve_cli_path(configured) == str(other)


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("outcome", ["missing", "broken", "healthy"])
@pytest.mark.parametrize("version", [None, "0.162.1"])
def test_codex_npm_certifies_its_declared_prefix_not_an_old_healthy_cli(
    native_case, monkeypatch, fresh, outcome, version
):
    c = native_case
    calls = c["home"] / "npm-install-args.json"
    old = executable(c["home"] / "old/lib/node_modules/@openai/codex/bin/codex.js", 'print("codex-cli 0.161.0")\n')
    destination = c["home"] / ".local/bin/codex"
    installed = c["home"] / "npm-finished"
    replacement = (
        "#!"
        + sys.executable
        + "\nprint("
        + repr("codex-cli 0.162.1" if outcome == "healthy" else "Node.js v22.18.0")
        + ")\n"
    )
    install_body = (
        f'p=Path(os.environ["NPM_CONFIG_PREFIX"])/"bin/codex";p.parent.mkdir(parents=True,exist_ok=True);p.write_text({replacement!r});p.chmod(0o755)\n'
        if outcome != "missing"
        else ""
    )
    npm = executable(
        c["tools"] / "npm",
        'import os,sys\nfrom pathlib import Path\nif sys.argv[1:]==["config","get","prefix"]:\n print("/fixture/different-global-prefix");sys.exit(0)\n'
        + install_body
        + f"Path({str(calls)!r}).write_text(__import__('json').dumps(sys.argv[1:]))\n"
        + f"Path({str(installed)!r}).touch()\n",
    )
    configured = "codex" if fresh else str(old)
    monkeypatch.setattr(api, "_configured_agent_cli_path", lambda _name: configured)
    monkeypatch.setattr(api, "_candidate_cli_paths", lambda *_args, **_kwargs: [])
    monkeypatch.setenv("PATH", c["env"]["PATH"])

    def resolve(binary, **_):
        if binary == "npm":
            return str(npm)
        if binary == "brew":
            return None
        if fresh and not installed.exists():
            return None
        return str(old) if binary in {"codex", str(old)} else None

    monkeypatch.setattr(api, "resolve_cli_path", resolve)
    result = api.install_agent("codex", version=version)
    assert installed.exists()
    # A pinned upgrade installs that release, not whatever npm calls latest by then.
    spec = f"@openai/codex@{version}" if version else "@openai/codex"
    assert json.loads(calls.read_text()) == ["install", "-g", spec]
    assert old.exists()  # the old selected/discovered CLI is still healthy
    assert api.resolve_cli_path("codex") == str(old)  # old fallback would certify it
    if outcome == "healthy":
        assert result["ok"] and result["path"] == str(destination)
        assert result["detection"]["version"] == "0.162.1"
        assert c["persist"] == [("codex", str(destination))]
    else:
        assert not result["ok"] and result["code"] == "cli_readback_failed"
        assert c["persist"] == []


def test_bare_nonexecutable_npm_cli_outside_home_and_path_keeps_its_prefix(native_case, monkeypatch):
    from vibe import cli_paths

    c = native_case
    prefix = c["home"].parent / "custom npm-global prefix"
    pkg = prefix / "lib/node_modules/@anthropic-ai/claude-code"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"name": "@anthropic-ai/claude-code"}))
    forbidden = c["home"] / "damaged-cli-was-executed"
    target = executable(pkg / "cli.js", f"from pathlib import Path\nPath({str(forbidden)!r}).touch()\n")
    target.chmod(0o644)
    launcher = prefix / "bin/claude"
    launcher.parent.mkdir()
    launcher.symlink_to(target)
    calls = c["home"] / "repair-prefix.json"
    replacement = "#!" + sys.executable + '\nprint("2.1.34 (Claude Code)")\n'
    npm = executable(
        c["tools"] / "npm",
        f"import sys,json\nfrom pathlib import Path\nPath({str(calls)!r}).write_text(json.dumps(sys.argv[1:]))\np=Path({str(target)!r});p.write_text({replacement!r});p.chmod(0o755)\n",
    )
    monkeypatch.setenv("PATH", c["env"]["PATH"])
    monkeypatch.setattr(api, "_configured_agent_cli_path", lambda _name: "claude")
    monkeypatch.setattr(cli_paths, "_npm_global_binary_candidates", lambda name: [launcher] if name == "claude" else [])
    observed_flags = []

    def candidates(name, *, include_npm_global=True):
        observed_flags.append(include_npm_global)
        return [launcher] if name == "claude" and include_npm_global else []

    monkeypatch.setattr(api, "_candidate_cli_paths", candidates)

    def resolve(binary, **_):
        if binary == "npm":
            return str(npm)
        return api._resolve_cli_path(binary, candidate_paths=candidates, include_desktop=False)

    monkeypatch.setattr(api, "resolve_cli_path", resolve)
    detection = api.detect_cli("claude", "claude")
    assert detection["found"] and detection["status"] == "broken" and detection["path"] == str(launcher)
    assert True in observed_flags and not forbidden.exists()
    result = api.install_agent("claude")
    assert result["ok"] and result["path"] == str(launcher)
    assert json.loads(calls.read_text()) == [
        "install",
        "--global",
        "--prefix",
        str(prefix),
        "@anthropic-ai/claude-code",
    ]
    assert c["persist"] == [("claude", str(launcher))] and not forbidden.exists()


def test_cli_path_resolution_defers_npm_prefix_until_standard_candidates_fail(tmp_path, monkeypatch):
    standard = executable(tmp_path / "standard" / "codex", 'print("codex-cli 0.162.1")\n')
    npm_candidate = tmp_path / "npm-prefix" / "bin" / "codex"
    observed_flags = []

    def candidates(name, *, include_npm_global=True):
        observed_flags.append(include_npm_global)
        return [standard] if not include_npm_global else [standard, npm_candidate]

    monkeypatch.setattr(cli_paths, "_candidate_cli_paths", candidates)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))

    assert cli_paths.resolve_cli_path("codex", candidate_paths=candidates, include_desktop=False) == str(standard)
    assert observed_flags == [False]


@pytest.mark.parametrize(
    "output",
    [
        "INFO refreshing models\n1.1.36\n",
        "INFO loading models\nINFO refreshing models\nopencode 1.1.36\n",
    ],
)
def test_opencode_version_accepts_prefixed_informational_output(output):
    assert cli_version("opencode", output, "") == "1.1.36"


@pytest.mark.parametrize(
    "output",
    [
        "Error: refresh failed 1.1.36\n1.1.36\n",
        "INFO refreshing models\nError: refresh failed\n1.1.36\n",
    ],
)
def test_opencode_version_rejects_non_informational_prefix(output):
    assert cli_version("opencode", output, "") is None


def test_unknown_installation_is_not_migrated(tmp_path):
    path = executable(tmp_path / "custom-claude", 'print("broken")\n')
    assert identify_repair_source("claude", str(path), home=tmp_path) is None


def test_path_isolation_removes_equivalent_directory_entries_only(tmp_path):
    installed = tmp_path / "native"
    installed.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(installed, target_is_directory=True)
    original = {
        "PATH": os.pathsep.join((str(installed), str(alias), "/usr/bin")),
        "BASH_ENV": "untrusted",
        "KEEP": "yes",
    }
    result = native_bootstrap_env(original, hide_directory=installed)
    assert result == {"PATH": "/usr/bin", "KEEP": "yes"} and "BASH_ENV" in original


@pytest.mark.parametrize("outcome", ["missing", "broken", "healthy"])
def test_windows_npm_upgrade_declares_active_prefix_and_never_certifies_old_cli(tmp_path, monkeypatch, outcome):
    """A real npm child and real target children exercise the Windows contract on POSIX."""
    home = tmp_path / "isolated home"
    tools = tmp_path / "tools"
    tools.mkdir()
    prefix_a = home / "npm-prefix-a"
    prefix_b = home / "npm-prefix-b"
    old_marker = home / "old-probed"
    new_marker = home / "new-probed"
    old = executable(
        prefix_a / "codex.cmd",
        f'from pathlib import Path\nPath({str(old_marker)!r}).touch()\nprint("codex-cli 0.161.0")\n',
    )
    replacement = (
        "#!"
        + sys.executable
        + "\nfrom pathlib import Path\nPath("
        + repr(str(new_marker))
        + ").touch()\n"
        + (
            'print("codex-cli 0.162.1")\n'
            if outcome == "healthy"
            else 'print("Node.js v22.18.0")\n'
            if outcome == "broken"
            else ""
        )
    )
    npm = executable(
        tools / "npm",
        f'import sys\nfrom pathlib import Path\nif sys.argv[1:] == ["config", "get", "prefix"]:\n print({str(prefix_b)!r}); raise SystemExit(0)\nif sys.argv[1:] == ["install", "-g", "@openai/codex"]:\n p=Path({str(prefix_b / "codex.cmd")!r}); p.parent.mkdir(parents=True,exist_ok=True)\n'
        + (f"p.write_text({replacement!r}); p.chmod(0o755)\n" if outcome != "missing" else "")
        + "raise SystemExit(0)\n",
    )
    env = fixture_env(tmp_path)
    env.update(HOME=str(home), AVIBE_HOME=str(home / ".avibe"))
    monkeypatch.setenv("AVIBE_HOME", str(home / ".avibe"))
    monkeypatch.setattr(api.paths, "get_state_dir", lambda: home / "state")
    env["NPM_CONFIG_PREFIX"] = str(prefix_b)
    monkeypatch.setattr(api, "_command_env_for", lambda _path: dict(env))
    monkeypatch.setattr(api, "_configured_agent_cli_path", lambda _name: "codex")
    monkeypatch.setattr(api, "_is_npm_codex_install", lambda _path: True)
    monkeypatch.setattr(api, "_is_homebrew_codex_install", lambda _path: False)
    monkeypatch.setattr(api, "_npm_prefix_for_existing_codex_install", lambda *_args: None)
    monkeypatch.setattr(
        api,
        "resolve_cli_path",
        lambda name, **_kwargs: str(old) if name == "codex" else str(npm) if name == "npm" else None,
    )
    persisted = []
    monkeypatch.setattr(api, "_persist_agent_cli_path", lambda _name, path: persisted.append(path))
    observed = []

    def observe(binary, backend):
        path = str(old) if binary == "codex" else binary
        observed.append(path)
        if not Path(path).is_file() or not os.access(path, os.X_OK):
            status = "missing" if not Path(path).exists() else "broken"
            return api.CliHealth(False, path if Path(path).exists() else None, status).payload()
        result = subprocess.run([path, "--version"], env=env, capture_output=True, text=True, check=False)
        version = cli_version(backend, result.stdout, result.stderr) if result.returncode == 0 else None
        return api.CliHealth(
            True, path, "ok" if version else "broken", version=version, reason=None if version else "probe_failed"
        ).payload()

    monkeypatch.setattr(api, "detect_cli", observe)
    observe(str(old), "codex")
    result = api._run_install_command(
        "codex",
        [str(npm), "install", "-g", "@openai/codex"],
        lambda value: value,
        mode="upgrade",
        env=env,
        expected_path=str(prefix_b / "codex.cmd"),
    )
    target = str(prefix_b / "codex.cmd")
    assert observed == [str(old), target]
    assert old_marker.exists() and new_marker.exists() is (outcome != "missing")
    if outcome == "healthy":
        assert result["ok"] and result["path"] == target and persisted == [target]
    else:
        assert not result["ok"] and result["code"] == "cli_readback_failed" and persisted == []
    assert result.get("detection", {}).get("path") in {target, None}


def test_windows_npm_install_env_declares_prefix_from_real_npm_child(tmp_path, monkeypatch):
    prefix = tmp_path / "active-prefix"
    npm = executable(
        tmp_path / "npm",
        f'import sys\nif sys.argv[1:] == ["config", "get", "prefix"]: print({str(prefix)!r}); raise SystemExit(0)\nraise SystemExit(2)\n',
    )
    monkeypatch.setattr(api.os, "name", "nt", raising=False)
    monkeypatch.setattr(api, "_command_env_for", lambda _path: {"PATH": str(tmp_path)})
    observed = subprocess.run([str(npm), "config", "get", "prefix"], env={"PATH": str(tmp_path)}, capture_output=True, text=True, check=True)
    monkeypatch.setattr(api, "_npm_prefix_for", lambda _path: Path(observed.stdout.strip()))
    env = api._codex_npm_install_env(str(npm))
    normalized_prefix = env["NPM_CONFIG_PREFIX"].replace("\\", "/")
    assert env is not None and normalized_prefix == str(prefix)
    assert env["PATH"].split(os.pathsep)[0].replace("\\", "/") == str(prefix)
    assert normalized_prefix + "/codex.cmd" == str(prefix / "codex.cmd")


def test_windows_npm_prefix_query_failure_fails_closed(tmp_path, monkeypatch):
    npm = executable(tmp_path / "npm", "import sys\nraise SystemExit(1)\n")
    monkeypatch.setattr(api.os, "name", "nt", raising=False)
    monkeypatch.setattr(api, "_command_env_for", lambda _path: {"PATH": str(tmp_path)})
    assert api._codex_npm_install_env(str(npm)) is None
    assert api._declared_codex_npm_target(None) is None


def test_cli_discovery_preserves_path_before_npm_for_presence_and_run(tmp_path, monkeypatch):
    path_dir = tmp_path / "path-bin"
    npm_dir = tmp_path / "npm-bin"
    path_cli = path_dir / "codex"
    npm_cli = npm_dir / "codex"
    path_dir.mkdir()
    npm_dir.mkdir()
    path_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    npm_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    path_cli.chmod(0o644)
    npm_cli.chmod(0o644)
    monkeypatch.setenv("PATH", str(path_dir))

    def candidates(binary, *, include_npm_global=True):
        assert binary == "codex"
        return [npm_cli] if include_npm_global else []

    monkeypatch.setattr(api, "_candidate_cli_paths", candidates)
    assert api.resolve_cli_path("codex") is None
    backend, detected = api._cli_detection_target("codex", "codex")
    assert backend == "codex" and detected == str(path_cli.absolute())


def _fake_homebrew_cask_run(brew: Path, prefix: Path, *, version: str = "1.2.3", query_ok: bool = True):
    def fake_run(cmd, **_kwargs):
        if cmd == [str(brew), "--prefix"]:
            return SimpleNamespace(returncode=0, stdout=str(prefix) + "\n", stderr="")
        if cmd == [str(brew), "info", "--cask", "--json=v2", "--installed", "codex"]:
            if not query_ok:
                return SimpleNamespace(returncode=1, stdout="", stderr="metadata unavailable")
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"casks": [{"token": "codex", "installed": version}]}),
                stderr="",
            )
        raise AssertionError(f"unexpected Homebrew command: {cmd!r}")

    return fake_run


def test_homebrew_symlinked_stable_bin_target_is_attributable(tmp_path, monkeypatch):
    prefix = tmp_path / "homebrew"
    brew_repo = executable(prefix / "Homebrew/bin/brew", "#!/bin/sh\n")
    brew = prefix / "bin/brew"
    brew.parent.mkdir(parents=True, exist_ok=True)
    brew.symlink_to(brew_repo)
    payload = prefix / "Caskroom/codex/1.2.3/libexec"
    target = executable(payload / "codex", 'print("codex-cli 0.162.1")\n')
    launcher = prefix / "bin/codex"
    launcher.symlink_to(target)

    monkeypatch.setattr(api.subprocess, "run", _fake_homebrew_cask_run(brew, prefix))
    assert api._homebrew_codex_target(str(brew), str(launcher)) == str(launcher)


def test_homebrew_codex_attribution_fails_closed_for_ambiguous_targets(tmp_path, monkeypatch):
    prefix = tmp_path / "homebrew"
    brew = executable(prefix / "bin/brew", "#!/bin/sh\n")
    payload = prefix / "Caskroom/codex/1.2.3/libexec"
    target = executable(payload / "codex", 'print("codex-cli 0.162.1")\n')
    outside = executable(tmp_path / "outside/codex", 'print("codex-cli 0.162.1")\n')
    regular = prefix / "bin/codex"
    regular.parent.mkdir(parents=True, exist_ok=True)
    regular.write_text("#!/bin/sh\n", encoding="utf-8")
    regular.chmod(0o755)
    other_prefix = tmp_path / "other-homebrew"
    other_target = executable(other_prefix / "Caskroom/codex/1.2.3/libexec/codex", 'print("codex-cli 0.162.1")\n')
    other_launcher = other_prefix / "bin/codex"
    other_launcher.parent.mkdir(parents=True, exist_ok=True)
    other_launcher.symlink_to(other_target)
    nested = prefix / "bin/nested/codex"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.symlink_to(target)
    unrelated_target = executable(prefix / "Caskroom/other/1.2.3/libexec/codex", 'print("codex-cli 0.162.1")\n')

    monkeypatch.setattr(api.subprocess, "run", _fake_homebrew_cask_run(brew, prefix))
    assert api._homebrew_codex_target(str(brew), str(regular)) is None
    assert api._homebrew_codex_target(str(brew), str(other_launcher)) is None
    assert api._homebrew_codex_target(str(brew), str(nested)) is None
    regular.unlink()
    regular.symlink_to(outside)
    assert api._homebrew_codex_target(str(brew), str(regular)) is None
    regular.unlink()
    regular.symlink_to(unrelated_target)
    assert api._homebrew_codex_target(str(brew), str(regular)) is None
    monkeypatch.setattr(api, "resolve_cli_path", lambda name, **_kwargs: str(brew) if name == "brew" else None)
    monkeypatch.setattr(api, "_is_npm_codex_install", lambda _path: False)
    monkeypatch.setattr(api, "_codex_cli_supports_update", lambda _path: pytest.fail("ambiguous target must not self-update"))
    result = api._codex_upgrade_command(str(regular))
    assert result["code"] == "cli_target_unknown"
    assert api._codex_upgrade_command(str(other_launcher))["code"] == "cli_target_unknown"
    assert api._codex_upgrade_command(str(nested))["code"] == "cli_target_unknown"


@pytest.mark.parametrize("query_ok", [True, False], ids=["installed", "metadata-unknown"])
def test_codex_custom_self_update_ignores_unrelated_homebrew_cask(tmp_path, monkeypatch, query_ok):
    prefix = tmp_path / "homebrew"
    brew = executable(prefix / "bin/brew", "#!/bin/sh\n")
    custom = executable(tmp_path / "custom/bin/codex", 'print("codex-cli 0.162.1")\n')

    monkeypatch.setattr(api.subprocess, "run", _fake_homebrew_cask_run(brew, prefix, query_ok=query_ok))
    monkeypatch.setattr(api, "resolve_cli_path", lambda name, **_kwargs: str(brew) if name == "brew" else None)
    monkeypatch.setattr(api, "_is_npm_codex_install", lambda _path: False)
    monkeypatch.setattr(api, "_codex_cli_supports_update", lambda path: path == str(custom))

    assert api._codex_upgrade_command(str(custom)) == ([str(custom), "update"], None)


def test_homebrew_codex_malformed_metadata_refuses_upgrade(tmp_path, monkeypatch):
    prefix = tmp_path / "homebrew"
    brew = executable(prefix / "bin/brew", "#!/bin/sh\n")
    launcher = executable(prefix / "bin/codex", 'print("codex-cli 0.162.1")\n')

    def fake_run(cmd, **_kwargs):
        if cmd == [str(brew), "--prefix"]:
            return SimpleNamespace(returncode=0, stdout=str(prefix) + "\n", stderr="")
        if cmd == [str(brew), "info", "--cask", "--json=v2", "--installed", "codex"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"casks": [{"token": "codex", "installed": "../escape"}]}),
                stderr="",
            )
        raise AssertionError(f"unexpected Homebrew command: {cmd!r}")

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    monkeypatch.setattr(api, "resolve_cli_path", lambda name, **_kwargs: str(brew) if name == "brew" else None)
    monkeypatch.setattr(api, "_is_npm_codex_install", lambda _path: False)
    monkeypatch.setattr(api, "_codex_cli_supports_update", lambda _path: pytest.fail("malformed metadata must not self-update"))

    result = api._codex_upgrade_command(str(launcher))
    assert result["code"] == "cli_target_unknown"


def test_homebrew_codex_metadata_query_failure_refuses_upgrade(tmp_path, monkeypatch):
    prefix = tmp_path / "homebrew"
    brew = executable(prefix / "bin/brew", "#!/bin/sh\n")
    launcher = executable(prefix / "bin/codex", 'print("codex-cli 0.162.1")\n')
    monkeypatch.setattr(api.subprocess, "run", _fake_homebrew_cask_run(brew, prefix, query_ok=False))
    monkeypatch.setattr(api, "resolve_cli_path", lambda name, **_kwargs: str(brew) if name == "brew" else None)
    monkeypatch.setattr(api, "_is_npm_codex_install", lambda _path: False)
    monkeypatch.setattr(api, "_codex_cli_supports_update", lambda _path: pytest.fail("ambiguous target must not self-update"))
    result = api._codex_upgrade_command(str(launcher))
    assert result["code"] == "cli_target_unknown"


def test_codex_homebrew_upgrade_plan_binds_the_stable_launcher(tmp_path, monkeypatch):
    prefix = tmp_path / "homebrew"
    brew = executable(prefix / "bin/brew", "#!/bin/sh\n")
    target = executable(prefix / "Caskroom/codex/1.2.3/libexec/codex", 'print("codex-cli 0.162.1")\n')
    launcher = prefix / "bin/codex"
    launcher.symlink_to(target)

    monkeypatch.setattr(api.subprocess, "run", _fake_homebrew_cask_run(brew, prefix))
    monkeypatch.setattr(api, "resolve_cli_path", lambda name, **_kwargs: str(brew) if name == "brew" else None)
    monkeypatch.setattr(api, "_is_npm_codex_install", lambda _path: False)
    plan = api._codex_upgrade_command(str(launcher))
    assert plan == ([str(brew), "upgrade", "--cask", "codex"], None)


def test_codex_homebrew_upgrade_readback_rechecks_identity(tmp_path, monkeypatch):
    prefix = tmp_path / "homebrew"
    brew = executable(prefix / "bin/brew", "#!/bin/sh\n")
    payload = prefix / "Caskroom/codex/1.2.3/libexec"
    target = executable(payload / "codex", 'print("codex-cli 0.162.1")\n')
    escape = executable(tmp_path / "escape/codex", 'print("codex-cli 0.162.1")\n')
    launcher = prefix / "bin/codex"
    launcher.symlink_to(target)
    installer = executable(
        tmp_path / "upgrade",
        f'from pathlib import Path\np=Path({str(launcher)!r}); p.unlink(); p.symlink_to({str(escape)!r})\n',
    )
    monkeypatch.setattr(api.subprocess, "run", _fake_homebrew_cask_run(brew, prefix))
    monkeypatch.setattr(api, "detect_cli", lambda _binary, _backend: api.CliHealth(True, str(launcher), "ok").payload())
    persisted = []
    monkeypatch.setattr(api, "_persist_agent_cli_path", lambda _name, path: persisted.append(path))
    result = api._run_install_command(
        "codex",
        [str(installer)],
        lambda value: value,
        mode="upgrade",
        expected_path=str(launcher),
        readback_validator=lambda path: api._homebrew_codex_target(str(brew), path) == str(launcher),
    )
    assert not result["ok"] and result["code"] == "cli_readback_failed" and persisted == []
