"""Structural and behavioral guards for the complete ``vibe doctor`` surface."""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.install_integrity import IntegrityResult
from core.tmux_runtime import TmuxRuntimeManager
from vibe import cli
from vibe.i18n import get_supported_languages, t
from vibe.restart_supervisor import RestartState


REPO_ROOT = Path(__file__).resolve().parents[1]
I18N_DIR = REPO_ROOT / "vibe" / "i18n"
DOCTOR_SOURCE = REPO_ROOT / "vibe" / "cli.py"
RESTART_SOURCE = REPO_ROOT / "vibe" / "restart_supervisor.py"


def _flatten(value: object, prefix: str = "") -> dict[str, str]:
    if not isinstance(value, dict):
        return {prefix: str(value)}
    flattened: dict[str, str] = {}
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        flattened.update(_flatten(child, path))
    return flattened


def _doctor_bundle(language: str) -> dict[str, str]:
    payload = json.loads((I18N_DIR / f"{language}.json").read_text(encoding="utf-8"))
    return _flatten(payload["doctor"], "doctor")


def _is_i18n_expression(node: ast.AST, *, allow_none: bool = False) -> bool:
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id == "i18n_t"
    if isinstance(node, ast.IfExp):
        return _is_i18n_expression(node.body) and _is_i18n_expression(
            node.orelse,
            allow_none=allow_none,
        )
    return allow_none and isinstance(node, ast.Constant) and node.value is None


def _assert_doctor_namespace(node: ast.AST) -> None:
    if isinstance(node, ast.IfExp):
        _assert_doctor_namespace(node.body)
        _assert_doctor_namespace(node.orelse)
        return
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return
    if node.func.id != "i18n_t" or not node.args:
        return
    key = node.args[0]
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        assert key.value.startswith("doctor."), f"Doctor copy escaped its namespace: {key.value}"


def test_doctor_calls_route_every_user_visible_argument_through_i18n() -> None:
    """The rule is structural so a new call cannot silently add English copy."""

    source = DOCTOR_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(DOCTOR_SOURCE))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "_add_doctor_item":
            assert len(node.args) >= 3
            assert _is_i18n_expression(node.args[2]), f"message at line {node.lineno} is not i18n_t"
            _assert_doctor_namespace(node.args[2])
            if len(node.args) >= 4:
                assert _is_i18n_expression(node.args[3], allow_none=True), (
                    f"action at line {node.lineno} is not i18n_t"
                )
                _assert_doctor_namespace(node.args[3])
        elif node.func.id == "_doctor_repair_result":
            assert len(node.args) >= 3
            assert _is_i18n_expression(node.args[2]), (
                f"repair message at line {node.lineno} is not i18n_t"
            )
            _assert_doctor_namespace(node.args[2])


def test_doctor_bundles_have_identical_keys_and_interpolation() -> None:
    en = _doctor_bundle("en")
    zh = _doctor_bundle("zh")
    assert set(en) == set(zh)

    placeholder = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
    for key, english in en.items():
        names = set(placeholder.findall(english))
        for language in get_supported_languages():
            rendered = t(key, language, **{name: f"sample-{name}" for name in names})
            assert rendered != key
            assert not placeholder.search(rendered), f"unresolved placeholder in {language}:{key}"


def test_doctor_finite_vocabulary_has_one_projection_owner() -> None:
    assert set(cli.DOCTOR_DISPLAY_PROJECTIONS) == {
        "tunnel_state",
        "tunnel_grade",
        "tunnel_protocol",
        "download_kind",
        "repair_reason",
        "repair_suffix",
        "restart_state",
        "show_runtime_provider",
    }
    for projection in cli.DOCTOR_DISPLAY_PROJECTIONS.values():
        assert projection
        assert all(key.startswith("doctor.") for key in projection.values())

    assert cli._doctor_display_value("future-state", "tunnel_state", "zh") == "未知"
    assert cli._doctor_display_value("future-kind", "download_kind", "zh") == "未知"


def test_restart_state_owner_drives_producer_retention_display_and_tests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    projection = cli.DOCTOR_DISPLAY_PROJECTIONS["restart_state"]
    assert set(projection) == {state.value for state in RestartState}

    status_path = tmp_path / "restart.json"
    status_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli.time, "time", lambda: status_path.stat().st_mtime + 3600)
    for state in RestartState:
        expected_stale = state.retention is not None
        assert cli._restart_status_is_stale({"state": state.value}, status_path) is expected_stale
        for language in ("en", "zh"):
            assert cli._doctor_display_value(state.value, "restart_state", language) == t(
                projection[state.value], language
            )


def test_restart_producer_cannot_bypass_owned_state_vocabulary() -> None:
    tree = ast.parse(RESTART_SOURCE.read_text(encoding="utf-8"), filename=str(RESTART_SOURCE))
    bypasses: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "state"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    bypasses.append(value.lineno)
        elif isinstance(node, ast.keyword) and node.arg == "state":
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                bypasses.append(node.value.lineno)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "state"
                    and isinstance(node.value.value, str)
                ):
                    bypasses.append(node.value.lineno)
    assert not bypasses, f"restart state literal bypassed RestartState at lines {bypasses}"


def test_tmux_failure_reasons_are_producer_owned_and_doctor_projected(tmp_path: Path) -> None:
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "tmux")
    reasons = manager.install_failure_reasons()
    assert "tmux_install_missing_binary" in reasons
    assert "xattr_failed" in reasons

    for reason in reasons:
        key = cli._doctor_managed_reason_key(reason)
        assert key is not None, f"Doctor has no display projection for {reason}"
        for language in ("en", "zh"):
            detail = cli._doctor_managed_failure_detail("tmux", {"reason": reason}, language)
            assert detail
            assert reason not in detail


@pytest.mark.parametrize(
    ("reason", "english_marker", "chinese_marker"),
    [
        (
            "tmux_install_missing_binary",
            "archive did not contain its binary",
            "归档不包含其二进制文件",
        ),
        (
            "xattr_failed",
            "metadata could not be updated",
            "无法更新 tmux 二进制元数据",
        ),
    ],
)
def test_tmux_actual_failure_spellings_render_in_both_languages(
    reason: str,
    english_marker: str,
    chinese_marker: str,
) -> None:
    result = {"ok": False, "reason": reason}

    english = cli._doctor_managed_failure_detail("tmux", result, "en")
    chinese = cli._doctor_managed_failure_detail("tmux", result, "zh")

    assert english_marker in english
    assert chinese_marker in chinese
    assert reason not in english
    assert reason not in chinese
    assert result["reason"] == reason


@pytest.mark.parametrize(
    ("language", "integrity", "message_marker", "action_marker"),
    [
        ("en", IntegrityResult(ok=True, checked_files=7), "files are intact (7 checked)", None),
        ("zh", IntegrityResult(ok=True, checked_files=7), "文件完整（已检查 7 个）", None),
        (
            "en",
            IntegrityResult(ok=False, checked_files=7, failures=("bad/path.py",)),
            "integrity check failed: bad/path.py",
            "Rerun the Avibe installer",
        ),
        (
            "zh",
            IntegrityResult(ok=False, checked_files=7, failures=("bad/path.py",)),
            "完整性检查失败：bad/path.py",
            "重新运行 Avibe 安装器",
        ),
    ],
)
def test_package_integrity_doctor_items_are_localized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    language: str,
    integrity: IntegrityResult,
    message_marker: str,
    action_marker: str | None,
) -> None:
    site_packages = tmp_path / "site-packages"
    (site_packages / "avibe_os-1.0.dist-info").mkdir(parents=True)
    active_vibe = (Path.home() / ".local" / "bin" / "vibe").resolve()
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: language)
    monkeypatch.setattr(cli, "_path_entries_for_executable", lambda _name: [active_vibe])
    monkeypatch.setattr(cli, "_uv_tool_site_packages_for_vibe", lambda _path: [site_packages])
    monkeypatch.setattr(cli, "_is_uv_tool_editable", lambda _path: False)
    monkeypatch.setattr(cli, "_current_sqlite_revision", lambda: None)
    monkeypatch.setattr(cli, "verify_site_packages", lambda _path: integrity)

    item = next(
        item
        for item in cli._local_cli_installation_items()
        if item.get("code") == "installation.package_integrity"
    )

    assert item["status"] == ("pass" if integrity.ok else "fail")
    assert message_marker in item["message"]
    if action_marker is None:
        assert "action" not in item
    else:
        assert action_marker in item["action"]


@pytest.mark.parametrize(
    ("language", "state_marker", "field_markers"),
    [
        ("en", "state=error", ("error=disk full", "trigger=upgrade", "job_id=job-1", "log=/tmp/restart.log")),
        ("zh", "状态=错误", ("错误=disk full", "触发来源=upgrade", "任务 ID=job-1", "日志=/tmp/restart.log")),
    ],
)
def test_restart_failure_summary_localizes_labels_and_preserves_raw_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    language: str,
    state_marker: str,
    field_markers: tuple[str, ...],
) -> None:
    payload = {
        "ok": False,
        "state": RestartState.ERROR.value,
        "error": "disk\nfull",
        "trigger": "upgrade",
        "job_id": "job-1",
        "log_path": "/tmp/restart.log",
    }
    original = dict(payload)
    status_path = tmp_path / "restart.json"
    status_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: language)
    monkeypatch.setattr(cli.runtime, "get_restart_status_path", lambda: status_path)
    monkeypatch.setattr(cli.runtime, "read_json", lambda _path: payload)
    monkeypatch.setattr(cli.runtime, "verified_service_running", lambda: False)

    item = cli._restart_state_items()[0]

    assert item["code"] == "runtime.restart_failed"
    assert state_marker in item["message"]
    assert all(marker in item["message"] for marker in field_markers)
    assert payload == original
    if language == "zh":
        assert "state=error" not in item["message"]


@pytest.mark.parametrize(("language", "missing_marker"), [("en", "missing"), ("zh", "缺失")])
def test_service_pid_sentinel_is_localized_only_at_rendering_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    language: str,
    missing_marker: str,
) -> None:
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: language)
    monkeypatch.setattr(cli.paths, "get_runtime_pid_path", lambda: tmp_path / "absent.pid")
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda **_kwargs: 4242)
    monkeypatch.setattr(cli.runtime, "service_lock_holder_pid", lambda: 4242)
    monkeypatch.setattr(cli.runtime, "read_status", lambda: {"service_pid": None})

    items = cli._service_lifecycle_items(detect_extra_processes=False)

    mismatch_codes = {
        "runtime.service_pidfile_mismatch",
        "runtime.status_pid_mismatch",
    }
    mismatches = [item for item in items if item.get("code") in mismatch_codes]
    assert {item["code"] for item in mismatches} == mismatch_codes
    assert all(missing_marker in item["message"] for item in mismatches)
    if language == "zh":
        assert all("missing" not in item["message"] for item in mismatches)


@pytest.mark.parametrize(
    ("language", "attempt_marker"),
    [("en", "after 3 attempts"), ("zh", "经过 3 次尝试")],
)
def test_dependency_attempt_copy_reports_total_attempts(language: str, attempt_marker: str) -> None:
    item = t(
        "doctor.item.dependencyHttpErrorRetried",
        language,
        label="tmux",
        status=503,
        attempts=3,
        url="https://example.test/tmux.tgz",
    )
    repair = t("doctor.repair.dependencyDownloadNetwork", language, attempts=3)

    assert attempt_marker in item
    assert ("3 attempts" if language == "en" else "3 次尝试") in repair
    if language == "zh":
        assert "重试 3 次" not in item
        assert "已重试 3 次" not in repair


def test_restart_recovery_action_preserves_branch_semantics_in_both_languages() -> None:
    english = t("doctor.action.restartFailed", "en")
    chinese = t("doctor.action.restartFailed", "zh")

    assert "stops a process holding no lock and brings the service up" in english
    assert "if the failed process holds the lock itself, and then repeat the start above" in english
    assert "停止未持有锁的进程并启动服务" in chinese
    assert "若失败进程持有锁，则运行 `vibe stop`，然后重试上述启动命令" in chinese


def test_doctor_dependency_status_stays_machine_data_and_out_of_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli.api,
        "dependencies_status",
        lambda **_kwargs: {
            "deps": [
                {"id": "askill", "required": True, "installed": False, "status": "missing"},
                {"id": "avault", "required": True, "installed": False, "status": "error"},
                {
                    "id": "tmux",
                    "required": False,
                    "installed": False,
                    "status": "upgrade_required",
                },
            ]
        },
    )
    monkeypatch.setattr(cli.api, "askill_auto_install_supported", lambda: True)
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: "zh")

    items = cli._managed_dependencies_doctor_items(deep=False)

    expected = {
        "dependencies.askill.not_ready": "missing",
        "dependencies.avault.not_ready": "error",
        "dependencies.tmux.not_ready": "upgrade_required",
    }
    for code, raw_status in expected.items():
        item = next(item for item in items if item.get("code") == code)
        assert item["dependency_status"] == raw_status
        assert raw_status not in item["message"]
    assert all("尚未就绪" in item["message"] for item in items if item.get("code") in expected)


@pytest.mark.parametrize(
    ("probe_reason", "label_marker", "english_label"),
    [
        ("runtime_manifest_download_failed", "Show Runtime 清单", "Show Runtime manifest"),
        ("runtime_archive_download_failed", "Show Runtime 归档", "Show Runtime archive"),
    ],
)
def test_show_runtime_download_labels_are_localized(
    monkeypatch: pytest.MonkeyPatch,
    probe_reason: str,
    label_marker: str,
    english_label: str,
) -> None:
    from types import SimpleNamespace

    manager = SimpleNamespace(
        status=lambda: {
            "provider": "manifest-cache",
            "platform": "darwin-arm64",
            "manifest": {"runtime_version": "test"},
            "archive": {"name": "show-runtime.tgz"},
            "explicit_command": None,
            "node_available": True,
        },
        probe_archive_reachability=lambda: {
            "ok": False,
            "checked": True,
            "reason": probe_reason,
            "download_error": {"kind": "http", "http_status": 503, "attempts": 2},
        },
        archive_cache_status=lambda: None,
    )
    monkeypatch.setattr("core.show_runtime.ShowRuntimeManager", lambda **kwargs: manager)
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: "zh")

    items = cli._show_runtime_doctor_items(deep=True)
    message = "\n".join(item["message"] for item in items)
    assert label_marker in message
    assert english_label not in message


def test_doctor_uses_real_chinese_path_and_stable_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cli.paths.AVIBE_HOME_ENV, "/tmp/avibe-doctor-home")
    rendered: dict[str, list[dict]] = {}
    for language in ("en", "zh"):
        monkeypatch.setattr(cli, "_configured_cli_language", lambda language=language: language)
        rendered[language] = cli._home_migration_items()

    items = rendered["zh"]

    assert items[0]["code"] == "runtime.explicit_home"
    assert "已显式设置" in items[0]["message"]
    assert "runtime.explicit_home" not in items[0]["message"]
    assert [item["code"] for item in rendered["en"]] == [item["code"] for item in rendered["zh"]]


def test_doctor_localizes_nested_repair_details_and_missing_payload_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: "zh")

    repaired = cli._repair_managed_dependency(
        "avault",
        lambda force: {
            "ok": True,
            "changed": True,
            "version": "0.1.6",
            "message": "English installer completed",
        },
    )
    assert "English installer completed" not in repaired["message"]
    assert "0.1.6" in repaired["message"]
    assert "修复完成" in repaired["message"]

    failures = [
        (
            "askill",
            {
                "ok": False,
                "reason": "askill_auto_install_unsupported",
                "required_tools": ["curl", "bash"],
                "message": "askill auto-install needs curl + bash",
            },
            "自动安装需要",
        ),
        (
            "avault",
            {
                "ok": False,
                "reason": "avault_checksum_mismatch",
                "expected_sha256": "expected",
                "actual_sha256": "actual",
                "message": "checksum verification failed",
            },
            "校验和验证失败",
        ),
    ]
    for target, result, marker in failures:
        failed = cli._repair_managed_dependency(target, lambda force, result=result: result)
        assert marker in failed["message"]
        assert result["reason"] not in failed["message"]

    download = cli._repair_managed_dependency(
        "git-runtime",
        lambda force: {
            "ok": False,
            "reason": "git_archive_download_failed",
            "download_error": {"kind": "dns", "attempts": 3},
            "message": "raw download copy",
        },
    )
    assert "DNS 查询失败" in download["message"]
    assert "dns" not in download["message"]
    unknown_download = cli._repair_managed_dependency(
        "tmux",
        lambda force: {
            "ok": False,
            "reason": "tmux_archive_download_failed",
            "download_error": {"kind": "future-kind", "attempts": 1},
        },
    )
    assert "future-kind" not in unknown_download["message"]

    items: list[dict] = []
    cli._add_dependency_download_failure(
        items,
        {"kind": "timeout", "attempts": 2},
        label="avault",
        code_prefix="dependencies.avault.download",
        repair_target=None,
    )
    assert "the selected dependency URL" not in items[0]["message"]
    assert "所选依赖 URL" in items[0]["message"]
    assert items[0]["download_kind"] == "timeout"

    from types import SimpleNamespace

    manager = SimpleNamespace(
        status=lambda: {
            "provider": "manifest-cache",
            "platform": None,
            "manifest": {"runtime_version": "test"},
            "archive": None,
            "explicit_command": None,
            "node_available": False,
        },
        archive_cache_status=lambda: None,
    )
    monkeypatch.setattr("core.show_runtime.ShowRuntimeManager", lambda **kwargs: manager)
    runtime_items = cli._show_runtime_doctor_items()
    messages = "\n".join(item["message"] for item in runtime_items)
    assert "this platform" not in messages
    assert "当前平台" in messages


@pytest.mark.parametrize(
    ("shape", "expected_key"),
    [
        ("requests_unavailable", "tunnelQualityRequestsUnavailable"),
        ("latency", "tunnelQualityLatency"),
        ("rtt_unavailable", "tunnelQualityRttUnavailable"),
        ("rtt", "tunnelQualityRtt"),
    ],
)
def test_doctor_tunnel_quality_projects_every_finite_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    shape: str,
    expected_key: str,
) -> None:
    config = cli.V2Config.default()
    config.language = "zh"
    config.remote_access.vibe_cloud.enabled = True
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli.paths, "get_config_path", lambda: config_path)
    monkeypatch.setattr(cli.paths, "get_runtime_doctor_path", lambda: tmp_path / "doctor.json")
    monkeypatch.setattr(cli.V2Config, "load", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(cli, "_home_migration_items", lambda: [])
    monkeypatch.setattr(cli, "_service_lifecycle_items", lambda **_kwargs: [])
    monkeypatch.setattr(cli, "_service_install_family_items", lambda **_kwargs: [])
    monkeypatch.setattr(cli, "_restart_state_items", lambda: [])
    monkeypatch.setattr(cli, "_runtime_architecture_items", lambda: [])
    monkeypatch.setattr(cli, "_show_git_checkpoint_items", lambda: [])
    monkeypatch.setattr(cli, "_managed_dependencies_doctor_items", lambda **_kwargs: [])
    monkeypatch.setattr(cli, "_show_runtime_doctor_items", lambda **_kwargs: [])
    monkeypatch.setattr(cli, "_local_cli_installation_items", lambda: [])
    monkeypatch.setattr(cli.api, "detect_cli", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        cli.paths,
        "get_logs_dir",
        lambda: tmp_path / "logs",
    )
    monkeypatch.setattr(
        "vibe.remote_access.status",
        lambda _config: {
            "running": True,
            "tunnel_quality": {
                "state": "degraded",
                "grade": "poor",
                "protocol": "quic",
                "sampled_at": datetime.now(timezone.utc).isoformat(),
                **(
                    {
                        "request_path": {
                            "status": "unavailable",
                            "confidence": "high",
                            "success_count": 0,
                            "sample_count": 4,
                        }
                    }
                    if shape == "requests_unavailable"
                    else {
                        "request_path": {
                            "status": "ok",
                            "confidence": "high",
                            "success_count": 4,
                            "sample_count": 4,
                            "latency_ms": {"p95": 100, "p99": 200},
                            "slow_request_rate": {"over_1000_ms": 0.25},
                        }
                    }
                    if shape == "latency"
                    else {"rtt_ms": {"median": 10, "max": 20}}
                    if shape == "rtt"
                    else {}
                ),
            },
        },
    )
    monkeypatch.setattr(
        "vibe.remote_access.tunnel_quality.request_path_has_usable_latency",
        lambda request_path: bool(request_path and request_path.get("latency_ms")),
    )
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: "zh")

    result = cli._doctor()
    remote_group = next(group for group in result["groups"] if group["name"] == t("doctor.group.remoteAccess", "zh"))
    shape_marker = {
        "tunnelQualityRequestsUnavailable": "远程请求不可用",
        "tunnelQualityLatency": "远程请求 P95",
        "tunnelQualityRttUnavailable": "没有边缘 RTT",
        "tunnelQualityRtt": "边缘 RTT 中位数",
    }[expected_key]
    item = next(item for item in remote_group["items"] if shape_marker in item["message"])
    assert "降级" in item["message"]
    assert item["tunnel_state"] == "degraded"
    assert item["tunnel_grade"] == "poor"
    assert item["tunnel_protocol"] == "quic"
    if expected_key != "tunnelQualityRttUnavailable":
        assert "较差" in item["message"]
    if expected_key != "tunnelQualityRtt":
        assert "QUIC" in item["message"]


@pytest.mark.parametrize(
    ("value_type", "values"),
    [
        ("state", ("healthy", "degraded", "recovering", "unknown")),
        ("grade", ("good", "fair", "poor", "critical", "unknown")),
        ("protocol", ("quic", "http2", "unknown")),
    ],
)
def test_doctor_tunnel_display_projection_covers_producer_enums(
    value_type: str,
    values: tuple[str, ...],
) -> None:
    expected = {
        "state": ("健康", "降级", "恢复中", "未知"),
        "grade": ("良好", "一般", "较差", "严重", "未知"),
        "protocol": ("QUIC", "HTTP/2", "未知"),
    }[value_type]
    for raw_value, localized_value in zip(values, expected):
        assert cli._doctor_tunnel_display_value(raw_value, value_type, "zh") == localized_value


def test_managed_repair_failure_contract_has_structured_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cli.api, "resolve_cli_path", lambda _binary: None)
    monkeypatch.setattr(cli.api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr("platform.system", lambda: "FreeBSD")
    monkeypatch.setattr("platform.machine", lambda: "riscv64")
    producer_failures = {
        "askill": cli.api.install_askill(),
        "avault": cli.api.install_avault(),
    }
    from core.git_runtime import GitRuntimeManager
    producer_failures.update(
        {
            "tmux": TmuxRuntimeManager(runtime_dir=tmp_path / "tmux")._failure(
                "tmux_archive_unavailable"
            ),
            "git-runtime": GitRuntimeManager(runtime_dir=tmp_path / "git")._failure("git_archive_unavailable"),
        }
    )
    for target, result in producer_failures.items():
        assert result.get("ok") is False
        assert result.get("reason") or result.get("download_error")
        repaired = cli._repair_managed_dependency(target, lambda force, result=result: result)
        assert repaired["status"] == "failed"
        assert repaired["reason"] == result["reason"]
        assert "message" in repaired and repaired["message"]
