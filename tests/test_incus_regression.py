from __future__ import annotations

import argparse
import io
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "incus_regression.py"
SPEC = importlib.util.spec_from_file_location("incus_regression", SCRIPT_PATH)
incus_regression = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = incus_regression
SPEC.loader.exec_module(incus_regression)


def test_master_target_uses_stable_project_instance_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REGRESSION_PORT", raising=False)
    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="master",
            slug=None,
            host_port=None,
            ui_host="127.0.0.1",
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        Path("/tmp/repo"),
        dry_run=True,
    )

    assert target.project == "avr-master"
    assert target.instance == "avibe-master"
    assert target.host_port == 15130


def test_master_target_uses_env_host_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGRESSION_PORT", "15131")

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="master",
            slug=None,
            host_port=None,
            ui_host="127.0.0.1",
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        Path("/tmp/repo"),
        dry_run=True,
    )

    assert target.host_port == 15131


def test_master_target_ignores_legacy_env_host_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REGRESSION_PORT", raising=False)
    monkeypatch.setenv("THREE_REGRESSION_PORT", "15132")

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="master",
            slug=None,
            host_port=None,
            ui_host="127.0.0.1",
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        Path("/tmp/repo"),
        dry_run=True,
    )

    assert target.host_port == 15130


def test_master_target_uses_env_bind_host_after_env_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGRESSION_PORT_BIND_HOST", "0.0.0.0")

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="master",
            slug=None,
            host_port=None,
            ui_host=None,
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        Path("/tmp/repo"),
        dry_run=True,
    )

    assert target.ui_host == "0.0.0.0"


def test_master_target_prefers_port_bind_host_over_container_ui_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGRESSION_UI_HOST", "127.0.0.1")
    monkeypatch.setenv("REGRESSION_PORT_BIND_HOST", "0.0.0.0")

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="master",
            slug=None,
            host_port=None,
            ui_host=None,
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        Path("/tmp/repo"),
        dry_run=True,
    )

    assert target.ui_host == "0.0.0.0"


def test_master_target_accepts_legacy_ui_host_as_bind_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REGRESSION_PORT_BIND_HOST", raising=False)
    monkeypatch.setenv("REGRESSION_UI_HOST", "0.0.0.0")

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="master",
            slug=None,
            host_port=None,
            ui_host=None,
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        Path("/tmp/repo"),
        dry_run=True,
    )

    assert target.ui_host == "0.0.0.0"


def test_worktree_target_slug_includes_path_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(incus_regression, "branch_name", lambda repo_root: "feature/Show Runtime")
    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="worktree",
            slug=None,
            host_port=15234,
            ui_host="127.0.0.1",
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        Path("/tmp/repo-a"),
        dry_run=True,
    )

    assert target.project.startswith("avr-wt-feature-show-runtime-")
    assert target.instance.startswith("avibe-wt-feature-show-runtime-")
    assert target.host_port == 15234


def test_remote_worktree_target_skips_local_port_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(incus_regression, "branch_name", lambda repo_root: "feature/demo")
    monkeypatch.setattr(incus_regression, "ensure_host_port_available", lambda host, port: (_ for _ in ()).throw(AssertionError("should not preflight remote ports")))

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="worktree",
            slug=None,
            host_port=None,
            ui_host="127.0.0.1",
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15200,
        ),
        Path("/tmp/repo-a"),
        dry_run=False,
        preflight_ports=False,
    )

    assert target.host_port == 15200


def test_worktree_target_reuses_mapped_port_without_allocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / ".runtime" / "incus-regression"
    runtime.mkdir(parents=True)
    (runtime / "worktrees.json").write_text(
        json.dumps({"schema_version": 1, "worktrees": {"demo-branch": {"host_port": 15234}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)
    monkeypatch.setattr(incus_regression, "allocate_worktree_port", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should reuse mapped port")))

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="worktree",
            slug="demo-branch",
            host_port=None,
            ui_host="127.0.0.1",
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        tmp_path,
        dry_run=False,
    )

    assert target.host_port == 15234


def test_worktree_maintenance_target_does_not_allocate_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(incus_regression, "allocate_worktree_port", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not allocate for maintenance")))

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="worktree",
            slug="missing-branch",
            host_port=None,
            ui_host="127.0.0.1",
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        tmp_path,
        dry_run=False,
        allocate_port=False,
        preflight_ports=False,
    )

    assert target.host_port == 0


def test_cloud_init_configures_systemd_service_without_source_code() -> None:
    data = incus_regression.cloud_init_user_data()

    assert "#cloud-config" in data
    assert "name: avibe" in data
    assert "Description=Avibe regression service" in data
    assert "Environment=VIBE_DEPLOYMENT_ENV=regression" in data
    assert "Environment=VIBE_BUILD_METADATA_PATH=/var/lib/avibe-regression/metadata.json" in data
    assert "Environment=AVIBE_ALLOW_DEV_STATE_MIGRATION=1" in data
    assert "EnvironmentFile=-/etc/avibe-regression.env" in data
    assert "ExecStart=/opt/avibe/venv/bin/python scripts/incus_regression_supervisor.py" in data
    assert "Delegate=yes" in data
    assert "MemoryAccounting=yes" in data
    assert "/opt/avibe/source" in data
    assert "/home/avibe/.vibe_remote" in data


def test_project_config_marks_regression_target() -> None:
    target = incus_regression.RegressionTarget(
        target="worktree",
        slug="demo-branch",
        project="avr-wt-demo-branch",
        instance="avibe-wt-demo-branch",
        host_port=15200,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    config = incus_regression.project_create_config(target)

    assert "restricted=true" in config
    assert "restricted.devices.proxy=allow" in config
    assert "user.avibe_regression.target=worktree" in config
    assert "user.avibe_regression.host_port=15200" in config


def test_tenant_exec_exports_regression_guard_override() -> None:
    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    command = " ".join(incus_regression.tenant_exec(target, "/opt/avibe/venv/bin/vibe status"))

    assert "[ ! -f /etc/avibe-regression.env ] || . /etc/avibe-regression.env" in command
    assert "VIBE_DEPLOYMENT_ENV=regression" in command
    assert "AVIBE_ALLOW_DEV_STATE_MIGRATION=1" in command


def test_remote_ref_prefixes_resource_names_only() -> None:
    assert incus_regression.remote_ref("lab", "demo") == "lab:demo"
    assert incus_regression.remote_ref(None, "demo") == "demo"
    assert incus_regression.remote_ref("lab") == "lab:"
    assert incus_regression.optional_remote_ref(None) == []
    assert incus_regression.optional_remote_ref("lab") == ["lab:"]


def test_incus_command_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCUS_CMD", "sudo incus")

    assert incus_regression.incus("info") == ["sudo", "incus", "info"]


def test_require_incus_uses_command_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCUS_CMD", "/custom/incus --debug")
    monkeypatch.setattr(incus_regression.shutil, "which", lambda executable: executable if executable == "/custom/incus" else None)

    incus_regression.require_incus()


def test_require_incus_reports_missing_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCUS_CMD", "/missing/incus")
    monkeypatch.setattr(incus_regression.shutil, "which", lambda executable: None)

    with pytest.raises(incus_regression.RegressionError, match="/missing/incus"):
        incus_regression.require_incus()


def test_default_base_image_alias_is_not_remote_syntax() -> None:
    assert ":" not in incus_regression.DEFAULT_IMAGE


def test_proxy_device_uses_remote_instance_ref() -> None:
    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    args = incus_regression.proxy_device_args(target, remote="lab")

    assert args[3] == "lab:avibe-master"
    assert "listen=tcp:127.0.0.1:15130" in args
    assert "connect=tcp:127.0.0.1:5123" in args


def test_existing_instance_proxy_device_is_refreshed() -> None:
    commands = []

    class RecordingRunner:
        def exists(self, command):
            return True

        def run(self, command, *, check=True, **kwargs):
            commands.append((command, check))
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15131,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.ensure_project_and_instance(
        RecordingRunner(),
        target,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
    )

    rendered = [" ".join(command) for command, _ in commands]
    assert "incus --project avr-master config device remove avibe-master ui" in rendered
    assert any("incus --project avr-master config device add avibe-master ui proxy listen=tcp:127.0.0.1:15131" in command for command in rendered)


def test_build_base_uses_publishable_temp_instance() -> None:
    commands = []

    class RecordingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def run(self, command, *, check=True, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    args = argparse.Namespace(
        dry_run=True,
        remote=None,
        source_image="images:ubuntu/24.04/cloud",
        temp_instance="avibe-regression-base-build",
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
    )

    original_runner = incus_regression.Runner
    try:
        incus_regression.Runner = RecordingRunner
        assert incus_regression.cmd_build_base(args) == 0
    finally:
        incus_regression.Runner = original_runner

    joined = "\n".join(" ".join(command) for command in commands)
    assert "--ephemeral" not in joined
    assert "incus launch images:ubuntu/24.04/cloud avibe-regression-base-build --storage default --network incusbr0" in joined
    assert "https://deb.nodesource.com/setup_20.x" in joined
    assert 'HOME="$avibe_home" npm install -g @anthropic-ai/claude-code @openai/codex' in joined
    assert "https://askill.sh | sh -s -- -b /usr/local/bin" in joined
    assert ".npm-global" in joined
    assert 'ln -sf "$avibe_home/.npm-global/bin/claude" "$avibe_home/.local/bin/claude"' in joined
    assert 'ln -sf "$avibe_home/.npm-global/bin/codex" "$avibe_home/.local/bin/codex"' in joined
    assert 'curl -fsSL https://opencode.ai/install | HOME="$avibe_home" bash -s -- --no-modify-path' in joined
    assert 'ln -sf "$avibe_home/.opencode/bin/opencode" "$avibe_home/.local/bin/opencode"' in joined
    # Backends must not be root-global: the non-root avibe user owns them and self-updates.
    assert "/usr/local/bin/opencode" not in joined
    assert "cloud-init clean --logs || true" in joined
    assert "incus publish avibe-regression-base-build --alias avibe-regression-base-current" in joined


def test_source_exclude_drops_runtime_and_dependency_dirs() -> None:
    assert incus_regression.should_exclude(".runtime/state.json")
    assert incus_regression.should_exclude("ui/node_modules/pkg/index.js")
    assert incus_regression.should_exclude("ui/dist/assets/app.js")
    assert not incus_regression.should_exclude("ui/dist/assets/app.js", include_ui_dist=True)
    assert incus_regression.should_exclude("pkg/__pycache__/x.pyc")
    assert incus_regression.should_exclude(".env")
    assert incus_regression.should_exclude(".env.regression")
    assert incus_regression.should_exclude(".env.three-regression")
    assert incus_regression.should_exclude(".env.e2e")
    assert incus_regression.should_exclude("ui/.env.local")
    assert incus_regression.should_exclude("api/.env.preview.local")
    assert not incus_regression.should_exclude("vibe/ui_server.py")


def test_source_tar_excludes_regression_secret_file(tmp_path: Path) -> None:
    (tmp_path / ".env.regression").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    (tmp_path / "vibe").mkdir()
    (tmp_path / "vibe" / "ui_server.py").write_text("print('ok')\n", encoding="utf-8")

    payload = incus_regression.build_source_tar(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r") as archive:
        names = set(archive.getnames())
        ui_server = archive.extractfile("vibe/ui_server.py")
        assert ui_server is not None
        content = ui_server.read()

    assert ".env.regression" not in names
    assert b"print('ok')" in content


def test_source_tar_excludes_all_local_env_files(tmp_path: Path) -> None:
    (tmp_path / ".env.e2e").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / ".env.preview.local").write_text("SECRET=2\n", encoding="utf-8")
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / ".env.local").write_text("SECRET=3\n", encoding="utf-8")
    (tmp_path / "vibe").mkdir()
    (tmp_path / "vibe" / "ui_server.py").write_text("print('ok')\n", encoding="utf-8")

    payload = incus_regression.build_source_tar(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r") as archive:
        names = set(archive.getnames())

    assert ".env.e2e" not in names
    assert ".env.preview.local" not in names
    assert "ui/.env.local" not in names


def test_source_tar_can_include_existing_ui_dist_when_build_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "ui" / "dist" / "assets").mkdir(parents=True)
    (tmp_path / "ui" / "dist" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    (tmp_path / "ui" / "dist" / "assets" / "app.js").write_text("console.log('ok')\n", encoding="utf-8")

    payload = incus_regression.build_source_tar(tmp_path, include_ui_dist=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r") as archive:
        names = set(archive.getnames())

    assert "ui/dist/index.html" in names
    assert "ui/dist/assets/app.js" in names


def test_sync_source_clears_stale_files_even_without_clean(tmp_path: Path) -> None:
    commands = []

    class RecordingRunner:
        dry_run = True

        def run(self, command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.sync_source(RecordingRunner(), target, tmp_path, remote=None, clean=False)

    joined = "\n".join(" ".join(command) for command in commands)
    assert f"find {incus_regression.SOURCE_DIR} -mindepth 1 -maxdepth 1 -exec rm -rf" in joined


def test_ui_public_assets_are_part_of_source_fingerprint(tmp_path: Path) -> None:
    (tmp_path / "ui" / "src").mkdir(parents=True)
    (tmp_path / "ui" / "public").mkdir(parents=True)
    (tmp_path / "ui" / "public" / "push-sw.js").write_text("one\n", encoding="utf-8")

    before = incus_regression.compute_fingerprints(tmp_path)["ui_source"]
    (tmp_path / "ui" / "public" / "push-sw.js").write_text("two\n", encoding="utf-8")
    after = incus_regression.compute_fingerprints(tmp_path)["ui_source"]

    assert before != after


def test_runtime_env_payload_maps_show_runtime_and_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGRESSION_SHOW_RUNTIME_GITHUB_REF", "main")
    monkeypatch.setenv("REGRESSION_SLACK_CHANNEL", "C123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    payload = incus_regression.runtime_env_payload().decode()

    assert "SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0.dev0" in payload
    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AVIBE_OS=0.0.0.dev0" in payload
    assert "AVIBE_ALLOW_DEV_STATE_MIGRATION=1" in payload
    assert "VIBE_SHOW_RUNTIME_SOURCE=github-source" in payload
    assert "VIBE_SHOW_RUNTIME_GITHUB_REF=main" in payload
    assert "REGRESSION_SLACK_CHANNEL=C123" in payload
    assert "OPENAI_API_KEY=sk-test" in payload


def test_runtime_env_payload_ignores_legacy_regression_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REGRESSION_SHOW_RUNTIME_GITHUB_REF", raising=False)
    monkeypatch.delenv("REGRESSION_SLACK_CHANNEL", raising=False)
    monkeypatch.setenv("THREE_REGRESSION_SHOW_RUNTIME_GITHUB_REF", "legacy-ref")
    monkeypatch.setenv("THREE_REGRESSION_SLACK_CHANNEL", "CLEGACY")

    payload = incus_regression.runtime_env_payload().decode()

    assert "VIBE_SHOW_RUNTIME_GITHUB_REF=main" in payload
    assert "REGRESSION_SLACK_CHANNEL=CLEGACY" not in payload
    assert "THREE_REGRESSION_SLACK_CHANNEL" not in payload


def test_runtime_env_payload_forces_container_ui_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGRESSION_UI_HOST", "192.168.2.3")
    monkeypatch.setenv("THREE_REGRESSION_UI_HOST", "10.1.2.3")

    payload = incus_regression.runtime_env_payload().decode()

    assert "REGRESSION_UI_HOST=127.0.0.1" in payload
    assert "REGRESSION_UI_HOST=192.168.2.3" not in payload
    assert "REGRESSION_UI_HOST=10.1.2.3" not in payload


def test_load_env_file_accepts_export_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env.regression"
    env_file.write_text("export REGRESSION_SLACK_CHANNEL=C123\n", encoding="utf-8")
    monkeypatch.delenv("REGRESSION_SLACK_CHANNEL", raising=False)

    loaded = incus_regression.load_env_file(tmp_path, env_file)

    assert loaded == env_file
    assert incus_regression.os.environ["REGRESSION_SLACK_CHANNEL"] == "C123"


def test_load_env_file_ignores_legacy_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env.three-regression"
    env_file.write_text("export THREE_REGRESSION_SLACK_CHANNEL=C123\n", encoding="utf-8")
    monkeypatch.delenv("REGRESSION_SLACK_CHANNEL", raising=False)
    monkeypatch.delenv("THREE_REGRESSION_SLACK_CHANNEL", raising=False)

    loaded = incus_regression.load_env_file(tmp_path, None)

    assert loaded is None
    assert "THREE_REGRESSION_SLACK_CHANNEL" not in incus_regression.os.environ


def test_require_runtime_seed_env_fails_fast_for_blank_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "  ")

    with pytest.raises(SystemExit) as excinfo:
        incus_regression.require_runtime_seed_env()

    assert "ANTHROPIC_API_KEY" in str(excinfo.value)
    assert "OPENAI_API_KEY" in str(excinfo.value)


def test_require_runtime_seed_env_checks_platform_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "REGRESSION_SLACK_BOT_TOKEN",
        "REGRESSION_SLACK_APP_TOKEN",
        "REGRESSION_DISCORD_BOT_TOKEN",
        "REGRESSION_FEISHU_APP_ID",
        "REGRESSION_FEISHU_APP_SECRET",
    ):
        monkeypatch.setenv(key, "set")
    monkeypatch.setenv("REGRESSION_FEISHU_APP_SECRET", "")

    with pytest.raises(SystemExit) as excinfo:
        incus_regression.require_runtime_seed_env()

    assert "REGRESSION_FEISHU_APP_SECRET" in str(excinfo.value)


def test_require_runtime_seed_env_rejects_legacy_platform_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "set")
    monkeypatch.setenv("OPENAI_API_KEY", "set")
    for key in incus_regression.required_platform_seed_envs():
        monkeypatch.delenv(key, raising=False)
        legacy_key = "THREE_" + key
        monkeypatch.setenv(legacy_key, "legacy")

    with pytest.raises(SystemExit) as excinfo:
        incus_regression.require_runtime_seed_env()

    assert "REGRESSION_SLACK_BOT_TOKEN" in str(excinfo.value)


def test_prepare_state_skips_existing_state_without_reset() -> None:
    commands = []

    class RecordingRunner:
        dry_run = False

        def run(self, command, *, check=True, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.run_prepare_state(RecordingRunner(), target, reset_mode="none", remote=None)

    joined = "\n".join(" ".join(command) for command in commands)
    assert "test -f /home/avibe/.avibe/config/config.json" in joined
    assert "prepare_regression.py" not in joined


def test_prepare_state_reseeds_when_reset_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "openai")

    class RecordingRunner:
        dry_run = False

        def run(self, command, *, check=True, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.run_prepare_state(RecordingRunner(), target, reset_mode="config", remote=None)

    joined = "\n".join(" ".join(command) for command in commands)
    assert "rm -rf /home/avibe/.avibe/config /home/avibe/.avibe/state /home/avibe/.avibe/runtime" in joined
    assert "rm -rf /home/avibe/.regression-seed" in joined
    assert "prepare_regression.py" in joined


def test_prepare_state_reset_all_deletes_target_home_before_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "openai")

    class RecordingRunner:
        dry_run = False

        def run(self, command, *, check=True, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.run_prepare_state(RecordingRunner(), target, reset_mode="all", remote=None)

    joined = "\n".join(" ".join(command) for command in commands)
    assert "rm -rf /home/avibe/.avibe /home/avibe/.vibe_remote" in joined
    assert "/home/avibe/.codex" in joined
    assert "ln -sfn /home/avibe/.avibe /home/avibe/.vibe_remote" in joined


def test_guard_paired_master_reset_rejects_remote_access_state() -> None:
    commands = []

    class PairingRunner:
        dry_run = False

        def run(self, command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout='{"state": "paired"}')

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    with pytest.raises(incus_regression.RegressionError, match="pairing state is present"):
        incus_regression.guard_paired_master_reset(
            PairingRunner(),
            target,
            reset_mode="config",
            allow_reset_paired_master=False,
            remote=None,
        )

    joined = "\n".join(" ".join(command) for command in commands)
    assert "/home/avibe/.avibe/config/config.json" in joined


def test_remote_pairing_probe_detects_nested_vibe_cloud_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "remote_access": {
                    "provider": "vibe_cloud",
                    "vibe_cloud": {
                        "enabled": True,
                        "public_url": "https://test-app.avibe.bot",
                        "instance_id": "inst_123",
                        "tunnel_token": "token_123",
                    },
                }
            }
        )
    )

    result = subprocess.run(
        [sys.executable, "-c", incus_regression.remote_pairing_probe_script()],
        check=True,
        capture_output=True,
        env={**os.environ, "AVIBE_REMOTE_PAIRING_CONFIG_PATH": str(config_path)},
        text=True,
    )

    assert json.loads(result.stdout)["state"] == "paired"


def test_remote_pairing_probe_detects_legacy_only_config(tmp_path: Path) -> None:
    missing_new_config = tmp_path / ".avibe" / "config" / "config.json"
    legacy_config = tmp_path / ".vibe_remote" / "config" / "config.json"
    legacy_config.parent.mkdir(parents=True)
    legacy_config.write_text(
        json.dumps(
            {
                "remote_access": {
                    "provider": "vibe_cloud",
                    "vibe_cloud": {
                        "public_url": "https://test-app.avibe.bot",
                    },
                }
            }
        )
    )

    result = subprocess.run(
        [sys.executable, "-c", incus_regression.remote_pairing_probe_script()],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "AVIBE_REMOTE_PAIRING_CONFIG_PATHS": os.pathsep.join([str(missing_new_config), str(legacy_config)]),
        },
        text=True,
    )

    assert json.loads(result.stdout)["state"] == "paired"


def test_guard_paired_master_reset_fails_closed_when_probe_fails() -> None:
    class BrokenProbeRunner:
        dry_run = False

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="venv missing")

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    with pytest.raises(incus_regression.RegressionError, match="could not be verified safely"):
        incus_regression.guard_paired_master_reset(
            BrokenProbeRunner(),
            target,
            reset_mode="config",
            allow_reset_paired_master=False,
            remote=None,
        )


def test_guard_paired_master_reset_fails_closed_when_probe_json_is_invalid() -> None:
    class InvalidJsonRunner:
        dry_run = False

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="not json")

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    with pytest.raises(incus_regression.RegressionError, match="could not be verified safely"):
        incus_regression.guard_paired_master_reset(
            InvalidJsonRunner(),
            target,
            reset_mode="all",
            allow_reset_paired_master=False,
            remote=None,
        )


def test_guard_paired_master_reset_fails_closed_when_config_is_unreadable() -> None:
    class UnreadableConfigRunner:
        dry_run = False

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout='{"state": "unknown"}')

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    with pytest.raises(incus_regression.RegressionError, match="could not be verified safely"):
        incus_regression.guard_paired_master_reset(
            UnreadableConfigRunner(),
            target,
            reset_mode="config",
            allow_reset_paired_master=False,
            remote=None,
        )


def test_guard_paired_master_reset_allows_verified_unpaired_config() -> None:
    class UnpairedRunner:
        dry_run = False

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout='{"state": "unpaired"}')

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.guard_paired_master_reset(
        UnpairedRunner(),
        target,
        reset_mode="config",
        allow_reset_paired_master=False,
        remote=None,
    )


def test_guard_paired_master_reset_allows_explicit_override() -> None:
    class FailingRunner:
        dry_run = False

        def run(self, command, **kwargs):
            raise AssertionError("override should skip remote status probing")

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.guard_paired_master_reset(
        FailingRunner(),
        target,
        reset_mode="all",
        allow_reset_paired_master=True,
        remote=None,
    )


def test_guard_paired_master_reset_ignores_worktree_targets() -> None:
    class FailingRunner:
        dry_run = False

        def run(self, command, **kwargs):
            raise AssertionError("worktree resets are not protected by master pairing guard")

    target = incus_regression.RegressionTarget(
        target="worktree",
        slug="feature",
        project="avr-wt-feature",
        instance="avibe-wt-feature",
        host_port=15200,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.guard_paired_master_reset(
        FailingRunner(),
        target,
        reset_mode="all",
        allow_reset_paired_master=False,
        remote=None,
    )


def test_write_runtime_env_uses_stdin_not_command_line() -> None:
    commands = []
    inputs = []

    class RecordingRunner:
        dry_run = False

        def run(self, command, *, input_bytes=None, **kwargs):
            commands.append(command)
            inputs.append(input_bytes)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.write_runtime_env(RecordingRunner(), target, remote="lab")

    joined_command = " ".join(commands[0])
    assert commands[0][:5] == ["incus", "--project", "avr-master", "exec", "lab:avibe-master"]
    assert "chown root:avibe /etc/avibe-regression.env" in joined_command
    assert "chmod 0640 /etc/avibe-regression.env" in joined_command
    assert b"VIBE_SHOW_RUNTIME_SOURCE" in inputs[0]
    assert "OPENAI_API_KEY" not in joined_command


def test_cleanup_stale_deletes_missing_worktree_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime = repo / ".runtime" / "incus-regression"
    runtime.mkdir(parents=True)
    (runtime / "worktrees.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "worktrees": {
                    "old": {
                        "path": str(tmp_path / "missing"),
                        "project": "avr-wt-old",
                        "instance": "avibe-wt-old",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    commands = []

    class RecordingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def run(self, command, *, check=True, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: repo)
    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)
    monkeypatch.setattr(incus_regression, "Runner", RecordingRunner)

    exit_code = incus_regression.cmd_cleanup_stale(argparse.Namespace(yes=True, dry_run=False, remote=None))

    assert exit_code == 0
    assert ["incus", "--project", "avr-wt-old", "delete", "avibe-wt-old", "--force"] in commands
    payload = json.loads((runtime / "worktrees.json").read_text(encoding="utf-8"))
    assert payload["worktrees"] == {}


def test_up_skips_host_port_preflight_for_existing_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "ui" / "package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "ui" / "src").mkdir()

    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def exists(self, command):
            return command[:4] == ["incus", "--project", "avr-master", "info"]

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    monkeypatch.setattr(incus_regression, "ensure_host_port_available", lambda host, port: (_ for _ in ()).throw(AssertionError("should not preflight existing instance")))
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "stop_service_for_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "write_runtime_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "sync_source", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "run_prepare_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "write_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "restart_and_verify", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "prepare_show_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "update_worktree_mapping", lambda *args, **kwargs: None)

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        reset_mode="none",
    )

    assert incus_regression.cmd_up(args) == 0


def test_up_defers_master_port_preflight_until_after_instance_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def exists(self, command):
            return True

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    def fail_if_resolve_target_preflights(repo_root, ui_host, start, end, *, dry_run, preflight):
        if preflight:
            raise AssertionError("master target resolution must not preflight ports")
        return start

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    monkeypatch.setattr(incus_regression, "allocate_worktree_port", fail_if_resolve_target_preflights)
    monkeypatch.setattr(
        incus_regression,
        "ensure_host_port_available",
        lambda host, port: (_ for _ in ()).throw(AssertionError("should not preflight existing master instance")),
    )
    monkeypatch.setattr(incus_regression, "reserve_worktree_mapping", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "stop_service_for_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "write_runtime_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "sync_source", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "run_prepare_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "write_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "restart_and_verify", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "prepare_show_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "update_worktree_mapping", lambda *args, **kwargs: None)

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        reset_mode="none",
    )

    assert incus_regression.cmd_up(args) == 0


def test_up_checks_host_port_preflight_for_new_local_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class NewRemoteRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def exists(self, command):
            return False

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", NewRemoteRunner)
    preflight_calls = []
    monkeypatch.setattr(incus_regression, "ensure_host_port_available", lambda host, port: preflight_calls.append((host, port)))
    monkeypatch.setattr(incus_regression, "require_runtime_seed_env", lambda: None)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "stop_service_for_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "write_runtime_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "should_seed_state", lambda *args, **kwargs: False)
    monkeypatch.setattr(incus_regression, "sync_source", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "compute_fingerprints", lambda repo_root: {})
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "run_prepare_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "write_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "restart_and_verify", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "prepare_show_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "update_worktree_mapping", lambda *args, **kwargs: None)

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        reset_mode="none",
    )

    assert incus_regression.cmd_up(args) == 0
    assert preflight_calls == [("127.0.0.1", 15130)]


def test_up_checks_seed_env_before_target_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class NewRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def exists(self, command):
            return False

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="")

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)
            if name == "require_runtime_seed_env":
                raise SystemExit("missing")

        return wrapper

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "ensure_host_port_available", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "Runner", NewRunner)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))
    monkeypatch.setattr(incus_regression, "write_runtime_env", record("write_runtime_env"))
    monkeypatch.setattr(incus_regression, "require_runtime_seed_env", record("require_runtime_seed_env"))
    monkeypatch.setattr(incus_regression, "sync_source", record("sync_source"))

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        reset_mode="none",
    )

    with pytest.raises(SystemExit):
        incus_regression.cmd_up(args)

    assert calls == ["require_runtime_seed_env"]


def test_up_checks_platform_seed_env_before_existing_reset_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def exists(self, command):
            return True

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="")

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)
            if name == "require_runtime_seed_env":
                raise SystemExit("missing platform")

        return wrapper

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    monkeypatch.setattr(incus_regression, "require_runtime_seed_env", record("require_runtime_seed_env"))
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        reset_mode="config",
    )

    with pytest.raises(SystemExit):
        incus_regression.cmd_up(args)

    assert calls == ["require_runtime_seed_env"]


def test_up_rejects_paired_master_reset_before_instance_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def exists(self, command):
            return True

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout='{"state": "paired"}')

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)

        return wrapper

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "require_runtime_seed_env", record("require_runtime_seed_env"))
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        reset_mode="config",
        allow_reset_paired_master=False,
    )

    with pytest.raises(incus_regression.RegressionError, match="pairing state is present"):
        incus_regression.cmd_up(args)

    assert calls == ["require_runtime_seed_env"]


def test_up_dry_run_does_not_require_seed_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class DryRunRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def exists(self, command):
            return False

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)
            if name == "require_runtime_seed_env":
                raise AssertionError("dry-run should not require seed secrets")

        return wrapper

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "Runner", DryRunRunner)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))
    monkeypatch.setattr(incus_regression, "write_runtime_env", record("write_runtime_env"))
    monkeypatch.setattr(incus_regression, "require_runtime_seed_env", record("require_runtime_seed_env"))
    monkeypatch.setattr(incus_regression, "sync_source", record("sync_source"))
    monkeypatch.setattr(incus_regression, "compute_fingerprints", lambda repo_root: {})
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", record("update_dependencies_and_build"))
    monkeypatch.setattr(incus_regression, "run_prepare_state", record("run_prepare_state"))
    monkeypatch.setattr(incus_regression, "normalize_runtime_config", record("normalize_runtime_config"))
    monkeypatch.setattr(incus_regression, "write_metadata", record("write_metadata"))
    monkeypatch.setattr(incus_regression, "restart_and_verify", record("restart_and_verify"))
    monkeypatch.setattr(incus_regression, "prepare_show_runtime", record("prepare_show_runtime"))
    monkeypatch.setattr(incus_regression, "update_worktree_mapping", record("update_worktree_mapping"))

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=True,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        reset_mode="none",
    )

    assert incus_regression.cmd_up(args) == 0
    assert "require_runtime_seed_env" not in calls
    assert not (tmp_path / ".runtime" / "incus-regression" / "worktrees.json").exists()


def test_up_stops_old_service_before_mutating_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    env_file = tmp_path / ".env.regression"
    env_file.write_text("OPENAI_API_KEY=set\n", encoding="utf-8")

    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def exists(self, command):
            return True

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)

        return wrapper

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))
    monkeypatch.setattr(incus_regression, "write_runtime_env", record("write_runtime_env"))
    monkeypatch.setattr(incus_regression, "should_seed_state", lambda *args, **kwargs: False)
    monkeypatch.setattr(incus_regression, "sync_source", record("sync_source"))
    monkeypatch.setattr(incus_regression, "compute_fingerprints", lambda repo_root: {})
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", record("update_dependencies_and_build"))
    monkeypatch.setattr(incus_regression, "run_prepare_state", record("run_prepare_state"))
    monkeypatch.setattr(incus_regression, "normalize_runtime_config", record("normalize_runtime_config"))
    monkeypatch.setattr(incus_regression, "write_metadata", record("write_metadata"))
    monkeypatch.setattr(incus_regression, "restart_and_verify", record("restart_and_verify"))
    monkeypatch.setattr(incus_regression, "prepare_show_runtime", record("prepare_show_runtime"))
    monkeypatch.setattr(incus_regression, "update_worktree_mapping", record("update_worktree_mapping"))

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        reset_mode="none",
    )

    assert incus_regression.cmd_up(args) == 0
    assert calls[:3] == ["ensure_project_and_instance", "stop_service_for_update", "write_runtime_env"]
    assert calls.index("sync_source") < calls.index("update_dependencies_and_build")
    assert calls.index("normalize_runtime_config") < calls.index("restart_and_verify")
    assert calls.index("prepare_show_runtime") < calls.index("restart_and_verify")


def test_up_preserves_runtime_env_when_existing_target_has_no_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def exists(self, command):
            return True

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)

        return wrapper

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))
    monkeypatch.setattr(incus_regression, "write_runtime_env", record("write_runtime_env"))
    monkeypatch.setattr(incus_regression, "should_seed_state", lambda *args, **kwargs: False)
    monkeypatch.setattr(incus_regression, "sync_source", record("sync_source"))
    monkeypatch.setattr(incus_regression, "compute_fingerprints", lambda repo_root: {})
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", record("update_dependencies_and_build"))
    monkeypatch.setattr(incus_regression, "run_prepare_state", record("run_prepare_state"))
    monkeypatch.setattr(incus_regression, "normalize_runtime_config", record("normalize_runtime_config"))
    monkeypatch.setattr(incus_regression, "write_metadata", record("write_metadata"))
    monkeypatch.setattr(incus_regression, "restart_and_verify", record("restart_and_verify"))
    monkeypatch.setattr(incus_regression, "prepare_show_runtime", record("prepare_show_runtime"))
    monkeypatch.setattr(incus_regression, "update_worktree_mapping", record("update_worktree_mapping"))

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        reset_mode="none",
    )

    assert incus_regression.cmd_up(args) == 0
    assert "write_runtime_env" not in calls
    assert calls.index("stop_service_for_update") < calls.index("sync_source")


def test_up_rewrites_runtime_env_when_env_file_is_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    env_file = tmp_path / ".env.regression"
    env_file.write_text("OPENAI_API_KEY=set\n", encoding="utf-8")

    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def exists(self, command):
            return True

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)

        return wrapper

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))
    monkeypatch.setattr(incus_regression, "write_runtime_env", record("write_runtime_env"))
    monkeypatch.setattr(incus_regression, "should_seed_state", lambda *args, **kwargs: False)
    monkeypatch.setattr(incus_regression, "sync_source", record("sync_source"))
    monkeypatch.setattr(incus_regression, "compute_fingerprints", lambda repo_root: {})
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", record("update_dependencies_and_build"))
    monkeypatch.setattr(incus_regression, "run_prepare_state", record("run_prepare_state"))
    monkeypatch.setattr(incus_regression, "normalize_runtime_config", record("normalize_runtime_config"))
    monkeypatch.setattr(incus_regression, "write_metadata", record("write_metadata"))
    monkeypatch.setattr(incus_regression, "restart_and_verify", record("restart_and_verify"))
    monkeypatch.setattr(incus_regression, "prepare_show_runtime", record("prepare_show_runtime"))
    monkeypatch.setattr(incus_regression, "update_worktree_mapping", record("update_worktree_mapping"))

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        reset_mode="none",
    )

    assert incus_regression.cmd_up(args) == 0
    assert "write_runtime_env" in calls


def test_up_reserves_worktree_port_under_mapping_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def exists(self, command):
            return True

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    def mapping_lock(repo_root, *, dry_run):
        class Lock:
            def __enter__(self):
                calls.append("mapping_lock_enter")

            def __exit__(self, exc_type, exc, tb):
                calls.append("mapping_lock_exit")

        return Lock()

    def target_lock(repo_root, target, *, dry_run):
        class Lock:
            def __enter__(self):
                calls.append("target_lock_enter")

            def __exit__(self, exc_type, exc, tb):
                calls.append("target_lock_exit")

        return Lock()

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)

        return wrapper

    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)
    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    monkeypatch.setattr(incus_regression, "worktree_mapping_lock", mapping_lock)
    original_reserve_worktree_mapping = incus_regression.reserve_worktree_mapping

    def reserve_worktree_mapping(repo_root, target):
        calls.append("reserve_worktree_mapping")
        original_reserve_worktree_mapping(repo_root, target)

    monkeypatch.setattr(incus_regression, "target_update_lock", target_lock)
    monkeypatch.setattr(incus_regression, "reserve_worktree_mapping", reserve_worktree_mapping)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))
    monkeypatch.setattr(incus_regression, "should_seed_state", lambda *args, **kwargs: False)
    monkeypatch.setattr(incus_regression, "write_runtime_env", record("write_runtime_env"))
    monkeypatch.setattr(incus_regression, "sync_source", record("sync_source"))
    monkeypatch.setattr(incus_regression, "compute_fingerprints", lambda repo_root: {})
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", record("update_dependencies_and_build"))
    monkeypatch.setattr(incus_regression, "run_prepare_state", record("run_prepare_state"))
    monkeypatch.setattr(incus_regression, "normalize_runtime_config", record("normalize_runtime_config"))
    monkeypatch.setattr(incus_regression, "write_metadata", record("write_metadata"))
    monkeypatch.setattr(incus_regression, "restart_and_verify", record("restart_and_verify"))
    monkeypatch.setattr(incus_regression, "prepare_show_runtime", record("prepare_show_runtime"))

    args = argparse.Namespace(
        target="worktree",
        slug="demo-branch",
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        reset_mode="none",
    )

    assert incus_regression.cmd_up(args) == 0

    assert calls[:4] == ["mapping_lock_enter", "reserve_worktree_mapping", "mapping_lock_exit", "target_lock_enter"]
    payload = json.loads((tmp_path / ".runtime" / "incus-regression" / "worktrees.json").read_text(encoding="utf-8"))
    mapping = payload["worktrees"]["demo-branch"]
    assert mapping["host_port"] == 15200
    assert mapping["project"] == "avr-wt-demo-branch"
    assert "updated_at" in mapping


def test_normalize_runtime_config_updates_host_and_port() -> None:
    commands = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            commands.append(" ".join(command))
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=6123,
    )

    incus_regression.normalize_runtime_config(RecordingRunner(), target, remote=None)

    joined = "\n".join(commands)
    assert "ui.get(\"setup_host\") != '127.0.0.1'" in joined
    assert 'ui.get("setup_port") != 6123' in joined
    assert 'ui["setup_port"] = 6123' in joined


def test_stop_service_for_update_ignores_missing_service() -> None:
    commands = []

    class RecordingRunner:
        def run(self, command, *, check=True, **kwargs):
            commands.append((command, check))
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.stop_service_for_update(RecordingRunner(), target, remote=None)

    joined = " ".join(commands[0][0])
    assert "systemctl stop avibe-regression.service || true" in joined
    assert commands[0][1] is False


def test_update_builds_ui_before_editable_install() -> None:
    commands = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            commands.append(" ".join(command))
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.update_dependencies_and_build(
        RecordingRunner(),
        target,
        previous_fingerprints={},
        next_fingerprints={"python": "p", "ui_deps": "d", "ui_source": "s"},
        force_deps=False,
        build_ui=True,
        force_ui=False,
        remote=None,
    )

    install_index = next(i for i, command in enumerate(commands) if "pip install -e ." in command)
    build_index = next(i for i, command in enumerate(commands) if "npm run build" in command)
    assert build_index < install_index


def test_force_ui_rebuilds_even_when_fingerprints_match() -> None:
    commands = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            commands.append(" ".join(command))
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.update_dependencies_and_build(
        RecordingRunner(),
        target,
        previous_fingerprints={"python": "p", "ui_deps": "d", "ui_source": "s"},
        next_fingerprints={"python": "p", "ui_deps": "d", "ui_source": "s"},
        force_deps=False,
        build_ui=True,
        force_ui=True,
        remote=None,
    )

    joined = "\n".join(commands)
    assert "cd ui && npm ci" in joined
    assert "cd ui && npm run build" in joined
    assert "pip install -e ." not in joined


def test_missing_ui_dist_overrides_no_build_ui_before_editable_install() -> None:
    commands = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            commands.append(" ".join(command))
            if "test -d ui/dist" in commands[-1]:
                return subprocess.CompletedProcess(command, 1)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.update_dependencies_and_build(
        RecordingRunner(),
        target,
        previous_fingerprints={},
        next_fingerprints={"python": "p", "ui_deps": "d", "ui_source": "s"},
        force_deps=False,
        build_ui=False,
        force_ui=False,
        remote=None,
    )

    joined = "\n".join(commands)
    install_index = next(i for i, command in enumerate(commands) if "pip install -e ." in command)
    build_index = next(i for i, command in enumerate(commands) if "npm run build" in command)
    assert "test -d ui/dist && test -f ui/dist/index.html" in joined
    assert "cd ui && npm ci" in joined
    assert build_index < install_index


def test_missing_ui_dist_rebuilds_even_when_python_is_unchanged() -> None:
    commands = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            commands.append(" ".join(command))
            if "test -d ui/dist" in commands[-1]:
                return subprocess.CompletedProcess(command, 1)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.update_dependencies_and_build(
        RecordingRunner(),
        target,
        previous_fingerprints={"python": "p", "ui_deps": "d", "ui_source": "s"},
        next_fingerprints={"python": "p", "ui_deps": "d", "ui_source": "s"},
        force_deps=False,
        build_ui=False,
        force_ui=False,
        remote=None,
    )

    joined = "\n".join(commands)
    assert "test -d ui/dist && test -f ui/dist/index.html" in joined
    assert "cd ui && npm run build" in joined
    assert "pip install -e ." not in joined


def test_prepare_show_runtime_cleans_partial_source_and_retries_once() -> None:
    commands = []

    class RecordingRunner:
        def __init__(self) -> None:
            self.prepare_attempts = 0

        def run(self, command, **kwargs):
            joined = " ".join(command)
            commands.append(joined)
            if "vibe runtime prepare --strict" in joined:
                self.prepare_attempts += 1
                return subprocess.CompletedProcess(command, 1 if self.prepare_attempts == 1 else 0)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.prepare_show_runtime(RecordingRunner(), target, remote=None)

    joined = "\n".join(commands)
    assert joined.count("vibe runtime prepare --strict") == 2
    assert "rm -rf ~/.avibe/runtime/show-runtime/source ~/.npm/_cacache" in joined
    assert "vibe runtime status --json" in joined


def test_restart_waits_for_service_and_status_running() -> None:
    commands = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            commands.append(" ".join(command))
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.restart_and_verify(RecordingRunner(), target, remote=None)

    joined = "\n".join(commands)
    assert "systemctl is-active --quiet avibe-regression.service" in joined
    assert "http://127.0.0.1:5123/status" in joined
    assert "'\"state\":\"running\"'" in joined
