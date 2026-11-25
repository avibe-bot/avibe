"""Exercise the native machine protocol with actual OS pipes, never an installed Runtime."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

import pytest

from tests.test_pairing_recovery import pairing_host as pairing_host
from tests.test_personal_claims import claim_peer as claim_peer
from vibe import desktop_claim_admission as gate, desktop_claim_pipe as pipe, desktop_local_instance as local

_ORIGIN = "http://127.0.0.1:5123"
_OPEN = {"op": "open", "origin": _ORIGIN, "subject": "owner-id", "locale": "en"}


class Parent:
    def __init__(self, write, read):
        self.input = os.fdopen(write, "wb", buffering=0)
        self.frames = queue.Queue()

        def drain():
            with os.fdopen(read, "rb") as stream:
                for line in stream:
                    self.frames.put(json.loads(line))
            self.frames.put(None)

        threading.Thread(target=drain, daemon=True).start()

    def send(self, **fields):
        self.input.write(json.dumps({"v": 1, **fields}).encode() + b"\n")

    def receive(self):
        return self.frames.get(timeout=8)

    def open(self):
        self.send(**_OPEN)
        ack = self.receive()
        assert set(ack) == {"v", "event", "challenge"} and ack["v"] == 1 and ack["event"] == "challenge"
        return ack["challenge"]

    def ticket(self):
        challenge = self.open()
        assert gate.acknowledge(_ORIGIN, challenge)
        self.send(op="begin", challenge=challenge)
        t = self.receive()
        assert (
            set(t)
            == {
                "event",
                "v",
                "claim_id",
                "user_code",
                "user_url",
                "mode",
                "device_name",
                "expires_at",
                "ordinary_decision_readback",
            }
            and t["event"] == "ticket"
            and t["mode"] == "desktop"
        )
        return t


@contextmanager
def _parent():
    source, write = os.pipe()
    read, sink = os.pipe()

    def run():
        try:
            pipe._conversation(source, sink)
        finally:
            os.close(sink)

    thread = threading.Thread(target=run, daemon=True)
    p = Parent(write, read)
    thread.start()
    try:
        yield p
    finally:
        p.input.close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("case", ["bound", "wrong_ticket", "wrong_host", "close", "eof"])
def test_pipe_can_only_accept_the_frozen_ticket_target_after_root_ack(claim_peer, case):
    with _parent() as p:
        t = p.ticket()
        assert claim_peer.decisions == [] and len(claim_peer.calls) == 1
        if case == "eof":
            p.input.close()
        elif case == "close":
            p.send(op="close")
        else:
            p.send(
                op="arm",
                claim_id="stale" if case == "wrong_ticket" else t["claim_id"],
                kind="new",
                hostname="changed.avibe.bot" if case == "wrong_host" else claim_peer.hostname,
                existing_instance_id=None,
            )
        if case in ("close", "eof"):
            assert p.receive() is None
        else:
            result = p.receive()
            expected = {
                "bound": {"outcome": "publication_finished"},
                "wrong_ticket": {"outcome": "error", "code": "decision_changed"},
                "wrong_host": {"outcome": "declined"},
            }[case]
            assert result == {"v": 1, "event": "result", **expected}
            if case == "bound":
                p.input.close()  # notification-only parent: no publication read or recheck
            assert p.receive() is None
    assert claim_peer.decisions == (["owner-id"] if case == "bound" else ["decline"])


@pytest.mark.parametrize("case", ["fake_success", "different_challenge", "early_arm", "close", "partial_eof"])
def test_pipe_never_starts_on_an_unadmitted_or_crossed_handshake(claim_peer, case):
    with _parent() as p:
        challenge = p.open()
        if case == "different_challenge":
            assert gate.acknowledge(_ORIGIN, challenge)
            p.send(op="begin", challenge="d" * 64)
        elif case == "early_arm":
            p.send(op="arm")
        elif case == "close":
            p.send(op="close")
        elif case == "partial_eof":
            p.input.write(b'{"v":1,')
            p.input.close()
        else:
            p.send(op="begin", challenge=challenge)  # the listener's synthetic 204 is not evidence
        assert p.receive() is None
    assert not gate._slot().exists() and not claim_peer.calls and not local._identity_path().exists()


@pytest.mark.parametrize(
    "wire",
    [
        b"x" * 4097,
        b'{"v":true,"op":"open"}\n',
        b'{"v":1,"op":"open","op":"close"}\n',
        json.dumps({"v": 1, **_OPEN, "issuer": "fixture-no-echo"}).encode() + b"\n",
    ],
)
def test_bad_frame_returns_no_echo_or_slot(pairing_host, wire):
    with _parent() as p:
        p.input.write(wire)
        assert p.receive() is None
    assert not gate._slot().exists()


def test_idle_handshake_expires_without_waiting_for_a_newline(pairing_host, monkeypatch):
    monkeypatch.setattr(pipe, "_OPEN_SECONDS", 0.3)
    with _parent() as p:
        p.open()
        p.input.write(b"{")
        assert p.receive() is None
    assert not gate._slot().exists()


@pytest.mark.parametrize(
    "extra,authority",
    [(["private-canary-arg"], {}), ([], {"AVIBE_CALLER_REMOTE": "1"}), ([], {"AVIBE_SESSION_ID": "fixture-session"})],
)
def test_hidden_cli_rejects_represented_callers_and_argv_without_diagnostics(pairing_host, extra, authority):
    result = subprocess.run(
        [sys.executable, "-c", "from vibe.cli import main; main()", "__desktop-claim-pipe", *extra],
        env={**os.environ, **authority},
        input=json.dumps({"v": 1, **_OPEN}).encode() + b"\n",
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0 and result.stdout == b"" and b"private-canary" not in result.stderr
    assert not gate._slot().exists()


def test_real_child_exits_on_parent_eof_without_other_output(pairing_host):
    # No admission is given: this exercises the exact launch dispatch and process
    # descriptors while ruling out any hosted call or interaction with the live service.
    with subprocess.Popen(
        [sys.executable, "-c", "from vibe.cli import main; main()", "__desktop-claim-pipe"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        out, err = child.communicate(json.dumps({"v": 1, **_OPEN}).encode() + b"\n", timeout=10)
    assert len(out.splitlines()) == 1 and set(json.loads(out)) == {"v", "event", "challenge"} and err == b""
    assert child.returncode != 0 and not gate._slot().exists() and not local._identity_path().exists()


def _blocked_peer_child():
    """Only in the hermetic subprocess test. The fake issuer has no network transport."""
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4
    from config import paths
    from vibe import cli, personal_claims
    from vibe.personal_claim_client import Ticket

    def block():
        (paths.get_state_dir() / "fixture-blocked").touch()
        threading.Event().wait(60)
        raise AssertionError("stalled fake issuer")

    phase = os.environ["TEST_BLOCK_PHASE"]
    if phase in ("source", "dispatch"):
        sync = Path(os.environ["TEST_SOURCE_SYNC"])

        def pause():
            (sync / "paused").touch()
            stop = time.monotonic() + 8
            while not (sync / "continue").exists():
                if time.monotonic() >= stop:
                    raise TimeoutError
                time.sleep(0.02)

        if phase == "source":
            original = personal_claims.DesktopClaim.run

            def paused_run(self):
                pause()  # Actual child has consumed the slot; worker hasn't taken a lock yet.
                return original(self)

            personal_claims.DesktopClaim.run = paused_run

    class Issuer:
        def __init__(self, base):
            assert base == "https://avibe.bot"
            if phase == "dispatch":
                pause()  # Source acquired; no start request sent yet.

        def start(self, *_args, **_kwargs):
            if phase in ("source", "dispatch"):
                (sync / "start-called").touch()
            if phase == "start":
                block()
            when = datetime.now(timezone.utc) + timedelta(minutes=5)
            return Ticket(
                str(uuid4()),
                "ABCD234567",
                "https://avibe.bot/connect/ABCD234567",
                when.isoformat(),
                when.timestamp(),
                5,
            )

        def observe(self, _ticket):
            block()

        def decline(self, _ticket):
            pass

    personal_claims.ClaimsClient = Issuer
    cli.main()


@contextmanager
def _child_peer(phase, **env):
    cmd = [
        sys.executable,
        "-c",
        "from tests.test_desktop_claim_pipe import _blocked_peer_child; _blocked_peer_child()",
        "__desktop-claim-pipe",
    ]
    with subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "TEST_BLOCK_PHASE": phase, **env},
    ) as child:
        p = Parent(os.dup(child.stdin.fileno()), os.dup(child.stdout.fileno()))
        child.stdin.close()  # only the test parent below holds the write end
        try:
            yield child, p
        finally:
            p.input.close()
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


@pytest.mark.parametrize("phase", ["start", "observe"])
def test_actual_child_eof_releases_machine_lock_during_stalled_remote_read(pairing_host, phase):
    from config import paths
    from storage.lock import MigrationFileLock, MigrationLockTimeout

    lock = paths.get_state_dir() / "personal-claim.lock"
    with _child_peer(phase) as (child, p):
        challenge = p.open()
        assert gate.acknowledge(_ORIGIN, challenge)
        p.send(op="begin", challenge=challenge)
        if phase == "observe":
            t = p.receive()
            p.send(
                op="arm",
                claim_id=t["claim_id"],
                kind="new",
                hostname="fixture.avibe.bot",
                existing_instance_id=None,
            )
        stop = time.monotonic() + 6
        while not (paths.get_state_dir() / "fixture-blocked").exists():
            assert time.monotonic() < stop and child.poll() is None
            time.sleep(0.02)
        with pytest.raises(MigrationLockTimeout), MigrationFileLock(lock, timeout_seconds=0):
            pytest.fail("claim did not hold machine lock")
        p.input.close()
        assert child.wait(timeout=3) != 0 and child.stderr.read() == b""
        with MigrationFileLock(lock, timeout_seconds=0):
            assert not (paths.get_state_dir() / "personal-claim-outcome.json").exists()
        assert p.receive() is None


@pytest.mark.parametrize("change", ["unchanged", "replace", "migrate", "before_dispatch"])
def test_admitted_work_starts_only_on_its_original_home(pairing_host, monkeypatch, change):
    from config import paths
    from config.v2_config import V2Config
    from vibe import remote_access

    sandbox, _ = pairing_host
    profile = sandbox / "profile"
    preferred = profile / ".avibe"
    original = profile / ".vibe_remote" if change == "migrate" else preferred
    journal = Path("state") / remote_access._PENDING_PAIRING_FILENAME
    record = remote_access._new_pairing_claim(V2Config.load(), "https://avibe.bot", "fixture")
    record["phase"] = "revoked"  # Real start on this unbound home would retire it.

    def seed(root):
        shutil.copytree(sandbox / "config", root / "config")
        (root / "state").mkdir()
        (root / journal).write_text(json.dumps(record), encoding="utf-8")

    seed(original)
    before = (original / journal).read_bytes()
    monkeypatch.setattr(Path, "home", lambda: profile)  # Parent pytest also pins Path.home.
    monkeypatch.setenv("HOME", str(profile))
    monkeypatch.setenv("USERPROFILE", str(profile))
    if change == "before_dispatch":
        selected = profile / "selected-home"
        try:
            selected.symlink_to(original, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                pytest.skip("creating the home alias needs symlink privileges")
            raise
        monkeypatch.setenv("AVIBE_HOME", str(selected))
    else:
        monkeypatch.delenv("AVIBE_HOME")
    # Both processes keep this same environment; only on-disk homes change.
    assert paths.get_vibe_remote_dir() == original
    phase = "dispatch" if change == "before_dispatch" else "source"
    with _child_peer(phase, TEST_SOURCE_SYNC=str(sandbox)) as (child, p):
        challenge = p.open()
        assert gate.acknowledge(_ORIGIN, challenge)
        p.send(op="begin", challenge=challenge)
        stop = time.monotonic() + 8
        while not (sandbox / "paused").exists():
            assert time.monotonic() < stop and child.poll() is None
            time.sleep(0.02)
        assert not gate._slot().exists()
        if change == "replace":
            original.rename(profile / "retired")
            seed(original)  # Same path, different directory.
        elif change == "migrate":
            assert paths.migrate_default_home() == preferred  # Same directory, new physical path.
        elif change == "before_dispatch":
            assert (original / "state/installation-id.json").is_file()
            successor = profile / "successor"
            seed(successor)
            selected.unlink()
            selected.symlink_to(successor, target_is_directory=True)  # Original lock remains held.
        (sandbox / "continue").touch()
        frame = p.receive()
        if change == "unchanged":
            assert frame["event"] == "ticket" and (sandbox / "start-called").exists()
            assert local._identity_path().is_file() and not (original / journal).exists()
            p.input.close()
        else:
            assert frame == {"v": 1, "event": "result", "outcome": "error", "code": "local_binding_changed"}
            assert not (sandbox / "start-called").exists() and not local._identity_path().exists()
            assert not (paths.get_state_dir() / "personal-claim.lock").exists()
            assert (paths.get_vibe_remote_dir() / journal).read_bytes() == before
        assert p.receive() is None and child.wait(timeout=5) in (0, 1) and child.stderr.read() == b""


def test_unknown_worker_outcome_cannot_become_a_diagnostic(monkeypatch, claim_peer):
    monkeypatch.setattr(
        pipe.DesktopClaim,
        "run",
        lambda self: {"state": "error", "code": "fixture-sensitive-error", "private": "fixture-not-for-transcripts"},
    )
    with _parent() as p:
        challenge = p.open()
        assert gate.acknowledge(_ORIGIN, challenge)
        p.send(op="begin", challenge=challenge)
        assert p.receive() == {"v": 1, "event": "result", "outcome": "error", "code": "local_claim_unavailable"}
