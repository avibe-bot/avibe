"""Tests for the vault REST wrappers in vibe/api.py.

REST create delegates sealing to avault and stores only the returned envelope.
"""

from __future__ import annotations

import json

import pytest

from storage import vault_service
from storage.vault_crypto import Sealed
from vibe import api


def _sealed(suffix: str = "1") -> Sealed:
    return Sealed(ciphertext=f"ct-{suffix}", nonce=f"n-{suffix}", wrap_meta=f"wm-{suffix}")


def test_create_list_delete_roundtrip(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed("api"))
    monkeypatch.setattr(api, "avault_seal", seal)

    created = api.create_vault_secret({"name": "OPENAI_API_KEY", "value": "sk-ant-abcd1234", "description": "key"})
    assert created["ok"] is True
    assert created["secret"]["name"] == "OPENAI_API_KEY"
    assert created["secret"]["preview"] == "…1234"
    assert "sk-ant-abcd1234" not in json.dumps(created)
    seal.assert_called_once_with("OPENAI_API_KEY", b"sk-ant-abcd1234")
    with api._vault_engine().connect() as conn:
        assert vault_service.get_envelope(conn, "OPENAI_API_KEY") == _sealed("api")

    listed = api.get_vault_secrets()
    assert [s["name"] for s in listed["secrets"]] == ["OPENAI_API_KEY"]
    assert "sk-ant-abcd1234" not in json.dumps(listed)

    removed = api.delete_vault_secret("OPENAI_API_KEY")
    assert removed == {"ok": True, "removed": True, "name": "OPENAI_API_KEY"}
    assert api.get_vault_secrets()["secrets"] == []


def test_create_with_policy_persists_allowed_hosts(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {"name": "GH_PAT", "value": "ghp-x", "policy": {"allowed_hosts": ["api.github.com"], "auth": {"type": "bearer"}}}
    )
    secret = api.get_vault_secrets()["secrets"][0]
    assert secret["policy"]["allowed_hosts"] == ["api.github.com"]


def test_duplicate_name_conflict(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal", Mock(return_value=_sealed()))
    api.create_vault_secret({"name": "DUP", "value": "one"})
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({"name": "DUP", "value": "two"})
    assert exc.value.code == "secret_exists"
    assert exc.value.status == 409


def test_invalid_name_rejected_before_avault(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed())
    monkeypatch.setattr(api, "avault_seal", seal)
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({"name": "lower", "value": "x"})
    assert exc.value.code == "invalid_name"
    seal.assert_not_called()


def test_empty_value_rejected_before_avault(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed())
    monkeypatch.setattr(api, "avault_seal", seal)
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({"name": "EMPTY", "value": ""})
    assert exc.value.code == "empty_value"
    seal.assert_not_called()


def test_avault_failure_maps_to_api_error(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal", Mock(side_effect=api.AvaultError("seal failed")))
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({"name": "FAIL_KEY", "value": "x"})
    assert exc.value.code == "avault_failed"


def test_delete_missing_is_404():
    with pytest.raises(api.VaultApiError) as exc:
        api.delete_vault_secret("NOPE")
    assert exc.value.code == "secret_not_found"
    assert exc.value.status == 404


def test_audit_lists_events_without_values(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal", Mock(return_value=_sealed()))
    api.create_vault_secret({"name": "AUD_KEY", "value": "supersecret-AUD"})
    api.delete_vault_secret("AUD_KEY")
    audit = api.get_vault_audit()
    events = {e["event"] for e in audit["events"]}
    assert {"created", "deleted"} <= events
    assert "supersecret-AUD" not in json.dumps(audit)
