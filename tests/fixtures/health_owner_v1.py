"""Frozen released owner gate at febd71b7c8, for Cloud compatibility tests."""
from __future__ import annotations

from core.mobile_health_contract import MobileError
from storage.mobile_store import MobileStore


def current_identity(store: MobileStore) -> dict:
    """The instance owner, from a live control-plane answer.

    Any instance kind and any sharing state qualifies: who may reach this
    instance, and therefore its Agents and synced health data, is the owner's
    own access setting. Health only binds the phone to the current owner.
    An answer that cannot confirm the owner pauses this call; only a live,
    confirmed owner change revokes and purges (explicit revoke/unpair aside).
    """
    from config.v2_config import V2Config, config_file_lock
    from vibe import permissions
    from vibe.account_session_revoke import pairing_epoch
    from vibe.binding_admission import reset_in_progress
    from vibe.remote_access import device_binding

    unavailable = MobileError("instance_eligibility_unavailable", 503)
    try:
        config = V2Config.load()
        cloud = config.remote_access.vibe_cloud
        if reset_in_progress() or cloud.runtime_credentials() is None:
            raise unavailable
        observed = store.active_device()
        result = permissions.get_current_permissions(config)
    except permissions.PermissionsError:
        raise unavailable from None
    except (FileNotFoundError, OSError, ValueError):
        raise unavailable from None
    if result.source != "live":
        raise unavailable
    instance = result.projection["instance"]
    owner = (result.projection["access"]["owner"].get("email") or "").strip().casefold()
    if not owner or not cloud.instance_id or instance["id"] != cloud.instance_id:
        raise unavailable
    identity = {"id": instance["id"], "name": instance.get("name") or instance["id"], "owner": owner}
    # Network I/O holds no local lock. Only the generation that asked for
    # this answer may use it to read or purge; reset and publication use the
    # same config lock. A stale answer is unavailability, not an owner change.
    with config_file_lock():
        current = V2Config.load()
        if (reset_in_progress() or current.remote_access.vibe_cloud.runtime_credentials() is None
                or pairing_epoch(current) != pairing_epoch(config) or device_binding(current) != device_binding(config)):
            raise unavailable
        store.retire_other_identities(instance=identity['id'], owner=owner, expected_device=observed['id'] if observed else None)
    return identity
