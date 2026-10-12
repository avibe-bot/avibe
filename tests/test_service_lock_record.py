"""The service lock must never make its own holder record unreadable.

``service.lock`` is two things at once: the exclusion primitive, and the record
naming who holds it. On POSIX that costs nothing -- ``fcntl.flock`` is advisory,
so the bytes stay readable whether or not anyone holds the lock. On Windows a
byte-range lock is mandatory and scoped to a HANDLE rather than to a process: an
exclusive lock denies every other handle, including a second handle opened by
the locking process itself, both read and write access to the locked range.

Locking the bytes the record lives in therefore hid that record for exactly as
long as the lock meant anything, and every reader in ``vibe.runtime`` treats an
unreadable record as "nobody is holding this". A Windows service consequently
could not be seen to have started by the process that launched it, and could not
recognise itself as the lock owner, so it tore itself down over a lease it had
never lost.

Each test below asserts one consequence of that. They run on every platform:
red on Windows until the lock byte moved past the record, green on POSIX before
and after, where their job is to pin the semantics the Windows fix must not
change.
"""

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe import runtime


class _HeldServiceLockTestCase(unittest.TestCase):
    """Holds the real service lock over a temporary runtime directory.

    Nothing here is faked: the lock is taken through the same entry point
    ``main.py`` uses, so the handle under test is the one a live service holds.
    Only the paths are redirected, and ``ensure_dirs`` is stubbed because its
    bootstrap guard is about the real data directory, not about this lock.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        runtime_dir = Path(self._tmp.name) / "runtime"
        runtime_dir.mkdir(parents=True)
        self.lock_path = runtime_dir / "service.lock"
        self.pid_path = runtime_dir / "vibe.pid"
        self._patches = [
            patch("vibe.runtime.ensure_dirs", return_value=None),
            patch(
                "vibe.runtime.paths.get_runtime_service_lock_path",
                return_value=self.lock_path,
            ),
            patch("vibe.runtime.paths.get_runtime_pid_path", return_value=self.pid_path),
        ]
        for started in self._patches:
            started.start()
        self.addCleanup(self._tmp.cleanup)
        for started in reversed(self._patches):
            self.addCleanup(started.stop)
        self.addCleanup(runtime.release_service_instance_lock)
        runtime.acquire_service_instance_lock()


class ServiceLockRecordReadableWhileHeldTests(_HeldServiceLockTestCase):
    def test_the_holder_record_is_readable_while_the_lock_is_held(self):
        """The record only ever gets read while the lock is held.

        Every caller asks "who holds this" of a lock that is, by construction,
        currently held. A lock that hides its own record answers that question
        with silence at the only moment the question is asked.
        """

        record = runtime.read_service_instance_lock_record()
        self.assertIsNotNone(record)
        self.assertEqual(record.get("pid"), os.getpid())

    def test_a_probe_learns_the_holder_pid_rather_than_nothing(self):
        """``service_instance_lock_available`` is how a second process finds out
        who beat it to the lock. Returning ``None`` for the holder turns a
        precise refusal into an anonymous one.
        """

        available, holder_pid = runtime.service_instance_lock_available()
        self.assertFalse(available)
        self.assertEqual(holder_pid, os.getpid())

    def test_the_holder_recognises_itself_through_a_second_handle(self):
        """This is the in-process two-handle case, and the expensive one.

        ``current_process_owns_service_instance`` re-opens the lock file instead
        of consulting the handle it already holds, so it reads the record back
        through a handle that is not the one holding the lock. Reading ``False``
        here is what makes a healthy service tear itself down for a lease it
        never lost.
        """

        self.assertTrue(runtime.service_lock_held_by(os.getpid()))
        self.assertTrue(runtime.current_process_owns_service_instance())

    def test_a_launcher_can_see_the_service_it_just_spawned(self):
        """The bounded wait a launcher runs against a freshly spawned service.

        ``service_pid_recorded`` is the predicate behind that wait; when it can
        never become true the launcher reports the service as having failed to
        acquire a lock the service is in fact holding.
        """

        self.assertTrue(runtime.service_pid_recorded(os.getpid()))

    def test_the_phase_the_holder_wrote_is_visible_to_a_watcher(self):
        """An upgrade watcher waits for exactly this transition to call the new
        release good. It is written through the locked handle and read back
        through an unlocked one.
        """

        runtime.mark_service_instance_started()
        self.assertTrue(runtime.service_instance_started(os.getpid()))

    def test_the_lock_file_holds_the_record_and_nothing_else(self):
        """Pins the on-disk shape against a fix that reserves bytes inside the
        file rather than past it.

        Keeping the locked byte at offset 0 and moving the record to offset 1
        would also make ``_lock_file_pid`` work, and would break every reader
        that takes the file at face value -- ``read_service_instance_lock_record``
        among them, and a human running ``type service.lock`` as well.
        """

        payload = json.loads(self.lock_path.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("pid"), os.getpid())


class ServiceLockStillExcludesTests(unittest.TestCase):
    """Moving the Windows lock byte must not cost the lock its only real job."""

    def test_the_lock_is_free_before_taken_during_and_free_again_after(self):
        with TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir) / "runtime"
            runtime_dir.mkdir(parents=True)
            lock_path = runtime_dir / "service.lock"
            pid_path = runtime_dir / "vibe.pid"

            with patch("vibe.runtime.ensure_dirs", return_value=None), patch(
                "vibe.runtime.paths.get_runtime_service_lock_path",
                return_value=lock_path,
            ), patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                self.assertEqual(runtime.service_instance_lock_available(), (True, None))
                runtime.acquire_service_instance_lock()
                try:
                    available, _holder = runtime.service_instance_lock_available()
                    self.assertFalse(available)
                finally:
                    runtime.release_service_instance_lock()
                self.assertEqual(runtime.service_instance_lock_available(), (True, None))


@unittest.skipIf(os.name == "nt", "POSIX locking path")
class PosixServiceLockUnchangedTests(unittest.TestCase):
    """The POSIX branch has no lock byte, and must not acquire one.

    The Windows fix moves an offset that POSIX does not have and does not need,
    because an advisory whole-file lock never denied a reader anything. This
    pins that asymmetry so the platform with an installed base cannot be
    silently migrated to a different locked range.
    """

    def test_posix_takes_an_advisory_whole_file_flock_at_offset_zero(self):
        import fcntl

        observed: list[tuple[int, int]] = []
        real_flock = fcntl.flock

        def _record(fd, operation):
            observed.append((os.lseek(fd, 0, os.SEEK_CUR), operation))
            return real_flock(fd, operation)

        with TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir) / "runtime"
            runtime_dir.mkdir(parents=True)
            lock_path = runtime_dir / "service.lock"
            pid_path = runtime_dir / "vibe.pid"

            with patch("vibe.runtime.ensure_dirs", return_value=None), patch(
                "vibe.runtime.paths.get_runtime_service_lock_path",
                return_value=lock_path,
            ), patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path), patch(
                "fcntl.flock", _record
            ):
                runtime.acquire_service_instance_lock()
                runtime.release_service_instance_lock()

        self.assertEqual(observed[0], (0, fcntl.LOCK_EX | fcntl.LOCK_NB))
        self.assertEqual(observed[-1][1], fcntl.LOCK_UN)


# Holds the service lock the way a running service does, through the entry
# point `main.py` uses, until it is killed. With a third argument it holds it
# as an older release did: the lock, and a record naming only its pid.
_HOLDER = """
import json, os, sys, time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from vibe import runtime
lock_path = Path(sys.argv[2])
if len(sys.argv) > 3:
    held = lock_path.open("a+", encoding="utf-8")
    assert runtime._take_lock_file(held)
    held.seek(0)
    held.truncate()
    held.write(json.dumps({"pid": os.getpid()}))
    held.flush()
else:
    with patch("vibe.runtime.ensure_dirs", return_value=None), patch(
        "vibe.runtime.paths.get_runtime_service_lock_path", return_value=lock_path
    ), patch("vibe.runtime.paths.get_runtime_pid_path", return_value=lock_path.with_name("vibe.pid")):
        runtime.acquire_service_instance_lock()
print("held", flush=True)
time.sleep(600)
"""


class ServiceLockContentionTests(unittest.TestCase):
    """Who holds the lock, as a starting service and a launcher must read it.

    The lock is held by a running service, and also, for an instant, by every
    check of it: a lock cannot be tested without being taken. ``vibe start``
    checks every 100 ms while it waits for the service it launched, and a
    service whose single attempt landed in one of those instants took the check
    for another service and exited ("Vibe service process pid=... did not
    acquire the service lock" on a fresh Windows home). A holder that was just
    killed can also keep the lock a while on Windows. Neither names a live
    process in the record; a running service does, and must still be refused
    at once.
    """

    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime_dir = Path(tmp.name) / "runtime"
        runtime_dir.mkdir(parents=True)
        self.lock_path = runtime_dir / "service.lock"
        for started in (
            patch("vibe.runtime.ensure_dirs", return_value=None),
            patch("vibe.runtime.paths.get_runtime_service_lock_path", return_value=self.lock_path),
            patch("vibe.runtime.paths.get_runtime_pid_path", return_value=runtime_dir / "vibe.pid"),
        ):
            started.start()
            self.addCleanup(started.stop)

    def _hold_like_a_check(self, *, record: str = "") -> None:
        """Hold the lock through another handle, naming whatever ``record`` says, for 200 ms."""

        check = self.lock_path.open("a+", encoding="utf-8")
        self.addCleanup(check.close)
        self.assertTrue(runtime._take_lock_file(check))
        check.seek(0)
        check.truncate()
        check.write(record)
        check.flush()
        release = threading.Timer(0.2, runtime._unlock_file, args=(check,))
        release.start()
        self.addCleanup(release.join)

    def _acquire(self) -> None:
        runtime.acquire_service_instance_lock()
        self.addCleanup(runtime.release_service_instance_lock)
        self.assertEqual(json.loads(self.lock_path.read_text(encoding="utf-8"))["pid"], os.getpid())

    def _holder(self, *, older_release: bool = False) -> subprocess.Popen:
        holder = subprocess.Popen(
            [
                sys.executable, "-c", _HOLDER, str(Path(__file__).resolve().parents[1]), str(self.lock_path),
                *(["older-release"] if older_release else []),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.wait, 10)
        self.addCleanup(holder.kill)
        self.addCleanup(holder.stdout.close)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        return holder

    def test_a_service_starting_during_a_check_takes_the_lock_once_the_check_ends(self):
        self._hold_like_a_check()
        self._acquire()

    def test_a_service_starting_while_a_dead_holder_s_lock_is_released_takes_it(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait(timeout=10)
        self._hold_like_a_check(record=json.dumps({"pid": gone.pid}))
        self._acquire()

    def test_a_record_whose_pid_passed_to_another_process_is_not_a_running_service(self):
        """After a kill or a reboot the record's pid can belong to anything."""

        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(unrelated.wait, 10)
        self.addCleanup(unrelated.kill)
        self._hold_like_a_check(record=json.dumps({"pid": unrelated.pid, "start_token": "not-its-start"}))
        self._acquire()

    def test_a_record_without_a_start_time_names_no_one_while_a_check_holds_the_lock(self):
        """An older release's record cannot tell its holder from a pid reused since."""

        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(unrelated.wait, 10)
        self.addCleanup(unrelated.kill)
        self._hold_like_a_check(record=json.dumps({"pid": unrelated.pid}))
        self._acquire()

    def test_a_holder_from_a_release_that_wrote_no_start_time_is_still_refused(self):
        """It keeps the lock past the wait, so the refusal comes when the wait runs out."""

        holder = self._holder(older_release=True)
        with self.assertRaises(runtime.ServiceAlreadyRunningError) as refused:
            runtime.acquire_service_instance_lock()
        self.assertEqual(refused.exception.holder_pid, holder.pid)

    def test_a_service_started_right_after_the_last_was_killed_takes_the_lock(self):
        """The acceptance test's own order: kill the service outright, start the next."""

        holder = self._holder()
        holder.kill()
        holder.wait(timeout=10)
        self._acquire()

    def test_a_running_service_is_still_refused_at_once(self):
        holder = self._holder()
        began = time.monotonic()
        with self.assertRaises(runtime.ServiceAlreadyRunningError) as refused:
            runtime.acquire_service_instance_lock()
        self.assertLess(time.monotonic() - began, runtime._SERVICE_LOCK_SETTLE_SECONDS)
        self.assertEqual(refused.exception.holder_pid, holder.pid)

    @unittest.skipUnless(sys.platform.startswith("linux"), "only Linux derives a start time from the boot time")
    def test_a_running_service_is_still_recognised_after_the_clock_is_stepped(self):
        """psutil's Linux start time moves with the clock; the holder's identity must not.

        A VM's time sync, a WSL2 resume or NTP after boot steps the wall clock,
        and psutil derives a Linux process's start time from the boot time that
        step moves: the same holder then reads as started 21 seconds away from
        its record. Recognising it by that time made every check wait out the
        bound for a service that was running.
        """

        holder = self._holder()
        real_create_time = runtime.psutil.Process.create_time
        with patch.object(
            runtime.psutil.Process, "create_time", lambda process: real_create_time(process) - 21.0
        ), patch.object(runtime.time, "sleep", side_effect=AssertionError("waited for a running service")):
            self.assertEqual(runtime.service_instance_lock_available(settle=True), (False, holder.pid))
            with self.assertRaises(runtime.ServiceAlreadyRunningError) as refused:
                runtime.acquire_service_instance_lock()
        self.assertEqual(refused.exception.holder_pid, holder.pid)

    @unittest.skipUnless(sys.platform == "darwin", "macOS moves kern.boottime in routine clock adjustments")
    def test_a_running_service_is_still_recognised_after_kern_boottime_ticks(self):
        """psutil's macOS start time moves with kern.boottime; the holder's identity must not.

        psutil adds the change in whole seconds of kern.boottime since it was
        imported, and the system time daemon moves kern.boottime across a whole
        second in routine adjustments. The holder then reads exactly one second
        away from its record, in this process and in no fresh one.
        """

        from psutil import _psosx

        holder = self._holder()
        before = runtime.process_create_time(holder.pid)
        with patch.object(_psosx, "boot_time", lambda: _psosx.INIT_BOOT_TIME - 1), patch.object(
            runtime.time, "sleep", side_effect=AssertionError("waited for a running service")
        ):
            self.assertEqual(runtime.process_create_time(holder.pid), before + 1.0)
            self.assertEqual(runtime.service_instance_lock_available(settle=True), (False, holder.pid))
            with self.assertRaises(runtime.ServiceAlreadyRunningError) as refused:
                runtime.acquire_service_instance_lock()
        self.assertEqual(refused.exception.holder_pid, holder.pid)

    def test_checks_outside_a_start_answer_from_one_attempt(self):
        """The service asks these on its event loop, where a wait stalls everything behind it."""

        self._hold_like_a_check()
        with patch.object(runtime.time, "sleep", side_effect=AssertionError("a check waited")):
            self.assertEqual(runtime.service_instance_lock_available(), (False, None))
            self.assertFalse(runtime.service_lock_held_by(os.getpid()))

    def test_a_check_still_reads_a_running_service_at_once(self):
        holder = self._holder()
        began = time.monotonic()
        self.assertEqual(runtime.service_instance_lock_available(), (False, holder.pid))
        self.assertLess(time.monotonic() - began, runtime._SERVICE_LOCK_SETTLE_SECONDS)


class ServiceOwnsItsLockTests(unittest.TestCase):
    """The running service's own question, asked on its event loop by every lane.

    It is answered from the handle the service holds: taking the lock again,
    or reading a record that a clock step can make unrecognisable, cost each
    call the full wait and stalled turns, scheduled tasks and watches.
    """

    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime_dir = Path(tmp.name) / "runtime"
        runtime_dir.mkdir(parents=True)
        self.lock_path = runtime_dir / "service.lock"
        for started in (
            patch("vibe.runtime.ensure_dirs", return_value=None),
            patch("vibe.runtime.paths.get_runtime_service_lock_path", return_value=self.lock_path),
            patch("vibe.runtime.paths.get_runtime_pid_path", return_value=runtime_dir / "vibe.pid"),
        ):
            started.start()
            self.addCleanup(started.stop)
        self.addCleanup(runtime.release_service_instance_lock)
        runtime.acquire_service_instance_lock()

    def test_the_owner_knows_without_taking_the_lock_or_waiting(self):
        with patch.object(runtime, "_take_lock_file", side_effect=AssertionError("took the lock")), patch.object(
            runtime.time, "sleep", side_effect=AssertionError("waited")
        ), patch.object(runtime, "process_start_token", side_effect=AssertionError("read its start")):
            self.assertTrue(runtime.current_process_owns_service_instance())

    def test_a_released_lock_is_no_longer_owned(self):
        runtime.release_service_instance_lock()
        self.assertFalse(runtime.current_process_owns_service_instance())

    def test_a_child_that_inherited_the_handle_does_not_own_it(self):
        with patch.object(runtime, "_SERVICE_INSTANCE_LOCK_OWNER_PID", os.getpid() + 1):
            self.assertFalse(runtime.current_process_owns_service_instance())

    @unittest.skipIf(os.name == "nt", "Windows cannot remove a file this process holds open")
    def test_a_lock_file_replaced_under_the_service_is_no_longer_owned(self):
        """Another service could lock the new file, so this one must stop scheduling."""

        self.lock_path.unlink()
        self.assertFalse(runtime.current_process_owns_service_instance())
        self.lock_path.write_text("", encoding="utf-8")
        self.assertFalse(runtime.current_process_owns_service_instance())


@unittest.skipUnless(os.name == "nt", "Windows byte-range lock semantics")
class WindowsMandatoryLockFactTests(unittest.TestCase):
    """The platform fact the fix rests on, pinned so nobody re-derives it.

    This asserts nothing about Avibe. It asserts that Windows denies a read of a
    locked range through a second handle opened by the same process. If that
    ever stops being true, the reason the service lock byte lives past the
    record stops applying, and this is the test that will say so.
    """

    def test_a_second_handle_in_this_process_cannot_read_a_locked_byte(self):
        import msvcrt

        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "locked.bin"
            target.write_bytes(b"payload")

            with open(target, "r+b") as holder:
                holder.seek(0)
                msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
                try:
                    with open(target, "rb") as reader:
                        with self.assertRaises(OSError):
                            reader.read()
                finally:
                    holder.seek(0)
                    msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 1)

            self.assertEqual(target.read_bytes(), b"payload")

    def test_a_byte_past_the_end_can_be_locked_without_hiding_the_content(self):
        """The other half of the fix: a lock beyond end-of-file is legal, does
        not extend the file, and leaves every byte a reader cares about
        readable. The offset here is the service lock's own idea, written out
        locally so this stays a statement about Windows rather than about us.
        """

        import msvcrt

        past_any_record = 1 << 30

        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "sentinel.bin"
            target.write_bytes(b"payload")

            with open(target, "r+b") as holder:
                os.lseek(holder.fileno(), past_any_record, os.SEEK_SET)
                msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
                try:
                    self.assertEqual(target.read_bytes(), b"payload")
                finally:
                    os.lseek(holder.fileno(), past_any_record, os.SEEK_SET)
                    msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 1)

            self.assertEqual(target.stat().st_size, len(b"payload"))


if __name__ == "__main__":
    unittest.main()
