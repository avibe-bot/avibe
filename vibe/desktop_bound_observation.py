"""Read-only views of the local publication transaction. Neither view is a claim permit."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
import stat
import time
from uuid import UUID

from config import paths
from config.atomic_io import open_snapshot
from config.v2_config import V2Config, config_file_lock
from core.private_fs import is_private
from vibe import desktop_legacy_observation as legacy, desktop_local_instance as local, remote_access
from vibe.personal_claim_client import ClaimError, text
from vibe.personal_claims import DesktopPublication, _binding_stamp, _marker


def _unique_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError
    return obj


def _id(info):
    return info.st_dev, info.st_ino


def _node(path, kind, *, optional=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise
    if not kind(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ClaimError("local_binding_unavailable")
    return info


def _file_stamp(info):
    return None if info is None else (_id(info), info.st_mtime_ns, info.st_ctime_ns, info.st_size)


@dataclass(frozen=True, repr=False)
class BindingSample:
    # Only instance_id crosses the pipe. The stamp is scoped to the admitted physical root.
    instance_id: str | None
    _stamp: tuple


def _bound_receipt(cloud, pins):
    path = _marker()
    info = _node(path, stat.S_ISREG)
    reader = pins.enter_context(open_snapshot(path))
    if info.st_nlink != 1 or not os.path.samestat(info, os.fstat(reader.fileno())):
        raise ClaimError("local_binding_unavailable")
    data = reader.read(4097)
    if len(data) > 4096:
        raise ClaimError("local_binding_unavailable")
    saved = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    generation = _binding_stamp(remote_access._pairing_identity_from_cloud(cloud))
    if (
        not isinstance(saved, dict)
        or "claim_id" in saved
        or not isinstance(bound := saved.get("bound"), dict)
        or set(bound) != {"claim_id", "generation"}
        or bound["generation"] != generation
        or _file_stamp(_node(path, stat.S_ISREG)) != _file_stamp(info)
    ):
        raise ClaimError("local_binding_changed")
    return bound, (_file_stamp(info), hashlib.sha256(data).digest())


def _directories(root, root_id):
    if paths.physical_home(paths.get_vibe_remote_dir()) != root or _id(_node(root, stat.S_ISDIR)) != root_id:
        raise ClaimError("local_binding_changed")
    return tuple(_id(_node(directory, stat.S_ISDIR)) for directory in (root / "config", root / "state"))


@contextmanager
def _pinned(path, limit, *, optional=False):
    info = _node(path, stat.S_ISREG, optional=optional)
    with ExitStack() as stack:
        data = None
        if info is not None:
            if info.st_nlink != 1 or info.st_size > limit:
                raise ClaimError(legacy.PRESERVE)
            fd = stack.enter_context(open_snapshot(path))
            handle = os.fstat(fd.fileno())
            if _file_stamp(info) != _file_stamp(handle):
                raise ClaimError("local_binding_changed")
            if ((os.name != "nt" and handle.st_uid != os.geteuid())
                    or not is_private(path, handle.st_mode, fd=fd.fileno())):
                raise ClaimError(legacy.PRESERVE)
            data = fd.read(limit + 1)
            if len(data) != info.st_size:
                raise ClaimError("local_binding_changed")
        yield data, info
        if _file_stamp(info) != _file_stamp(_node(path, stat.S_ISREG, optional=optional)):
            raise ClaimError("local_binding_changed")


def _pending_code(root, root_id, dirs, lock_id):
    # Caller holds the publisher's config lock. Use only the admitted physical
    # paths, keep all three files pinned, and check even absence again at exit.
    config = root / "config" / "config.json"
    state = root / "state"
    with (_pinned(state / remote_access._PENDING_PAIRING_FILENAME, 1 << 20) as (journal, info),
          _pinned(config, 8 << 20) as (cfg, _),
          _pinned(state / "installation-id.json", 4096, optional=True) as (identity, _)):
        try:
            code = legacy.classify(journal, cfg, identity, recent=time.time_ns() - info.st_mtime_ns <= 60_000_000_000)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            code = legacy.PRESERVE
    if (dirs != _directories(root, root_id)
            or lock_id != _id(_node(config.with_name(".config.json.lock"), stat.S_ISREG))):
        raise ClaimError("local_binding_changed")
    return code


@contextmanager
def _snapshot(root, root_id, pins, *, inspect_pending=False):
    try:
        dirs = _directories(root, root_id)
        path = paths.get_config_path()
        lock = _node(path.with_name(f".{path.name}.lock"), stat.S_ISREG, optional=True)
        if lock is not None and lock.st_nlink != 1:
            raise ClaimError("local_binding_unavailable")
        if inspect_pending and lock is None and os.path.lexists(remote_access._pending_pairing_path()):
            # Do not bootstrap a missing lock in order to diagnose an orphaned journal.
            raise ClaimError(legacy.PRESERVE)
        with config_file_lock(path):
            if dirs != _directories(root, root_id):
                raise ClaimError("local_binding_changed")
            # A journal is never a published binding. Only the initial read-only
            # inspector may distinguish its reason; neither recheck nor service
            # detection interprets pending data as a receipt or a new claim.
            try:
                remote_access._pending_pairing_path().lstat()
            except FileNotFoundError:
                pass
            else:
                if inspect_pending and lock is not None:
                    raise ClaimError(_pending_code(root, root_id, dirs, _id(lock)))
                raise ClaimError("pairing_recovery_required")
            before = _file_stamp(_node(path, stat.S_ISREG))
            # Keep the actual file open across hosted classification (delete-sharing on
            # Windows). A publisher can replace it, but its file ID cannot be recycled
            # while pinned. Also compare content in case of a non-atomic external edit.
            reader = pins.enter_context(open_snapshot(path))
            if before != _file_stamp(os.fstat(reader.fileno())):
                raise ClaimError("local_binding_changed")
            data = reader.read()
            raw = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(raw, dict) or not isinstance(remote := raw.get("remote_access", {}), dict):
                raise ValueError
            if not isinstance(cloud_data := remote.get("vibe_cloud", {}), dict):
                raise ValueError
            # Unlike load(), from_payload never initializes data directories or emits
            # a recovery backup. A failed read must not fabricate a default pairing.
            cloud = V2Config.from_payload(raw).remote_access.vibe_cloud
            identity_file = _file_stamp(_node(local._identity_path(), stat.S_ISREG, optional=True))
            installation = local._installation_id() if identity_file is not None else None
            if identity_file is not None and installation is None:
                raise ClaimError("local_binding_changed")
            fields = remote_access._PAIRING_REQUIRED_FIELDS
            instance = None
            if cloud.enabled or any(getattr(cloud, field) for field in (*fields, "session_secret", "instance_kind")):
                if not all(getattr(cloud, field).strip() for field in (*fields, "session_secret")):
                    raise ClaimError("local_binding_unavailable")
                if cloud_data.get("backend_url") not in ("https://avibe.bot", "https://avibe.bot/"):
                    raise ClaimError("invalid_backend")
                if not text(cloud.instance_id, 120):
                    raise ClaimError("local_binding_unavailable")
                instance = cloud.instance_id
            stamp = (dirs, before, hashlib.sha256(data).digest(), identity_file, installation)
            yield instance, installation, cloud, stamp
            if (
                dirs != _directories(root, root_id)
                or before != _file_stamp(_node(path, stat.S_ISREG))
                or identity_file != _file_stamp(_node(local._identity_path(), stat.S_ISREG, optional=True))
            ):
                raise ClaimError("local_binding_changed")
    except ClaimError:
        raise
    except Exception:
        # Parse/lock/IO diagnostics may identify the installation or contain arbitrary
        # stored values. This child has only the closed machine error vocabulary.
        raise ClaimError("local_binding_unavailable") from None


def sample(root, root_id, pins: ExitStack, *, publication: DesktopPublication | None = None, inspect_pending=False) -> BindingSample:
    """Pin the helper's exact files across classification, including unrelated config saves."""
    with _snapshot(root, root_id, pins, inspect_pending=inspect_pending and publication is None) as (instance, installation, cloud, stamp):
        receipt = None
        if publication:
            if (instance != publication.instance_id or installation != publication.installation
                    or cloud.public_url != "https://" + publication.hostname):
                raise ClaimError("local_binding_changed")
            bound, receipt = _bound_receipt(cloud, pins)
            if bound["claim_id"] != publication.claim_id:
                raise ClaimError("local_binding_changed")
        return BindingSample(instance, (*stamp, receipt))


def service_generation(root, root_id) -> bytes | None:
    """Current committed personal pairing, not evidence that *this* Desktop published it."""
    # Cheap negative for services that have never seen a personal claim. Existence
    # never admits work: all positive evidence is reread under the publisher lock.
    _directories(root, root_id)
    if _node(_marker(), stat.S_ISREG, optional=True) is None:
        return None
    with ExitStack() as pins, _snapshot(root, root_id, pins) as (instance, installation, cloud, _):
        if not instance or not installation:
            return None
        bound, _receipt = _bound_receipt(cloud, pins)
        if str(UUID(bound["claim_id"])) != bound["claim_id"]:
            return None
        # The connector's On request is never ours to make. An explicit Off → On
        # change does, however, make an address read eligible that was skipped
        # while Off. Session refreshes and unrelated saves owe no extra read.
        fields = [installation, bound["claim_id"], bound["generation"], cloud.enabled]
        return hashlib.sha256(json.dumps(fields).encode()).digest()
