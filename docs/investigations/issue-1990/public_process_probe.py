"""Verify public psutil child operations under probe-local clock correction."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from unittest.mock import patch

import psutil
import psutil._psosx as osx

with tempfile.TemporaryDirectory(prefix='avibe-1990-public-') as directory:
    child = subprocess.Popen(
        [sys.executable, '-B', '-c',
         'import sys; print("ready", flush=True); sys.stdin.readline()'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        env={'HOME': directory, 'PATH': '/usr/bin:/bin'}, cwd=directory,
    )
    try:
        assert child.stdout.readline().strip() == 'ready'
        initial_boot = osx.INIT_BOOT_TIME
        with patch.object(osx, 'boot_time', lambda: initial_boot):
            owned = psutil.Process(child.pid)
            original_display_time = owned.create_time()
        with patch.object(osx, 'boot_time', lambda: initial_boot + 1):
            fresh = psutil.Process(child.pid)
            shifted = fresh.create_time() - original_display_time
            same = owned == fresh
            alive = owned.is_running()
            assert shifted == 1 and same and alive
            owned.terminate()
            exit_code = child.wait(timeout=5)
        assert exit_code == -signal.SIGTERM
        assert not owned.is_running()
        print(json.dumps({
            'host': os.uname().sysname, 'arch': os.uname().machine,
            'psutil': psutil.__version__, 'display_time_shift_seconds': shifted,
            'public_identity_equal': same, 'alive_before_stop': alive,
            'public_terminate_succeeded': True, 'exit_code': exit_code,
            'alive_after_reap': owned.is_running(),
        }, indent=2))
    finally:
        if child.poll() is None:
            child.stdin.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
