"""Replay reported identity discrepancy through production lifecycle methods.
All process, health and storage boundaries are fake; no OS processes are signaled.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]

async def replay(root):
    import avibe_memory.process as proc
    from avibe_memory.runtime import MemoryRuntime
    from avibe_memory.supervisor import EverOSSupervisor

    class ObservedProcess:
        pid = 999999999  # Synthetic; never passed to an OS primitive.
        returncode = None
        stamp = 1789430408.890855
        def create_time(self): return self.stamp
        def status(self): return 'running' if self.returncode is None else 'zombie'
        def send_signal(self, signum):
            signals.append(int(signum))
            self.returncode = 0

    child = ObservedProcess()
    signals = []
    class Host:
        def live(self, ids): return proc._live_owned_processes(ids)
        def snapshot_tree(self, pid, group): return {pid: child.stamp}
        def has_tcp_listener(self, ids): return False
        def signal(self, ids, signum, *, process_group=None, process=None):
            # Use the production per-PID authority checks; avoid killpg entirely.
            proc._signal_owned_processes(ids, signum)
        async def wait_for_exit(self, ids, timeout, **kwargs):
            await asyncio.sleep(0)
            return child.returncode is not None and not self.live(ids)

    class Module:
        paused = False
        @asynccontextmanager
        async def lifecycle(self): yield
        def pause_claims(self): self.paused = True
        async def quiesce_claims(self, **kwargs): return True
    module = Module()
    async def checkpoint(operation): return await operation
    async def close_writer(): pass
    runtime = SimpleNamespace(
        _require_lifecycle_work=lambda: None, needs_repair=False,
        available=True, _store=object(), _module=module, module=module,
        _artifact_installing=False, _wake_config=SimpleNamespace(enabled=True),
        _artifact_manager=SimpleNamespace(resolve_python=lambda: Path(sys.executable)),
        _lifecycle_checkpoint=checkpoint, _close_writer=close_writer,
        _runtime_error=None, _closing=False,
    )
    outcomes = []
    async def recover():
        result = await MemoryRuntime._wake_locked(runtime)
        outcomes.append(result)
        return result['ok']
    supervisor = EverOSSupervisor(
        provider_root=root / 'memory/root', effective_home=root,
        socket_path=root / 'memory/sidecar.sock', on_ready=lambda: None,
        on_unavailable=lambda: MemoryRuntime._current_sidecar_unavailable(runtime),
        on_recover=recover, restart_delays=(0, 0, 0),
    )
    runtime._supervisor = supervisor
    adapter = proc.EverOSProcess(
        sys.executable, effective_home=root,
        stop_timeout_seconds=0.01, _host=Host(),
        on_unexpected_exit=lambda: supervisor._handle_unexpected_exit(adapter, 0),
    )
    adapter._process = child
    adapter._owned_processes = {child.pid: 1789430407.890855}
    adapter._desired_running = True
    supervisor._child = adapter
    supervisor._python = Path(sys.executable)
    supervisor._settings = proc.EverOSProcessSettings()
    with patch.object(proc.psutil, 'Process', lambda pid: child), \
         patch.object(proc, '_uses_linux_starttime_stamp', lambda: False):
        await adapter._monitor_child(child)
        for _ in range(400):
            if len(outcomes) == 3 and supervisor._restart_task is None: break
            await asyncio.sleep(0.001)
        assert len(outcomes) == 3, outcomes
        assert all(r.get('error') == 'memory_wake_failed' for r in outcomes)
        assert module.paused and signals == [] and child.returncode is None
        child.stamp = 1789430407.890855
        assert proc._host_identity_is_live(adapter._host, child.pid, adapter._owned_processes)
        await asyncio.sleep(0.02)
        assert len(outcomes) == 3 and module.paused and supervisor._restart_task is None
        result = {
            'automatic_wake_attempts': len(outcomes), 'error': runtime._runtime_error,
            'claims_paused_after_identity_returns': module.paused,
            'live_child': child.returncode is None, 'signals_during_mismatch': signals.copy(),
            'recovery_task_after_identity_returns': supervisor._restart_task is not None,
            'health_and_read_boundary': 'assumed responsive; not exercised by this replay',
        }
        # Verify stop becomes feasible after identity returns. Cleanup is test-only.
        adapter._ownership = SimpleNamespace(retire_if_group_is_clear=lambda *args: None)
        await adapter.stop()
        result['explicit_stop_after_identity_returns'] = child.returncode == 0
        await supervisor.close()
        print(json.dumps(result, indent=2))
        if '--expect-recovery' in sys.argv:
            assert not module.paused, 'BUG: identity returned, but processing remains paused'

with tempfile.TemporaryDirectory(prefix='avibe-1990-hermetic-') as directory:
    root = Path(directory)
    os.environ.clear()
    os.environ.update({
        'HOME': str(root), 'AVIBE_HOME': str(root / 'avibe'),
        'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_DATA_HOME': str(root / 'data'),
        'XDG_CACHE_HOME': str(root / 'cache'), 'XDG_STATE_HOME': str(root / 'state'),
        'CODEX_HOME': str(root / 'codex'), 'CLAUDE_CONFIG_DIR': str(root / 'claude'),
        'PATH': '/usr/bin:/bin',
    })
    sys.path.insert(0, str(REPO))
    logging.disable(logging.CRITICAL)
    asyncio.run(replay(root))
