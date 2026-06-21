"""Tests for the vault REST wrappers in vibe/api.py (P0 commit 4 backend).

These drive the same functions the FastAPI routes call. Conftest isolates
VIBE_REMOTE_HOME, so the state DB + machine key are under tmp, never the real home.
"""

from __future__ import annotations

import json

import pytest

from vibe import api


def test_create_list_delete_roundtrip():
    created = api.create_vault_secret({"name": "OPENAI_API_KEY", "value": "sk-ant-abcd1234", "description": "key"})
    assert created["ok"] is True
    assert created["secret"]["name"] == "OPENAI_API_KEY"
    assert created["secret"]["preview"] == "…1234"
    assert "sk-ant-abcd1234" not in json.dumps(created)

    listed = api.get_vault_secrets()
    assert [s["name"] for s in listed["secrets"]] == ["OPENAI_API_KEY"]
    assert "sk-ant-abcd1234" not in json.dumps(listed)  # masked

    removed = api.delete_vault_secret("OPENAI_API_KEY")
    assert removed == {"ok": True, "removed": True, "name": "OPENAI_API_KEY"}
    assert api.get_vault_secrets()["secrets"] == []


def test_create_with_policy_persists_allowed_hosts():
    api.create_vault_secret(
        {"name": "GH_PAT", "value": "ghp-x", "policy": {"allowed_hosts": ["api.github.com"], "auth": {"type": "bearer"}}}
    )
    secret = api.get_vault_secrets()["secrets"][0]
    assert secret["policy"]["allowed_hosts"] == ["api.github.com"]


def test_duplicate_name_conflict():
    api.create_vault_secret({"name": "DUP", "value": "one"})
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({"name": "DUP", "value": "two"})
    assert exc.value.code == "secret_exists"
    assert exc.value.status == 409


def test_invalid_name_rejected():
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({"name": "lower", "value": "x"})
    assert exc.value.code == "invalid_name"


def test_empty_value_rejected():
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({"name": "EMPTY", "value": ""})
    assert exc.value.code == "empty_value"


def test_delete_missing_is_404():
    with pytest.raises(api.VaultApiError) as exc:
        api.delete_vault_secret("NOPE")
    assert exc.value.code == "secret_not_found"
    assert exc.value.status == 404


def test_audit_lists_events_without_values():
    api.create_vault_secret({"name": "AUD_KEY", "value": "supersecret-AUD"})
    api.delete_vault_secret("AUD_KEY")
    audit = api.get_vault_audit()
    events = {e["event"] for e in audit["events"]}
    assert {"created", "deleted"} <= events
    assert "supersecret-AUD" not in json.dumps(audit)
