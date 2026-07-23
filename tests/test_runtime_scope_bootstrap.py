import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe import runtime


def _passing_patches():
    """Every predicate in maybe_systemd_scope_prefix() set to the happy path.

    Individual tests override one entry to assert it fails open to []."""
    stack = ExitStack()
    stack.enter_context(patch.object(runtime.sys, "platform", "linux"))
    stack.enter_context(patch("vibe.runtime.shutil.which", return_value="/usr/bin/systemd-run"))
    stack.enter_context(patch("vibe.runtime.os.getuid", return_value=1018, create=True))
    stack.enter_context(patch("vibe.runtime.Path.is_socket", return_value=True))
    stack.enter_context(patch("core.resource_governance.detect_cgroup_root", return_value=Path("/sys/fs/cgroup")))
    stack.enter_context(patch("vibe.runtime._resource_governance_mode", return_value="auto"))
    stack.enter_context(patch("vibe.runtime._ensure_linger_enabled", return_value=True))
    stack.enter_context(patch("vibe.runtime._systemd_run_self_test_ok", return_value=True))
    return stack


class MaybeSystemdScopePrefixTests(unittest.TestCase):
    def test_all_predicates_pass_returns_full_prefix(self):
        with _passing_patches():
            self.assertEqual(runtime.maybe_systemd_scope_prefix(), list(runtime.SYSTEMD_SCOPE_PREFIX))

    def test_prefix_ends_with_double_dash_separator(self):
        with _passing_patches():
            self.assertEqual(runtime.maybe_systemd_scope_prefix()[-1], "--")

    def test_non_linux_fails_open(self):
        with _passing_patches():
            with patch.object(runtime.sys, "platform", "darwin"):
                self.assertEqual(runtime.maybe_systemd_scope_prefix(), [])

    def test_missing_systemd_run_fails_open(self):
        with _passing_patches():
            with patch("vibe.runtime.shutil.which", return_value=None):
                self.assertEqual(runtime.maybe_systemd_scope_prefix(), [])

    def test_no_user_manager_socket_fails_open(self):
        with _passing_patches():
            with patch("vibe.runtime.Path.is_socket", return_value=False):
                self.assertEqual(runtime.maybe_systemd_scope_prefix(), [])

    def test_no_cgroup_v2_fails_open(self):
        with _passing_patches():
            with patch("core.resource_governance.detect_cgroup_root", return_value=None):
                self.assertEqual(runtime.maybe_systemd_scope_prefix(), [])

    def test_governance_disabled_fails_open(self):
        with _passing_patches():
            with patch("vibe.runtime._resource_governance_mode", return_value="disabled"):
                self.assertEqual(runtime.maybe_systemd_scope_prefix(), [])

    def test_auto_mode_only_checks_linger(self):
        with _passing_patches():
            with patch("vibe.runtime._ensure_linger_enabled", return_value=True) as ensure_linger:
                runtime.maybe_systemd_scope_prefix()

        ensure_linger.assert_called_once_with(allow_enable=False)

    def test_enabled_mode_may_enable_linger(self):
        with _passing_patches():
            with patch("vibe.runtime._resource_governance_mode", return_value="enabled"):
                with patch("vibe.runtime._ensure_linger_enabled", return_value=True) as ensure_linger:
                    runtime.maybe_systemd_scope_prefix()

        ensure_linger.assert_called_once_with(allow_enable=True)

    def test_linger_unconfirmed_fails_open(self):
        with _passing_patches():
            with patch("vibe.runtime._ensure_linger_enabled", return_value=False):
                self.assertEqual(runtime.maybe_systemd_scope_prefix(), [])

    def test_self_test_failure_fails_open(self):
        with _passing_patches():
            with patch("vibe.runtime._systemd_run_self_test_ok", return_value=False):
                self.assertEqual(runtime.maybe_systemd_scope_prefix(), [])


class LingerHelperTests(unittest.TestCase):
    def test_already_enabled_short_circuits_without_enable(self):
        with patch("vibe.runtime._linger_is_enabled", return_value=True) as is_enabled:
            with patch("vibe.runtime.subprocess.run") as run:
                self.assertTrue(runtime._ensure_linger_enabled(allow_enable=False))
                run.assert_not_called()
        is_enabled.assert_called_once()

    def test_auto_mode_does_not_enable_when_initially_off(self):
        with patch("vibe.runtime._linger_is_enabled", return_value=False):
            with patch("vibe.runtime.subprocess.run") as run:
                self.assertFalse(runtime._ensure_linger_enabled(allow_enable=False))
                run.assert_not_called()

    def test_explicit_mode_self_enables_when_initially_off(self):
        # first check False -> enable-linger -> re-check True
        with patch("vibe.runtime._linger_is_enabled", side_effect=[False, True]):
            with patch("vibe.runtime.subprocess.run") as run:
                self.assertTrue(runtime._ensure_linger_enabled(allow_enable=True))
                run.assert_called_once()
                self.assertIn("enable-linger", run.call_args.args[0])

    def test_fails_open_when_enable_does_not_take(self):
        with patch("vibe.runtime._linger_is_enabled", side_effect=[False, False]):
            with patch("vibe.runtime.subprocess.run"):
                self.assertFalse(runtime._ensure_linger_enabled(allow_enable=True))


class ResourceGovernanceModeTests(unittest.TestCase):
    def test_broken_config_defaults_to_auto(self):
        with patch("config.v2_config.V2Config.load", side_effect=RuntimeError("boom")):
            self.assertEqual(runtime._resource_governance_mode(), "auto")


class StartServiceUsesScopePrefixTests(unittest.TestCase):
    def test_start_service_prepends_scope_prefix_to_argv(self):
        marker = ["systemd-run", "--user", "--scope", "-q", "-p", "Delegate=yes", "--"]
        captured = {}

        def fake_spawn(args, *a, **kw):
            captured["args"] = args
            raise _StopSpawn()

        with ExitStack() as stack:
            stack.enter_context(patch("vibe.runtime.extra_service_process_pids", return_value=[]))
            stack.enter_context(
                patch("vibe.runtime.service_instance_lock_available", return_value=(True, 0))
            )
            stack.enter_context(patch("vibe.runtime.paths.get_runtime_pid_path", return_value=Path("/nonexistent/x.pid")))
            stack.enter_context(patch("vibe.runtime.maybe_systemd_scope_prefix", return_value=marker))
            stack.enter_context(patch("vibe.runtime.spawn_service_background_process", side_effect=fake_spawn))
            stack.enter_context(
                patch("storage.migrations.guard_source_checkout_default_state_bootstrap", create=True)
            )
            with self.assertRaises(_StopSpawn):
                runtime.start_service(wait_for_ready=False, initial_ready_timeout=0)

        self.assertEqual(captured["args"][: len(marker)], marker)
        self.assertEqual(captured["args"][len(marker):], [sys.executable, str(runtime.get_service_main_path())])


class AdoptScopedServiceOwnerTests(unittest.TestCase):
    """The scope-gated fallback that neutralizes the systemd-run pid concern."""

    def test_adopts_live_lock_holder_when_it_differs_from_shim(self):
        proc = object()
        with ExitStack() as stack:
            stack.enter_context(patch("vibe.runtime.resolve_service_owner_pid", return_value=4321))
            stack.enter_context(patch("vibe.runtime.pid_alive", return_value=True))
            rec = stack.enter_context(patch("vibe.runtime._record_service_pid_reservation"))
            reap = stack.enter_context(
                patch(
                    "vibe.runtime._reap_service_start_process",
                    side_effect=lambda pid: runtime._SERVICE_START_PROCESSES.pop(pid, None),
                )
            )
            stack.enter_context(patch.dict("vibe.runtime._SERVICE_START_PROCESSES", {1234: proc}, clear=True))
            self.assertEqual(runtime._adopt_scoped_service_owner(1234), 4321)
            rec.assert_called_once_with(4321)
            reap.assert_called_once_with(1234)
            self.assertNotIn(1234, runtime._SERVICE_START_PROCESSES)
            self.assertNotIn(4321, runtime._SERVICE_START_PROCESSES)

    def test_no_adoption_when_owner_matches_shim_pid(self):
        # The normal exec() case: Popen.pid already IS the lock holder.
        with patch("vibe.runtime.resolve_service_owner_pid", return_value=1234):
            with patch("vibe.runtime.pid_alive", return_value=True):
                with patch("vibe.runtime._record_service_pid_reservation") as rec:
                    self.assertIsNone(runtime._adopt_scoped_service_owner(1234))
                    rec.assert_not_called()

    def test_no_adoption_when_no_live_owner_yet(self):
        with patch("vibe.runtime.resolve_service_owner_pid", return_value=None):
            with patch("vibe.runtime._record_service_pid_reservation") as rec:
                self.assertIsNone(runtime._adopt_scoped_service_owner(1234))
                rec.assert_not_called()

    def test_no_adoption_when_owner_not_alive(self):
        with patch("vibe.runtime.resolve_service_owner_pid", return_value=4321):
            with patch("vibe.runtime.pid_alive", return_value=False):
                self.assertIsNone(runtime._adopt_scoped_service_owner(1234))


class ReapServiceStartProcessTests(unittest.TestCase):
    def test_reaps_tracked_process_in_daemon_thread(self):
        process = Mock()
        thread = Mock()
        with patch.dict(runtime._SERVICE_START_PROCESSES, {1234: process}, clear=True):
            with patch("vibe.runtime.threading.Thread", return_value=thread) as thread_factory:
                runtime._reap_service_start_process(1234)

        self.assertNotIn(1234, runtime._SERVICE_START_PROCESSES)
        thread_factory.assert_called_once()
        self.assertTrue(thread_factory.call_args.kwargs["daemon"])
        thread_factory.call_args.kwargs["target"]()
        process.wait.assert_called_once_with()
        thread.start.assert_called_once_with()


class WaitForScopedServicePidTests(unittest.TestCase):
    """Poll-and-adopt loop that resolves the authoritative lock-holder pid."""

    def test_returns_spawn_pid_when_recorded_immediately(self):
        with patch("vibe.runtime.service_pid_recorded", return_value=True):
            with patch.dict("vibe.runtime._SERVICE_START_PROCESSES", {111: object()}, clear=True):
                self.assertEqual(runtime._wait_for_scoped_service_pid(111, 5.0), 111)
                self.assertNotIn(111, runtime._SERVICE_START_PROCESSES)

    def test_adopts_lock_holder_then_returns_it(self):
        with ExitStack() as stack:
            # spawn pid never recorded; adopted owner is recorded on the next tick.
            stack.enter_context(patch("vibe.runtime.service_pid_recorded", side_effect=[False, True]))
            stack.enter_context(patch("vibe.runtime._adopt_scoped_service_owner", return_value=222))
            stack.enter_context(patch("vibe.runtime.time.sleep"))
            self.assertEqual(runtime._wait_for_scoped_service_pid(111, 5.0), 222)

    def test_returns_none_on_timeout(self):
        times = iter([0.0, 0.0, 10.0])  # deadline=5.0; third tick is past it
        with ExitStack() as stack:
            stack.enter_context(patch("vibe.runtime.service_pid_recorded", return_value=False))
            stack.enter_context(patch("vibe.runtime._adopt_scoped_service_owner", return_value=None))
            stack.enter_context(patch("vibe.runtime._service_start_exit_code", return_value=None))
            stack.enter_context(patch("vibe.runtime.pid_alive", return_value=True))
            stack.enter_context(patch("vibe.runtime.resolve_service_owner_pid", return_value=None))
            stack.enter_context(patch("vibe.runtime.time.sleep"))
            stack.enter_context(patch("vibe.runtime.time.monotonic", side_effect=lambda: next(times)))
            self.assertIsNone(runtime._wait_for_scoped_service_pid(111, 5.0))


class StartScopedServiceResultTests(unittest.TestCase):
    def test_returns_ready_pid_from_first_phase(self):
        with patch("vibe.runtime._wait_for_scoped_service_pid", return_value=333):
            self.assertEqual(
                runtime._start_scoped_service_result(333, initial_ready_timeout=5.0, wait_for_ready=True),
                333,
            )

    def test_fast_return_adopts_owner_when_not_waiting(self):
        with ExitStack() as stack:
            stack.enter_context(patch("vibe.runtime._wait_for_scoped_service_pid", return_value=None))
            stack.enter_context(patch("vibe.runtime._service_start_exit_code", return_value=None))
            stack.enter_context(patch("vibe.runtime.resolve_service_owner_pid", return_value=444))
            stack.enter_context(patch("vibe.runtime.pid_alive", return_value=True))
            self.assertEqual(
                runtime._start_scoped_service_result(111, initial_ready_timeout=0, wait_for_ready=False),
                444,
            )

    def test_raises_when_spawn_exits_with_no_owner(self):
        with ExitStack() as stack:
            stack.enter_context(patch("vibe.runtime._wait_for_scoped_service_pid", return_value=None))
            stack.enter_context(patch("vibe.runtime._service_start_exit_code", return_value=1))
            stack.enter_context(patch("vibe.runtime.resolve_service_owner_pid", return_value=None))
            with self.assertRaises(RuntimeError):
                runtime._start_scoped_service_result(111, initial_ready_timeout=5.0, wait_for_ready=True)


class WaitForServiceReadyTests(unittest.TestCase):
    """Public wait that resolves the authoritative owner for any caller."""

    def test_returns_resolved_owner_when_spawn_pid_is_a_wrapper(self):
        # spawn pid never records itself; the lock holder is a different pid.
        with ExitStack() as stack:
            stack.enter_context(patch("vibe.runtime.service_pid_recorded", side_effect=[False, True]))
            stack.enter_context(patch("vibe.runtime._adopt_scoped_service_owner", return_value=999))
            stack.enter_context(patch("vibe.runtime.time.sleep"))
            self.assertEqual(runtime.wait_for_service_ready(111, 5.0), 999)

    def test_returns_none_when_no_owner_appears(self):
        times = iter([0.0, 0.0, 10.0])
        with ExitStack() as stack:
            stack.enter_context(patch("vibe.runtime.service_pid_recorded", return_value=False))
            stack.enter_context(patch("vibe.runtime._adopt_scoped_service_owner", return_value=None))
            stack.enter_context(patch("vibe.runtime._service_start_exit_code", return_value=None))
            stack.enter_context(patch("vibe.runtime.pid_alive", return_value=True))
            stack.enter_context(patch("vibe.runtime.resolve_service_owner_pid", return_value=None))
            stack.enter_context(patch("vibe.runtime.time.sleep"))
            stack.enter_context(patch("vibe.runtime.time.monotonic", side_effect=lambda: next(times)))
            self.assertIsNone(runtime.wait_for_service_ready(111, 5.0))


class _StopSpawn(Exception):
    pass


if __name__ == "__main__":
    unittest.main()
