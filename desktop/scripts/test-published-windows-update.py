"""One disposable hosted Windows published RC17 -> RC19 update observation.

No app build/adapters, path environment overrides, config writes, credentials,
or update endpoint changes. The installed shell and its helper own the update.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import urllib.request

ROOT = Path(os.environ["RUNNER_TEMP"]) / "published-windows-update"
EVIDENCE = ROOT / "evidence"
OLD = "3.2.6-rc.17"
NEW = "3.2.6-rc.19"
RELEASE = "https://github.com/avibe-bot/avibe/releases/download/gh-v3.2.6rc17/"
ASSET = f"Avibe_{OLD}_x86_64-pc-windows-msvc.exe"


def save(name, data):
    (EVIDENCE / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def ps(script):
    return subprocess.check_output(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        text=True, encoding="utf-8", errors="replace", timeout=180,
    ).strip()


def wait_for(callback, seconds, label):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = callback()
        if result:
            return result
        time.sleep(0.25)
    raise TimeoutError(label)


def capture_requests(stop):
    """Copy only the app-owned request/helper/status evidence, never state trees."""
    roots = {Path(os.environ[key]) for key in ("TMP", "TEMP") if os.environ.get(key)}
    roots.add(Path(ps("[IO.Path]::GetTempPath()")))
    while not stop.is_set():
        for root in roots:
            for directory in root.glob("avibe-update-*"):
                if not directory.is_dir():
                    continue
                for name in ("request.json", "install.ps1", "error.txt", "restore-error.txt", "ready", "requires-host"):
                    source = directory / name
                    try:
                        if source.is_file():
                            target = EVIDENCE / "handoff" / directory.name
                            target.mkdir(parents=True, exist_ok=True)
                            data = source.read_bytes()
                            if name == "request.json":
                                json.loads(data.decode("utf-8-sig"))
                            temporary = target / (name + ".tmp")
                            temporary.write_bytes(data)
                            temporary.replace(target / name)
                    except (OSError, UnicodeError, ValueError):
                        pass
        stop.wait(0.02)


def exact_offer(text):
    current = re.findall(r"(?m)^Current version: ([^\r\n]+)\r?$", text)
    latest = re.findall(r"(?m)^Latest version: ([^\r\n]+)\r?$", text)
    return bool(current and latest) and set(current) == {OLD} and set(latest) == {NEW}


def main():
    if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Disposable hosted Windows only")
    if os.environ.get("GITHUB_REPOSITORY") != "avibe-bot/avibe":
        raise RuntimeError("Repository mismatch")
    if any(os.environ.get(key) for key in ("GH_TOKEN", "GITHUB_TOKEN", "AVIBE_HOME")):
        raise RuntimeError("Native process must not inherit credentials or fixture home override")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    save("environment.json", {key: os.environ.get(key) for key in (
        "TMP", "TEMP", "RUNNER_TEMP", "LOCALAPPDATA", "APPDATA", "USERPROFILE",
    )} | {"cwd": str(Path.cwd()), "GetTempPath": ps("[IO.Path]::GetTempPath()")})
    from pywinauto import Application, Desktop
    import psutil

    if any(p.name().lower() == "avibe.exe" for p in psutil.process_iter()):
        raise RuntimeError("Runner already has Avibe; refusing to touch existing installation")
    installer = ROOT / ASSET
    urllib.request.urlretrieve(RELEASE + ASSET, installer)
    # Fixed public asset, fixed local destination; no arbitrary command input.
    os.environ["ACCEPTANCE_PUBLIC_INSTALLER"] = str(installer)
    signature = json.loads(ps("Get-AuthenticodeSignature -LiteralPath $env:ACCEPTANCE_PUBLIC_INSTALLER | Select-Object Status,StatusMessage,@{n='Signer';e={$_.SignerCertificate.Subject}} | ConvertTo-Json"))
    installer_sha = hashlib.sha256(installer.read_bytes()).hexdigest()
    if installer_sha != "c1829e076592d4b0425f3819f5bfc060696a36b71f3eb70c17db6db5c93da5b4":
        raise RuntimeError("RC17 installer differs from the published signed manifest's pinned SHA256")
    save("installer.json", {"url": RELEASE + ASSET, "sha256": installer_sha, "signature": signature})
    if signature["Status"] != 0:
        raise RuntimeError("Published RC17 installer Authenticode is not Valid")
    subprocess.run([str(installer), "/S"], check=True, timeout=240)
    # Use the installed Start-menu shortcut, not a copied/canonicalized binary.
    shortcuts = list((Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs").rglob("Avibe.lnk"))
    if len(shortcuts) != 1:
        raise RuntimeError(f"Expected one per-user installed shortcut, got {len(shortcuts)}")
    shortcut = shortcuts[0]
    os.environ["ACCEPTANCE_INSTALLED_SHORTCUT"] = str(shortcut)
    target = ps("(New-Object -ComObject WScript.Shell).CreateShortcut($env:ACCEPTANCE_INSTALLED_SHORTCUT).TargetPath")
    executable = Path(target)
    if not executable.is_relative_to(Path(os.environ["LOCALAPPDATA"])):
        raise RuntimeError("Installation is not in the default per-user local app directory")
    os.environ["ACCEPTANCE_INSTALLED_EXE"] = target
    version = lambda: ps("[Diagnostics.FileVersionInfo]::GetVersionInfo($env:ACCEPTANCE_INSTALLED_EXE).ProductVersion")
    if version() != OLD:
        raise RuntimeError("Installed source version mismatch")
    save("launch.json", {"shortcut": str(shortcut), "target": target, "source_version": version(), "source_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()})
    stop = threading.Event()
    collector = threading.Thread(target=capture_requests, args=(stop,), daemon=True)
    collector.start()
    try:
        ps("Start-Process -FilePath $env:ACCEPTANCE_INSTALLED_SHORTCUT")
        def app_process():
            for proc in psutil.process_iter(["pid", "exe"]):
                try:
                    if proc.info["exe"] and os.path.samefile(proc.info["exe"], executable):
                        return proc
                except (OSError, psutil.Error):
                    pass
            return None
        original = wait_for(app_process, 180, "Installed application did not launch")
        app = Application(backend="win32").connect(process=original.pid)
        def menu_window():
            return next((window for window in app.windows() if window.menu()), None)
        window = wait_for(menu_window, 120, "No installed application menu")
        window.capture_as_image().save(str(EVIDENCE / "installed-window.png"))
        # Native menu is the shipped user-facing channel control. It checks
        # immediately and presents the native offer on the fresh account view.
        window.menu_select("File->Get Pre-release Updates")
        desktop = Desktop(backend="uia")
        def offer():
            for dialog in desktop.windows(process=original.pid):
                texts = [control.window_text() for control in dialog.descendants()]
                joined = "\n".join(texts)
                if exact_offer(joined):
                    buttons = [control for control in dialog.descendants(control_type="Button") if control.window_text() == "Download and Install"]
                    if len(buttons) == 1:
                        return dialog, buttons[0], joined
            return None
        dialog, button, text = wait_for(offer, 180, "Exact RC17 to RC19 native offer not found; no installation authorized")
        save("offer.json", {"text": text, "pid": original.pid, "expected_target": NEW})
        dialog.capture_as_image().save(str(EVIDENCE / "update-offer.png"))
        button.click_input()
        def replacement():
            current = app_process()
            return current if current and current.pid != original.pid and version() == NEW else None
        relaunched = wait_for(replacement, 600, "RC19 replacement and relaunch not observed")
        new_app = Application(backend="win32").connect(process=relaunched.pid)
        new_window = wait_for(lambda: next((item for item in new_app.windows() if item.is_visible()), None), 90, "Relaunched app did not show its window")
        new_window.capture_as_image().save(str(EVIDENCE / "relaunched-window.png"))
        stop.set()
        collector.join(timeout=10)
        if collector.is_alive():
            raise RuntimeError("Evidence collector did not stop before result verification")
        requests = list((EVIDENCE / "handoff").glob("*/request.json"))
        if len(requests) != 1:
            raise RuntimeError("Replacement observed but exact helper request was not preserved")
        request = json.loads(requests[0].read_text(encoding="utf-8-sig"))
        if request["version"] != NEW or request["pid"] != original.pid:
            raise RuntimeError("Helper request does not bind the observed installed source and target")
        if not os.path.samefile(request["executable"], executable):
            raise RuntimeError("Helper request executable differs from installed shortcut target")
        save("result.json", {"ok": True, "old_pid": original.pid, "new_pid": relaunched.pid, "new_version": version(), "executable": target, "new_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(), "request_paths": {key: request[key] for key in ("directory", "executable", "installer", "staging")}})
    finally:
        stop.set()
        collector.join(timeout=10)
        # App-generated public updater status only. No config/db/credential copy.
        for root in (Path(os.environ["APPDATA"]), Path(os.environ["LOCALAPPDATA"])):
            for status in (root / "bot.avibe.desktop").glob("desktop-update*.json"):
                if "request" not in status.name:
                    shutil.copyfile(status, EVIDENCE / (root.name + "-" + status.name))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        save("failure.json", {"error_type": type(error).__name__, "message": str(error)})
        raise
