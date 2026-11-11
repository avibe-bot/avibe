"""The signed Model Hub engine channel: contract, verification, and selection.

``https://dl.avibe.bot/index/model-hub-engine.json`` is a signed envelope
around an exact payload. The payload lists engine releases, each with the
minimum Avibe version that may run it and the exact bytes of its release
manifest, and the releases that were withdrawn. This module turns downloaded
bytes into a decision, in this order, and stops at the first step that fails:

1. parse the envelope and find its ``key_id`` among the roots Avibe ships;
2. check the signature over ``SHA256(domain || payload)`` with avault's
   keyless ``verify``, with the algorithm and domain fixed here;
3. only then parse the payload, and every manifest inside it, strictly;
4. compare it with the newest payload this machine accepted before, so an
   older or altered index cannot replace a newer one;
5. select a release for this Avibe version, host, and installed engine.

Nothing here downloads, installs, or starts an engine, and nothing the index
says can choose the key, the algorithm, or a digest that was not signed.

An index has no expiry. A machine follows the highest sequence it has
accepted for as long as that is the highest it has, and no clock is read
anywhere in acceptance or selection. So a machine that never receives a newer
index keeps acting on the one it has, withdrawals it has not seen included.
That is an intended limit of the design, not something to close with a time
gate. Design: docs/plans/model-hub-engine-channel.md.

Standard library only at import time. The release scripts load this module on
a runner that has no Avibe dependencies installed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from core.model_hub_engine_manifest import (
    ASSET_TAG_RE,
    SHA256_RE,
    EngineManifestError,
    ReleaseSpec,
    parse_release_manifest,
    release_revision,
)

CHANNEL_INDEX_URL = "https://dl.avibe.bot/index/model-hub-engine.json"
CHANNEL_REPOSITORY = "avibe-bot/avibe"
CHANNEL_NAME = "stable"
#: Signed bytes are ``SHA256(SIGNATURE_DOMAIN || payload)``. Fixed by the client.
SIGNATURE_DOMAIN = b"avibe:model-hub-engine-channel:v1\n"
SIGNATURE_SCHEME = "ecdsa-secp256k1-der"
ENVELOPE_SCHEMA_VERSION = 1
#: The first avault release with the keyless ``verify`` verb.
VERIFIER_MIN_VERSION = (0, 1, 10)
#: The first Avibe release that reads the channel. No engine may claim less.
FIRST_CHANNEL_AVIBE_VERSION = (3, 2, 6)

MAX_MANIFEST_BYTES = 16 * 1024
MAX_RELEASES = 64
MAX_REVOKED = 256
MAX_REASON_CHARS = 512
MAX_PAYLOAD_BYTES = 1536 * 1024
MAX_ENVELOPE_BYTES = 2 * 1024 * 1024 + 4096
MAX_TRUST_ROOTS = 4
MAX_SEQUENCE = 2**53 - 1

_VERIFY_REQUEST_LIMIT = 4096
_VERIFY_OUTPUT_LIMIT = 256
_KEY_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_PUBLIC_KEY_RE = re.compile(r"0[23][0-9a-f]{64}")
_SIGNATURE_RE = re.compile(r"(?:[0-9a-f]{2}){8,72}")
_STABLE_VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_AVAULT_VERSION_RE = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)([-+].+)?")
_STATE_SCHEMA_VERSION = 1


class ChannelError(Exception):
    """Why the channel could not be trusted. ``reason`` is a stable token."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


# ----- strict JSON -----------------------------------------------------------


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"repeated field {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-JSON number {name}")


def strict_json(raw: bytes) -> Any:
    """Parse UTF-8 JSON, refusing a repeated field or a non-JSON number."""

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except RecursionError as exc:
        raise ValueError("nesting is too deep") from exc


def _object(value: Any, fields: frozenset[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{context} must be an object with exactly {sorted(fields)}")
    return value


def _integer(value: Any, context: str, *, minimum: int, maximum: int = MAX_SEQUENCE) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{context} must be an integer from {minimum} to {maximum}")
    return value


def _canonical_base64(value: Any, context: str, *, limit: int) -> bytes:
    """Decode standard padded base64 that has exactly one encoding of its bytes."""

    if not isinstance(value, str) or len(value) > (limit + 2) // 3 * 4:
        raise ValueError(f"{context} is not bounded base64")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ValueError(f"{context} is not base64") from exc
    if len(decoded) > limit or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError(f"{context} is not canonical bounded base64")
    return decoded


def _timestamp(value: Any, context: str) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise ValueError(f"{context} must be a UTC time such as 2026-11-10T06:00:00Z")
    try:
        return datetime.strptime(value, _TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"{context} is not a real time") from exc


def format_timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT)


def stable_version(value: Any, context: str = "version") -> tuple[int, int, int]:
    """Parse a stable ``X.Y.Z`` Avibe version; anything else is not a minimum."""

    match = _STABLE_VERSION_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"{context} must be a stable X.Y.Z version")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


# ----- trust roots -----------------------------------------------------------


@dataclass(frozen=True)
class TrustRoot:
    key_id: str
    #: Lowercase hex of the 33-byte compressed SEC1 secp256k1 public key.
    public_key: str


@dataclass(frozen=True)
class TrustRoots:
    """The public keys this Avibe release accepts, and its sequence floor.

    An Avibe release that ships no root has no channel: it is uncommissioned,
    and every check stops before any network or verifier call.
    """

    roots: tuple[TrustRoot, ...] = ()
    sequence_floor: int = 0

    @property
    def commissioned(self) -> bool:
        return bool(self.roots)

    def find(self, key_id: str) -> TrustRoot | None:
        return next((root for root in self.roots if root.key_id == key_id), None)


def parse_trust_roots(raw: bytes) -> TrustRoots:
    """Read the reviewed trust-root document Avibe ships. Raises ``ValueError``."""

    document = _object(strict_json(raw), frozenset({"schema_version", "sequence_floor", "roots"}), "trust roots")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("trust roots schema_version must be 1")
    floor = _integer(document["sequence_floor"], "sequence_floor", minimum=0)
    raw_roots = document["roots"]
    if not isinstance(raw_roots, list) or len(raw_roots) > MAX_TRUST_ROOTS:
        raise ValueError(f"roots must be a list of at most {MAX_TRUST_ROOTS} keys")
    roots: list[TrustRoot] = []
    for index, item in enumerate(raw_roots):
        entry = _object(item, frozenset({"key_id", "public_key"}), f"roots[{index}]")
        key_id, public_key = entry["key_id"], entry["public_key"]
        if not isinstance(key_id, str) or _KEY_ID_RE.fullmatch(key_id) is None:
            raise ValueError(f"roots[{index}].key_id is invalid")
        if not isinstance(public_key, str) or _PUBLIC_KEY_RE.fullmatch(public_key) is None:
            raise ValueError(f"roots[{index}].public_key must be a lowercase hex compressed secp256k1 key")
        if any(root.key_id == key_id or root.public_key == public_key for root in roots):
            raise ValueError(f"roots[{index}] repeats a key")
        roots.append(TrustRoot(key_id=key_id, public_key=public_key))
    return TrustRoots(roots=tuple(roots), sequence_floor=floor)


# ----- envelope and payload --------------------------------------------------


@dataclass(frozen=True)
class Envelope:
    key_id: str
    #: The exact signed bytes, as decoded from the envelope.
    payload: bytes
    #: Lowercase hex of the DER signature.
    signature: str
    raw: bytes


@dataclass(frozen=True)
class ChannelRelease:
    min_avibe_version: str
    manifest_sha256: str
    #: The exact published ``model-hub-engine-manifest.json`` bytes.
    manifest_bytes: bytes
    spec: ReleaseSpec

    @property
    def asset_release_tag(self) -> str:
        return self.spec.asset_release_tag

    @property
    def revision(self) -> tuple[int, int, int, int]:
        return self.spec.revision

    @property
    def minimum(self) -> tuple[int, int, int]:
        return stable_version(self.min_avibe_version)


@dataclass(frozen=True)
class Tombstone:
    asset_release_tag: str
    rollback_to: str
    reason: str


@dataclass(frozen=True)
class ChannelPayload:
    #: The only thing that orders one payload against another.
    sequence: int
    #: When the signer allocated this payload. A record for people: it is
    #: checked for format and decides nothing, neither acceptance nor order.
    issued_at: datetime
    releases: tuple[ChannelRelease, ...]
    revoked: tuple[Tombstone, ...]
    raw: bytes
    #: SHA-256 of ``raw``: the identity anti-replay state remembers.
    sha256: str

    def release(self, asset_release_tag: str) -> ChannelRelease | None:
        return next((item for item in self.releases if item.asset_release_tag == asset_release_tag), None)

    def tombstone(self, asset_release_tag: str) -> Tombstone | None:
        return next((item for item in self.revoked if item.asset_release_tag == asset_release_tag), None)


def channel_digest(payload: bytes) -> str:
    """The hex digest avault signs and verifies for ``payload``."""

    return hashlib.sha256(SIGNATURE_DOMAIN + payload).hexdigest()


def encode_envelope(*, key_id: str, payload: bytes, signature: str) -> bytes:
    """Serialize an envelope. The payload bytes travel unchanged, as base64."""

    document = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "key_id": key_id,
        "payload": base64.b64encode(payload).decode("ascii"),
        "signature": signature,
    }
    return (json.dumps(document, indent=2) + "\n").encode("ascii")


def parse_envelope(raw: bytes) -> Envelope:
    """Parse the outer envelope only. Nothing in it is trusted yet."""

    if len(raw) > MAX_ENVELOPE_BYTES:
        raise ChannelError("envelope_invalid", "the index is larger than the channel allows")
    try:
        document = _object(
            strict_json(raw), frozenset({"schema_version", "key_id", "payload", "signature"}), "envelope"
        )
        if type(document["schema_version"]) is not int or document["schema_version"] != ENVELOPE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {ENVELOPE_SCHEMA_VERSION}")
        key_id, signature = document["key_id"], document["signature"]
        if not isinstance(key_id, str) or _KEY_ID_RE.fullmatch(key_id) is None:
            raise ValueError("key_id is invalid")
        if not isinstance(signature, str) or _SIGNATURE_RE.fullmatch(signature) is None:
            raise ValueError("signature must be lowercase hex of a DER signature")
        payload = _canonical_base64(document["payload"], "payload", limit=MAX_PAYLOAD_BYTES)
        if not payload:
            raise ValueError("payload is empty")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ChannelError("envelope_invalid", str(exc)) from exc
    return Envelope(key_id=key_id, payload=payload, signature=signature, raw=bytes(raw))


def _parse_release(item: Any, index: int) -> ChannelRelease:
    context = f"releases[{index}]"
    entry = _object(item, frozenset({"min_avibe_version", "manifest_sha256", "manifest_base64"}), context)
    stable_version(entry["min_avibe_version"], f"{context}.min_avibe_version")
    digest = entry["manifest_sha256"]
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise ValueError(f"{context}.manifest_sha256 is invalid")
    manifest_bytes = _canonical_base64(entry["manifest_base64"], f"{context}.manifest_base64", limit=MAX_MANIFEST_BYTES)
    if hashlib.sha256(manifest_bytes).hexdigest() != digest:
        raise ValueError(f"{context}.manifest_sha256 does not match the manifest bytes")
    try:
        spec = parse_release_manifest(manifest_bytes)
    except EngineManifestError as exc:
        raise ValueError(f"{context} manifest is invalid: {exc}") from exc
    return ChannelRelease(
        min_avibe_version=entry["min_avibe_version"],
        manifest_sha256=digest,
        manifest_bytes=manifest_bytes,
        spec=spec,
    )


def _parse_tombstone(item: Any, index: int) -> Tombstone:
    context = f"revoked[{index}]"
    entry = _object(item, frozenset({"asset_release_tag", "rollback_to", "reason"}), context)
    tag, rollback_to, reason = entry["asset_release_tag"], entry["rollback_to"], entry["reason"]
    for name, value in (("asset_release_tag", tag), ("rollback_to", rollback_to)):
        if not isinstance(value, str) or ASSET_TAG_RE.fullmatch(value) is None:
            raise ValueError(f"{context}.{name} is not an engine release tag")
    if tag == rollback_to:
        raise ValueError(f"{context} rolls back to itself")
    if (
        not isinstance(reason, str)
        or not 0 < len(reason) <= MAX_REASON_CHARS
        or reason != reason.strip()
        or not reason.isprintable()
    ):
        raise ValueError(f"{context}.reason must be 1 to {MAX_REASON_CHARS} printable characters")
    return Tombstone(asset_release_tag=tag, rollback_to=rollback_to, reason=reason)


def parse_payload(raw: bytes) -> ChannelPayload:
    """Validate exact payload bytes. Call only after their signature verified."""

    if not 0 < len(raw) <= MAX_PAYLOAD_BYTES:
        raise ChannelError("payload_invalid", "the payload size is outside the channel's bounds")
    try:
        document = _object(
            strict_json(raw),
            frozenset({"repository", "channel", "sequence", "issued_at", "releases", "revoked"}),
            "payload",
        )
        if document["repository"] != CHANNEL_REPOSITORY:
            raise ValueError(f"repository must be {CHANNEL_REPOSITORY}")
        if document["channel"] != CHANNEL_NAME:
            raise ValueError(f"channel must be {CHANNEL_NAME}")
        sequence = _integer(document["sequence"], "sequence", minimum=1)
        issued_at = _timestamp(document["issued_at"], "issued_at")

        raw_releases, raw_revoked = document["releases"], document["revoked"]
        if not isinstance(raw_releases, list) or len(raw_releases) > MAX_RELEASES:
            raise ValueError(f"releases must be a list of at most {MAX_RELEASES} entries")
        if not isinstance(raw_revoked, list) or len(raw_revoked) > MAX_REVOKED:
            raise ValueError(f"revoked must be a list of at most {MAX_REVOKED} entries")
        releases = tuple(_parse_release(item, index) for index, item in enumerate(raw_releases))
        revoked = tuple(_parse_tombstone(item, index) for index, item in enumerate(raw_revoked))

        release_tags = [release.asset_release_tag for release in releases]
        if len(set(release_tags)) != len(release_tags):
            raise ValueError("releases names an engine release twice")
        revoked_tags = [tombstone.asset_release_tag for tombstone in revoked]
        if len(set(revoked_tags)) != len(revoked_tags):
            raise ValueError("revoked names an engine release twice")
        missing = sorted({tombstone.rollback_to for tombstone in revoked} - set(release_tags))
        if missing:
            raise ValueError(f"revoked rolls back to releases the payload does not carry: {missing}")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ChannelError("payload_invalid", str(exc)) from exc
    return ChannelPayload(
        sequence=sequence,
        issued_at=issued_at,
        releases=releases,
        revoked=revoked,
        raw=bytes(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


# ----- signature verification ------------------------------------------------


class SignatureVerifier(Protocol):
    def verify(self, *, public_key: str, digest: str, signature: str) -> None:
        """Return only when the signature verifies; raise ``ChannelError`` otherwise."""


def run_bounded(
    argv: Sequence[str],
    *,
    stdin: bytes,
    timeout: float,
    output_limit: int,
    env: Mapping[str, str],
    popen_kwargs: Mapping[str, Any],
) -> tuple[int, bytes]:
    """Run one short command with a deadline and a hard cap on what it may print."""

    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(env),
            **dict(popen_kwargs),
        )
    except OSError as exc:
        raise ChannelError("verifier_unavailable", f"avault could not be started ({type(exc).__name__})") from exc

    deadline = time.monotonic() + timeout
    captured = bytearray()
    overflowed = threading.Event()

    def read_output() -> None:
        stream = process.stdout
        assert stream is not None
        try:
            while len(captured) <= output_limit:
                chunk = stream.read1(output_limit + 1 - len(captured))
                if not chunk:
                    return
                captured.extend(chunk)
            overflowed.set()
        except (OSError, ValueError):
            return

    def write_input() -> None:
        stream = process.stdin
        assert stream is not None
        try:
            stream.write(stdin)
            stream.close()
        except (OSError, ValueError):
            # The command exited before reading its request; its status says why.
            return

    # Both pipes are served off this thread, so neither a command that never
    # reads nor one that never stops writing can hold the caller past the deadline.
    reader = threading.Thread(target=read_output, name="avault-output", daemon=True)
    writer = threading.Thread(target=write_input, name="avault-input", daemon=True)
    reader.start()
    writer.start()
    try:
        reader.join(max(0.0, deadline - time.monotonic()))
        if overflowed.is_set():
            raise ChannelError("verifier_failed", "avault printed more than a result")
        if reader.is_alive():
            raise ChannelError("verifier_failed", "avault did not answer in time")
        try:
            status = process.wait(max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise ChannelError("verifier_failed", "avault did not exit in time") from exc
        return status, bytes(captured)
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                pass
        for thread, stream in ((writer, process.stdin), (reader, process.stdout)):
            thread.join(1)
            # A thread still blocked on its pipe holds the stream's lock;
            # closing it here would wait on that call instead of ending this one.
            if stream is None or thread.is_alive():
                continue
            try:
                stream.close()
            except OSError:
                pass


def _verifier_version_supported(reported: str) -> bool:
    match = _AVAULT_VERSION_RE.fullmatch(reported)
    if match is None:
        return False
    core = tuple(int(part) for part in match.groups()[:3])
    # A pre-release or dev build of the minimum is still below the minimum.
    return core > VERIFIER_MIN_VERSION or (core == VERIFIER_MIN_VERSION and match.group(4) is None)


class AvaultVerifier:
    """Check a channel signature with avault's keyless ``verify`` verb.

    This is deliberately not Avibe's general avault caller. ``verify`` reads no
    master key, so nothing here selects a store, sends a passphrase line,
    records Keychain authorization state, or installs or upgrades avault. A
    missing, old, slow, or confused binary is a failed verification.

    Each call starts two short processes (``version``, then ``verify``), both
    bounded in time and output. It blocks; run it off any event loop.
    """

    def __init__(
        self,
        resolve_binary: Callable[[], str | None],
        *,
        timeout: float = 10.0,
        popen_kwargs: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._resolve_binary = resolve_binary
        self._timeout = timeout
        self._popen_kwargs = dict(popen_kwargs or {})
        self._environ = environ

    def _environment(self) -> dict[str, str]:
        source = os.environ if self._environ is None else self._environ
        # ``verify`` uses no store, but avault still parses AVAULT_STORE; an
        # unrelated store setting must not decide whether an index verifies.
        return {name: value for name, value in source.items() if not name.upper().startswith("AVAULT_")}

    def _run(self, binary: str, verb: str, stdin: bytes) -> tuple[int, bytes]:
        return run_bounded(
            [binary, verb],
            stdin=stdin,
            timeout=self._timeout,
            output_limit=_VERIFY_OUTPUT_LIMIT,
            env=self._environment(),
            popen_kwargs=self._popen_kwargs,
        )

    def verify(self, *, public_key: str, digest: str, signature: str) -> None:
        try:
            binary = self._resolve_binary()
        except Exception as exc:  # noqa: BLE001 - any resolver failure is "no verifier"
            raise ChannelError("verifier_unavailable", f"avault could not be located ({type(exc).__name__})") from exc
        if not binary:
            raise ChannelError("verifier_unavailable", "avault is not installed")

        status, output = self._run(binary, "version", b"")
        fields = output.decode("utf-8", errors="replace").split()
        if status != 0 or not fields:
            raise ChannelError("verifier_unavailable", "avault did not report its version")
        if not _verifier_version_supported(fields[-1]):
            minimum = ".".join(str(part) for part in VERIFIER_MIN_VERSION)
            raise ChannelError("verifier_too_old", f"signature checks need avault {minimum} or later")

        request = json.dumps(
            {"scheme": SIGNATURE_SCHEME, "public_key": public_key, "digest": digest, "signature": signature},
            separators=(",", ":"),
        ).encode("ascii")
        if len(request) > _VERIFY_REQUEST_LIMIT:
            raise ChannelError("verifier_failed", "the verification request is larger than avault accepts")
        status, output = self._run(binary, "verify", request)
        try:
            answer = strict_json(output)
        except (UnicodeDecodeError, ValueError):
            answer = None
        well_formed = isinstance(answer, dict) and set(answer) == {"valid"} and type(answer["valid"]) is bool
        if status == 0 and well_formed and answer["valid"] is True:
            return
        if status == 1 and well_formed and answer["valid"] is False:
            raise ChannelError("signature_invalid", "the index signature does not match a shipped key")
        raise ChannelError("verifier_failed", f"avault verify gave no usable answer (exit status {status})")


def authenticate(raw: bytes, *, roots: TrustRoots, verifier: SignatureVerifier) -> tuple[Envelope, ChannelPayload]:
    """Return the envelope and payload only if a shipped root signed these bytes.

    The payload is parsed after its signature verifies, so unauthenticated
    bytes never reach the manifest parser.
    """

    if not roots.commissioned:
        raise ChannelError("uncommissioned", "this Avibe release ships no channel key")
    envelope = parse_envelope(raw)
    root = roots.find(envelope.key_id)
    if root is None:
        raise ChannelError("key_unknown", "the index names a key this Avibe release does not ship")
    verifier.verify(public_key=root.public_key, digest=channel_digest(envelope.payload), signature=envelope.signature)
    return envelope, parse_payload(envelope.payload)


# ----- anti-replay state -----------------------------------------------------


@dataclass(frozen=True)
class ReleaseIdentity:
    min_avibe_version: str
    manifest_sha256: str


def check_successor(
    known_releases: Mapping[str, ReleaseIdentity],
    known_revoked: Sequence[Tombstone],
    payload: ChannelPayload,
) -> None:
    """Refuse a payload that rewrites what an earlier payload already said.

    A release that was promoted keeps its bytes and its minimum; a withdrawal
    is permanent. A release may stop being listed, which is not a withdrawal.
    """

    for tombstone in known_revoked:
        if payload.tombstone(tombstone.asset_release_tag) != tombstone:
            raise ChannelError(
                "tombstone_removed", f"the index dropped or changed the withdrawal of {tombstone.asset_release_tag}"
            )
    for release in payload.releases:
        known = known_releases.get(release.asset_release_tag)
        if known is not None and known != ReleaseIdentity(release.min_avibe_version, release.manifest_sha256):
            raise ChannelError(
                "release_mutated", f"the index changed the bytes or minimum of {release.asset_release_tag}"
            )


@dataclass(frozen=True)
class ChannelState:
    sequence: int
    payload_sha256: str
    envelope: bytes
    releases: Mapping[str, ReleaseIdentity]
    revoked: tuple[Tombstone, ...]


@dataclass(frozen=True)
class ChannelSnapshot:
    """The recorded payload, authenticated again, and how it stands against the shipped floor."""

    envelope: Envelope
    payload: ChannelPayload
    #: False when the recorded index is below the sequence floor this Avibe
    #: release ships. Its withdrawals still apply; it brings in nothing new.
    meets_floor: bool


class ChannelStateStore:
    """The newest payload this machine accepted, kept across restarts.

    One file holds the highest accepted sequence, that payload's digest, every
    release identity and withdrawal seen so far, and the accepted envelope.
    The envelope is kept so a later, offline start can prove where a retained
    engine came from. Acceptance is decided under a cross-process lock against
    the file's current contents, so concurrent refreshes cannot move it back,
    and a rejected payload never changes the file.

    Nothing recorded here ages. The sequence alone decides what replaces it:
    the same payload offered again is accepted at any later time, and what was
    accepted stays the machine's index until a higher sequence arrives.

    The file is replaced atomically, with a best-effort directory flush. That
    suits a record nothing acts on yet; it is not proof that an accepted
    sequence survives power loss, which a consumer that activates engines
    must have first (docs/plans/model-hub-engine-channel.md).

    This is not a defense against whoever controls the OS account.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.state_path = directory / "state.json"
        self.lock_path = directory / ".state.lock"

    def load(self) -> ChannelState | None:
        try:
            raw = self.state_path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ChannelError("state_unreadable", f"channel state cannot be read ({type(exc).__name__})") from exc
        try:
            document = _object(
                strict_json(raw),
                frozenset({"schema_version", "sequence", "payload_sha256", "envelope", "releases", "revoked"}),
                "channel state",
            )
            if type(document["schema_version"]) is not int or document["schema_version"] != _STATE_SCHEMA_VERSION:
                raise ValueError("unsupported schema_version")
            sequence = _integer(document["sequence"], "sequence", minimum=1)
            digest = document["payload_sha256"]
            if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
                raise ValueError("payload_sha256 is invalid")
            envelope = _canonical_base64(document["envelope"], "envelope", limit=MAX_ENVELOPE_BYTES)
            raw_releases, raw_revoked = document["releases"], document["revoked"]
            if not isinstance(raw_releases, dict) or not isinstance(raw_revoked, list):
                raise ValueError("releases and revoked have the wrong shape")
            releases: dict[str, ReleaseIdentity] = {}
            for tag, item in raw_releases.items():
                entry = _object(item, frozenset({"min_avibe_version", "manifest_sha256"}), f"releases[{tag}]")
                release_revision(tag)
                stable_version(entry["min_avibe_version"])
                if not isinstance(entry["manifest_sha256"], str) or SHA256_RE.fullmatch(entry["manifest_sha256"]) is None:
                    raise ValueError(f"releases[{tag}].manifest_sha256 is invalid")
                releases[tag] = ReleaseIdentity(entry["min_avibe_version"], entry["manifest_sha256"])
            revoked = tuple(_parse_tombstone(item, index) for index, item in enumerate(raw_revoked))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ChannelError("state_unreadable", f"channel state is damaged: {exc}") from exc
        return ChannelState(
            sequence=sequence, payload_sha256=digest, envelope=envelope, releases=releases, revoked=revoked
        )

    def accept(self, envelope: Envelope, payload: ChannelPayload, *, floor: int) -> bool:
        """Record an authenticated payload. Return whether it is new to this machine.

        Raises ``ChannelError`` and leaves the stored state untouched when the
        payload is below the shipped floor, is older than what was accepted,
        differs from the accepted payload at the same sequence, or rewrites
        earlier history. When it was issued plays no part.
        """

        from config.atomic_io import write_atomic
        from storage.lock import MigrationFileLock

        if payload.sequence < floor:
            raise ChannelError("replayed", "the index is older than this Avibe release accepts")

        self.directory.mkdir(parents=True, exist_ok=True)
        with MigrationFileLock(self.lock_path):
            state = self.load()
            if state is not None:
                if payload.sequence < state.sequence:
                    raise ChannelError("replayed", "the index is older than one this machine already accepted")
                if payload.sequence == state.sequence:
                    if payload.sha256 != state.payload_sha256:
                        raise ChannelError("equivocation", "two different indexes carry the same sequence")
                    return False
                check_successor(state.releases, state.revoked, payload)
            releases = dict(state.releases) if state is not None else {}
            for release in payload.releases:
                releases[release.asset_release_tag] = ReleaseIdentity(
                    release.min_avibe_version, release.manifest_sha256
                )
            document = {
                "schema_version": _STATE_SCHEMA_VERSION,
                "sequence": payload.sequence,
                "payload_sha256": payload.sha256,
                "envelope": base64.b64encode(envelope.raw).decode("ascii"),
                "releases": {
                    tag: {"min_avibe_version": identity.min_avibe_version, "manifest_sha256": identity.manifest_sha256}
                    for tag, identity in sorted(releases.items())
                },
                "revoked": [
                    {"asset_release_tag": item.asset_release_tag, "rollback_to": item.rollback_to, "reason": item.reason}
                    for item in payload.revoked
                ],
            }
            write_atomic(self.state_path, json.dumps(document, sort_keys=True) + "\n")
            return True

    def cached(self, *, roots: TrustRoots, verifier: SignatureVerifier) -> ChannelSnapshot | None:
        """Re-authenticate the stored envelope. ``None`` when nothing was accepted yet.

        A snapshot below the sequence floor this Avibe release ships is still
        returned, marked as not meeting it: it can show where a retained
        engine came from and which releases were withdrawn, but it brings in
        nothing new. The floor is checked here as well as on acceptance,
        because an index recorded by an earlier Avibe release may predate the
        floor a later one ships.
        """

        state = self.load()
        if state is None:
            return None
        envelope, payload = authenticate(state.envelope, roots=roots, verifier=verifier)
        if payload.sequence != state.sequence or payload.sha256 != state.payload_sha256:
            raise ChannelError("state_unreadable", "the stored index does not match the recorded sequence")
        return ChannelSnapshot(envelope=envelope, payload=payload, meets_floor=payload.sequence >= roots.sequence_floor)


# ----- selection -------------------------------------------------------------

#: Why an index may bring nothing new onto a machine although it is the
#: machine's index. Neither is about time: an index does not age.
#: The caller was told to stay off the network.
HOLD_OFFLINE = "offline"
#: The recorded index is below the sequence floor this Avibe release ships.
HOLD_BELOW_FLOOR = "index_below_floor"


class ResolutionAction(str, Enum):
    #: Stay on the installed engine.
    KEEP = "keep"
    #: Move forward to ``target``.
    ADOPT = "adopt"
    #: The installed engine was withdrawn; move to the safe fallback ``target``.
    WITHDRAW = "withdraw"
    #: The installed engine was withdrawn and nothing safe can replace it.
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Resolution:
    action: ResolutionAction
    reason: str
    target: ChannelRelease | None = None
    #: The withdrawal that applies to the installed engine, if any.
    tombstone: Tombstone | None = None
    #: False under a hold: ``target`` may then only be activated from an
    #: engine already verified on this machine.
    download_authorized: bool = False


def avibe_version_qualifies(avibe_version: str) -> Any | None:
    """Return the parsed version of a released Avibe build, or ``None``.

    A source checkout, an editable install, or a build with a local label
    names no published release, so no minimum can be held against it.
    """

    from packaging.version import InvalidVersion, Version

    try:
        parsed = Version(avibe_version)
    except (InvalidVersion, TypeError):
        return None
    if parsed.is_devrelease or parsed.local is not None:
        return None
    return parsed


def resolve(
    payload: ChannelPayload,
    *,
    hold: str | None,
    avibe_version: str,
    host_platform: str,
    bundled: str | None,
    current: str,
    quarantined: frozenset[str] = frozenset(),
    allow_adoption: bool = True,
) -> Resolution:
    """Decide what an authenticated payload means for this machine.

    ``bundled`` is the engine release this Avibe ships, ``current`` the one in
    effect, both as asset release tags. ``quarantined`` holds releases that
    failed here and wait for an explicit retry. ``allow_adoption`` is false
    when routine updates are switched off.

    ``hold`` is ``None``, or the reason this index may bring nothing new onto
    the machine: ``HOLD_OFFLINE`` or ``HOLD_BELOW_FLOOR``. Under a hold nothing
    is adopted and no download is authorized. How old the index is is never a
    reason: it is the machine's index until a higher sequence replaces it.

    A withdrawal of the engine in effect is decided first and by itself. It
    does not depend on a hold, on routine updates being on, on the bundled
    identity, or on this being a released Avibe build. The engine
    moves to the one fallback its tombstone names, and only if that release is
    listed, not withdrawn, not quarantined, has an asset for this host, and its
    minimum Avibe is provably met. Otherwise Model Hub is blocked: a withdrawn
    engine is never kept, and no other release is substituted.

    Ordinary selection only moves forward and never below the bundled engine.
    A release that is no longer listed is not a withdrawal.
    """

    from packaging.version import Version

    running = avibe_version_qualifies(avibe_version)
    revoked = {tombstone.asset_release_tag: tombstone for tombstone in payload.revoked}

    def usable(release: ChannelRelease) -> bool:
        return (
            running is not None
            and release.asset_release_tag not in revoked
            and release.asset_release_tag not in quarantined
            and Version(release.min_avibe_version) <= running
            and release.spec.archive_for(host_platform) is not None
        )

    tombstone = revoked.get(current)
    if tombstone is not None:
        fallback = payload.release(tombstone.rollback_to)
        if fallback is not None and usable(fallback):
            return Resolution(
                ResolutionAction.WITHDRAW, "engine_withdrawn", fallback, tombstone, download_authorized=hold is None
            )
        return Resolution(ResolutionAction.BLOCKED, "engine_withdrawn_without_fallback", None, tombstone)

    if running is None:
        return Resolution(ResolutionAction.KEEP, "avibe_version_unreleased")
    try:
        current_revision = release_revision(current)
        bundled_revision = release_revision(bundled)
    except EngineManifestError:
        return Resolution(ResolutionAction.KEEP, "engine_identity_unrecognized")
    if hold is not None:
        return Resolution(ResolutionAction.KEEP, hold)
    if not allow_adoption:
        return Resolution(ResolutionAction.KEEP, "adoption_disabled")
    candidates = [
        release
        for release in payload.releases
        if usable(release) and release.revision > current_revision and release.revision >= bundled_revision
    ]
    newer = max(candidates, key=lambda release: release.revision, default=None)
    if newer is None:
        return Resolution(ResolutionAction.KEEP, "up_to_date")
    return Resolution(ResolutionAction.ADOPT, "newer_release", newer, download_authorized=True)
