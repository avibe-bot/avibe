"""Hosted-only native acceptance. No direct IPC calls for updater actions.

Positive automatic scenario uses ordinary real elapsed time: first offer ~30m,
final-edge activity rejection ~90m, later successful automatic retry ~150m.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from common import Processes, digest, hosted, runtime_environment, save, wait
from fixture_server import Fixture
from panel_dom import Panel


def lines(path: Path) -> list[dict]:
    result = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                result.append(json.loads(line))
            except ValueError:
                pass  # An incomplete concurrent append is read again next poll.
    return result


class Case:
    def __init__(self, source: Path, root: Path, build: dict, name: str):
        self.source, self.build, self.name = source, build, name
        self.root = root / "cases" / name
        self.root.mkdir(parents=True, exist_ok=False)
        self.evidence = root / "evidence" / name
        self.evidence.mkdir(parents=True)
        self.processes = Processes()
        self.fixture = None
        self.native_pid = None
        self.handles = []
        self.started = time.monotonic()
        self.env = runtime_environment(self.root)
        self.env["PYTHONPATH"] = str(source)
        self.installation = self.root / "installation"
        self.installation.mkdir()
        original = Path(build["outputs"]["base"])
        if os.name == "nt":
            self.binary = self.installation / "avibe-desktop.exe"
            shutil.copy2(original, self.binary)
        else:
            shutil.copytree(original, self.installation / "Avibe.app")
            self.binary = self.installation / "Avibe.app/Contents/MacOS/avibe-desktop"
        assert digest(self.binary) == build["binary_sha256"]["base"]
        self.env["AVIBE_NATIVE_ACCEPTANCE_INSTALLATION"] = str(self.installation)
        self.env["AVIBE_NATIVE_ACCEPTANCE_CONFIG"] = str(self.root / "config.json")
        self.panel = Panel(self, source)

    def spawn(self, argv, log_name: str):
        log = (self.evidence / log_name).open("w", encoding="utf-8")
        self.handles.append(log)
        child = subprocess.Popen([str(a) for a in argv], cwd=self.root, env=self.env, stdout=log, stderr=subprocess.STDOUT)
        self.processes.add(child.pid)
        return child

    def wait(self, predicate, timeout: float, description: str):
        return wait(predicate, timeout, description, self.processes)

    def events(self):
        return lines(self.root / "events.jsonl")

    def start(self):
        runtime = self.spawn([sys.executable, Path(__file__).with_name("runtime.py"), "--root", self.root], "runtime.log")

        def runtime_ready():
            if runtime.poll() is not None:
                raise RuntimeError("Real controller cold-start failed; see runtime.log")
            path = self.root / "runtime-ready.json"
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

        producer = self.wait(runtime_ready, 90, "real idle producer did not become ready")
        self.fixture = Fixture(self.root, self.build)
        save(self.root / "config.json", {"root": str(self.root), "run_id": os.environ["GITHUB_RUN_ID"], "local": producer["local"],
                                       "proxy": self.fixture.proxy, "certificate": str(self.fixture.certificate)})
        self.native_started = time.monotonic()
        self.native = self.spawn([self.binary], "native.log")
        self.native_pid = self.native.pid
        initial = self.wait(lambda: next((event for event in self.events() if event["event"] == "native_ready"), None), 30, "native GUI startup failed")
        assert initial["version"] == self.build["base_version"]
        assert initial["source_sha"] == self.build["source_sha"]
        # Rust canonicalize emits Windows verbatim paths (\\\\?\\...), while
        # Python retains the input's prefix style. Compare filesystem identity,
        # not those different spellings, and fail if either path is absent.
        assert Path(initial["data"]).samefile(self.root / "native-state")
        self.wait(lambda: any(event["event"] == "loaded" and "account/index.html?view=updates" in event.get("url", "") for event in self.events()),
                  15, "native trusted panel did not load")
        snapshot = self.wait(self.panel.snapshot, 10, "actual rendered panel missing")
        expected_origin = "http://tauri.localhost" if os.name == "nt" else "tauri://localhost"
        assert snapshot["origin"] == expected_origin and snapshot["bridge"]
        assert snapshot["width"] > 100 and snapshot["height"] > 100
        boundary = len(self.events())
        self.send(action="state")
        native_state = self.wait(lambda: next((event for event in self.events()[boundary:] if event["event"] == "state"), None),
                                 10, "native window visibility unavailable")
        assert native_state["visible"], "native panel window is not actually visible"
        session_id = None
        if os.name == "nt":
            import ctypes
            value = ctypes.c_ulong()
            if not ctypes.windll.kernel32.ProcessIdToSessionId(self.native_pid, ctypes.byref(value)):
                raise RuntimeError("Windows runner session preflight failed")
            session_id = value.value  # Informational; DOM control is tested, not SendInput.
        assert self.panel.checked("autoUpdate") is False
        assert self.panel.checked("prereleases") is True
        save(self.evidence / "gui-preflight.json", {"native": initial, "panel": snapshot, "visible": native_state["visible"],
                                                   "windows_session_id": session_id, "elapsed": time.monotonic() - self.native_started})
        (self.root / "native-state/user-sentinel").write_text("preserve fixture state\n", encoding="utf-8")

    def send(self, **command):
        with (self.root / "commands.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": self.native_pid, **command}) + "\n")

    def js(self, script: str):
        request_id = uuid.uuid4().hex
        wrapper = ("Promise.resolve().then(async()=>{" + script + "}).then(result=>({id:"
                   + json.dumps(request_id) + ",result})).catch(error=>({id:" + json.dumps(request_id)
                   + ",error:String(error)})).then(value=>{window.location='avibe-acceptance-result://result?value='+encodeURIComponent(JSON.stringify(value));});")
        self.send(action="eval", script=wrapper)
        result = self.wait(lambda: next((event["value"] for event in self.events() if event["event"] == "javascript"
                                        and event["value"].get("id") == request_id), None), 10, "WebView evaluation did not respond")
        if "error" in result:
            raise RuntimeError(result["error"])
        return result.get("result")

    def phase(self, phase: str, after: int = 0):
        return next((event for event in self.events()[after:] if event["event"] == "update" and event["state"]["phase"] == phase), None)

    def command_events(self, action: str):
        return [event for event in self.events() if event["event"] == "panel_command" and event["action"]["type"] == action]

    def success(self, expected_auto: bool, timeout: float = 120):
        old_pid = self.native_pid
        target = self.wait(lambda: next((event for event in self.events() if event["event"] == "native_ready"
                                        and event["version"] == self.build["next_version"] and event["pid"] != old_pid), None),
                           timeout, "no target-version native relaunch")
        assert target["source_sha"] == self.build["source_sha"]
        assert Path(target["executable"]).samefile(self.binary)
        self.native_pid = target["pid"]
        self.processes.add(self.native_pid)
        self.wait(lambda: self.native.poll() is not None, 20, "original native process did not exit")
        assert digest(self.binary) == self.build["binary_sha256"]["next"]
        assert (self.root / "native-state/user-sentinel").read_text(encoding="utf-8") == "preserve fixture state\n"
        self.wait(lambda: any(event["event"] == "loaded" and event["pid"] == self.native_pid
                              and "view=updates" in event.get("url", "") for event in self.events()), 30, "target panel did not load")
        assert self.wait(lambda: self.panel.snapshot(), 10, "target WebView is unusable")
        assert self.panel.checked("autoUpdate") is expected_auto
        assert not self.fixture.denied, self.fixture.denied
        save(self.evidence / "handoff.json", {"old_pid": old_pid, "new": target,
                                              "target_binary_sha256": digest(self.binary), "auto_update": expected_auto})

    def close(self):
        if self.native_pid:
            self.send(action="stop")
        self.processes.close()
        if self.fixture:
            self.fixture.close()
        for name in ("events.jsonl", "runtime-ready.json", "runtime-events.jsonl", "idle.jsonl",
                     "updater-http.jsonl", "updater-denied.json"):
            path = self.root / name
            if path.exists():
                shutil.copy2(path, self.evidence / name)
        for stream in self.handles:
            stream.close()


def explicit(case: Case):
    case.start()
    case.panel.switch("prereleases", False)
    case.wait(lambda: case.panel.snapshot()["status"] == "current", 30, "stable channel did not settle")
    case.panel.switch("prereleases", True)
    case.wait(lambda: case.panel.snapshot()["status"] == "available", 30, "test release not offered")
    before = len(case.events())
    case.panel.click("check")
    case.wait(lambda: case.phase("available", before), 30, "panel Check did not complete")
    time.sleep(2)
    assert case.fixture.payload_count() == 0
    assert len(case.command_events("update_check")) == 1
    assert len(case.command_events("update_set_prereleases")) == 2
    assert not case.command_events("update_install")
    case.panel.click("install")
    case.success(False)
    assert len(case.command_events("update_install")) == 1
    assert case.fixture.payload_count() == 1


def automatic(case: Case):
    case.start()
    case.panel.switch("autoUpdate", True)
    if time.monotonic() - case.native_started >= 55:
        raise RuntimeError("UI preflight too slow to enable policy before first normal timer poll")
    assert json.loads((case.root / "native-state/update-auto.json").read_text(encoding="utf-8"))["enabled"] is True
    assert not case.command_events("update_check") and not case.command_events("update_install")
    case.wait(lambda: len(case.fixture.indexes()) >= 1, 40 * 60, "first automatic discovery never happened")
    case.wait(lambda: case.phase("available"), 30, "first automatic offer absent")
    first = case.fixture.indexes()[0]
    assert first["monotonic"] - case.native_started >= 29 * 60
    assert case.fixture.payload_count() == 0, "first offer must not install"
    (case.root / "gate-arm").write_text("one final-edge rejection", encoding="utf-8")
    edge = case.wait(lambda: next((event for event in case.events() if event["event"] == "before_commit"), None),
                     70 * 60, "second automatic check did not reach the real installer's final callback")
    assert len(case.fixture.indexes()) >= 2 and case.fixture.payload_count() == 1
    assert case.fixture.indexes()[1]["monotonic"] - first["monotonic"] >= 59 * 60
    assert case.native.poll() is None and digest(case.binary) == case.build["binary_sha256"]["base"]
    ticket = uuid.uuid4().hex
    with (case.root / "runtime-commands.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"action": "activity", "id": ticket}) + "\n")
    observation = case.wait(lambda: next((event for event in lines(case.root / "runtime-events.jsonl")
                                         if event.get("id") == ticket), None), 10, "actual controller did not record activity")
    assert observation["idle"]["idle_seconds"] < 5
    boundary = len(case.events())
    (case.root / "gate-release").write_text("recheck actual policy now", encoding="utf-8")
    case.wait(lambda: case.phase("available", boundary), 30, "activity did not block final installer commit")
    assert case.native.poll() is None and digest(case.binary) == case.build["binary_sha256"]["base"]
    assert not any(event["event"] == "native_ready" and event["version"] == case.build["next_version"] for event in case.events())
    save(case.evidence / "final-edge-rejection.json", {"edge": edge, "activity": observation, "old_binary_retained": True})
    # No direct tick/check, no synthesized idle duration, no Install click.
    # The real one-hour timer retries after a fresh quiet period.
    case.success(True, timeout=75 * 60)
    assert len(case.fixture.indexes()) >= 3 and case.fixture.payload_count() == 2
    assert not case.command_events("update_install") and not case.command_events("update_check")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = hosted(args.root)
    build = json.loads((root / "build.json").read_text(encoding="utf-8"))
    source = args.source.resolve()
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip() == build["source_sha"]
    results = []
    try:
        for name, run in (("explicit", explicit), ("automatic", automatic)):
            case = Case(source, root, build, name)
            try:
                run(case)
                results.append({"scenario": name, "ok": True, "elapsed_seconds": time.monotonic() - case.started})
            except Exception as error:
                results.append({"scenario": name, "ok": False, "error": str(error)})
                raise
            finally:
                case.close()
    finally:
        save(root / "evidence/acceptance.json", {"source_sha": build["source_sha"], "target": build["target"],
                                                "ok": len(results) == 2 and all(item["ok"] for item in results), "results": results})


if __name__ == "__main__":
    main()
