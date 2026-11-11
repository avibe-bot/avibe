from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from packaging.markers import Marker
from packaging.requirements import Requirement

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def _updates() -> list[dict]:
    payload = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    assert payload["version"] == 2
    return payload["updates"]


def test_dependabot_covers_ordinary_ecosystems_with_reviewable_update_policy() -> None:
    updates = _updates()
    assert [(item["package-ecosystem"], item["directory"]) for item in updates] == [
        ("github-actions", "/"),
        ("npm", "/ui"),
        ("npm", "/desktop"),
        ("cargo", "/desktop"),
        ("cargo", "/direct-transport"),
        ("uv", "/"),
    ]

    # Preserve existing weekdays; the separately reviewed Rust runtime shares
    # the Rust maintenance day rather than moving another ecosystem's schedule.
    days = [item["schedule"]["day"] for item in updates]
    assert days == ["monday", "wednesday", "thursday", "friday", "friday", "tuesday"]
    assert len(set(days)) == 5
    for item in updates:
        assert item["schedule"] == {
            "interval": "weekly",
            "day": item["schedule"]["day"],
            "time": "06:00",
            "timezone": "Asia/Shanghai",
        }
        assert item["open-pull-requests-limit"] == 3
        assert set(item["groups"][next(iter(item["groups"]))]["update-types"]) == {"minor", "patch"}
        assert "auto-merge" not in item
        assert "automerge" not in item

    # Known-incompatible Windows IPC versions must not overwrite the safety pin.
    # Keep the exclusion version-scoped so later fixes still receive review.
    cargo = next(item for item in updates if item["package-ecosystem"] == "cargo")
    assert cargo["ignore"] == [
        {"dependency-name": "tauri-plugin-single-instance", "versions": ["2.5.4", "2.5.5"]}
    ]

    uv = updates[-1]
    assert uv["package-ecosystem"] == "uv"
    assert uv["versioning-strategy"] == "increase"
    assert uv["ignore"] == [{"dependency-name": "claude-agent-sdk"}]


def test_direct_transport_iroh_pin_is_not_an_automatic_group_update() -> None:
    update = next(item for item in _updates() if item["directory"] == "/direct-transport")
    assert update["package-ecosystem"] == "cargo"
    assert update["ignore"] == [{"dependency-name": "iroh"}]
    pins = dict(line.split("=", 1) for line in
                (ROOT / "packaging/iroh-apple/versions.env").read_text().splitlines() if "=" in line)
    crate = tomllib.loads((ROOT / "direct-transport/Cargo.toml").read_text())
    assert crate["dependencies"]["iroh"] == {
        "version": f"={pins['IROH_CORE_VERSION']}",
        "default-features": False,
        "features": ["tls-ring"],
    }
    lock = tomllib.loads((ROOT / "direct-transport/Cargo.lock").read_text())
    assert [package["version"] for package in lock["package"] if package["name"] == "iroh"] == [
        pins["IROH_CORE_VERSION"]
    ]


def test_ui_refresh_hold_preserves_the_established_export_contract() -> None:
    # 0.5.4 adds diagnostics for primitive aliases, lazy registry entries and
    # deferred components. Keep only demonstrated incompatible releases out;
    # eslintConventions.test.mjs exercises the actual rule on those shapes.
    ui = next(item for item in _updates() if item["package-ecosystem"] == "npm" and item["directory"] == "/ui")
    assert [item for item in ui["ignore"] if item["dependency-name"] == "eslint-plugin-react-refresh"] == [
        {"dependency-name": "eslint-plugin-react-refresh", "versions": ["0.5.4", "0.5.5", "0.5.6", "0.5.7"]}
    ]
    package = json.loads((ROOT / "ui/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "ui/package-lock.json").read_text(encoding="utf-8"))
    assert package["devDependencies"]["eslint-plugin-react-refresh"] == "0.4.26"
    assert lock["packages"][""]["devDependencies"]["eslint-plugin-react-refresh"] == "0.4.26"
    assert lock["packages"]["node_modules/eslint-plugin-react-refresh"]["version"] == "0.4.26"


def test_ui_vitest_hold_preserves_the_node20_toolchain() -> None:
    # The candidate's engine contract drops Node20 even when a smoke test works.
    # Hold that candidate, not unrelated UI packages or future reviewed releases.
    ui = next(item for item in _updates() if item["package-ecosystem"] == "npm" and item["directory"] == "/ui")
    assert [item for item in ui["ignore"] if item["dependency-name"] == "vitest"] == [
        {"dependency-name": "vitest", "versions": ["5.0.5", "5.1.0"]}
    ]
    package = json.loads((ROOT / "ui/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "ui/package-lock.json").read_text(encoding="utf-8"))
    assert "^20." in package["engines"]["node"]
    assert package["devDependencies"]["vitest"].startswith("^4.")
    assert lock["packages"][""]["devDependencies"]["vitest"] == package["devDependencies"]["vitest"]
    assert lock["packages"]["node_modules/vitest"]["version"].startswith("4.")
    assert "^20." in lock["packages"]["node_modules/vitest"]["engines"]["node"]


@pytest.mark.parametrize("python_version,expected", [("3.10", "2.0.57"), ("3.11", "2.1.0"), ("3.12", "2.1.0")])
@pytest.mark.parametrize("platform,machine", [("linux", "x86_64"), ("darwin", "arm64"), ("win32", "AMD64")])
def test_sqlalchemy_requirements_and_lock_preserve_python_version_branches(
    python_version: str, expected: str, platform: str, machine: str,
) -> None:
    # A Python3.10 patch must not collapse the already-adopted 2.1 branch on
    # newer Python. Check the actual requirement and root lock edge per target.
    environment = {
        "python_version": python_version,
        "python_full_version": f"{python_version}.0",
        "sys_platform": platform,
        "platform_machine": machine,
    }
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    requirements = [Requirement(value) for value in project["dependencies"]]
    selected = [item for item in requirements if item.name.lower() == "sqlalchemy"
                and (item.marker is None or item.marker.evaluate(environment))]
    assert len(selected) == 1
    assert expected in selected[0].specifier
    assert "2.0.55" not in selected[0].specifier
    assert ("2.1.0" if python_version == "3.10" else "2.0.57") not in selected[0].specifier

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    root = next(item for item in lock["package"] if item["name"] == "avibe-os")
    edges = [item for item in root["dependencies"] if item["name"] == "sqlalchemy"
             and ("marker" not in item or Marker(item["marker"]).evaluate(environment))]
    assert len(edges) == 1
    assert edges[0]["version"] == expected
    assert any(item["name"] == "sqlalchemy" and item["version"] == expected for item in lock["package"])


def test_ui_jsdom_hold_preserves_node20_and_existing_dom_consumers() -> None:
    # 31.0.0 excludes Node20 and fails startup there. Keep this candidate out
    # without blocking later independently reviewed releases or changing tests.
    ui = next(item for item in _updates() if item["package-ecosystem"] == "npm" and item["directory"] == "/ui")
    assert [item for item in ui["ignore"] if item["dependency-name"] == "jsdom"] == [
        {"dependency-name": "jsdom", "versions": ["31.0.0"]}
    ]
    package = json.loads((ROOT / "ui/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "ui/package-lock.json").read_text(encoding="utf-8"))
    assert "^20." in package["engines"]["node"]
    assert package["devDependencies"]["jsdom"] == "^29.1.1"
    assert lock["packages"][""]["devDependencies"]["jsdom"] == package["devDependencies"]["jsdom"]
    assert lock["packages"]["node_modules/jsdom"]["version"] == "29.1.1"
    assert "^20." in lock["packages"]["node_modules/jsdom"]["engines"]["node"]


def test_ui_playwright_hold_keeps_the_test_runner_and_browser_driver_coherent() -> None:
    # 1.65.0 regresses the unchanged activity-expiry consumer. Exclude only that
    # tested candidate and retain one matching runner/driver set for npm ci.
    ui = next(item for item in _updates() if item["package-ecosystem"] == "npm" and item["directory"] == "/ui")
    assert [item for item in ui["ignore"] if item["dependency-name"] == "@playwright/test"] == [
        {"dependency-name": "@playwright/test", "versions": ["1.65.0"]}
    ]
    package = json.loads((ROOT / "ui/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "ui/package-lock.json").read_text(encoding="utf-8"))
    assert package["devDependencies"]["@playwright/test"] == "1.64.1"
    assert lock["packages"][""]["devDependencies"]["@playwright/test"] == "1.64.1"
    for name in ("@playwright/test", "playwright", "playwright-core"):
        assert lock["packages"][f"node_modules/{name}"]["version"] == "1.64.1"
