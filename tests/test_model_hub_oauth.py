from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from core.handlers.model_hub.native_oauth import _signed_in
from core.handlers.model_hub.oauth import OAuthFlowRegistry
from core.handlers.model_hub.revocations import (
    CredentialRevocationJournal,
    PendingCredentialRevocation,
)


def test_flow_registry_persists_final_binding_shape(tmp_path):
    path = tmp_path / "oauth_flows.json"
    registry = OAuthFlowRegistry(path)

    registry.remember(
        "oaf_registry01",
        "hub",
        "src_oauth01",
        "anthropic",
    )

    binding = OAuthFlowRegistry(path).binding("oaf_registry01")
    assert binding is not None
    assert binding.completed is False
    assert set(json.loads(path.read_text(encoding="utf-8"))["oaf_registry01"]) == {
        "channel",
        "source_id",
        "vendor",
        "intent",
        "completed",
        "recovered",
        "interrupted_pairs",
        "client_nonce",
        "expires_at_iso",
        "terminal_state",
    }

    OAuthFlowRegistry(path).complete("oaf_registry01")
    completed = OAuthFlowRegistry(path).binding("oaf_registry01")
    assert completed is not None
    assert completed.completed is True


def test_flow_registry_rejects_legacy_binding_shapes(tmp_path):
    path = tmp_path / "oauth_flows.json"
    path.write_text(
        json.dumps(
            {
                "oaf_legacy01": {
                    "channel": "hub",
                    "source_id": "src_legacy01",
                    "vendor": "anthropic",
                }
            }
        ),
        encoding="utf-8",
    )

    assert OAuthFlowRegistry(path).binding("oaf_legacy01") is None


def test_flow_registry_claims_exact_nonce_tuple_and_replays_committed_flow(tmp_path):
    now = [datetime(2026, 7, 25, tzinfo=timezone.utc)]
    registry = OAuthFlowRegistry(tmp_path / "oauth_flows.json", now=lambda: now[0])
    nonce = "ofn_01j5w8z7p4n6q2rt"

    owner = registry.claim_nonce(nonce, "anthropic", "hub")
    follower = registry.claim_nonce(nonce, "anthropic", "hub")
    assert owner.owner is True
    assert owner.status == follower.status == "in_flight"
    assert follower.owner is False

    registry.remember(
        "oaf_nonce01",
        "hub",
        "src_nonce01",
        "anthropic",
        client_nonce=nonce,
        expires_at_iso="2026-07-25T00:15:00+00:00",
    )
    committed = registry.claim_nonce(nonce, "anthropic", "hub")
    assert committed.status == "committed"
    assert committed.flow_id == "oaf_nonce01"


def test_flow_registry_releases_failed_claim_and_keeps_tuples_independent(tmp_path):
    registry = OAuthFlowRegistry(tmp_path / "oauth_flows.json")
    nonce = "ofn_01j5w8z7p4n6q2rt"
    registry.claim_nonce(nonce, "anthropic", "hub")
    registry.release_nonce(nonce, "anthropic", "hub")

    fresh = registry.claim_nonce(nonce, "anthropic", "hub")
    other_vendor = registry.claim_nonce(nonce, "openai", "hub")
    other_channel = registry.claim_nonce(nonce, "anthropic", "native_cli")
    assert fresh.owner is True
    assert other_vendor.owner is True
    assert other_channel.owner is True


def test_flow_registry_retains_nonce_cancel_until_expiry_then_releases(tmp_path):
    current = [datetime(2026, 7, 25, tzinfo=timezone.utc)]
    registry = OAuthFlowRegistry(
        tmp_path / "oauth_flows.json",
        now=lambda: current[0],
    )
    nonce = "ofn_01j5w8z7p4n6q2rt"
    registry.remember(
        "oaf_cancel01",
        "hub",
        "src_cancel01",
        "anthropic",
        client_nonce=nonce,
        expires_at_iso="2026-07-25T00:15:00+00:00",
    )
    registry.retain_cancelled("oaf_cancel01")
    assert registry.binding("oaf_cancel01").terminal_state == "cancelled"
    assert registry.claim_nonce(nonce, "anthropic", "hub").status == "committed"

    current[0] = datetime(2026, 7, 25, 0, 15, tzinfo=timezone.utc)
    released = registry.claim_nonce(nonce, "anthropic", "hub")
    assert released.owner is True


def test_flow_registry_coalesced_waiter_observes_release(tmp_path):
    registry = OAuthFlowRegistry(tmp_path / "oauth_flows.json")
    nonce = "ofn_01j5w8z7p4n6q2rt"
    owner = registry.claim_nonce(nonce, "anthropic", "hub")
    follower = registry.claim_nonce(nonce, "anthropic", "hub")

    async def wait_for_release():
        task = asyncio.create_task(registry.wait_for_nonce(follower))
        await asyncio.sleep(0)
        registry.release_nonce(nonce, "anthropic", "hub")
        return await task

    result = asyncio.run(wait_for_release())
    assert owner.owner is True
    assert result.status == "released"


def test_flow_registry_returns_latest_pending_reauth_for_source(tmp_path):
    registry = OAuthFlowRegistry(tmp_path / "oauth_flows.json")
    registry.remember(
        "oaf_first001",
        "native_cli",
        "src_native001",
        "anthropic",
        intent="reauth",
    )
    registry.remember(
        "oaf_second01",
        "native_cli",
        "src_native001",
        "anthropic",
        intent="reauth",
    )
    registry.remember(
        "oaf_create01",
        "hub",
        "src_native001",
        "anthropic",
    )
    registry.complete("oaf_second01")

    pending = registry.pending_reauth("src_native001")

    assert pending is not None
    assert pending[0] == "oaf_first001"

    registry.remember(
        "oaf_replaced1",
        "native_cli",
        "src_native001",
        "anthropic",
        intent="reauth",
        replace_flow_id="oaf_first001",
    )

    assert registry.binding("oaf_first001") is None
    replaced = registry.pending_reauth("src_native001")
    assert replaced is not None
    assert replaced[0] == "oaf_replaced1"


def test_native_status_trusts_codex_keyring_success_but_not_active_api_keys():
    assert _signed_in(
        "codex",
        {
            "active_auth_mode": "none",
            "has_chatgpt_tokens": False,
            "auth_mode_uncertain": False,
        },
    )
    assert not _signed_in(
        "codex",
        {
            "active_auth_mode": "api_key",
            "has_chatgpt_tokens": True,
        },
    )


def test_native_status_does_not_override_explicit_claude_api_key_mode():
    assert not _signed_in(
        "claude",
        {
            "active_auth_mode": "api_key",
            "has_oauth_credentials": True,
        },
    )
    assert _signed_in("claude", {"has_oauth_credentials": True})


def test_revocation_journal_round_trips_final_entries_across_instances(tmp_path):
    path = tmp_path / "revocations.json"
    journal = CredentialRevocationJournal(path)

    journal.add("src_cleanup001", "cred_old001")
    journal.add(
        "src_cleanup002",
        "cred_oauth002",
        operation="cleanup_orphaned_oauth_material",
    )

    reconstructed = CredentialRevocationJournal(path)
    assert reconstructed.list() == [
        PendingCredentialRevocation(
            source_id="src_cleanup001",
            credential_ref="cred_old001",
            operation="revoke_credential",
        ),
        PendingCredentialRevocation(
            source_id="src_cleanup002",
            credential_ref="cred_oauth002",
            operation="cleanup_orphaned_oauth_material",
        ),
    ]

    reconstructed.remove("src_cleanup001", "cred_old001")
    assert CredentialRevocationJournal(path).list() == [
        PendingCredentialRevocation(
            source_id="src_cleanup002",
            credential_ref="cred_oauth002",
            operation="cleanup_orphaned_oauth_material",
        )
    ]


@pytest.mark.parametrize(
    "payload",
    (
        {},
        [{"source_id": "src_cleanup001", "credential_ref": "cred_old001"}],
        [
            {
                "source_id": "src_cleanup001",
                "credential_ref": "cred_old001",
                "operation": "revoke_credential",
                "unexpected": True,
            }
        ],
        [
            {
                "source_id": "src_cleanup001",
                "credential_ref": "cred_old001",
                "operation": "revoke_credential",
            },
            {
                "source_id": "src_cleanup001",
                "credential_ref": "cred_old001",
                "operation": "revoke_credential",
            },
        ],
    ),
)
def test_revocation_journal_rejects_noncanonical_state_without_overwriting(
    tmp_path,
    payload,
):
    path = tmp_path / "revocations.json"
    original = json.dumps(payload)
    path.write_text(original, encoding="utf-8")
    journal = CredentialRevocationJournal(path)

    with pytest.raises(OSError, match="invalid credential revocation journal"):
        journal.add("src_new0001", "cred_new001")

    assert path.read_text(encoding="utf-8") == original


def test_revocation_journal_rejects_conflicting_cleanup_operations(tmp_path):
    journal = CredentialRevocationJournal(tmp_path / "revocations.json")
    journal.add("src_cleanup001", "cred_old001")

    with pytest.raises(ValueError, match="conflicting operation"):
        journal.add(
            "src_cleanup001",
            "cred_old001",
            operation="cleanup_orphaned_oauth_material",
        )
