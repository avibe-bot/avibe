from __future__ import annotations

import json
from pathlib import Path

import yaml


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
        ("uv", "/"),
    ]

    # One weekday per ecosystem keeps the weekly PRs from saturating the hosted
    # runner slots together.
    days = [item["schedule"]["day"] for item in updates]
    assert days == ["monday", "wednesday", "thursday", "friday", "tuesday"]
    assert len(set(days)) == len(updates)
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

    # Keep the known lint-policy drift out of grouped UI bumps without blocking
    # future candidates from being reviewed against the existing lint ledger.
    ui = next(item for item in updates if item["directory"] == "/ui")
    refresh_ignores = [item for item in ui.get("ignore", []) if item["dependency-name"] == "eslint-plugin-react-refresh"]
    assert refresh_ignores == [{"dependency-name": "eslint-plugin-react-refresh", "versions": ["0.5.6", "0.5.7"]}]

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


def test_ui_vitest_hold_preserves_the_node20_toolchain() -> None:
    # The candidate's engine contract drops Node20 even when a smoke test works.
    # Hold that candidate, not unrelated UI packages or future reviewed releases.
    ui = next(item for item in _updates() if item["package-ecosystem"] == "npm" and item["directory"] == "/ui")
    assert [item for item in ui["ignore"] if item["dependency-name"] == "vitest"] == [
        {"dependency-name": "vitest", "versions": ["5.1.0"]}
    ]
    package = json.loads((ROOT / "ui/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "ui/package-lock.json").read_text(encoding="utf-8"))
    assert "^20." in package["engines"]["node"]
    assert package["devDependencies"]["vitest"].startswith("^4.")
    assert lock["packages"][""]["devDependencies"]["vitest"] == package["devDependencies"]["vitest"]
    assert lock["packages"]["node_modules/vitest"]["version"].startswith("4.")
    assert "^20." in lock["packages"]["node_modules/vitest"]["engines"]["node"]
