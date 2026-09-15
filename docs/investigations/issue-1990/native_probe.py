"""Own-child Darwin probe; simulate boot read changes only inside this process."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
import psutil
import psutil._psosx as osx

class BsdInfo(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid',
        'ruid', 'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(n, ctypes.c_uint32) for n in ('nfiles', 'pgid', 'jobc', 'tdev', 'tpgid')]
    _fields_ += [('nice', ctypes.c_int32), ('sec', ctypes.c_uint64), ('usec', ctypes.c_uint64)]

lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
lib.proc_pidinfo.restype = ctypes.c_int

def raw(pid):
    info = BsdInfo()
    ctypes.set_errno(0)
    count = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if count != ctypes.sizeof(info):
        return {'bytes': count, 'errno': ctypes.get_errno()}
    assert info.pid == pid
    return {'sec': info.sec, 'usec': info.usec, 'bytes': count}

with tempfile.TemporaryDirectory(prefix='avibe-1990-native-') as directory:
    root = Path(directory)
    env = {'HOME': str(root), 'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}
    after = 'import sys; print("after", flush=True); sys.stdin.readline()'
    before = 'import os,sys; print("before", flush=True); sys.stdin.readline(); os.execv(sys.executable,[sys.executable,"-B","-c",sys.argv[1]])'
    child = subprocess.Popen([sys.executable, '-B', '-c', before, after], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=root)
    try:
        assert child.stdout.readline().strip() == 'before'
        raw_initial = raw(child.pid)
        captured = psutil.Process(child.pid)
        samples = []
        initial_boot = osx.INIT_BOOT_TIME
        with patch.object(osx, 'INIT_BOOT_TIME', initial_boot):
            for delta in (0, 1, 0, -1, 0):
                with patch.object(osx, 'boot_time', lambda delta=delta: initial_boot + delta):
                    current = psutil.Process(child.pid)
                    samples.append({'injected_boot_delta': delta,
                        'public_time': current.create_time(),
                        'raw_time': current._proc.create_time(monotonic=True),
                        'public_identity_equal': captured == current,
                        'native_raw': raw(child.pid)})
        assert samples[1]['public_time'] - samples[0]['public_time'] == 1
        assert samples[3]['public_time'] - samples[0]['public_time'] == 1
        assert all(s['native_raw'] == raw_initial for s in samples)
        assert len({s['raw_time'] for s in samples}) == 1
        assert all(s['public_identity_equal'] for s in samples)
        child.stdin.write('\n'); child.stdin.flush()
        assert child.stdout.readline().strip() == 'after'
        raw_after_exec = raw(child.pid)
        assert raw_after_exec == raw_initial
        child.stdin.write('\n'); child.stdin.flush()
        child.wait(timeout=5)
        raw_after_exit = raw(child.pid)
        print(json.dumps({'host': os.uname().sysname, 'arch': os.uname().machine,
            'psutil': psutil.__version__, 'abi_bytes': ctypes.sizeof(BsdInfo),
            'samples': samples, 'raw_unchanged_across_exec': raw_after_exec == raw_initial,
            'after_exit': raw_after_exit, 'child_exit_code': child.returncode}, indent=2))
    finally:
        if child.poll() is None:
            child.stdin.close()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=5)
