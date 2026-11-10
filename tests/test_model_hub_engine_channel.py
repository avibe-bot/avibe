"""The signed Model Hub engine channel contract (docs/plans/model-hub-engine-channel.md).

Each table states what an installed Avibe must refuse: bytes no shipped key
signed, a payload outside the strict schema, an index older than or different
from one already accepted, and a selection that moves an engine anywhere but
forward, except on an explicit withdrawal.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from core import model_hub_engine_channel as channel
from core.model_hub_engine_channel import ChannelError, ResolutionAction
from tests.engine_channel_support import (
    KEY_ID,
    NOW,
    LocalVerifier,
    Signer,
    encode,
    envelope_bytes,
    manifest_bytes,
    payload_document,
    release_entry,
    signed,
    tombstone,
)

V63_1 = "model-hub-engine-v8.0.63-1"
V63_2 = "model-hub-engine-v8.0.63-2"
V63_3 = "model-hub-engine-v8.0.63-3"
V70_1 = "model-hub-engine-v8.0.70-1"


@pytest.fixture
def signer() -> Signer:
    return Signer()


def _reason(call) -> str:
    with pytest.raises(ChannelError) as raised:
        call()
    return raised.value.reason


# ----- authentication --------------------------------------------------------


def test_the_signed_digest_is_the_fixed_domain_followed_by_the_exact_payload() -> None:
    # avault's shared vector "domain-separated-payload-digest" (verify_ecdsa_secp256k1_der.json).
    vectors = json.loads(
        (Path(__file__).parent / "fixtures/avault/verify_ecdsa_secp256k1_der.json").read_text(encoding="utf-8")
    )
    (case,) = [item for item in vectors["cases"] if item["name"] == "domain-separated-payload-digest"]
    preimage = case["digest_preimage"]

    assert preimage["domain_utf8"].encode() == channel.SIGNATURE_DOMAIN
    assert channel.channel_digest(preimage["payload_utf8"].encode()) == case["digest_hex"]
    assert vectors["scheme"] == channel.SIGNATURE_SCHEME


def test_an_index_signed_by_a_shipped_key_yields_its_exact_payload_and_manifest_bytes(signer) -> None:
    manifest = manifest_bytes()
    payload = encode(payload_document(7, releases=[release_entry(manifest)]))

    envelope, parsed = channel.authenticate(
        envelope_bytes(signer, payload), roots=signer.roots(), verifier=LocalVerifier()
    )

    assert envelope.payload == payload and parsed.raw == payload
    assert parsed.sequence == 7
    assert parsed.releases[0].manifest_bytes == manifest
    assert parsed.releases[0].asset_release_tag == V63_2


def test_an_avibe_that_ships_no_key_never_reaches_the_verifier(signer) -> None:
    verifier = LocalVerifier()

    assert _reason(lambda: channel.authenticate(signed(signer), roots=channel.TrustRoots(), verifier=verifier)) == (
        "uncommissioned"
    )
    assert verifier.calls == 0


def test_the_key_comes_from_the_shipped_roots_never_from_the_index(signer) -> None:
    """MH-ENGINE-CHANNEL-001: only a key shipped in Avibe can authenticate an index."""

    other = Signer()
    verifier = LocalVerifier()

    # Signed by a key Avibe does not ship, under an id it does not ship.
    assert _reason(
        lambda: channel.authenticate(
            envelope_bytes(other, encode(payload_document()), key_id="attacker-key"),
            roots=signer.roots(),
            verifier=verifier,
        )
    ) == "key_unknown"
    assert verifier.calls == 0
    # Signed by another key, claiming the shipped id: checked against the shipped key.
    assert _reason(
        lambda: channel.authenticate(
            envelope_bytes(other, encode(payload_document())), roots=signer.roots(), verifier=verifier
        )
    ) == "signature_invalid"


def test_an_unverified_payload_is_never_parsed(signer, monkeypatch) -> None:
    parsed = []
    monkeypatch.setattr(channel, "parse_payload", lambda raw: parsed.append(raw))
    forged = channel.encode_envelope(
        key_id=KEY_ID, payload=encode(payload_document()), signature=Signer().sign(channel.channel_digest(b"x"))
    )

    assert _reason(lambda: channel.authenticate(forged, roots=signer.roots(), verifier=LocalVerifier())) == (
        "signature_invalid"
    )
    assert parsed == []


def _envelope_document(signer: Signer) -> dict:
    return json.loads(signed(signer))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d.update(schema_version=True),
        lambda d: d.update(schema_version="1"),
        lambda d: d.update(key_id="Bad Key"),
        lambda d: d.update(key_id=7),
        lambda d: d.update(algorithm="ed25519"),
        lambda d: d.update(public_key="02" + "11" * 32),
        lambda d: d.pop("signature"),
        lambda d: d.update(signature=d["signature"].upper()),
        lambda d: d.update(signature="0x" + d["signature"]),
        lambda d: d.update(signature=d["signature"] + "00" * 80),
        lambda d: d.update(payload=d["payload"].rstrip("=") + "!"),
        lambda d: d.update(payload=d["payload"] + "\n"),
        lambda d: d.update(payload=""),
        lambda d: d.update(payload=base64.urlsafe_b64encode(b"\xfb\xff payload").decode()),
    ],
)
def test_an_envelope_outside_the_strict_shape_is_rejected_before_any_signature_check(signer, mutate) -> None:
    document = _envelope_document(signer)
    mutate(document)
    verifier = LocalVerifier()

    assert _reason(lambda: channel.authenticate(encode(document), roots=signer.roots(), verifier=verifier)) == (
        "envelope_invalid"
    )
    assert verifier.calls == 0


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"\xff\xfe",
        b'{"schema_version":1,"schema_version":1,"key_id":"k","payload":"eA==","signature":"3006020101020101"}',
        b'{"schema_version":NaN,"key_id":"k","payload":"eA==","signature":"3006020101020101"}',
        b"[" * 100_000,
        b" " * (channel.MAX_ENVELOPE_BYTES + 1),
    ],
)
def test_bytes_that_are_not_one_bounded_json_envelope_are_rejected(signer, raw) -> None:
    assert _reason(lambda: channel.authenticate(raw, roots=signer.roots(), verifier=LocalVerifier())) == (
        "envelope_invalid"
    )


def test_base64_with_a_second_encoding_of_the_same_bytes_is_rejected(signer) -> None:
    # "eB==" decodes to the same byte as the canonical "eA==".
    document = _envelope_document(signer)
    document["payload"] = "eB=="

    assert _reason(lambda: channel.authenticate(encode(document), roots=signer.roots(), verifier=LocalVerifier())) == (
        "envelope_invalid"
    )


# ----- payload ---------------------------------------------------------------


def _payload_reason(document: object) -> str:
    return _reason(lambda: channel.parse_payload(document if isinstance(document, bytes) else encode(document)))


def _mutated_manifest(mutate) -> dict:
    manifest = json.loads(manifest_bytes())
    mutate(manifest)
    return release_entry(json.dumps(manifest).encode())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(repository="someone/else"),
        lambda d: d.update(channel="beta"),
        lambda d: d.update(sequence=0),
        lambda d: d.update(sequence=-1),
        lambda d: d.update(sequence=1.0),
        lambda d: d.update(sequence=True),
        lambda d: d.update(sequence="1"),
        lambda d: d.update(sequence=2**53),
        lambda d: d.update(issued_at="2026-11-10 06:00:00"),
        lambda d: d.update(issued_at="2026-11-10T06:00:00+00:00"),
        lambda d: d.update(issued_at="2026-02-30T06:00:00Z"),
        lambda d: d.update(expires_at=d["issued_at"]),
        lambda d: d.update(expires_at="2026-11-09T06:00:00Z"),
        lambda d: d.update(expires_at="2026-11-17T06:00:01Z"),
        lambda d: d.update(max_avibe_version="4.0.0"),
        lambda d: d.pop("revoked"),
        lambda d: d.update(releases={}),
        lambda d: d.update(releases=[release_entry(manifest_bytes())] * 2),
        lambda d: d.update(releases=[release_entry(manifest_bytes()), release_entry(manifest_bytes(salt="x"))]),
        lambda d: d.update(
            releases=[release_entry(manifest_bytes(build=n)) for n in range(1, channel.MAX_RELEASES + 2)]
        ),
        lambda d: d["releases"][0].update(min_avibe_version="3.2"),
        lambda d: d["releases"][0].update(min_avibe_version="3.2.6rc1"),
        lambda d: d["releases"][0].update(min_avibe_version="v3.2.6"),
        lambda d: d["releases"][0].update(min_avibe_version="03.2.6"),
        lambda d: d["releases"][0].update(min_avibe_version=3),
        lambda d: d["releases"][0].update(manifest_sha256="0" * 64),
        lambda d: d["releases"][0].update(manifest_sha256=d["releases"][0]["manifest_sha256"].upper()),
        lambda d: d["releases"][0].update(url="https://example.invalid/engine.tar.gz"),
        lambda d: d["releases"][0].pop("min_avibe_version"),
        lambda d: d.update(releases=[_mutated_manifest(lambda m: m["assets"].pop())]),
        lambda d: d.update(releases=[_mutated_manifest(lambda m: m["assets"][0].update(url="https://x.invalid/a"))]),
        lambda d: d.update(releases=[_mutated_manifest(lambda m: m["assets"][0].update(sha256="short"))]),
        lambda d: d.update(releases=[_mutated_manifest(lambda m: m.update(asset_release_tag="latest"))]),
        lambda d: d.update(releases=[release_entry(b"x" * (channel.MAX_MANIFEST_BYTES + 1))]),
        lambda d: d.update(revoked=[tombstone(V63_1, V63_2), tombstone(V63_1, V63_2, "again")]),
        lambda d: d.update(revoked=[tombstone(V63_1, V63_3)]),
        lambda d: d.update(revoked=[tombstone(V63_2, V63_2)]),
        lambda d: d.update(revoked=[tombstone("v8.0.63", V63_2)]),
        lambda d: d.update(revoked=[tombstone(V63_1, V63_2, "")]),
        lambda d: d.update(revoked=[tombstone(V63_1, V63_2, "line one\nline two")]),
        lambda d: d.update(revoked=[tombstone(V63_1, V63_2, "x" * (channel.MAX_REASON_CHARS + 1))]),
        lambda d: d.update(revoked=[{**tombstone(V63_1, V63_2), "delete_assets": True}]),
    ],
)
def test_a_payload_outside_the_strict_schema_is_rejected(mutate) -> None:
    document = copy.deepcopy(payload_document())
    mutate(document)

    assert _payload_reason(document) == "payload_invalid"


def test_a_manifest_that_repeats_a_field_is_rejected_whichever_value_a_reader_would_keep() -> None:
    manifest = manifest_bytes().replace(b'"license": "MIT"', b'"license": "GPL", "license": "MIT"')

    assert _payload_reason(payload_document(releases=[release_entry(manifest)])) == "payload_invalid"


def test_a_payload_carries_withdrawals_whose_fallback_it_also_carries() -> None:
    document = payload_document(
        releases=[release_entry(manifest_bytes(build=1)), release_entry(manifest_bytes(build=2))],
        revoked=[tombstone(V63_2, V63_1)],
    )

    payload = channel.parse_payload(encode(document))

    assert payload.tombstone(V63_2) == channel.Tombstone(V63_2, V63_1, "regression in streaming")
    assert payload.release(V63_1) is not None


# ----- trust roots -----------------------------------------------------------


def test_trust_roots_name_compressed_keys_and_a_sequence_floor(signer) -> None:
    roots = channel.parse_trust_roots(
        encode({"schema_version": 1, "sequence_floor": 40, "roots": [{"key_id": KEY_ID, "public_key": signer.public_key}]})
    )

    assert roots.commissioned and roots.sequence_floor == 40
    assert roots.find(KEY_ID) == channel.TrustRoot(KEY_ID, signer.public_key)
    assert not channel.parse_trust_roots(encode({"schema_version": 1, "sequence_floor": 0, "roots": []})).commissioned


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d.update(sequence_floor=-1),
        lambda d: d.update(algorithm="ed25519"),
        lambda d: d["roots"][0].update(public_key=d["roots"][0]["public_key"].upper()),
        lambda d: d["roots"][0].update(public_key="04" + "11" * 64),
        lambda d: d["roots"][0].update(public_key="11" * 32),
        lambda d: d["roots"][0].update(key_id=""),
        lambda d: d["roots"].append(dict(d["roots"][0])),
        lambda d: d.update(roots=[{"key_id": f"k{n}", "public_key": f"02{n:064x}"} for n in range(5)]),
    ],
)
def test_a_trust_root_document_outside_the_strict_shape_is_refused(signer, mutate) -> None:
    document = {"schema_version": 1, "sequence_floor": 0, "roots": [{"key_id": KEY_ID, "public_key": signer.public_key}]}
    mutate(document)

    with pytest.raises(ValueError):
        channel.parse_trust_roots(encode(document))


# ----- anti-replay state -----------------------------------------------------


def _accept(store: channel.ChannelStateStore, signer: Signer, raw: bytes, *, floor: int = 0, now=NOW) -> bool:
    envelope, payload = channel.authenticate(raw, roots=signer.roots(), verifier=LocalVerifier())
    return store.accept(envelope, payload, floor=floor, now=now)


def test_a_newer_index_replaces_the_recorded_one_and_an_identical_one_changes_nothing(tmp_path, signer) -> None:
    store = channel.ChannelStateStore(tmp_path / "channel")
    first, second = signed(signer, 5), signed(signer, 6)

    assert _accept(store, signer, first) is True
    assert _accept(store, signer, first) is False
    assert _accept(store, signer, second) is True

    snapshot = store.cached(roots=signer.roots(), verifier=LocalVerifier(), now=NOW)
    assert snapshot.payload.sequence == 6 and snapshot.fresh
    assert snapshot.envelope.raw == second


def _history(signer: Signer) -> dict[str, bytes]:
    releases = [release_entry(manifest_bytes(build=1)), release_entry(manifest_bytes(build=2))]
    return {
        "base": signed(signer, 10, releases=releases, revoked=[tombstone(V63_2, V63_1)]),
        "older": signed(signer, 9, releases=releases),
        "same sequence, other bytes": signed(signer, 10, releases=releases),
        "withdrawal dropped": signed(signer, 11, releases=releases),
        "withdrawal reworded": signed(signer, 11, releases=releases, revoked=[tombstone(V63_2, V63_1, "never mind")]),
        "release bytes changed": signed(
            signer, 11,
            releases=[release_entry(manifest_bytes(build=1, salt="x")), release_entry(manifest_bytes(build=2))],
            revoked=[tombstone(V63_2, V63_1)],
        ),
        "release minimum changed": signed(
            signer, 11,
            releases=[release_entry(manifest_bytes(build=1), "3.3.0"), release_entry(manifest_bytes(build=2))],
            revoked=[tombstone(V63_2, V63_1)],
        ),
    }


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("older", "replayed"),
        ("same sequence, other bytes", "equivocation"),
        ("withdrawal dropped", "tombstone_removed"),
        ("withdrawal reworded", "tombstone_removed"),
        ("release bytes changed", "release_mutated"),
        ("release minimum changed", "release_mutated"),
    ],
)
def test_an_index_that_goes_back_or_rewrites_history_is_refused_and_leaves_state_untouched(
    tmp_path, signer, name, reason
) -> None:
    """MH-ENGINE-CHANNEL-002: replay, equivocation and rewritten history are refused without changing state."""

    store = channel.ChannelStateStore(tmp_path / "channel")
    history = _history(signer)
    _accept(store, signer, history["base"])
    before = store.state_path.read_bytes()

    assert _reason(lambda: _accept(store, signer, history[name])) == reason
    assert store.state_path.read_bytes() == before


def test_a_release_that_was_once_listed_keeps_its_identity_after_it_stops_being_listed(tmp_path, signer) -> None:
    store = channel.ChannelStateStore(tmp_path / "channel")
    both = [release_entry(manifest_bytes(build=1)), release_entry(manifest_bytes(build=2))]
    _accept(store, signer, signed(signer, 1, releases=both))
    # Build 1 stops being listed: allowed, and not a withdrawal.
    assert _accept(store, signer, signed(signer, 2, releases=both[1:])) is True

    relisted = [release_entry(manifest_bytes(build=1, salt="x")), both[1]]
    assert _reason(lambda: _accept(store, signer, signed(signer, 3, releases=relisted))) == "release_mutated"


@pytest.mark.parametrize(
    ("now", "floor", "reason"),
    [
        (NOW + timedelta(days=7), 0, "expired"),
        (NOW + timedelta(days=30), 0, "expired"),
        (NOW - channel.CLOCK_SKEW - timedelta(seconds=1), 0, "not_yet_valid"),
        (NOW, 6, "replayed"),
    ],
)
def test_an_index_outside_its_validity_or_below_the_shipped_floor_is_not_recorded(
    tmp_path, signer, now, floor, reason
) -> None:
    store = channel.ChannelStateStore(tmp_path / "channel")

    assert _reason(lambda: _accept(store, signer, signed(signer, 5), floor=floor, now=now)) == reason
    assert not store.state_path.exists()


def test_a_small_clock_difference_does_not_reject_a_just_issued_index(tmp_path, signer) -> None:
    store = channel.ChannelStateStore(tmp_path / "channel")

    assert _accept(store, signer, signed(signer, 5), now=NOW - channel.CLOCK_SKEW) is True


def test_an_expired_recorded_index_still_proves_provenance_but_is_not_fresh(tmp_path, signer) -> None:
    store = channel.ChannelStateStore(tmp_path / "channel")
    _accept(store, signer, signed(signer, 5))

    snapshot = store.cached(roots=signer.roots(), verifier=LocalVerifier(), now=NOW + timedelta(days=60))

    assert snapshot.payload.sequence == 5 and snapshot.fresh is False
    # The same expired bytes offered again as new input are refused.
    assert _reason(lambda: _accept(store, signer, signed(signer, 5), now=NOW + timedelta(days=60))) == "expired"


@pytest.mark.parametrize(
    "damage",
    [
        lambda path: path.write_bytes(b"{"),
        lambda path: path.write_bytes(b"[]"),
        lambda path: path.write_text(json.dumps({**json.loads(path.read_text()), "sequence": "5"})),
        lambda path: path.write_text(json.dumps({**json.loads(path.read_text()), "schema_version": 2})),
    ],
)
def test_damaged_state_fails_closed_instead_of_starting_over(tmp_path, signer, damage) -> None:
    store = channel.ChannelStateStore(tmp_path / "channel")
    _accept(store, signer, signed(signer, 5))
    damage(store.state_path)
    damaged = store.state_path.read_bytes()

    assert _reason(lambda: _accept(store, signer, signed(signer, 6))) == "state_unreadable"
    assert _reason(lambda: store.cached(roots=signer.roots(), verifier=LocalVerifier(), now=NOW)) == "state_unreadable"
    assert store.state_path.read_bytes() == damaged


def test_a_recorded_envelope_swapped_on_disk_is_not_served_as_the_accepted_index(tmp_path, signer) -> None:
    store = channel.ChannelStateStore(tmp_path / "channel")
    _accept(store, signer, signed(signer, 5))
    state = json.loads(store.state_path.read_text())
    state["envelope"] = base64.b64encode(signed(signer, 4)).decode()
    store.state_path.write_text(json.dumps(state))

    assert _reason(lambda: store.cached(roots=signer.roots(), verifier=LocalVerifier(), now=NOW)) == "state_unreadable"


def test_concurrent_refreshes_never_move_the_recorded_sequence_back(tmp_path, signer) -> None:
    store = channel.ChannelStateStore(tmp_path / "channel")
    indexes = {sequence: signed(signer, sequence) for sequence in range(1, 13)}
    start = threading.Barrier(len(indexes))
    outcomes: list[str] = []

    def refresh(sequence: int) -> None:
        start.wait()
        try:
            _accept(store, signer, indexes[sequence])
            outcomes.append("accepted")
        except ChannelError as exc:
            outcomes.append(exc.reason)

    threads = [threading.Thread(target=refresh, args=(sequence,)) for sequence in indexes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert store.load().sequence == 12
    assert set(outcomes) <= {"accepted", "replayed"} and outcomes.count("accepted") >= 1


# ----- selection -------------------------------------------------------------


def _payload(releases: list[tuple[str, int, str]], revoked: list[dict] | None = None) -> channel.ChannelPayload:
    entries = [release_entry(manifest_bytes(version, build), minimum) for version, build, minimum in releases]
    return channel.parse_payload(encode(payload_document(releases=entries, revoked=revoked)))


def _resolve(payload, **overrides):
    arguments = {
        "fresh": True,
        "avibe_version": "3.2.6",
        "host_platform": "linux-amd64",
        "bundled": V63_1,
        "current": V63_1,
    }
    return channel.resolve(payload, **{**arguments, **overrides})


LISTED = [("v8.0.63", 1, "3.2.6"), ("v8.0.63", 2, "3.2.6"), ("v8.0.63", 3, "3.2.7"), ("v8.0.70", 1, "3.3.0")]


@pytest.mark.parametrize(
    ("overrides", "action", "target", "reason"),
    [
        # The newest release this Avibe may run, by upstream version then build revision.
        ({}, ResolutionAction.ADOPT, V63_2, "newer_release"),
        ({"avibe_version": "3.2.7"}, ResolutionAction.ADOPT, V63_3, "newer_release"),
        ({"avibe_version": "3.3.0"}, ResolutionAction.ADOPT, V70_1, "newer_release"),
        ({"avibe_version": "3.10.0"}, ResolutionAction.ADOPT, V70_1, "newer_release"),
        # A stable minimum excludes that version's own pre-release.
        ({"avibe_version": "3.2.7rc1"}, ResolutionAction.ADOPT, V63_2, "newer_release"),
        ({"avibe_version": "3.2.6rc2"}, ResolutionAction.KEEP, None, "up_to_date"),
        ({"avibe_version": "3.3.0rc1", "current": V63_3}, ResolutionAction.KEEP, None, "up_to_date"),
        # Builds that name no published release hold no minimum.
        ({"avibe_version": "0.0.0.dev0"}, ResolutionAction.KEEP, None, "avibe_version_unreleased"),
        ({"avibe_version": "3.3.1.dev4+g1a2b3c4"}, ResolutionAction.KEEP, None, "avibe_version_unreleased"),
        ({"avibe_version": "3.3.0+local"}, ResolutionAction.KEEP, None, "avibe_version_unreleased"),
        ({"avibe_version": "not a version"}, ResolutionAction.KEEP, None, "avibe_version_unreleased"),
        # Never backward, never below the bundled engine, never to a failed candidate.
        ({"current": V63_2}, ResolutionAction.KEEP, None, "up_to_date"),
        ({"current": V70_1, "avibe_version": "3.3.0"}, ResolutionAction.KEEP, None, "up_to_date"),
        ({"bundled": V63_3, "current": V63_1}, ResolutionAction.KEEP, None, "up_to_date"),
        ({"quarantined": frozenset({V63_2})}, ResolutionAction.KEEP, None, "up_to_date"),
        ({"avibe_version": "3.2.7", "quarantined": frozenset({V63_3})}, ResolutionAction.ADOPT, V63_2, "newer_release"),
        # Without authority to adopt, nothing is selected.
        ({"fresh": False}, ResolutionAction.KEEP, None, "index_stale"),
        ({"allow_adoption": False}, ResolutionAction.KEEP, None, "adoption_disabled"),
        ({"host_platform": "freebsd-amd64"}, ResolutionAction.KEEP, None, "up_to_date"),
        ({"current": "custom-engine"}, ResolutionAction.KEEP, None, "engine_identity_unrecognized"),
    ],
)
def test_ordinary_selection_only_moves_forward_to_the_newest_release_this_avibe_may_run(
    overrides, action, target, reason
) -> None:
    """MH-ENGINE-CHANNEL-003: selection honors the minimum Avibe, the host, and forward-only movement."""

    resolution = _resolve(_payload(LISTED), **overrides)

    assert (resolution.action, resolution.reason) == (action, reason)
    assert (resolution.target.asset_release_tag if resolution.target else None) == target
    assert resolution.download_authorized is (action is ResolutionAction.ADOPT)


def test_a_release_that_is_no_longer_listed_is_not_a_withdrawal() -> None:
    resolution = _resolve(_payload([("v8.0.63", 1, "3.2.6")]), current=V63_3, bundled=V63_1)

    assert (resolution.action, resolution.reason) == (ResolutionAction.KEEP, "up_to_date")


@pytest.mark.parametrize(
    ("revoked", "overrides", "action", "target"),
    [
        # The installed engine was withdrawn: back to the named fallback, even below the bundled engine.
        ([tombstone(V63_2, V63_1)], {"current": V63_2, "bundled": V63_2}, ResolutionAction.WITHDRAW, V63_1),
        # Routine updates switched off do not stop a withdrawal.
        (
            [tombstone(V63_2, V63_1)],
            {"current": V63_2, "allow_adoption": False},
            ResolutionAction.WITHDRAW,
            V63_1,
        ),
        # The fallback was itself withdrawn later: follow it to one that stands.
        (
            [tombstone(V63_3, V63_2), tombstone(V63_2, V63_1)],
            {"current": V63_3, "avibe_version": "3.2.7"},
            ResolutionAction.WITHDRAW,
            V63_1,
        ),
        # A fallback this Avibe cannot run is unsafe; a newer release it can run is ordinary adoption.
        (
            [tombstone(V63_1, V63_3)],
            {"current": V63_1},
            ResolutionAction.ADOPT,
            V63_2,
        ),
        # No safe fallback and nothing newer: Model Hub must stop rather than keep the withdrawn engine.
        ([tombstone(V63_2, V63_3)], {"current": V63_2}, ResolutionAction.BLOCKED, None),
        ([tombstone(V63_2, V63_1)], {"current": V63_2, "quarantined": frozenset({V63_1})}, ResolutionAction.BLOCKED, None),
        (
            [tombstone(V63_2, V63_1), tombstone(V63_1, V63_2)],
            {"current": V63_2},
            ResolutionAction.BLOCKED,
            None,
        ),
        (
            [tombstone(V63_1, V63_3)],
            {"current": V63_1, "allow_adoption": False},
            ResolutionAction.BLOCKED,
            None,
        ),
        # A withdrawn release is never an adoption target.
        ([tombstone(V63_2, V63_1)], {"current": V63_1}, ResolutionAction.KEEP, None),
    ],
)
def test_only_an_explicit_withdrawal_moves_an_engine_back_and_only_to_a_safe_fallback(
    revoked, overrides, action, target
) -> None:
    """MH-ENGINE-CHANNEL-004: a withdrawal is the only way back, and an unsafe fallback blocks."""

    resolution = _resolve(_payload(LISTED[:3], revoked), **overrides)

    assert resolution.action is action
    assert (resolution.target.asset_release_tag if resolution.target else None) == target
    if action is not ResolutionAction.KEEP:
        assert resolution.tombstone.asset_release_tag == overrides["current"]


def test_a_stale_index_still_withdraws_but_authorizes_no_download() -> None:
    payload = _payload(LISTED[:2], [tombstone(V63_2, V63_1)])

    resolution = _resolve(payload, current=V63_2, fresh=False)

    assert resolution.action is ResolutionAction.WITHDRAW
    assert resolution.target.asset_release_tag == V63_1
    assert resolution.download_authorized is False
    # With no safe fallback, a stale index cannot offer a newer release instead.
    stale = _resolve(_payload(LISTED[:3], [tombstone(V63_1, V63_3)]), current=V63_1, fresh=False)
    assert stale.action is ResolutionAction.BLOCKED


# ----- the avault verifier ---------------------------------------------------

posix_only = pytest.mark.skipif(os.name == "nt", reason="the stand-in avault is a POSIX shell script")
REQUEST = {"public_key": "02" + "11" * 32, "digest": "22" * 32, "signature": "3006020101020101"}


def _fake_avault(root: Path, *, version: str = "0.1.10", verify: str = "echo '{\"valid\":true}'; exit 0") -> Path:
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    script = root / "avault"
    script.write_text(
        f"""#!/bin/sh
STATE="{state}"
echo "$*" >> "$STATE/calls"
env | grep '^AVAULT_' >> "$STATE/environment"
case "$1" in
  version) echo "avault {version}"; exit 0 ;;
  verify) cat > "$STATE/request"; {verify} ;;
esac
exit 70
"""
    )
    script.chmod(0o755)
    return script


def _verifier(script: Path | None, **options) -> channel.AvaultVerifier:
    return channel.AvaultVerifier(lambda: str(script) if script else None, **options)


@posix_only
def test_only_exit_zero_with_a_true_boolean_verifies(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-007: verification runs avault's two keyless verbs and nothing else."""

    script = _fake_avault(tmp_path)

    _verifier(script, environ={**os.environ, "AVAULT_STORE": "file-passphrase", "AVAULT_HOME": "/nowhere"}).verify(
        **REQUEST
    )

    state = tmp_path / "state"
    # Exactly the two keyless verbs: no store option, no install, no key verb.
    assert (state / "calls").read_text().splitlines() == ["version", "verify"]
    assert (state / "environment").read_text() == ""
    assert json.loads((state / "request").read_text()) == {"scheme": "ecdsa-secp256k1-der", **REQUEST}


@posix_only
@pytest.mark.parametrize(
    ("verify", "reason"),
    [
        ("echo '{\"valid\":false}'; exit 1", "signature_invalid"),
        ("echo '{\"valid\":false}'; exit 0", "verifier_failed"),
        ("echo '{\"valid\":true}'; exit 1", "verifier_failed"),
        ("echo '{\"valid\":true}'; exit 70", "verifier_failed"),
        ("exit 0", "verifier_failed"),
        ("exit 1", "verifier_failed"),
        ("exit 70", "verifier_failed"),
        ("exit 77", "verifier_failed"),
        ("echo '{\"valid\":\"true\"}'; exit 0", "verifier_failed"),
        ("echo '{\"valid\":1}'; exit 0", "verifier_failed"),
        ("echo '{\"valid\":true,\"valid\":true}'; exit 0", "verifier_failed"),
        ("echo '{\"valid\":true,\"key\":\"x\"}'; exit 0", "verifier_failed"),
        ("echo 'true'; exit 0", "verifier_failed"),
        ("echo 'valid: true'; exit 0", "verifier_failed"),
        ("head -c 100000 /dev/zero; exit 0", "verifier_failed"),
    ],
)
def test_every_other_answer_fails_closed(tmp_path, verify, reason) -> None:
    """MH-ENGINE-CHANNEL-007: any answer but exit 0 with a true boolean is a failed verification."""

    script = _fake_avault(tmp_path, verify=verify)

    assert _reason(lambda: _verifier(script).verify(**REQUEST)) == reason


@posix_only
@pytest.mark.parametrize(
    "verify",
    [
        # Never answers, with its output open and with it closed.
        "exec sleep 30",
        "exec 1>&-; exec sleep 30",
    ],
)
def test_an_avault_that_does_not_finish_in_time_is_stopped_and_fails_closed(tmp_path, verify) -> None:
    script = _fake_avault(tmp_path, verify=verify)
    started = time.monotonic()

    assert _reason(lambda: _verifier(script, timeout=2.0).verify(**REQUEST)) == "verifier_failed"
    assert time.monotonic() - started < 15


@posix_only
@pytest.mark.parametrize(
    ("version", "reason"),
    [
        ("0.1.9", "verifier_too_old"),
        ("0.1.8", "verifier_too_old"),
        ("0.1.10-rc.1", "verifier_too_old"),
        ("unknown", "verifier_too_old"),
        ("", "verifier_too_old"),
    ],
)
def test_an_avault_without_the_verify_verb_is_never_asked_to_verify(tmp_path, version, reason) -> None:
    script = _fake_avault(tmp_path, version=version)

    assert _reason(lambda: _verifier(script).verify(**REQUEST)) == reason
    assert (tmp_path / "state" / "calls").read_text().splitlines() == ["version"]


@posix_only
@pytest.mark.parametrize("version", ["0.1.10", "0.1.11", "0.2.0", "1.0.0", "0.2.0-rc.1"])
def test_the_first_release_with_verify_and_every_later_one_is_accepted(tmp_path, version) -> None:
    _verifier(_fake_avault(tmp_path, version=version)).verify(**REQUEST)


def test_a_missing_avault_is_a_failed_verification(tmp_path) -> None:
    def broken() -> str:
        raise RuntimeError("configuration unreadable")

    assert _reason(lambda: _verifier(None).verify(**REQUEST)) == "verifier_unavailable"
    assert _reason(lambda: _verifier(tmp_path / "absent").verify(**REQUEST)) == "verifier_unavailable"
    assert _reason(lambda: channel.AvaultVerifier(broken).verify(**REQUEST)) == "verifier_unavailable"
