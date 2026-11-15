"""Hermetic assertion tests only; these are NOT native Windows evidence."""
from __future__ import annotations

import copy
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest


WORKER = Path(__file__).with_name("worker.py")
MODULE = runpy.run_path(str(WORKER))


class RefusalAssertionsTest(unittest.TestCase):
    def setUp(self):
        self.result = {
            "isError": True,
            "structuredContent": {
                "code": "protected_window",
                "params": {"reason": "administrator"},
                "message": MODULE["EXPECTED_MESSAGE"],
                "detail": MODULE["EXPECTED_MESSAGE"],
            },
        }

    def test_exact_raw_and_folded_contract(self):
        MODULE["check_refusal"](self.result, folded=True)
        del self.result["structuredContent"]["detail"]
        MODULE["check_refusal"](self.result, folded=False)

    def test_generic_lost_wrong_or_invented_fields_fail(self):
        cases = [
            ("code", "tool_invocation_failed"),
            ("code", "upstream_tool_error"),
            ("params", {}),
            ("params", {"reason": "higher_integrity"}),
            ("params", {"reason": "administrator", "invented": True}),
            ("message", "Controller error: protected_window"),
            ("message", None),
            ("detail", "wrong bounded detail"),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                result = copy.deepcopy(self.result)
                result["structuredContent"][key] = value
                with self.assertRaises(AssertionError):
                    MODULE["check_refusal"](result, folded=True)
        self.result["isError"] = False
        with self.assertRaises(AssertionError):
            MODULE["check_refusal"](self.result, folded=False)

    def test_cleanup_removes_private_state_before_parent_fixture_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            state = work / "state"
            state.mkdir()
            (state / "computer-use.json").write_text("{}", encoding="utf-8")
            MODULE["remove_owned_state"](work)
            self.assertFalse(state.exists())

    @unittest.skipIf(sys.platform == "win32", "host-guard probe is for non-Windows hosts")
    def test_non_windows_entrypoint_refuses_before_import_or_side_effect(self):
        # Nonexistent paths prove no fixture/dependency imports or file creation
        # precede the host guard. Never launch a driver, editor or SDK locally.
        result = subprocess.run(
            [sys.executable, "-I", str(WORKER), "--repo", "missing-repo",
             "--input", "missing-input", "--work", "missing-work"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires disposable hosted Windows", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)


if __name__ == "__main__":
    unittest.main()
