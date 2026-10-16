import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from modules.agents import claude_process_reaper


def test_claude_process_state_helpers_report_signal_and_stderr_tail():
    client = SimpleNamespace(
        _transport=SimpleNamespace(_process=SimpleNamespace(returncode=-signal.SIGABRT)),
        _vibe_stderr_lines=["first", "", "fatal Claude error"],
    )

    assert claude_process_reaper.get_claude_client_returncode(client) == -6
    assert claude_process_reaper.claude_process_exit_reason(-6) == "SIGABRT (signal 6)"
    assert claude_process_reaper.claude_process_exit_reason_i18n(-6) == (
        "error.claudeProcessSignal",
        {"signal": "SIGABRT", "number": 6},
    )
    assert claude_process_reaper.get_claude_client_stderr_tail(client) == "first\nfatal Claude error"


def test_claude_process_state_helpers_ignore_running_process():
    client = SimpleNamespace(
        _transport=SimpleNamespace(_process=SimpleNamespace(returncode=None)),
    )

    assert claude_process_reaper.get_claude_client_returncode(client) is None


def test_find_claude_resume_processes_matches_exact_resume_id(monkeypatch):
    table = "\n".join(
        [
            "100 1 /usr/local/bin/claude --resume sess-1 --model opus",
            "101 1 /usr/local/bin/claude --resume sess-10 --model opus",
            "102 1 /usr/local/bin/claude --resume=sess-1 --model opus",
            "103 1 /usr/local/bin/codex --resume sess-1",
            "104 1 /usr/local/bin/not-claude --resume sess-1",
            "105 1 /usr/local/bin/node /usr/local/bin/claude --resume sess-1",
        ]
    )
    monkeypatch.setattr(claude_process_reaper, "_process_table", lambda: table)

    rows = claude_process_reaper.find_claude_resume_processes("sess-1")

    assert [row.pid for row in rows] == [100, 102, 105]


def test_find_claude_resume_processes_matches_configured_wrapper(monkeypatch):
    table = "\n".join(
        [
            "100 1 /usr/local/bin/claude-proxy --resume sess-1 --model opus",
            "101 1 /usr/local/bin/other-wrapper --resume sess-1 --model opus",
            "102 1 /usr/local/bin/node /usr/local/bin/claude-proxy --resume=sess-1",
        ]
    )
    monkeypatch.setattr(claude_process_reaper, "_process_table", lambda: table)

    rows = claude_process_reaper.find_claude_resume_processes(
        "sess-1",
        cli_path="/usr/local/bin/claude-proxy",
    )

    assert [row.pid for row in rows] == [100, 102]


def test_reap_duplicate_claude_resume_processes_kills_matches_and_descendants(monkeypatch):
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"100 {service_pid} /usr/local/bin/node /usr/local/bin/claude --resume sess-1 --model opus",
            f"101 {service_pid} /usr/local/bin/claude --resume sess-1 --model opus",
            "102 101 node helper.js",
            "200 1 /usr/local/bin/claude --resume sess-2 --model opus",
        ]
    )
    signals = []
    alive = {100, 101, 102, 200}

    def fake_kill(pid, sig):
        if sig == 0:
            if pid not in alive:
                raise ProcessLookupError
            return
        signals.append((pid, sig))
        alive.discard(pid)

    monkeypatch.setattr(claude_process_reaper, "_process_table", lambda: table)
    monkeypatch.setattr(claude_process_reaper.os, "kill", fake_kill)

    reaped = asyncio.run(
        claude_process_reaper.reap_duplicate_claude_resume_processes(
            "sess-1",
            keep_pid=100,
            logger=logging.getLogger("test.claude_reaper"),
        )
    )

    assert reaped == 2
    assert (101, signal.SIGTERM) in signals
    assert (102, signal.SIGTERM) in signals
    assert all(pid not in (100, 200) for pid, _ in signals)


def test_reap_duplicate_claude_resume_processes_keeps_single_tracked_pid(monkeypatch):
    service_pid = os.getpid()
    monkeypatch.setattr(
        claude_process_reaper,
        "_process_table",
        lambda: "\n".join(
            [
                f"{service_pid} 1 python service_main.py",
                f"100 {service_pid} /usr/local/bin/claude --resume sess-1 --model opus",
            ]
        ),
    )
    signals = []
    monkeypatch.setattr(claude_process_reaper.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    reaped = asyncio.run(
        claude_process_reaper.reap_duplicate_claude_resume_processes(
            "sess-1",
            keep_pid=100,
            logger=logging.getLogger("test.claude_reaper"),
        )
    )

    assert reaped == 0
    assert signals == []


def test_reap_duplicate_claude_resume_processes_reaps_scoped_orphan_without_keep_pid(monkeypatch):
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"100 {service_pid} /usr/local/bin/claude --resume sess-1 --model opus",
            "300 1 /usr/local/bin/claude --resume sess-1 --model opus",
        ]
    )
    signals = []
    alive = {100, 300}

    def fake_kill(pid, sig):
        if sig == 0:
            if pid not in alive:
                raise ProcessLookupError
            return
        signals.append((pid, sig))
        alive.discard(pid)

    monkeypatch.setattr(claude_process_reaper, "_process_table", lambda: table)
    monkeypatch.setattr(claude_process_reaper.os, "kill", fake_kill)

    reaped = asyncio.run(
        claude_process_reaper.reap_duplicate_claude_resume_processes(
            "sess-1",
            keep_pid=None,
            logger=logging.getLogger("test.claude_reaper"),
        )
    )

    assert reaped == 1
    assert (100, signal.SIGTERM) in signals
    assert all(pid != 300 for pid, _ in signals)


def test_reap_duplicate_claude_resume_processes_spares_owned_replacement(monkeypatch):
    """A live client resuming the same native id must survive keep_pid=None.

    Regression for the idle-eviction race: force cleanup passes keep_pid=None,
    so without exclude_pids the reaper kills the healthy replacement client
    (and its helpers) that took over the session during the slow disconnect.
    """
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"100 {service_pid} /usr/local/bin/claude --resume sess-1 --model opus",
            f"400 {service_pid} /usr/local/bin/claude --resume sess-1 --model opus",
            "401 400 node helper.js",
        ]
    )
    signals = []
    alive = {100, 400, 401}

    def fake_kill(pid, sig):
        if sig == 0:
            if pid not in alive:
                raise ProcessLookupError
            return
        signals.append((pid, sig))
        alive.discard(pid)

    monkeypatch.setattr(claude_process_reaper, "_process_table", lambda: table)
    monkeypatch.setattr(claude_process_reaper.os, "kill", fake_kill)

    reaped = asyncio.run(
        claude_process_reaper.reap_duplicate_claude_resume_processes(
            "sess-1",
            keep_pid=None,
            exclude_pids={400},
            logger=logging.getLogger("test.claude_reaper"),
        )
    )

    assert reaped == 1
    assert (100, signal.SIGTERM) in signals
    assert all(pid not in (400, 401) for pid, _ in signals)


def test_reap_duplicate_claude_resume_processes_skips_when_all_matches_owned(monkeypatch):
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"400 {service_pid} /usr/local/bin/claude --resume sess-1 --model opus",
        ]
    )
    signals = []
    monkeypatch.setattr(claude_process_reaper, "_process_table", lambda: table)
    monkeypatch.setattr(claude_process_reaper.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    reaped = asyncio.run(
        claude_process_reaper.reap_duplicate_claude_resume_processes(
            "sess-1",
            keep_pid=None,
            exclude_pids={400},
            logger=logging.getLogger("test.claude_reaper"),
        )
    )

    assert reaped == 0
    assert signals == []


def test_reap_duplicate_claude_resume_processes_counts_owned_matches_in_the_guard(monkeypatch):
    """A departed keep_pid must not hide an orphan behind an owned replacement.

    Receiver-side cleanup keeps a non-null keep_pid, but that process can be
    gone from the table by the time the scan runs. With an owned replacement
    and an orphan both resuming the session, filtering the replacement out
    before the duplicate guard leaves one scoped match and spares the orphan.
    """
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"100 {service_pid} /usr/local/bin/claude --resume sess-1 --model opus",
            f"400 {service_pid} /usr/local/bin/claude --resume sess-1 --model opus",
        ]
    )
    signals = []
    alive = {100, 400}

    def fake_kill(pid, sig):
        if sig == 0:
            if pid not in alive:
                raise ProcessLookupError
            return
        signals.append((pid, sig))
        alive.discard(pid)

    monkeypatch.setattr(claude_process_reaper, "_process_table", lambda: table)
    monkeypatch.setattr(claude_process_reaper.os, "kill", fake_kill)

    reaped = asyncio.run(
        claude_process_reaper.reap_duplicate_claude_resume_processes(
            "sess-1",
            keep_pid=999,
            exclude_pids={400},
            logger=logging.getLogger("test.claude_reaper"),
        )
    )

    assert reaped == 1
    assert (100, signal.SIGTERM) in signals
    assert all(pid != 400 for pid, _ in signals)


def test_reap_duplicate_claude_resume_processes_ignores_unrelated_unique_match(monkeypatch):
    service_pid = os.getpid()
    monkeypatch.setattr(
        claude_process_reaper,
        "_process_table",
        lambda: "\n".join(
            [
                f"{service_pid} 1 python service_main.py",
                "300 1 /usr/local/bin/claude --resume sess-1 --model opus",
            ]
        ),
    )
    signals = []
    monkeypatch.setattr(claude_process_reaper.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    reaped = asyncio.run(
        claude_process_reaper.reap_duplicate_claude_resume_processes(
            "sess-1",
            keep_pid=None,
            logger=logging.getLogger("test.claude_reaper"),
        )
    )

    assert reaped == 0
    assert signals == []


def _patch_orphan_env(monkeypatch, table: str, ages: dict[int, float], alive: set[int]):
    def fake_kill(pid, sig):
        if sig == 0:
            if pid not in alive:
                raise ProcessLookupError
            return
        alive.discard(pid)

    monkeypatch.setattr(claude_process_reaper, "_process_table", lambda: table)
    monkeypatch.setattr(claude_process_reaper, "_process_ages", lambda pids: ages)
    signals: list[tuple[int, int]] = []
    real_kill = fake_kill

    def recording_kill(pid, sig):
        if sig != 0:
            signals.append((pid, sig))
        return real_kill(pid, sig)

    monkeypatch.setattr(claude_process_reaper.os, "kill", recording_kill)
    return signals


def test_reap_orphaned_reaps_in_tree_no_owner_and_descendants(monkeypatch):
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"100 {service_pid} /usr/local/bin/claude --resume sess-1 --model opus",
            f"101 {service_pid} /usr/local/bin/claude --output-format stream-json --verbose --resume sess-2 --input-format stream-json",
            "102 101 node helper.js",
        ]
    )
    signals = _patch_orphan_env(monkeypatch, table, {101: 999.0, 102: 999.0}, {100, 101, 102})
    monkeypatch.setattr("vibe.runtime.pid_alive", lambda pid: True)
    monkeypatch.setattr("vibe.runtime.process_start_seconds", lambda pid: 123.0)

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids={100},
            tracked_resume_ids={"sess-1": 100},
            logger=logging.getLogger("test.claude_orphan"),
            owned_processes=[
                claude_process_reaper.ClaudeOwnedProcess(
                    pid=101,
                    native_session_id="sess-2",
                    started_at=123.0,
                )
            ],
        )
    )

    assert reaped == 2
    killed = {pid for pid, _ in signals}
    assert 101 in killed and 102 in killed
    assert 100 not in killed
    assert service_pid not in killed


def test_reap_orphaned_ignores_managed_watch_descendants(monkeypatch):
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"500 {service_pid} /bin/sh -c claude -p --input-format stream-json watch-work",
            "501 500 /usr/local/bin/claude -p --input-format stream-json watch-work",
        ]
    )
    signals = _patch_orphan_env(monkeypatch, table, {501: 999.0}, {500, 501})

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids=set(),
            tracked_resume_ids={},
            logger=logging.getLogger("test.claude_orphan"),
            exclude_pids={500},
        )
    )

    assert reaped == 0
    assert signals == []


def test_reap_orphaned_keeps_owned_in_tree_process(monkeypatch):
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"100 {service_pid} /usr/local/bin/claude --resume sess-1 --model opus",
        ]
    )
    signals = _patch_orphan_env(monkeypatch, table, {100: 999.0}, {100})

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids={100},
            tracked_resume_ids={"sess-1": 100},
            logger=logging.getLogger("test.claude_orphan"),
        )
    )

    assert reaped == 0
    assert signals == []


def test_reap_orphaned_ignores_cross_restart_init_parented_match(monkeypatch):
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"100 {service_pid} /usr/local/bin/claude --resume sess-1 --model opus",
            "300 1 /usr/local/bin/claude --output-format stream-json --verbose --resume sess-1 --input-format stream-json",
        ]
    )
    signals = _patch_orphan_env(monkeypatch, table, {300: 999.0}, {100, 300})

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids={100},
            tracked_resume_ids={"sess-1": 100},
            logger=logging.getLogger("test.claude_orphan"),
        )
    )

    assert reaped == 0
    assert signals == []


def test_reap_orphaned_respects_min_age_grace_window(monkeypatch):
    """A freshly spawned, not-yet-tracked process must not be reaped (TOCTOU)."""
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"101 {service_pid} /usr/local/bin/claude --output-format stream-json --verbose --resume sess-2 --input-format stream-json",
        ]
    )
    signals = _patch_orphan_env(monkeypatch, table, {101: 5.0}, {101})
    monkeypatch.setattr("vibe.runtime.pid_alive", lambda pid: True)
    monkeypatch.setattr("vibe.runtime.process_start_seconds", lambda pid: 123.0)

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids=set(),
            tracked_resume_ids={},
            logger=logging.getLogger("test.claude_orphan"),
            owned_processes=[claude_process_reaper.ClaudeOwnedProcess(pid=101, started_at=123.0)],
        )
    )

    assert reaped == 0
    assert signals == []


def test_reap_orphaned_skips_when_age_unknown(monkeypatch):
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"101 {service_pid} /usr/local/bin/claude --output-format stream-json --verbose --resume sess-2 --input-format stream-json",
        ]
    )
    signals = _patch_orphan_env(monkeypatch, table, {}, {101})
    monkeypatch.setattr("vibe.runtime.pid_alive", lambda pid: True)
    monkeypatch.setattr("vibe.runtime.process_start_seconds", lambda pid: 123.0)

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids=set(),
            tracked_resume_ids={},
            logger=logging.getLogger("test.claude_orphan"),
            owned_processes=[claude_process_reaper.ClaudeOwnedProcess(pid=101, started_at=123.0)],
        )
    )

    assert reaped == 0
    assert signals == []


def test_reap_orphaned_ignores_unrelated_out_of_tree_claude(monkeypatch):
    """Out-of-tree claude not matching any tracked --resume id is left alone."""
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            "300 1 /usr/local/bin/claude --resume someone-elses-session",
        ]
    )
    signals = _patch_orphan_env(monkeypatch, table, {300: 999.0}, {300})

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids=set(),
            tracked_resume_ids={"sess-1": 100},
            logger=logging.getLogger("test.claude_orphan"),
        )
    )

    assert reaped == 0
    assert signals == []


def test_reap_orphaned_ignores_in_tree_non_sdk_claude(monkeypatch):
    """An in-tree `claude` that is NOT an SDK session subprocess (no
    --input-format stream-json) — e.g. launched by another backend or a watch
    command — must not be reaped even though it is unowned and old."""
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"401 {service_pid} /usr/local/bin/claude -p run-a-build",
        ]
    )
    signals = _patch_orphan_env(monkeypatch, table, {401: 999.0}, {401})

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids=set(),
            tracked_resume_ids={},
            logger=logging.getLogger("test.claude_orphan"),
        )
    )

    assert reaped == 0
    assert signals == []


def test_reap_orphaned_skips_in_tree_sweep_when_disabled(monkeypatch):
    """reap_in_tree=False (incomplete owner set) must not touch in-tree pids."""
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"101 {service_pid} /usr/local/bin/claude --output-format stream-json --verbose --resume sess-2 --input-format stream-json",
        ]
    )
    signals = _patch_orphan_env(monkeypatch, table, {101: 999.0}, {101})
    monkeypatch.setattr("vibe.runtime.pid_alive", lambda pid: True)
    monkeypatch.setattr("vibe.runtime.process_start_seconds", lambda pid: 123.0)

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids=set(),
            tracked_resume_ids={},
            logger=logging.getLogger("test.claude_orphan"),
            reap_in_tree=False,
            owned_processes=[claude_process_reaper.ClaudeOwnedProcess(pid=101, started_at=123.0)],
        )
    )

    assert reaped == 0
    assert signals == []


def test_reap_orphaned_ignores_out_of_tree_resume_match_not_init_parented(monkeypatch):
    """A user's manual `claude --resume <id>` (parented to a shell, not init)
    must not be reaped even though the resume id matches a tracked session."""
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"100 {service_pid} /usr/local/bin/claude --resume sess-1 --model opus",
            "300 999 /usr/local/bin/claude --resume sess-1 --model opus",
        ]
    )
    signals = _patch_orphan_env(monkeypatch, table, {300: 999.0}, {100, 300})

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids={100},
            tracked_resume_ids={"sess-1": 100},
            logger=logging.getLogger("test.claude_orphan"),
        )
    )

    assert reaped == 0
    assert signals == []


def test_reap_orphaned_ignores_init_parented_resume_match_without_sdk_marker(monkeypatch):
    """A user's manual `claude --resume <id>` may become init-reparented after
    its shell exits, but it is not an Avibe SDK session subprocess unless it
    carries the stream-json input marker."""
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"100 {service_pid} /usr/local/bin/claude --resume sess-1 --model opus",
            "300 1 /usr/local/bin/claude --resume sess-1 --model opus",
        ]
    )
    signals = _patch_orphan_env(monkeypatch, table, {300: 999.0}, {100, 300})

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids={100},
            tracked_resume_ids={"sess-1": 100},
            logger=logging.getLogger("test.claude_orphan"),
        )
    )

    assert reaped == 0
    assert signals == []


def test_reap_orphaned_ignores_external_init_parented_stream_json_resume_client(monkeypatch):
    """External programmatic Claude clients use public stream-json/resume flags too."""
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"100 {service_pid} /usr/local/bin/claude --resume sess-1 --model opus",
            "300 1 /usr/local/bin/claude --output-format stream-json --verbose --resume sess-1 --input-format stream-json",
        ]
    )
    signals = _patch_orphan_env(monkeypatch, table, {300: 999.0}, {100, 300})

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids={100},
            tracked_resume_ids={"sess-1": 100},
            logger=logging.getLogger("test.claude_orphan"),
        )
    )

    assert reaped == 0
    assert signals == []


def test_reap_orphaned_reaps_registry_owned_out_of_tree_resume_match(monkeypatch):
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"100 {service_pid} /usr/local/bin/claude --resume sess-1 --model opus",
            "300 1 /usr/local/bin/claude --output-format stream-json --verbose --resume sess-1 --input-format stream-json",
        ]
    )
    signals = _patch_orphan_env(monkeypatch, table, {300: 999.0}, {100, 300})
    monkeypatch.setattr("vibe.runtime.pid_alive", lambda pid: True)
    monkeypatch.setattr("vibe.runtime.process_start_seconds", lambda pid: 123.0)

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids={100},
            tracked_resume_ids={"sess-1": 100},
            logger=logging.getLogger("test.claude_orphan"),
            owned_processes=[
                claude_process_reaper.ClaudeOwnedProcess(
                    pid=300,
                    native_session_id="sess-1",
                    started_at=123.0,
                )
            ],
        )
    )

    assert reaped == 1
    assert (300, signal.SIGTERM) in signals


def test_reap_orphaned_ignores_registered_out_of_tree_without_birth_identity(monkeypatch):
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"100 {service_pid} /usr/local/bin/claude --resume sess-1 --model opus",
            "300 1 /usr/local/bin/claude --output-format stream-json --verbose --resume sess-1 --input-format stream-json",
        ]
    )
    signals = _patch_orphan_env(monkeypatch, table, {300: 999.0}, {100, 300})

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids={100},
            tracked_resume_ids={"sess-1": 100},
            logger=logging.getLogger("test.claude_orphan"),
            owned_processes=[
                claude_process_reaper.ClaudeOwnedProcess(
                    pid=300,
                    native_session_id="sess-1",
                )
            ],
        )
    )

    assert reaped == 0
    assert signals == []


def test_reap_orphaned_ignores_other_backend_stream_json_descendant_without_registry(monkeypatch):
    service_pid = os.getpid()
    table = "\n".join(
        [
            f"{service_pid} 1 python service_main.py",
            f"700 {service_pid} /usr/local/bin/codex app-server",
            "701 700 /usr/local/bin/claude --output-format stream-json --verbose --resume sess-9 --input-format stream-json",
        ]
    )
    signals = _patch_orphan_env(monkeypatch, table, {701: 999.0}, {700, 701})

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids=set(),
            tracked_resume_ids={},
            logger=logging.getLogger("test.claude_orphan"),
            owned_processes=[],
        )
    )

    assert reaped == 0
    assert signals == []


def _client(pid: int) -> SimpleNamespace:
    return SimpleNamespace(_transport=SimpleNamespace(_process=SimpleNamespace(pid=pid)))


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_registering_a_process_reads_and_writes_on_the_registry_thread(tmp_path, monkeypatch):
    # The service registers on its event loop; the start-token read and the file rewrite must not run there.
    threads: list[str] = []

    def start_token(pid):
        threads.append(threading.current_thread().name)
        return "boot:1000"

    monkeypatch.setattr("vibe.runtime.process_start_token", start_token)
    child = _sleeper()
    try:
        pending = claude_process_reaper.register_claude_owned_process(
            _client(child.pid), native_session_id="sess-1", runtime_dir=tmp_path
        )
        pending.result(timeout=10)
    finally:
        child.kill()
        child.wait(10)

    assert threads and all(name.startswith("claude-process-registry") for name in threads)
    assert claude_process_reaper._load_owned_process_registry(tmp_path) == [
        claude_process_reaper.ClaudeOwnedProcess(pid=child.pid, native_session_id="sess-1", start_token="boot:1000")
    ]


def test_a_registration_drops_the_records_of_processes_that_have_exited(tmp_path, monkeypatch):
    monkeypatch.setattr("vibe.runtime.process_start_token", lambda pid: "boot:1000")
    exited = subprocess.Popen([sys.executable, "-c", "pass"])
    exited.wait(10)
    running, registering = _sleeper(), _sleeper()
    try:
        for child in (exited, running):
            claude_process_reaper.register_claude_owned_process(_client(child.pid), runtime_dir=tmp_path).result(timeout=10)

        claude_process_reaper.register_claude_owned_process(_client(registering.pid), runtime_dir=tmp_path).result(timeout=10)

        recorded = {record.pid for record in claude_process_reaper._load_owned_process_registry(tmp_path)}
    finally:
        for child in (running, registering):
            child.kill()
            child.wait(10)

    assert recorded == {running.pid, registering.pid}


def test_concurrent_registrations_keep_every_record(tmp_path, monkeypatch):
    monkeypatch.setattr("vibe.runtime.process_start_token", lambda pid: "boot:1000")
    children = [_sleeper() for _ in range(4)]
    try:
        with ThreadPoolExecutor(max_workers=4) as callers:
            pending = list(
                callers.map(
                    lambda child: claude_process_reaper.register_claude_owned_process(
                        _client(child.pid), runtime_dir=tmp_path
                    ),
                    children,
                )
            )
        for write in pending:
            write.result(timeout=10)

        recorded = {record.pid for record in claude_process_reaper._load_owned_process_registry(tmp_path)}
    finally:
        for child in children:
            child.kill()
            child.wait(10)

    assert recorded == {child.pid for child in children}


# Holds the registry's file lock the way another service process's registration does, until stdin closes.
_HOLD_REGISTRY_LOCK = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from storage.lock import MigrationFileLock
with MigrationFileLock(Path(sys.argv[2]), timeout_seconds=None):
    print("held", flush=True)
    sys.stdin.read()
"""


def test_a_registration_waits_while_another_process_rewrites_the_registry(tmp_path, monkeypatch):
    # The Web UI's process registers auth and probe clients into the same file.
    monkeypatch.setattr("vibe.runtime.process_start_token", lambda pid: "boot:1000")
    lock_path = tmp_path / f"{claude_process_reaper.CLAUDE_PROCESS_REGISTRY_NAME}.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_REGISTRY_LOCK, str(Path(__file__).resolve().parents[1]), str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    child = _sleeper()
    try:
        assert holder.stdout.readline().strip() == "held"
        pending = claude_process_reaper.register_claude_owned_process(_client(child.pid), runtime_dir=tmp_path)
        time.sleep(0.5)
        assert not pending.done()

        holder.stdin.close()
        holder.wait(10)
        pending.result(timeout=10)

        recorded = {record.pid for record in claude_process_reaper._load_owned_process_registry(tmp_path)}
    finally:
        for process in (holder, child):
            process.kill()
            process.wait(10)
        holder.stdout.close()

    assert recorded == {child.pid}


def test_a_recorded_pid_no_process_can_have_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr("vibe.runtime.process_start_token", lambda pid: "boot:1000")
    (tmp_path / claude_process_reaper.CLAUDE_PROCESS_REGISTRY_NAME).write_text(
        '{"processes":[{"pid":%d,"native_session_id":null,"owner":"session","started_at":1.0}]}' % 2**63
    )
    child = _sleeper()
    try:
        claude_process_reaper.register_claude_owned_process(_client(child.pid), runtime_dir=tmp_path).result(timeout=10)

        recorded = {record.pid for record in claude_process_reaper._load_owned_process_registry(tmp_path)}
    finally:
        child.kill()
        child.wait(10)

    assert recorded == {child.pid}


def test_two_writers_interleaved_by_force_keep_both_records(tmp_path, monkeypatch):
    # Without the registry lock, the second writer would load the file before the first replaced it,
    # and its rewrite would drop the first record.
    monkeypatch.setattr("vibe.runtime.process_start_token", lambda pid: "boot:1000")
    first_loaded = threading.Event()
    release_first = threading.Event()
    real_load = claude_process_reaper._load_owned_process_registry

    def load(runtime_dir=None):
        records = real_load(runtime_dir)
        if threading.current_thread().name == "first-writer":
            first_loaded.set()
            assert release_first.wait(10)
        return records

    monkeypatch.setattr(claude_process_reaper, "_load_owned_process_registry", load)
    first, second = _sleeper(), _sleeper()
    try:
        writers = [
            threading.Thread(
                target=claude_process_reaper._record_owned_process,
                args=(tmp_path, claude_process_reaper.ClaudeOwnedProcess(pid=child.pid)),
                name=name,
            )
            for child, name in ((first, "first-writer"), (second, "second-writer"))
        ]
        writers[0].start()
        assert first_loaded.wait(10)
        writers[1].start()
        time.sleep(0.3)
        release_first.set()
        for writer in writers:
            writer.join(10)

        recorded = {record.pid for record in real_load(tmp_path)}
    finally:
        for child in (first, second):
            child.kill()
            child.wait(10)

    assert recorded == {first.pid, second.pid}


# ---------------------------------------------------------------------------
# The registry's process identity (#2511)
# ---------------------------------------------------------------------------


def _released_record(pid: int) -> dict:
    """The registry entry a released build wrote: the ``ps -o lstart`` text of the
    process's start, parsed as that build parsed it."""

    from email.utils import parsedate_to_datetime

    import psutil

    lstart = time.strftime("%a %b %e %H:%M:%S %Y", time.localtime(int(psutil.Process(pid).create_time())))
    started_at = parsedate_to_datetime(lstart).timestamp()
    return {"pid": pid, "native_session_id": "sess-released", "owner": "session", "started_at": started_at}


@pytest.mark.skipif(os.name == "nt", reason="the registry is POSIX-only")
def test_a_registered_process_keeps_its_identity_across_a_clock_adjustment(tmp_path):
    """A record names its process by start token. The wall-clock start psutil shows
    moves when the clock is stepped (Linux) or ``kern.boottime`` ticks (macOS); the
    token does not, so the live process stays the recorded one."""

    from tests.test_process_start_token import _clock_adjusted

    child = _sleeper()
    try:
        claude_process_reaper.register_claude_owned_process(_client(child.pid), runtime_dir=tmp_path).result(timeout=10)
        (record,) = claude_process_reaper._load_owned_process_registry(tmp_path)
        assert record.start_token is not None and record.started_at is None
        assert claude_process_reaper.recorded_process_mismatch(record) is None

        with _clock_adjusted():
            assert claude_process_reaper.recorded_process_mismatch(record) is None
    finally:
        child.kill()
        child.wait(10)


@pytest.mark.skipif(os.name == "nt", reason="the registry is POSIX-only")
def test_a_released_record_keeps_the_proof_its_build_applied(tmp_path):
    """A record a released build wrote carries ``started_at`` from ``ps -o lstart``
    and no token. It is not migrated: it still names its live process, read now
    without ``ps``, and a registration keeps it as it was."""

    child, registering = _sleeper(), _sleeper()
    try:
        released = _released_record(child.pid)
        registry = tmp_path / claude_process_reaper.CLAUDE_PROCESS_REGISTRY_NAME
        registry.write_text(json.dumps({"processes": [released]}), encoding="utf-8")

        (record,) = claude_process_reaper._load_owned_process_registry(tmp_path)
        assert record.start_token is None and record.started_at == released["started_at"]
        assert claude_process_reaper.recorded_process_mismatch(record) is None

        claude_process_reaper.register_claude_owned_process(_client(registering.pid), runtime_dir=tmp_path).result(
            timeout=10
        )
        entries = {entry["pid"]: entry for entry in json.loads(registry.read_text(encoding="utf-8"))["processes"]}
        assert entries[child.pid] == {**released, "native_session_id": "sess-released"}
    finally:
        for process in (child, registering):
            process.kill()
            process.wait(10)


@pytest.fixture
def new_york_time(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.skipif(os.name == "nt", reason="the registry is POSIX-only")
@pytest.mark.parametrize("occurrence", ["first", "second"])
def test_a_released_record_written_in_a_dst_fold_keeps_matching(monkeypatch, new_york_time, occurrence):
    """``ps -o lstart`` prints local time without a zone, and the released parser read
    a repeated hour as its first occurrence. A process started in the second one was
    recorded an hour early, and must still match its record."""

    from email.utils import parsedate_to_datetime

    # 01:30 on 2026-11-01 happens twice in New York: at 05:30 and at 06:30 UTC.
    started = {"first": 1793511000.4, "second": 1793514600.4}[occurrence]
    recorded = parsedate_to_datetime("Sun Nov  1 01:30:00 2026").timestamp()
    monkeypatch.setattr("vibe.runtime.pid_alive", lambda pid: True)
    monkeypatch.setattr("vibe.runtime.process_start_seconds", lambda pid: started)
    record = claude_process_reaper.ClaudeOwnedProcess(pid=4321, started_at=recorded)

    assert claude_process_reaper.recorded_process_mismatch(record) is None
    later = claude_process_reaper.ClaudeOwnedProcess(pid=4321, started_at=recorded + 7200)
    assert claude_process_reaper.recorded_process_mismatch(later) == "pid_reused"


@pytest.mark.parametrize(
    ("record", "alive", "token", "started", "reason"),
    [
        (claude_process_reaper.ClaudeOwnedProcess(pid=4321), True, "boot:1", 1000.0, "identity_unprovable"),
        (claude_process_reaper.ClaudeOwnedProcess(pid=4321, start_token="boot:1"), False, None, None, "process_not_alive"),
        (claude_process_reaper.ClaudeOwnedProcess(pid=4321, start_token="boot:1"), True, None, None, "identity_unverifiable"),
        (claude_process_reaper.ClaudeOwnedProcess(pid=4321, start_token="boot:1"), True, "boot:2", None, "pid_reused"),
        (claude_process_reaper.ClaudeOwnedProcess(pid=4321, start_token="boot:1"), True, "boot:1", None, None),
        (claude_process_reaper.ClaudeOwnedProcess(pid=4321, started_at=1000.0), True, None, None, "identity_unverifiable"),
        (claude_process_reaper.ClaudeOwnedProcess(pid=4321, started_at=1000.0), True, None, 5000.0, "pid_reused"),
        (claude_process_reaper.ClaudeOwnedProcess(pid=4321, started_at=1000.0), True, None, 1000.4, None),
    ],
)
def test_a_record_that_cannot_be_shown_to_name_its_process_says_why(monkeypatch, record, alive, token, started, reason):
    """Running's orphan list, "end orphan" and the reaper share this verdict. A record
    with no identity -- none could be read when it was written -- is never acted on;
    nor is one whose identity cannot be read now. "End orphan" reports which."""

    monkeypatch.setattr("vibe.runtime.pid_alive", lambda pid: alive)
    monkeypatch.setattr("vibe.runtime.process_start_token", lambda pid: token)
    monkeypatch.setattr("vibe.runtime.process_start_seconds", lambda pid: started)

    assert claude_process_reaper.recorded_process_mismatch(record) == reason
