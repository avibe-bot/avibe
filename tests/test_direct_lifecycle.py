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

    def publish(self, endpoint, *, reason="not_running", retire_at=None):
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
        return SimpleNamespace(running=self.running, endpoint_id=self.endpoint_id, reason=self.reason, connections=3, refusals=2)

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
        supervisor_factory=lambda executable, ingress, rendezvous, **kw: supervisor_type(calls, ingress, rendezvous, **kw),
        binary_resolver=options.pop("binary_resolver", lambda config: "/injected/test-sidecar"),
        rendezvous=rendezvous, config_loader=options.pop("config_loader", lambda: config), **options,
    )
    await owner.start()
    try:
        await _wait(lambda: owner._config is not None)
        yield owner, calls, cert, rendezvous
    finally:
        await owner.stop()


async def _permission(owner, config, allowed=True, **fields):
    ticket = owner.runtime_status_request(config)
    assert ticket is not None
    owner.submit_runtime_status_response(ticket, {"ok": True, "direct_access_allowed": allowed, **fields})
    await _wait(lambda: owner._last_response_sequence == ticket.sequence)


def test_setting_default_legacy_config_and_roundtrip():
    config = _config(direct=False)
    config.save()
    path = paths.get_config_path()
    payload = json.loads(path.read_text())
    cloud = payload["remote_access"]["vibe_cloud"]
    assert cloud["direct"] is False and cloud["direct_choice"] == "off"
    assert V2Config.load().remote_access.vibe_cloud.direct is False
    # The released writer emitted false even for an untouched choice. It left
    # no provenance; migration intentionally interprets that shape as default.
    del cloud["direct_choice"]
    path.write_text(json.dumps(payload))
    migrated = V2Config.load()
    assert migrated.remote_access.vibe_cloud.direct is True
    assert migrated.remote_access.vibe_cloud.direct_choice == "default"
    assert V2Config.default().remote_access.vibe_cloud.direct is True
    migrated.remote_access.vibe_cloud.set_path_choice("direct", False)
    migrated.save()
    assert V2Config.load().remote_access.vibe_cloud.direct is False


def test_generic_save_preserves_host_choices_and_does_not_expose_markers():
    from vibe import api

    config = V2Config.default()
    cloud = config.remote_access.vibe_cloud
    cloud.set_path_choice('direct', False)
    cloud.set_path_choice('public_relay_trial', False)
    config.save()
    projection = api.config_to_payload(config)['remote_access']['vibe_cloud']
    assert 'direct_choice' not in projection and 'public_relay_choice' not in projection
    api.save_config({'remote_access': {'vibe_cloud': {'direct': False, 'public_relay_trial': False,
                    'direct_choice': 'default', 'public_relay_choice': 'on'}}}, generic_remote_access=True)
    restored = V2Config.load().remote_access.vibe_cloud
    assert not restored.direct and not restored.public_relay_trial
    assert restored.direct_choice == restored.public_relay_choice == 'off'


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


@pytest.mark.parametrize("bad_pin,reason", [("pending", "other"), ("tampered", "integrity"), ("unavailable", "download")])
async def test_failed_auto_install_backs_off_without_starting(reports, published, monkeypatch, bad_pin, reason):
    from core.direct_transport_runtime import DirectTransportRuntimeManager

    payload, manifest, downloads, sources = published
    if bad_pin == "pending":
        payload["release_state"] = "pending"
        manifest.write_text(json.dumps(payload))
    else:
        source = sources[payload["archives"]["linux-x64"]["url"]]
        if bad_pin == "unavailable":
            source.unlink()  # hermetic fetch_to_path fails before returning an archive
        else:
            source.write_bytes(b"x" * source.stat().st_size)
    attempts = []
    ensure = DirectTransportRuntimeManager.ensure
    def counted(manager):
        attempts.append(time.monotonic())
        return ensure(manager)
    monkeypatch.setattr(DirectTransportRuntimeManager, "ensure", counted)
    monkeypatch.setattr(module, "INSTALL_RETRY_INITIAL_SECONDS", 0.3)
    monkeypatch.setattr(module, "BINARY_REFRESH_INTERVAL_SECONDS", 0.02)
    config = _config()
    async with _owner(config, reports, binary_resolver=None) as (owner, calls, cert, rv):
        await _permission(owner, config)
        await _wait(lambda: owner.status().reason == "transport_install_failed")
        await _wait(lambda: len(attempts) >= 2 and owner.status().reason == "transport_install_failed" and owner._install_task is None)
        assert attempts[1] - attempts[0] >= 0.3
        assert owner.report_fields(owner.runtime_status_request(config)) == {
            "direct_state": "install_failed", "direct_reason": reason,
        }
        assert not cert.calls and rv.endpoint is None and "supervisor_start" not in calls
        assert DirectTransportRuntimeManager().resolve_binary() is None
        assert not downloads if bad_pin == "pending" else len(downloads) == 2


async def test_auto_failure_logs_hide_manager_details_but_not_other_callers(reports, published, monkeypatch, caplog):
    from core import managed_runtime
    from core.direct_transport_runtime import DirectTransportRuntimeManager

    entered, release = threading.Event(), threading.Event()
    def fail_lock(manager):
        entered.set()
        assert release.wait(3)
        raise OSError("/private/path/owner/test-lock")
    monkeypatch.setattr(DirectTransportRuntimeManager, "_acquire_mutation_lock", fail_lock)
    caplog.set_level("INFO")
    config = _config()
    async with _owner(config, reports, binary_resolver=None) as (owner, *_):
        await _permission(owner, config)
        try:
            await _wait(entered.is_set)
            managed_runtime.logger.warning("unrelated manager diagnostic")
        finally:
            release.set()
        await _wait(lambda: owner.status().reason == "transport_install_failed")
    records = [r for r in caplog.records if r.name in (managed_runtime.logger.name, module.logger.name)]
    assert [r.getMessage() for r in records] == ["Direct transport install reason=transport_installing",
        "unrelated manager diagnostic", "Direct transport install reason=transport_install_failed"]
    assert all(not r.exc_info for r in records)


async def test_skipped_install_and_prior_failure_do_not_delay_reenable(reports, published, monkeypatch):
    from core.direct_transport_runtime import DirectTransportRuntimeManager

    config = _config()
    entered, release = threading.Event(), threading.Event()
    worker = threading.local()
    install_pin = DirectAccessLifecycle._install_pin
    def mark_worker(self, *args):
        worker.install = True
        try:
            return install_pin(self, *args)
        finally:
            worker.install = False
    def load():
        if getattr(worker, "install", False) and not entered.is_set():
            entered.set()
            assert release.wait(3)
        return config
    attempts = []
    ensure = DirectTransportRuntimeManager.ensure
    def fail_once(manager):
        attempts.append(1)
        return {"ok": False} if len(attempts) == 1 else ensure(manager)
    monkeypatch.setattr(DirectAccessLifecycle, "_install_pin", mark_worker)
    monkeypatch.setattr(DirectTransportRuntimeManager, "ensure", fail_once)
    async with _owner(config, reports, config_loader=load, binary_resolver=None) as (owner, *_):
        async def toggle(enabled):
            config.remote_access.vibe_cloud.direct = enabled
            owner.notify_config_changed(config)
            await _wait(lambda: owner.status().enabled == enabled)
            if enabled:
                await _permission(owner, config)
        try:
            await _permission(owner, config)
            await _wait(entered.is_set)
            await toggle(False)
            await toggle(True)
        finally:
            release.set()
        await _wait(lambda: owner.status().reason == "transport_install_failed")
        assert attempts == [1] and owner._install_delay == 2 * module.INSTALL_RETRY_INITIAL_SECONDS
        await toggle(False)
        await toggle(True)
        await _wait(lambda: owner.status().state == "running")
        assert attempts == [1, 1]


async def test_sleep_between_install_scheduling_and_worker_admission_skips_download(reports, monkeypatch):
    from core.direct_transport_runtime import DirectTransportRuntimeManager
    config = _config()
    clock, attempts = [300.0], []
    monkeypatch.setattr(module, 'suspend_elapsed', lambda: clock[0])
    monkeypatch.setattr(DirectTransportRuntimeManager, 'ensure', lambda self: attempts.append(True))
    original = DirectAccessLifecycle._install_pin
    returned = threading.Event()
    def resume_after_sleep(self, *args):
        clock[0] += 901
        try:
            return original(self, *args)
        finally:
            returned.set()
    monkeypatch.setattr(DirectAccessLifecycle, '_install_pin', resume_after_sleep)
    async with _owner(config, reports, binary_resolver=lambda cfg: None) as (owner, *_):
        await _permission(owner, config)
        await _wait(returned.is_set)
        await _wait(lambda: owner._install_task is None)
        assert not attempts and not owner._install_failed


async def test_auto_install_once_only_when_enabled(reports, published, monkeypatch):
    from core.direct_transport_runtime import DirectTransportRuntimeManager

    config = _config(direct=False)
    attempts = []
    entered, release = threading.Event(), threading.Event()
    ensure = DirectTransportRuntimeManager.ensure
    def slow(manager):
        attempts.append(1)
        entered.set()
        assert release.wait(4)
        return ensure(manager)
    monkeypatch.setattr(DirectTransportRuntimeManager, "ensure", slow)
    async with _owner(config, reports, binary_resolver=None) as (owner, calls, cert, rv):
        assert not attempts and not published[2]
        config.remote_access.vibe_cloud.direct = True
        owner.notify_config_changed(config)
        try:
            await _wait(lambda: owner.status().state == 'transport-not-installed')
            await _permission(owner, config, False)
            await asyncio.sleep(0.15)
            assert not attempts and not published[2]
            await _permission(owner, config)
            await _wait(entered.is_set)
            await _wait(lambda: owner.status().state == "installing")
            await asyncio.sleep(0.15)
            assert not cert.calls and rv.endpoint is None and attempts == [1]
        finally:
            release.set()
        await _wait(lambda: owner.status().state == "running")
        assert attempts == [1] and len(published[2]) == 1 and rv.endpoint is not None


@pytest.mark.parametrize('contents', [b'broken', b'{}', b'{"version":true,"binding":null}',
                                      b'{"version":1,"binding":"bad"}', b'{"version":1,"binding":null,"binding":null}'])
def test_invalid_enrollment_never_selects_relay(tmp_path, monkeypatch, contents):
    from vibe import connection_settings as cs
    path = tmp_path / 'enrollment.json'
    monkeypatch.setattr(cs, '_relay_enrollment_path', lambda: path)
    config = _config()
    path.write_bytes(contents)
    assert cs.relay_enrollment_status(config) == 'unknown'
    assert not module._effective_relay(config)
    config.remote_access.vibe_cloud.set_path_choice('public_relay_trial', True)
    assert not module._effective_relay(config)


def test_unreadable_enrollment_is_not_absent(tmp_path, monkeypatch):
    from vibe import connection_settings as cs
    path = tmp_path / 'enrollment.json'
    monkeypatch.setattr(cs, '_relay_enrollment_path', lambda: path)
    config = _config()
    assert module._effective_relay(config)
    cs.enroll_from_host(config)
    original = Path.read_text
    def refuse(self, *args, **kwargs):
        if self == path:
            raise PermissionError('fixture')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', refuse)
    assert cs.relay_enrollment_status(config) == 'unknown' and not module._effective_relay(config)


def test_the_relay_is_wanted_unless_the_user_turned_it_off(tmp_path, monkeypatch):
    from vibe import connection_settings as cs

    monkeypatch.setattr(cs, "_relay_enrollment_path", lambda: tmp_path / "enrollment.json")
    config = _config()
    cloud = config.remote_access.vibe_cloud
    # Untouched: no host enrollment and no local opt-in. This grants nothing
    # by itself; the server's fresh bit is asked for with every report.
    assert (cloud.public_relay_trial, cloud.public_relay_choice) == (False, "default")
    assert not cs.relay_enrolled(config)
    assert module._config_signature(config)[-1] is True
    # An explicit off wins, with or without an enrollment, and across a save.
    cloud.set_path_choice("public_relay_trial", False)
    assert module._config_signature(config)[-1] is False
    cs.enroll_from_host(config)
    assert module._config_signature(config)[-1] is False
    config.save()
    reloaded = V2Config.load()
    assert reloaded.remote_access.vibe_cloud.public_relay_choice == "off"
    assert module._config_signature(reloaded)[-1] is False
    cloud.set_path_choice("public_relay_trial", True)
    assert module._config_signature(config)[-1] is True


def test_an_enrolled_pairing_whose_trial_was_switched_off_before_choices_were_recorded_stays_off(tmp_path, monkeypatch):
    from vibe import connection_settings as cs

    monkeypatch.setattr(cs, "_relay_enrollment_path", lambda: tmp_path / "enrollment.json")
    config = _config()
    cs.enroll_from_host(config)
    config.save()
    path = paths.get_config_path()
    payload = json.loads(path.read_text())
    stored = payload["remote_access"]["vibe_cloud"]
    # What the released writer left after `public-trial on` and then off:
    # the setting false, and nothing that says who made it so.
    stored["public_relay_trial"] = False
    stored.pop("public_relay_choice", None)
    path.write_text(json.dumps(payload))
    switched_off = V2Config.load()
    cloud = switched_off.remote_access.vibe_cloud
    assert (cloud.public_relay_trial, cloud.public_relay_choice) == (False, "default")
    assert cs.relay_enrolled(switched_off)
    assert module._config_signature(switched_off)[-1] is False
    # The same stored shape on a pairing the host never enrolled is the
    # untouched default, and follows the server.
    cloud.instance_secret = "different-pairing-credential"
    assert not cs.relay_enrolled(switched_off)
    assert module._config_signature(switched_off)[-1] is True
    # Enrolled and left on: the released writer's true is an explicit on.
    stored["public_relay_trial"] = True
    path.write_text(json.dumps(payload))
    left_on = V2Config.load()
    assert left_on.remote_access.vibe_cloud.public_relay_choice == "on"
    assert module._config_signature(left_on)[-1] is True


@pytest.mark.parametrize("grant", [{}, {"public_traversal_allowed": False}, {"public_traversal_allowed": "true"},
                                   {"public_traversal_allowed": 1}, {"public_traversal_allowed": None}])
async def test_without_the_servers_true_bit_no_public_relay_policy_is_used(reports, grant):
    config = _config()
    policies = []
    class Recorder(FakeSupervisor):
        def __init__(self, *args, relay_policy=None):
            super().__init__(*args)
            policies.append(relay_policy)

    async with _owner(config, reports, supervisor_type=Recorder) as (owner, calls, _, rv):
        await _permission(owner, config, **grant)
        await _wait(lambda: owner.status().state == "running")
        assert policies == [None] and owner._public_traversal_allowed is False
        # The bit, fresh and true, is all it takes; a renewal without it
        # retires the public endpoint and ordinary direct goes on.
        await _permission(owner, config, public_traversal_allowed=True)
        await _wait(lambda: policies == [None, "public-trial-v1"] and owner.status().state == "running")
        await _permission(owner, config, **grant)
        await _wait(lambda: policies == [None, "public-trial-v1", None] and owner.status().state == "running")
        assert calls.count("supervisor_stop") == 2


async def test_the_public_relay_ends_when_the_servers_grant_expires_or_is_denied(reports, monkeypatch):
    config = _config()
    monkeypatch.setattr(module, "PERMISSION_MAX_AGE_SECONDS", 0.3)
    policies = []
    class Recorder(FakeSupervisor):
        def __init__(self, *args, relay_policy=None):
            super().__init__(*args)
            policies.append(relay_policy)

    async with _owner(config, reports, supervisor_type=Recorder) as (owner, calls, _, rv):
        await _permission(owner, config, public_traversal_allowed=True)
        await _wait(lambda: owner.status().state == "running")
        assert policies == ["public-trial-v1"] and owner._public_traversal_allowed is True
        # Nothing renews it: the grant ages out, and the relay with it.
        await _wait(lambda: owner.status().reason == "permission_expired")
        assert owner._public_traversal_allowed is False
        assert rv.endpoint is None and "supervisor_stop" in calls
        assert policies == ["public-trial-v1"]
        # An expired true is not carried into the next grant.
        await _permission(owner, config)
        await _wait(lambda: owner.status().state == "running")
        assert policies == ["public-trial-v1", None]
        await _permission(owner, config, public_traversal_allowed=True)
        await _wait(lambda: len(policies) == 3 and owner.status().state == "running")
        # A denial of direct itself takes the relay too, whatever else it says.
        await _permission(owner, config, False, public_traversal_allowed=True)
        await _wait(lambda: owner.status().reason == "permission_denied")
        assert owner._public_traversal_allowed is False and rv.endpoint is None
        assert policies == ["public-trial-v1", None, "public-trial-v1"]


async def test_an_explicit_off_wins_over_the_servers_true_bit(reports):
    config = _config()
    config.remote_access.vibe_cloud.set_path_choice("public_relay_trial", False)
    policies, asked = [], []
    class Recorder(FakeSupervisor):
        def __init__(self, *args, relay_policy=None):
            super().__init__(*args)
            policies.append(relay_policy)

    def resolver(config):
        asked.append(module._effective_relay(config))
        return "/injected/test-sidecar"

    async with _owner(config, reports, supervisor_type=Recorder, binary_resolver=resolver) as (owner, calls, _, rv):
        await _permission(owner, config, public_traversal_allowed=True)
        await _wait(lambda: owner.status().state == "running")
        assert policies == [None] and owner._public_traversal_allowed is False
        # Turned on again, the next fresh true bit gives the relay; off again
        # retires it at once, without waiting for the server.
        config.remote_access.vibe_cloud.set_path_choice("public_relay_trial", True)
        owner.notify_config_changed(config)
        await _wait(lambda: owner.status().state == "waiting-permission")
        await _permission(owner, config, public_traversal_allowed=True)
        await _wait(lambda: policies == [None, "public-trial-v1"] and owner.status().state == "running")
        config.remote_access.vibe_cloud.set_path_choice("public_relay_trial", False)
        owner.notify_config_changed(config)
        await _wait(lambda: owner.status().state == "waiting-permission" and rv.endpoint is None)
        await _permission(owner, config, public_traversal_allowed=True)
        await _wait(lambda: policies == [None, "public-trial-v1", None] and owner.status().state == "running")
    # The pin is asked for the relay only while the relay may be used, and
    # the user's stored choice is never what was edited to ask.
    assert True in asked and asked[0] is False and asked[-1] is False
    assert config.remote_access.vibe_cloud.public_relay_choice == "off"


async def test_trial_lifecycle_requires_compatible_install_and_fresh_permission_then_retires_on_off(reports, published, monkeypatch):
    from core.direct_transport_runtime import DirectTransportRuntimeManager

    # No host enrollment and no local opt-in: the server's bit decides.
    config = _config()
    manager = DirectTransportRuntimeManager()
    assert manager.ensure()["ok"]
    monkeypatch.setattr(module, "BINARY_REFRESH_INTERVAL_SECONDS", 0.02)
    installs, ensure = [], DirectTransportRuntimeManager.ensure
    monkeypatch.setattr(DirectTransportRuntimeManager, "ensure", lambda self, **kw: (installs.append(1), ensure(self, **kw))[1])
    policies = []
    class Recorder(FakeSupervisor):
        def __init__(self, *args, relay_policy=None):
            super().__init__(*args)
            policies.append(relay_policy)

    async with _owner(config, reports, supervisor_type=Recorder, binary_resolver=None) as (owner, calls, _, rv):
        # Without the bit the ordinary pin is all that is asked for, so a pin
        # that is not a reviewed one still serves ordinary direct.
        await _permission(owner, config)
        await _wait(lambda: owner.status().state == "running")
        assert policies == [None]
        await _permission(owner, config, public_traversal_allowed=True)
        await _wait(lambda: owner.status().state == "transport-not-installed")
        assert policies == [None]
        # The installed pin is only not a reviewed one: it is not missing,
        # and several observations later nothing has tried to install it.
        await asyncio.sleep(0.2)
        assert installs == [] and owner.status().state == "transport-not-installed"
        await _permission(owner, config, False)
        payload, manifest, _, _ = published
        payload["provenance"] = {"source_sha": "578659184dfa38bc87261a6fa832f9b4c65a1c90"}
        manifest.write_text(json.dumps(payload))
        assert DirectTransportRuntimeManager().ensure()["ok"]
        await _wait(lambda: owner.status().state == "waiting-permission")
        assert policies == [None]
        await _permission(owner, config, public_traversal_allowed=True)
        await _wait(lambda: owner.status().state == "running")
        assert policies == [None, "public-trial-v1"]
        # A valid ordinary-direct renewal from an older backend removes only
        # the public policy. Nothing local grants it again.
        await _permission(owner, config)
        await _wait(lambda: policies == [None, "public-trial-v1", None] and owner.status().state == "running")
        assert "supervisor_stop" in calls
        await _permission(owner, config, public_traversal_allowed=True)
        await _wait(lambda: len(policies) == 4 and owner.status().state == "running")
        assert policies[-1] == "public-trial-v1"
        generation = owner._generation
        config.remote_access.vibe_cloud.set_path_choice("public_relay_trial", False)
        owner.notify_config_changed(config)
        await _wait(lambda: owner.status().state == "waiting-permission" and rv.endpoint is None)
        assert owner._generation > generation and "supervisor_stop" in calls
        await _permission(owner, config, public_traversal_allowed=True)
        await _wait(lambda: owner.status().state == "running")
        assert policies == [None, "public-trial-v1", None, "public-trial-v1", None]


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
        async with _owner(config, reports, cert=WaitingCertificate(), binary_resolver=None) as (owner, calls, cert, rv):
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
            await _wait(lambda: calls.count("supervisor_stop") == 1)
            await _wait(lambda: owner.status().state == "running")
            assert manager.resolve_binary() == binary and binary.read_bytes() != b"tampered test fixture"
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
            from core.direct_transport_runtime import DirectTransportRuntimeManager
            monkeypatch.setattr(DirectTransportRuntimeManager, "ensure", lambda self: {"ok": False})
            available[0] = False
            await _wait(lambda: owner.status().reason == "transport_install_failed")
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
        assert cert.calls[0]["permission_deadline"] > module.suspend_elapsed()
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


@pytest.mark.parametrize("error,coarse", [("acme_rate_limited", "rate_limited"),
                                        ("backend_unavailable", "provider"),
                                        ("dns_challenge_visibility_timeout", "timeout")])
async def test_retryable_certificate_failure_preserves_tunnel_and_provider_deadline(reports, monkeypatch, error, coarse):
    monkeypatch.setattr(module, "INSTALL_RETRY_INITIAL_SECONDS", 0.05)
    config = _config()
    deadline = time.time() + 0.65
    cert = FakeCertificateClient([CertificateResult("failed", last_error_code=error, next_poll_at=deadline)])
    async with _owner(config, reports, cert=cert) as (owner, calls, _, rv):
        await _permission(owner, config)
        await _wait(lambda: owner.report_fields(owner.runtime_status_request(config)).get("direct_state") == "certificate_failed")
        assert owner.report_fields(owner.runtime_status_request(config))["direct_reason"] == coarse
        assert rv.endpoint is None and "supervisor_start" not in calls and config.remote_access.vibe_cloud.enabled
        assert owner._poll_at >= deadline
        await _wait(lambda: owner.status().state == "running")
        assert cert.calls[-1]["retry"] is True and time.time() >= deadline


async def test_daily_turnover_latches_and_retries_failed_stop_before_replacement(reports, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module, 'suspend_elapsed', lambda: clock[0])
    monkeypatch.setattr(module, 'INSTALL_RETRY_INITIAL_SECONDS', 0.04)
    monkeypatch.setattr(module, 'INSTALL_RETRY_MAX_SECONDS', 0.08)
    monkeypatch.setattr(module, 'TICK_SECONDS', 0.005)
    attempts = []
    class FaultyStop(FakeSupervisor):
        async def stop(self):
            attempts.append((self.loop.time(), owner._generation, calls.count('supervisor_start'), rv.endpoint))
            if len(attempts) <= 3:
                raise OSError('fixture retirement failure')
            await super().stop()
    config = _config()
    async with _owner(config, reports, supervisor_type=FaultyStop) as (owner, calls, _, rv):
        await _permission(owner, config)
        await _wait(lambda: owner.status().state == 'running')
        before = owner._generation
        clock[0] = owner._endpoint_retire_at
        # Model a binding with a renewed receipt when the old key expires.
        await _permission(owner, config)
        await _wait(lambda: len(attempts) == 1)
        await _permission(owner, config)
        await _wait(lambda: calls.count('supervisor_start') == 2)
        assert [row[1:] for row in attempts] == [(before + 1, 1, None)] * 4
        for prior, later, minimum in zip(attempts, attempts[1:], (0.04, 0.08, 0.08)):
            assert later[0] - prior[0] >= minimum
        assert rv.endpoint and owner._turnover_retry_at is None


@pytest.mark.parametrize('final_stop', ['success', 'raises', 'stalls', 'already_in_flight'])
async def test_shutdown_bypasses_turnover_backoff_once_without_replacement(reports, monkeypatch, final_stop):
    clock = [100.0]
    release = asyncio.Event()
    attempts = []
    monkeypatch.setattr(module, 'suspend_elapsed', lambda: clock[0])
    monkeypatch.setattr(module, 'TURNOVER_SHUTDOWN_WAIT_SECONDS', 0.05)
    class FaultyStop(FakeSupervisor):
        async def stop(self):
            attempts.append(self)
            if len(attempts) == 1 and final_stop != 'already_in_flight':
                raise OSError('first retirement attempt failed')
            if final_stop == 'raises':
                raise OSError('final teardown failed')
            if final_stop in {'stalls', 'already_in_flight'}:
                await release.wait()
            await super().stop()
    config = _config()
    async with _owner(config, reports, supervisor_type=FaultyStop) as (owner, calls, _, rv):
        await _permission(owner, config)
        await _wait(lambda: owner.status().state == 'running')
        clock[0] = owner._endpoint_retire_at
        await _permission(owner, config)
        if final_stop == 'already_in_flight':
            await _wait(lambda: len(attempts) == 1)
        else:
            await _wait(lambda: (owner._turnover_retry_at or 0) > owner._loop.time() + 20)
        try:
            await asyncio.wait_for(owner.stop(), timeout=0.5)
            assert len(attempts) == (1 if final_stop == 'already_in_flight' else 2)
            assert rv.endpoint is None and calls.count('supervisor_start') == 1
            assert (owner._supervisor is None) == (final_stop == 'success')
        finally:
            release.set()
            if owner._turnover_stop is not None:
                await asyncio.wait_for(owner._turnover_stop, timeout=0.5)


async def test_start_error_backs_off_then_publishes_without_tunnel_change(reports, monkeypatch):
    monkeypatch.setattr(module, "INSTALL_RETRY_INITIAL_SECONDS", 0.2)
    attempts = []
    class RetrySupervisor(FakeSupervisor):
        async def start(self):
            attempts.append(time.monotonic())
            if len(attempts) == 1:
                raise OSError("private details must not be reported")
            await super().start()
    config = _config()
    async with _owner(config, reports, supervisor_type=RetrySupervisor) as (owner, _, _, rv):
        await _permission(owner, config)
        await _wait(lambda: owner.status().reason == "transport_start_failed")
        assert owner.report_fields(owner.runtime_status_request(config)) == {"direct_state": "install_failed", "direct_reason": "other"}
        assert config.remote_access.vibe_cloud.enabled and not rv.endpoint
        await _wait(lambda: owner.status().state == "running")
        assert len(attempts) == 2 and attempts[1] - attempts[0] >= 0.2


async def test_permission_denial_missing_field_and_resume_stop_serving(reports):
    config = _config()
    async with _owner(config, reports) as (owner, calls, cert, rv):
        await _permission(owner, config)
        await _wait(lambda: owner.status().state == "running")
        running_ticket = owner.runtime_status_request(config)
        assert owner.report_fields(running_ticket) == {"direct_state": "running"}
        owner.submit_runtime_status_response(running_ticket, {"ok": True})
        await _wait(lambda: owner.status().reason == "permission_denied")
        assert owner.report_fields(running_ticket) == {}
        assert owner.report_fields(owner.runtime_status_request(config)) == {"direct_state": "denied"}
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
        assert owner.report_fields(owner.runtime_status_request(config)) == {
            "direct_state": "denied", "direct_reason": "permission_expired",
        }
        assert "supervisor_stop" in calls and "ingress_stop" in calls
        assert not owner.status().transport_running and rv.endpoint is None


@pytest.mark.parametrize('change_endpoint', [False, True])
async def test_only_heartbeat_issued_after_certificate_refusal_can_restore_grants(reports, change_endpoint):
    config = _config()
    entered, release = threading.Event(), threading.Event()
    class RefuseOnce(FakeCertificateClient):
        def poll(self, **opts):
            self.calls.append(opts)
            if len(self.calls) == 1:
                entered.set()
                assert release.wait(3)
                return CertificateResult('blocked', last_error_code='direct_access_not_allowed')
            return _ready()
    class Trial(FakeSupervisor):
        def __init__(self, *args, relay_policy):
            assert relay_policy == 'public-trial-v1'
            super().__init__(*args)
    async with _owner(config, reports, cert=RefuseOnce(), supervisor_type=Trial) as (owner, _, cert, rv):
        try:
            await _permission(owner, config)
            await _wait(entered.is_set)
            if change_endpoint:
                await _permission(owner, config, public_traversal_allowed=True)
            stale = owner.runtime_status_request(config)
        finally:
            release.set()
        await _wait(lambda: owner.status().reason == 'permission_denied')
        owner.submit_runtime_status_response(stale, {'direct_access_allowed': True, 'public_traversal_allowed': True})
        await asyncio.sleep(0)  # deliver the queued, pre-refusal ticket
        assert not owner._permission_fresh() and not owner._public_traversal_allowed
        assert rv.endpoint is None and len(cert.calls) == 1
        await _permission(owner, config, public_traversal_allowed=True)
        await _wait(lambda: rv.endpoint is not None)
        assert cert.calls[-1]['resume'] and owner._public_traversal_allowed


@pytest.mark.parametrize('reverse', [False, True])
async def test_two_heartbeat_tickets_are_ordered_across_endpoint_generation(reports, reverse):
    config = _config()
    async with _owner(config, reports) as (owner, _, _, rv):
        older = owner.runtime_status_request(config)
        newer = owner.runtime_status_request(config)
        replies = [(older, {'direct_access_allowed': True, 'public_traversal_allowed': True}),
                   (newer, {'direct_access_allowed': False})]
        for ticket, reply in (reversed(replies) if reverse else replies):
            owner.submit_runtime_status_response(ticket, reply)
        await _wait(lambda: owner.status().reason == 'permission_denied')
        assert owner._last_response_sequence == newer.sequence and not owner._public_traversal_allowed
        assert owner._generation > older.generation and rv.endpoint is None


async def test_sleep_expires_ordinary_and_public_grants_without_event_loop_time_advancing(reports, monkeypatch):
    clock = [500.0]
    monkeypatch.setattr(module, 'suspend_elapsed', lambda: clock[0])
    config = _config()
    class Trial(FakeSupervisor):
        def __init__(self, *args, relay_policy):
            assert relay_policy == 'public-trial-v1'
            super().__init__(*args)
    async with _owner(config, reports, supervisor_type=Trial) as (owner, _, cert, rv):
        await _permission(owner, config, public_traversal_allowed=True)
        await _wait(lambda: owner.status().state == 'running')
        assert cert.calls[0]['permission_deadline'] == clock[0] + 900
        clock[0] += 3600
        assert not owner._can_commit(owner._generation, owner._ingress)
        await _wait(lambda: owner.status().reason == 'permission_expired')
        assert not owner._public_traversal_allowed and rv.endpoint is None


@pytest.mark.parametrize('status,code,denied', [(401, 'invalid_device_secret', True),
    (403, 'instance_credential_mismatch', True), (409, 'instance_inactive', True), (404, 'instance_not_found', True),
    (503, 'cloud_policy_unavailable', False), (503, 'invalid_device_secret', False), (403, 'backend_http_error', False)])
async def test_heartbeat_refusal_withdraws_but_outage_does_not_renew(reports, monkeypatch, status, code, denied):
    config = _config()
    monkeypatch.setattr(remote_access, 'runtime_status_payload', lambda *args, **kw: {'direct_paths_enabled': True})
    def refuse(*args, **kw):
        raise remote_access.BackendRequestError(status, {'error': code, 'detail': 'fixture'})
    monkeypatch.setattr(remote_access, '_json_request', refuse)
    async with _owner(config, reports) as (owner, _, _, rv):
        monkeypatch.setattr(module, 'active_lifecycle', lambda: owner)
        await _permission(owner, config)
        await _wait(lambda: owner.status().state == 'running')
        receipt = owner._permission_received_at
        assert not (await asyncio.to_thread(remote_access.report_runtime_status, config))['ok']
        if denied:
            await _wait(lambda: owner.status().reason == 'permission_denied')
            assert rv.endpoint is None
        else:
            assert rv.endpoint and owner._permission_received_at == receipt


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
    monkeypatch.setattr(module, "suspend_elapsed", lambda: clock[0])
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
                # This stop's own token, so only that stop can be undone.
                body = response.get_json()
                assert body == {"ok": True, "stopped": True, "token": owner._suspension_token}
                assert len(body["token"]) == 32
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
        assert set(payload) == {"enabled", "state", "reason", "certificate_not_after", "transport_running", "direct_access_allowed", "streams_opened", "refusals"}
        assert (payload["streams_opened"], payload["refusals"]) == (3, 2)
        text = json.dumps(payload)
        for sensitive in ["device-secret", "alice-app", "/test/", "endpoint_id"]:
            assert sensitive not in text
