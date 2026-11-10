"""Certificate-client boundary: fake paired backend, real local keys/files/TLS.

These contracts were not reachable through the Phase 0 ingress tests: issuance,
restart recovery, scheduling, retirement, and atomic selection precede ingress.
Each test names the regression it protects. No CA, cloud API, or real home is used.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import ssl
import stat
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from config import paths
from config.v2_config import V2Config
from vibe import direct_certificates as certificates
from vibe import remote_access
from vibe.direct_certificates import DirectCertificateClient, discard_direct_certificates
from vibe.direct_ingress import DirectIngress

HOST = "hermetic-app.avibe.bot"
_DEVICE_REQUEST = remote_access._device_json_request


def fingerprint(public_key):
    return base64.urlsafe_b64encode(hashlib.sha256(public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )).digest()).decode().rstrip("=")


class Backend:
    """Only the documented wire contract; no replacement of client logic."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.orders = {}
        self.current = None
        self.error = None
        self.lose_start = False
        self.ca_key = ec.generate_private_key(ec.SECP256R1())
        self.ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Hermetic certificate test CA")])
        now = datetime.fromtimestamp(clock.now, timezone.utc)
        self.ca = (x509.CertificateBuilder().subject_name(self.ca_name).issuer_name(self.ca_name)
                   .public_key(self.ca_key.public_key()).serial_number(1)
                   .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=365))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.ca_key.public_key()), critical=False)
                   .sign(self.ca_key, hashes.SHA256()))

    def issue(self, public_key=None, *, sans=None, expired=False, future=False):
        if public_key is None:
            public_key = next(reversed(self.orders.values()))["csr"].public_key()
        now = datetime.fromtimestamp(self.clock.now, timezone.utc)
        start = now + timedelta(days=1) if future else now - timedelta(days=2)
        end = now - timedelta(days=1) if expired else now + timedelta(days=14)
        leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)]))
                .issuer_name(self.ca_name).public_key(public_key).serial_number(x509.random_serial_number())
                .not_valid_before(start).not_valid_after(end)
                .add_extension(x509.SubjectAlternativeName(sans or [x509.DNSName(HOST)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca_key.public_key()), critical=False)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
                .sign(self.ca_key, hashes.SHA256()))
        return {
            "certificate_pem": (leaf.public_bytes(serialization.Encoding.PEM) + self.ca.public_bytes(serialization.Encoding.PEM)).decode(),
            "public_key_fingerprint": fingerprint(public_key),
            "certificate_not_after": leaf.not_valid_after_utc.isoformat(),
            "direct_access_allowed": True,
        }

    def __call__(self, config, method, suffix, payload=None, **kwargs):
        assert config.remote_access.vibe_cloud.instance_id == "instance-one"
        assert kwargs["timeout"] == 8.0
        self.calls.append((method, suffix, copy.deepcopy(payload)))
        if self.error:
            if callable(self.error):
                return self.error(config, method, suffix, payload)
            raise self.error
        if suffix == "certificates/current":
            assert method == "GET" and payload is None
            return copy.deepcopy(self.current)
        if suffix == "certificates/orders":
            assert method == "POST"
            assert set(payload) == {"csr_pem", "purpose"}
            csr = x509.load_pem_x509_csr(payload["csr_pem"].encode())
            assert csr.is_signature_valid
            assert isinstance(csr.public_key().curve, ec.SECP256R1)
            assert list(csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value) == [x509.DNSName(HOST)]
            fp = fingerprint(csr.public_key())
            if fp not in self.orders:
                self.orders[fp] = {"csr": csr, "pem": payload["csr_pem"], "id": f"certord_{len(self.orders) + 1}"}
            assert payload["csr_pem"] == self.orders[fp]["pem"], "restart/lost reply changed the CSR"
            if self.lose_start:
                self.lose_start = False
                raise TimeoutError("synthetic lost start response")
            return {"order_id": self.orders[fp]["id"], "status": "queued", "hostname": HOST,
                    "public_key_fingerprint": fp, "certificate_pem": None, "certificate_not_after": None,
                    "next_poll_at": datetime.fromtimestamp(self.clock.now + 120, timezone.utc).isoformat()}
        assert suffix.endswith("/advance") and method == "POST" and payload is None
        order = next(order for order in self.orders.values() if order["id"] == suffix.split("/")[-2])
        self.current = self.issue(order["csr"].public_key())
        return {**self.current, "order_id": order["id"], "hostname": HOST, "status": "issued", "next_poll_at": None}


@pytest.fixture
def setup(monkeypatch):
    """Real config under conftest's temporary AVIBE_HOME; only HTTP is faked."""
    config = V2Config.default()
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.instance_id = "instance-one"
    cloud.instance_secret = "test-device-secret-not-a-real-credential"
    cloud.public_url = f"https://{HOST}"
    cloud.backend_url = "https://control.invalid"
    config.save()
    clock = SimpleNamespace(now=float(int(time.time())))
    monkeypatch.setattr(certificates, "time", SimpleNamespace(time=lambda: clock.now))
    backend = Backend(clock)
    monkeypatch.setattr(remote_access, "_device_json_request", backend)
    return SimpleNamespace(client=DirectCertificateClient(), clock=clock, backend=backend, config=config)


def due(env, result):
    env.clock.now = result.next_poll_at
    return env.client.poll(allowed=True)


def installed(env):
    first = env.client.poll(allowed=True)
    result = due(env, first)
    assert result.ready and result.reload_required
    return result


def test_backend_shared_spki_fingerprint_vector():
    """Pin the cross-language wire fingerprint, not merely a self-consistent hash.

    Public-only vector from avibe-backend#325, docs/direct-certificates.md §2.1.
    """
    key = serialization.load_pem_public_key(b"""-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEDwM6UHPKK/wooGd23NPde6d+Jw66
AwBoSuuvp3E1xcvJVobl+aNNp6tMByJ5TupmdROp/Tg2kCYAZ0eo85R0QA==
-----END PUBLIC KEY-----
""")
    assert certificates._public_fingerprint(key) == "t25CGk8zgvqwtE3DKL27jeWKBiNqUvVC7CZpPRChs98"


def test_first_issuance_and_restart_reuse_exact_csr_and_never_send_a_key(setup, caplog):
    """A lost POST response/restart must not duplicate the key or signing request."""
    env = setup
    env.backend.lose_start = True
    first = env.client.poll(allowed=True)
    assert not first.ready and first.last_error_code == "backend_unavailable"
    env.client = DirectCertificateClient()
    queued = due(env, first)
    assert len(env.backend.orders) == 1
    before = len(env.backend.calls)
    env.client = DirectCertificateClient()
    assert not env.client.poll(allowed=True).ready
    assert len(env.backend.calls) == before
    result = due(env, queued)
    assert result.ready and result.reload_required
    key = Path(result.key_file).read_text()
    assert key.startswith("-----BEGIN PRIVATE KEY-----")
    assert key not in caplog.text
    assert key not in json.dumps([asdict(first), asdict(queued), asdict(result), env.backend.calls])
    state = (paths.get_state_dir() / "direct/state.json").read_text()
    assert key not in state and env.config.remote_access.vibe_cloud.instance_secret not in state
    assert result.public_key_fingerprint == next(iter(env.backend.orders))
    if os.name == "posix":
        assert stat.S_IMODE(Path(result.key_file).stat().st_mode) == 0o600
        assert stat.S_IMODE(Path(result.key_file).parent.stat().st_mode) == 0o700
    assert env.backend.calls[-1][0:2] == ("POST", "certificates/orders/certord_1/advance")


@pytest.mark.parametrize("bad", ["key", "san", "extra_san", "expired", "future", "garbage", "broken_intermediate", "private_key_suffix"])
def test_invalid_chain_keeps_previous_pair_byte_for_byte(setup, bad):
    """No key/name/validity/parse failure may replace a previously usable pair."""
    env = setup
    previous = installed(env)
    key_before, chain_before = Path(previous.key_file).read_bytes(), Path(previous.certificate_file).read_bytes()
    kwargs = {}
    if bad == "key":
        kwargs["public_key"] = ec.generate_private_key(ec.SECP256R1()).public_key()
    if bad == "san":
        kwargs["sans"] = [x509.DNSName("wrong-app.avibe.bot")]
    if bad == "extra_san":
        kwargs["sans"] = [x509.DNSName(HOST), x509.UniformResourceIdentifier("https://other.invalid")]
    if bad in {"expired", "future"}:
        kwargs[bad] = True
    candidate = env.backend.issue(**kwargs)
    # Correct envelope metadata must not substitute for checking the actual key.
    candidate["public_key_fingerprint"] = previous.public_key_fingerprint
    if bad == "garbage":
        candidate["certificate_pem"] = "not a certificate"
    if bad == "broken_intermediate":
        candidate["certificate_pem"] += "\n-----BEGIN CERTIFICATE-----\nYmFk\n-----END CERTIFICATE-----\n"
    if bad == "private_key_suffix":
        candidate["certificate_pem"] += key_before.decode()
    env.backend.current = candidate
    result = due(env, previous)
    assert not result.reload_required
    assert result.last_error_code == "invalid_certificate"
    assert Path(previous.key_file).read_bytes() == key_before
    assert Path(previous.certificate_file).read_bytes() == chain_before
    assert result.key_file == previous.key_file and result.certificate_file == previous.certificate_file


def test_current_renewal_is_reload_once_without_new_order(setup):
    """Backend-owned same-key renewal changes the active leaf, not the local key."""
    env = setup
    first = installed(env)
    same = due(env, first)
    assert same.ready and not same.reload_required
    env.backend.current = env.backend.issue()
    renewed = due(env, same)
    assert renewed.ready and renewed.reload_required
    assert renewed.key_file == first.key_file and renewed.certificate_file != first.certificate_file
    assert not Path(first.certificate_file).exists()
    assert not due(env, renewed).reload_required
    assert len(env.backend.orders) == 1
    assert [method for method, suffix, _ in env.backend.calls if suffix == "certificates/current"] == ["GET"] * 3


@pytest.mark.parametrize("code,status", [
    ("invalid_device_secret", 401), ("instance_credential_mismatch", 409),
    ("instance_inactive", 403), ("instance_not_found", 404),
    ("cloud_policy_unavailable", 503), ("missing_route", 404), ("timeout", 0),
    ("certificate_key_rotation_rate_limited", 429),
])
def test_backend_failures_are_quiet_bounded_and_resumable(setup, caplog, code, status):
    """Credential failures, old backends and transport errors cannot escape or spin."""
    env = setup
    env.backend.error = TimeoutError("sensitive synthetic detail") if not status else remote_access.BackendRequestError(status, {
        "error": code, "detail": "sensitive synthetic detail",
        "retry_at": datetime.fromtimestamp(env.clock.now + 900, timezone.utc).isoformat(),
    })
    result = env.client.poll(allowed=True)
    assert not result.ready and not result.reload_required
    assert result.next_poll_at >= env.clock.now + (60 if not status else 900)
    assert result.last_error_code == {"missing_route": "unavailable", "timeout": "backend_unavailable"}.get(code, code)
    env.client = DirectCertificateClient()
    for _ in range(3):
        env.client.poll(allowed=True)
    assert len(env.backend.calls) == 1
    assert "sensitive synthetic detail" not in caplog.text + repr(result)
    if not status:
        for interval in (120, 240, 480, 960, 1920, 3600, 3600):
            env.clock.now = result.next_poll_at
            result = env.client.poll(allowed=True)
            assert result.next_poll_at == env.clock.now + interval
    env.backend.error = None
    recovered = due(env, result)
    assert recovered.status == "queued"


@pytest.mark.parametrize("source", ["error", "current"])
def test_policy_denial_stays_blocked_across_restart_until_explicit_permission(setup, source):
    """A stale allowed=True input must not override backend denial on every tick."""
    env = setup
    if source == "current":
        previous = installed(env)
        env.backend.current["direct_access_allowed"] = False
        result = due(env, previous)
    else:
        env.backend.error = remote_access.BackendRequestError(403, {"error": "direct_access_not_allowed"})
        result = env.client.poll(allowed=True)
    assert not result.ready and result.status == "blocked"
    count = len(env.backend.calls)
    env.client = DirectCertificateClient()
    env.clock.now += 86400
    assert not env.client.poll(allowed=True).ready
    assert len(env.backend.calls) == count
    env.backend.error = None
    if env.backend.current:
        env.backend.current["direct_access_allowed"] = True
    env.client.poll(allowed=True, resume=True)
    assert len(env.backend.calls) == count + 1


def test_no_request_or_key_without_opt_in_and_no_poll_before_server_deadline(setup):
    """Creation is opt-in only; next_poll_at survives restart and clock rollback."""
    env = setup
    assert env.client.poll(allowed=False).status == "disabled"
    assert not (paths.get_state_dir() / "direct").exists() and not env.backend.calls
    result = env.client.poll(allowed=True)
    assert result.next_poll_at == env.clock.now + 120
    env.client = DirectCertificateClient()
    env.clock.now = result.next_poll_at - 0.01
    assert env.client.poll(allowed=True).status == "queued"
    assert len(env.backend.calls) == 1
    assert due(env, result).ready


def test_rotation_keeps_active_pair_until_new_pair_commits_then_deletes_old(setup):
    """Pending rotation must never pair an old leaf with a new private key."""
    env = setup
    old = installed(env)
    queued = env.client.poll(allowed=True, rotate_key=True)
    assert queued.ready and not queued.reload_required and queued.key_file == old.key_file
    assert len(list(Path(old.key_file).parent.glob("key-*.pem"))) == 2
    assert env.backend.calls[-1][2]["purpose"] == "key_rotation"
    env.client = DirectCertificateClient()
    new = due(env, queued)
    assert new.ready and new.reload_required and new.key_file != old.key_file
    assert not Path(old.key_file).exists() and not Path(old.certificate_file).exists()
    assert len(list(Path(new.key_file).parent.glob("key-*.pem"))) == 1


@pytest.mark.parametrize("field,value", [
    ("instance_id", "instance-two"), ("public_url", "https://different-app.avibe.bot"),
    ("instance_secret", "replacement-test-device-secret"), ("session_secret", "replacement-test-session-secret"),
    ("enabled", False),
])
def test_pairing_change_and_unpair_discard_every_pair_through_real_config_save(setup, field, value):
    """The generic config save must retire active and pending keys, not only pair()."""
    from vibe import api

    env = setup
    old = installed(env)
    env.client.poll(allowed=True, rotate_key=True)
    assert len(list(Path(old.key_file).parent.glob("key-*.pem"))) == 2
    api.save_config({"remote_access": {"vibe_cloud": {field: value}}})
    assert not Path(old.key_file).parent.exists()
    discard_direct_certificates()  # idempotent no-op


@pytest.mark.parametrize("field,value", [("instance_id", "different"), ("public_url", "http://invalid-for-tls.test")])
def test_client_detects_out_of_band_identity_change_without_request(setup, field, value):
    """A restored/externally edited config cannot adopt another pairing's key."""
    env = setup
    old = installed(env)
    setattr(env.config.remote_access.vibe_cloud, field, value)
    env.config.save()
    assert not env.client.poll(allowed=False).ready
    assert not Path(old.key_file).exists()


@pytest.mark.parametrize("content", [b"{broken", b'{"schema_version": 999}', b'[]'])
def test_unknown_or_corrupt_state_is_discarded_with_no_startup_failure(setup, content, caplog):
    """Optional-file corruption/version skew must retire credentials, not crash."""
    env = setup
    old = installed(env)
    Path(old.key_file).parent.joinpath("state.json").write_bytes(content)
    assert env.client.poll(allowed=False).status == "disabled"
    assert not Path(old.key_file).parent.exists()
    assert "Discarding invalid or unsupported direct certificate state" in caplog.text


def test_late_backend_response_cannot_recreate_unpaired_files(setup):
    """An in-flight order completion loses publication authority after unpair."""
    from vibe import api

    env = setup
    def revoke(*_args):
        api.save_config({"remote_access": {"vibe_cloud": {"enabled": False}}})
        return {"untrusted": "late response"}
    env.backend.error = revoke
    result = env.client.poll(allowed=True)
    assert result.status == "pairing_changed" and not result.ready
    assert not (paths.get_state_dir() / "direct").exists()


def test_atomic_chain_write_failure_preserves_active_pair_and_sanitizes_exception(setup, monkeypatch, caplog):
    """A failed rename must not publish paths or leak exception text/key bytes."""
    env = setup
    old = installed(env)
    key = Path(old.key_file).read_bytes()
    chain = Path(old.certificate_file).read_bytes()
    env.backend.current = env.backend.issue()
    original = certificates.write_atomic
    def fail_chain(path, data, **kwargs):
        if path.name.startswith("chain-"):
            raise OSError(key.decode())
        return original(path, data, **kwargs)
    monkeypatch.setattr(certificates, "write_atomic", fail_chain)
    result = due(env, old)
    assert result.ready and not result.reload_required and result.certificate_file == old.certificate_file
    assert Path(old.key_file).read_bytes() == key and Path(old.certificate_file).read_bytes() == chain
    assert key.decode() not in caplog.text + repr(result)


def test_manifest_commit_failure_keeps_old_selection_and_recovers_on_restart(setup, monkeypatch):
    """Publishing a chain is not an install until the manifest swap succeeds."""
    env = setup
    old = installed(env)
    old_chain = Path(old.certificate_file).read_bytes()
    env.backend.current = env.backend.issue()
    original = certificates.write_atomic
    fail = True
    def fail_manifest(path, data, **kwargs):
        nonlocal fail
        if path.name == "state.json" and json.loads(data).get("active_certificate") != Path(old.certificate_file).stem.removeprefix("chain-") and fail:
            fail = False
            raise OSError("synthetic full disk")
        return original(path, data, **kwargs)
    monkeypatch.setattr(certificates, "write_atomic", fail_manifest)
    result = due(env, old)
    assert not result.ready and not result.reload_required and result.last_error_code == "local_state_unavailable"
    assert Path(old.certificate_file).read_bytes() == old_chain and Path(old.key_file).exists()
    env.client = DirectCertificateClient()
    result = due(env, result)
    assert result.ready and result.reload_required and result.key_file == old.key_file
    assert not Path(old.certificate_file).exists()


def test_second_client_observes_persisted_request_lease_without_another_request(setup):
    """Separate clients/process restarts must not concurrently advance one order."""
    env = setup
    observed = []
    def concurrent(*_args):
        observed.append(DirectCertificateClient().poll(allowed=True))
        raise TimeoutError("synthetic first request in progress")
    env.backend.error = concurrent
    result = env.client.poll(allowed=True)
    assert len(env.backend.calls) == 1 and len(observed) == 1
    assert observed[0].next_poll_at > env.clock.now and result.next_poll_at > env.clock.now


def test_key_not_in_state_migration_backup_or_config_projection(setup):
    """State backups must remain allowlisted, never recursively bundle direct/."""
    import sqlite3

    from storage.backups import create_sqlite_migration_backup
    from storage.importer import _backup_json_state
    from vibe import api

    result = installed(setup)
    key = Path(result.key_file).read_bytes()
    database = paths.get_sqlite_state_path()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE fixture(value TEXT)")
    backups = [_backup_json_state(paths.get_state_dir()), create_sqlite_migration_backup(database)]
    for backup in backups:
        assert not (backup / "direct").exists()
        for path in backup.rglob("*"):
            if path.is_file():
                assert key not in path.read_bytes()
    assert key.decode() not in json.dumps(api.config_to_payload(V2Config.load(), include_secrets=True))


@pytest.mark.parametrize("known", [True, False])
def test_conflicting_order_resumes_owned_key_or_cancels_unrecoverable_key(setup, known):
    """A conflict supplies its own scoped ID; never invent a list/lookup route."""
    env = setup
    queued = env.client.poll(allowed=True)
    conflict_fp = next(iter(env.backend.orders))
    if not known:
        discard_direct_certificates()  # loss of the prior key, not a backend reset
    else:
        # Simulate a lost response without losing the original key/CSR.
        state_path = paths.get_state_dir() / "direct/state.json"
        saved = json.loads(state_path.read_text())
        saved.update(order_id=None, status="new")
        state_path.write_text(json.dumps(saved))
    deadline = queued.next_poll_at + 600
    conflict = remote_access.BackendRequestError(409, {
        "error": "certificate_order_in_progress", "order_id": "certord_1",
        "public_key_fingerprint": conflict_fp,
        "next_poll_at": datetime.fromtimestamp(deadline, timezone.utc).isoformat(),
    })
    env.backend.error = conflict
    env.clock.now = queued.next_poll_at
    result = env.client.poll(allowed=True)
    assert result.next_poll_at == deadline
    count = len(env.backend.calls)
    env.client = DirectCertificateClient()
    env.client.poll(allowed=True)
    assert len(env.backend.calls) == count
    if known:
        env.backend.error = None
        result = due(env, result)
        assert result.ready and result.public_key_fingerprint == conflict_fp
        assert env.backend.calls[-1][1] == "certificates/orders/certord_1/advance"
    else:
        # A cancellation acknowledgement may contain a later cleanup deadline.
        env.backend.error = lambda *_: {"next_poll_at": datetime.fromtimestamp(deadline + 120, timezone.utc).isoformat()}
        cancelled = due(env, result)
        assert env.backend.calls[-1][1] == "certificates/orders/certord_1/cancel"
        assert cancelled.next_poll_at == deadline + 120
        env.backend.error = conflict
        waiting = due(env, cancelled)
        assert env.backend.calls[-1][1] == "certificates/orders"
        waiting = due(env, waiting)
        assert env.backend.calls[-1][1] == "certificates/orders", "cleanup waiting must not cancel in a loop"
        env.backend.error = None
        replacement = due(env, waiting)
        result = due(env, replacement)
        assert result.ready and result.public_key_fingerprint != conflict_fp


def test_conflict_with_still_owned_active_key_abandons_pending_rotation_safely(setup):
    """An in-flight backend renewal may own the old key when rotation is requested."""
    env = setup
    old = installed(env)
    env.backend.error = remote_access.BackendRequestError(409, {
        "error": "certificate_order_in_progress", "order_id": "certord_1",
        "public_key_fingerprint": old.public_key_fingerprint,
        "next_poll_at": datetime.fromtimestamp(env.clock.now + 300, timezone.utc).isoformat(),
    })
    result = env.client.poll(allowed=True, rotate_key=True)
    assert result.key_file == old.key_file and len(list(Path(old.key_file).parent.glob("key-*.pem"))) == 1
    env.backend.error = None
    result = due(env, result)
    assert result.ready and result.key_file == old.key_file
    assert not any(suffix.endswith("/cancel") for _, suffix, _ in env.backend.calls)


def test_terminal_failure_requires_explicit_retry_and_preserves_retry_deadline(setup):
    """CA failure cannot silently open fresh orders, including after restart."""
    env = setup
    deadline = env.clock.now + 3600
    env.backend.error = remote_access.BackendRequestError(400, {
        "error": "acme_authorization_invalid",
        "retry_at": datetime.fromtimestamp(deadline, timezone.utc).isoformat(),
    })
    result = env.client.poll(allowed=True)
    assert result.status == "failed"
    env.client = DirectCertificateClient()
    env.client.poll(allowed=True, retry=True)  # command survives the wait/restart
    assert len(env.backend.calls) == 1
    env.client = DirectCertificateClient()
    env.backend.error = None
    assert due(env, result).status == "queued"


def test_installed_leaf_expiring_while_offline_is_not_offered_and_does_not_start_renewal(setup):
    """After long sleep only the backend may renew; expired local files are unusable."""
    env = setup
    result = installed(env)
    env.clock.now += 20 * 86400
    env.backend.current = {"certificate_pem": None, "public_key_fingerprint": None,
                           "certificate_not_after": None, "direct_access_allowed": True}
    result = env.client.poll(allowed=True)
    assert not result.ready and result.key_file is None and result.certificate_file is None
    assert env.backend.calls[-1][0:2] == ("GET", "certificates/current")
    assert len(env.backend.orders) == 1 and result.next_poll_at > env.clock.now
    count = len(env.backend.calls)
    for _ in range(3):
        assert env.client.poll(allowed=True).next_poll_at == result.next_poll_at
    assert len(env.backend.calls) == count, "expiry must not erase the recovery poll deadline"
    assert not due(env, result).ready
    assert len(env.backend.calls) == count + 1


def test_policy_resume_does_not_erase_independent_rotation_retry_at(setup):
    """An allowlist toggle cannot bypass a still-live order admission rate limit."""
    env = setup
    installed(env)
    env.backend.error = remote_access.BackendRequestError(429, {
        "error": "certificate_key_rotation_rate_limited",
        "retry_at": datetime.fromtimestamp(env.clock.now + 7 * 86400, timezone.utc).isoformat(),
    })
    env.client.poll(allowed=True, rotate_key=True)
    post_count = sum(method == "POST" for method, _, _ in env.backend.calls)
    env.backend.error = None
    env.backend.current["direct_access_allowed"] = False
    env.clock.now += 6 * 3600
    assert env.client.poll(allowed=True).status == "blocked"
    env.clock.now += 60
    env.backend.current["direct_access_allowed"] = True
    env.client = DirectCertificateClient()
    env.client.poll(allowed=True, resume=True)
    assert sum(method == "POST" for method, _, _ in env.backend.calls) == post_count
    assert env.backend.calls[-1][:2] == ("GET", "certificates/current")


def test_terminal_order_retry_at_does_not_starve_active_current_fetch(setup):
    """A terminal order's retry_at belongs to explicit order retry, not K1's GET."""
    env = setup
    old = installed(env)
    old_public = next(iter(env.backend.orders.values()))["csr"].public_key()
    expiry = datetime.fromisoformat(old.certificate_not_after).timestamp()
    env.clock.now = expiry - 600
    env.client.poll(allowed=True)
    queued = env.client.poll(allowed=True, rotate_key=True)
    env.backend.error = remote_access.BackendRequestError(400, {
        "error": "acme_authorization_invalid",
        "retry_at": datetime.fromtimestamp(env.clock.now + 3600, timezone.utc).isoformat(),
    })
    assert due(env, queued).status == "failed"
    env.backend.error = None
    env.clock.now = expiry + 1
    env.backend.current = env.backend.issue(old_public)
    env.client = DirectCertificateClient()
    result = env.client.poll(allowed=True)
    assert result.ready and result.reload_required and result.key_file == old.key_file
    assert result.status == "failed" and env.backend.calls[-1][:2] == ("GET", "certificates/current")


def test_current_null_keeps_short_recovery_when_existing_leaf_is_still_valid(setup):
    """A null current response must retry within five minutes, not six hours."""
    env = setup
    active = installed(env)
    env.backend.current = {"certificate_pem": None, "public_key_fingerprint": None,
                           "certificate_not_after": None, "direct_access_allowed": True}
    waiting = due(env, active)
    assert waiting.ready and waiting.next_poll_at - env.clock.now <= 300


def test_current_retry_at_survives_successful_pending_order_install(setup):
    """Order progress must not erase the current endpoint's separate retry bound."""
    env = setup
    installed(env)
    start = env.clock.now
    env.backend.error = remote_access.BackendRequestError(429, {
        "error": "certificate_budget_queued",
        "retry_at": datetime.fromtimestamp(start + 12 * 3600, timezone.utc).isoformat(),
    })
    env.client.poll(allowed=True, rotate_key=True)
    env.backend.error = remote_access.BackendRequestError(409, {
        "error": "certificate_cleanup_pending",
        "retry_at": datetime.fromtimestamp(start + 48 * 3600, timezone.utc).isoformat(),
    })
    env.clock.now = start + 6 * 3600
    env.client.poll(allowed=True)
    assert env.backend.calls[-1][:2] == ("GET", "certificates/current")
    env.backend.error = None
    env.clock.now = start + 12 * 3600
    queued = env.client.poll(allowed=True)
    result = due(env, queued)
    assert result.ready and result.reload_required
    count = len(env.backend.calls)
    env.clock.now = start + 18 * 3600 + 120
    env.client = DirectCertificateClient()
    result = env.client.poll(allowed=True)
    assert len(env.backend.calls) == count
    assert result.next_poll_at >= start + 48 * 3600
    due(env, result)
    assert env.backend.calls[-1][:2] == ("GET", "certificates/current")


def test_existing_device_http_boundary_sends_secret_only_in_header(setup, monkeypatch):
    """The client must reuse paired-device URL escaping, auth and redirect refusal."""
    # Restore only the high-level HTTP boundary; never undo the home isolation.
    monkeypatch.setattr(remote_access, "_device_json_request", _DEVICE_REQUEST)
    seen = []
    def request(method, url, **kwargs):
        seen.append((method, url, kwargs))
        return SimpleNamespace(status_code=404, json=lambda: {"error": "not_found"})
    monkeypatch.setattr(remote_access.requests, "request", request)
    result = DirectCertificateClient().poll(allowed=True)
    assert result.last_error_code == "unavailable"
    method, url, kwargs = seen[0]
    secret = setup.config.remote_access.vibe_cloud.instance_secret
    assert method == "POST" and url == "https://control.invalid/api/v1/instances/instance-one/certificates/orders"
    assert kwargs["headers"]["X-Vibe-Device-Secret"] == secret
    assert kwargs["allow_redirects"] is False and kwargs["timeout"] == 8
    assert secret not in url + json.dumps(kwargs["json"])


@pytest.mark.parametrize("failure", ["failed", "rate_limited"])
def test_pending_rotation_cannot_starve_active_key_renewal(setup, failure):
    """Failed/queued K2 must not stop backend-owned renewals of still-active K1."""
    env = setup
    old = installed(env)
    old_public = next(iter(env.backend.orders.values()))["csr"].public_key()
    if failure == "rate_limited":
        env.backend.error = remote_access.BackendRequestError(429, {
            "error": "certificate_key_rotation_rate_limited",
            "retry_at": datetime.fromtimestamp(env.clock.now + 7 * 86400, timezone.utc).isoformat(),
        })
        env.client.poll(allowed=True, rotate_key=True)
    else:
        queued = env.client.poll(allowed=True, rotate_key=True)
        env.backend.error = remote_access.BackendRequestError(400, {"error": "acme_authorization_invalid"})
        assert due(env, queued).status == "failed"
    env.client = DirectCertificateClient()
    env.backend.error = None
    # Only K1 is renewed: the backend still considers it current.
    env.clock.now += 6 * 3600
    env.backend.current = env.backend.issue(old_public)
    result = env.client.poll(allowed=True)
    assert env.backend.calls[-1][0:2] == ("GET", "certificates/current")
    assert result.ready and result.reload_required and result.key_file == old.key_file
    assert result.certificate_file != old.certificate_file
    assert result.status != "issued", "refreshing K1 must not complete K2's order"


def test_current_fetch_is_due_before_installed_certificate_expires(setup):
    """The six-hour idle cadence must shrink near expiry, including after restart."""
    env = setup
    old = installed(env)
    expiry = datetime.fromisoformat(old.certificate_not_after).timestamp()
    env.clock.now = expiry - 60
    near_expiry = env.client.poll(allowed=True)
    assert not near_expiry.reload_required
    env.backend.current = env.backend.issue()
    env.clock.now = expiry + 1
    env.client = DirectCertificateClient()
    result = env.client.poll(allowed=True)
    assert result.ready and result.reload_required


def test_parseable_but_tls_unloadable_chain_never_replaces_usable_files(setup):
    """OpenSSL security-level rejection must happen before retiring the old chain."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    env = setup
    old = installed(env)
    env.backend.ca_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    now = datetime.fromtimestamp(env.clock.now, timezone.utc)
    env.backend.ca = (x509.CertificateBuilder().subject_name(env.backend.ca_name).issuer_name(env.backend.ca_name)
                      .public_key(env.backend.ca_key.public_key()).serial_number(3)
                      .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=365))
                      .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                      .sign(env.backend.ca_key, hashes.SHA256()))
    env.backend.current = env.backend.issue()
    old_bytes = Path(old.certificate_file).read_bytes()
    result = due(env, old)
    assert not result.reload_required and result.certificate_file == old.certificate_file
    assert result.ready and Path(old.certificate_file).read_bytes() == old_bytes


def test_certificate_time_validation_uses_response_time_after_sleep(setup):
    """A machine sleeping during HTTP must not publish an already expired leaf."""
    env = setup
    queued = env.client.poll(allowed=True)
    candidate = env.backend.issue()
    def sleeping_request(*_args):
        env.clock.now = datetime.fromisoformat(candidate["certificate_not_after"]).timestamp() + 1
        return {**candidate, "hostname": HOST, "order_id": "certord_1", "status": "issued", "next_poll_at": None}
    env.backend.error = sleeping_request
    result = due(env, queued)
    assert not result.ready and result.last_error_code == "invalid_certificate"
    assert result.next_poll_at > env.clock.now


def test_valid_current_chain_repairs_corrupted_selected_file_even_for_same_leaf(setup):
    """Leaf-identity deduplication must not make a broken public file permanent."""
    env = setup
    old = installed(env)
    correct = Path(old.certificate_file).read_bytes()
    Path(old.certificate_file).write_bytes(b"corrupted public chain")
    env.client = DirectCertificateClient()
    result = due(env, old)
    assert result.ready and result.reload_required
    assert result.key_file == old.key_file and Path(result.certificate_file).read_bytes() == correct
    assert not due(env, result).reload_required


@pytest.mark.asyncio
async def test_installed_files_start_real_ingress_and_reload_new_leaf(setup):
    """Client output must be usable by SSL and a live ingress, not merely parse."""
    env = setup
    result = await asyncio.to_thread(installed, env)
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})
    ingress = DirectIngress(app, hostname=HOST, certfile=result.certificate_file, keyfile=result.key_file)
    context = ssl.create_default_context(cadata=env.backend.ca.public_bytes(serialization.Encoding.PEM).decode())
    async def presented():
        reader, writer = await asyncio.open_connection(*ingress.address, ssl=context, server_hostname=HOST)
        try:
            der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
            writer.write(f"GET / HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
            response = await reader.read()
            assert b"200 OK" in response and response.endswith(b"ok")
            return x509.load_der_x509_certificate(der).serial_number
        finally:
            writer.close()
            await writer.wait_closed()
    await ingress.start()
    try:
        first_serial = await presented()
        env.backend.current = env.backend.issue()
        renewed = await asyncio.to_thread(due, env, result)
        assert renewed.reload_required
        await asyncio.to_thread(ingress.reload_certificate, renewed.certificate_file, renewed.key_file)
        second_serial = await presented()
        expected = x509.load_pem_x509_certificate(env.backend.current["certificate_pem"].encode()).serial_number
        assert second_serial == expected and second_serial != first_serial
    finally:
        await ingress.stop()
