from __future__ import annotations

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

    uv = updates[-1]
    assert uv["package-ecosystem"] == "uv"
    assert uv["versioning-strategy"] == "increase"
    assert uv["ignore"] == [{"dependency-name": "claude-agent-sdk"}]
