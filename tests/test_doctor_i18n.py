"""Structural and behavioral guards for the complete ``vibe doctor`` surface."""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vibe import cli
from vibe.i18n import get_supported_languages, t


REPO_ROOT = Path(__file__).resolve().parents[1]
I18N_DIR = REPO_ROOT / "vibe" / "i18n"
DOCTOR_SOURCE = REPO_ROOT / "vibe" / "cli.py"


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
    expected = {
        "tunnel_state": {"healthy", "degraded", "recovering", "unknown"},
        "tunnel_grade": {"good", "fair", "poor", "critical", "unknown"},
        "tunnel_protocol": {"quic", "http2", "unknown"},
        "download_kind": {"http", "dns", "tls", "timeout", "network", "permission", "disk", "io"},
        "repair_reason": {
            "askill_auto_install_unsupported",
            "askill_install_path_missing",
            "askill_install_timeout",
            "askill_install_failed",
            "askill_install_error",
            "avault_platform_unsupported",
            "avault_checksum_mismatch",
            "avault_install_path_missing",
            "avault_install_failed",
            "avault_download_failed",
            "avault_p2_release_unavailable",
            "git_runtime_unpublished",
        },
        "repair_suffix": {
            "install_already_running",
            "platform_unsupported",
            "manifest_missing",
            "manifest_invalid",
            "manifest_unavailable",
            "manifest_unavailable_offline",
            "manifest_download_failed",
            "manifest_url_unsupported",
            "archive_unavailable",
            "archive_unavailable_offline",
            "archive_url_unsupported",
            "archive_download_failed",
            "archive_checksum_mismatch",
            "archive_size_mismatch",
            "binary_checksum_mismatch",
            "binary_not_runnable",
            "binary_prepare_failed",
            "install_missing_binary",
            "install_failed",
            "install_lock_failed",
            "install_claim_failed",
            "install_target_changed",
            "pointer_write_failed",
            "codesign_missing",
            "codesign_failed",
            "codesign_verify_failed",
            "xattr_failed",
        },
        "restart_state": {"running", "scheduled", "succeeded", "failed", "skipped", "unknown"},
        "show_runtime_provider": {"manifest-cache", "archive", "npm", "unknown"},
    }
    assert set(cli.DOCTOR_DISPLAY_PROJECTIONS) == set(expected)
    for category, members in expected.items():
        assert set(cli.DOCTOR_DISPLAY_PROJECTIONS[category]) == members
        assert all(key.startswith("doctor.") for key in cli.DOCTOR_DISPLAY_PROJECTIONS[category].values())

    assert cli._doctor_display_value("future-state", "tunnel_state", "zh") == "未知"
    assert cli._doctor_display_value("future-kind", "download_kind", "zh") == "未知"


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
    from core.tmux_runtime import TmuxRuntimeManager

    producer_failures.update(
        {
            "tmux": TmuxRuntimeManager(runtime_dir=tmp_path / "tmux")._failure("tmux_archive_unavailable"),
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
