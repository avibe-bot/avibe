"""Capability evidence store (``model-hub-capability-model.md`` §3, jsH stale rule).

The store is the only routing truth for hosted tools and the only negative
truth for efforts, so its resolution table is pinned here at its owning boundary.
"""

from __future__ import annotations

import json
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
    resolve,
)

CONTRACTS = Path(__file__).parents[1] / "docs/plans/model-hub-contracts"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
ENGINE = "model-hub-engine-v8.0.23-3"


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
        models=[ModelHubModelConfig(id="grok-4.6", provenance="discovered")],
        credential_ref=f"cred_{source_id}",
    )
    return replace(source, **changes)


def _record(source: ModelHubSourceConfig, feature: EvidenceFeature, **changes) -> EvidenceRecord:
    fields = dict(
        source_id=source.id,
        model_id="grok-4.6",
        feature=feature,
        status="verified",
        basis="probe",
        observed_at=NOW,
        engine=ENGINE,
        shape=1,
        source_identity=identity_digest(source),
    )
    fields.update(changes)
    return EvidenceRecord(**fields)


CALL = EvidenceFeature.hosted("x_search", "call")
REPLAY = EvidenceFeature.hosted("web_search", "replay")
ULTRA = EvidenceFeature.effort("ultra")
DAYS = timedelta(days=1)

# (case, feature, record changes, current engine, current shape, age) -> (routing status, stale)
RESOLUTION = [
    ("fresh-verified-call", CALL, {}, ENGINE, 1, 0 * DAYS, "verified", None),
    ("probe-unsupported-call", CALL, {"status": "unsupported"}, ENGINE, 1, 0 * DAYS, "unsupported", None),
    ("engine-upgrade-call-is-unknown", CALL, {}, "model-hub-engine-v8.0.24-1", 1, 0 * DAYS, "unknown", "wire"),
    ("shape-change-replay-is-unknown", REPLAY, {}, ENGINE, 2, 0 * DAYS, "unknown", "wire"),
    ("old-call-still-routes", CALL, {}, ENGINE, 1, 31 * DAYS, "verified", "age"),
    ("engine-upgrade-effort-negative-still-applies", ULTRA, {"status": "unsupported"}, "model-hub-engine-v9", 1, 0 * DAYS, "unsupported", "wire"),
    ("observed-negative-within-ttl", ULTRA, {"status": "unsupported", "basis": "observed", "detail": "400 invalid_value"}, "model-hub-engine-v9", 1, 6 * DAYS, "unsupported", None),
    ("observed-negative-expires", CALL, {"status": "unsupported", "basis": "observed", "detail": "400 invalid_request_error"}, ENGINE, 1, 8 * DAYS, "unknown", None),
    ("unknown-engine-never-vouches", CALL, {"engine": "unknown-engine"}, "unknown-engine", 1, 0 * DAYS, "unknown", "wire"),
]


@pytest.mark.parametrize(
    "feature,changes,engine,shape,age,status,stale",
    [case[1:] for case in RESOLUTION],
    ids=[case[0] for case in RESOLUTION],
)
def test_evidence_resolves_for_routing(feature, changes, engine, shape, age, status, stale):
    """MH-HOSTED-003: probe and observed evidence resolve for routing, with the jsH stale rule."""
    source = _source()
    record = _record(source, feature, **changes)
    view = resolve(record, source=source, engine=engine, shape=shape, now=NOW + age)
    assert (view.status, view.stale) == (status, stale)
    # When the reading changes on its own: an observed record expires, a fresh probe record ages.
    if view.status == "unknown" and view.stale is None:
        assert view.changes_at is None
    elif record.basis == "observed":
        assert view.changes_at == record.observed_at + timedelta(days=7)
    else:
        assert view.changes_at == (record.observed_at + timedelta(days=30) if stale is None else None)


def test_a_changed_source_identity_reads_as_unknown_and_is_pruned(tmp_path):
    store = CapabilityEvidenceStore(tmp_path / "model_hub_capability_evidence.json")
    keep, rekeyed = _source("src_keep0001"), _source("src_swap0001")
    store.write(_record(keep, CALL), live=[keep, rekeyed])
    store.write(_record(rekeyed, CALL), live=[keep, rekeyed])

    replaced = replace(rekeyed, credential_ref="cred_rotated")
    assert store.view(replaced, "grok-4.6", CALL, engine=ENGINE, shape=1, now=NOW).status == "unknown"
    assert store.view(keep, "grok-4.6", CALL, engine=ENGINE, shape=1, now=NOW).status == "verified"

    store.write(_record(keep, REPLAY), live=[keep, replaced])
    assert {record.source_id for record in store.records()} == {"src_keep0001"}
    with pytest.raises(ValueError):
        store.write(_record(rekeyed, CALL), live=[keep, replaced])


def test_persisted_records_match_the_schema_and_unreadable_ones_are_unknown(tmp_path):
    path = tmp_path / "model_hub_capability_evidence.json"
    store = CapabilityEvidenceStore(path)
    source = _source()
    store.write(_record(source, CALL), live=[source])
    store.write(
        _record(source, ULTRA, status="unsupported", basis="observed", detail="400 invalid_value"), live=[source]
    )
    schema = json.loads((CONTRACTS / "capability-evidence.schema.json").read_text())
    validator = Draft7Validator(
        schema,
        registry=Registry().with_resources(
            [(schema["$id"], Resource.from_contents(schema))]
        ),
    )
    written = json.loads(path.read_text())
    for payload in written:
        validator.validate(payload)

    written.append({**written[0], "status": "maybe"})
    written.append({**written[0], "feature": {"kind": "hosted_tool", "name": "image_generation", "aspect": "replay"}})
    path.write_text(json.dumps(written))
    assert len(CapabilityEvidenceStore(path).records()) == 2
    assert not validator.is_valid(written[2]) and not validator.is_valid(written[3])


@pytest.mark.parametrize(
    "changes",
    [
        {"basis": "observed"},
        {"basis": "observed", "status": "verified", "detail": "400 x"},
        {"detail": "x" * 65},
        {"observed_at": datetime(2026, 10, 13)},
        {"shape": -1},
        {"source_identity": ""},
    ],
    ids=["observed-without-detail", "observed-positive", "long-detail", "naive-time", "negative-shape", "no-identity"],
)
def test_a_record_refuses_what_the_contract_forbids(changes):
    with pytest.raises(ValueError):
        _record(_source(), CALL, **changes)


def test_image_generation_has_no_replay_evidence():
    with pytest.raises(ValueError):
        EvidenceFeature.hosted("image_generation", "replay")


def test_lookups_after_construction_never_touch_the_disk(tmp_path, monkeypatch):
    """MH-HOSTED-003: the store reads its file when built at startup; no lookup or write reads it again."""
    path = tmp_path / "model_hub_capability_evidence.json"
    source = _source()
    CapabilityEvidenceStore(path).write(_record(source, CALL), live=[source])
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
        assert store.view(source, "grok-4.6", CALL, engine=ENGINE, shape=1, now=NOW).status == "verified"
    store.write(_record(source, REPLAY), live=[source])
    assert store.view(source, "grok-4.6", REPLAY, engine=ENGINE, shape=1, now=NOW).status == "verified"
    assert touched == []


def test_a_read_never_waits_for_a_write_on_a_slow_disk(tmp_path, monkeypatch):
    """MH-HOSTED-003: the request path reads a published snapshot, so a write stuck on the disk cannot stall it."""
    import threading

    from core.handlers.model_hub import capability_evidence

    path = tmp_path / "model_hub_capability_evidence.json"
    source = _source()
    store = CapabilityEvidenceStore(path)
    store.write(_record(source, CALL), live=[source])

    on_disk = threading.Event()
    release = threading.Event()
    persist = capability_evidence.write_state_document

    def slow_disk(target, payload):
        on_disk.set()
        assert release.wait(5)
        persist(target, payload)

    monkeypatch.setattr(capability_evidence, "write_state_document", slow_disk)
    writer = threading.Thread(target=store.write, args=(_record(source, REPLAY),), kwargs={"live": [source]})
    writer.start()
    try:
        assert on_disk.wait(5)
        read = []
        reader = threading.Thread(
            target=lambda: read.append(store.view(source, "grok-4.6", CALL, engine=ENGINE, shape=1, now=NOW).status)
        )
        reader.start()
        reader.join(1)
        assert read == ["verified"]
    finally:
        release.set()
        writer.join(5)
    assert store.view(source, "grok-4.6", REPLAY, engine=ENGINE, shape=1, now=NOW).status == "verified"
