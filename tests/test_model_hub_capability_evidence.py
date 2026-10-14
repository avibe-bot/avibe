"""Capability evidence store (``model-hub-capability-model.md`` §3; ``vibey-hosted-tools.md`` §3.2).

Evidence is passive: a refusal is routing state with no time limit, a success is
display history. These tests pin how each is kept, read and cleared at the
store, the owning boundary.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft7Validator
from referencing import Registry, Resource

from config.v2_config import ModelHubModelConfig, ModelHubSourceConfig, ModelHubSourceStateConfig
from core.handlers.model_hub.capability_evidence import (
    CapabilityEvidenceStore,
    EvidenceFeature,
    EvidenceRecord,
    identity_digest,
    wire_stamp,
)

CONTRACTS = Path(__file__).parents[1] / "docs/plans/model-hub-contracts"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
ENGINE = "model-hub-engine-v8.0.23-3"
AVIBE = "3.2.1"


def _source(source_id: str = "src_xai00001", **changes) -> ModelHubSourceConfig:
    source = ModelHubSourceConfig(
        id=source_id,
        kind="api_key",
        vendor="xai",
        display_name="xAI 密钥",
        protocol="openai_responses",
        supply_channel="hub",
        billing="metered",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[ModelHubModelConfig(id="grok-4.6", provenance="discovered", reasoning_efforts=["low", "high"])],
        credential_ref=f"cred_{source_id}",
    )
    return replace(source, **changes)


def _wire(source: ModelHubSourceConfig, *, engine: str = ENGINE, avibe: str = AVIBE) -> str:
    return wire_stamp(engine=engine, avibe=avibe, source=source, model_id="grok-4.6")


def _refusal(source: ModelHubSourceConfig, feature: EvidenceFeature, **changes) -> EvidenceRecord:
    fields = dict(
        source_id=source.id,
        model_id="grok-4.6",
        feature=feature,
        status="unsupported",
        observed_at=NOW,
        wire=_wire(source),
        source_identity=identity_digest(source),
        detail="400 invalid_request_error",
    )
    fields.update(changes)
    return EvidenceRecord(**fields)


def _success(source: ModelHubSourceConfig, feature: EvidenceFeature, **changes) -> EvidenceRecord:
    fields = dict(
        source_id=source.id,
        model_id="grok-4.6",
        feature=feature,
        status="verified",
        observed_at=NOW,
        wire=_wire(source),
        source_identity=identity_digest(source),
        mode="native",
    )
    fields.update(changes)
    return EvidenceRecord(**fields)


CALL = EvidenceFeature.hosted("x_search", "call")
REPLAY = EvidenceFeature.hosted("web_search", "replay")
ULTRA = EvidenceFeature.effort("ultra")


def _store(tmp_path) -> CapabilityEvidenceStore:
    return CapabilityEvidenceStore(tmp_path / "model_hub_capability_evidence.json")


@pytest.mark.parametrize(
    "case,current,applies",
    [
        ("same-wire-a-year-later", lambda source: (source, _wire(source)), True),
        ("engine-upgrade", lambda source: (source, _wire(source, engine="model-hub-engine-v8.0.28-1")), False),
        ("avibe-release", lambda source: (source, _wire(source, avibe="3.2.2")), False),
        (
            "model-tiers-edited",
            lambda source: (
                (edited := replace(source, models=[replace(source.models[0], reasoning_efforts=["low", "high", "max"])])),
                _wire(edited),
            ),
            False,
        ),
        (
            "credential-rotated",
            lambda source: ((rotated := replace(source, credential_ref="cred_rotated")), _wire(rotated)),
            False,
        ),
        (
            "base-url-edited",
            lambda source: ((moved := replace(source, base_url="https://relay.example/v1")), _wire(moved)),
            False,
        ),
    ],
)
def test_a_refusal_holds_until_the_wire_or_the_source_changes(tmp_path, case, current, applies):
    """MH-HOSTED-003: a refusal has no time limit; it stops applying when the Source's config, the engine release, the
    Avibe release or the model's rendered tiers change (owner rule: a Source config change resets it)."""
    source = _source()
    store = _store(tmp_path)
    for feature in (CALL, ULTRA):
        store.write_refusal(_refusal(source, feature), live=[source])
    now_source, wire = current(source)
    for feature in (CALL, ULTRA):
        assert (store.refusal(now_source, "grok-4.6", feature, wire=wire) is not None) is applies


def test_a_success_never_replaces_a_refusal_and_never_routes(tmp_path):
    """MH-HOSTED-003: a success that settles after a concurrent refusal must not erase it; both are kept."""
    source = _source()
    store = _store(tmp_path)
    store.write_refusal(_refusal(source, CALL), live=[source])
    store.note_success(_success(source, CALL, observed_at=NOW + timedelta(seconds=1)), live=[source], now=NOW)
    assert store.refusal(source, "grok-4.6", CALL, wire=_wire(source)) is not None
    assert {record.status for record in store.records()} == {"verified", "unsupported"}


def test_successes_update_the_latest_at_once_and_the_file_at_most_hourly(tmp_path, monkeypatch):
    """MH-HOSTED-003: the status line is current while file writes are throttled, and it survives a restart."""
    from core.handlers.model_hub import capability_evidence

    path = tmp_path / "model_hub_capability_evidence.json"
    source, other = _source(), _source("src_xai00002")
    store = CapabilityEvidenceStore(path)
    writes = []
    persist = capability_evidence.write_state_document
    monkeypatch.setattr(capability_evidence, "write_state_document", lambda *args: (writes.append(1), persist(*args)))

    store.note_success(_success(source, CALL), live=[source, other], now=NOW)
    store.note_success(_success(other, CALL, observed_at=NOW + timedelta(minutes=5), mode="executor"), live=[source, other], now=NOW + timedelta(minutes=5))
    store.note_success(_success(source, CALL, observed_at=NOW + timedelta(minutes=10)), live=[source, other], now=NOW + timedelta(minutes=10))
    assert len(writes) == 2
    assert store.latest_success("x_search").source_id == source.id
    assert store.latest_success("x_search").at == NOW + timedelta(minutes=10)

    store.note_success(_success(source, CALL, observed_at=NOW + timedelta(hours=2)), live=[source, other], now=NOW + timedelta(hours=2))
    assert len(writes) == 3
    reloaded = CapabilityEvidenceStore(path)
    assert (reloaded.latest_success("x_search").source_id, reloaded.latest_success("x_search").at) == (
        source.id, NOW + timedelta(hours=2),
    )


def test_a_refusal_holds_in_memory_when_its_write_fails(tmp_path, monkeypatch, caplog):
    """MH-HOSTED-003: a refusal is routing state; the re-render that follows must not declare the feature again."""
    from core.handlers.model_hub import capability_evidence

    source = _source()
    store = _store(tmp_path)

    def full_disk(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(capability_evidence, "write_state_document", full_disk)
    with caplog.at_level(logging.WARNING):
        store.write_refusal(_refusal(source, CALL), live=[source])
    assert store.refusal(source, "grok-4.6", CALL, wire=_wire(source)) is not None
    assert "holds until restart" in caplog.text


def test_a_clear_is_persisted_before_it_takes_effect(tmp_path, monkeypatch):
    """MH-HOSTED-003: a clear that cannot be written changes nothing, so it never comes back after a restart; one that
    can removes only the refusals it names, never a success."""
    from core.handlers.model_hub import capability_evidence

    path = tmp_path / "model_hub_capability_evidence.json"
    source = _source()
    store = CapabilityEvidenceStore(path)
    store.write_refusal(_refusal(source, CALL), live=[source])
    store.write_refusal(_refusal(source, ULTRA), live=[source])
    store.note_success(_success(source, CALL), live=[source], now=NOW)
    persist = capability_evidence.write_state_document

    def full_disk(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(capability_evidence, "write_state_document", full_disk)
    with pytest.raises(OSError):
        store.clear(lambda record: record.feature == CALL, live=[source])
    assert store.refusal(source, "grok-4.6", CALL, wire=_wire(source)) is not None

    monkeypatch.setattr(capability_evidence, "write_state_document", persist)
    assert store.clear(lambda record: record.feature == CALL, live=[source]) == 1
    assert store.clear(lambda record: record.feature == CALL, live=[source]) == 0
    reloaded = CapabilityEvidenceStore(path)
    assert reloaded.refusal(source, "grok-4.6", CALL, wire=_wire(source)) is None
    assert reloaded.refusal(source, "grok-4.6", ULTRA, wire=_wire(source)) is not None
    assert reloaded.get(source.id, "grok-4.6", CALL, "verified") is not None


@pytest.mark.parametrize(
    "marker,digest",
    [
        (None, "sha256:b682a96ef48e2f2834ed22ba2d42cdae568e5768f3fc3a1334a24728ffdb7166"),
        ("vp_" + "7" * 32, "sha256:d0b054e8827133cbeb6b4427af5ea696e2d29ab8f08de21d079336155bed8333"),
    ],
    ids=["verified", "pending"],
)
def test_a_released_sources_identity_digest_is_unchanged(marker, digest):
    """MH-HOSTED-003: evidence follows the Source a released build saved. The digests are the ones released builds
    computed, whose identity held the verification marker; a pending Source's marker seeds its generation."""
    released = replace(_source(), models=[ModelHubModelConfig(id="grok-4.6", provenance="discovered")]).to_payload()
    if marker is not None:
        released["verification_pending"] = marker
    assert "credential_generation" not in released
    assert identity_digest(ModelHubSourceConfig.from_payload(released)) == digest


def test_a_changed_source_identity_is_pruned_at_the_next_write(tmp_path):
    store = _store(tmp_path)
    keep, rekeyed = _source("src_keep0001"), _source("src_swap0001")
    store.write_refusal(_refusal(keep, CALL), live=[keep, rekeyed])
    store.write_refusal(_refusal(rekeyed, CALL), live=[keep, rekeyed])

    replaced = replace(rekeyed, credential_ref="cred_rotated")
    store.write_refusal(_refusal(keep, REPLAY), live=[keep, replaced])
    assert {record.source_id for record in store.records()} == {"src_keep0001"}
    with pytest.raises(ValueError):
        store.write_refusal(_refusal(rekeyed, CALL), live=[keep, replaced])


def test_persisted_records_match_the_schema_and_rc_era_ones_are_pruned(tmp_path):
    """MH-HOSTED-003: an rc build's probe records (version 1) do not load; nothing migrates them."""
    path = tmp_path / "model_hub_capability_evidence.json"
    store = CapabilityEvidenceStore(path)
    source = _source()
    store.write_refusal(_refusal(source, ULTRA), live=[source])
    store.note_success(_success(source, CALL), live=[source], now=NOW)
    schema = json.loads((CONTRACTS / "capability-evidence.schema.json").read_text())
    validator = Draft7Validator(schema, registry=Registry().with_resources([(schema["$id"], Resource.from_contents(schema))]))
    written = json.loads(path.read_text())
    assert len(written) == 2
    for payload in written:
        validator.validate(payload)

    rc_era = {
        "version": 1, "source_id": source.id, "model_id": "grok-4.6",
        "feature": {"kind": "hosted_tool", "name": "x_search", "aspect": "call"}, "status": "verified",
        "basis": "probe", "observed_at": "2026-10-14T00:00:00Z", "engine": ENGINE, "shape": 1,
        "source_identity": identity_digest(source),
    }
    path.write_text(json.dumps([*written, rc_era, {**written[0], "status": "maybe"}]))
    assert len(CapabilityEvidenceStore(path).records()) == 2
    assert not validator.is_valid(rc_era)


@pytest.mark.parametrize(
    "build",
    [
        lambda: _refusal(_source(), CALL, detail=None),
        lambda: _refusal(_source(), CALL, mode="native"),
        lambda: _success(_source(), CALL, mode=None),
        lambda: _success(_source(), CALL, detail="400 x"),
        lambda: _refusal(_source(), CALL, detail="x" * 65),
        lambda: _refusal(_source(), CALL, observed_at=datetime(2026, 10, 15)),
        lambda: _refusal(_source(), CALL, source_identity=""),
    ],
    ids=["refusal-without-detail", "refusal-with-mode", "success-without-mode", "success-with-detail", "long-detail",
         "naive-time", "no-identity"],
)
def test_a_record_refuses_what_the_contract_forbids(build):
    with pytest.raises(ValueError):
        build()


def test_image_generate_has_no_replay_evidence():
    with pytest.raises(ValueError):
        EvidenceFeature.hosted("image_generate", "replay")
    with pytest.raises(ValueError):
        EvidenceFeature.hosted("image_generation", "call")


def test_lookups_after_construction_never_touch_the_disk(tmp_path, monkeypatch):
    """MH-HOSTED-003: the store reads its file when built at startup; no lookup or write reads it again."""
    path = tmp_path / "model_hub_capability_evidence.json"
    source = _source()
    CapabilityEvidenceStore(path).write_refusal(_refusal(source, CALL), live=[source])
    store = CapabilityEvidenceStore(path)
    assert len(store.records()) == 1

    touched = []
    original_stat, original_read = Path.stat, Path.read_text

    def stat(self, *args, **kwargs):
        if self == path:
            touched.append("stat")
        return original_stat(self, *args, **kwargs)

    def read_text(self, *args, **kwargs):
        if self == path:
            touched.append("read")
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "read_text", read_text)
    for _ in range(50):
        assert store.refusal(source, "grok-4.6", CALL, wire=_wire(source)) is not None
    store.write_refusal(_refusal(source, REPLAY), live=[source])
    assert store.refusal(source, "grok-4.6", REPLAY, wire=_wire(source)) is not None
    assert touched == []


def test_a_read_never_waits_for_a_write_on_a_slow_disk(tmp_path, monkeypatch):
    """MH-HOSTED-003: the request path reads a published snapshot, so a write stuck on the disk cannot stall it."""
    import threading

    from core.handlers.model_hub import capability_evidence

    path = tmp_path / "model_hub_capability_evidence.json"
    source = _source()
    store = CapabilityEvidenceStore(path)
    store.write_refusal(_refusal(source, CALL), live=[source])

    on_disk = threading.Event()
    release = threading.Event()
    persist = capability_evidence.write_state_document

    def slow_disk(target, payload):
        on_disk.set()
        assert release.wait(5)
        persist(target, payload)

    monkeypatch.setattr(capability_evidence, "write_state_document", slow_disk)
    writer = threading.Thread(target=store.write_refusal, args=(_refusal(source, REPLAY),), kwargs={"live": [source]})
    writer.start()
    try:
        assert on_disk.wait(5)
        read = []
        reader = threading.Thread(
            target=lambda: read.append(store.refusal(source, "grok-4.6", CALL, wire=_wire(source)) is not None)
        )
        reader.start()
        reader.join(1)
        assert read == [True]
    finally:
        release.set()
        writer.join(5)
    assert store.refusal(source, "grok-4.6", REPLAY, wire=_wire(source)) is not None
