from __future__ import annotations

import json

import pytest

from core.agent_auth_service import (
    BackendLoginInProgressError,
    _NativeLoginCoordinator,
)


def test_native_login_coordinator_refuses_a_second_owner_until_settlement() -> None:
    coordinator = _NativeLoginCoordinator()
    owner = coordinator.claim(
        "openai",
        "codex",
        flow_id="flow-owner",
        owner_ref="source-owner",
        hold_after_success=True,
    )

    with pytest.raises(BackendLoginInProgressError) as conflict:
        coordinator.claim(
            "openai",
            "codex",
            flow_id="flow-follower",
            owner_ref="source-follower",
            hold_after_success=True,
        )
    assert conflict.value.owner_ref == "source-owner"
    assert conflict.value.flow_id == "flow-owner"

    owner.release()
    fresh = coordinator.claim(
        "openai",
        "codex",
        flow_id="flow-fresh",
        owner_ref="source-fresh",
        hold_after_success=False,
    )
    fresh.release()


def test_recovered_native_journal_keeps_expired_and_legacy_pending_bindings(tmp_path) -> None:
    journal = tmp_path / "model_hub_oauth_flows.json"
    journal.write_text(
        json.dumps(
            {
                # Expiry is metadata, never a release timer.
                "legacy-old": {
                    "channel": "native_cli",
                    "source_id": "source-old",
                    "vendor": "openai",
                    "expires_at_iso": "2000-01-01T00:00:00+00:00",
                },
                # This released shape omitted optional fields entirely.
                "legacy-new": {
                    "channel": "native_cli",
                    "source_id": "source-new",
                    "vendor": "openai",
                },
            }
        ),
        encoding="utf-8",
    )
    coordinator = _NativeLoginCoordinator()
    coordinator.sync_persisted(str(journal))

    with pytest.raises(BackendLoginInProgressError) as first_conflict:
        coordinator.claim(
            "openai",
            "codex",
            flow_id="new-flow",
            owner_ref="new-source",
            hold_after_success=False,
        )
    assert first_conflict.value.owner_ref == "source-old"

    journal.write_text(
        json.dumps(
            {
                "legacy-new": {
                    "channel": "native_cli",
                    "source_id": "source-new",
                    "vendor": "openai",
                }
            }
        ),
        encoding="utf-8",
    )
    coordinator.sync_persisted(str(journal))
    with pytest.raises(BackendLoginInProgressError) as second_conflict:
        coordinator.claim(
            "openai",
            "codex",
            flow_id="new-flow",
            owner_ref="new-source",
            hold_after_success=False,
        )
    assert second_conflict.value.owner_ref == "source-new"

    coordinator.release_flow("legacy-new")
    fresh = coordinator.claim(
        "openai",
        "codex",
        flow_id="new-flow",
        owner_ref="new-source",
        hold_after_success=False,
    )
    fresh.release()
