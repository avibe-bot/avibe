"""Disposable-container driver; never run on the developer host.

Execute the released CLI/planner/preflight/install/activation unchanged. Only
update discovery and stopped-runtime preparation are test sinks, not services.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

from github_release_fixture import create_certificate, make_server


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, timeout=600, **kwargs)
    assert result.returncode == 0, f"{command}\n{result.stdout}\n{result.stderr}"
    return result.stdout.strip()


def snapshot(home):
    return {
        str(path.relative_to(home)): (path.read_bytes(), path.lstat().st_ino, path.is_symlink(),
                                    os.readlink(path) if path.is_symlink() else None)
        for base in (home / ".avibe/memory", home / ".vibe_remote/memory")
        for path in base.rglob("*") if path.is_file()
    }


def case():
    import importlib.metadata
    import vibe.cli as cli
    import vibe.upgrade as upgrade

    home = Path.home()
    before = snapshot(home)
    version = os.environ["TARGET_VERSION"]
    tag = os.environ["TARGET_TAG"]
    core_url = f"https://github.com/avibe-bot/avibe/releases/download/{tag}/avibe_os-{version}-py3-none-any.whl"
    bridge_url = f"https://github.com/avibe-bot/avibe/releases/download/{tag}/avibe_memory-{version}-py3-none-any.whl"
    assert importlib.metadata.version("avibe-os") == "3.1.0"
    installed = os.environ["CASE_SHAPE"] == "installed"
    assert upgrade.memory_package_installed() is installed
    # The released parser may normalize the obsolete flag during recovery;
    # the enabled case is represented by its persisted fixture, while the
    # installed case is represented by the companion distribution.
    assert isinstance(upgrade.configured_memory_enabled(), bool)
    os.environ["VIBE_UPGRADE_PACKAGE_SPEC"] = core_url
    cli.get_latest_version = lambda: {"error": None, "has_update": True, "latest": version}
    if os.environ["CASE_SHAPE"] == "enabled":
        # Exercise the released enabled branch directly; config recovery is
        # separately covered by the persisted fixture and must not hide it.
        cli.configured_memory_enabled = lambda: True
    cli._runtime_process_was_running = lambda: False
    cli._prepare_show_runtime_after_install = lambda *_: None
    cli.schedule_restart = lambda **_: (_ for _ in ()).throw(AssertionError("no service restart"))
    # Observe, without rewriting, the real old planner and subprocesses.
    original_plan = cli.build_upgrade_plan
    observed = []

    def plan(**kwargs):
        result = original_plan(**kwargs)
        preflight_text = " ".join(result.preflight_command or []) + " " + " ".join(result.preflight_fallback_command or [])
        assert bridge_url in preflight_text, preflight_text
        assert result.method == os.environ["CASE_METHOD"]
        observed.append(result)
        return result

    cli.build_upgrade_plan = plan
    assert cli.cmd_upgrade() == 0
    assert len(observed) == 1
    selected = observed[0]
    python = (str(upgrade._candidate_python(selected.activation.candidate_launcher))
              if selected.activation else sys.executable)
    check = '''
import importlib.metadata as m, importlib.util as u, json, pathlib
assert m.version("avibe-os") == VERSION
d = m.distribution("avibe-memory")
assert d.version == VERSION and not d.requires
assert u.find_spec("avibe_memory") is None
assert u.find_spec("vibe.memory_contract") is None
assert not any(str(p).endswith("memory_runtime_manifest.json") for p in d.files)
assert json.loads(d.read_text("direct_url.json"))["url"] == URL
from config.v2_config import V2Config
c = V2Config.load()
c.save()
assert "memory" not in json.loads((pathlib.Path.home()/".avibe/config/config.json").read_text())
'''.replace("VERSION", repr(version)).replace("URL", repr(bridge_url))
    run([python, "-I", "-c", check], cwd="/tmp")
    assert snapshot(home) == before
    print(json.dumps({"method": selected.method, "shape": os.environ["CASE_SHAPE"],
                      "version": version, "bridge": bridge_url, "sentinels": "unchanged"}))


def main():
    assert Path("/avibe-bridge-container").is_file(), "container-only probe"
    if "--case" in sys.argv:
        case()
        return
    fixtures = Path("/fixtures")
    core = next(fixtures.glob("avibe_os-*.whl"))
    version = core.name.split("-")[1]
    tag = f"v{version}"
    uv = "/opt/probe/bin/uv"
    cases = []
    for method in ("pip", "uv"):
        for shape in ("installed", "enabled"):
            home = Path(f"/cases/{method}-{shape}")
            home.mkdir(parents=True)
            env = {**os.environ, "HOME": str(home), "AVIBE_HOME": str(home / ".avibe"),
                   "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
                   "XDG_CACHE_HOME": str(home / ".cache"), "XDG_STATE_HOME": str(home / ".local/state"),
                   "UV_CACHE_DIR": "/cache/uv", "UV_TOOL_DIR": str(home / ".local/share/uv/tools"),
                   "UV_TOOL_BIN_DIR": str(home / ".local/bin"), "UV_PYTHON_DOWNLOADS": "never",
                   "UV_PYTHON": "/usr/bin/python3", "CASE_METHOD": method, "CASE_SHAPE": shape,
                   "TARGET_VERSION": version, "TARGET_TAG": tag,
                   "PATH": f"/opt/probe/bin:{home}/.local/bin:/usr/bin:/bin"}
            old = fixtures / "old/avibe_os-3.1.0-py3-none-any.whl"
            companion = fixtures / "old/avibe_memory-3.1.0-py3-none-any.whl"
            if method == "pip":
                environment = home / "venv"
                run(["/usr/bin/python3", "-m", "venv", str(environment)], env=env)
                python = str(environment / "bin/python")
                run([uv, "pip", "install", "--python", python, str(old),
                     *([str(companion)] if shape == "installed" else [])], env=env)
                env["PATH"] = f"{environment}/bin:" + env["PATH"]
            else:
                run([uv, "tool", "install", str(old),
                     *(["--with", str(companion)] if shape == "installed" else [])], env=env)
                python = str(home / ".local/share/uv/tools/avibe-os/bin/python")
            config = home / ".avibe/config/config.json"
            config.parent.mkdir(parents=True)
            run([python, "-c", (
                "from config.v2_config import V2Config; "
                "c=V2Config.default(); c.runtime.default_cwd='" + str(home) + "'; c.save()"
            )], env=env)
            payload = json.loads(config.read_text())
            payload.setdefault("memory", {})["enabled"] = shape == "enabled"
            config.write_text(json.dumps(payload))
            for base in (home / ".avibe/memory", home / ".vibe_remote/memory"):
                base.mkdir(parents=True)
                (base / "sentinel.bin").write_bytes(b"\x00\xffold user data\n")
                (base / "sentinel-link").symlink_to("sentinel.bin")
            cases.append((python, env))
    root = Path("/release-server")
    release = root / "avibe-bot/avibe/releases/download" / tag
    release.mkdir(parents=True)
    for asset in (core, next(fixtures.glob("avibe_memory-*.whl"))):
        shutil.copy2(asset, release)
    ca, cert, key = create_certificate(Path("/certificates"))
    trust = Path("/certificates/trust.pem")
    trust.write_bytes(Path("/etc/ssl/certs/ca-certificates.crt").read_bytes() + ca.read_bytes())
    # This container alone owns its hosts/trust configuration; no host changes.
    with Path("/etc/hosts").open("a") as stream:
        stream.write("\n127.0.0.1 github.com\n")
    with make_server(root, cert, key, port=443) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for python, env in cases:
                env.update({"SSL_CERT_FILE": str(trust), "REQUESTS_CA_BUNDLE": str(trust),
                            "PIP_CERT": str(trust), "UV_NATIVE_TLS": "true", "NO_PROXY": "*"})
                print(run([python, str(fixtures / "retired_updater_probe.py"), "--case"], env=env, cwd="/tmp"), flush=True)
        finally:
            server.shutdown()
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
