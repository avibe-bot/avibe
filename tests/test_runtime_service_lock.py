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

    def tearDown(self):
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

if __name__ == "__main__":
    unittest.main()
