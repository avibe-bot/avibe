"""Disposable real signed rc2 -> rc3 update-panel observation.

The runner downloads only public GitHub Release assets, installs the untouched
signed rc2, operates the visible Desktop panel, and records allowlisted evidence.
It never builds, publishes, mutates release state, or uses owner-machine state.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request

import psutil

REPO = "avibe-bot/avibe"
ROOT = Path(os.environ.get("SMOKE_ROOT") or os.environ.get("ACCEPTANCE_ROOT") or (os.environ["RUNNER_TEMP"] + "/signed-rc-smoke"))
EVIDENCE = ROOT / "evidence"
SOURCE_TAG = os.environ.get("SOURCE_TAG", "gh-v3.2.7rc2")
TARGET_TAG = os.environ.get("TARGET_TAG", "gh-v3.2.7rc3")
SOURCE_VERSION = os.environ.get("SOURCE_VERSION", "3.2.7-rc.2")
TARGET_VERSION = os.environ.get("TARGET_VERSION", "3.2.7-rc.3")
SOURCE_SHORT = re.sub(r"-?(rc|beta|alpha)\.(\d+)", r"\1\2", SOURCE_VERSION)
TARGET_SHORT = re.sub(r"-?(rc|beta|alpha)\.(\d+)", r"\1\2", TARGET_VERSION)
ACTIONS = []
OWNED_CHILDREN = {}


def record(action: str, **details):
    ACTIONS.append({"time": time.time(), "action": action, **details})
    save("actions.json", ACTIONS)



def save(name: str, value) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(command, *, timeout=180, check=True, text=True, env=None):
    return subprocess.run(command, check=check, timeout=timeout, text=text, capture_output=True, env=env)


def wait_for(callback, seconds: float, label: str):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            value = callback()
            if value:
                return value
        except Exception as error:  # process turnover is expected
            last = f"{type(error).__name__}: {str(error)[:500]}"
            if isinstance(error, subprocess.CalledProcessError):
                last += f" stderr={str(error.stderr)[:1000]}"
        time.sleep(0.5)
    raise TimeoutError(f"{label}; last_error={last}")


def normalize_version(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", value).lower()


def release_url(tag: str, asset: str) -> str:
    return f"https://github.com/{REPO}/releases/download/{tag}/{asset}"


def download(tag: str, asset: str) -> Path:
    path = ROOT / "downloads" / tag / asset
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        urllib.request.urlretrieve(release_url(tag, asset), path)
    return path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sums_entry(sums: Path, asset: str) -> str:
    for line in sums.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 2 and Path(fields[-1]).name == asset:
            return fields[0].lower()
    raise RuntimeError(f"SHA256SUMS has no entry for {asset}")


def verify_public_assets(target: str) -> dict:
    if sys.platform == "darwin":
        installer_name = f"Avibe_{SOURCE_VERSION}_aarch64-apple-darwin.dmg"
        target_manifest_name = "desktop-update-aarch64-apple-darwin.json"
        target_sig_name = target_manifest_name + ".sig"
    elif os.name == "nt":
        installer_name = f"Avibe_{SOURCE_VERSION}_x86_64-pc-windows-msvc.exe"
        target_manifest_name = "desktop-update-x86_64-pc-windows-msvc.json"
        target_sig_name = target_manifest_name + ".sig"
    else:
        raise RuntimeError("This smoke supports macOS arm64 and Windows x64 only")
    source_sums_name = installer_name.rsplit(".", 1)[0] + ".SHA256SUMS"
    source = download(SOURCE_TAG, installer_name)
    sums = download(SOURCE_TAG, source_sums_name)
    expected = sums_entry(sums, installer_name)
    actual = sha256(source)
    if actual != expected:
        raise RuntimeError(f"source installer hash mismatch: {actual} != {expected}")
    source_meta = download(SOURCE_TAG, installer_name.rsplit(".", 1)[0] + ".SOURCE.json")
    target_manifest = download(TARGET_TAG, target_manifest_name)
    target_sig = download(TARGET_TAG, target_sig_name)
    manifest = json.loads(target_manifest.read_text(encoding="utf-8"))
    if manifest.get("tag") != TARGET_TAG or manifest.get("version") != TARGET_VERSION:
        raise RuntimeError(f"target manifest identity mismatch: {manifest}")
    if manifest.get("repository") != REPO:
        raise RuntimeError("target manifest repository mismatch")
    target_platform = next(iter(manifest.get("platforms", {}).values()), {})
    if not target_platform.get("url") or not target_platform.get("sha256"):
        raise RuntimeError("target manifest has no signed payload identity")
    evidence = {
        "source_tag": SOURCE_TAG,
        "target_tag": TARGET_TAG,
        "source_installer": {"name": installer_name, "sha256": actual, "url": release_url(SOURCE_TAG, installer_name)},
        "source_sha256sums": release_url(SOURCE_TAG, source_sums_name),
        "source_metadata": json.loads(source_meta.read_text(encoding="utf-8")),
        "target_manifest": manifest,
        "target_manifest_url": release_url(TARGET_TAG, target_manifest_name),
        "target_signature_url": release_url(TARGET_TAG, target_sig_name),
        "target_signature_sha256": sha256(target_sig),
    }
    save("public-assets.json", evidence)
    return {"installer": source, "manifest": manifest, "evidence": evidence}


def powershell(script: str, *, env=None, timeout=120):
    # Use the runner's PowerShell 7 consistently with its inherited module path.
    path = ROOT / "observer-command.ps1"
    path.write_text("$ErrorActionPreference = 'Stop'\n[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n" + script, encoding="utf-8")
    result = subprocess.run(["pwsh.exe", "-NoProfile", "-NonInteractive", "-File", str(path)],
                            text=True, encoding="utf-8-sig", capture_output=True, env=env, timeout=timeout)
    if result.returncode:
        save("powershell-failure.json", {"code": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        result.check_returncode()
    return result.stdout.strip()


def windows_signature(path: Path) -> dict:
    script = (
        "$s=Get-AuthenticodeSignature -LiteralPath $env:SMOKE_INSTALLER; "
        "$s | Select-Object Status,StatusMessage,@{n='Signer';e={$_.SignerCertificate.Subject}} | ConvertTo-Json"
    )
    env = os.environ.copy()
    env["SMOKE_INSTALLER"] = str(path)
    output = powershell(script, env=env)
    save("source-signature-output.json", {"stdout": output})
    value = json.loads(output)
    if value.get("Status") not in (0, "Valid"):
        raise RuntimeError(f"source installer Authenticode invalid: {value}")
    return value


def mac_signature(path: Path) -> str:
    result = run(["spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "-v", str(path)], check=True)
    if "Notarized Developer ID" not in result.stderr and "Notarized Developer ID" not in result.stdout:
        raise RuntimeError(f"source DMG is not notarized Developer ID: {result.stdout}{result.stderr}")
    return (result.stderr or result.stdout).strip()


def install_windows(installer: Path) -> tuple[Path, int]:
    signature = windows_signature(installer)
    run([str(installer), "/S"], timeout=300)
    appdata = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs"
    shortcuts = list(appdata.rglob("Avibe.lnk"))
    if len(shortcuts) != 1:
        raise RuntimeError(f"expected one installed Avibe shortcut, got {len(shortcuts)}")
    env = os.environ.copy()
    env["SMOKE_SHORTCUT"] = str(shortcuts[0])
    target = powershell("(New-Object -ComObject WScript.Shell).CreateShortcut($env:SMOKE_SHORTCUT).TargetPath", env=env, timeout=60)
    exe = Path(target)
    if not exe.is_relative_to(Path(os.environ["LOCALAPPDATA"])):
        raise RuntimeError("source app is not installed in the default per-user directory")
    save("source-signature.json", signature)
    if normalize_version(windows_version(exe)) != normalize_version(SOURCE_VERSION):
        raise RuntimeError("installed Windows source version mismatch")
    powershell("Start-Process -FilePath $env:SMOKE_SHORTCUT", env=env, timeout=60)
    original = wait_for(lambda: find_process(exe, exclude=set()), 180, "rc2 Windows process did not start")
    save("source-install.json", {"shortcut": str(shortcuts[0]), "executable": str(exe), "pid": original.pid, "version": windows_version(exe)})
    return exe, original.pid


def windows_version(exe: Path) -> str:
    env = os.environ.copy(); env["SMOKE_EXE"] = str(exe)
    return powershell("[Diagnostics.FileVersionInfo]::GetVersionInfo($env:SMOKE_EXE).ProductVersion", env=env, timeout=60)


def find_process(exe: Path, exclude: set[int]):
    for process in psutil.process_iter(["pid", "exe"]):
        try:
            if process.pid in exclude or not process.info["exe"]:
                continue
            if os.path.samefile(process.info["exe"], exe):
                return process
        except (OSError, psutil.Error):
            continue
    return None


def windows_tree(window):
    rows = []
    for c in [window, *window.descendants()]:
        try:
            rows.append({"type": c.element_info.control_type, "text": c.window_text(), "enabled": c.is_enabled()})
        except Exception:
            continue
    return rows


def windows_button(window, label, *, version=False):
    candidates = []
    for c in window.descendants():
        try:
            text = c.window_text().strip()
            matches = (normalize_version(label) in normalize_version(text)) if version else text == label
            if matches and c.element_info.control_type == "Button" and c.is_visible() and c.is_enabled():
                candidates.append(c)
        except Exception:
            continue
    if not candidates:
        return None
    # The panel can expose the same Check action in its header and lead row.
    if len(candidates) > 1 and label != "Check for updates":
        raise RuntimeError(f"ambiguous Windows button: {label}")
    return sorted(candidates, key=lambda c: c.rectangle().top)[0]


FIRST_RUN_BUTTONS = ("Get started", "Set up later", "Start using this Mac", "Continue on this Mac")


def windows_first_run(app):
    clicked = set()
    def ready():
        window = app.top_window()
        rows = windows_tree(window)
        save("first-run-windows.json", rows)
        if windows_button(window, SOURCE_SHORT, version=True):
            return True
        # The startup offer is not the unified panel. Dismiss, never install here.
        if window.window_text() == "Avibe Desktop Updates":
            later = windows_button(window, "Later")
            if later:
                later.click_input()
                record("dismiss-startup-native-offer", platform="windows", label="Later")
                return False
        for label in FIRST_RUN_BUTTONS:
            button = windows_button(window, label)
            if button and label not in clicked:
                button.click_input()
                clicked.add(label)
                record("first-run-click", platform="windows", label=label)
                return False
        return False
    try:
        wait_for(ready, 300, "Windows first-run did not reach the version trigger")
    finally:
        app.top_window().capture_as_image().save(str(EVIDENCE / "first-run-windows.png"))


def windows_panel(pid: int, version: str, stage: str):
    from pywinauto import Application
    app = Application(backend="uia").connect(process=pid)
    if stage == "rc2":
        windows_first_run(app)
    def trigger():
        window = app.top_window()
        save(f"{stage}-accessibility.json", windows_tree(window))
        return windows_button(window, version, version=True)
    button = wait_for(trigger, 300, f"{stage}: version trigger not exposed after cold start")
    button.click_input()
    record("click-version", version=version, platform="windows", label=button.window_text())
    def opened():
        window = app.top_window()
        rows = windows_tree(window)
        save(f"{stage}-panel.json", rows)
        texts = [r["text"].strip() for r in rows]
        if "Version & Update" not in texts or "Current version" not in texts:
            return None
        current_index = texts.index("Current version")
        if not any(normalize_version(t) == normalize_version(version) for t in texts[current_index + 1:current_index + 5]):
            return None
        return window
    window = wait_for(opened, 60, f"{stage}: panel current row not verified")
    window.capture_as_image().save(str(EVIDENCE / f"{stage}-panel.png"))
    record("panel-current-verified", version=version, platform="windows")
    return app


def windows_ui(exe: Path, original_pid: int) -> None:
    app = windows_panel(original_pid, SOURCE_SHORT, "rc2")
    def prerelease():
        for c in app.top_window().descendants():
            if c.window_text().strip() == "Get pre-release updates" and c.is_enabled():
                try:
                    return c, c.iface_toggle.CurrentToggleState
                except Exception:
                    continue
        return None
    toggle, state = wait_for(prerelease, 60, "Windows prerelease toggle unreadable")
    if state not in (0, 1):
        raise RuntimeError("prerelease toggle has indeterminate state")
    if state == 0:
        toggle.click_input()
        record("click-prereleases", from_state=0, platform="windows")
    wait_for(lambda: (v := prerelease()) and v[1] == 1, 30, "prerelease toggle did not become enabled")
    record("prereleases-enabled", platform="windows", initially_enabled=state == 1)
    check = wait_for(lambda: windows_button(app.top_window(), "Check for updates"), 60, "Check button disabled")
    check.click_input()
    record("click-check", platform="windows")
    def offered():
        window = app.top_window()
        rows = windows_tree(window)
        save("rc3-offer-panel.json", rows)
        has_target = any(normalize_version(r["text"]) == normalize_version(TARGET_SHORT) for r in rows)
        return windows_button(window, "Update") if has_target else None
    update = wait_for(offered, 180, "Windows panel did not offer exact rc3 with enabled Update")
    app.top_window().capture_as_image().save(str(EVIDENCE / "rc3-offer-panel.png"))
    record("exact-target-offered", version=TARGET_SHORT, platform="windows")
    update.click_input()
    record("click-update", version=TARGET_SHORT, platform="windows")


def install_macos(installer: Path) -> tuple[Path, int]:
    signature = mac_signature(installer)
    mount = run(["hdiutil", "attach", "-nobrowse", "-readonly", "-plist", str(installer)], timeout=120).stdout
    info = plistlib.loads(mount.encode())
    volumes = [item.get("mount-point") for item in info.get("system-entities", []) if item.get("mount-point")]
    if len(volumes) != 1:
        raise RuntimeError(f"expected one mounted DMG volume, got {volumes}")
    volume = Path(volumes[0]); app_source = volume / "Avibe.app"
    app = ROOT / "installed" / "Avibe.app"
    app.parent.mkdir(parents=True, exist_ok=True)
    if app.exists(): shutil.rmtree(app)
    run(["ditto", str(app_source), str(app)], timeout=180)
    run(["hdiutil", "detach", str(volume)], timeout=120)
    run(["spctl", "--assess", "-v", str(app)], timeout=120)
    binary = app / "Contents/MacOS/avibe-desktop"
    if normalize_version(mac_version(app)) != normalize_version(SOURCE_VERSION):
        raise RuntimeError("installed macOS source version mismatch")
    # Launch the installed bundle through LaunchServices, as a user opening it
    # in Finder would, so activation/window lifecycle is not bypassed.
    run(["open", "-n", str(app)], timeout=60)
    original = wait_for(lambda: find_process(binary, exclude=set()), 180, "rc2 macOS process did not start")
    save("source-signature.json", {"dmg": signature, "app": "spctl --assess passed"})
    save("source-install.json", {"app": str(app), "executable": str(binary), "pid": original.pid, "version": mac_version(app)})
    return app, original.pid


def mac_version(app: Path) -> str:
    with (app / "Contents/Info.plist").open("rb") as stream:
        return str(plistlib.load(stream).get("CFBundleShortVersionString", ""))


def apple_script(script: str, *args: str) -> str:
    result = subprocess.run(["osascript", "-e", script, *args], check=True, text=True, capture_output=True, timeout=120)
    return result.stdout.strip()


# Only public accessibility actions; no JS injection, deep links or updater IPC.
MAC_AX = r'''on run argv
local dumpText, candidateControls, allControls
set targetPid to (item 1 of argv) as integer
set operation to item 2 of argv
set needle to item 3 of argv
 tell application "System Events"
  set p to first application process whose unix id is targetPid
  tell p
   set frontmost to true
   set dumpText to "AXWindowCount" & tab & (count of windows) & linefeed
   set candidateControls to {}
   set allControls to {}
   repeat with appWindow in windows
    set end of allControls to appWindow
    set allControls to allControls & (entire contents of appWindow)
   end repeat
   repeat with e in allControls
    set elementRole to ""
    set elementName to ""
    set elementValue to ""
    set elementDescription to ""
    try
     set elementRole to role of e as text
    end try
    try
     set elementName to name of e as text
    end try
    try
     set elementValue to value of e as text
    end try
    try
     set elementDescription to description of e as text
    end try
    set dumpText to dumpText & elementRole & tab & elementName & tab & elementValue & tab & elementDescription & linefeed
    set labelMatch to elementName is needle or elementValue is needle or elementDescription is needle
    if operation is "version" then
     set labelMatch to elementName contains needle or elementValue contains needle or elementDescription contains needle
    end if
    if labelMatch then
     try
      if (enabled of e) and ((name of every action of e) contains "AXPress") then set end of candidateControls to e
     end try
    end if
   end repeat
   if operation is "dump" then return dumpText
   if (count candidateControls) is 0 then error "no matching enabled AXPress control: " & needle
   if (count candidateControls) is not 1 and needle is not "Check for updates" then error "ambiguous AXPress control: " & needle
   set chosen to item 1 of candidateControls
   if operation is "available" then return "ready"
   if operation is "state" then return value of chosen as text
   perform action "AXPress" of chosen
   return "clicked:" & needle
  end tell
 end tell
end run'''


def mac_ax(pid, operation, label=""):
    return apple_script(MAC_AX, str(pid), operation, label)


def mac_snapshot(pid, stage):
    text = mac_ax(pid, "dump")
    (EVIDENCE / f"{stage}-panel.txt").write_text(text + "\n", encoding="utf-8")
    return text


def mac_has_version(text, version):
    return any(normalize_version(field) == normalize_version(version)
               for row in text.splitlines() for field in row.split("\t"))


def mac_first_run(pid):
    clicked = set()
    def ready():
        text = mac_snapshot(pid, "first-run-macos")
        if mac_has_version(text, SOURCE_SHORT):
            # Do not mistake the startup dialog's Current-version text for a badge.
            if "Download and Install" not in text:
                return True
        labels = ("Later",) if "Download and Install" in text else FIRST_RUN_BUTTONS
        for label in labels:
            if label not in text or (label in clicked and label != "Later"):
                continue
            try:
                mac_ax(pid, "available", label)
            except subprocess.CalledProcessError:
                continue
            receipt = mac_ax(pid, "click", label)
            clicked.add(label)
            record("dismiss-startup-native-offer" if label == "Later" else "first-run-click",
                   platform="macos", label=label, receipt=receipt)
            return False
        return False
    try:
        wait_for(ready, 300, "macOS first-run did not reach the version trigger")
    finally:
        run(["screencapture", "-x", str(EVIDENCE / "first-run-macos.png")], timeout=60, check=False)


def mac_panel(pid: int, version: str, stage: str):
    if stage == "rc2":
        mac_first_run(pid)
    def loaded():
        text = mac_snapshot(pid, stage + "-before")
        return mac_has_version(text, version)
    wait_for(loaded, 300, f"{stage}: macOS version trigger unavailable after cold start")
    native_version = SOURCE_VERSION if normalize_version(version) == normalize_version(SOURCE_VERSION) else TARGET_VERSION
    receipt = mac_ax(pid, "version", native_version)
    record("click-version", version=version, platform="macos", receipt=receipt)
    def current():
        text = mac_snapshot(pid, stage)
        if "Version & Update" not in text or "Current version" not in text:
            return False
        # Validate the current row, not a release-notes link or an offer elsewhere.
        current_tail = text.split("Current version", 1)[1].splitlines()[:5]
        return mac_has_version("\n".join(current_tail), version)
    wait_for(current, 60, f"{stage}: macOS panel current row not verified")
    run(["screencapture", "-x", str(EVIDENCE / f"{stage}-panel.png")], timeout=60)
    record("panel-current-verified", version=version, platform="macos")


def mac_ui(app: Path, original_pid: int) -> None:
    pid = original_pid
    mac_panel(pid, SOURCE_SHORT, "rc2")
    state = mac_ax(pid, "state", "Get pre-release updates")
    if state not in ("0", "1", "false", "true"):
        raise RuntimeError(f"macOS prerelease state unreadable: {state}")
    initially_enabled = state in ("1", "true")
    if not initially_enabled:
        receipt = mac_ax(pid, "click", "Get pre-release updates")
        record("click-prereleases", platform="macos", receipt=receipt)
    wait_for(lambda: mac_ax(pid, "state", "Get pre-release updates") in ("1", "true"), 30, "macOS prerelease setting did not enable")
    record("prereleases-enabled", platform="macos", initially_enabled=initially_enabled)
    # Retry only discovery/state reads, never an Update click.
    wait_for(lambda: mac_ax(pid, "available", "Check for updates") == "ready", 60, "macOS Check button unavailable")
    receipt = mac_ax(pid, "click", "Check for updates")
    record("click-check", platform="macos", receipt=receipt)
    wait_for(lambda: mac_has_version(mac_snapshot(pid, "rc3-offer"), TARGET_SHORT), 180, "macOS panel did not offer rc3")
    run(["screencapture", "-x", str(EVIDENCE / "rc3-offer-panel.png")], timeout=60)
    record("exact-target-offered", version=TARGET_SHORT, platform="macos")
    receipt = mac_ax(pid, "click", "Update")
    record("click-update", version=TARGET_SHORT, platform="macos", receipt=receipt)


def source_is_running(pid):
    child = OWNED_CHILDREN.get(pid)
    if child is not None:
        return child.poll() is None  # poll reaps our own exited child on POSIX.
    try:
        return psutil.Process(pid).status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
    except psutil.NoSuchProcess:
        return False


def wait_for_target(exe_or_app, original_pid: int):
    def replacement():
        version = mac_version(exe_or_app) if sys.platform == "darwin" else windows_version(exe_or_app)
        if normalize_version(version) != normalize_version(TARGET_VERSION):
            return None
        if source_is_running(original_pid):
            return None
        for process in psutil.process_iter(["pid", "exe"]):
            try:
                if process.pid == original_pid or not process.info["exe"]:
                    continue
                if sys.platform == "darwin":
                    same = Path(process.info["exe"]).resolve() == (exe_or_app / "Contents/MacOS/avibe-desktop").resolve()
                else:
                    same = os.path.samefile(process.info["exe"], exe_or_app)
                if same and normalize_version(version) == normalize_version(TARGET_VERSION):
                    return process
            except (OSError, psutil.Error):
                continue
        return None
    process = wait_for(replacement, 600, "rc3 replacement/relaunch not observed")
    save("relaunch.json", {"old_pid": original_pid, "new_pid": process.pid, "target_version": TARGET_VERSION})
    return process


def main() -> None:
    if os.environ.get("GITHUB_REPOSITORY") != REPO:
        raise RuntimeError("repository mismatch")
    if any(os.environ.get(key) for key in ("GH_TOKEN", "GITHUB_TOKEN", "AVIBE_HOME")):
        raise RuntimeError("native process must not inherit credentials or home override")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    assets = verify_public_assets(TARGET_TAG)
    installer = assets["installer"]
    if sys.platform == "darwin":
        app, pid = install_macos(installer)
        mac_ui(app, pid)
        new = wait_for_target(app, pid)
        mac_panel(new.pid, TARGET_SHORT, "rc3-current")
    elif os.name == "nt":
        exe, pid = install_windows(installer)
        windows_ui(exe, pid)
        new = wait_for_target(exe, pid)
        windows_panel(new.pid, TARGET_SHORT, "rc3-current")
    else:
        raise RuntimeError("unsupported runner")
    save("result.json", {"ok": True, "source": SOURCE_TAG, "target": TARGET_TAG, "actions": ACTIONS, "target_version": TARGET_VERSION, "final_panel_current_verified": True})


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        save("failure.json", {"error_type": type(error).__name__, "message": str(error)})
        raise
