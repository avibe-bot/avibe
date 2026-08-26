"""Structural and behavioral guards for the complete ``vibe doctor`` surface."""

from __future__ import annotations

import ast
import json
import re
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
