"""Disposable Windows GUI spike: no product changes or production credentials."""
from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import zipfile

ROOT = Path(os.environ.get("RUNNER_TEMP", ".")) / "computer-use-spike"
OUT = ROOT / "evidence"
VERSIONS = {
    "0.31.0": "461c7d4acd12685ab777fa4a3800e25f75d2f06e8a67ab7a1778701eb25e6728",
    "0.47.8": "19d9ffb1844b33a9a7ee5fbe9a3fac56be6740a0c947fd55eb08ee4b3f1f3866",
}
TEXT = "Avibe Windows spike: Hello, 中文输入成功。"


def save(name, value):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def scrub_images(value, prefix):
    if isinstance(value, dict):
        if value.get("type") == "image" and "data" in value:
            image = OUT / f"{prefix}-{time.time_ns()}.png"
            image.write_bytes(base64.b64decode(value["data"]))
            return {"type": "image", "file": image.name, "bytes": image.stat().st_size}
        return {k: scrub_images(v, prefix) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_images(v, prefix) for v in value]
    return value


class MCP:
    def __init__(self, executable, pipe, directory):
        self.number = 0
        self.directory = directory
        self.log = (OUT / f"{directory}-proxy.stderr.txt").open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(executable), "mcp", "--embedded", "--socket", pipe],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            text=True, encoding="utf-8", bufsize=1,
        )
        self.queue = queue.Queue()
        threading.Thread(target=self.read, daemon=True).start()
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "avibe-windows-spike", "version": "1"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.schemas = {x["name"]: x for x in self.request("tools/list", {}).get("tools", [])}
        save(f"{directory}/tools.json", self.schemas)

    def read(self):
        for line in self.proc.stdout:
            try:
                self.queue.put(json.loads(line))
            except ValueError:
                self.queue.put({"non_json": line[:1000]})
        self.queue.put({"eof": True})

    def send(self, message):
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params):
        self.number += 1
        number = self.number
        self.send({"jsonrpc": "2.0", "id": number, "method": method, "params": params})
        end = time.monotonic() + 60
        while time.monotonic() < end:
            reply = self.queue.get(timeout=max(0.1, end-time.monotonic()))
            if reply.get("eof"):
                raise RuntimeError("MCP proxy exited")
            if reply.get("id") == number:
                if "error" in reply:
                    raise RuntimeError(json.dumps(reply["error"]))
                return reply.get("result", {})
        raise TimeoutError(method)

    def call(self, name, arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        save(f"{self.directory}/{self.number:03}-{name}.json", {
            "arguments": arguments, "result": scrub_images(result, f"{self.directory}-{self.number}"),
        })
        return result

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.log.close()


def structured(result):
    value = result.get("structuredContent")
    if value is not None:
        return value
    for part in result.get("content", []):
        if part.get("type") == "text":
            try:
                return json.loads(part["text"])
            except ValueError:
                pass
    return {}


def runtime_probe():
    sys.path.insert(0, str(Path.cwd()))
    from core.computer_use import (
        _shell_lock_is_held, _unix_socket_accepts, effective_computer_use_status,
    )
    status = effective_computer_use_status(state_path=ROOT / "absent-state.json")
    facts = {"effective_status": {"status": status.status, "reason": status.reason},
             "shell_lock_is_held": _shell_lock_is_held(ROOT / "shell.lock"),
             "endpoint_accepts": _unix_socket_accepts(r"\\.\pipe\avibe-spike")}
    try:
        from core.computer_js_kernels import _kernel_platform_supported
        facts["js_kernel_platform_supported"] = _kernel_platform_supported()
    except Exception as error:
        facts["js_import_error"] = repr(error)
    try:
        from core.computer_server import ComputerUseServer
        async def probe():
            server = ComputerUseServer(state_path=ROOT / "absent-state.json")
            try:
                return await server.call_tool("list_apps", {"session": "spike"})
            finally:
                await server.close()
        facts["server_result"] = asyncio.run(probe())
    except Exception as error:
        facts["server_import_or_call_error"] = repr(error)
    save("avibe-runtime.json", facts)


def run_version(version, digest):
    import psutil
    from pywinauto import Application
    directory = ROOT / version
    directory.mkdir(parents=True, exist_ok=True)
    asset = f"cua-driver-rs-{version}-windows-x86_64-binary.zip"
    url = f"https://github.com/trycua/cua/releases/download/cua-driver-rs-v{version}/{asset}"
    archive = directory / asset
    urllib.request.urlretrieve(url, archive)
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != digest:
        raise RuntimeError("Driver archive hash mismatch")
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(directory)
    exe = next(directory.rglob("cua-driver.exe"))
    save(f"{version}/binary.json", {"url": url, "archive_sha256": actual,
        "binary_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
        "version": subprocess.check_output([str(exe), "--version"], text=True).strip(),
        "variant": "upstream-release-unpatched"})
    pipe = rf"\\.\pipe\avibe-spike-{os.getpid()}-{version}"
    env = os.environ.copy()
    env.update(CUA_DRIVER_EMBEDDED="1", CUA_DRIVER_RS_TELEMETRY_ENABLED="0", CUA_DRIVER_RS_UPDATE_CHECK="0")
    daemon_log = (OUT / f"{version}-daemon.txt").open("w", encoding="utf-8")
    daemon = subprocess.Popen([str(exe), "serve", "--embedded", "--socket", pipe],
                              stdin=subprocess.PIPE, stdout=daemon_log, stderr=daemon_log, env=env)
    client = None
    notepad_pid = None
    try:
        time.sleep(2)
        client = MCP(exe, pipe, version)
        client.call("health_report", {})
        client.call("start_session", {"session": "spike"})
        client.call("get_desktop_state", {"session": "spike"})
        launch = client.call("launch_app", {"name": "notepad", "session": "spike"})
        data = structured(launch)
        save(f"{version}/launch-structured.json", data)
        notepad_pid = data.get("pid")
        if not notepad_pid:
            raise RuntimeError("Driver launch_app returned no pid")
        deadline = time.monotonic() + 20
        window = None
        while time.monotonic() < deadline:
            windows = structured(client.call("list_windows", {"pid": notepad_pid, "session": "spike"}))
            candidates = windows if isinstance(windows, list) else windows.get("windows", [])
            if candidates:
                window = candidates[0]
                break
            time.sleep(0.5)
        if not window:
            raise RuntimeError("Driver list_windows found no Notepad window")
        wid = window.get("window_id", window.get("id", window.get("hwnd")))
        target = {"pid": notepad_pid, "window_id": wid, "session": "spike"}
        before = client.call("get_window_state", target)
        app = Application(backend="win32").connect(process=notepad_pid)
        native = app.window(handle=int(wid))
        elements = structured(before).get("elements", [])
        editors = [e for e in elements if any(word in str(e.get("role", "")).lower() for word in ("edit", "text"))]
        args = dict(target, text=TEXT, delivery_mode="background")
        if editors and editors[0].get("element_token"):
            args["element_token"] = editors[0]["element_token"]
        client.call("type_text", args)
        after = client.call("get_window_state", target)
        def read_text():
            return [c.window_text() for c in native.descendants() if "edit" in c.class_name().lower()]
        texts = read_text()
        if not any(TEXT in text for text in texts):
            # Re-observed first; retry only when independent native text proves absent.
            args["delivery_mode"] = "foreground"
            args.pop("element_token", None)
            client.call("type_text", args)
            after = client.call("get_window_state", target)
            texts = read_text()
        save(f"{version}/native-text-readback.json", {"expected": TEXT, "actual": texts})
        if not any(TEXT in text for text in texts):
            raise RuntimeError("Chinese text not independently visible in Notepad")
        elements = structured(after).get("elements", [])
        menus = [e for e in elements if str(e.get("label", e.get("name", ""))).replace("&", "").lower() == "file"]
        if menus and menus[0].get("element_token"):
            click = dict(target, element_token=menus[0]["element_token"])
        else:
            # Observe native menu geometry; delivery remains entirely Cua Driver.
            item = native.menu_item("File")
            rect = item.rectangle()
            origin = native.client_to_screen((0, 0))
            click = dict(target, x=(rect.left+rect.right)/2-origin[0], y=(rect.top+rect.bottom)/2-origin[1], delivery_mode="foreground")
            save(f"{version}/native-menu-geometry.json", {"rect": [rect.left, rect.top, rect.right, rect.bottom], "client_origin": origin})
        client.call("click", click)
        client.call("get_window_state", target)
        native.capture_as_image().save(str(OUT / f"{version}-notepad-final.png"))
        menu_hwnd = ctypes.windll.user32.FindWindowW("#32768", None)
        save(f"{version}/result.json", {"text_ok": True, "menu_popup_hwnd": menu_hwnd,
            "menu_popup_observed": bool(menu_hwnd), "notepad_pid": notepad_pid,
            "daemon_pid": daemon.pid, "driver_direct_embedded_proxy": True,
            "avibe_shell_hosted": False, "avibe_cli_end_to_end": False})
    finally:
        if client:
            client.close()
        daemon.stdin.close()
        try:
            daemon.wait(timeout=3)
        except subprocess.TimeoutExpired:
            daemon.terminate()
            daemon.wait(timeout=5)
        daemon_log.close()
        if notepad_pid:
            try:
                psutil.Process(notepad_pid).terminate()
            except psutil.Error:
                pass


def main():
    if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Disposable hosted Windows only")
    if any(os.environ.get(key) for key in ("GH_TOKEN", "GITHUB_TOKEN")):
        raise RuntimeError("GUI child must not inherit credentials")
    OUT.mkdir(parents=True, exist_ok=True)
    session_id = ctypes.c_ulong()
    ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id))
    save("environment.json", {"session_id": session_id.value, "is_admin": bool(ctypes.windll.shell32.IsUserAnAdmin()),
        "source_sha": os.environ.get("ACCEPTANCE_SOURCE_SHA"), "python": sys.version})
    if session_id.value == 0:
        raise RuntimeError("Runner is in Session 0; no usable interactive desktop")
    runtime_probe()
    outcomes = {}
    for version, digest in VERSIONS.items():
        try:
            run_version(version, digest)
            outcomes[version] = "completed"
        except Exception as error:
            outcomes[version] = repr(error)
            save(f"{version}/failure.json", {"error": repr(error), "traceback": traceback.format_exc()})
    save("summary.json", outcomes)
    print(json.dumps(outcomes))


if __name__ == "__main__":
    main()
