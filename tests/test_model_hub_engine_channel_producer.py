"""The one writer of the signed engine channel index (scripts/model_hub_engine_channel.py).

What this protects: desired state lives in the signer's ledger and nowhere
else, so an old, missing or replaced published index cannot make the signer
sign older state; one sequence is only ever spent on one payload, whatever
fails; nothing is signed or written while the channel is uncommissioned or
the run is not the approved, reviewed workflow; and a writer cannot replace a
newer index or drop a withdrawal.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from core import model_hub_engine_channel as channel
from scripts import model_hub_engine_channel as producer
from scripts.model_hub_engine_channel import ProducerError, Promote, Renew, Revoke
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
    tombstone,
)

V1, V2, V3 = (f"model-hub-engine-v8.0.63-{build}" for build in (1, 2, 3))
WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/model-hub-engine-channel.yml"
LATER = NOW + timedelta(hours=1)
needs_directory_flush = pytest.mark.skipif(
    not hasattr(os, "O_DIRECTORY"), reason="the ledger refuses a platform that cannot flush a directory"
)


def _current(sequence: int = 10, *, releases=None, revoked=None) -> channel.ChannelPayload:
    entries = releases if releases is not None else [release_entry(manifest_bytes(build=1)), release_entry(manifest_bytes(build=2))]
    return channel.parse_payload(encode(payload_document(sequence, releases=entries, revoked=revoked)))


def _plan(current, operation, *, now=LATER, sequence=None) -> channel.ChannelPayload:
    sequence = sequence if sequence is not None else (current.sequence + 1 if current else 1)
    return channel.parse_payload(producer.plan_payload(current, operation, now=now, sequence=sequence))


# ----- planning --------------------------------------------------------------


def test_the_first_allocation_promotes_one_release_at_the_sequence_it_is_given() -> None:
    planned = _plan(None, Promote(manifest_bytes(build=1), "3.2.6"), sequence=40)

    assert planned.sequence == 40
    assert [item.asset_release_tag for item in planned.releases] == [V1] and planned.revoked == ()
    # The index carries the reviewed manifest's exact bytes, not a reserialization.
    assert planned.releases[0].manifest_bytes == manifest_bytes(build=1)
    assert planned.expires_at - planned.issued_at == channel.MAX_VALIDITY


def test_every_change_copies_allocated_releases_and_withdrawals_byte_for_byte() -> None:
    current = _current(revoked=[tombstone(V2, V1)])

    for operation in (Renew(), Promote(manifest_bytes(build=3), "3.2.7"), Revoke(V3, V1, "bundled engine crashes")):
        planned = _plan(current, operation, sequence=25)

        # Sequences only need to increase; a gap is fine.
        assert planned.sequence == 25
        assert [item.manifest_bytes for item in planned.releases][:2] == [item.manifest_bytes for item in current.releases]
        assert planned.revoked[:1] == current.revoked
        channel.check_successor(producer._identities(current), current.revoked, planned)

    renewed = _plan(current, Renew())
    assert [(item.asset_release_tag, item.min_avibe_version) for item in renewed.releases] == [(V1, "3.2.6"), (V2, "3.2.6")]
    assert renewed.revoked == current.revoked
    assert renewed.issued_at == LATER


@pytest.mark.parametrize(
    ("current", "operation", "message"),
    [
        (None, Renew(), "no allocated index"),
        (None, Revoke(V2, V1, "x"), "no allocated index"),
        (_current(), Promote(manifest_bytes(build=2), "3.2.6"), "already promoted"),
        (_current(), Promote(manifest_bytes(build=2, salt="x"), "3.2.6"), "already promoted"),
        (_current(revoked=[tombstone(V3, V1)]), Promote(manifest_bytes(build=3), "3.2.6"), "withdrawn"),
        (_current(), Promote(manifest_bytes(build=3), "3.2.5"), "minimum cannot be lower"),
        (_current(), Promote(manifest_bytes(build=3), "3.2.6rc1"), "stable X.Y.Z"),
        (_current(), Promote(b"{}", "3.2.6"), "cannot be promoted"),
        (_current(revoked=[tombstone(V2, V1)]), Revoke(V2, V1, "again"), "already withdrawn"),
        (_current(), Revoke(V2, V3, "x"), "listed release"),
        (_current(revoked=[tombstone(V2, V1)]), Revoke(V1, V2, "x"), "listed release"),
        (_current(), Revoke(V2, V2, "x"), "itself"),
        (_current(), Revoke("v8.0.63", V1, "x"), "not an engine release tag"),
        (_current(), Revoke(V2, V1, "line one\nline two"), "not one a client would accept"),
        (
            _current(releases=[release_entry(manifest_bytes(build=1), "3.3.0"), release_entry(manifest_bytes(build=2))]),
            Revoke(V2, V1, "x"),
            "newer Avibe",
        ),
    ],
)
def test_a_change_that_would_rewrite_or_break_the_index_is_not_planned(current, operation, message) -> None:
    with pytest.raises(ProducerError, match=message):
        producer.plan_payload(current, operation, now=LATER, sequence=11)


def test_a_bundled_engine_that_was_never_listed_can_still_be_withdrawn() -> None:
    planned = _plan(_current(), Revoke(V3, V2, "bundled engine crashes on start"))

    assert planned.tombstone(V3).rollback_to == V2


def test_a_clock_behind_or_a_sequence_that_does_not_increase_plans_nothing() -> None:
    with pytest.raises(ProducerError, match="clock is behind"):
        producer.plan_payload(_current(), Renew(), now=NOW - timedelta(minutes=1), sequence=11)
    for sequence in (10, 9):
        with pytest.raises(ProducerError, match="higher sequence"):
            producer.plan_payload(_current(), Renew(), now=LATER, sequence=sequence)


# ----- signing ---------------------------------------------------------------


def _sign(signer: Signer, payload: bytes, *, with_key: Signer | None = None, **overrides):
    requests = []

    def sign(request: bytes) -> str:
        requests.append(json.loads(request))
        return (with_key or signer).sign(requests[-1]["digest"])

    arguments = {
        "key_id": KEY_ID,
        "key_name": "MODEL_HUB_ENGINE_CHANNEL_KEY",
        "key_envelope": {"ciphertext": "c2VhbGVk", "nonce": "bm9uY2U=", "wrap_meta": "{}"},
        "roots": signer.roots(),
        "verifier": LocalVerifier(),
        "sign": sign,
    }
    return producer.sign_payload(payload, **{**arguments, **overrides}), requests


def test_signing_asks_avault_for_the_fixed_scheme_over_the_domain_digest_and_yields_a_client_valid_envelope() -> None:
    signer = Signer()
    payload = producer.plan_payload(_current(), Renew(), now=LATER, sequence=11)

    envelope, requests = _sign(signer, payload)

    assert requests == [
        {
            "name": "MODEL_HUB_ENGINE_CHANNEL_KEY",
            "key_envelope": {"ciphertext": "c2VhbGVk", "nonce": "bm9uY2U=", "wrap_meta": "{}"},
            "digest": channel.channel_digest(payload),
            "scheme": "ecdsa-secp256k1-der",
        }
    ]
    parsed, accepted = channel.authenticate(envelope, roots=signer.roots(), verifier=LocalVerifier())
    assert parsed.payload == payload and accepted.sequence == 11


def test_a_signer_holding_any_other_key_produces_nothing_publishable() -> None:
    signer = Signer()
    payload = producer.plan_payload(_current(), Renew(), now=LATER, sequence=11)

    with pytest.raises(ProducerError, match="does not verify against the shipped root"):
        _sign(signer, payload, with_key=Signer())
    with pytest.raises(ProducerError, match="not a key Avibe ships"):
        _sign(signer, payload, key_id="some-other-key")
    with pytest.raises(ProducerError, match="no client accepts"):
        _sign(signer, b'{"sequence": 1}')


@pytest.mark.skipif(os.name == "nt", reason="the stand-in avault is a POSIX shell script")
@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        ("cat > /dev/null; echo '{\"signature\":\"3006020101020101\",\"recovery_id\":null}'; exit 0", "3006020101020101"),
        ("cat > /dev/null; echo '{\"signature\":\"3006020101020ABC\",\"recovery_id\":null}'; exit 0", "3006020101020abc"),
        ("cat > /dev/null; echo 'avault: key not found' >&2; exit 70", "exit status 70"),
        ("cat > /dev/null; exit 77", "exit status 77"),
        ("cat > /dev/null; echo '{}'; exit 0", "returned no signature"),
        ("cat > /dev/null; head -c 100000 /dev/zero; exit 0", "did not complete"),
    ],
)
def test_avault_sign_is_bounded_and_only_a_signature_counts(tmp_path, body, outcome) -> None:
    script = tmp_path / "avault"
    script.write_text(f"#!/bin/sh\n[ \"$1\" = sign ] || exit 64\n{body}\n")
    script.chmod(0o755)

    try:
        result = producer._avault_sign(str(script), b"{}")
    except ProducerError as exc:
        result = str(exc)

    assert outcome in result


# ----- uncommissioned --------------------------------------------------------


def _recording_avault(tmp_path) -> Path:
    script = tmp_path / "avault"
    script.write_text(f"#!/bin/sh\necho \"$*\" >> {tmp_path / 'calls'}\nexit 70\n")
    script.chmod(0o755)
    return script


@pytest.mark.parametrize(
    "arguments",
    [
        ["authorize", "--environment", producer.SIGNER_ENVIRONMENT, "--operation", "renew"],
        ["authorize", "--environment", producer.WRITER_ENVIRONMENT, "--operation", "promote", "--require-approval"],
        ["initialize-ledger", "--ledger", "{ledger}", "--key-id", KEY_ID, "--first-sequence", "1"],
        ["sign", "--operation", "renew", "--ledger", "{ledger}", "--key-name", "K", "--key-envelope", "{envelope}",
         "--output", "{output}", "--avault", "{avault}"],
        ["sign", "--operation", "resume", "--ledger", "{ledger}", "--key-name", "K", "--key-envelope", "{envelope}",
         "--output", "{output}", "--avault", "{avault}"],
        ["publish", "--envelope", "{envelope}", "--bucket", "avibe", "--endpoint-url", "https://example.invalid",
         "--avault", "{avault}"],
    ],
)
def test_while_avibe_ships_no_key_every_producer_step_stops_before_it_does_anything(
    tmp_path, monkeypatch, capsys, arguments
) -> None:
    """MH-ENGINE-CHANNEL-009: an uncommissioned channel signs and publishes nothing."""

    avault = _recording_avault(tmp_path)
    (tmp_path / "envelope.json").write_text("{}")
    values = {
        "avault": str(avault),
        "envelope": str(tmp_path / "envelope.json"),
        "output": str(tmp_path / "out"),
        "ledger": str(tmp_path / "ledger"),
    }

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an uncommissioned channel must not reach the network, the bucket, or avault")

    for name in ("github_api", "fetch_public_index", "_run_command", "_avault_sign"):
        monkeypatch.setattr(producer, name, forbidden)

    status = producer.main([item.format(**values) for item in arguments])

    assert status == 1
    assert "uncommissioned" in json.loads(capsys.readouterr().err)["error"]
    assert not (tmp_path / "calls").exists() and not (tmp_path / "out").exists()
    assert not (tmp_path / "ledger").exists()


def _commissioned(tmp_path, monkeypatch, signer: Signer) -> None:
    path = tmp_path / "roots.json"
    path.write_text(json.dumps({"schema_version": 1, "sequence_floor": 0,
                                "roots": [{"key_id": KEY_ID, "public_key": signer.public_key}]}))
    monkeypatch.setattr(producer, "TRUST_ROOTS_PATH", path)


def test_an_uncommissioned_channel_lists_no_release_for_the_guard_without_any_request(
    tmp_path, monkeypatch, capsys
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("an uncommissioned channel has no index to read")

    monkeypatch.setattr(producer, "fetch_public_index", forbidden)

    assert producer.main(["commissioned"]) == 0
    assert capsys.readouterr().out.strip() == "false"
    assert producer.main(["listed", "--output-dir", str(tmp_path / "listed")]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "releases": []}
    assert list((tmp_path / "listed").iterdir()) == []


def test_the_guard_is_given_every_listed_release_withdrawn_ones_and_rollback_targets_included(
    tmp_path, monkeypatch, capsys
) -> None:
    """MH-ENGINE-CHANNEL-011: withdrawn releases and rollback targets stay under the guard."""

    signer = Signer()
    _commissioned(tmp_path, monkeypatch, signer)
    monkeypatch.setattr(producer, "avault_verifier", lambda _path: LocalVerifier())
    index = envelope_bytes(signer, _current(revoked=[tombstone(V2, V1)]).raw)
    monkeypatch.setattr(producer, "fetch_public_index", lambda: index)

    assert producer.main(["commissioned"]) == 0
    assert capsys.readouterr().out.strip() == "true"
    assert producer.main(["listed", "--avault", "avault", "--output-dir", str(tmp_path / "listed")]) == 0

    assert json.loads(capsys.readouterr().out)["releases"] == [V1, V2]
    assert {path.name: path.read_bytes() for path in (tmp_path / "listed").iterdir()} == {
        f"{V1}.json": manifest_bytes(build=1),
        f"{V2}.json": manifest_bytes(build=2),
    }


@pytest.mark.parametrize(
    ("index", "arguments", "status"),
    [
        # Commissioned but never published: nothing is listed yet.
        (None, [], 0),
        # An index that does not verify is a failure to report, not an empty list.
        (b"not an index", ["--avault", "avault"], 1),
        ("other-key", ["--avault", "avault"], 1),
        ("signed", [], 1),
    ],
)
def test_the_guard_list_is_read_only_through_the_shipped_keys(tmp_path, monkeypatch, capsys, index, arguments, status) -> None:
    signer = Signer()
    _commissioned(tmp_path, monkeypatch, signer)
    monkeypatch.setattr(producer, "avault_verifier", lambda _path: LocalVerifier())
    raw = {"other-key": envelope_bytes(Signer(), _current().raw), "signed": envelope_bytes(signer, _current().raw)}.get(
        index, index
    )
    monkeypatch.setattr(producer, "fetch_public_index", lambda: raw)

    assert producer.main(["listed", *arguments, "--output-dir", str(tmp_path / "listed")]) == status
    assert list((tmp_path / "listed").iterdir()) == []
    capsys.readouterr()


# ----- authorization ---------------------------------------------------------

RUN = {
    "GITHUB_REPOSITORY": "avibe-bot/avibe",
    "GITHUB_REF": "refs/heads/master",
    "GITHUB_WORKFLOW_REF": "avibe-bot/avibe/.github/workflows/model-hub-engine-channel.yml@refs/heads/master",
    "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_RUN_ID": "4242",
}


@pytest.mark.parametrize(
    "change",
    [
        {"GITHUB_REF": "refs/heads/feature/x"},
        {"GITHUB_REF": "refs/tags/master"},
        {"GITHUB_REF": "refs/pull/7/merge"},
        {"GITHUB_REPOSITORY": "someone/avibe"},
        {"GITHUB_WORKFLOW_REF": "avibe-bot/avibe/.github/workflows/model-hub-engine-channel.yml@refs/heads/feature/x"},
        {"GITHUB_WORKFLOW_REF": "avibe-bot/avibe/.github/workflows/other.yml@refs/heads/master"},
        {"GITHUB_EVENT_NAME": "pull_request"},
        {"GITHUB_EVENT_NAME": "pull_request_target"},
        {"GITHUB_EVENT_NAME": "push"},
        {"GITHUB_EVENT_NAME": "schedule"},
        {"GITHUB_RUN_ID": ""},
    ],
)
def test_only_the_reviewed_workflow_dispatched_on_the_default_branch_is_a_valid_run(change) -> None:
    assert producer.run_context_problems(RUN) == []
    assert producer.run_context_problems({**RUN, **change}) != []


def _environment(*, reviewers=(("User", "cyhhao"),), policy=True) -> dict:
    return {
        "name": producer.SIGNER_ENVIRONMENT,
        "protection_rules": [
            {"type": "branch_policy"},
            {
                "type": "required_reviewers",
                "reviewers": [{"type": kind, "reviewer": {"login": login, "slug": login}} for kind, login in reviewers],
            },
        ],
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True} if policy else None,
    }


POLICIES = {"total_count": 1, "branch_policies": [{"name": "master", "type": "branch"}]}


@pytest.mark.parametrize(
    ("environment", "policies"),
    [
        # A name nobody configured: GitHub would create it, unprotected, when the job starts.
        (None, None),
        ({"message": "Not Found"}, POLICIES),
        (_environment(reviewers=()), POLICIES),
        (_environment(reviewers=(("Team", "release-team"),)), POLICIES),
        ({**_environment(), "protection_rules": [{"type": "wait_timer", "wait_timer": 30}]}, POLICIES),
        (_environment(policy=False), POLICIES),
        ({**_environment(), "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False}}, POLICIES),
        (_environment(), None),
        (_environment(), {"branch_policies": []}),
        (_environment(), {"branch_policies": [{"name": "master", "type": "branch"}, {"name": "release/*", "type": "branch"}]}),
        (_environment(), {"branch_policies": [{"name": "feature/*", "type": "branch"}]}),
        (_environment(), {"branch_policies": [{"name": "master", "type": "tag"}]}),
    ],
)
def test_an_environment_protects_a_job_only_when_it_exists_requires_a_named_reviewer_and_admits_only_master(
    environment, policies
) -> None:
    assert producer.environment_protection_problems(_environment(), POLICIES) == ([], frozenset({"cyhhao"}))

    problems, _reviewers = producer.environment_protection_problems(environment, policies)

    assert problems != []


def _approval(state="approved", login="cyhhao", environment=producer.SIGNER_ENVIRONMENT) -> dict:
    return {"state": state, "user": {"login": login}, "environments": [{"name": environment}], "comment": ""}


@pytest.mark.parametrize(
    "approvals",
    [
        None,
        {"message": "Not Found"},
        [],
        [_approval(state="rejected")],
        [_approval(login="someone-else")],
        [_approval(environment=producer.WRITER_ENVIRONMENT)],
        [{"state": "approved"}],
    ],
)
def test_a_privileged_step_needs_this_runs_approval_by_a_required_reviewer(approvals) -> None:
    arguments = {"environment": producer.SIGNER_ENVIRONMENT, "reviewers": frozenset({"cyhhao"})}
    assert producer.approval_problems([_approval(state="rejected"), _approval()], **arguments) == []

    assert producer.approval_problems(approvals, **arguments) != []


def test_authorization_reads_protection_and_approval_from_github_for_this_run(tmp_path, monkeypatch) -> None:
    """MH-ENGINE-CHANNEL-009: a privileged step needs the reviewed run, a protected environment, and its approval."""

    _commissioned(tmp_path, monkeypatch, Signer())
    base = f"/repos/avibe-bot/avibe/environments/{producer.SIGNER_ENVIRONMENT}"
    answers = {
        base: _environment(),
        f"{base}/deployment-branch-policies": POLICIES,
        "/repos/avibe-bot/avibe/actions/runs/4242/approvals": [_approval()],
    }
    asked = []

    def api(path: str):
        asked.append(path)
        return answers.get(path)

    producer.authorize(producer.SIGNER_ENVIRONMENT, operation="renew", environ=RUN, api=api, require_approval=False)
    assert asked == [base, f"{base}/deployment-branch-policies"]
    for operation in producer.OPERATION_POLICY:
        producer.authorize(producer.SIGNER_ENVIRONMENT, operation=operation, environ=RUN, api=api, require_approval=True)

    for broken in (
        {**answers, base: None},
        {**answers, "/repos/avibe-bot/avibe/actions/runs/4242/approvals": []},
        {**answers, "/repos/avibe-bot/avibe/actions/runs/4242/approvals": None},
    ):
        with pytest.raises(ProducerError, match="is not authorized"):
            producer.authorize(
                producer.SIGNER_ENVIRONMENT, operation="revoke", environ=RUN, api=broken.get, require_approval=True
            )
    with pytest.raises(ProducerError, match="refs/heads/master"):
        producer.authorize(
            producer.SIGNER_ENVIRONMENT, operation="renew", environ={**RUN, "GITHUB_REF": "refs/heads/x"}, api=api,
            require_approval=True,
        )


def test_every_operation_is_a_manual_dispatch_that_needs_approval_and_none_creates_a_ledger(tmp_path, monkeypatch) -> None:
    _commissioned(tmp_path, monkeypatch, Signer())

    # Renewal included: a schedule, and any other event, starts nothing.
    assert producer.OPERATION_POLICY == {
        operation: (frozenset({"workflow_dispatch"}), True)
        for operation in ("release", "promote", "renew", "revoke", "resume")
    }
    for operation in ("initialize", "initialize-ledger", "restore", ""):
        with pytest.raises(ProducerError, match="not an operation"):
            producer.authorize(
                producer.SIGNER_ENVIRONMENT, operation=operation, environ=RUN, api=lambda _path: None,
                require_approval=True,
            )
    for event in ("schedule", "push", "pull_request"):
        with pytest.raises(ProducerError, match="was started by"):
            producer.authorize(
                producer.SIGNER_ENVIRONMENT, operation="renew", environ={**RUN, "GITHUB_EVENT_NAME": event},
                api=lambda _path: None, require_approval=True,
            )


# ----- the signer's ledger ---------------------------------------------------

KEY_ENVELOPE = {"ciphertext": "c2VhbGVk", "nonce": "bm9uY2U=", "wrap_meta": "{}"}


class Avault:
    """The signer's ``avault sign``: every request it saw, and whether it works."""

    def __init__(self, signer: Signer) -> None:
        self.signer = signer
        self.requests: list[dict] = []
        self.fail = False

    def __call__(self, request: bytes) -> str:
        self.requests.append(json.loads(request))
        if self.fail:
            raise ProducerError("avault sign failed (exit status 70)")
        return self.signer.sign(self.requests[-1]["digest"])

    def digests(self) -> list[str]:
        return [request["digest"] for request in self.requests]


class Signing:
    """One commissioned ledger and the pieces a ``sign`` step is given."""

    def __init__(self, root: Path, *, first: int = 1, floor: int = 0) -> None:
        self.signer = Signer()
        self.roots = self.signer.roots(floor=floor)
        self.ledger = producer.PublisherLedger(root / "ledger")
        self.ledger.initialize(key_id=KEY_ID, next_sequence=first, roots=self.roots)
        self.avault = Avault(self.signer)
        self.published: bytes | None = None

    def sign(self, operation, *, now=LATER, baselines=None, roots=None) -> bytes:
        return producer.sign_from_ledger(
            self.ledger, operation, now=now, roots=roots or self.roots, verifier=LocalVerifier(), sign=self.avault,
            key_name="MODEL_HUB_ENGINE_CHANNEL_KEY", key_envelope=KEY_ENVELOPE, published=self.published,
            qualified_baselines=baselines,
        )

    def payload(self, envelope: bytes) -> channel.ChannelPayload:
        return channel.authenticate(envelope, roots=self.roots, verifier=LocalVerifier())[1]

    def state(self) -> producer.LedgerState:
        with self.ledger.locked():
            return self.ledger.load(self.roots)

    def state_bytes(self) -> bytes:
        return self.ledger.state_path.read_bytes()

    def journal(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in sorted(self.ledger.journal_dir.iterdir())}


PROMOTE_1 = Promote(manifest_bytes(build=1), "3.2.6")
PROMOTE_2 = Promote(manifest_bytes(build=2), "3.2.6")
REVOKE_2 = Revoke(V2, V1, "regression in streaming")


def _listed(signing: Signing) -> Signing:
    """A ledger that promoted builds 1 and 2: sequences 1 and 2."""
    signing.sign(PROMOTE_1)
    signing.sign(PROMOTE_2, baselines={V1: hashlib.sha256(manifest_bytes(build=1)).hexdigest()})
    return signing


@needs_directory_flush
def test_commissioning_creates_the_ledger_once_bound_to_the_channel_and_a_shipped_key(tmp_path) -> None:
    signer = Signer()
    roots = signer.roots(floor=40)
    ledger = producer.PublisherLedger(tmp_path / "ledger")

    for arguments in ({"key_id": "another-key", "next_sequence": 40}, {"key_id": KEY_ID, "next_sequence": 39},
                      {"key_id": KEY_ID, "next_sequence": 0}):
        with pytest.raises(ProducerError):
            ledger.initialize(roots=roots, **arguments)
    assert not ledger.state_path.exists()

    ledger.initialize(key_id=KEY_ID, next_sequence=40, roots=roots)

    state = json.loads(ledger.state_path.read_text())
    assert state == {
        "schema_version": 1, "repository": "avibe-bot/avibe", "channel": "stable", "key_id": KEY_ID,
        "next_sequence": 40, "allocation": None, "releases": {}, "revoked": [],
    }
    # A ledger is initialized once: not over existing state, and not over history alone.
    with pytest.raises(ProducerError, match="initialized once"):
        ledger.initialize(key_id=KEY_ID, next_sequence=40, roots=roots)
    ledger.state_path.unlink()
    (ledger.journal_dir / "000000000040.json").write_bytes(b"{}")
    with pytest.raises(ProducerError, match="initialized once"):
        ledger.initialize(key_id=KEY_ID, next_sequence=41, roots=roots)


@pytest.mark.parametrize(
    ("variable", "relative"),
    [("RUNNER_TEMP", "ledger"), ("GITHUB_WORKSPACE", "nested/ledger"), ("RUNNER_WORKSPACE", "ledger")],
)
def test_a_ledger_must_outlive_the_job_that_uses_it(tmp_path, variable, relative) -> None:
    ephemeral = tmp_path / "ephemeral"
    producer.PublisherLedger(tmp_path / "durable" / "ledger").require_durable_location({variable: str(ephemeral)})

    with pytest.raises(ProducerError, match="outlive the job"):
        producer.PublisherLedger(ephemeral / relative).require_durable_location({variable: str(ephemeral)})
    with pytest.raises(ProducerError, match="absolute"):
        producer.PublisherLedger(Path("ledger")).require_durable_location({})


@needs_directory_flush
def test_each_change_is_allocated_from_the_ledger_signed_and_journaled(tmp_path) -> None:
    signing = _listed(Signing(tmp_path))

    withdrawn = signing.payload(signing.sign(REVOKE_2))
    renewed_envelope = signing.sign(Renew(), now=LATER + timedelta(days=1))
    renewed = signing.payload(renewed_envelope)

    assert (withdrawn.sequence, renewed.sequence) == (3, 4)
    assert renewed.tombstone(V2) == withdrawn.tombstone(V2)
    assert [item.manifest_bytes for item in renewed.releases] == [manifest_bytes(build=1), manifest_bytes(build=2)]
    state = signing.state()
    assert (state.next_sequence, state.allocation.sequence, state.allocation.envelope) == (5, 4, renewed_envelope)
    assert sorted(signing.journal()) == [f"{sequence:012d}.json" for sequence in (1, 2, 3, 4)]
    assert signing.journal()["000000000004.json"] == renewed_envelope
    # One request per allocation, each over that allocation's own payload.
    assert len(set(signing.avault.digests())) == len(signing.avault.digests()) == 4


@needs_directory_flush
@pytest.mark.parametrize("published", ["an older signed index", "deleted", "garbage", "another key's index"])
def test_what_the_published_index_says_is_never_the_state_a_change_is_signed_from(tmp_path, published) -> None:
    """MH-ENGINE-CHANNEL-008: an old, missing or foreign published index cannot make the signer sign older state."""

    signing = _listed(Signing(tmp_path))
    before_withdrawal = signing.ledger.state_path.read_bytes()
    first_index = signing.journal()["000000000001.json"]
    signing.sign(REVOKE_2)
    assert signing.state_bytes() != before_withdrawal
    # The mirror now serves something that predates the withdrawal, or nothing a client could use.
    signing.published = {
        "an older signed index": first_index,
        "deleted": None,
        "garbage": b"not an index",
        "another key's index": envelope_bytes(Signer(), _current(99).raw),
    }[published]

    renewed = signing.payload(signing.sign(Renew()))

    assert renewed.sequence == 4
    assert renewed.tombstone(V2) is not None and len(renewed.releases) == 2


@needs_directory_flush
@pytest.mark.parametrize("case", ["a higher sequence", "the same sequence, another payload", "a ledger with no allocation"])
def test_a_published_index_this_ledger_never_allocated_stops_the_signer(tmp_path, case) -> None:
    signing = Signing(tmp_path)
    if case != "a ledger with no allocation":
        _listed(signing)
    foreign = {
        "a higher sequence": _current(7),
        "the same sequence, another payload": _current(2, revoked=[tombstone(V2, V1)]),
        "a ledger with no allocation": _current(1),
    }[case]
    signing.published = envelope_bytes(signing.signer, foreign.raw)
    before, requests = signing.state_bytes(), len(signing.avault.requests)

    for operation in (Renew(), None, PROMOTE_1):
        with pytest.raises(ProducerError, match="behind the published index"):
            signing.sign(operation)

    assert signing.state_bytes() == before and len(signing.avault.requests) == requests
    # The index the ledger itself allocated is, of course, not a conflict.
    if case != "a ledger with no allocation":
        signing.published = signing.state().allocation.envelope
        assert signing.payload(signing.sign(Renew())).sequence == 3


def _damage(mutate):
    def apply(signing: Signing) -> None:
        document = json.loads(signing.ledger.state_path.read_text())
        mutate(document)
        signing.ledger.state_path.write_text(json.dumps(document))
    return apply


@needs_directory_flush
@pytest.mark.parametrize(
    "damage",
    [
        lambda signing: signing.ledger.state_path.unlink(),
        lambda signing: signing.ledger.state_path.write_bytes(b""),
        lambda signing: signing.ledger.state_path.write_bytes(b"{"),
        _damage(lambda d: d.update(schema_version=2)),
        _damage(lambda d: d.update(repository="someone/else")),
        _damage(lambda d: d.update(channel="beta")),
        _damage(lambda d: d.update(key_id="another-key")),
        _damage(lambda d: d.update(next_sequence=2)),
        _damage(lambda d: d.update(next_sequence=9)),
        _damage(lambda d: d.update(next_sequence="3")),
        _damage(lambda d: d.update(allocation=None)),
        _damage(lambda d: d.update(revoked=[])),
        _damage(lambda d: d["releases"].pop(V1)),
        _damage(lambda d: d["releases"][V1].update(min_avibe_version="3.3.0")),
        _damage(lambda d: d["allocation"].update(payload=d["allocation"]["payload"][:-8])),
        _damage(lambda d: d["allocation"].update(sequence=5)),
        _damage(lambda d: d.pop("revoked")),
        _damage(lambda d: d.update(note="edited by hand")),
    ],
)
def test_a_missing_damaged_or_mismatched_ledger_stops_every_routine_operation(tmp_path, damage) -> None:
    """MH-ENGINE-CHANNEL-009: nothing is signed from a ledger that is absent or does not check out."""

    signing = _listed(Signing(tmp_path))
    signing.sign(REVOKE_2)
    damage(signing)
    before = signing.state_bytes() if signing.ledger.state_path.exists() else None
    requests = len(signing.avault.requests)

    for operation in (Renew(), None, Promote(manifest_bytes(build=3), "3.2.6")):
        with pytest.raises(ProducerError):
            signing.sign(operation)

    assert len(signing.avault.requests) == requests
    assert (signing.state_bytes() if signing.ledger.state_path.exists() else None) == before


def test_a_ledger_directory_that_does_not_exist_is_not_created_by_a_routine_operation(tmp_path) -> None:
    signer = Signer()
    ledger = producer.PublisherLedger(tmp_path / "never-commissioned")
    avault = Avault(signer)

    for operation in (PROMOTE_1, Renew(), None):
        with pytest.raises(ProducerError, match="owner commissioning"):
            producer.sign_from_ledger(
                ledger, operation, now=LATER, roots=signer.roots(), verifier=LocalVerifier(), sign=avault,
                key_name="K", key_envelope=KEY_ENVELOPE, published=None,
            )
    assert not ledger.directory.exists() and avault.requests == []


def test_a_platform_that_cannot_flush_a_directory_cannot_hold_the_ledger(tmp_path, monkeypatch) -> None:
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)
    signer = Signer()
    ledger = producer.PublisherLedger(tmp_path / "ledger")

    with pytest.raises(ProducerError, match="cannot flush a directory"):
        ledger.initialize(key_id=KEY_ID, next_sequence=1, roots=signer.roots())
    assert not ledger.state_path.exists()


class _Fail:
    """Make the Nth call of one ``os`` function fail, as a full disk or a crash would."""

    def __init__(self, monkeypatch, name: str, *, on_call: int) -> None:
        self.calls = 0
        original = getattr(os, name)

        def failing(*args, **kwargs):
            self.calls += 1
            if self.calls == on_call:
                raise OSError(28, f"injected {name} failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(os, name, failing)


@needs_directory_flush
@pytest.mark.parametrize("boundary", ["fsync", "replace"])
def test_an_allocation_that_could_not_be_committed_is_neither_recorded_nor_signed(tmp_path, monkeypatch, boundary) -> None:
    signing = _listed(Signing(tmp_path))
    before, requests = signing.state_bytes(), len(signing.avault.requests)

    with monkeypatch.context() as patch:
        _Fail(patch, boundary, on_call=1)
        with pytest.raises(OSError):
            signing.sign(REVOKE_2)

    assert signing.state_bytes() == before and len(signing.avault.requests) == requests
    assert not any(path.name.endswith(".tmp") for path in signing.ledger.directory.iterdir())
    # The sequence was not spent: the next change takes it.
    assert signing.payload(signing.sign(REVOKE_2)).sequence == 3


@needs_directory_flush
def test_a_commit_whose_durability_is_unknown_is_never_signed_in_that_run(tmp_path, monkeypatch) -> None:
    signing = _listed(Signing(tmp_path))
    requests = len(signing.avault.requests)

    with monkeypatch.context() as patch:
        # The file is flushed and renamed; flushing the directory entry fails.
        _Fail(patch, "fsync", on_call=2)
        with pytest.raises(OSError):
            signing.sign(REVOKE_2)

    assert len(signing.avault.requests) == requests
    # Whatever survived is the committed payload itself: resuming signs exactly it, at its sequence.
    state = signing.state()
    assert (state.allocation.sequence, state.allocation.envelope) == (3, None)
    resumed = signing.sign(None)
    assert channel.parse_envelope(resumed).payload == state.allocation.payload
    assert signing.avault.digests()[requests:] == [channel.channel_digest(state.allocation.payload)]


@needs_directory_flush
def test_a_sequence_is_only_ever_signed_over_the_payload_it_was_committed_with(tmp_path, monkeypatch) -> None:
    """MH-ENGINE-CHANNEL-012: a failed signature or commit never yields a second payload for a sequence."""

    signing = _listed(Signing(tmp_path))
    requests = len(signing.avault.requests)

    # avault fails after the allocation was committed.
    signing.avault.fail = True
    with pytest.raises(ProducerError, match="avault sign failed"):
        signing.sign(REVOKE_2)
    signing.avault.fail = False
    committed = signing.state().allocation
    assert (committed.sequence, committed.envelope) == (3, None)

    # The envelope is signed, but committing it fails: it is not returned.
    with monkeypatch.context() as patch:
        _Fail(patch, "replace", on_call=1)
        with pytest.raises(OSError):
            signing.sign(None)
    assert signing.state().allocation == committed

    envelope = signing.sign(None)

    assert channel.parse_envelope(envelope).payload == committed.payload
    assert signing.payload(envelope).sequence == 3 and signing.state().next_sequence == 4
    # Three requests, all for the same sequence and the same payload digest.
    assert set(signing.avault.digests()[requests:]) == {channel.channel_digest(committed.payload)}
    assert len(signing.avault.digests()[requests:]) == 3


@needs_directory_flush
def test_a_committed_envelope_whose_journal_copy_failed_is_repaired_without_a_new_signature(tmp_path, monkeypatch) -> None:
    signing = _listed(Signing(tmp_path))

    with monkeypatch.context() as patch:
        _Fail(patch, "link", on_call=1)
        with pytest.raises(OSError):
            signing.sign(REVOKE_2)

    committed = signing.state().allocation
    assert committed.envelope is not None and "000000000003.json" not in signing.journal()
    requests = len(signing.avault.requests)

    resumed = signing.sign(None)

    assert resumed == committed.envelope and signing.journal()["000000000003.json"] == committed.envelope
    assert len(signing.avault.requests) == requests and signing.state().next_sequence == 4
    # A journal that disagrees with the ledger is damage, not something to overwrite.
    (signing.ledger.journal_dir / "000000000003.json").write_bytes(b"{}")
    with pytest.raises(ProducerError, match="journal holds a different envelope"):
        signing.sign(None)


@needs_directory_flush
def test_resuming_publishes_the_same_envelope_again_byte_for_byte(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-012: a retry after a failed publication is the identical envelope."""

    signing = _listed(Signing(tmp_path))
    envelope = signing.sign(REVOKE_2)
    requests = len(signing.avault.requests)

    # The hand-off, the bucket write or the read-back failed; each retry starts here.
    for published in (None, signing.journal()["000000000002.json"], envelope):
        signing.published = published
        assert signing.sign(None) == envelope

    assert len(signing.avault.requests) == requests and signing.state().next_sequence == 4
    with pytest.raises(ProducerError, match="expired; renew"):
        signing.sign(None, now=LATER + timedelta(days=8))
    assert signing.payload(signing.sign(Renew(), now=LATER + timedelta(days=8))).tombstone(V2) is not None


@needs_directory_flush
def test_a_change_that_was_allocated_but_never_published_is_carried_by_the_next_one(tmp_path) -> None:
    signing = _listed(Signing(tmp_path))
    signing.avault.fail = True
    with pytest.raises(ProducerError):
        signing.sign(REVOKE_2)
    signing.avault.fail = False

    # Another approved change supersedes the unsigned withdrawal instead of resuming it.
    promoted = signing.payload(
        signing.sign(Promote(manifest_bytes(build=3), "3.2.7"),
                     baselines={V1: hashlib.sha256(manifest_bytes(build=1)).hexdigest()})
    )

    # Sequence 3 was spent on the withdrawal and is never signed; nothing it allocated is lost.
    assert promoted.sequence == 4
    assert promoted.tombstone(V2) == channel.Tombstone(V2, V1, "regression in streaming")
    assert [item.asset_release_tag for item in promoted.releases] == [V1, V2, V3]
    assert sorted(signing.journal()) == [f"{sequence:012d}.json" for sequence in (1, 2, 4)]


@needs_directory_flush
def test_a_raised_shipped_floor_moves_the_next_sequence_up_and_never_down(tmp_path) -> None:
    signing = _listed(Signing(tmp_path))

    raised = signing.signer.roots(floor=100)
    jumped = signing.payload(signing.sign(Renew(), roots=raised))
    following = signing.payload(signing.sign(Renew(), roots=raised))
    lowered = signing.payload(signing.sign(Renew(), roots=signing.signer.roots(floor=5)))

    assert (jumped.sequence, following.sequence, lowered.sequence) == (100, 101, 102)
    assert len(lowered.releases) == 2


@needs_directory_flush
def test_a_candidate_is_promoted_only_if_it_was_qualified_against_every_release_the_ledger_lists(tmp_path) -> None:
    signing = _listed(Signing(tmp_path))
    signing.sign(Promote(manifest_bytes(build=3), "3.2.6"),
                 baselines={V1: hashlib.sha256(manifest_bytes(build=1)).hexdigest(),
                            V2: hashlib.sha256(manifest_bytes(build=2)).hexdigest()})
    signing.sign(Revoke(V3, V2, "regression in streaming"))
    before, requests = signing.state_bytes(), len(signing.avault.requests)
    candidate = Promote(manifest_bytes("v8.0.70", 1), "3.2.6")
    digests = {tag: hashlib.sha256(manifest_bytes(build=build)).hexdigest() for tag, build in ((V1, 1), (V2, 2))}

    # The published index the qualification read was older, or was replaced: a listed release is missing.
    for baselines in (None, {}, {V1: digests[V1]}, {**digests, V2: "0" * 64}):
        with pytest.raises(ProducerError, match="not qualified against every release"):
            signing.sign(candidate, baselines=baselines)
    assert signing.state_bytes() == before and len(signing.avault.requests) == requests

    # The withdrawn release is not a baseline; extra baselines, such as a bundled engine, are fine.
    promoted = signing.payload(signing.sign(candidate, baselines={**digests, "model-hub-engine-v8.0.60-9": "1" * 64}))
    assert promoted.release("model-hub-engine-v8.0.70-1") is not None


@needs_directory_flush
def test_concurrent_signers_are_serialized_into_distinct_increasing_sequences(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-012: concurrent signers never share or reuse a sequence."""

    signing = _listed(Signing(tmp_path))
    start = threading.Barrier(8)
    envelopes: list[bytes] = []

    def renew() -> None:
        start.wait()
        envelopes.append(signing.sign(Renew()))

    threads = [threading.Thread(target=renew) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    sequences = sorted(signing.payload(envelope).sequence for envelope in envelopes)
    assert sequences == list(range(3, 11)) and signing.state().next_sequence == 11
    assert sorted(signing.journal()) == [f"{sequence:012d}.json" for sequence in range(1, 11)]
    assert all(len(signing.payload(envelope).releases) == 2 for envelope in envelopes)


@needs_directory_flush
def test_concurrent_resumes_of_an_unsigned_allocation_yield_one_envelope(tmp_path) -> None:
    signing = _listed(Signing(tmp_path))
    signing.avault.fail = True
    with pytest.raises(ProducerError):
        signing.sign(REVOKE_2)
    signing.avault.fail = False
    requests = len(signing.avault.requests)
    start = threading.Barrier(6)
    envelopes: list[bytes] = []

    def resume() -> None:
        start.wait()
        envelopes.append(signing.sign(None))

    threads = [threading.Thread(target=resume) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(envelopes) == 6 and len(set(envelopes)) == 1
    assert len(signing.avault.requests) == requests + 1 and signing.state().next_sequence == 4


# ----- publication -----------------------------------------------------------


class FakeBucket:
    """An S3 API that honors If-Match and If-None-Match, like the real bucket."""

    def __init__(self, stored: bytes | None = None) -> None:
        self.stored = stored
        self.version = 0
        self.writes: list[list[str]] = []
        self.before_write = None

    def etag(self) -> str:
        return f'"version-{self.version}"'

    def __call__(self, command):
        verb = command[2]
        if verb == "list-objects-v2":
            listing = {"Contents": [{"Key": producer.INDEX_KEY, "Size": len(self.stored)}]} if self.stored is not None else {}
            return subprocess.CompletedProcess(command, 0, json.dumps(listing), "")
        if verb == "get-object":
            Path(command[command.index("json") + 1]).write_bytes(self.stored)
            return subprocess.CompletedProcess(command, 0, json.dumps({"ETag": self.etag()}), "")
        assert verb == "put-object"
        self.writes.append(list(command))
        if self.before_write is not None:
            self.before_write(self)
            self.before_write = None
        if "--if-match" in command:
            allowed = self.stored is not None and command[command.index("--if-match") + 1] == self.etag()
        else:
            allowed = command[command.index("--if-none-match") + 1] == "*" and self.stored is None
        if not allowed:
            return subprocess.CompletedProcess(command, 254, "", "PreconditionFailed")
        self.stored = Path(command[command.index("--body") + 1]).read_bytes()
        self.version += 1
        return subprocess.CompletedProcess(command, 0, "{}", "")


def _envelope(signer: Signer, current, operation, *, sequence=None) -> bytes:
    sequence = sequence if sequence is not None else current.sequence + 1
    return envelope_bytes(signer, producer.plan_payload(current, operation, now=LATER, sequence=sequence))


def _publish(signer: Signer, bucket: FakeBucket, envelope: bytes, **options):
    options.setdefault("fetch_public", lambda: bucket.stored)
    return producer.publish(
        envelope,
        bucket=producer.IndexBucket("avibe", "https://example.invalid", bucket),
        roots=signer.roots(),
        verifier=LocalVerifier(),
        sleep=lambda _seconds: None,
        public_wait_seconds=0,
        **options,
    )


def _stored(signer: Signer, payload: channel.ChannelPayload) -> bytes:
    return envelope_bytes(signer, payload.raw)


def test_the_index_is_replaced_only_over_the_object_that_was_read_and_then_read_back() -> None:
    signer = Signer()
    current = _current()
    bucket = FakeBucket(_stored(signer, current))
    envelope = _envelope(signer, current, REVOKE_2)

    published = _publish(signer, bucket, envelope)

    assert published.sequence == 11 and bucket.stored == envelope
    (write,) = bucket.writes
    assert write[write.index("--if-match") + 1] == '"version-0"'
    assert write[write.index("--key") + 1] == "index/model-hub-engine.json"
    assert write[write.index("--cache-control") + 1] == "public, max-age=60"


@pytest.mark.parametrize(
    ("stored", "condition"),
    [
        ("deleted", "--if-none-match"),
        ("garbage", "--if-match"),
        ("another key's index", "--if-match"),
        ("an older signed index", "--if-match"),
        # Sequences may skip: the signer spent some that were never published.
        ("several sequences back", "--if-match"),
    ],
)
def test_an_object_that_is_missing_unreadable_or_older_is_put_back_from_the_signers_envelope(stored, condition) -> None:
    """MH-ENGINE-CHANNEL-008: a deleted or rolled-back index is replaced, never continued from."""

    signer = Signer()
    withdrawn = _plan(_current(), REVOKE_2)
    envelope = _envelope(signer, withdrawn, Renew(), sequence=20)
    bucket = FakeBucket(
        {
            "deleted": None,
            "garbage": b"not an index",
            "another key's index": envelope_bytes(Signer(), _current(99).raw),
            "an older signed index": _stored(signer, _current()),
            "several sequences back": _stored(signer, withdrawn),
        }[stored]
    )

    published = _publish(signer, bucket, envelope)

    assert bucket.stored == envelope and published.tombstone(V2) is not None
    assert condition in bucket.writes[0]


def test_publishing_the_same_envelope_again_writes_nothing() -> None:
    signer = Signer()
    envelope = _envelope(signer, _current(), Renew())
    bucket = FakeBucket(envelope)

    published = _publish(signer, bucket, envelope)

    assert published.sequence == 11 and bucket.writes == [] and bucket.stored == envelope


def test_a_writer_that_lost_a_race_does_not_overwrite_the_winner() -> None:
    """MH-ENGINE-CHANNEL-008: of two racing writers, only the conditional write that still matches wins."""

    signer = Signer()
    current = _current()
    bucket = FakeBucket(_stored(signer, current))
    winner = _envelope(signer, current, REVOKE_2, sequence=12)
    loser = _envelope(signer, current, Renew(), sequence=11)

    def another_writer_publishes(target: FakeBucket) -> None:
        target.stored, target.version = winner, target.version + 1

    bucket.before_write = another_writer_publishes

    with pytest.raises(ProducerError, match="changed since it was read"):
        _publish(signer, bucket, loser)
    assert bucket.stored == winner


@pytest.mark.parametrize(
    ("published", "message"),
    [
        # The signer's ledger is behind what this key already published.
        ("a higher sequence", "not older than the one to publish"),
        ("the same sequence, another payload", "not older than the one to publish"),
        # A newer sequence that forgets a withdrawal, or changes a release, is not a successor.
        ("a newer sequence without the withdrawal", "rewrites the stored one"),
        ("a newer sequence with other release bytes", "rewrites the stored one"),
    ],
)
def test_a_stale_writer_cannot_undo_a_newer_index_or_drop_its_withdrawal(published, message) -> None:
    """MH-ENGINE-CHANNEL-008: a stale writer cannot put back an older index or drop a withdrawal."""

    signer = Signer()
    old = _current()
    withdrawn = _plan(old, REVOKE_2, sequence=12)
    stored = _stored(signer, withdrawn)
    bucket = FakeBucket(stored)
    listed = [release_entry(manifest_bytes(build=1)), release_entry(manifest_bytes(build=2))]
    envelope = {
        "a higher sequence": _envelope(signer, old, Renew(), sequence=11),
        "the same sequence, another payload": _envelope(signer, old, Renew(), sequence=12),
        "a newer sequence without the withdrawal": envelope_bytes(signer, encode(payload_document(13, releases=listed))),
        "a newer sequence with other release bytes": envelope_bytes(
            signer,
            encode(payload_document(
                13, releases=[release_entry(manifest_bytes(build=1, salt="x")), listed[1]], revoked=[tombstone(V2, V1)]
            )),
        ),
    }[published]

    with pytest.raises(ProducerError, match=message):
        _publish(signer, bucket, envelope)

    assert bucket.writes == [] and bucket.stored == stored


def test_an_index_no_shipped_key_signed_is_never_written() -> None:
    signer = Signer()
    bucket = FakeBucket(_stored(signer, _current()))

    with pytest.raises(ProducerError, match="does not verify"):
        _publish(signer, bucket, _envelope(Signer(), _current(), Renew()))
    assert bucket.writes == []


def test_a_change_is_published_only_when_clients_are_served_the_exact_envelope() -> None:
    signer = Signer()
    current = _current()
    envelope = _envelope(signer, current, Renew())
    # The same payload and sequence under a second signature is still not the envelope that was written.
    resigned = envelope_bytes(signer, channel.parse_envelope(envelope).payload)
    assert resigned != envelope

    for served in (lambda: _stored(signer, current), lambda: None, lambda: resigned):
        bucket = FakeBucket(_stored(signer, current))
        with pytest.raises(ProducerError, match="did not serve it in time"):
            _publish(signer, bucket, envelope, fetch_public=served)
        assert bucket.stored == envelope
        # Publishing it again after the failure only repeats the read-back.
        assert _publish(signer, bucket, envelope).sequence == 11 and len(bucket.writes) == 1


@needs_directory_flush
def test_a_change_survives_a_failed_publication_and_a_rolled_back_mirror_end_to_end(tmp_path) -> None:
    signing = _listed(Signing(tmp_path))
    bucket = FakeBucket(signing.journal()["000000000002.json"])
    withdrawal = signing.sign(REVOKE_2)

    # The write lands, the public read-back does not; then the mirror is rolled back to an old signed index.
    with pytest.raises(ProducerError, match="did not serve it in time"):
        _publish(signing.signer, bucket, withdrawal, fetch_public=lambda: None)
    bucket.stored, bucket.version = signing.journal()["000000000001.json"], bucket.version + 1
    signing.published = bucket.stored

    resumed = signing.sign(None)
    assert resumed == withdrawal
    assert _publish(signing.signer, bucket, resumed).tombstone(V2) is not None
    signing.published = bucket.stored
    renewed = _publish(signing.signer, bucket, signing.sign(Renew()))

    assert renewed.sequence == 4 and renewed.tombstone(V2) is not None and bucket.stored != withdrawal


# ----- evidence read from the published index --------------------------------


def test_baselines_come_from_the_authenticated_published_index_and_what_released_avibe_bundles(
    tmp_path, monkeypatch, capsys
) -> None:
    bundled = tmp_path / "bundled.json"
    bundled.write_bytes(manifest_bytes(build=3))

    # Uncommissioned: there is no index to read, and no key is needed.
    monkeypatch.setattr(producer, "fetch_public_index", lambda: (_ for _ in ()).throw(AssertionError("no request")))
    assert producer.main(["baselines", "--bundled", str(bundled), "--output-dir", str(tmp_path / "first")]) == 0
    assert json.loads((tmp_path / "first" / "baselines.json").read_text()) == {
        V3: hashlib.sha256(manifest_bytes(build=3)).hexdigest()
    }

    signer = Signer()
    _commissioned(tmp_path, monkeypatch, signer)
    monkeypatch.setattr(producer, "avault_verifier", lambda _path: LocalVerifier())
    index = envelope_bytes(signer, _current(revoked=[tombstone(V2, V1)]).raw)
    monkeypatch.setattr(producer, "fetch_public_index", lambda: index)
    arguments = ["baselines", "--avault", "avault", "--bundled", str(bundled), "--output-dir", str(tmp_path / "listed")]
    assert producer.main(arguments) == 0

    # The standing listed release and the bundled engine; the withdrawn release is not a baseline.
    assert sorted(json.loads((tmp_path / "listed" / "baselines.json").read_text())) == [V1, V3]
    assert (tmp_path / "listed" / "manifests" / f"{V1}.json").read_bytes() == manifest_bytes(build=1)
    capsys.readouterr()
    with pytest.raises(ProducerError, match="different bytes"):
        producer.baseline_manifests(_current(), [manifest_bytes(build=1, salt="x")])


# ----- the workflow ----------------------------------------------------------


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_finalizer_is_dispatch_only_serialized_and_starts_with_no_token_scope() -> None:
    workflow = _jobs()

    # No schedule: how renewals are approved is an open commissioning decision.
    assert set(workflow[True]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {}
    assert workflow["concurrency"] == {"group": "model-hub-engine-channel", "cancel-in-progress": False}
    assert workflow["jobs"]["authorize"]["if"] == (
        "github.repository == 'avibe-bot/avibe' && github.ref == 'refs/heads/master'"
    )
    assert "environment" not in workflow["jobs"]["authorize"] and "environment" not in workflow["jobs"]["qualify"]


def test_the_workflow_offers_only_the_operations_the_script_authorizes_and_none_touches_the_ledgers_existence() -> None:
    workflow = _jobs()
    inputs = workflow[True]["workflow_dispatch"]["inputs"]

    assert inputs["operation"]["options"] == list(producer.OPERATION_POLICY)
    assert set(inputs) == {"operation", "asset_release_tag", "qualification_run_id", "rollback_to", "reason"}
    # Creating a ledger is owner commissioning, run by hand; no step of this workflow reaches it.
    scripts = "\n".join(step.get("run", "") for job in workflow["jobs"].values() for step in job["steps"])
    for forbidden in ("initialize", "--first-sequence", "first_publication", "recover"):
        assert forbidden not in scripts and forbidden not in json.dumps(inputs)
    # Workflow artifacts carry an envelope between jobs for one day; none is an archive to continue from.
    retained = [
        step["with"]["retention-days"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert retained and set(retained) == {1}


def test_every_privileged_job_follows_authorization_and_proves_approval_before_its_first_privileged_step() -> None:
    jobs = _jobs()["jobs"]
    privileged = {
        "release": producer.WRITER_ENVIRONMENT,
        "sign": producer.SIGNER_ENVIRONMENT,
        "publish": producer.WRITER_ENVIRONMENT,
    }

    assert {name for name, job in jobs.items() if "environment" in job} == set(privileged)
    for name, environment in privileged.items():
        job = jobs[name]
        assert job["environment"] == environment
        assert "authorize" in job["needs"]
        assert "needs.authorize.result == 'success'" in job["if"] or "always()" not in job["if"]
        runs = [step.get("run", "") for step in job["steps"]]
        first_run = next(index for index, run in enumerate(runs) if run)
        assert (
            f'authorize \\\n  --environment {environment} --operation "$OPERATION" --require-approval'
            in runs[first_run]
        )
        # Nothing but the checkout precedes the approval check.
        assert all(str(step.get("uses", "")).startswith("actions/checkout@") for step in job["steps"][:first_run])


def test_the_signing_job_holds_no_bucket_credential_and_the_writer_holds_neither_key_nor_ledger() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    jobs = _jobs()["jobs"]

    def mentions(job: str, needle: str) -> bool:
        return needle in json.dumps(jobs[job])

    # Scheduled onto the signer's runner group, whose policy decides which workflows it admits.
    assert jobs["sign"]["runs-on"] == {
        "group": "model-hub-engine-channel-signer",
        "labels": ["self-hosted", "model-hub-engine-channel-signer"],
    }
    assert not mentions("sign", "secrets.") and not mentions("sign", "AWS_")
    assert mentions("sign", "MODEL_HUB_ENGINE_CHANNEL_LEDGER") and mentions("sign", "--ledger")
    for name in ("authorize", "qualify", "release", "publish"):
        assert not mentions(name, "LEDGER") and not mentions(name, "KEY_ENVELOPE")
        assert not mentions(name, "model_hub_engine_channel.py sign") and jobs[name]["runs-on"] == "ubuntu-latest"
    # The channel's bucket credential is not the release mirror's.
    assert "secrets.R2_" not in text and text.count("secrets.MODEL_HUB_ENGINE_CHANNEL_R2_") == 3
    # The signer runs this checkout's scripts only: no downloaded assets or receipt, no installs.
    sign_text = json.dumps(jobs["sign"])
    for forbidden in ("candidate-assets", "receipt", "pip install", "gh run download", "download-artifact",
                      "contents\": \"write"):
        assert forbidden not in sign_text
    assert all(job.get("permissions", {}).get("contents") != "write" for name, job in jobs.items() if name != "release")
    # What the hosted qualification read from the published index reaches the signer as data, to check against its ledger.
    assert jobs["qualify"]["outputs"] == {"baselines": "${{ steps.receipt.outputs.baselines }}"}
    assert mentions("sign", "needs.qualify.outputs.baselines") and mentions("sign", "--qualified-baselines")


def test_the_finalizer_reads_the_artifacts_the_qualification_workflow_writes() -> None:
    from scripts import model_hub_engine_qualification as qualification

    produced = (WORKFLOW.parent / "model-hub-engine-qualification.yml").read_text(encoding="utf-8")

    for artifact in (qualification.RECEIPT_ARTIFACT, qualification.ASSETS_ARTIFACT, "qualification-released-"):
        assert f"name: {artifact}" in produced
        assert artifact in json.dumps(_jobs()["jobs"]["qualify"])
