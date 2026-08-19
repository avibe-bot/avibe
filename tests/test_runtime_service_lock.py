import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe import runtime


class RuntimeServiceLockTests(unittest.TestCase):
    def setUp(self):
        self._extra_service_pids = patch("vibe.runtime.extra_service_process_pids", return_value=[])
        self._extra_service_pids.start()
        # These tests target the generic (non-scoped) start_service contract via
        # wait_for_service_pid. On a Linux dev host with a user systemd manager,
        # maybe_systemd_scope_prefix() is truthy and would route start_service
        # through the scoped poll-and-adopt path (and real host lock state), so
        # pin it off here. The scoped path has its own dedicated tests.
        self._no_scope = patch("vibe.runtime.maybe_systemd_scope_prefix", return_value=[])
        self._no_scope.start()

    def tearDown(self):
        self._no_scope.stop()
        self._extra_service_pids.stop()

    def test_start_service_reuses_existing_live_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            pid_path.write_text("12345", encoding="utf-8")

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.pid_alive", return_value=True):
                    with patch(
                        "vibe.runtime.get_process_command",
                        return_value=f"{sys.executable} {runtime.get_service_main_path()}",
                    ):
                        with patch("vibe.runtime.service_pid_recorded", return_value=True):
                            with patch("vibe.runtime.spawn_service_background_process") as spawn_background:
                                pid = runtime.start_service()

            self.assertEqual(pid, 12345)
            spawn_background.assert_not_called()

    def test_start_service_ignores_reused_unrelated_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            pid_path.write_text("12345", encoding="utf-8")

            def fake_spawn(args, stdout_name, stderr_name, env=None):
                return SimpleNamespace(pid=67890, poll=lambda: None)

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.pid_alive", return_value=True):
                    with patch("vibe.runtime.get_process_command", return_value="/usr/bin/unrelated --work"):
                        with patch("vibe.runtime.service_instance_lock_available", return_value=(True, None)):
                            with patch(
                                "vibe.runtime.spawn_service_background_process", side_effect=fake_spawn
                            ) as spawn_background:
                                with patch("vibe.runtime.wait_for_service_pid", return_value=True):
                                    pid = runtime.start_service()

            self.assertEqual(pid, 67890)
            spawn_background.assert_called_once()
            self.assertEqual(pid_path.read_text(encoding="utf-8"), "67890")

    def test_start_service_preserves_mismatched_pidfile_when_lock_is_held(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            pid_path.write_text("12345", encoding="utf-8")

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.pid_alive", return_value=True):
                    with patch("vibe.runtime.get_process_command", return_value="/other/worktree/main.py"):
                        with patch("vibe.runtime.service_pid_recorded", return_value=False):
                            with patch("vibe.runtime.service_instance_lock_available", return_value=(False, 12345)):
                                with patch("vibe.runtime.spawn_service_background_process") as spawn_background:
                                    pid = runtime.start_service()

            self.assertEqual(pid, 12345)
            self.assertEqual(pid_path.read_text(encoding="utf-8"), "12345")
            spawn_background.assert_not_called()

    def test_start_service_refuses_duplicate_when_pidfile_missing_but_lock_is_held(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.service_instance_lock_available", return_value=(False, 12345)):
                    with patch("vibe.runtime.pid_alive", return_value=True):
                        with patch("vibe.runtime.spawn_service_background_process") as spawn_background:
                            with self.assertRaises(runtime.ServiceAlreadyRunningError):
                                runtime.start_service()

            spawn_background.assert_not_called()

    def test_start_service_reuses_live_pid_when_command_is_unreadable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            pid_path.write_text("12345", encoding="utf-8")

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.pid_alive", return_value=True):
                    with patch("vibe.runtime.get_process_command", return_value=None):
                        with patch("vibe.runtime.service_pid_recorded", return_value=True):
                            with patch("vibe.runtime.spawn_service_background_process") as spawn_background:
                                pid = runtime.start_service()

            self.assertEqual(pid, 12345)
            spawn_background.assert_not_called()

    def test_start_service_errors_when_lock_holder_is_not_recorded_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.service_instance_lock_available", return_value=(False, 12345)):
                    with patch("vibe.runtime.pid_alive", return_value=True):
                        with patch("vibe.runtime.spawn_service_background_process") as spawn_background:
                            with self.assertRaises(runtime.ServiceAlreadyRunningError):
                                runtime.start_service()

            spawn_background.assert_not_called()

    def test_start_service_refuses_duplicate_when_extra_service_process_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.service_instance_lock_available", return_value=(True, None)):
                    with patch("vibe.runtime.extra_service_process_pids", return_value=[22222]):
                        with patch("vibe.runtime.spawn_service_background_process") as spawn_background:
                            with self.assertRaises(runtime.ServiceAlreadyRunningError):
                                runtime.start_service()

            spawn_background.assert_not_called()

    def test_start_service_does_not_adopt_stale_lockless_pidfile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            pid_path.write_text("12345", encoding="utf-8")
            stale_time = runtime.time.time() - runtime.SERVICE_SLOW_START_TIMEOUT_SECONDS - 10
            os.utime(pid_path, (stale_time, stale_time))

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.pid_alive", return_value=True):
                    with patch(
                        "vibe.runtime.get_process_command",
                        return_value=f"{sys.executable} {runtime.get_service_main_path()}",
                    ):
                        with patch("vibe.runtime.service_pid_recorded", return_value=False):
                            with patch("vibe.runtime.process_create_time", return_value=stale_time):
                                with patch("vibe.runtime.service_instance_lock_available", return_value=(True, None)):
                                    with patch("vibe.runtime.extra_service_process_pids", return_value=[12345]):
                                        with patch("vibe.runtime.wait_for_service_pid") as wait_for_pid:
                                            with patch(
                                                "vibe.runtime.spawn_service_background_process"
                                            ) as spawn_background:
                                                with self.assertRaises(runtime.ServiceAlreadyRunningError):
                                                    runtime.start_service(wait_for_ready=False)

            wait_for_pid.assert_not_called()
            spawn_background.assert_not_called()

    def test_start_service_returns_live_pid_when_lock_write_is_slow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            process = SimpleNamespace(pid=67890, poll=lambda: None)

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.service_instance_lock_available", return_value=(True, None)):
                    with patch("vibe.runtime.spawn_service_background_process", return_value=process):
                        with patch("vibe.runtime.wait_for_service_pid", return_value=False):
                            with patch("vibe.runtime.pid_alive", return_value=True):
                                pid = runtime.start_service(wait_for_ready=False)

            self.assertEqual(pid, 67890)
            self.assertEqual(pid_path.read_text(encoding="utf-8"), "67890")

    def test_start_service_can_skip_initial_ready_wait(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            process = SimpleNamespace(pid=67890, poll=lambda: None)

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.service_instance_lock_available", return_value=(True, None)):
                    with patch("vibe.runtime.spawn_service_background_process", return_value=process):
                        with patch("vibe.runtime.wait_for_service_pid", return_value=True) as wait_for_pid:
                            with patch("vibe.runtime.pid_alive", return_value=True):
                                pid = runtime.start_service(wait_for_ready=False, initial_ready_timeout=0)

            self.assertEqual(pid, 67890)
            wait_for_pid.assert_not_called()
            self.assertEqual(pid_path.read_text(encoding="utf-8"), "67890")

    def test_start_service_errors_when_spawned_process_dies_before_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            process = SimpleNamespace(pid=67890, poll=lambda: 1)

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.service_instance_lock_available", return_value=(True, None)):
                    with patch("vibe.runtime.spawn_service_background_process", return_value=process):
                        with patch("vibe.runtime.wait_for_service_pid", return_value=False):
                            with patch("vibe.runtime.pid_alive", return_value=True):
                                with self.assertRaises(RuntimeError):
                                    runtime.start_service()

            self.assertFalse(pid_path.exists())

    def test_start_service_waits_for_readiness_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            process = SimpleNamespace(pid=67890, poll=lambda: None)

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.service_instance_lock_available", return_value=(True, None)):
                    with patch("vibe.runtime.spawn_service_background_process", return_value=process):
                        with patch("vibe.runtime.wait_for_service_pid", side_effect=[False, True]) as wait_for_pid:
                            with patch("vibe.runtime.pid_alive", return_value=True):
                                pid = runtime.start_service()

            self.assertEqual(pid, 67890)
            self.assertEqual(wait_for_pid.call_count, 2)

    def test_start_service_reuses_pending_reservation_without_spawning_second_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            pid_path.write_text("67890", encoding="utf-8")

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.pid_alive", return_value=True):
                    with patch(
                        "vibe.runtime.get_process_command",
                        return_value=f"{sys.executable} {runtime.get_service_main_path()}",
                    ):
                        with patch("vibe.runtime.service_pid_recorded", return_value=False):
                            with patch("vibe.runtime.spawn_service_background_process") as spawn_background:
                                pid = runtime.start_service(wait_for_ready=False)

            self.assertEqual(pid, 67890)
            spawn_background.assert_not_called()

    def test_start_service_reuses_scoped_wrapper_reservation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            pid_path.write_text("67890", encoding="utf-8")
            command = runtime.shlex.join(
                [
                    "systemd-run",
                    "--user",
                    "--scope",
                    "-q",
                    "-p",
                    "Delegate=yes",
                    "--",
                    sys.executable,
                    str(runtime.get_service_main_path()),
                ]
            )

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.pid_alive", return_value=True):
                    with patch("vibe.runtime.get_process_command", return_value=command):
                        with patch("vibe.runtime.service_pid_recorded", return_value=False):
                            with patch("vibe.runtime.spawn_service_background_process") as spawn_background:
                                pid = runtime.start_service(wait_for_ready=False)

            self.assertEqual(pid, 67890)
            spawn_background.assert_not_called()

    def test_start_service_adopts_scoped_wrapper_lock_holder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            pid_path.write_text("67890", encoding="utf-8")
            command = runtime.shlex.join(
                [
                    "systemd-run",
                    "--user",
                    "--scope",
                    "-q",
                    "-p",
                    "Delegate=yes",
                    "--",
                    sys.executable,
                    str(runtime.get_service_main_path()),
                ]
            )

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.pid_alive", return_value=True):
                    with patch("vibe.runtime.get_process_command", return_value=command):
                        with patch("vibe.runtime.service_pid_recorded", return_value=False):
                            with patch("vibe.runtime.wait_for_service_ready", return_value=78901) as wait_for_ready:
                                with patch("vibe.runtime.spawn_service_background_process") as spawn_background:
                                    pid = runtime.start_service()

            self.assertEqual(pid, 78901)
            wait_for_ready.assert_called_once_with(
                67890,
                timeout=runtime.SERVICE_SLOW_START_TIMEOUT_SECONDS,
            )
            spawn_background.assert_not_called()

    def test_stop_service_stops_pending_pid_reservation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            pid_path.write_text("67890", encoding="utf-8")

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.pid_alive", return_value=True):
                    with patch("vibe.runtime.stop_pid", return_value=True) as stop_pid:
                        self.assertTrue(runtime.stop_service())

            stop_pid.assert_called_once_with(67890, timeout=5)
            self.assertFalse(pid_path.exists())

    def test_stop_service_targets_lock_holder_when_pidfile_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.service_instance_lock_available", return_value=(False, 12345)):
                    with patch("vibe.runtime.pid_alive", return_value=True):
                        with patch("vibe.runtime.stop_pid", return_value=True) as stop_pid:
                            self.assertTrue(runtime.stop_service())

            stop_pid.assert_called_once_with(12345, timeout=5)

    def test_stop_service_prefers_lock_holder_over_live_pidfile_reservation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            pid_path.write_text("11111", encoding="utf-8")

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.pid_alive", return_value=True):
                    with patch("vibe.runtime.service_pid_recorded", return_value=False):
                        with patch("vibe.runtime.service_instance_lock_available", return_value=(False, 22222)):
                            with patch("vibe.runtime.stop_pid", return_value=True) as stop_pid:
                                self.assertTrue(runtime.stop_service())

            stop_pid.assert_called_once_with(22222, timeout=5)
            self.assertEqual(pid_path.read_text(encoding="utf-8"), "11111")

    def test_stop_service_stops_extra_lockless_service_processes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            pid_path.write_text("11111", encoding="utf-8")

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.resolve_service_owner_pid", return_value=11111):
                    with patch("vibe.runtime.extra_service_process_pids", return_value=[22222]):
                        with patch("vibe.runtime.stop_pid", return_value=True) as stop_pid:
                            self.assertTrue(runtime.stop_service())

            self.assertEqual([call.args[0] for call in stop_pid.call_args_list], [11111, 22222])
            self.assertFalse(pid_path.exists())

    def test_stop_service_fails_if_extra_lockless_service_survives(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            pid_path.write_text("11111", encoding="utf-8")

            def fake_stop(pid, timeout=5):
                return pid == 11111

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.resolve_service_owner_pid", return_value=11111):
                    with patch("vibe.runtime.extra_service_process_pids", return_value=[22222]):
                        with patch("vibe.runtime.stop_pid", side_effect=fake_stop) as stop_pid:
                            self.assertFalse(runtime.stop_service())

            self.assertEqual([call.args[0] for call in stop_pid.call_args_list], [11111, 22222])
            self.assertFalse(pid_path.exists())

    def test_service_processes_detects_matching_service_entry_and_home(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            package_dir = Path(tmpdir) / "pkg" / "vibe"
            package_dir.mkdir(parents=True)
            (package_dir / "service_main.py").write_text("", encoding="utf-8")
            (package_dir / "runtime.py").write_text("", encoding="utf-8")

            class FakeProcess:
                info = {"pid": 33333, "cmdline": [sys.executable, str(package_dir / "service_main.py")]}

                def cwd(self):
                    return str(package_dir)

                def environ(self):
                    return {"AVIBE_HOME": str(home)}

            with patch("vibe.runtime.paths.get_vibe_remote_dir", return_value=home):
                with patch("vibe.runtime.psutil.process_iter", return_value=[FakeProcess()]):
                    with patch("vibe.runtime.pid_alive", return_value=True):
                        with patch("vibe.runtime.service_lock_held_by", return_value=False):
                            with patch("vibe.runtime._process_is_service_session_leader", return_value=True):
                                processes = runtime.service_processes()

            self.assertEqual([process["pid"] for process in processes], [33333])
            self.assertTrue(processes[0]["home_match"])
            self.assertFalse(processes[0]["lock_owner"])

    def test_service_processes_ignores_systemd_scope_wrapper(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            command = [
                "systemd-run",
                "--user",
                "--scope",
                "-q",
                "-p",
                "Delegate=yes",
                "--",
                sys.executable,
                str(runtime.get_service_main_path()),
            ]

            class FakeProcess:
                info = {"pid": 33333, "cmdline": command}

                def cwd(self):
                    return str(runtime.get_working_dir())

                def environ(self):
                    return {
                        "AVIBE_HOME": str(home),
                        "VIBE_REQUIRE_SHUTDOWN_INTENT": "1",
                    }

            with patch("vibe.runtime.paths.get_vibe_remote_dir", return_value=home):
                with patch("vibe.runtime.psutil.process_iter", return_value=[FakeProcess()]):
                    with patch("vibe.runtime.pid_alive", return_value=True):
                        with patch("vibe.runtime.service_lock_held_by", return_value=False):
                            self.assertEqual(runtime.service_processes(), [])

    def test_service_processes_ignores_same_user_service_main_without_avibe_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            avibe_home = home / ".avibe"
            avibe_home.mkdir()
            package_dir = Path(tmpdir) / "pkg" / "vibe"
            package_dir.mkdir(parents=True)
            (package_dir / "service_main.py").write_text("", encoding="utf-8")
            (package_dir / "runtime.py").write_text("", encoding="utf-8")

            class FakeProcess:
                info = {"pid": 33333, "cmdline": [sys.executable, str(package_dir / "service_main.py")]}

                def cwd(self):
                    return str(package_dir)

                def environ(self):
                    return {"HOME": str(home)}

            with patch("vibe.runtime.paths.get_vibe_remote_dir", return_value=avibe_home):
                with patch("vibe.runtime.psutil.process_iter", return_value=[FakeProcess()]):
                    with patch("vibe.runtime.pid_alive", return_value=True):
                        with patch("vibe.runtime.service_lock_held_by", return_value=False):
                            with patch("vibe.runtime._process_is_service_session_leader", return_value=True):
                                self.assertEqual(runtime.service_processes(), [])

    def test_service_processes_detects_shell_launched_lockless_service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            repo_root = Path(tmpdir) / "repo"
            (repo_root / "vibe").mkdir(parents=True)
            (repo_root / "core").mkdir()
            (repo_root / "main.py").write_text("", encoding="utf-8")
            (repo_root / "vibe" / "runtime.py").write_text("", encoding="utf-8")
            (repo_root / "core" / "controller.py").write_text("", encoding="utf-8")

            class FakeProcess:
                info = {"pid": 33333, "cmdline": [sys.executable, "main.py"]}

                def cwd(self):
                    return str(repo_root)

                def environ(self):
                    return {"AVIBE_HOME": str(home), "VIBE_REQUIRE_SHUTDOWN_INTENT": "1"}

            with patch("vibe.runtime.paths.get_vibe_remote_dir", return_value=home):
                with patch("vibe.runtime.psutil.process_iter", return_value=[FakeProcess()]):
                    with patch("vibe.runtime.pid_alive", return_value=True):
                        with patch("vibe.runtime.service_lock_held_by", return_value=False):
                            with patch("vibe.runtime._process_is_service_session_leader", return_value=False):
                                processes = runtime.service_processes()

            self.assertEqual([process["pid"] for process in processes], [33333])
            self.assertFalse(processes[0]["session_leader"])

    def test_service_processes_ignores_service_entry_path_used_as_data_argument(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            repo_root = Path(tmpdir) / "repo"
            (repo_root / "vibe").mkdir(parents=True)
            (repo_root / "core").mkdir()
            (repo_root / "main.py").write_text("", encoding="utf-8")
            (repo_root / "vibe" / "runtime.py").write_text("", encoding="utf-8")
            (repo_root / "core" / "controller.py").write_text("", encoding="utf-8")

            class FakeProcess:
                info = {
                    "pid": 33333,
                    "cmdline": [sys.executable, "-c", "print('not service')", str(repo_root / "main.py")],
                }

                def cwd(self):
                    return str(repo_root)

                def environ(self):
                    return {"AVIBE_HOME": str(home), "VIBE_REQUIRE_SHUTDOWN_INTENT": "1"}

            with patch("vibe.runtime.paths.get_vibe_remote_dir", return_value=home):
                with patch("vibe.runtime.psutil.process_iter", return_value=[FakeProcess()]):
                    with patch("vibe.runtime.pid_alive", return_value=True):
                        with patch("vibe.runtime._process_is_service_session_leader", return_value=True):
                            self.assertEqual(runtime.service_processes(), [])

    def test_stop_service_ignores_pidfile_data_argument_that_references_service_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            pid_path.write_text("33333", encoding="utf-8")
            repo_root = Path(tmpdir) / "repo"
            (repo_root / "vibe").mkdir(parents=True)
            (repo_root / "core").mkdir()
            (repo_root / "main.py").write_text("", encoding="utf-8")
            (repo_root / "vibe" / "runtime.py").write_text("", encoding="utf-8")
            (repo_root / "core" / "controller.py").write_text("", encoding="utf-8")
            command = runtime.shlex.join([sys.executable, "-c", "print('not service')", str(repo_root / "main.py")])

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.pid_alive", return_value=True):
                    with patch("vibe.runtime.service_pid_recorded", return_value=False):
                        with patch("vibe.runtime.service_instance_lock_available", return_value=(True, None)):
                            with patch("vibe.runtime.get_process_command", return_value=command):
                                with patch(
                                    "vibe.runtime.psutil.Process",
                                    return_value=SimpleNamespace(cwd=lambda: str(repo_root)),
                                ):
                                    with patch("vibe.runtime.stop_pid") as stop_pid:
                                        self.assertFalse(runtime.stop_service())

            stop_pid.assert_not_called()

    def test_render_status_uses_lock_holder_when_pidfile_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status_path = Path(tmpdir) / "status.json"
            pid_path = Path(tmpdir) / "service.pid"
            runtime.write_json(status_path, {"state": "stopped", "service_pid": None})

            with patch("vibe.runtime.paths.get_runtime_status_path", return_value=status_path):
                with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                    with patch("vibe.runtime.service_instance_lock_available", return_value=(False, 12345)):
                        with patch("vibe.runtime.pid_alive", return_value=True):
                            payload = runtime.json.loads(runtime.render_status())

            self.assertTrue(payload["running"])
            self.assertEqual(payload["state"], "running")
            self.assertEqual(payload["service_pid"], 12345)
            self.assertEqual(payload["pid"], 12345)

    def test_render_status_skips_extra_process_scan_when_requested(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status_path = Path(tmpdir) / "status.json"
            runtime.write_json(status_path, {"state": "running", "service_pid": 12345})

            with patch("vibe.runtime.paths.get_runtime_status_path", return_value=status_path):
                with patch("vibe.runtime.resolve_service_owner_pid", return_value=12345):
                    with patch("vibe.runtime.extra_service_process_pids") as extra_service_process_pids:
                        payload = runtime.json.loads(runtime.render_status(detect_extra_processes=False))

            extra_service_process_pids.assert_not_called()
            self.assertTrue(payload["running"])
            self.assertEqual(payload["state"], "running")
            self.assertEqual(payload["service_pid"], 12345)
            self.assertEqual(payload["pid"], 12345)
            self.assertEqual(payload["service_owner_pid"], 12345)
            self.assertNotIn("extra_service_pids", payload)

    def test_render_status_fast_path_skips_extra_process_scan_when_owner_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status_path = Path(tmpdir) / "status.json"
            runtime.write_json(status_path, {"state": "running", "service_pid": 12345})

            with patch("vibe.runtime.paths.get_runtime_status_path", return_value=status_path):
                with patch("vibe.runtime.resolve_service_owner_pid", return_value=None):
                    with patch("vibe.runtime.extra_service_process_pids") as extra_service_process_pids:
                        payload = runtime.json.loads(runtime.render_status(detect_extra_processes=False))

            extra_service_process_pids.assert_not_called()
            self.assertFalse(payload["running"])
            self.assertEqual(payload["state"], "stopped")
            self.assertIsNone(payload["service_pid"])
            self.assertIsNone(payload["pid"])
            self.assertEqual(payload["service_owner_pid"], None)

    def test_render_status_can_surface_extra_processes_when_owner_is_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status_path = Path(tmpdir) / "status.json"
            runtime.write_json(status_path, {"state": "running", "service_pid": 12345})

            with patch("vibe.runtime.paths.get_runtime_status_path", return_value=status_path):
                with patch("vibe.runtime.resolve_service_owner_pid", return_value=12345):
                    with patch("vibe.runtime.extra_service_process_pids", return_value=[22222]):
                        payload = runtime.json.loads(runtime.render_status())

            self.assertTrue(payload["running"])
            self.assertEqual(payload["state"], "running")
            self.assertEqual(payload["service_pid"], 12345)
            self.assertEqual(payload["service_owner_pid"], 12345)
            self.assertEqual(payload["extra_service_pids"], [22222])
            self.assertEqual(payload["detail"], "pid=12345; extra_service_pids=22222")

    def test_render_status_surfaces_lockless_service_process(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status_path = Path(tmpdir) / "status.json"
            runtime.write_json(status_path, {"state": "running", "service_pid": None})

            with patch("vibe.runtime.paths.get_runtime_status_path", return_value=status_path):
                with patch("vibe.runtime.resolve_service_owner_pid", return_value=None):
                    with patch("vibe.runtime.extra_service_process_pids", return_value=[22222]):
                        payload = runtime.json.loads(runtime.render_status())

            self.assertTrue(payload["running"])
            self.assertEqual(payload["state"], "degraded")
            self.assertEqual(payload["service_pid"], 22222)
            self.assertEqual(payload["pid"], 22222)
            self.assertEqual(payload["service_owner_pid"], None)
            self.assertEqual(payload["extra_service_pids"], [22222])

    def test_wait_for_service_pid_adopts_slow_pid_file_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"
            calls = []

            def fake_service_pid_recorded(pid):
                calls.append(pid)
                if len(calls) == 2:
                    pid_path.write_text(str(pid), encoding="utf-8")
                    return True
                return False

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.service_pid_recorded", side_effect=fake_service_pid_recorded):
                    with patch("vibe.runtime.pid_alive", return_value=True):
                        with patch("vibe.runtime.time.sleep", return_value=None):
                            self.assertTrue(runtime.wait_for_service_pid(67890, timeout=1.0))

            self.assertEqual(calls, [67890, 67890])

    def test_wait_for_service_pid_fails_only_when_worker_dies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "service.pid"

            with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                with patch("vibe.runtime.service_pid_recorded", return_value=False):
                    with patch("vibe.runtime.pid_alive", return_value=False):
                        self.assertFalse(runtime.wait_for_service_pid(67890, timeout=1.0))

    def test_service_instance_lock_blocks_second_holder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir) / "runtime"
            runtime_dir.mkdir(parents=True)
            lock_path = runtime_dir / "service.lock"
            pid_path = runtime_dir / "vibe.pid"

            with patch("vibe.runtime.paths.get_runtime_service_lock_path", return_value=lock_path):
                with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                    with patch("vibe.runtime.paths.ensure_data_dirs", return_value=None):
                        runtime.acquire_service_instance_lock()
                        try:
                            available, holder_pid = runtime.service_instance_lock_available()
                        finally:
                            runtime.release_service_instance_lock()

            self.assertFalse(available)
            self.assertEqual(holder_pid, os.getpid())
            self.assertFalse(pid_path.exists())

    def test_current_process_owns_service_instance_tracks_lock_lifetime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir) / "runtime"
            runtime_dir.mkdir(parents=True)
            lock_path = runtime_dir / "service.lock"
            pid_path = runtime_dir / "vibe.pid"

            with patch("vibe.runtime.paths.get_runtime_service_lock_path", return_value=lock_path):
                with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                    with patch("vibe.runtime.paths.ensure_data_dirs", return_value=None):
                        runtime.acquire_service_instance_lock()
                        try:
                            self.assertTrue(runtime.current_process_owns_service_instance())
                        finally:
                            runtime.release_service_instance_lock()

            self.assertFalse(runtime.current_process_owns_service_instance())

    def test_a_working_service_is_a_held_lock_plus_the_holders_own_report(self):
        """"Something is holding the lock" is not "this instance has a service".

        The lock is taken before the database is migrated, so every failed
        migration passes through a state where it is held by a process that will
        never serve anything. That state is what the incident looked like, and a
        predicate that answers "running" there is the reason a dark instance was
        reported healthy -- so the holder's own claim to have finished starting is
        part of the fact, not a detail of it.

        An unreadable record while the lock is held reads as not running for the
        same reason: nothing has claimed to have started. It is indistinguishable
        from the mid-startup window, and calling that window "running" is exactly
        the mistake. The bounded wait, not this predicate, is what tells a slow
        start from a stuck one.

        ``service_instance_lock_available`` still answers the different question
        ``start_service`` asks -- whether anything at all would block a second
        start -- and stays true to the lock alone.
        """

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir) / "runtime"
            runtime_dir.mkdir(parents=True)
            lock_path = runtime_dir / "service.lock"
            pid_path = runtime_dir / "vibe.pid"

            with patch("vibe.runtime.paths.get_runtime_service_lock_path", return_value=lock_path):
                with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                    with patch("vibe.runtime.paths.ensure_data_dirs", return_value=None):
                        self.assertFalse(runtime.verified_service_running())

                        runtime.acquire_service_instance_lock()
                        try:
                            self.assertFalse(runtime.verified_service_running())
                            self.assertFalse(runtime.service_instance_lock_available()[0])

                            runtime.mark_service_instance_started()
                            self.assertTrue(runtime.verified_service_running())

                            lock_path.write_text("", encoding="utf-8")
                            self.assertIsNone(runtime.service_instance_lock_available()[1])
                            self.assertFalse(runtime.service_instance_lock_available()[0])
                            self.assertFalse(runtime.verified_service_running())
                        finally:
                            runtime.release_service_instance_lock()

                        self.assertFalse(runtime.verified_service_running())

    def test_holding_the_lock_is_not_yet_having_started(self):
        """Two different facts, and the gap between them is where upgrades fail.

        The lock is taken before the database is migrated, so a holder can be
        mid-startup or already dead from a migration it never finished. Only the
        holder can say which, and it says so by rewriting its own record through
        the handle that holds the lock -- so the claim cannot outlive the process,
        and cannot be read off a record the PREVIOUS holder left behind.
        """

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir) / "runtime"
            runtime_dir.mkdir(parents=True)
            lock_path = runtime_dir / "service.lock"
            pid_path = runtime_dir / "vibe.pid"

            with patch("vibe.runtime.paths.get_runtime_service_lock_path", return_value=lock_path):
                with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                    with patch("vibe.runtime.paths.ensure_data_dirs", return_value=None):
                        runtime.acquire_service_instance_lock()
                        try:
                            self.assertFalse(runtime.service_instance_started(os.getpid()))

                            runtime.mark_service_instance_started()
                            self.assertTrue(runtime.service_instance_started(os.getpid()))
                            # Someone else's startup, reported by a record this
                            # process happens to be able to read.
                            self.assertFalse(runtime.service_instance_started(os.getpid() + 1))
                        finally:
                            runtime.release_service_instance_lock()

    def test_a_release_that_never_reported_startup_reads_as_started(self):
        """What a rollback target on an older version can offer, kept working.

        Releases predating this distinction wrote ``running`` when they took the
        lock and never wrote again. Demanding the second write of them would make
        every rollback report a failed start for a service that is up.
        """

        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "service.lock"
            lock_path.write_text(json.dumps({"pid": os.getpid(), "phase": "running"}), encoding="utf-8")
            with patch("vibe.runtime.paths.get_runtime_service_lock_path", return_value=lock_path):
                self.assertTrue(runtime.service_instance_started(os.getpid()))

    def test_every_reporter_says_starting_while_the_lock_holder_is_still_starting(self):
        """One machine, one word, whichever reporter is asked for it.

        The lock is taken before the database is migrated, so between the two a
        holder occupies this instance without serving anybody. Everything that
        reports that machine has to say so -- `vibe status`, the dashboard reading
        the same payload, and the repair that PERSISTS the word for later readers
        alike. Each of them deriving it separately is one fact with three answers,
        and an instance stuck in its migration reading `running` for eight days is
        what the wrong answer cost.

        Asserted over the reporters together rather than one test each, because
        the property is that they agree: a fourth reporter written to derive its
        own word is the bug returning, and it belongs here where the reason is
        written down.
        """

        from vibe import cli

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir) / "runtime"
            runtime_dir.mkdir(parents=True)
            lock_path = runtime_dir / "service.lock"
            pid_path = runtime_dir / "vibe.pid"
            status_path = runtime_dir / "status.json"

            with patch("vibe.runtime.paths.get_runtime_service_lock_path", return_value=lock_path):
                with patch("vibe.runtime.paths.get_runtime_pid_path", return_value=pid_path):
                    with patch("vibe.runtime.paths.get_runtime_status_path", return_value=status_path):
                        with patch("vibe.runtime.paths.ensure_data_dirs", return_value=None):
                            runtime.acquire_service_instance_lock()
                            try:
                                self.assertEqual(runtime.resolve_service_state().state, "starting")
                                self.assertFalse(runtime.resolve_service_state().running)

                                payload = json.loads(runtime.render_status())
                                self.assertEqual(payload["state"], "starting")
                                self.assertFalse(payload["running"])
                                self.assertEqual(payload["service_pid"], os.getpid())

                                cli._write_refreshed_runtime_status()
                                self.assertEqual(runtime.read_status()["state"], "starting")

                                # And all three again once the holder reports it
                                # got through startup: the word follows the
                                # machine rather than latching to either answer.
                                runtime.mark_service_instance_started()
                                self.assertEqual(runtime.resolve_service_state().state, "running")
                                self.assertTrue(json.loads(runtime.render_status())["running"])
                                cli._write_refreshed_runtime_status()
                                self.assertEqual(runtime.read_status()["state"], "running")
                            finally:
                                runtime.release_service_instance_lock()

    def test_only_a_record_saying_so_moves_the_word_off_running(self):
        """`starting` is claimed by evidence, never inferred from its absence.

        The distinction matters because the record is not the only thing that can
        be missing -- it can be lost, truncated, or left behind by a holder that
        is gone. Reading any of those as `starting` would tell the owner of a
        working machine that it is coming up, forever, since nothing will ever
        write the record that answer waits for. That trades a stuck instance
        reading healthy for a healthy instance reading stuck, which is the same
        bug pointed the other way.

        `service_instance_started` takes the opposite default on purpose: it asks
        whether a new generation PROVED it works, and there an absent record is
        an absent proof.
        """

        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "service.lock"
            with patch("vibe.runtime.paths.get_runtime_service_lock_path", return_value=lock_path):
                with patch("vibe.runtime.resolve_service_owner_pid", return_value=4242):
                    self.assertEqual(runtime.resolve_service_state().state, "running")

                    lock_path.write_text(json.dumps({"pid": 4242, "phase": "starting"}), encoding="utf-8")
                    self.assertEqual(runtime.resolve_service_state().state, "starting")

                    # Someone else's startup, in a record this instance happens
                    # to be able to read.
                    lock_path.write_text(json.dumps({"pid": 4243, "phase": "starting"}), encoding="utf-8")
                    self.assertEqual(runtime.resolve_service_state().state, "running")


class ReadinessWaitIsNeverOptionalTests(unittest.TestCase):
    """The class of bug this closes, stated once instead of caught once per site.

    Three separate starters had each decided for themselves that a recorded pid
    meant the wait could be skipped, and each was wrong in the same way, and each
    cost its own review round. The mistake is available to every future starter
    for as long as the two predicates sit next to each other, so it is worth a
    test rather than three fixes: a fourth one written the same way fails here,
    where the reason is written down, instead of in an incident.

    Stated as the property -- no starter makes the wait conditional on the lock --
    rather than as the list of starters, so a module added later is covered
    without editing this.
    """

    PRODUCTION_MODULES = (
        Path(__file__).resolve().parents[1] / "vibe" / "runtime.py",
        Path(__file__).resolve().parents[1] / "vibe" / "cli.py",
        Path(__file__).resolve().parents[1] / "vibe" / "restart_supervisor.py",
        Path(__file__).resolve().parents[1] / "scripts" / "incus_regression_supervisor.py",
    )

    def test_no_starter_makes_the_readiness_wait_conditional_on_the_lock(self):
        import ast

        for module in self.PRODUCTION_MODULES:
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.If):
                    continue
                if "service_pid_recorded" not in ast.dump(node.test):
                    continue
                guarded = [*node.body, *node.orelse]
                waits = [
                    child
                    for branch in guarded
                    for child in ast.walk(branch)
                    if isinstance(child, ast.Call) and getattr(child.func, "attr", None) == "wait_for_service_ready"
                ]
                self.assertEqual(
                    waits,
                    [],
                    f"{module.name}:{node.lineno} waits for readiness only when the lock says so; "
                    "the lock is taken before the database is migrated, so that is the case the "
                    "wait exists for",
                )


if __name__ == "__main__":
    unittest.main()
