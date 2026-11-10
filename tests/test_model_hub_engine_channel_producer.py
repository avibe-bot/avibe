"""The one writer of the signed engine channel index (scripts/model_hub_engine_channel.py).

What this protects: a change to the index is planned from the index published
now and adds at most one entry; nothing is signed or written while the channel
is uncommissioned or the run is not the approved, reviewed workflow; and a
stale or racing writer cannot replace a newer index or drop a withdrawal.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
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


def _current(sequence: int = 10, *, releases=None, revoked=None) -> channel.ChannelPayload:
    entries = releases if releases is not None else [release_entry(manifest_bytes(build=1)), release_entry(manifest_bytes(build=2))]
    return channel.parse_payload(encode(payload_document(sequence, releases=entries, revoked=revoked)))


def _plan(current, operation, *, now=NOW + timedelta(hours=1), floor=0) -> channel.ChannelPayload:
    return channel.parse_payload(producer.plan_payload(current, operation, now=now, floor=floor))


# ----- planning --------------------------------------------------------------


def test_a_first_publication_promotes_one_release_at_the_shipped_floor() -> None:
    planned = _plan(None, Promote(manifest_bytes(build=1), "3.2.6"), floor=40)

    assert planned.sequence == 40
    assert [item.asset_release_tag for item in planned.releases] == [V1] and planned.revoked == ()
    # The index carries the reviewed manifest's exact bytes, not a reserialization.
    assert planned.releases[0].manifest_bytes == manifest_bytes(build=1)
    assert planned.expires_at - planned.issued_at == channel.MAX_VALIDITY
    assert _plan(None, Promote(manifest_bytes(build=1), "3.2.6")).sequence == 1


def test_every_change_copies_published_releases_and_withdrawals_byte_for_byte() -> None:
    current = _current(revoked=[tombstone(V2, V1)])

    for operation in (Renew(), Promote(manifest_bytes(build=3), "3.2.7"), Revoke(V3, V1, "bundled engine crashes")):
        planned = _plan(current, operation)

        assert planned.sequence == current.sequence + 1
        assert [item.manifest_bytes for item in planned.releases][:2] == [item.manifest_bytes for item in current.releases]
        assert planned.revoked[:1] == current.revoked
        channel.check_successor(producer._identities(current), current.revoked, planned)

    renewed = _plan(current, Renew())
    assert [(item.asset_release_tag, item.min_avibe_version) for item in renewed.releases] == [(V1, "3.2.6"), (V2, "3.2.6")]
    assert renewed.revoked == current.revoked
    assert renewed.issued_at == NOW + timedelta(hours=1)


@pytest.mark.parametrize(
    ("current", "operation", "message"),
    [
        (None, Renew(), "no published index"),
        (None, Revoke(V2, V1, "x"), "no published index"),
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
        producer.plan_payload(current, operation, now=NOW + timedelta(hours=1), floor=0)


def test_a_bundled_engine_that_was_never_listed_can_still_be_withdrawn() -> None:
    planned = _plan(_current(), Revoke(V3, V2, "bundled engine crashes on start"))

    assert planned.tombstone(V3).rollback_to == V2


def test_a_runner_clock_behind_the_published_index_plans_nothing() -> None:
    with pytest.raises(ProducerError, match="clock is behind"):
        producer.plan_payload(_current(), Renew(), now=NOW - timedelta(minutes=1), floor=0)


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
    payload = producer.plan_payload(_current(), Renew(), now=NOW + timedelta(hours=1), floor=0)

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
    payload = producer.plan_payload(_current(), Renew(), now=NOW + timedelta(hours=1), floor=0)

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
        ["authorize", "--environment", producer.SIGNER_ENVIRONMENT],
        ["authorize", "--environment", producer.WRITER_ENVIRONMENT, "--require-approval"],
        ["sign", "--operation", "renew", "--key-id", KEY_ID, "--key-name", "K", "--key-envelope", "{envelope}",
         "--output", "{output}", "--avault", "{avault}"],
        ["publish", "--envelope", "{envelope}", "--bucket", "avibe", "--endpoint-url", "https://example.invalid",
         "--avault", "{avault}"],
        ["baselines", "--output-dir", "{output}", "--avault", "{avault}"],
    ],
)
def test_while_avibe_ships_no_key_every_producer_step_stops_before_it_does_anything(
    tmp_path, monkeypatch, capsys, arguments
) -> None:
    avault = _recording_avault(tmp_path)
    (tmp_path / "envelope.json").write_text("{}")
    values = {"avault": str(avault), "envelope": str(tmp_path / "envelope.json"), "output": str(tmp_path / "out")}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an uncommissioned channel must not reach the network, the bucket, or avault")

    for name in ("github_api", "fetch_public_index", "_run_command", "_avault_sign"):
        monkeypatch.setattr(producer, name, forbidden)

    status = producer.main([item.format(**values) for item in arguments])

    assert status == 1
    assert "uncommissioned" in json.loads(capsys.readouterr().err)["error"]
    assert not (tmp_path / "calls").exists() and not (tmp_path / "out").exists()


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


def _commissioned(tmp_path, monkeypatch, signer: Signer) -> None:
    path = tmp_path / "roots.json"
    path.write_text(json.dumps({"schema_version": 1, "sequence_floor": 0,
                                "roots": [{"key_id": KEY_ID, "public_key": signer.public_key}]}))
    monkeypatch.setattr(producer, "TRUST_ROOTS_PATH", path)


def test_authorization_reads_protection_and_approval_from_github_for_this_run(tmp_path, monkeypatch) -> None:
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

    producer.authorize(producer.SIGNER_ENVIRONMENT, environ=RUN, api=api, require_approval=False)
    assert asked == [base, f"{base}/deployment-branch-policies"]
    producer.authorize(producer.SIGNER_ENVIRONMENT, environ=RUN, api=api, require_approval=True)

    for broken in (
        {**answers, base: None},
        {**answers, "/repos/avibe-bot/avibe/actions/runs/4242/approvals": []},
        {**answers, "/repos/avibe-bot/avibe/actions/runs/4242/approvals": None},
    ):
        with pytest.raises(ProducerError, match="is not authorized"):
            producer.authorize(producer.SIGNER_ENVIRONMENT, environ=RUN, api=broken.get, require_approval=True)
    with pytest.raises(ProducerError, match="refs/heads/master"):
        producer.authorize(
            producer.SIGNER_ENVIRONMENT, environ={**RUN, "GITHUB_REF": "refs/heads/x"}, api=api, require_approval=True
        )


def test_a_first_publication_is_refused_once_any_index_was_archived() -> None:
    producer.require_never_published(lambda _path: {"total_count": 0, "artifacts": []})

    for listing in ({"total_count": 3}, None, {"artifacts": []}):
        with pytest.raises(ProducerError):
            producer.require_never_published(lambda _path, listing=listing: listing)


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


def _envelope(signer: Signer, current, operation) -> bytes:
    return envelope_bytes(signer, producer.plan_payload(current, operation, now=NOW + timedelta(hours=1), floor=0))


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
    envelope = _envelope(signer, current, Revoke(V2, V1, "regression in streaming"))

    published = _publish(signer, bucket, envelope)

    assert published.sequence == 11 and bucket.stored == envelope
    (write,) = bucket.writes
    assert write[write.index("--if-match") + 1] == '"version-0"'
    assert write[write.index("--key") + 1] == "index/model-hub-engine.json"
    assert write[write.index("--cache-control") + 1] == "public, max-age=60"


def test_a_first_publication_writes_only_where_no_object_exists() -> None:
    signer = Signer()
    bucket = FakeBucket()
    envelope = _envelope(signer, None, Promote(manifest_bytes(build=1), "3.2.6"))

    with pytest.raises(ProducerError, match="first publication must be requested"):
        _publish(signer, bucket, envelope)
    _publish(signer, bucket, envelope, first_publication=True)

    assert bucket.stored == envelope and "--if-none-match" in bucket.writes[0]
    with pytest.raises(ProducerError, match="not a first publication"):
        _publish(signer, bucket, envelope, first_publication=True)


def test_a_writer_that_lost_a_race_does_not_overwrite_the_winner() -> None:
    signer = Signer()
    current = _current()
    bucket = FakeBucket(_stored(signer, current))
    winner = _envelope(signer, current, Revoke(V2, V1, "regression in streaming"))
    loser = _envelope(signer, current, Renew())

    def another_writer_publishes(target: FakeBucket) -> None:
        target.stored, target.version = winner, target.version + 1

    bucket.before_write = another_writer_publishes

    with pytest.raises(ProducerError, match="changed since it was read"):
        _publish(signer, bucket, loser)
    assert bucket.stored == winner


def test_a_stale_writer_cannot_undo_a_newer_index_or_drop_its_withdrawal() -> None:
    signer = Signer()
    old = _current()
    withdrawn = channel.parse_payload(
        producer.plan_payload(old, Revoke(V2, V1, "regression in streaming"), now=NOW + timedelta(hours=1), floor=0)
    )
    published = _stored(signer, withdrawn)
    bucket = FakeBucket(published)
    # Correctly signed, but planned from the index before the withdrawal.
    stale_renewal = _envelope(signer, old, Renew())
    # Correctly signed and correctly numbered, but without the withdrawal.
    rewritten = envelope_bytes(signer, encode(payload_document(12, releases=[release_entry(manifest_bytes(build=1)), release_entry(manifest_bytes(build=2))])))

    with pytest.raises(ProducerError, match="plan again"):
        _publish(signer, bucket, stale_renewal)
    with pytest.raises(ProducerError, match="rewrites published history"):
        _publish(signer, bucket, rewritten)

    assert bucket.writes == [] and bucket.stored == published
    # A renewal planned from the index published now carries the withdrawal on.
    renewed = _publish(signer, bucket, _envelope(signer, withdrawn, Renew()))
    assert renewed.tombstone(V2) == withdrawn.tombstone(V2)


def test_an_index_no_shipped_key_signed_is_never_written() -> None:
    signer = Signer()
    bucket = FakeBucket(_stored(signer, _current()))

    with pytest.raises(ProducerError, match="does not verify"):
        _publish(signer, bucket, _envelope(Signer(), _current(), Renew()))
    assert bucket.writes == []


def test_a_damaged_object_is_continued_from_the_archive_not_started_over() -> None:
    signer = Signer()
    archived = channel.parse_payload(
        producer.plan_payload(_current(), Revoke(V2, V1, "regression in streaming"), now=NOW + timedelta(hours=1), floor=0)
    )
    bucket = FakeBucket(b"not an index")
    successor = _envelope(signer, archived, Renew())

    with pytest.raises(ProducerError, match="recover from an archived envelope"):
        _publish(signer, bucket, successor)
    published = _publish(signer, bucket, successor, recovered=_stored(signer, archived))

    assert published.sequence == 12 and published.tombstone(V2) is not None
    assert "--if-match" in bucket.writes[0]

    # Recovery is refused while the stored object is intact and at least as new.
    intact = FakeBucket(_stored(signer, archived))
    with pytest.raises(ProducerError, match="recovery is not needed"):
        _publish(signer, intact, successor, recovered=_stored(signer, _current()))


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


def test_the_newest_archive_that_verifies_is_the_one_recovered_from(tmp_path) -> None:
    signer = Signer()
    ninth = _stored(signer, _current(9))
    for name, raw in {
        "1/a.json": _stored(signer, _current(7)),
        "2/a.json": ninth,
        "3/a.json": envelope_bytes(Signer(), _current(50).raw),
        "4/a.json": b"garbage",
    }.items():
        (tmp_path / name).parent.mkdir()
        (tmp_path / name).write_bytes(raw)

    newest = producer._newest_archived(tmp_path, roots=signer.roots(), verifier=LocalVerifier())

    assert newest == ninth
    with pytest.raises(ProducerError, match="no archived envelope verifies"):
        producer._newest_archived(tmp_path, roots=Signer().roots(), verifier=LocalVerifier())


def test_before_any_index_exists_baselines_are_listed_without_a_key(tmp_path, monkeypatch, capsys) -> None:
    bundled = tmp_path / "bundled.json"
    bundled.write_bytes(manifest_bytes(build=1))
    monkeypatch.setattr(producer, "fetch_public_index", lambda: None)

    status = producer.main(["baselines", "--first-publication", "--bundled", str(bundled), "--output-dir", str(tmp_path / "out")])

    assert status == 0
    assert json.loads((tmp_path / "out" / "baselines.json").read_text()) == {
        V1: hashlib.sha256(manifest_bytes(build=1)).hexdigest()
    }
    assert (tmp_path / "out" / "manifests" / f"{V1}.json").read_bytes() == manifest_bytes(build=1)
    # Once an index exists, a first publication is no longer a way around reading it.
    monkeypatch.setattr(producer, "fetch_public_index", lambda: b"{}")
    assert producer.main(["baselines", "--first-publication", "--output-dir", str(tmp_path / "again")]) == 1
    assert "not a first publication" in capsys.readouterr().err


def test_baselines_are_the_standing_listed_releases_and_what_released_avibe_bundles() -> None:
    current = _current(revoked=[tombstone(V2, V1)])

    baselines = producer.baseline_manifests(current, [manifest_bytes(build=3), manifest_bytes(build=2)])

    assert baselines == {V1: manifest_bytes(build=1), V3: manifest_bytes(build=3)}
    assert producer.baseline_manifests(None, [manifest_bytes(build=1)]) == {V1: manifest_bytes(build=1)}
    with pytest.raises(ProducerError, match="different bytes"):
        producer.baseline_manifests(current, [manifest_bytes(build=1, salt="x")])


# ----- the workflow ----------------------------------------------------------


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_finalizer_is_dispatch_only_serialized_and_starts_with_no_token_scope() -> None:
    workflow = _jobs()

    # No schedule: activating the daily renewal is part of commissioning.
    assert set(workflow[True]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {}
    assert workflow["concurrency"] == {"group": "model-hub-engine-channel", "cancel-in-progress": False}
    assert workflow["jobs"]["authorize"]["if"] == (
        "github.repository == 'avibe-bot/avibe' && github.ref == 'refs/heads/master'"
    )
    assert "environment" not in workflow["jobs"]["authorize"] and "environment" not in workflow["jobs"]["qualify"]


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
        assert f"authorize \\\n  --environment {environment} --require-approval" in runs[first_run]
        # Nothing but the checkout precedes the approval check.
        assert all(str(step.get("uses", "")).startswith("actions/checkout@") for step in job["steps"][:first_run])


def test_the_signing_job_holds_no_bucket_credential_and_the_writer_holds_no_key() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    jobs = _jobs()["jobs"]

    def mentions(job: str, needle: str) -> bool:
        return needle in json.dumps(jobs[job])

    assert jobs["sign"]["runs-on"] == ["self-hosted", "model-hub-engine-channel-signer"]
    assert not mentions("sign", "secrets.") and not mentions("sign", "AWS_")
    assert not mentions("publish", "KEY_ENVELOPE") and not mentions("publish", "model_hub_engine_channel.py sign")
    assert jobs["publish"]["runs-on"] == "ubuntu-latest"
    # The channel's bucket credential is not the release mirror's.
    assert "secrets.R2_" not in text and text.count("secrets.MODEL_HUB_ENGINE_CHANNEL_R2_") == 3
    # The signer runs this checkout's scripts only: no candidate assets, no receipt, no pip install.
    sign_text = json.dumps(jobs["sign"])
    for forbidden in ("candidate-assets", "receipt", "pip install", "gh run download", "contents\": \"write"):
        assert forbidden not in sign_text
    assert all(job.get("permissions", {}).get("contents") != "write" for name, job in jobs.items() if name != "release")


def test_the_finalizer_reads_the_artifacts_the_qualification_workflow_writes() -> None:
    from scripts import model_hub_engine_qualification as qualification

    produced = (WORKFLOW.parent / "model-hub-engine-qualification.yml").read_text(encoding="utf-8")
    consumed = WORKFLOW.read_text(encoding="utf-8")

    for artifact in (qualification.RECEIPT_ARTIFACT, qualification.ASSETS_ARTIFACT, "qualification-released-"):
        assert f"name: {artifact}" in produced
        assert artifact in json.dumps(_jobs()["jobs"]["qualify"])
    # The archive a recovery continues from is the one every publication leaves behind.
    assert consumed.count(producer.ENVELOPE_ARTIFACT) == 2
    assert f"name: {producer.ENVELOPE_ARTIFACT}" in consumed and f"name={producer.ENVELOPE_ARTIFACT}" in consumed
