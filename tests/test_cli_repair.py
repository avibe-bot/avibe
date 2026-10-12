"""Real child-shell repair boundaries; no user CLI/package manager is changed."""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from vibe import api
from vibe.cli_health import probe_cli
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
def test_broken_npm_cli_is_repaired_by_its_prefix_not_self_update(
    native_case, monkeypatch, backend, package, version_output
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
    result = api.install_agent(backend)
    assert result["ok"], result
    assert json.loads(calls.read_text()) == ["install", "--global", "--prefix", str(prefix), package]
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
def test_codex_npm_certifies_its_declared_prefix_not_an_old_healthy_cli(native_case, monkeypatch, fresh, outcome):
    c = native_case
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
    result = api.install_agent("codex")
    assert installed.exists()
    assert old.exists()  # the old selected/discovered CLI is still healthy
    assert api.resolve_cli_path("codex") == str(old)  # old fallback would certify it
    if outcome == "healthy":
        assert result["ok"] and result["path"] == str(destination)
        assert result["detection"]["version"] == "0.162.1"
        assert c["persist"] == [("codex", str(destination))]
    else:
        assert not result["ok"] and result["code"] == "cli_readback_failed"
        assert c["persist"] == []


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
