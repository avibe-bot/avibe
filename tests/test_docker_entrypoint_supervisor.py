import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO_ROOT / "scripts" / "docker-entrypoint.sh"


class DockerEntrypointSupervisorTests(unittest.TestCase):
    def test_full_mode_exits_when_service_process_dies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            fake_python = tmp_path / "python"
            fake_python.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import os
                    import sys
                    import time
                    from pathlib import Path

                    args = sys.argv[1:]
                    if args and args[0] == "main.py":
                        sys.exit(42)

                    if len(args) >= 2 and args[0] == "-c":
                        code = args[1]
                        if "get_runtime_dir" in code:
                            print(Path(os.environ["AVIBE_HOME"]) / "runtime")
                            sys.exit(0)
                        if "run_ui_server" in code:
                            time.sleep(30)
                        sys.exit(0)

                    sys.exit(0)
                    """
                ),
                encoding="utf-8",
            )
            fake_python.chmod(fake_python.stat().st_mode | stat.S_IEXEC)

            env = os.environ.copy()
            env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
            env["AVIBE_HOME"] = str(tmp_path / "home")

            result = subprocess.run(
                ["bash", str(ENTRYPOINT), "full"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
            self.assertIn("Service exited unexpectedly", result.stderr)

    def test_full_mode_uses_avibe_home_for_runtime_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            fake_python = tmp_path / "python"
            fake_python.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import os
                    import sys
                    import time
                    from pathlib import Path

                    args = sys.argv[1:]
                    avibe_runtime_dir = Path(os.environ["AVIBE_HOME"]) / "runtime"

                    if args and args[0] == "main.py":
                        time.sleep(30)
                        sys.exit(0)

                    if len(args) >= 2 and args[0] == "-c":
                        code = args[1]
                        if "get_runtime_dir" in code:
                            print(avibe_runtime_dir)
                            sys.exit(0)
                        if "run_ui_server" in code:
                            time.sleep(30)
                            sys.exit(0)
                        if "write_status" in code:
                            avibe_runtime_dir.mkdir(parents=True, exist_ok=True)
                            (avibe_runtime_dir / "status.json").write_text("{}", encoding="utf-8")
                            sys.exit(0)
                        if "stop_service" in code or "ensure_data_dirs" in code:
                            sys.exit(0)
                    sys.exit(0)
                    """
                ),
                encoding="utf-8",
            )
            fake_python.chmod(fake_python.stat().st_mode | stat.S_IEXEC)

            env = os.environ.copy()
            env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
            env["AVIBE_HOME"] = str(tmp_path / "avibe-home")
            env["VIBE_REMOTE_HOME"] = str(tmp_path / "legacy-home")

            proc = subprocess.Popen(
                ["bash", str(ENTRYPOINT), "full"],
                cwd=REPO_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                runtime_pid_path = Path(env["AVIBE_HOME"]) / "runtime" / "vibe.pid"
                for _ in range(50):
                    if runtime_pid_path.exists():
                        break
                    time.sleep(0.1)
                self.assertTrue(runtime_pid_path.exists())
                legacy_pid_path = Path(env["VIBE_REMOTE_HOME"]) / "runtime" / "vibe.pid"
                self.assertFalse(legacy_pid_path.exists())
            finally:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate(timeout=5)

    def test_full_mode_tracks_restarted_service_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            fake_python = tmp_path / "python"
            fake_python.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import os
                    import subprocess
                    import sys
                    import threading
                    import time
                    from pathlib import Path

                    args = sys.argv[1:]
                    runtime_dir = Path(os.environ["AVIBE_HOME"]) / "runtime"
                    runtime_dir.mkdir(parents=True, exist_ok=True)
                    real_python = os.environ["REAL_PYTHON"]

                    if args and args[0] == "main.py":
                        time.sleep(1)
                        sys.exit(42)

                    if len(args) >= 2 and args[0] == "-c":
                        code = args[1]
                        if "get_runtime_dir" in code:
                            print(runtime_dir)
                            sys.exit(0)
                        if "run_ui_server" in code:
                            def restart_service_later():
                                time.sleep(2)
                                proc = subprocess.Popen(
                                    [real_python, "-c", "import time; time.sleep(30)"],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    start_new_session=True,
                                )
                                (runtime_dir / "vibe.pid").write_text(str(proc.pid), encoding="utf-8")

                            threading.Thread(target=restart_service_later, daemon=True).start()
                            time.sleep(30)
                            sys.exit(0)
                        sys.exit(0)

                    sys.exit(0)
                    """
                ),
                encoding="utf-8",
            )
            fake_python.chmod(fake_python.stat().st_mode | stat.S_IEXEC)

            env = os.environ.copy()
            env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
            env["AVIBE_HOME"] = str(tmp_path / "home")
            env["REAL_PYTHON"] = sys.executable

            proc = subprocess.Popen(
                ["bash", str(ENTRYPOINT), "full"],
                cwd=REPO_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                time.sleep(4)
                self.assertIsNone(proc.poll())
            finally:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    _stdout, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    _stdout, stderr = proc.communicate(timeout=5)

            self.assertNotIn("Service exited unexpectedly", stderr)
            self.assertIn("Detected replacement service PID", stderr)

    def test_full_mode_keeps_ui_alive_when_service_is_stopped_intentionally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            fake_python = tmp_path / "python"
            fake_python.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys
                    import threading
                    import time
                    from pathlib import Path

                    args = sys.argv[1:]
                    runtime_dir = Path(os.environ["AVIBE_HOME"]) / "runtime"
                    runtime_dir.mkdir(parents=True, exist_ok=True)
                    status_path = runtime_dir / "status.json"

                    if args and args[0] == "main.py":
                        time.sleep(1)
                        sys.exit(0)

                    if args and args[0] == "-":
                        try:
                            payload = json.loads(Path(args[1]).read_text(encoding="utf-8"))
                        except Exception:
                            sys.exit(1)
                        state = payload.get("state")
                        if isinstance(state, str):
                            print(state)
                        sys.exit(0)

                    if len(args) >= 2 and args[0] == "-c":
                        code = args[1]
                        if "get_runtime_dir" in code:
                            print(runtime_dir)
                            sys.exit(0)
                        if "run_ui_server" in code:
                            def stop_service_later():
                                time.sleep(0.5)
                                status_path.write_text(json.dumps({"state": "stopped"}), encoding="utf-8")

                            threading.Thread(target=stop_service_later, daemon=True).start()
                            time.sleep(30)
                            sys.exit(0)
                        if "write_status" in code:
                            state = "running"
                            if "stopping" in code:
                                state = "stopping"
                            elif "stopped" in code:
                                state = "stopped"
                            elif "restarting" in code:
                                state = "restarting"
                            status_path.write_text(json.dumps({"state": state}), encoding="utf-8")
                            sys.exit(0)
                        sys.exit(0)

                    sys.exit(0)
                    """
                ),
                encoding="utf-8",
            )
            fake_python.chmod(fake_python.stat().st_mode | stat.S_IEXEC)

            env = os.environ.copy()
            env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
            env["AVIBE_HOME"] = str(tmp_path / "home")

            proc = subprocess.Popen(
                ["bash", str(ENTRYPOINT), "full"],
                cwd=REPO_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                time.sleep(4)
                self.assertIsNone(proc.poll())
            finally:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    _stdout, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    _stdout, stderr = proc.communicate(timeout=5)

            self.assertNotIn("Service exited unexpectedly", stderr)


if __name__ == "__main__":
    unittest.main()
