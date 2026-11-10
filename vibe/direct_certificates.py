"""Optional paired-instance certificate client; nothing starts it here.

All entry points are synchronous (filesystem, crypto and bounded device HTTP).
Call from a worker thread, never a controller/ASGI event loop. I3 owns opt-in,
policy freshness, scheduling and ingress reload. Cloudflare is never touched.

The versioned manifest atomically selects immutable key/chain files under
``state/direct``. A pending rotation cannot overwrite the active pair. The
config transaction fences local publication against unpair/re-pair; no network
call holds that lock. Only a CSR, never a private key, crosses the device API.
See docs/plans/remote-direct-cert-client.md for the persistence/caller contract.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from config import paths
from config.atomic_io import write_atomic
from config.v2_config import V2Config, config_file_lock
from vibe import remote_access
from vibe.direct_ingress import _load_certificate_context, normalize_ingress_hostname

logger = logging.getLogger(__name__)
_MIN_POLL = 30.0
_MAX_BACKOFF = 3600.0
_CURRENT_INTERVAL = 6 * 3600.0
_MAX_DOCUMENT = 256 * 1024
_FINGERPRINT = re.compile(r"[A-Za-z0-9_-]{43}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_ORDER_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_PEM_CERT = re.compile(rb"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----")
_ORDER_STATES = frozenset({
    "new", "queued", "pending", "order_created", "challenge_ready", "dns_presented",
    "dns_visible", "validating", "finalizing", "certificate_ready", "issued",
    "issued_cleanup_pending", "failed", "failed_cleanup_pending", "expired", "cancelled",
})
_TERMINAL_FAILURE = frozenset({"failed", "failed_cleanup_pending", "expired", "cancelled"})
_ISSUED = frozenset({"issued", "issued_cleanup_pending"})
_ERROR_CODES = frozenset({
    "invalid_device_secret", "instance_credential_mismatch", "instance_not_found",
    "instance_inactive", "cloud_policy_unavailable", "direct_access_not_allowed",
    "csr_malformed", "csr_bad_signature", "csr_wrong_key_type", "csr_san_required",
    "csr_san_count", "csr_san_wrong_hostname", "certificate_order_in_progress",
    "certificate_budget_queued", "certificate_key_rotation_rate_limited",
    "dns_challenge_delegated", "dns_challenge_delegation_check_failed",
    "dns_challenge_visibility_timeout", "acme_rate_limited", "acme_authorization_invalid",
    "acme_order_invalid", "certificate_leaf_mismatch", "certificate_public_key_mismatch",
    "certificate_order_expired", "certificate_cleanup_pending", "unavailable",
    "backend_unavailable", "backend_rejected", "invalid_response", "invalid_certificate",
    "local_state_unavailable", "order_failed",
})
_STOP_ERRORS = frozenset({
    "acme_authorization_invalid", "acme_order_invalid", "certificate_order_expired",
    "dns_challenge_delegated", "dns_challenge_delegation_check_failed",
})


@dataclass(frozen=True)
class CertificateResult:
    """Safe local handoff, not a Web payload. Paths contain no credential bytes.

    ``ready`` only means a currently valid local pair exists; I3 must also hold
    fresh permission. ``reload_required`` signals a newly selected leaf/pair.
    After restart the caller starts its ingress from the returned file paths.
    """

    status: str
    ready: bool = False
    reload_required: bool = False
    certificate_file: str | None = None
    key_file: str | None = None
    public_key_fingerprint: str | None = None
    certificate_not_after: str | None = None
    next_poll_at: float | None = None
    last_error_code: str | None = None


@dataclass
class _State:
    schema_version: int
    instance_id: str
    hostname: str
    pairing_fingerprint: str
    public_key_fingerprint: str
    csr_pem: str
    purpose: str = "first_issuance"
    order_id: str | None = None
    status: str = "new"
    next_poll_at: float = 0.0
    order_not_before: float = 0.0
    next_current_at: float = 0.0
    current_not_before: float = 0.0
    request_not_before: float = 0.0
    certificate_not_after: str | None = None
    last_error_code: str | None = None
    active_fingerprint: str | None = None
    active_certificate: str | None = None
    blocked: bool = False
    failures: int = 0
    request_id: str = ""
    active_csr_pem: str | None = None
    conflict_order_id: str | None = None
    cancelled_order_id: str | None = None


def _directory() -> Path:
    return paths.get_state_dir() / "direct"


def _key_path(fingerprint: str) -> Path:
    if not _FINGERPRINT.fullmatch(fingerprint):
        raise ValueError("invalid fingerprint")
    return _directory() / f"key-{fingerprint}.pem"


def _chain_path(digest: str) -> Path:
    if not _DIGEST.fullmatch(digest):
        raise ValueError("invalid certificate identifier")
    return _directory() / f"chain-{digest}.pem"


def _private_directory() -> None:
    directory = _directory()
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or (os.name == "posix" and info.st_uid != os.getuid()):
        raise ValueError("unsafe certificate directory")
    # Never widen existing permissions. New files are 0600 before publication.
    if os.name == "posix" and info.st_mode & 0o077:
        directory.chmod(stat.S_IMODE(info.st_mode) & 0o700)


def _read_private(path: Path, limit: int = _MAX_DOCUMENT) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.getuid())):
        raise ValueError("unsafe certificate file")
    if info.st_size > limit:
        raise ValueError("oversized certificate file")
    with path.open("rb") as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise ValueError("oversized certificate file")
    return value


def _discard() -> None:
    directory = _directory()
    if directory.is_symlink():
        directory.unlink()
    elif directory.exists():
        shutil.rmtree(directory)


def discard_direct_certificates() -> None:
    """Best-effort retirement under the pairing/config lock; absent is a no-op.

    Called by the pairing mutation hook. Failures never break pairing/tunnel
    startup, and carry no path, raw exception, credential, or certificate bytes.
    """
    if not _directory().exists() and not _directory().is_symlink():
        return
    try:
        with config_file_lock():
            _discard()
    except Exception:
        logger.warning("Direct certificate retirement failed; direct access must remain unavailable")


def _binding(config: V2Config) -> tuple[str, str, str] | None:
    cloud = config.remote_access.vibe_cloud
    if not cloud.is_runtime_paired() or not cloud.backend_url:
        return None
    url = urlsplit(cloud.public_url)
    if url.scheme != "https" or url.username or url.password or url.port not in (None, 443):
        raise ValueError("invalid paired hostname")
    hostname = normalize_ingress_hostname(url.hostname or "")
    # A credential change is a new pairing even if hostname and ID are reused.
    identity = remote_access._pairing_identity_from_cloud(cloud)
    return str(cloud.instance_id), hostname, remote_access._pairing_identity_fingerprint(identity)


def _public_fingerprint(key: ec.EllipticCurvePublicKey) -> str:
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.urlsafe_b64encode(hashlib.sha256(der).digest()).decode("ascii").rstrip("=")


def _local_key(fingerprint: str) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(_read_private(_key_path(fingerprint)), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("invalid certificate key")
    if _public_fingerprint(key.public_key()) != fingerprint:
        raise ValueError("certificate key changed")
    return key


def _make_csr(key: ec.EllipticCurvePrivateKey, hostname: str) -> str:
    csr = (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([]))
           .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
           .sign(key, hashes.SHA256()))
    return csr.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _new_key(state: _State) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    state.public_key_fingerprint = _public_fingerprint(key.public_key())
    state.csr_pem = _make_csr(key, state.hostname)
    write_atomic(_key_path(state.public_key_fingerprint), key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))


def _save(state: _State) -> None:
    write_atomic(_directory() / "state.json", json.dumps(asdict(state), sort_keys=True) + "\n")


def _timestamp(value: object) -> float:
    if not isinstance(value, str):
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp missing timezone")
    return parsed.timestamp()


def _load() -> _State | None:
    directory = _directory()
    if not directory.exists() and not directory.is_symlink():
        return None
    try:
        _private_directory()
        payload = json.loads(_read_private(directory / "state.json"))
        if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
            raise ValueError("unsupported state")
        state = _State(**{field.name: payload[field.name] for field in fields(_State) if field.name in payload})
        if (not isinstance(state.instance_id, str) or not state.instance_id
                or normalize_ingress_hostname(state.hostname) != state.hostname
                or not _DIGEST.fullmatch(state.pairing_fingerprint)
                or state.status not in _ORDER_STATES or state.purpose not in {"first_issuance", "key_rotation"}
                or type(state.blocked) is not bool or type(state.failures) is not int or not 0 <= state.failures <= 8
                or any(not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0
                       for value in (state.next_poll_at, state.order_not_before, state.next_current_at, state.current_not_before, state.request_not_before))
                or not isinstance(state.request_id, str)
                or (state.order_id is not None and not _ORDER_ID.fullmatch(state.order_id))
                or (state.last_error_code is not None and state.last_error_code not in _ERROR_CODES)
                or any(value is not None and (not isinstance(value, str) or not _ORDER_ID.fullmatch(value))
                       for value in (state.conflict_order_id, state.cancelled_order_id))):
            raise ValueError("invalid state")
        key = _local_key(state.public_key_fingerprint)
        csr = x509.load_pem_x509_csr(state.csr_pem.encode("ascii"))
        if (not csr.is_signature_valid or csr.public_key() != key.public_key()
                or list(csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value) != [x509.DNSName(state.hostname)]):
            raise ValueError("invalid persisted CSR")
        if (state.active_fingerprint is None) != (state.active_certificate is None):
            raise ValueError("incomplete active pair")
        if state.active_fingerprint is not None:
            _local_key(state.active_fingerprint)
            _read_private(_chain_path(state.active_certificate))
            _timestamp(state.certificate_not_after)
            active_csr = x509.load_pem_x509_csr(state.active_csr_pem.encode("ascii"))
            if (not active_csr.is_signature_valid
                    or _public_fingerprint(active_csr.public_key()) != state.active_fingerprint
                    or list(active_csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value) != [x509.DNSName(state.hostname)]):
                raise ValueError("invalid active CSR")
        return state
    except Exception:
        # Neither unknown versions nor corruption may break service startup or
        # resurrect a key whose pairing/CSR cannot be established.
        logger.warning("Discarding invalid or unsupported direct certificate state")
        _discard()
        return None


def _validate_chain(pem: object, state: _State, fingerprint: str, now: float) -> tuple[bytes, str, str]:
    if not isinstance(pem, str) or len(pem) > _MAX_DOCUMENT:
        raise ValueError("invalid certificate")
    data = pem.encode("ascii")
    blocks = _PEM_CERT.findall(data)
    # The parser can ignore surrounding text; explicitly refuse trailing PEM
    # fragments, private keys, and garbage rather than silently truncate a chain.
    if not blocks or _PEM_CERT.sub(b"", data).strip():
        raise ValueError("invalid certificate")
    chain = [x509.load_pem_x509_certificate(block) for block in blocks]
    leaf = chain[0]
    if (leaf.public_key() != _local_key(fingerprint).public_key()
            or list(leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value) != [x509.DNSName(state.hostname)]
            or not leaf.not_valid_before_utc.timestamp() <= now < leaf.not_valid_after_utc.timestamp()):
        raise ValueError("invalid certificate")
    canonical = b"".join(cert.public_bytes(serialization.Encoding.PEM) for cert in chain)
    digest = hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER)).hexdigest()
    return canonical, digest, leaf.not_valid_after_utc.isoformat()


def _active_usable(state: _State, now: float) -> bool:
    if state.active_fingerprint is None:
        return False
    try:
        path = _chain_path(state.active_certificate)
        _, digest, _ = _validate_chain(_read_private(path).decode("ascii"), state, state.active_fingerprint, now)
        if digest != state.active_certificate:
            return False
        _load_certificate_context(str(path), str(_key_path(state.active_fingerprint)))
        return True
    except Exception:
        return False


def _next_work(state: _State) -> tuple[bool | None, float]:
    """Independent order and current-leaf deadlines, with one shared HTTP lease."""
    order_due = max(state.next_poll_at, state.order_not_before) if state.status not in _TERMINAL_FAILURE | _ISSUED else math.inf
    current_due = max(state.next_current_at, state.current_not_before) if state.active_fingerprint or state.status in _ISSUED else math.inf
    if math.isinf(min(order_due, current_due)):
        return None, max(state.next_poll_at, state.order_not_before, state.request_not_before)
    return current_due < order_due, max(min(order_due, current_due), state.request_not_before)


def _current_deadline(state: _State, now: float) -> float:
    if state.certificate_not_after is None:
        return now + 300
    return min(now + _CURRENT_INTERVAL, max(now + _MIN_POLL, _timestamp(state.certificate_not_after) - 300))


def _preflight_chain(canonical: bytes, fingerprint: str) -> None:
    """Use ingress's actual SSL loader before replacing any selected file."""
    staged = _directory() / f".chain-check-{uuid.uuid4().hex}.pem"
    try:
        write_atomic(staged, canonical)
        _load_certificate_context(str(staged), str(_key_path(fingerprint)))
    finally:
        staged.unlink(missing_ok=True)


def _result(state: _State, now: float, *, changed: bool = False) -> CertificateResult:
    ready = _active_usable(state, now)
    return CertificateResult(
        status="blocked" if state.blocked else state.status,
        ready=ready and not state.blocked, reload_required=changed and ready and not state.blocked,
        certificate_file=str(_chain_path(state.active_certificate)) if ready and not state.blocked else None,
        key_file=str(_key_path(state.active_fingerprint)) if ready and not state.blocked else None,
        public_key_fingerprint=state.active_fingerprint or state.public_key_fingerprint,
        certificate_not_after=state.certificate_not_after,
        next_poll_at=_next_work(state)[1], last_error_code=state.last_error_code,
    )


def _clean_unreferenced(state: _State) -> None:
    keep = {"state.json", _key_path(state.public_key_fingerprint).name}
    if state.active_fingerprint:
        keep.add(_key_path(state.active_fingerprint).name)
        keep.add(_chain_path(state.active_certificate).name)
    for entry in _directory().iterdir():
        if entry.name not in keep and entry.is_file():
            entry.unlink()


class DirectCertificateClient:
    """One bounded request per poll; construction has no side effects.

    ``allowed`` must combine owner opt-in and fresh backend policy. After a
    server denial, ordinary ``allowed=True`` polls stay silent; ``resume=True``
    is the caller's explicit fresh-policy notification. ``retry=True`` retries
    a terminal failed order, and ``rotate_key=True`` requests a new key while
    retaining the active pair. These are commands, not recurring settings.
    """

    def __init__(self) -> None:
        self._local_retry_at = 0.0

    def poll(self, *, allowed: bool, resume: bool = False, retry: bool = False, rotate_key: bool = False) -> CertificateResult:
        now = time.time()
        if now < self._local_retry_at:
            return CertificateResult("unavailable", next_poll_at=self._local_retry_at, last_error_code="local_state_unavailable")
        try:
            return self._poll(now, allowed=allowed, resume=resume, retry=retry, rotate_key=rotate_key)
        except Exception:
            # Do not stringify exceptions: HTTP/crypto/filesystem errors can
            # contain credentials or attacker-controlled response material.
            self._local_retry_at = time.time() + 60
            return CertificateResult("unavailable", next_poll_at=self._local_retry_at, last_error_code="local_state_unavailable")

    def _poll(self, now: float, *, allowed: bool, resume: bool, retry: bool, rotate_key: bool) -> CertificateResult:
        with config_file_lock():
            config = V2Config.load()
            state = _load()
            identity = remote_access._pairing_identity_from_cloud(config.remote_access.vibe_cloud)
            if state and state.pairing_fingerprint != remote_access._pairing_identity_fingerprint(identity):
                _discard()
                state = None
            binding = _binding(config)
            if binding is None:
                _discard()
                return CertificateResult("unpaired")
            if state and (state.instance_id, state.hostname, state.pairing_fingerprint) != binding:
                _discard()
                state = None
            if not allowed:
                return CertificateResult("disabled")
            if state is None:
                _private_directory()
                state = _State(1, *binding, public_key_fingerprint="", csr_pem="")
                _new_key(state)
                _save(state)
            # Remove unpublished files from an interrupted install/rotation
            # before allocating another key; retain at most active + pending.
            _clean_unreferenced(state)
            command = False
            if state.blocked:
                if not resume:
                    return _result(state, now)
                state.blocked = False
                state.last_error_code = None
                # Fresh permission clears only the denial latch. It cannot
                # bypass either endpoint's deadline or an outstanding backoff.
                command = True
            if retry and state.status in _TERMINAL_FAILURE:
                state.status, state.order_id = "new", None
                command = True
            if rotate_key and state.active_fingerprint == state.public_key_fingerprint and state.status in _ISSUED:
                _new_key(state)
                state.purpose, state.status, state.order_id = "key_rotation", "new", None
                if state.last_error_code is None:
                    state.next_poll_at = now
                command = True
            if command:
                _save(state)
            # Readiness is not a scheduling event: repeatedly observing an
            # expired/corrupt leaf must not erase a prior recovery deadline.
            fetch_current, due_at = _next_work(state)
            if fetch_current is None or now < due_at:
                return _result(state, now)
            if fetch_current:
                method, suffix, payload = "GET", "certificates/current", None
            elif state.conflict_order_id:
                method, suffix, payload = "POST", f"certificates/orders/{quote(state.conflict_order_id, safe='')}/cancel", None
            elif state.order_id:
                method, suffix, payload = "POST", f"certificates/orders/{quote(state.order_id, safe='')}/advance", None
            else:
                method, suffix = "POST", "certificates/orders"
                payload = {"csr_pem": state.csr_pem, "purpose": state.purpose}
            # Persist intent and a bounded lease before HTTP: lost replies and
            # simultaneous clients cannot cause an immediate retry after restart.
            state.request_id = uuid.uuid4().hex
            state.failures = min(8, state.failures + 1)
            state.request_not_before = now + min(_MAX_BACKOFF, 60 * 2 ** (state.failures - 1))
            if fetch_current:
                state.next_current_at = state.request_not_before
            else:
                state.next_poll_at = state.request_not_before
            _save(state)
            request_id = state.request_id

        response, error, http_status = None, None, 0
        try:
            response = remote_access._device_json_request(config, method, suffix, payload, timeout=8.0)
        except remote_access.BackendRequestError as exc:
            error, http_status = exc.payload, exc.status
        except Exception:
            error = {"error": "backend_unavailable"}

        # Sleep or clock changes can occur during HTTP; validate at publication,
        # not against a timestamp captured before the request.
        now = time.time()
        with config_file_lock():
            if _binding(V2Config.load()) != binding:
                # Pairing's hook retires the old generation. Never remove a new
                # pairing's files or recreate an unpaired directory on late HTTP.
                return CertificateResult("pairing_changed")
            current = _load()
            if current is None or current.request_id != request_id:
                return CertificateResult("superseded")
            state = current
            # Rebase fallback backoff after a long sleep/slow request, too.
            state.request_not_before = max(state.request_not_before, now + min(_MAX_BACKOFF, 60 * 2 ** (state.failures - 1)))
            if fetch_current:
                state.next_current_at = max(state.next_current_at, state.request_not_before)
            else:
                state.next_poll_at = max(state.next_poll_at, state.request_not_before)
            changed = False
            if error is not None:
                self._error(state, error, http_status, now, current=fetch_current)
            else:
                try:
                    if suffix.endswith("/cancel"):
                        # Cancellation is idempotent; a lost response retries the
                        # same cancellation. Start-order remains the admission
                        # gate while backend cleanup/settling still owns the name.
                        state.cancelled_order_id = state.conflict_order_id
                        state.conflict_order_id = None
                        state.next_poll_at = now + _MIN_POLL
                        if response.get("next_poll_at") is not None:
                            state.next_poll_at = max(state.next_poll_at, _timestamp(response["next_poll_at"]))
                        state.last_error_code = None
                        state.failures = 0
                        state.request_not_before = 0.0
                    else:
                        changed = self._accept(state, response, suffix == "certificates/current", now)
                except Exception:
                    state.last_error_code = "invalid_response"
            _save(state)
            # Cleanup follows the manifest commit; failure does not unpublish
            # the usable pair. A later successful poll retries it.
            try:
                _clean_unreferenced(state)
            except OSError:
                pass
            return _result(state, now, changed=changed)

    @staticmethod
    def _error(state: _State, payload: object, http_status: int, now: float, *, current: bool) -> None:
        payload = payload if isinstance(payload, dict) else {}
        code = payload.get("error")
        code = code if isinstance(code, str) and code in _ERROR_CODES else "backend_rejected"
        if http_status == 404 and code not in {"instance_not_found"}:
            code = "unavailable"  # Older backends: quiet, hourly capability retry.
            state.request_not_before = max(state.request_not_before, now + _MAX_BACKOFF)
        if code == "direct_access_not_allowed":
            state.blocked = True
        if not current and code in _STOP_ERRORS:
            state.status = "failed"
        state.last_error_code = code
        if not current and code.startswith("csr_"):
            # A definitive early profile refusal admits no order. Unlike a lost
            # response, it permits rebuilding the CSR, retaining the local key.
            state.csr_pem = _make_csr(_local_key(state.public_key_fingerprint), state.hostname)
            state.order_id, state.status = None, "new"
        if not current and code == "certificate_order_in_progress":
            order_id, fingerprint = payload.get("order_id"), payload.get("public_key_fingerprint")
            if (isinstance(order_id, str) and _ORDER_ID.fullmatch(order_id)
                    and isinstance(fingerprint, str) and _FINGERPRINT.fullmatch(fingerprint)):
                if fingerprint in {state.public_key_fingerprint, state.active_fingerprint}:
                    if fingerprint != state.public_key_fingerprint and state.active_csr_pem:
                        state.public_key_fingerprint = fingerprint
                        state.csr_pem = state.active_csr_pem
                    state.order_id, state.status = order_id, "pending"
                    state.conflict_order_id = None
                elif order_id != state.cancelled_order_id:
                    state.conflict_order_id = order_id
            else:
                state.last_error_code = "invalid_response"
            if payload.get("next_poll_at") is not None:
                try:
                    state.next_poll_at = max(state.next_poll_at, _timestamp(payload["next_poll_at"]))
                except (ValueError, TypeError, OverflowError):
                    pass
        if payload.get("retry_at") is not None:
            try:
                retry_at = _timestamp(payload["retry_at"])
                if code.startswith(("csr_", "certificate_", "dns_", "acme_")):
                    # These are operation-specific refusals, not a credential
                    # or transport outage. A terminal/queued order must not
                    # prevent reading a renewal for the active key (or vice versa).
                    if current:
                        state.current_not_before = max(state.current_not_before, retry_at)
                    else:
                        state.order_not_before = max(state.order_not_before, retry_at)
                else:
                    state.request_not_before = max(state.request_not_before, retry_at)
            except (ValueError, TypeError, OverflowError):
                pass

    @staticmethod
    def _accept(state: _State, response: object, current: bool, now: float) -> bool:
        if not isinstance(response, dict):
            raise ValueError("invalid response")
        if current:
            if response.get("direct_access_allowed") is False:
                state.blocked, state.last_error_code = True, "direct_access_not_allowed"
                return False
            if response.get("direct_access_allowed") is not True:
                raise ValueError("missing permission")
            status, order_id = state.status, state.order_id
            next_poll = state.next_poll_at
        else:
            status, order_id = response.get("status"), response.get("order_id")
            if (status not in _ORDER_STATES - {"new"} or not isinstance(order_id, str)
                    or not _ORDER_ID.fullmatch(order_id) or (state.order_id and state.order_id != order_id)
                    or response.get("hostname") != state.hostname):
                raise ValueError("invalid order")
            next_poll = now + _MIN_POLL
            if response.get("next_poll_at") is not None:
                next_poll = max(next_poll, _timestamp(response["next_poll_at"]))
        pem = response.get("certificate_pem")
        fingerprint = response.get("public_key_fingerprint")
        accepted_keys = {state.public_key_fingerprint}
        if current and state.active_fingerprint:
            accepted_keys.add(state.active_fingerprint)
        if fingerprint not in accepted_keys and not (current and pem is None and fingerprint is None):
            raise ValueError("wrong public key metadata")
        # A recognizable completed order must not become another start-order
        # attempt merely because its leaf was invalid. Retry via current instead.
        state.status, state.order_id = status, order_id
        changed = False
        if pem is not None:
            try:
                canonical, digest, expiry = _validate_chain(pem, state, fingerprint, now)
                _preflight_chain(canonical, fingerprint)
            except Exception:
                state.last_error_code = "invalid_certificate"
                return False
            changed = digest != state.active_certificate or not _active_usable(state, now)
            if changed:
                write_atomic(_chain_path(digest), canonical)
            if current and fingerprint == state.public_key_fingerprint and fingerprint != state.active_fingerprint:
                # The backend may finish the pending rotation while we fetch
                # current; this is not a client-initiated renewal.
                state.status = "issued"
            state.active_fingerprint, state.active_certificate = fingerprint, digest
            state.certificate_not_after = expiry
            if fingerprint == state.public_key_fingerprint:
                state.active_csr_pem = state.csr_pem
        if current:
            state.next_current_at = _current_deadline(state, now)
            state.current_not_before = max(state.current_not_before, state.next_current_at)
            if pem is None:
                state.next_current_at = min(state.next_current_at, now + 300)
        else:
            state.next_poll_at = next_poll
            if changed or state.status in _ISSUED:
                state.next_current_at = _current_deadline(state, now)
        state.failures, state.request_not_before = 0, 0.0
        state.last_error_code = "order_failed" if state.status in _TERMINAL_FAILURE else None
        return changed
