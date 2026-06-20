import asyncio
import logging
import os
import signal

from modules.agents import claude_process_reaper


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
    monkeypatch.setattr(claude_process_reaper, "_run_ps", lambda: table)

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
    monkeypatch.setattr(claude_process_reaper, "_run_ps", lambda: table)

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

    monkeypatch.setattr(claude_process_reaper, "_run_ps", lambda: table)
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
        "_run_ps",
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

    monkeypatch.setattr(claude_process_reaper, "_run_ps", lambda: table)
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


def test_reap_duplicate_claude_resume_processes_ignores_unrelated_unique_match(monkeypatch):
    service_pid = os.getpid()
    monkeypatch.setattr(
        claude_process_reaper,
        "_run_ps",
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

    monkeypatch.setattr(claude_process_reaper, "_run_ps", lambda: table)
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

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids={100},
            tracked_resume_ids={"sess-1": 100},
            logger=logging.getLogger("test.claude_orphan"),
        )
    )

    assert reaped == 2
    killed = {pid for pid, _ in signals}
    assert 101 in killed and 102 in killed
    assert 100 not in killed
    assert service_pid not in killed


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


def test_reap_orphaned_reaps_cross_restart_init_parented_match(monkeypatch):
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

    assert reaped == 1
    killed = {pid for pid, _ in signals}
    assert killed == {300}


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

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids=set(),
            tracked_resume_ids={},
            logger=logging.getLogger("test.claude_orphan"),
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

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids=set(),
            tracked_resume_ids={},
            logger=logging.getLogger("test.claude_orphan"),
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

    reaped = asyncio.run(
        claude_process_reaper.reap_orphaned_claude_processes(
            owned_pids=set(),
            tracked_resume_ids={},
            logger=logging.getLogger("test.claude_orphan"),
            reap_in_tree=False,
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
