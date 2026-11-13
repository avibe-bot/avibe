"""Deterministic ownership checks for children that outlive a forced service stop."""
from __future__ import annotations

import sys

from core import show_runtime


def test_show_runtime_orphan_sweep_terminates_workspace_bound_server(monkeypatch, tmp_path):
    terminated: list[int] = []

    class FakeProcess:
        def __init__(self, pid: int, cmdline: list[str]):
            self.info = {"pid": pid, "cmdline": cmdline}

        def children(self, recursive: bool):
            assert recursive is True
            return [FakeChild(456)]

        def terminate(self):
            terminated.append(self.info["pid"])

        def kill(self):
            terminated.append(self.info["pid"])

    class FakeChild:
        def __init__(self, pid: int):
            self.pid = pid

        def terminate(self):
            terminated.append(self.pid)

        def kill(self):
            terminated.append(self.pid)

    class FakePsutil:
        NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        AccessDenied = type("AccessDenied", (Exception,), {})

        @staticmethod
        def process_iter(attrs):
            assert attrs == ["pid", "cmdline"]
            return iter([
                FakeProcess(
                    123,
                    [
                        "/usr/bin/node",
                        "--workspace-root",
                        str(tmp_path),
                        "--fallback-delay-seconds",
                        "1",
                    ],
                )
            ])

        @staticmethod
        def wait_procs(victims, timeout):
            assert timeout == 3
            return [], []

    monkeypatch.setitem(sys.modules, "psutil", FakePsutil)

    assert show_runtime.sweep_orphan_show_runtime_servers(tmp_path) == [123]
    assert terminated == [456, 123]
