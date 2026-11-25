"""Only the original machine's durable publication can answer its read-only follow-up."""

import json
import re

import pytest

from config import paths
from config.atomic_io import write_atomic
from tests.test_desktop_claim_pipe import _parent
from tests.test_pairing_recovery import pairing_host as pairing_host
from tests.test_personal_claims import claim_peer as claim_peer
from vibe import personal_claims, remote_access


def _replace(which):
    if which == "pending":
        write_atomic(remote_access._pending_pairing_path(), "fixture-private-pending")
    elif which in ("other_claim", "marker_republished"):
        marker = personal_claims._marker()
        raw = marker.read_bytes()
        if which == "other_claim":
            payload = json.loads(raw)
            payload["bound"]["claim_id"] = "00000000-0000-0000-0000-000000000001"
            raw = json.dumps(payload).encode()
        write_atomic(marker, raw)
    elif which == "different_secret":
        # Same signed-in account, exact same instance ID and public hostname. Only the binding changed.
        path = paths.get_config_path()
        payload = json.loads(path.read_bytes())
        payload["remote_access"]["vibe_cloud"]["instance_secret"] = "fixture-subsequent-binding"
        write_atomic(path, json.dumps(payload))
    elif which == "different_installation":
        write_atomic(
            paths.get_state_dir() / "installation-id.json",
            json.dumps({"schema_version": 1, "installation_id": "00000000-0000-0000-0000-000000000002"}),
        )


@pytest.mark.parametrize(
    "when,which",
    [
        ("after", "unchanged"),
        ("before", "different_secret"),
        ("before", "other_claim"),
        ("before", "different_installation"),
        ("after", "pending"),
        ("after", "marker_republished"),
        ("after", "other_claim"),
        ("after", "different_secret"),
        ("before", "caller_selects"),
    ],
)
def test_publication_follows_redeemed_ticket_and_recorded_generation_on_the_original_pipe(
    claim_peer, pairing_host, when, which
):
    with _parent() as p:
        ticket = p.ticket()
        p.send(
            op="arm", claim_id=ticket["claim_id"], kind="new", hostname=claim_peer.hostname, existing_instance_id=None
        )
        assert p.receive() == {"v": 1, "event": "result", "outcome": "publication_finished"}
        # Everything from here on is a read, not a second grant, accept or redeem.
        assert claim_peer.decisions == ["owner-id"] and len(pairing_host[1]) == 1
        if when == "before":
            _replace(which)
        if which == "caller_selects":
            p.send(op="observe_publication", claim_id=ticket["claim_id"])
        else:
            p.send(op="observe_publication")
        if when == "after":
            w = p.receive()
            assert set(w) == {"v", "event", "claim_id", "instance_id", "generation"}
            assert (w["v"], w["event"], w["claim_id"], w["instance_id"]) == (
                1,
                "publication",
                ticket["claim_id"],
                "inst_A",
            )
            assert re.fullmatch(r"[a-f0-9]{64}", w["generation"])
            _replace(which)
            p.send(op="recheck", generation=w["generation"], challenge="e" * 32)
        if which == "unchanged":
            assert p.receive() == {"v": 1, "event": "unchanged", "generation": w["generation"], "challenge": "e" * 32}
        elif which != "caller_selects":
            assert p.receive() == {
                "v": 1,
                "event": "result",
                "outcome": "error",
                "code": "pairing_recovery_required" if which == "pending" else "local_binding_changed",
            }
        assert p.receive() is None
    assert claim_peer.decisions == ["owner-id"] and len(pairing_host[1]) == 1
