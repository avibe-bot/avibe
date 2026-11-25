"""The service, not the publisher or the final Desktop reply, adopts the local binding."""
from contextlib import ExitStack
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from config import paths
from config.atomic_io import write_atomic
from config.v2_config import V2Config, config_file_lock
from tests.test_desktop_claim_pipe import Parent
from tests.test_instance_address import ControlPlane
from tests.test_pairing_recovery import _response, pairing_host as pairing_host
from tests.test_personal_claims import Peer
from vibe import connection_runtime, desktop_claim_pipe, desktop_local_instance, instance_address
from vibe import personal_claims, remote_access, service_claim_binding, ui_server

HOST = 'fixture-app.avibe.bot'


def _lost_reply_child():
    """Real helper and pairing journal; no real network, connector, host name or account."""
    from vibe import cli

    peer = Peer()
    peer.hostname = HOST
    remote_access._resolve_pairing_backend_addresses = lambda *_: (ipaddress.ip_address('93.184.216.34'),)
    remote_access._validated_backend_connection = peer.connection
    remote_access._validated_backend_proxy_url = lambda _: None
    remote_access._json_request = lambda *a, **kw: dict(_response(), public_url='https://' + HOST,
                                                       redirect_uri='https://' + HOST + '/auth/callback')
    personal_claims.socket.gethostname = lambda: 'fixture-computer'
    personal_claims._Flow._wait = lambda *a, **kw: None
    remote_access.status = lambda *a, **kw: {'ok': True, 'paired': True, 'running': False}
    remote_access._report_runtime_status_async = lambda *a, **kw: None

    def failed_connector(_cfg):
        path = remote_access._connector_requested_path()
        path.parent.mkdir(exist_ok=True, parents=True)
        path.touch()  # explicit publisher request, not the service observer
        return {'ok': False, 'running': False, 'error': 'fixture-exited'}

    remote_access.start = failed_connector
    original = desktop_claim_pipe._result

    def lose_final(fd, view):
        if view.get('state') == 'bound':
            raise BrokenPipeError
        original(fd, view)

    desktop_claim_pipe._result = lose_final
    cli.main()


def _publish_in_child(root):
    home = root / 'fixture-home'
    home.mkdir()
    env = {k: os.environ[k] for k in ('SYSTEMROOT', 'WINDIR') if k in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), AVIBE_HOME=str(root), PYTHONNOUSERSITE='1',
               PYTHONDONTWRITEBYTECODE='1', PATH=os.defpath)
    env.update({f'XDG_{kind}_HOME': str(home / kind.lower()) for kind in ('CONFIG', 'CACHE', 'DATA', 'STATE')})
    with subprocess.Popen(
        [sys.executable, '-c', 'from tests.test_service_claim_binding import _lost_reply_child; _lost_reply_child()',
         '__desktop-claim-pipe'], cwd=Path(__file__).resolve().parents[1], env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as child:
        parent = Parent(os.dup(child.stdin.fileno()), os.dup(child.stdout.fileno()))
        child.stdin.close()
        try:
            ticket = parent.ticket()
            parent.send(op='arm', claim_id=ticket['claim_id'], kind='new', hostname=HOST, existing_instance_id=None)
            assert parent.receive() is None  # no publication frame was delivered
            assert child.wait(timeout=10) != 0
        finally:
            parent.input.close()
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        assert child.stderr.read() == b''


def _periodic_tick(monkeypatch):
    """Drive the actual recovery worker entry, stopping at the wait between ticks."""
    entries = []

    class Worker:
        def __init__(self, *, target, name, daemon):
            assert name == 'connection-settings-recovery' and daemon
            self.entry = target

        def start(self):
            entries.append(self.entry)

        def is_alive(self):
            return True

    class TickDone(Exception):
        pass

    def wait(_delay):
        raise TickDone

    monkeypatch.setattr(connection_runtime, 'threading', SimpleNamespace(Thread=Worker))
    monkeypatch.setattr(connection_runtime, '_worker', None)
    monkeypatch.setattr(connection_runtime, '_wake', SimpleNamespace(clear=lambda: None, wait=wait))
    connection_runtime.start()
    entry, = entries

    def tick():
        with pytest.raises(TickDone):
            entry()
    return tick


@pytest.mark.parametrize('off', [True, False])
def test_service_tick_adopts_after_helper_exits_without_final_pipe_reply(pairing_host, monkeypatch, off):
    root, pair_calls = pairing_host
    tick = _periodic_tick(monkeypatch)
    plane = ControlPlane('inst_A', HOST)
    monkeypatch.setattr(remote_access, '_device_json_request', plane)
    monkeypatch.setattr(ui_server, '_UI_RUNTIME_ACTIVE', True)
    monkeypatch.setattr(instance_address, '_worker_started', True)
    for name in ('_worker_wake', '_worker_read_requested'):
        monkeypatch.setattr(instance_address, name, threading.Event())
    monkeypatch.setattr(instance_address, '_hooks', {})
    monitors = []

    def monitor(config):
        assert config is None  # a worker must use current local state, never a publisher's config
        monitors.append(True)
        return len(monitors) > 1  # a rejected admission is still owed next tick

    automatic_starts = []

    def start_connector(config, *, record_request, install_failure):
        assert config is None and install_failure is None
        automatic_starts.append(record_request)
        return {'ok': True}

    monkeypatch.setattr(remote_access, '_ensure_cloudflared_pin', lambda _: None)
    monkeypatch.setattr(remote_access, '_start', start_connector)
    monkeypatch.setattr(ui_server, '_start_remote_monitors', monitor)
    monkeypatch.setattr(ui_server, '_wake_address_streams', lambda: None)
    for name in ('start', 'reconcile', 'pair', 'pair_personal_claim'):
        monkeypatch.setattr(remote_access, name, lambda *a, **kw: pytest.fail('observer attempted new pairing/start'))
    with service_claim_binding.serving(ui_server._on_claim_bound):
        old = service_claim_binding.current()
        tick()
        assert not monitors
        _publish_in_child(root)
        cfg = V2Config.load()
        assert cfg.remote_access.vibe_cloud.instance_id == 'inst_A' and cfg.remote_access.vibe_cloud.enabled
        assert not monitors and remote_access._connector_requested_path().is_file()
        if off:
            cfg.remote_access.vibe_cloud.enabled = False
            cfg.save()
        tick()
        tick()
        assert monitors == [True, True]
        assert instance_address._worker_read_requested.is_set() and instance_address._read_when_asked()
        assert plane.calls == ([] if off else [('POST', 'hostname-state', {})])
        assert V2Config.load().remote_access.vibe_cloud.enabled is not off
        schedule = remote_access._ConnectorRestartSchedule()
        assert not remote_access._restart_exited_connector(V2Config.load(), schedule, 1000.)
        restarted = remote_access._restart_exited_connector(V2Config.load(), schedule, 1000. + schedule.delay_seconds)
        assert restarted is not off and automatic_starts == ([] if off else [False])
        cfg.language = 'zh'
        cfg.save()
        tick()
        assert not old() and monitors == [True, True] and not instance_address._worker_read_requested.is_set()
        if off:
            cfg.remote_access.vibe_cloud.enabled = True  # explicit operator change, not an observer request
            cfg.save()
            tick()
            assert len(monitors) == 3 and instance_address._read_when_asked()
            assert plane.calls == [('POST', 'hostname-state', {})]
    with service_claim_binding.serving(lambda: pytest.fail('retired callback reached a successor')):
        assert not old()
    tick()
    assert not pair_calls and len(monitors) == (3 if off else 2)


def _write_local_commit():
    """Protected fixture files only; no SQLite connections or worker handles while moving the root."""
    with config_file_lock():
        desktop_local_instance.ensure_installation_id()
        cfg = V2Config.load()
        cloud = cfg.remote_access.vibe_cloud
        for key, value in _response().items():
            setattr(cloud, key, value)
        cloud.enabled, cloud.session_secret = True, 'fixture-session'
        cfg.save()
        personal_claims.record_bound('00000000-0000-4000-8000-000000000001', remote_access._pairing_identity_from_cloud(cloud))


@pytest.mark.parametrize('damage', ['pending', 'no_identity', 'partial', 'changed_key', 'only_attempt', 'bad_config',
                                    'different_root', 'replaced_root'])
def test_tick_requires_this_roots_committed_generation(pairing_host, monkeypatch, tmp_path_factory, damage):
    root, _ = pairing_host
    events = []
    _write_local_commit()
    with service_claim_binding.serving(lambda: events.append(True) or True):
        if damage == 'pending':
            write_atomic(remote_access._pending_pairing_path(), 'malformed fixture')
        elif damage == 'no_identity':
            desktop_local_instance._identity_path().unlink()
        elif damage in ('partial', 'changed_key'):
            cfg = V2Config.load()
            cfg.remote_access.vibe_cloud.instance_secret = '' if damage == 'partial' else 'subsequent-key'
            cfg.save()
        elif damage == 'only_attempt':
            write_atomic(personal_claims._marker(), json.dumps({'claim_id': 'fixture-attempt'}))
        elif damage == 'bad_config':
            write_atomic(paths.get_config_path(), 'not JSON')
        else:
            # Same bytes in the other installation; rejecting an incomplete copy would prove nothing.
            if damage == 'replaced_root':
                moved = root.with_name(root.name + '-original')
                root.rename(moved)
                shutil.copytree(moved, root)
            else:
                other = tmp_path_factory.mktemp('different-root')
                shutil.copytree(root, other, dirs_exist_ok=True)
                monkeypatch.setenv('AVIBE_HOME', str(other))
        assert not service_claim_binding.current()() and events == []


@pytest.mark.parametrize('change', ['new_binding', 'retired_service'])
def test_old_observation_does_not_dispatch_for_a_successor(pairing_host, monkeypatch, change):
    _write_local_commit()
    activated = []
    read = service_claim_binding.service_generation
    with ExitStack() as lifetime:
        lifetime.enter_context(service_claim_binding.serving(lambda: activated.append('first') or True))
        old = service_claim_binding.current()

        def crossing(*args):
            generation = read(*args)
            monkeypatch.setattr(service_claim_binding, 'service_generation', read)
            if change == 'retired_service':
                lifetime.close()
                lifetime.enter_context(service_claim_binding.serving(lambda: activated.append('next') or True))
            else:
                with config_file_lock():
                    cfg = V2Config.load()
                    cloud = cfg.remote_access.vibe_cloud
                    cloud.instance_secret = 'fixture-new-key-same-id'
                    cfg.save()
                    personal_claims.record_bound('00000000-0000-4000-8000-000000000002',
                                                 remote_access._pairing_identity_from_cloud(cloud))
            return generation

        monkeypatch.setattr(service_claim_binding, 'service_generation', crossing)
        assert not old() and not activated
        current = service_claim_binding.current()
        assert current() and not current()
        assert activated == ['next' if change == 'retired_service' else 'first']
