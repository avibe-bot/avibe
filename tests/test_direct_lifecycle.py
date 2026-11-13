"""I3 lifecycle invariants under isolated config, fake HTTP and injected transport."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import paths
from config.v2_config import V2Config
from vibe import direct_lifecycle as module, remote_access
from vibe.direct_certificates import CertificateResult
from vibe.direct_lifecycle import DirectAccessLifecycle
from tests.test_direct_transport_runtime import published  # Shared inert published-release fixture.


def _config(*, direct=True, public_url="https://alice-app.avibe.bot"):
    config = V2Config.default()
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.direct = direct
    cloud.backend_url = "https://backend.invalid"
    cloud.instance_id = "instance-one"
    cloud.instance_secret = "device-secret"
    cloud.public_url = public_url
    return config


def _ready(**changes):
    return replace(CertificateResult(
        "issued", ready=True, certificate_file="/test/cert.pem", key_file="/test/key.pem",
        certificate_not_after="2099-01-01T00:00:00+00:00",
    ), **changes)


class FakeCertificateClient:
    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []

    def poll(self, **kwargs):
        self.calls.append(kwargs)
        return self.results.pop(0) if self.results else _ready()


class FakeRendezvous:
    def __init__(self, calls):
        self.calls = calls
        self.endpoint = None
        self.reason = None

    def publish(self, endpoint, *, reason="not_running"):
        self.endpoint, self.reason = endpoint, reason
        self.calls.append(("publish", endpoint, reason))


class FakeIngress:
    def __init__(self, calls, **kwargs):
        self.calls = calls
        self.running = False
        self.context = "original"

    def _prepare_start(self):
        return object()

    async def _start_prepared(self, prepared):
        await self.start()

    async def start(self):
        self.calls.append("ingress_start")
        self.running = True

    async def stop(self):
        self.calls.append("ingress_stop")
        self.running = False

    def _prepare_certificate_reload(self, certfile, keyfile):
        return "renewed"

    def _apply_certificate_reload(self, context, certfile, keyfile):
        self.context = context
        self.calls.append("ingress_reload")


class FakeSupervisor:
    def __init__(self, calls, ingress, rendezvous):
        self.calls, self.rendezvous = calls, rendezvous
        self.running = False
        self.endpoint_id = None
        self.reason = "not_running"
        self.loop = asyncio.get_running_loop()

    @property
    def status(self):
        assert asyncio.get_running_loop() is self.loop
        return SimpleNamespace(running=self.running, endpoint_id=self.endpoint_id, reason=self.reason)

    async def start(self):
        assert asyncio.get_running_loop() is self.loop
        self.calls.append("supervisor_start")
        self.running = True
        self.endpoint_id = "e" * 64
        self.rendezvous.publish(self.endpoint_id, reason="running")

    async def stop(self):
        assert asyncio.get_running_loop() is self.loop
        self.calls.append("supervisor_stop")
        self.running = False
        self.endpoint_id = None
        self.rendezvous.publish(None, reason="stopped")


async def _wait(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture
def reports(monkeypatch):
    observed = []
    monkeypatch.setattr(remote_access, "_report_runtime_status_async", lambda config, **kw: observed.append((copy.deepcopy(config), kw)))
    return observed


@asynccontextmanager
async def _owner(config, reports, *, cert=None, ingress_type=FakeIngress, supervisor_type=FakeSupervisor, **options):
    calls = []
    cert = cert or FakeCertificateClient()
    rendezvous = FakeRendezvous(calls)
    owner = DirectAccessLifecycle(
        object(), certificate_client=cert,
        ingress_factory=lambda app, **kw: ingress_type(calls, **kw),
        supervisor_factory=lambda executable, ingress, rendezvous: supervisor_type(calls, ingress, rendezvous),
        binary_resolver=options.pop("binary_resolver", lambda config: "/injected/test-sidecar"),
        rendezvous=rendezvous, config_loader=options.pop("config_loader", lambda: config), **options,
    )
    await owner.start()
    try:
        await _wait(lambda: owner._config is not None)
        yield owner, calls, cert, rendezvous
    finally:
        await owner.stop()


async def _permission(owner, config, allowed=True):
    ticket = owner.runtime_status_request(config)
    assert ticket is not None
    owner.submit_runtime_status_response(ticket, {"ok": True, "direct_access_allowed": allowed})
    await _wait(lambda: owner._last_response_sequence == ticket.sequence)


def test_setting_default_legacy_config_and_roundtrip():
    config = _config(direct=False)
    config.save()
    payload = json.loads(paths.get_config_path().read_text())
    assert payload["remote_access"]["vibe_cloud"]["direct"] is False
    del payload["remote_access"]["vibe_cloud"]["direct"]
    assert V2Config.from_payload(payload).remote_access.vibe_cloud.direct is False
    config.remote_access.vibe_cloud.direct = True
    config.save()
    assert V2Config.load().remote_access.vibe_cloud.direct is True


def test_direct_toggle_does_not_reconcile_or_change_tunnel_identity():
    from vibe import api

    before = _config(direct=False)
    after = copy.deepcopy(before)
    after.remote_access.vibe_cloud.direct = True
    assert not api.remote_access_runtime_changed(before, after)
    assert not api.remote_access_runtime_changed(after, before)
    assert remote_access._pairing_identity_from_cloud(before.remote_access.vibe_cloud) == remote_access._pairing_identity_from_cloud(after.remote_access.vibe_cloud)


@pytest.mark.parametrize("proactor", [True, False])
def test_actual_windows_loop_guard_checks_proactor_type(monkeypatch, proactor):
    class Proactor:
        pass
    loop = Proactor() if proactor else object()
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(ProactorEventLoop=Proactor, get_running_loop=lambda: loop))
    assert module._subprocess_loop_supported() is proactor


async def test_off_is_literal_no_listener_child_order_or_file(reports, monkeypatch):
    config = _config(direct=False)
    observed = {"bind": 0, "spawn": 0, "order": 0, "resolver": 0}
    def forbidden(key):
        def call(*args, **kwargs):
            observed[key] += 1
            raise AssertionError(key)
        return call
    monkeypatch.setattr(socket.socket, "bind", forbidden("bind"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden("spawn"))
    monkeypatch.setattr(remote_access, "_device_json_request", forbidden("order"))
    before = set(paths.get_state_dir().rglob("*"))
    async with _owner(config, reports, binary_resolver=forbidden("resolver")) as (owner, calls, cert, rv):
        await _wait(lambda: bool(reports))
        await asyncio.sleep(0.12)
        assert owner.status().state == "disabled"
        assert owner.runtime_status_request(config) is None
        assert cert.calls == []
        assert "ingress_start" not in calls and "supervisor_start" not in calls
        assert all(not cfg.remote_access.vibe_cloud.direct for cfg, _ in reports)
        assert rv.endpoint is None
    assert observed == {key: 0 for key in observed}
    assert set(paths.get_state_dir().rglob("*")) == before


async def test_product_without_binary_never_orders_even_with_fresh_permission(reports):
    config = _config()
    async with _owner(config, reports, binary_resolver=module.resolve_direct_transport_binary) as (owner, calls, cert, rv):
        await _wait(lambda: owner.status().state == "transport-not-installed")
        await _permission(owner, config)
        await asyncio.sleep(0.12)
        assert not cert.calls
        assert "ingress_start" not in calls and "supervisor_start" not in calls
        assert rv.endpoint is None and rv.reason == "transport_not_installed"


async def test_managed_install_reaches_publication_only_after_all_lifecycle_gates(reports, published, monkeypatch):
    from core.direct_transport_runtime import DirectTransportRuntimeManager

    monkeypatch.setattr(module, "BINARY_REFRESH_INTERVAL_SECONDS", 0.02)
    config = _config(direct=False)
    config.save()
    persisted = paths.get_config_path().read_bytes()
    manager = DirectTransportRuntimeManager()
    assert manager.ensure()["ok"] is True
    binary = manager.resolve_binary()
    assert binary is not None
    assert paths.get_config_path().read_bytes() == persisted
    assert not (paths.get_state_dir() / "direct").exists()
    entered, release = threading.Event(), threading.Event()
    class WaitingCertificate(FakeCertificateClient):
        def poll(self, **kwargs):
            self.calls.append(kwargs)
            entered.set()
            assert release.wait(3)
            return _ready()
    try:
        async with _owner(config, reports, cert=WaitingCertificate(), binary_resolver=module.resolve_direct_transport_binary) as (owner, calls, cert, rv):
            assert cert.calls == [] and rv.endpoint is None
            config.remote_access.vibe_cloud.direct = True
            owner.notify_config_changed(config)
            await _wait(lambda: owner.status().state == "waiting-permission")
            assert cert.calls == [] and rv.endpoint is None
            await _permission(owner, config)
            await _wait(entered.is_set)
            assert rv.endpoint is None and "supervisor_start" not in calls
            release.set()
            await _wait(lambda: owner.status().state == "running")
            assert rv.endpoint is not None
            # Removing integrity approval, without changing pairing or permission,
            # withdraws the ready answer. Repair resumes the normal gated path.
            binary.write_bytes(b"tampered test fixture")
            await _wait(lambda: owner.status().state == "transport-not-installed")
            assert rv.endpoint is None and not owner.status().transport_running
            assert manager.ensure()["ok"] is True
            await _wait(lambda: owner.status().state == "running")
            assert calls.count("supervisor_start") == 2
    finally:
        release.set()


async def test_stalled_binary_verification_expires_without_blocking_loop_or_accumulating_workers(reports, monkeypatch):
    config = _config()
    monkeypatch.setattr(module, "CONFIG_POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(module, "BINARY_REFRESH_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(module, "BINARY_OBSERVATION_MAX_AGE_SECONDS", 0.15)
    entered, release = threading.Event(), threading.Event()
    count = 0
    loop_thread = threading.get_ident()
    def resolver(config):
        nonlocal count
        assert threading.get_ident() != loop_thread
        count += 1
        if count == 2:
            entered.set()
            assert release.wait(3)
        return "/test/verified-sidecar"
    try:
        async with _owner(config, reports, binary_resolver=resolver) as (owner, calls, cert, rv):
            await _permission(owner, config)
            await _wait(lambda: owner.status().state == "running")
            await _wait(entered.is_set)
            await _wait(lambda: owner.status().state == "transport-not-installed")
            assert rv.endpoint is None and "supervisor_stop" in calls
            assert count == 2
            # Stop/start cannot abandon the synchronous worker and create another.
            await asyncio.wait_for(owner.stop(), 0.3)
            await owner.start()
            await asyncio.sleep(0.05)
            assert count == 2 and rv.endpoint is None
            release.set()
            await _wait(lambda: count >= 3 and owner.status().state == "running")
    finally:
        release.set()


async def test_late_binary_observation_cannot_cross_off_on_generation(reports, monkeypatch):
    config = _config()
    monkeypatch.setattr(module, "CONFIG_POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(module, "BINARY_REFRESH_INTERVAL_SECONDS", 0.02)
    old_entered, old_release = threading.Event(), threading.Event()
    new_entered, new_release = threading.Event(), threading.Event()
    count = 0
    def resolver(config):
        nonlocal count
        count += 1
        if count == 2:
            old_entered.set()
            assert old_release.wait(3)
            return "/test/stale-sidecar"
        if count == 3:
            new_entered.set()
            assert new_release.wait(3)
        return "/test/current-sidecar"
    try:
        async with _owner(config, reports, binary_resolver=resolver) as (owner, calls, cert, rv):
            await _permission(owner, config)
            await _wait(lambda: owner.status().state == "running")
            await _wait(old_entered.is_set)
            config.remote_access.vibe_cloud.direct = False
            owner.notify_config_changed(config)
            await _wait(lambda: owner.status().state == "disabled")
            config.remote_access.vibe_cloud.direct = True
            owner.notify_config_changed(config)
            await _wait(lambda: len(reports) == 3)
            await _permission(owner, config)
            old_release.set()
            await _wait(new_entered.is_set)
            assert owner._binary is None and rv.endpoint is None
            assert calls.count("supervisor_start") == 1 and len(cert.calls) == 1
            new_release.set()
            await _wait(lambda: owner.status().state == "running")
            assert owner._binary == "/test/current-sidecar"
    finally:
        old_release.set()
        new_release.set()


async def test_cli_disk_only_optout_remains_observable_during_stalled_binary_hash(reports, monkeypatch):
    from vibe import cli, runtime

    config = _config()
    config.save()
    monkeypatch.setattr(module, "CONFIG_POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(module, "BINARY_REFRESH_INTERVAL_SECONDS", 0.02)
    # Success must come from the independent config read, not binary expiry.
    monkeypatch.setattr(module, "BINARY_OBSERVATION_MAX_AGE_SECONDS", 30.0)
    entered, release = threading.Event(), threading.Event()
    checks = 0
    def resolver(config):
        nonlocal checks
        checks += 1
        if checks == 2:
            entered.set()
            assert release.wait(4)
        return "/test/verified-sidecar"
    # Only service readback is stubbed; the CLI's real config transaction and
    # the owner's real disk loader must communicate without notify_config_changed.
    monkeypatch.setattr(remote_access, "live_direct_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "recorded_ui_is_gone", lambda: False)
    monkeypatch.setattr(remote_access, "status", lambda cfg: {
        "ok": True, "direct": {"enabled": cfg.remote_access.vibe_cloud.direct, "state": "disabled"},
    })
    try:
        async with _owner(config, reports, binary_resolver=resolver, config_loader=V2Config.load) as (owner, calls, cert, rv):
            await _permission(owner, config)
            await _wait(lambda: owner.status().state == "running")
            await _wait(entered.is_set)
            assert await asyncio.to_thread(cli.cmd_remote_direct, SimpleNamespace(direct_command="off", json=True)) == 0
            assert V2Config.load().remote_access.vibe_cloud.direct is False
            await _wait(lambda: owner.status().state == "disabled", timeout=1)
            assert owner.status().enabled is False and rv.endpoint is None
            assert not owner.status().transport_running and "supervisor_stop" in calls
            assert reports[-1][0].remote_access.vibe_cloud.direct is False
            assert checks == 2 and not release.is_set()
    finally:
        release.set()


async def test_binary_recovery_waits_for_certificate_after_discarded_renewal_retires_old_chain(reports, monkeypatch, tmp_path):
    config = _config()
    monkeypatch.setattr(module, "CONFIG_POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(module, "BINARY_REFRESH_INTERVAL_SECONDS", 0.02)
    old_chain, new_chain = tmp_path / "old-chain.pem", tmp_path / "new-chain.pem"
    old_chain.write_bytes(b"old test chain")
    renewal_entered, renewal_release = threading.Event(), threading.Event()
    fresh_entered, fresh_release = threading.Event(), threading.Event()
    available = [True]
    class RetiringCertificate(FakeCertificateClient):
        def poll(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return _ready(certificate_file=str(old_chain), next_poll_at=time.time() + 0.05)
            if len(self.calls) == 2:
                renewal_entered.set()
                assert renewal_release.wait(4)
                # The real client persists a renewal and retires its previous
                # chain before the worker returns a result to the lifecycle.
                new_chain.write_bytes(b"renewed test chain")
                old_chain.unlink()
            else:
                fresh_entered.set()
                assert fresh_release.wait(4)
            return _ready(certificate_file=str(new_chain))
    prepared = []
    class FileIngress(FakeIngress):
        def __init__(self, calls, **kwargs):
            super().__init__(calls, **kwargs)
            self.certfile = kwargs["certfile"]
        def _prepare_start(self):
            prepared.append(self.certfile)
            Path(self.certfile).read_bytes()  # Real missing-file failure, as in TLS preparation.
            return super()._prepare_start()
    try:
        async with _owner(
            config, reports, cert=RetiringCertificate(), ingress_type=FileIngress,
            binary_resolver=lambda cfg: "/test/verified-sidecar" if available[0] else None,
        ) as (owner, calls, cert, rv):
            await _permission(owner, config)
            await _wait(lambda: owner.status().state == "running")
            await _wait(renewal_entered.is_set)
            available[0] = False
            await _wait(lambda: owner.status().state == "transport-not-installed")
            renewal_release.set()
            await _wait(lambda: owner._poll_task is None and not old_chain.exists())
            available[0] = True
            await _wait(fresh_entered.is_set)
            await asyncio.sleep(0.05)
            fresh_release.set()
            await _wait(lambda: owner._poll_task is None)
            assert owner._failed_generation is None
            await _wait(lambda: owner.status().state == "running")
            assert prepared == [str(old_chain), str(new_chain)]
            assert rv.endpoint is not None and calls.count("supervisor_start") == 2
    finally:
        renewal_release.set()
        fresh_release.set()


@pytest.mark.parametrize("url", ["https://custom.test", "http://alice-app.avibe.bot", "https://alice-app.avibe.bot:8443", "https://user@alice-app.avibe.bot"])
async def test_hostname_gate_precedes_resolver_and_certificate(url, reports):
    config = _config(public_url=url)
    resolved = []
    async with _owner(config, reports, binary_resolver=lambda cfg: resolved.append(1)) as (owner, calls, cert, rv):
        await _wait(lambda: owner.status().reason == "hostname_not_direct")
        assert resolved == [] and cert.calls == []
        assert rv.endpoint is None


async def test_start_stop_order_and_renewal_from_client_deadline(reports):
    config = _config()
    cert = FakeCertificateClient([
        _ready(next_poll_at=time.time() + 0.2),
        _ready(reload_required=True, certificate_not_after="2099-02-01T00:00:00+00:00"),
    ])
    async with _owner(config, reports, cert=cert) as (owner, calls, _, rv):
        await _permission(owner, config)
        await _wait(lambda: owner.status().state == "running")
        assert calls.index("ingress_start") < calls.index("supervisor_start")
        assert owner.status().transport_running
        await _wait(lambda: "ingress_reload" in calls)
        assert len(cert.calls) == 2
        assert calls.count("supervisor_start") == 1
        assert cert.calls[0]["expected_pairing_fingerprint"] == module._config_signature(config)[3]
        assert cert.calls[0]["permission_deadline"] > time.monotonic()
        before_stop = len(calls)
        config.remote_access.vibe_cloud.direct = False
        owner.notify_config_changed(config)
        await _wait(lambda: owner.status().state == "disabled")
        tail = calls[before_stop:]
        assert tail[0][0] == "publish" and tail[0][1] is None
        assert tail.index("supervisor_stop") < tail.index("ingress_stop")
        assert rv.endpoint is None and rv.reason == "setting_off"
        assert not reports[-1][0].remote_access.vibe_cloud.direct
        assert reports[-1][1]["event"] == "direct_paths_disabled"


async def test_permission_denial_missing_field_and_resume_stop_serving(reports):
    config = _config()
    async with _owner(config, reports) as (owner, calls, cert, rv):
        await _permission(owner, config)
        await _wait(lambda: owner.status().state == "running")
        owner.submit_runtime_status_response(owner.runtime_status_request(config), {"ok": True})
        await _wait(lambda: owner.status().reason == "permission_denied")
        assert not owner.status().transport_running and not owner._ingress
        await _permission(owner, config)
        await _wait(lambda: len(cert.calls) == 2)
        assert cert.calls[-1]["resume"] is True


async def test_expiry_cancels_awaiting_supervisor_on_owning_loop(reports, monkeypatch):
    config = _config()
    monkeypatch.setattr(module, "PERMISSION_MAX_AGE_SECONDS", 0.15)
    entered = asyncio.Event()
    class SlowSupervisor(FakeSupervisor):
        async def start(self):
            self.running = True
            entered.set()
            await asyncio.Event().wait()
    async with _owner(config, reports, supervisor_type=SlowSupervisor) as (owner, calls, cert, rv):
        await _permission(owner, config)
        await asyncio.wait_for(entered.wait(), 1)
        await _wait(lambda: owner.status().reason == "permission_expired")
        assert "supervisor_stop" in calls and "ingress_stop" in calls
        assert not owner.status().transport_running and rv.endpoint is None


async def test_off_on_drops_old_permission_ticket_even_same_pairing(reports):
    config = _config()
    async with _owner(config, reports) as (owner, calls, cert, rv):
        old = owner.runtime_status_request(config)
        config.remote_access.vibe_cloud.direct = False
        owner.notify_config_changed(config)
        config.remote_access.vibe_cloud.direct = True
        owner.notify_config_changed(config)
        await _wait(lambda: len(reports) == 3)
        assert [cfg.remote_access.vibe_cloud.direct for cfg, _ in reports] == [True, False, True]
        owner.submit_runtime_status_response(old, {"direct_access_allowed": True})
        await asyncio.sleep(0.12)
        assert cert.calls == []
        # Opt-in needs a new off-loop installed-binary observation; it must
        # still reject the previous opt-in's permission ticket once observed.
        await _wait(lambda: owner.status().state == "waiting-permission")
        await _permission(owner, config)
        await _wait(lambda: owner.status().state == "running")


async def test_pairing_barrier_waits_for_resource_stop_not_poll(reports):
    config = _config()
    async with _owner(config, reports) as (owner, calls, cert, rv):
        await _permission(owner, config)
        await _wait(lambda: owner.status().state == "running")
        assert await asyncio.to_thread(owner.stop_for_pairing_change)
        tail = [item for item in calls if isinstance(item, str)]
        assert tail[-2:] == ["supervisor_stop", "ingress_stop"]
        assert owner._pairing_suspended and rv.endpoint is None
        # An unchanged file must not undo a pre-commit suspension.
        owner.notify_config_changed(config)
        await asyncio.sleep(0.05)
        assert owner._pairing_suspended and owner._supervisor is None


@pytest.mark.parametrize("leave_enabled", [False, True])
async def test_stuck_prepare_is_one_worker_and_late_result_cannot_bind(reports, leave_enabled):
    config = _config()
    entered, release = threading.Event(), threading.Event()
    active = []
    class SlowIngress(FakeIngress):
        def _prepare_start(self):
            active.append(self)
            entered.set()
            assert release.wait(3)
            return object()
        async def _start_prepared(self, prepared):
            await self.start()
    try:
        async with _owner(config, reports, ingress_type=SlowIngress) as (owner, calls, cert, rv):
            await _permission(owner, config)
            await _wait(entered.is_set)
            for _ in range(3):
                config.remote_access.vibe_cloud.direct = False
                owner.notify_config_changed(config)
                await _wait(lambda: owner.status().state == "disabled")
                config.remote_access.vibe_cloud.direct = True
                owner.notify_config_changed(config)
                await _wait(lambda: owner.status().state == "waiting-permission")
                await _permission(owner, config)
                await asyncio.sleep(0.02)
            assert len(active) == 1
            if not leave_enabled:
                config.remote_access.vibe_cloud.direct = False
                owner.notify_config_changed(config)
                await _wait(lambda: owner.status().state == "disabled")
            release.set()
            if leave_enabled:
                await _wait(lambda: owner.status().state == "running")
                assert len(active) == 2 and owner._ingress is active[1]
                assert calls.count("ingress_start") == 1 and calls.count("supervisor_start") == 1
            else:
                await asyncio.sleep(0.1)
                assert "ingress_start" not in calls and "supervisor_start" not in calls
                assert rv.endpoint is None
    finally:
        release.set()


async def test_old_reload_after_optout_never_applies_context(reports):
    config = _config()
    entered, release = threading.Event(), threading.Event()
    cert = FakeCertificateClient([_ready(next_poll_at=time.time() + 0.1), _ready(reload_required=True)])
    class SlowReload(FakeIngress):
        def _prepare_certificate_reload(self, *args):
            entered.set()
            assert release.wait(3)
            return "obsolete"
    try:
        async with _owner(config, reports, cert=cert, ingress_type=SlowReload) as (owner, calls, _, rv):
            await _permission(owner, config)
            await _wait(lambda: owner.status().state == "running")
            ingress = owner._ingress
            await _wait(entered.is_set)
            config.remote_access.vibe_cloud.direct = False
            owner.notify_config_changed(config)
            await _wait(lambda: owner.status().state == "disabled")
            release.set()
            await _wait(lambda: owner._poll_task is None)
            assert ingress.context == "original" and "ingress_reload" not in calls
            assert rv.endpoint is None
    finally:
        release.set()


@pytest.mark.parametrize("already_running", [False, True])
async def test_old_poll_deadline_retries_under_refreshed_grant(reports, monkeypatch, already_running):
    config = _config()
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    entered, release = threading.Event(), threading.Event()
    rejected_call = 2 if already_running else 1

    class ExpiredPoll(FakeCertificateClient):
        def poll(self, **kw):
            self.calls.append(kw)
            if len(self.calls) == rejected_call:
                entered.set()
                assert release.wait(3)
                assert kw["permission_deadline"] < clock[0]
                return CertificateResult("disabled", last_error_code="permission_expired")
            return _ready(next_poll_at=time.time() + 0.1 if len(self.calls) < rejected_call else None)

    try:
        async with _owner(config, reports, cert=ExpiredPoll()) as (owner, calls, cert, rv):
            await _permission(owner, config)
            if already_running:
                await _wait(lambda: owner.status().state == "running")
            await _wait(entered.is_set)
            generation = owner._generation
            clock[0] = 900.0  # Refresh before the first receipt expires at 1000.
            await _permission(owner, config)
            clock[0] = 1001.0
            release.set()
            await _wait(lambda: len(cert.calls) == rejected_call + 1)
            await _wait(lambda: owner.status().state == "running")
            assert owner._generation == generation
            assert cert.calls[-1]["permission_deadline"] == 1800.0
            assert calls.count("supervisor_start") == 1
            assert "supervisor_stop" not in calls and rv.endpoint is not None
    finally:
        release.set()


async def test_renewal_survives_old_ingress_expiry_during_tls_preparation(reports):
    config = _config()
    entered, release = threading.Event(), threading.Event()
    starts = []
    renewal = _ready(reload_required=True, certificate_file="/test/renewed.pem")
    cert = FakeCertificateClient([_ready(next_poll_at=time.time() + 0.1), renewal])

    class SlowRenewal(FakeIngress):
        def __init__(self, calls, **kwargs):
            super().__init__(calls, **kwargs)
            starts.append(kwargs["certfile"])

        def _prepare_certificate_reload(self, *args):
            entered.set()
            assert release.wait(3)
            return "renewed"

    try:
        async with _owner(config, reports, cert=cert, ingress_type=SlowRenewal) as (owner, calls, _, rv):
            await _permission(owner, config)
            await _wait(lambda: owner.status().state == "running")
            await _wait(entered.is_set)
            old_ingress, generation = owner._ingress, owner._generation
            owner._certificate_result = _ready(certificate_not_after="2000-01-01T00:00:00+00:00")
            owner._wake.set()
            await _wait(lambda: owner._ingress is None)
            assert rv.endpoint is None and not old_ingress.running
            release.set()
            await _wait(lambda: owner.status().state == "running")
            assert owner._generation == generation and owner._ingress is not old_ingress
            assert starts == ["/test/cert.pem", "/test/renewed.pem"]
            assert owner._certificate_result is renewal and len(cert.calls) == 2
            assert "ingress_reload" not in calls and old_ingress.context == "original"
    finally:
        release.set()


async def test_cancelled_order_retried_once_not_forever(reports):
    config = _config()
    cancelled = CertificateResult("cancelled", last_error_code="order_failed")
    cert = FakeCertificateClient([cancelled, cancelled])
    async with _owner(config, reports, cert=cert) as (owner, calls, _, rv):
        await _permission(owner, config)
        await _wait(lambda: len(cert.calls) == 2)
        await asyncio.sleep(0.15)
        assert len(cert.calls) == 2
        assert cert.calls[1]["retry"] is True
        assert "retry" not in cert.calls[0]
        assert rv.endpoint is None


async def test_rotation_one_shot_exposes_renewal_conflict(reports):
    config = _config()
    conflict = _ready(last_error_code="certificate_order_in_progress")
    cert = FakeCertificateClient([_ready(), conflict])
    async with _owner(config, reports, cert=cert) as (owner, calls, _, rv):
        await _permission(owner, config)
        await _wait(lambda: owner.status().state == "running")
        result = await owner.rotate_key()
        assert result is conflict
        assert cert.calls[-1]["rotate_key"] is True
        assert "rotate_key" not in cert.calls[0]
        assert len(cert.calls) == 2


async def test_windows_unsupported_loop_closes_before_order(reports, monkeypatch):
    config = _config()
    monkeypatch.setattr(module, "_subprocess_loop_supported", lambda: False)
    async with _owner(config, reports) as (owner, calls, cert, rv):
        await _permission(owner, config)
        await _wait(lambda: owner.status().reason == "windows_proactor_required")
        assert cert.calls == [] and rv.endpoint is None


async def test_start_does_not_wait_for_slow_config_and_shutdown_does_not_wait_for_backend(reports):
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        assert release.wait(2)
        return _config()
    owner = DirectAccessLifecycle(object(), config_loader=blocked, rendezvous=FakeRendezvous([]))
    try:
        await asyncio.wait_for(owner.start(), 0.1)
        await _wait(entered.is_set)
        await asyncio.wait_for(owner.stop(), 0.1)
        assert owner.status().transport_running is False
    finally:
        release.set()


async def test_slow_certificate_worker_does_not_block_optout_or_shutdown(reports):
    config = _config()
    entered, release = threading.Event(), threading.Event()
    class SlowClient(FakeCertificateClient):
        def poll(self, **kw):
            self.calls.append(kw)
            entered.set()
            assert release.wait(3)
            return _ready()
    try:
        async with _owner(config, reports, cert=SlowClient()) as (owner, calls, cert, rv):
            await _permission(owner, config)
            await _wait(entered.is_set)
            config.remote_access.vibe_cloud.direct = False
            owner.notify_config_changed(config)
            await _wait(lambda: owner.status().state == "disabled", timeout=0.3)
            await asyncio.wait_for(owner.stop(), 0.3)
            release.set()
            await _wait(lambda: owner._poll_task is None)
            assert "ingress_start" not in calls and rv.endpoint is None
    finally:
        release.set()


async def test_late_supervisor_publication_cannot_undo_withdrawal(reports):
    config = _config()
    async with _owner(config, reports) as (owner, calls, cert, rv):
        await _permission(owner, config)
        await _wait(lambda: owner.status().state == "running")
        old_view = owner._supervisor.rendezvous
        owner._deny("permission_denied")
        old_view.publish("e" * 64, reason="running")
        assert rv.endpoint is None


async def test_pairing_replacement_during_start_requires_new_report(reports):
    config = _config()
    entered = asyncio.Event()
    release = asyncio.Event()
    class SlowIngress(FakeIngress):
        async def start(self):
            entered.set()
            await release.wait()
            await super().start()
    async with _owner(config, reports, ingress_type=SlowIngress) as (owner, calls, cert, rv):
        await _permission(owner, config)
        await asyncio.wait_for(entered.wait(), 1)
        assert await asyncio.to_thread(owner.stop_for_pairing_change)
        config.remote_access.vibe_cloud.instance_id = "instance-two"
        owner.notify_config_changed(config)
        await _wait(lambda: owner.status().state == "waiting-permission")
        release.set()
        await asyncio.sleep(0.05)
        assert "supervisor_start" not in calls and rv.endpoint is None
        await _permission(owner, config)
        await _wait(lambda: owner.status().state == "running")
        assert cert.calls[-1]["expected_pairing_fingerprint"] == module._config_signature(config)[3]


async def test_shutdown_during_start_discards_pending_resource(reports):
    config = _config()
    entered = asyncio.Event()
    class SlowIngress(FakeIngress):
        async def start(self):
            entered.set()
            await asyncio.Event().wait()
    async with _owner(config, reports, ingress_type=SlowIngress) as (owner, calls, cert, rv):
        await _permission(owner, config)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(owner.stop(), 0.3)
        assert "ingress_stop" in calls and "supervisor_start" not in calls
        assert rv.endpoint is None


async def test_expired_certificate_stops_published_transport(reports):
    config = _config()
    async with _owner(config, reports) as (owner, calls, cert, rv):
        await _permission(owner, config)
        await _wait(lambda: owner.status().state == "running")
        owner._certificate_result = _ready(certificate_not_after="2000-01-01T00:00:00+00:00")
        owner._wake.set()
        await _wait(lambda: owner.status().state == "waiting-certificate")
        assert "supervisor_stop" in calls and rv.endpoint is None


async def test_suspend_route_under_writer_config_lock_does_not_wait_for_worker(reports, monkeypatch):
    from config.v2_config import config_file_lock
    from vibe import ui_server

    config = _config()
    config.save()
    async with _owner(config, reports) as (owner, calls, cert, rv):
        await _permission(owner, config)
        await _wait(lambda: owner.status().state == "running")
        monkeypatch.setattr(module, "_ACTIVE", owner)
        monkeypatch.setattr(ui_server, "show_cli_event_token", lambda: "isolated-cli-token")
        def writer_request():
            with config_file_lock():
                return ui_server.app.test_client().post(
                    "/api/remote-access/direct/suspend", base_url="http://127.0.0.1",
                    headers={"X-Vibe-Show-Client": "cli", "X-Vibe-Show-Cli-Token": "isolated-cli-token"},
                )
        response = await asyncio.wait_for(asyncio.to_thread(writer_request), 2)
        assert response.status_code == 200 and response.get_json()["stopped"]
        assert "supervisor_stop" in calls and "ingress_stop" in calls


def test_suspend_cookie_guard_reads_config_while_another_process_holds_writer_lock():
    """Exercise the real pre-exemption config read with non-reentrant contention.

    The existing in-process owner test covers resource stop ordering. This
    separate interpreter proves the global HTTP guard can read the persisted
    pairing while a different process holds the shared writer file lock.
    """
    from config.v2_config import config_file_lock
    from core.show_pages import show_cli_event_token

    config = _config(direct=True)
    config.save()
    show_cli_event_token()  # Seed only the isolated test home's local CLI token.
    script = textwrap.dedent("""\
        import asyncio
        import json
        import os
        import socket
        import sys
        import config.v2_config as config_module
        from core.show_pages import show_cli_event_token
        from vibe import direct_lifecycle, remote_access, ui_server

        # Positive control: this interpreter really contends on the parent's
        # persisted lock, rather than succeeding via same-process reentrancy.
        config_module.CONFIG_FILE_LOCK_TIMEOUT_SECONDS = 0.1
        try:
            with config_module.config_file_lock():
                raise AssertionError("writer lock was not held across processes")
        except TimeoutError:
            pass

        def no_network(*args, **kwargs):
            raise AssertionError("the local suspend path must not use a network dependency")
        socket.socket.connect = no_network
        remote_access._report_runtime_status_async = lambda *args, **kwargs: None
        original_load = ui_server._load_remote_access_config
        cookie_guard_loads = []
        def observed_load():
            config = original_load()
            if sys._getframe(1).f_code is ui_server.enforce_remote_access_cookie.__code__:
                # A swallowed lock timeout returning None must not pass merely
                # because a loopback request can still become a local Owner.
                assert config is not None
                cookie_guard_loads.append(config.remote_access.vibe_cloud.instance_id)
            return config
        ui_server._load_remote_access_config = observed_load

        async def exercise():
            owner = direct_lifecycle.DirectAccessLifecycle(ui_server.app)
            direct_lifecycle.register_active_lifecycle(owner)
            await owner.start()
            try:
                async with asyncio.timeout(2):
                    while owner._config is None:
                        await asyncio.sleep(0.005)
                def request_stop():
                    return ui_server.app.test_client().post(
                        "/api/remote-access/direct/suspend", base_url="http://127.0.0.1",
                        headers={"X-Vibe-Show-Client": "cli",
                                 "X-Vibe-Show-Cli-Token": show_cli_event_token()},
                    )
                response = await asyncio.wait_for(asyncio.to_thread(request_stop), 3)
                assert response.status_code == 200, response.get_json()
                assert response.get_json() == {"ok": True, "stopped": True}
                assert owner._pairing_suspended
                assert cookie_guard_loads == ["instance-one"]
                print(json.dumps({"pid": os.getpid(), "read_under_foreign_lock": True}))
            finally:
                await owner.stop()
        asyncio.run(exercise())
    """)
    with config_file_lock():
        child = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout)
    assert result["pid"] != os.getpid() and result["read_under_foreign_lock"] is True


async def test_status_running_only_after_publication_and_no_sensitive_material(reports):
    config = _config()
    class Unpublished(FakeSupervisor):
        async def start(self):
            self.running = True
    async with _owner(config, reports, supervisor_type=Unpublished) as (owner, calls, cert, rv):
        await _permission(owner, config)
        await _wait(lambda: owner.status().transport_running)
        assert owner.status().state == "starting" and rv.endpoint is None
        payload = owner.status().as_dict()
        assert set(payload) == {"enabled", "state", "reason", "certificate_not_after", "transport_running", "direct_access_allowed"}
        text = json.dumps(payload)
        for sensitive in ["device-secret", "alice-app", "/test/", "endpoint_id"]:
            assert sensitive not in text
