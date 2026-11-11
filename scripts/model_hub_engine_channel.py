#!/usr/bin/env python3
"""Plan, sign and publish the signed Model Hub engine channel index.

The index at ``https://dl.avibe.bot/index/model-hub-engine.json`` records which
engine releases installed Avibe may adopt and which were withdrawn. This script
is its only writer.

**Authority is a ledger on the signer host.** It holds the next sequence, the
exact payload last allocated, and every release identity and withdrawal ever
allocated. Each change is planned from the ledger and adds at most one entry.
The published object, the bucket and workflow artifacts are never read as
desired state: an index that was replaced by an older signed one, or deleted,
is put back from the ledger, and cannot make the signer sign older state again.

The ledger must not go backward. This script cannot tell a ledger that was
restored from an old copy from a current one when the published index was also
rolled back or removed, so a lost, damaged or doubtful ledger is an
operational stop, not something to restore and continue from. The comparisons
with the published index below are diagnostics, not that protection.

Steps run in separate jobs, so no job holds two authorities:

- ``authorize`` proves the run is the reviewed workflow on the default branch
  and that the GitHub Environment it is about to use requires approval. With
  ``--require-approval`` it also proves this run was approved.
- ``sign`` allocates the next payload in the ledger, then asks avault for a
  signature. It holds the signing key and no bucket credential.
- ``publish`` holds the bucket credential and no key. It replaces the object
  only if it is still the one it read, then reads it back.

The channel is uncommissioned until Avibe ships a public root and the owner
initializes the ledger: every step stops first. See docs/release.md and
docs/plans/model-hub-engine-channel.md.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import model_hub_engine_channel as channel  # noqa: E402
from core.model_hub_engine_channel import (  # noqa: E402
    ChannelError,
    ChannelPayload,
    ReleaseIdentity,
    Tombstone,
    TrustRoots,
)
from core.model_hub_engine_manifest import (  # noqa: E402
    ASSET_TAG_RE,
    EngineManifestError,
    parse_release_manifest,
    release_revision,
)

TRUST_ROOTS_PATH = REPO_ROOT / "vibe" / "model_hub_runtime" / "engine_channel_roots.json"
REPOSITORY = channel.CHANNEL_REPOSITORY
DEFAULT_BRANCH = "master"
WORKFLOW_PATH = ".github/workflows/model-hub-engine-channel.yml"
INDEX_KEY = "index/model-hub-engine.json"
INDEX_CACHE_CONTROL = "public, max-age=60"
SIGNER_ENVIRONMENT = "model-hub-engine-channel-signer"
WRITER_ENVIRONMENT = "model-hub-engine-channel-writer"
#: Which events may start each operation, and whether the run needs a required
#: reviewer's approval. Every operation is a manual dispatch with approval.
#: ``renew`` takes no input and can only re-sign what the ledger already holds,
#: so it is the only row that standing authorization could ever relax; no other
#: operation becomes reachable by changing it.
OPERATION_POLICY: dict[str, tuple[frozenset[str], bool]] = {
    operation: (frozenset({"workflow_dispatch"}), True)
    for operation in ("release", "promote", "renew", "revoke", "resume")
}

_SIGN_TIMEOUT_SECONDS = 30.0
_SIGN_OUTPUT_LIMIT = 1024
_GITHUB_API = "https://api.github.com"
_USER_AGENT = "avibe-model-hub-engine-channel/1"


class ProducerError(RuntimeError):
    """Raised when a channel change must not be signed or published."""


# ----- planning --------------------------------------------------------------


@dataclass(frozen=True)
class Promote:
    manifest_bytes: bytes
    min_avibe_version: str


@dataclass(frozen=True)
class Renew:
    pass


@dataclass(frozen=True)
class Revoke:
    asset_release_tag: str
    rollback_to: str
    reason: str


Operation = Promote | Renew | Revoke


def _release_entry(manifest_bytes: bytes, min_avibe_version: str) -> dict[str, str]:
    return {
        "min_avibe_version": min_avibe_version,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "manifest_base64": base64.b64encode(manifest_bytes).decode("ascii"),
    }


def _identities(payload: ChannelPayload) -> dict[str, ReleaseIdentity]:
    return {
        release.asset_release_tag: ReleaseIdentity(release.min_avibe_version, release.manifest_sha256)
        for release in payload.releases
    }


def plan_payload(current: ChannelPayload | None, operation: Operation, *, now: datetime, sequence: int) -> bytes:
    """Return the exact payload bytes that follow ``current`` by one change.

    ``current`` is the payload the ledger last allocated, or ``None`` only for
    the first allocation after commissioning. Every release and withdrawal it
    carries is copied byte for byte; the operation adds at most one entry.
    ``sequence`` is allocated by the ledger and must exceed ``current``'s.
    """

    now = now.astimezone(timezone.utc).replace(microsecond=0)
    releases = [_release_entry(item.manifest_bytes, item.min_avibe_version) for item in (current.releases if current else ())]
    revoked = [
        {"asset_release_tag": item.asset_release_tag, "rollback_to": item.rollback_to, "reason": item.reason}
        for item in (current.revoked if current else ())
    ]
    listed = {item.asset_release_tag: item for item in (current.releases if current else ())}
    withdrawn = {item.asset_release_tag for item in (current.revoked if current else ())}

    if current is None:
        if not isinstance(operation, Promote):
            raise ProducerError("the channel has no allocated index to renew or withdraw from")
    else:
        if now < current.issued_at:
            raise ProducerError("this host's clock is behind the index it last allocated")
        if sequence <= current.sequence:
            raise ProducerError("a new index must carry a higher sequence than the last one allocated")

    if isinstance(operation, Promote):
        try:
            spec = parse_release_manifest(operation.manifest_bytes)
            minimum = channel.stable_version(operation.min_avibe_version, "min_avibe_version")
        except (EngineManifestError, ValueError) as exc:
            raise ProducerError(f"the release cannot be promoted: {exc}") from exc
        if minimum < channel.FIRST_CHANNEL_AVIBE_VERSION:
            first = ".".join(str(part) for part in channel.FIRST_CHANNEL_AVIBE_VERSION)
            raise ProducerError(f"no Avibe before {first} reads the channel; the minimum cannot be lower")
        if spec.asset_release_tag in listed:
            raise ProducerError(f"{spec.asset_release_tag} is already promoted; a correction needs a new build revision")
        if spec.asset_release_tag in withdrawn:
            raise ProducerError(f"{spec.asset_release_tag} was withdrawn and cannot be promoted")
        releases.append(_release_entry(operation.manifest_bytes, operation.min_avibe_version))
    elif isinstance(operation, Revoke):
        if ASSET_TAG_RE.fullmatch(operation.asset_release_tag) is None:
            raise ProducerError("the release to withdraw is not an engine release tag")
        if operation.asset_release_tag in withdrawn:
            raise ProducerError(f"{operation.asset_release_tag} is already withdrawn")
        fallback = listed.get(operation.rollback_to)
        if fallback is None or operation.rollback_to in withdrawn:
            raise ProducerError("the rollback target must be a listed release that was not withdrawn")
        if operation.rollback_to == operation.asset_release_tag:
            raise ProducerError("a release cannot roll back to itself")
        target = listed.get(operation.asset_release_tag)
        if target is not None and fallback.minimum > target.minimum:
            raise ProducerError(
                "the rollback target needs a newer Avibe than the withdrawn release, "
                "so machines running the withdrawn release could not use it"
            )
        revoked.append(
            {
                "asset_release_tag": operation.asset_release_tag,
                "rollback_to": operation.rollback_to,
                "reason": operation.reason,
            }
        )

    document = {
        "repository": channel.CHANNEL_REPOSITORY,
        "channel": channel.CHANNEL_NAME,
        "sequence": sequence,
        "issued_at": channel.format_timestamp(now),
        "expires_at": channel.format_timestamp(now + channel.MAX_VALIDITY),
        "releases": releases,
        "revoked": revoked,
    }
    raw = json.dumps(document, separators=(",", ":")).encode("ascii")
    try:
        payload = channel.parse_payload(raw)
        if current is not None:
            channel.check_successor(_identities(current), current.revoked, payload)
    except ChannelError as exc:
        raise ProducerError(f"the planned index is not one a client would accept: {exc}") from exc
    return raw


def standing_releases(current: ChannelPayload | None) -> dict[str, str]:
    """The listed releases that were not withdrawn, by tag, with their manifest digest."""

    withdrawn = {item.asset_release_tag for item in (current.revoked if current else ())}
    return {
        item.asset_release_tag: item.manifest_sha256
        for item in (current.releases if current else ())
        if item.asset_release_tag not in withdrawn
    }


def baseline_manifests(current: ChannelPayload | None, bundled: Sequence[bytes]) -> dict[str, bytes]:
    """Every engine a new candidate must be compared with, by release tag.

    Those are the releases the index lists that were not withdrawn, and the
    engines that the qualifying Avibe releases bundle. A machine may be
    running any of them when the candidate arrives, or return to one.
    """

    withdrawn = {item.asset_release_tag for item in (current.revoked if current else ())}
    manifests = {
        item.asset_release_tag: item.manifest_bytes
        for item in (current.releases if current else ())
        if item.asset_release_tag not in withdrawn
    }
    for manifest_bytes in bundled:
        try:
            tag = parse_release_manifest(manifest_bytes).asset_release_tag
        except EngineManifestError as exc:
            raise ProducerError(f"a bundled engine manifest is invalid: {exc}") from exc
        if tag in withdrawn:
            continue
        if manifests.setdefault(tag, manifest_bytes) != manifest_bytes:
            raise ProducerError(f"{tag} names different bytes in the index and in a released Avibe")
    return manifests


def listed_manifests(current: ChannelPayload | None) -> dict[str, bytes]:
    """The manifest of every release the index lists, withdrawn ones included.

    A withdrawn release keeps its assets, and a rollback target must stay
    downloadable, so the release guard keeps all of them available.
    """

    return {item.asset_release_tag: item.manifest_bytes for item in (current.releases if current else ())}


# ----- trust roots, verification and signing ---------------------------------


def channel_commissioned(path: Path | None = None) -> bool:
    """Whether Avibe ships a channel key. Unreadable roots are an error, not "no"."""

    try:
        return channel.parse_trust_roots((path or TRUST_ROOTS_PATH).read_bytes()).commissioned
    except (OSError, ValueError) as exc:
        raise ProducerError(f"the shipped trust roots cannot be read: {exc}") from exc


def load_trust_roots(path: Path | None = None) -> TrustRoots:
    """The roots this checkout ships. An empty set stops every producer step."""

    try:
        roots = channel.parse_trust_roots((path or TRUST_ROOTS_PATH).read_bytes())
    except (OSError, ValueError) as exc:
        raise ProducerError(f"the shipped trust roots cannot be read: {exc}") from exc
    if not roots.commissioned:
        raise ProducerError(
            "the channel is uncommissioned: Avibe ships no channel key, so nothing may be signed or published"
        )
    return roots


def avault_verifier(avault: str) -> channel.AvaultVerifier:
    return channel.AvaultVerifier(lambda: avault)


def authenticate(raw: bytes, *, roots: TrustRoots, verifier: channel.SignatureVerifier) -> ChannelPayload:
    try:
        _envelope, payload = channel.authenticate(raw, roots=roots, verifier=verifier)
    except ChannelError as exc:
        raise ProducerError(f"the index does not verify against the shipped roots: {exc}") from exc
    return payload


def _avault_sign(avault: str, request: bytes) -> str:
    """Ask avault for one DER signature. The key never leaves avault."""

    # The signing store is this host's own configuration, so its environment
    # passes through unchanged; only the output is bounded here.
    try:
        status, output = channel.run_bounded(
            [avault, "sign"],
            stdin=request,
            timeout=_SIGN_TIMEOUT_SECONDS,
            output_limit=_SIGN_OUTPUT_LIMIT,
            env=os.environ,
            popen_kwargs={},
        )
    except ChannelError as exc:
        raise ProducerError(f"avault sign did not complete: {exc.detail}") from exc
    if status != 0:
        raise ProducerError(f"avault sign failed (exit status {status})")
    try:
        answer = channel.strict_json(output)
        signature = answer["signature"]
        if not isinstance(signature, str):
            raise ValueError("signature is not a string")
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        raise ProducerError("avault sign returned no signature") from exc
    return signature.lower()


def sign_payload(
    payload: bytes,
    *,
    key_id: str,
    key_name: str,
    key_envelope: Mapping[str, Any],
    roots: TrustRoots,
    verifier: channel.SignatureVerifier,
    sign: Callable[[bytes], str],
) -> bytes:
    """Sign ``payload`` and return an envelope that a client would accept.

    The result is checked the way a client checks it, against the shipped
    root for ``key_id``. A signer holding any other key therefore produces
    nothing publishable.
    """

    if roots.find(key_id) is None:
        raise ProducerError(f"{key_id} is not a key Avibe ships")
    try:
        channel.parse_payload(payload)
    except ChannelError as exc:
        raise ProducerError(f"refusing to sign a payload no client accepts: {exc}") from exc
    request = json.dumps(
        {
            "name": key_name,
            "key_envelope": dict(key_envelope),
            "digest": channel.channel_digest(payload),
            "scheme": channel.SIGNATURE_SCHEME,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    envelope = channel.encode_envelope(key_id=key_id, payload=payload, signature=sign(request))
    try:
        parsed, _payload = channel.authenticate(envelope, roots=roots, verifier=verifier)
    except ChannelError as exc:
        raise ProducerError(f"the new signature does not verify against the shipped root: {exc}") from exc
    if parsed.payload != payload:
        raise ProducerError("the envelope does not carry the planned payload bytes")
    return envelope


# ----- the signer's ledger ---------------------------------------------------

_LEDGER_SCHEMA_VERSION = 1
_LEDGER_FIELDS = frozenset(
    {"schema_version", "repository", "channel", "key_id", "next_sequence", "allocation", "releases", "revoked"}
)


@dataclass(frozen=True)
class Allocation:
    """One sequence, the exact payload it was spent on, and its envelope once signed."""

    sequence: int
    payload: bytes
    envelope: bytes | None


@dataclass(frozen=True)
class LedgerState:
    key_id: str
    next_sequence: int
    allocation: Allocation | None
    #: Every release ever allocated, listed now or not. Its bytes and minimum never change.
    releases: Mapping[str, ReleaseIdentity]
    #: Every withdrawal ever allocated. None is ever dropped or changed.
    revoked: tuple[Tombstone, ...]

    @property
    def payload(self) -> ChannelPayload | None:
        return channel.parse_payload(self.allocation.payload) if self.allocation is not None else None


def _durable_write(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    """Write ``data`` so that it is either wholly there after a crash or not at all.

    The bytes are flushed to a temporary file, moved into place, and the
    directory entry is flushed too. A platform that cannot flush a directory
    cannot make that promise, and this refuses rather than pretend to.
    ``exclusive`` never replaces an existing file.
    """

    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        raise ProducerError("this platform cannot flush a directory; the ledger needs that durability")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            os.link(temporary, path)
            os.unlink(temporary)
        else:
            os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    directory = os.open(path.parent, directory_flag)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class PublisherLedger:
    """The signer host's durable record of what the channel has allocated.

    ``state.json`` is the authority: the next sequence, the exact payload the
    last sequence was spent on, its envelope once signed, and the permanent
    sets. ``journal/`` keeps a write-once copy of every signed envelope for
    audit; nothing is read back from it as state. Every caller holds the
    ledger's lock for its whole step, so allocations are strictly serialized.

    Nothing here creates or repairs a ledger in passing. A directory with no
    state, or with state that does not check out, stops every routine
    operation; only the owner's one-time ``initialize`` writes a first state,
    and only into a directory that has never held one.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.state_path = directory / "state.json"
        self.journal_dir = directory / "journal"
        self.lock_path = directory / "ledger.lock"

    def require_durable_location(self, environ: Mapping[str, str] = os.environ) -> None:
        """Refuse a ledger in a place a runner wipes between jobs."""

        if not self.directory.is_absolute():
            raise ProducerError("the ledger path must be absolute")
        resolved = self.directory.resolve()
        for name in ("RUNNER_TEMP", "GITHUB_WORKSPACE", "RUNNER_WORKSPACE"):
            value = environ.get(name)
            if value and (resolved == Path(value).resolve() or Path(value).resolve() in resolved.parents):
                raise ProducerError(f"the ledger must outlive the job; {name} does not")

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        from storage.lock import MigrationFileLock

        if not self.directory.is_dir():
            raise ProducerError("the publisher ledger does not exist; it is created once, by owner commissioning")
        with MigrationFileLock(self.lock_path, timeout_seconds=120.0):
            yield

    def _journal_path(self, sequence: int) -> Path:
        return self.journal_dir / f"{sequence:012d}.json"

    def load(self, roots: TrustRoots) -> LedgerState:
        """Read the ledger, or stop. Call with the lock held."""

        try:
            raw = self.state_path.read_bytes()
        except FileNotFoundError:
            raise ProducerError(
                "the publisher ledger has no state; it is created once, by owner commissioning, and never recreated"
            ) from None
        except OSError as exc:
            raise ProducerError(f"the publisher ledger cannot be read ({type(exc).__name__})") from exc
        try:
            document = channel.strict_json(raw)
            if not isinstance(document, dict) or set(document) != _LEDGER_FIELDS:
                raise ValueError("unexpected fields")
            if type(document["schema_version"]) is not int or document["schema_version"] != _LEDGER_SCHEMA_VERSION:
                raise ValueError("unsupported schema_version")
            if document["repository"] != channel.CHANNEL_REPOSITORY or document["channel"] != channel.CHANNEL_NAME:
                raise ValueError("the ledger belongs to another channel")
            key_id, next_sequence = document["key_id"], document["next_sequence"]
            if not isinstance(key_id, str) or roots.find(key_id) is None:
                raise ValueError("the ledger is bound to a key Avibe does not ship")
            if type(next_sequence) is not int or not 1 <= next_sequence <= channel.MAX_SEQUENCE:
                raise ValueError("next_sequence is invalid")
            releases = {
                tag: ReleaseIdentity(entry["min_avibe_version"], entry["manifest_sha256"])
                for tag, entry in document["releases"].items()
            }
            for tag, identity in releases.items():
                release_revision(tag)
                channel.stable_version(identity.min_avibe_version)
                if not isinstance(identity.manifest_sha256, str) or len(identity.manifest_sha256) != 64:
                    raise ValueError("a recorded release identity is invalid")
            revoked = tuple(
                Tombstone(item["asset_release_tag"], item["rollback_to"], item["reason"]) for item in document["revoked"]
            )
            allocation = None
            if document["allocation"] is not None:
                entry = document["allocation"]
                if not isinstance(entry, dict) or set(entry) != {"sequence", "payload", "envelope"}:
                    raise ValueError("allocation has unexpected fields")
                payload_bytes = base64.b64decode(entry["payload"], validate=True)
                envelope_bytes = (
                    base64.b64decode(entry["envelope"], validate=True) if entry["envelope"] is not None else None
                )
                payload = channel.parse_payload(payload_bytes)
                if not payload.sequence == entry["sequence"] == next_sequence - 1:
                    raise ValueError("the allocation does not match the sequence counter")
                if payload.revoked != revoked or any(
                    releases.get(item.asset_release_tag) != ReleaseIdentity(item.min_avibe_version, item.manifest_sha256)
                    for item in payload.releases
                ):
                    raise ValueError("the allocation disagrees with the permanent record")
                if envelope_bytes is not None:
                    envelope = channel.parse_envelope(envelope_bytes)
                    if envelope.payload != payload_bytes or envelope.key_id != key_id:
                        raise ValueError("the envelope is not for the allocated payload")
                allocation = Allocation(entry["sequence"], payload_bytes, envelope_bytes)
            elif releases or revoked:
                raise ValueError("a ledger with history has no allocation")
        except (ChannelError, EngineManifestError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ProducerError(f"the publisher ledger is damaged and nothing will be signed from it: {exc}") from exc
        return LedgerState(key_id, next_sequence, allocation, releases, revoked)

    def _commit(self, state: LedgerState) -> None:
        allocation = state.allocation
        document = {
            "schema_version": _LEDGER_SCHEMA_VERSION,
            "repository": channel.CHANNEL_REPOSITORY,
            "channel": channel.CHANNEL_NAME,
            "key_id": state.key_id,
            "next_sequence": state.next_sequence,
            "allocation": None
            if allocation is None
            else {
                "sequence": allocation.sequence,
                "payload": base64.b64encode(allocation.payload).decode("ascii"),
                "envelope": None if allocation.envelope is None else base64.b64encode(allocation.envelope).decode("ascii"),
            },
            "releases": {
                tag: {"min_avibe_version": identity.min_avibe_version, "manifest_sha256": identity.manifest_sha256}
                for tag, identity in sorted(state.releases.items())
            },
            "revoked": [
                {"asset_release_tag": item.asset_release_tag, "rollback_to": item.rollback_to, "reason": item.reason}
                for item in state.revoked
            ],
        }
        _durable_write(self.state_path, (json.dumps(document, sort_keys=True) + "\n").encode("ascii"))

    def initialize(self, *, key_id: str, next_sequence: int, roots: TrustRoots) -> None:
        """Write the first state. One time, by the owner, for a channel that never signed anything.

        An empty directory is not evidence that this key never signed an
        index. That knowledge is the owner's; this only refuses the cases it
        can see: a directory that already holds state or journal entries, a
        key Avibe does not ship, and a sequence below the shipped floor.
        """

        if roots.find(key_id) is None:
            raise ProducerError(f"{key_id} is not a key Avibe ships")
        if type(next_sequence) is not int or not max(1, roots.sequence_floor) <= next_sequence <= channel.MAX_SEQUENCE:
            raise ProducerError("the first sequence must be at least 1 and at least the shipped sequence floor")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        with self.locked():
            if self.state_path.exists() or (self.journal_dir.exists() and any(self.journal_dir.iterdir())):
                raise ProducerError("this ledger already holds state or history; a ledger is initialized once")
            self.journal_dir.mkdir(mode=0o700, exist_ok=True)
            self._commit(LedgerState(key_id, next_sequence, None, {}, ()))

    def allocate(self, operation: Operation, *, now: datetime, roots: TrustRoots) -> LedgerState:
        """Spend the next sequence on the payload that applies ``operation``. Lock held.

        The payload is committed before anything is signed. From that commit
        the sequence belongs to that payload, whatever happens next. Planning
        starts from the ledger's own last allocation, signed or not, so a
        change that was allocated but never published is carried forward.
        """

        state = self.load(roots)
        sequence = max(state.next_sequence, roots.sequence_floor, 1)
        raw = plan_payload(state.payload, operation, now=now, sequence=sequence)
        payload = channel.parse_payload(raw)
        try:
            channel.check_successor(state.releases, state.revoked, payload)
        except ChannelError as exc:
            raise ProducerError(f"the planned index rewrites what this ledger already allocated: {exc}") from exc
        allocated = LedgerState(
            key_id=state.key_id,
            next_sequence=sequence + 1,
            allocation=Allocation(sequence, raw, None),
            releases={**state.releases, **_identities(payload)},
            revoked=payload.revoked,
        )
        self._commit(allocated)
        return allocated

    def record_envelope(self, state: LedgerState, envelope: bytes) -> LedgerState:
        """Commit the signature of the current allocation, then its journal copy. Lock held."""

        allocation = state.allocation
        assert allocation is not None
        if channel.parse_envelope(envelope).payload != allocation.payload:
            raise ProducerError("the envelope is not for the allocated payload")
        signed = LedgerState(
            state.key_id, state.next_sequence, Allocation(allocation.sequence, allocation.payload, envelope),
            state.releases, state.revoked,
        )
        self._commit(signed)
        self.journal(signed)
        return signed

    def journal(self, state: LedgerState) -> None:
        """Make sure the journal holds the current signed envelope, exactly. Lock held."""

        allocation = state.allocation
        if allocation is None or allocation.envelope is None:
            return
        path = self._journal_path(allocation.sequence)
        try:
            existing = path.read_bytes()
        except FileNotFoundError:
            self.journal_dir.mkdir(mode=0o700, exist_ok=True)
            _durable_write(path, allocation.envelope, exclusive=True)
            return
        if existing != allocation.envelope:
            raise ProducerError(
                f"the journal holds a different envelope for sequence {allocation.sequence}; the ledger is damaged"
            )


def published_index_problems(published: bytes | None, state: LedgerState, *, roots: TrustRoots,
                             verifier: channel.SignatureVerifier) -> list[str]:
    """Signs that the ledger is behind what was published. A diagnostic, not a guarantee.

    An index this key signed with a sequence the ledger has not reached, or
    with the ledger's sequence and other bytes, means the ledger went backward
    or two ledgers exist. A missing, unreadable or older published index says
    nothing: a rollback that also rolled back the published index looks the
    same as no rollback, and this cannot see it.
    """

    if published is None:
        return []
    try:
        _envelope, payload = channel.authenticate(published, roots=roots, verifier=verifier)
    except ChannelError:
        return []
    allocation = state.allocation
    if allocation is None or payload.sequence > allocation.sequence:
        return [f"the published index is at sequence {payload.sequence}, which this ledger has not allocated"]
    if payload.sequence == allocation.sequence and payload.raw != allocation.payload:
        return [f"the published index carries sequence {payload.sequence} with a different payload than this ledger"]
    return []


def sign_from_ledger(
    ledger: PublisherLedger,
    operation: Operation | None,
    *,
    now: datetime,
    roots: TrustRoots,
    verifier: channel.SignatureVerifier,
    sign: Callable[[bytes], str],
    key_name: str,
    key_envelope: Mapping[str, Any],
    published: bytes | None,
    qualified_baselines: Mapping[str, str] | None = None,
) -> bytes:
    """Allocate ``operation`` (or resume the current allocation) and return its envelope.

    ``operation`` ``None`` resumes: it returns the envelope of the current
    allocation byte for byte, signing that same committed payload first if it
    was never signed. Nothing is returned before it is committed to the
    ledger, so a failure anywhere leaves either no allocation or one that
    ``resume`` completes without a second payload for the same sequence.
    """

    with ledger.locked():
        state = ledger.load(roots)
        problems = published_index_problems(published, state, roots=roots, verifier=verifier)
        if problems:
            raise ProducerError("the ledger is behind the published index; stop and investigate: " + "; ".join(problems))
        if operation is None:
            if state.allocation is None:
                raise ProducerError("nothing was allocated yet; there is nothing to resume")
            if now >= channel.parse_payload(state.allocation.payload).expires_at:
                raise ProducerError("the allocated index has expired; renew instead of resuming it")
        else:
            if isinstance(operation, Promote):
                standing = standing_releases(state.payload)
                unqualified = sorted(
                    tag for tag, digest in standing.items() if (qualified_baselines or {}).get(tag) != digest
                )
                if unqualified:
                    raise ProducerError(
                        "the candidate was not qualified against every release this ledger lists: "
                        + ", ".join(unqualified)
                    )
            state = ledger.allocate(operation, now=now, roots=roots)
        allocation = state.allocation
        assert allocation is not None
        if allocation.envelope is None:
            envelope = sign_payload(
                allocation.payload, key_id=state.key_id, key_name=key_name, key_envelope=key_envelope,
                roots=roots, verifier=verifier, sign=sign,
            )
            state = ledger.record_envelope(state, envelope)
        else:
            # Already signed: prove it still verifies, and repair a missing journal copy.
            authenticate(allocation.envelope, roots=roots, verifier=verifier)
            ledger.journal(state)
        assert state.allocation is not None and state.allocation.envelope is not None
        return state.allocation.envelope


# ----- run and environment protection ----------------------------------------


def github_api(path: str, *, token: str | None) -> Any:
    """GET one GitHub REST resource; ``None`` when it does not exist."""

    headers = {"Accept": "application/vnd.github+json", "User-Agent": _USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{_GITHUB_API}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise ProducerError(f"GitHub answered {exc.code} for {path}") from exc
    except (OSError, ValueError) as exc:
        raise ProducerError(f"GitHub could not be read for {path}: {type(exc).__name__}") from exc


def run_context_problems(environ: Mapping[str, str], *, events: frozenset[str] = frozenset({"workflow_dispatch"})) -> list[str]:
    """Why this run is not the reviewed finalizer on the default branch."""

    expected = {
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF": f"refs/heads/{DEFAULT_BRANCH}",
        "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/{WORKFLOW_PATH}@refs/heads/{DEFAULT_BRANCH}",
    }
    problems = [
        f"{name} is {environ.get(name)!r}, not {value!r}" for name, value in expected.items() if environ.get(name) != value
    ]
    if environ.get("GITHUB_EVENT_NAME") not in events:
        problems.append(f"the run was started by {environ.get('GITHUB_EVENT_NAME')!r}, not by {sorted(events)}")
    if not environ.get("GITHUB_RUN_ID", "").isdigit():
        problems.append("the run has no numeric GITHUB_RUN_ID")
    return problems


def environment_protection_problems(environment: Any, branch_policies: Any) -> tuple[list[str], frozenset[str]]:
    """Why a GitHub Environment does not gate a job on approval, and who may approve.

    A job that names an environment nobody configured runs unprotected, and
    GitHub creates the environment on the spot. So protection is established
    from the API, never from the name: the environment must exist, require
    review by named users, and admit only the default branch.
    """

    if not isinstance(environment, dict):
        return ["the environment does not exist or cannot be read"], frozenset()
    problems: list[str] = []
    reviewers: set[str] = set()
    rules = environment.get("protection_rules")
    for rule in rules if isinstance(rules, list) else []:
        if isinstance(rule, dict) and rule.get("type") == "required_reviewers":
            for reviewer in rule.get("reviewers") or []:
                login = (reviewer.get("reviewer") or {}).get("login") if isinstance(reviewer, dict) else None
                if isinstance(reviewer, dict) and reviewer.get("type") == "User" and isinstance(login, str) and login:
                    reviewers.add(login)
    if not reviewers:
        problems.append("the environment has no required reviewer who is a named user")
    policy = environment.get("deployment_branch_policy")
    if not isinstance(policy, dict) or policy.get("custom_branch_policies") is not True:
        problems.append("the environment does not restrict which branches may deploy")
    else:
        listed = branch_policies.get("branch_policies") if isinstance(branch_policies, dict) else None
        names = [
            (item.get("name"), item.get("type", "branch")) for item in listed if isinstance(item, dict)
        ] if isinstance(listed, list) else None
        if names != [(DEFAULT_BRANCH, "branch")]:
            problems.append(f"the environment must admit exactly the {DEFAULT_BRANCH} branch")
    return problems, frozenset(reviewers)


def approval_problems(approvals: Any, *, environment: str, reviewers: frozenset[str]) -> list[str]:
    """Why this run has no recorded approval for ``environment`` by a required reviewer."""

    if not isinstance(approvals, list):
        return ["the run's approvals cannot be read"]
    for approval in approvals:
        if not isinstance(approval, dict) or approval.get("state") != "approved":
            continue
        names = {item.get("name") for item in approval.get("environments") or [] if isinstance(item, dict)}
        login = (approval.get("user") or {}).get("login")
        if environment in names and login in reviewers:
            return []
    return [f"no required reviewer approved this run for {environment}"]


def authorize(
    environment: str,
    *,
    operation: str,
    environ: Mapping[str, str],
    api: Callable[[str], Any],
    require_approval: bool,
) -> None:
    """Stop unless this run may use ``environment`` for a privileged step of ``operation``."""

    load_trust_roots()
    policy = OPERATION_POLICY.get(operation)
    if policy is None:
        raise ProducerError(f"{operation!r} is not an operation of the channel workflow")
    events, needs_approval = policy
    problems = run_context_problems(environ, events=events)
    base = f"/repos/{REPOSITORY}/environments/{environment}"
    protection, reviewers = environment_protection_problems(api(base), api(f"{base}/deployment-branch-policies"))
    problems += protection
    if require_approval and needs_approval and not protection:
        run_id = environ.get("GITHUB_RUN_ID", "")
        problems += approval_problems(
            api(f"/repos/{REPOSITORY}/actions/runs/{run_id}/approvals"), environment=environment, reviewers=reviewers
        )
    if problems:
        raise ProducerError(f"{environment} is not authorized: " + "; ".join(problems))


# ----- the published object --------------------------------------------------


def fetch_public_index(url: str = channel.CHANNEL_INDEX_URL) -> bytes | None:
    """The index as clients download it; ``None`` when it was never published."""

    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(channel.MAX_ENVELOPE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise ProducerError(f"the published index answered {exc.code}") from exc
    except OSError as exc:
        raise ProducerError(f"the published index cannot be read: {type(exc).__name__}") from exc
    if len(body) > channel.MAX_ENVELOPE_BYTES:
        raise ProducerError("the published index is larger than the channel allows")
    return body


class IndexBucket:
    """The one object this script writes, through the bucket's S3 API."""

    def __init__(self, name: str, endpoint_url: str, run: Callable[[Sequence[str]], subprocess.CompletedProcess]) -> None:
        self.name = name
        self.endpoint_url = endpoint_url
        self._run = run

    def _s3api(self, *arguments: str) -> subprocess.CompletedProcess:
        return self._run(["aws", "s3api", *arguments, "--bucket", self.name, "--endpoint-url", self.endpoint_url])

    def read(self) -> tuple[bytes, str] | None:
        """The stored index and its ETag; ``None`` when the key is absent."""

        listing = self._s3api("list-objects-v2", "--prefix", INDEX_KEY, "--output", "json")
        if listing.returncode != 0:
            raise ProducerError("the bucket could not be listed")
        contents = (json.loads(listing.stdout) if listing.stdout.strip() else {}).get("Contents") or []
        if not any(item.get("Key") == INDEX_KEY for item in contents):
            return None
        with tempfile.TemporaryDirectory(prefix="avibe-engine-channel-") as temporary:
            path = Path(temporary) / "index"
            fetched = self._s3api("get-object", "--key", INDEX_KEY, "--output", "json", str(path))
            if fetched.returncode != 0:
                raise ProducerError("the stored index could not be read")
            etag = json.loads(fetched.stdout).get("ETag")
            if not isinstance(etag, str) or not etag:
                raise ProducerError("the stored index has no ETag to replace it against")
            return path.read_bytes(), etag

    def replace(self, body: bytes, *, etag: str | None) -> None:
        """Write the index only over the object that was read, or only if none exists."""

        condition = ["--if-match", etag] if etag is not None else ["--if-none-match", "*"]
        with tempfile.TemporaryDirectory(prefix="avibe-engine-channel-") as temporary:
            path = Path(temporary) / "index"
            path.write_bytes(body)
            written = self._s3api(
                "put-object", "--key", INDEX_KEY, "--body", str(path),
                "--content-type", "application/json", "--cache-control", INDEX_CACHE_CONTROL,
                "--metadata", f"sha256={hashlib.sha256(body).hexdigest()}",
                *condition,
            )
        if written.returncode != 0:
            raise ProducerError(
                "the index was not replaced: it changed since it was read, or the bucket refused the write"
            )


def publish(
    envelope: bytes,
    *,
    bucket: IndexBucket,
    roots: TrustRoots,
    verifier: channel.SignatureVerifier,
    fetch_public: Callable[[], bytes | None] = fetch_public_index,
    sleep: Callable[[float], None] = time.sleep,
    public_wait_seconds: float = 300.0,
) -> ChannelPayload:
    """Put ``envelope`` in the bucket and prove clients receive exactly it.

    The envelope comes from the signer's ledger; what the bucket holds is not
    desired state. An object that is missing, unreadable, or an older index
    signed by this key is replaced. The write succeeds only over the object
    that was read, so two writers cannot both win. Publishing the same
    envelope again writes nothing and only repeats the read-back.

    One thing stops it: a stored index signed by this key that the new one
    does not strictly follow, by sequence and by content. That means the
    signer's ledger is behind, and nothing is overwritten.
    """

    successor = authenticate(envelope, roots=roots, verifier=verifier)
    stored = bucket.read()
    if stored is None or stored[0] != envelope:
        if stored is not None:
            try:
                _stored_envelope, current = channel.authenticate(stored[0], roots=roots, verifier=verifier)
            except ChannelError:
                current = None
            if current is not None:
                if current.sequence >= successor.sequence:
                    raise ProducerError(
                        f"the stored index is at sequence {current.sequence}, not older than the one to publish; "
                        "the signer's ledger is behind"
                    )
                try:
                    channel.check_successor(_identities(current), current.revoked, successor)
                except ChannelError as exc:
                    raise ProducerError(f"the index to publish rewrites the stored one: {exc}") from exc
        bucket.replace(envelope, etag=stored[1] if stored is not None else None)
    written = bucket.read()
    if written is None or written[0] != envelope:
        raise ProducerError("the bucket does not hold the envelope that was just written")

    # Clients read through a cache that may serve the previous object for its
    # max-age. Success means the public URL serves exactly these bytes.
    deadline = time.monotonic() + public_wait_seconds
    while True:
        try:
            served = fetch_public()
        except ProducerError:
            served = None
        if served == envelope:
            return successor
        if time.monotonic() >= deadline:
            raise ProducerError(
                "the bucket holds the new index, but the public URL did not serve it in time; "
                "do not report this change as published"
            )
        sleep(10.0)


# ----- command line ----------------------------------------------------------


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(command), check=False, capture_output=True, text=True)
    except OSError as exc:
        raise ProducerError(f"unable to run {command[0]}: {exc}") from exc


def _operation(args: argparse.Namespace) -> Operation | None:
    if args.operation == "resume":
        return None
    if args.operation == "renew":
        return Renew()
    if args.operation == "revoke":
        if not (args.tag and args.rollback_to and args.reason):
            raise ProducerError("a withdrawal needs the release, its rollback target and a public reason")
        return Revoke(args.tag, args.rollback_to, args.reason)
    if not args.candidate_dir:
        raise ProducerError("a promotion needs the reviewed candidate record")
    try:
        from scripts.model_hub_engine_qualification import load_candidate
    except ModuleNotFoundError as exc:
        if exc.name != "scripts":
            raise
        from model_hub_engine_qualification import load_candidate

    candidate = load_candidate(args.candidate_dir)
    if args.tag and args.tag != candidate.asset_release_tag:
        raise ProducerError("the candidate record is not the release that was requested")
    return Promote(candidate.manifest_bytes, candidate.min_avibe_version)


def _qualified_baselines(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        baselines = channel.strict_json(raw.encode("utf-8"))
    except ValueError as exc:
        raise ProducerError(f"the qualified baselines cannot be read: {exc}") from exc
    if not isinstance(baselines, dict) or not all(isinstance(value, str) for value in baselines.values()):
        raise ProducerError("the qualified baselines map a release tag to a manifest digest")
    return baselines


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    authorize_parser = commands.add_parser("authorize", help="Prove this run may use a protected environment.")
    authorize_parser.add_argument("--environment", required=True, choices=[SIGNER_ENVIRONMENT, WRITER_ENVIRONMENT])
    authorize_parser.add_argument("--operation", required=True)
    authorize_parser.add_argument("--require-approval", action="store_true")

    # Owner commissioning, run once by hand on the signer host. The channel
    # workflow has no operation that reaches it.
    initialize_parser = commands.add_parser(
        "initialize-ledger", help="Owner commissioning: create the signer's ledger for a channel that never signed.",
    )
    initialize_parser.add_argument("--ledger", type=Path, required=True)
    initialize_parser.add_argument("--key-id", required=True)
    initialize_parser.add_argument("--first-sequence", type=int, required=True)

    sign_parser = commands.add_parser("sign", help="Allocate the next index in the ledger and sign it with avault.")
    sign_parser.add_argument("--operation", required=True, choices=["promote", "renew", "revoke", "resume"])
    sign_parser.add_argument("--ledger", type=Path, required=True)
    sign_parser.add_argument("--tag")
    sign_parser.add_argument("--rollback-to")
    sign_parser.add_argument("--reason")
    sign_parser.add_argument("--candidate-dir", type=Path)
    sign_parser.add_argument("--qualified-baselines", help="JSON object the qualify job produced: tag to digest")
    sign_parser.add_argument("--avault", required=True)
    sign_parser.add_argument("--key-name", required=True)
    sign_parser.add_argument("--key-envelope", type=Path, required=True)
    sign_parser.add_argument("--output", type=Path, required=True)

    baselines_parser = commands.add_parser("baselines", help="List the engines a candidate must be compared with.")
    baselines_parser.add_argument("--avault")
    baselines_parser.add_argument("--bundled", type=Path, action="append", default=[])
    baselines_parser.add_argument("--output-dir", type=Path, required=True)

    commands.add_parser("commissioned", help="Print whether Avibe ships a channel key.")

    listed_parser = commands.add_parser("listed", help="Write the manifest of every release the index lists.")
    listed_parser.add_argument("--avault")
    listed_parser.add_argument("--output-dir", type=Path, required=True)

    publish_parser = commands.add_parser("publish", help="Replace the stored index and read it back.")
    publish_parser.add_argument("--envelope", type=Path, required=True)
    publish_parser.add_argument("--avault", required=True)
    publish_parser.add_argument("--bucket", required=True)
    publish_parser.add_argument("--endpoint-url", required=True)
    return parser


def _published_payload(avault: str | None) -> ChannelPayload | None:
    """The index clients are served, authenticated. ``None`` when there is none to read.

    Used only where the published index is evidence about availability or
    qualification, never as the state a new index is signed from.
    """

    if not channel_commissioned():
        return None
    published = fetch_public_index()
    if published is None:
        return None
    if not avault:
        raise ProducerError("reading the published index needs the avault verifier")
    return authenticate(published, roots=load_trust_roots(), verifier=avault_verifier(avault))


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "authorize":
            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            authorize(
                args.environment,
                operation=args.operation,
                environ=os.environ,
                api=lambda path: github_api(path, token=token),
                require_approval=args.require_approval,
            )
            print(json.dumps({"ok": True, "environment": args.environment}))
            return 0
        if args.command == "commissioned":
            print("true" if channel_commissioned() else "false")
            return 0
        if args.command == "listed":
            # Reads only. An uncommissioned channel, or one that never
            # published, lists nothing; an index that does not verify is an error.
            args.output_dir.mkdir(parents=True, exist_ok=True)
            manifests = listed_manifests(_published_payload(args.avault))
            for tag, manifest_bytes in manifests.items():
                (args.output_dir / f"{tag}.json").write_bytes(manifest_bytes)
            print(json.dumps({"ok": True, "releases": sorted(manifests)}))
            return 0
        if args.command == "baselines":
            # Evidence for qualification, not state: the signer checks it
            # against its own ledger before it promotes anything.
            manifests = baseline_manifests(_published_payload(args.avault), [path.read_bytes() for path in args.bundled])
            (args.output_dir / "manifests").mkdir(parents=True, exist_ok=True)
            for tag, manifest_bytes in manifests.items():
                (args.output_dir / "manifests" / f"{tag}.json").write_bytes(manifest_bytes)
            digests = {tag: hashlib.sha256(manifest_bytes).hexdigest() for tag, manifest_bytes in sorted(manifests.items())}
            (args.output_dir / "baselines.json").write_text(json.dumps(digests, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"ok": True, "baselines": digests}))
            return 0

        roots = load_trust_roots()
        if args.command == "initialize-ledger":
            ledger = PublisherLedger(args.ledger)
            ledger.require_durable_location()
            ledger.initialize(key_id=args.key_id, next_sequence=args.first_sequence, roots=roots)
            print(json.dumps({"ok": True, "next_sequence": args.first_sequence}))
            return 0

        verifier = avault_verifier(args.avault)
        if args.command == "sign":
            ledger = PublisherLedger(args.ledger)
            ledger.require_durable_location()
            try:
                key_envelope = channel.strict_json(args.key_envelope.read_bytes())
            except (OSError, ValueError) as exc:
                raise ProducerError(f"the sealed channel key cannot be read: {type(exc).__name__}") from exc
            if not isinstance(key_envelope, dict):
                raise ProducerError("the sealed channel key is not an avault envelope")
            envelope = sign_from_ledger(
                ledger,
                _operation(args),
                now=datetime.now(timezone.utc),
                roots=roots,
                verifier=verifier,
                sign=lambda request: _avault_sign(args.avault, request),
                key_name=args.key_name,
                key_envelope=key_envelope,
                published=fetch_public_index(),
                qualified_baselines=_qualified_baselines(args.qualified_baselines),
            )
            args.output.write_bytes(envelope)
            signed = channel.parse_payload(channel.parse_envelope(envelope).payload)
            print(json.dumps({"ok": True, "sequence": signed.sequence, "payload_sha256": signed.sha256}))
            return 0

        published_payload = publish(
            args.envelope.read_bytes(),
            bucket=IndexBucket(args.bucket, args.endpoint_url, _run_command),
            roots=roots,
            verifier=verifier,
        )
        print(
            json.dumps(
                {"ok": True, "sequence": published_payload.sequence, "payload_sha256": published_payload.sha256}
            )
        )
        return 0
    except ProducerError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
