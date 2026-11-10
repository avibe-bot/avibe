#!/usr/bin/env python3
"""Plan, sign and publish the signed Model Hub engine channel index.

The index at ``https://dl.avibe.bot/index/model-hub-engine.json`` is the one
record of which engine releases installed Avibe may adopt and which were
withdrawn. This script is its only writer. Each run makes one change to the
index that is published now: promote a qualified release, renew the
signature, or withdraw a release. It never rebuilds the index from GitHub
releases, so a withdrawal already published is carried into every later index.

Three steps run in three jobs, so no job holds two authorities:

- ``authorize`` proves the run is the reviewed workflow on the default branch
  and that the GitHub Environment it is about to use requires approval. With
  ``--require-approval`` it also proves this run was approved.
- ``sign`` plans the next payload and asks avault for a signature. It holds
  the signing key and no bucket credential.
- ``publish`` holds the bucket credential and no key. It replaces the object
  only if it is still the one the plan started from, then reads it back.

The channel is uncommissioned until Avibe ships a public root: every step
stops first. See docs/release.md and docs/plans/model-hub-engine-channel.md.
"""

from __future__ import annotations

import argparse
import base64
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
from typing import Any, Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import model_hub_engine_channel as channel  # noqa: E402
from core.model_hub_engine_channel import (  # noqa: E402
    ChannelError,
    ChannelPayload,
    ReleaseIdentity,
    TrustRoots,
)
from core.model_hub_engine_manifest import (  # noqa: E402
    ASSET_TAG_RE,
    EngineManifestError,
    parse_release_manifest,
)

TRUST_ROOTS_PATH = REPO_ROOT / "vibe" / "model_hub_runtime" / "engine_channel_roots.json"
REPOSITORY = channel.CHANNEL_REPOSITORY
DEFAULT_BRANCH = "master"
WORKFLOW_PATH = ".github/workflows/model-hub-engine-channel.yml"
INDEX_KEY = "index/model-hub-engine.json"
INDEX_CACHE_CONTROL = "public, max-age=60"
SIGNER_ENVIRONMENT = "model-hub-engine-channel-signer"
WRITER_ENVIRONMENT = "model-hub-engine-channel-writer"
ENVELOPE_ARTIFACT = "model-hub-engine-channel-envelope"

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


def _require_terminating_fallbacks(payload: ChannelPayload) -> None:
    """Every withdrawal must lead, through later withdrawals, to a release still standing."""

    revoked = {tombstone.asset_release_tag: tombstone for tombstone in payload.revoked}
    for tombstone in payload.revoked:
        seen = {tombstone.asset_release_tag}
        step = tombstone
        while step.rollback_to in revoked:
            if step.rollback_to in seen:
                raise ProducerError(f"withdrawals of {tombstone.asset_release_tag} lead back to a withdrawn release")
            seen.add(step.rollback_to)
            step = revoked[step.rollback_to]


def plan_payload(current: ChannelPayload | None, operation: Operation, *, now: datetime, floor: int) -> bytes:
    """Return the exact payload bytes that follow ``current`` by one change.

    ``current`` is the authenticated index published now, or ``None`` only
    before the channel's first publication. Every release and withdrawal it
    carries is copied byte for byte; the operation adds at most one entry.
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
            raise ProducerError("the channel has no published index to renew or withdraw from")
        sequence = max(1, floor)
    else:
        if now < current.issued_at:
            raise ProducerError("this runner's clock is behind the published index")
        sequence = current.sequence + 1

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
    _require_terminating_fallbacks(payload)
    return raw


def require_single_step(current: ChannelPayload | None, successor: ChannelPayload, *, floor: int) -> None:
    """Refuse to publish an index that is not the direct successor of ``current``."""

    try:
        if current is None:
            if successor.sequence != max(1, floor):
                raise ProducerError("a first publication must start at the shipped sequence floor")
            return
        if successor.sequence != current.sequence + 1:
            raise ProducerError(
                f"the signed index has sequence {successor.sequence}, but the published one is {current.sequence}; "
                "plan again from the index published now"
            )
        channel.check_successor(_identities(current), current.revoked, successor)
    except ChannelError as exc:
        raise ProducerError(f"the signed index rewrites published history: {exc}") from exc
    _require_terminating_fallbacks(successor)


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


# ----- trust roots, verification and signing ---------------------------------


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
    environ: Mapping[str, str],
    api: Callable[[str], Any],
    require_approval: bool,
) -> None:
    """Stop unless this run may use ``environment`` for a privileged step."""

    load_trust_roots()
    problems = run_context_problems(environ)
    base = f"/repos/{REPOSITORY}/environments/{environment}"
    protection, reviewers = environment_protection_problems(api(base), api(f"{base}/deployment-branch-policies"))
    problems += protection
    if require_approval and not protection:
        run_id = environ.get("GITHUB_RUN_ID", "")
        problems += approval_problems(
            api(f"/repos/{REPOSITORY}/actions/runs/{run_id}/approvals"), environment=environment, reviewers=reviewers
        )
    if problems:
        raise ProducerError(f"{environment} is not authorized: " + "; ".join(problems))


def require_never_published(api: Callable[[str], Any]) -> None:
    """Refuse a first publication when an earlier run already archived an index.

    Starting again from nothing would drop every withdrawal published so far.
    A channel that was published before continues from its newest archive.
    """

    listing = api(f"/repos/{REPOSITORY}/actions/artifacts?name={ENVELOPE_ARTIFACT}&per_page=1")
    if not isinstance(listing, dict) or type(listing.get("total_count")) is not int:
        raise ProducerError("earlier publications cannot be ruled out: the archive listing is unreadable")
    if listing["total_count"] != 0:
        raise ProducerError("the channel was published before; recover from its newest archived envelope instead")


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
    recovered: bytes | None = None,
    first_publication: bool = False,
    fetch_public: Callable[[], bytes | None] = fetch_public_index,
    sleep: Callable[[float], None] = time.sleep,
    public_wait_seconds: float = 300.0,
) -> ChannelPayload:
    """Replace the stored index with ``envelope`` and prove clients receive it.

    The stored object is read with its ETag, and the write succeeds only over
    that object, so two writers cannot both win and a stale one cannot undo a
    newer index. ``recovered`` is an archived, authenticated envelope to
    continue from when the stored object was lost or damaged.
    """

    successor = authenticate(envelope, roots=roots, verifier=verifier)
    stored = bucket.read()
    current: ChannelPayload | None = None
    if stored is not None:
        try:
            current = authenticate(stored[0], roots=roots, verifier=verifier)
        except ProducerError:
            if recovered is None:
                raise ProducerError("the stored index does not verify; recover from an archived envelope") from None
    if recovered is not None:
        archived = authenticate(recovered, roots=roots, verifier=verifier)
        if current is not None and current.sequence >= archived.sequence:
            raise ProducerError("the stored index is intact and at least as new as the archive; recovery is not needed")
        current = archived
    if current is None and not (first_publication and stored is None):
        raise ProducerError("no index is published; a first publication must be requested explicitly")
    if current is not None and first_publication:
        raise ProducerError("the channel already has an index; this is not a first publication")
    require_single_step(current, successor, floor=roots.sequence_floor)

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


def _newest_archived(directory: Path, *, roots: TrustRoots, verifier: channel.SignatureVerifier) -> bytes:
    """The archived envelope with the highest sequence that verifies."""

    best: tuple[int, bytes] | None = None
    for path in sorted(directory.rglob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        raw = path.read_bytes()
        try:
            payload = authenticate(raw, roots=roots, verifier=verifier)
        except ProducerError:
            continue
        if best is None or payload.sequence > best[0]:
            best = (payload.sequence, raw)
    if best is None:
        raise ProducerError("no archived envelope verifies against the shipped roots")
    return best[1]


def _operation(args: argparse.Namespace) -> Operation:
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    authorize_parser = commands.add_parser("authorize", help="Prove this run may use a protected environment.")
    authorize_parser.add_argument("--environment", required=True, choices=[SIGNER_ENVIRONMENT, WRITER_ENVIRONMENT])
    authorize_parser.add_argument("--require-approval", action="store_true")

    sign_parser = commands.add_parser("sign", help="Plan the next index and sign it with avault.")
    sign_parser.add_argument("--operation", required=True, choices=["promote", "renew", "revoke"])
    sign_parser.add_argument("--tag")
    sign_parser.add_argument("--rollback-to")
    sign_parser.add_argument("--reason")
    sign_parser.add_argument("--candidate-dir", type=Path)
    sign_parser.add_argument("--avault", required=True)
    sign_parser.add_argument("--key-id", required=True)
    sign_parser.add_argument("--key-name", required=True)
    sign_parser.add_argument("--key-envelope", type=Path, required=True)
    sign_parser.add_argument("--recover-from", type=Path, help="directory of archived envelopes to continue from")
    sign_parser.add_argument("--first-publication", action="store_true")
    sign_parser.add_argument("--output", type=Path, required=True)

    baselines_parser = commands.add_parser("baselines", help="List the engines a candidate must be compared with.")
    baselines_parser.add_argument("--avault")
    baselines_parser.add_argument("--bundled", type=Path, action="append", default=[])
    baselines_parser.add_argument("--first-publication", action="store_true")
    baselines_parser.add_argument("--output-dir", type=Path, required=True)

    publish_parser = commands.add_parser("publish", help="Replace the stored index and read it back.")
    publish_parser.add_argument("--envelope", type=Path, required=True)
    publish_parser.add_argument("--avault", required=True)
    publish_parser.add_argument("--bucket", required=True)
    publish_parser.add_argument("--endpoint-url", required=True)
    publish_parser.add_argument("--recover-from", type=Path)
    publish_parser.add_argument("--first-publication", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "authorize":
            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            authorize(
                args.environment,
                environ=os.environ,
                api=lambda path: github_api(path, token=token),
                require_approval=args.require_approval,
            )
            print(json.dumps({"ok": True, "environment": args.environment}))
            return 0

        if args.command == "baselines":
            # Listing baselines publishes nothing. Before the channel has an
            # index there is nothing signed to read, so no key is needed.
            if args.first_publication:
                if fetch_public_index() is not None:
                    raise ProducerError("the channel already has an index; this is not a first publication")
                current = None
            else:
                roots = load_trust_roots()
                if not args.avault:
                    raise ProducerError("reading the published index needs the avault verifier")
                published = fetch_public_index()
                if published is None:
                    raise ProducerError("no index is published; a first publication must be requested explicitly")
                current = authenticate(published, roots=roots, verifier=avault_verifier(args.avault))
            manifests = baseline_manifests(current, [path.read_bytes() for path in args.bundled])
            (args.output_dir / "manifests").mkdir(parents=True, exist_ok=True)
            for tag, manifest_bytes in manifests.items():
                (args.output_dir / "manifests" / f"{tag}.json").write_bytes(manifest_bytes)
            digests = {tag: hashlib.sha256(manifest_bytes).hexdigest() for tag, manifest_bytes in sorted(manifests.items())}
            (args.output_dir / "baselines.json").write_text(json.dumps(digests, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"ok": True, "baselines": digests}))
            return 0

        roots = load_trust_roots()
        verifier = avault_verifier(args.avault)
        recovered = (
            _newest_archived(args.recover_from, roots=roots, verifier=verifier) if args.recover_from else None
        )
        if args.command == "sign":
            published = fetch_public_index()
            if recovered is not None:
                current = authenticate(recovered, roots=roots, verifier=verifier)
            elif published is not None:
                if args.first_publication:
                    raise ProducerError("the channel already has an index; this is not a first publication")
                current = authenticate(published, roots=roots, verifier=verifier)
            elif args.first_publication:
                token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
                require_never_published(lambda path: github_api(path, token=token))
                current = None
            else:
                raise ProducerError("no index is published; a first publication must be requested explicitly")
            payload = plan_payload(
                current, _operation(args), now=datetime.now(timezone.utc), floor=roots.sequence_floor
            )
            try:
                key_envelope = channel.strict_json(args.key_envelope.read_bytes())
            except (OSError, ValueError) as exc:
                raise ProducerError(f"the sealed channel key cannot be read: {type(exc).__name__}") from exc
            if not isinstance(key_envelope, dict):
                raise ProducerError("the sealed channel key is not an avault envelope")
            envelope = sign_payload(
                payload,
                key_id=args.key_id,
                key_name=args.key_name,
                key_envelope=key_envelope,
                roots=roots,
                verifier=verifier,
                sign=lambda request: _avault_sign(args.avault, request),
            )
            args.output.write_bytes(envelope)
            signed = channel.parse_payload(payload)
            print(json.dumps({"ok": True, "sequence": signed.sequence, "payload_sha256": signed.sha256}))
            return 0

        bucket = IndexBucket(args.bucket, args.endpoint_url, _run_command)
        published_payload = publish(
            args.envelope.read_bytes(),
            bucket=bucket,
            roots=roots,
            verifier=verifier,
            recovered=recovered,
            first_publication=args.first_publication,
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
