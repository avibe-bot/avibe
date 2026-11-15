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
SOURCE_SHORT = SOURCE_VERSION.replace("-", "")
TARGET_SHORT = TARGET_VERSION.replace("-", "")


def save(name: str, value) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(command, *, timeout=180, check=True, text=True):
    return subprocess.run(command, check=check, timeout=timeout, text=text, capture_output=True)


def wait_for(callback, seconds: float, label: str):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            value = callback()
            if value:
                return value
        except Exception as error:  # process turnover is expected
            last = type(error).__name__
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


def windows_signature(path: Path) -> dict:
    script = (
        "$s=Get-AuthenticodeSignature -LiteralPath $env:SMOKE_INSTALLER; "
        "$s | Select-Object Status,StatusMessage,@{n='Signer';e={$_.SignerCertificate.Subject}} | ConvertTo-Json"
    )
    env = os.environ.copy()
    env["SMOKE_INSTALLER"] = str(path)
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            check=True, text=True, capture_output=True, env=env, timeout=120)
    value = json.loads(result.stdout)
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
    target = subprocess.check_output(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "(New-Object -ComObject WScript.Shell).CreateShortcut($env:SMOKE_SHORTCUT).TargetPath"],
        text=True, env=env, timeout=60).strip()
    exe = Path(target)
    if not exe.is_relative_to(Path(os.environ["LOCALAPPDATA"])):
        raise RuntimeError("source app is not installed in the default per-user directory")
    save("source-signature.json", signature)
    run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Start-Process -FilePath $env:SMOKE_SHORTCUT"], timeout=60)
    original = wait_for(lambda: find_process(exe, exclude=set()), 180, "rc2 Windows process did not start")
    save("source-install.json", {"shortcut": str(shortcuts[0]), "executable": str(exe), "pid": original.pid, "version": windows_version(exe)})
    return exe, original.pid


def windows_version(exe: Path) -> str:
    env = os.environ.copy(); env["SMOKE_EXE"] = str(exe)
    return subprocess.check_output(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "[Diagnostics.FileVersionInfo]::GetVersionInfo($env:SMOKE_EXE).ProductVersion"],
        text=True, env=env, timeout=60).strip()


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


def windows_ui(exe: Path, original_pid: int) -> None:
    from pywinauto import Application
    app = Application(backend="uia").connect(process=original_pid)
    window = app.top_window()
    window.capture_as_image().save(str(EVIDENCE / "rc2-before-panel.png"))
    controls = window.descendants()
    dump = []
    for control in controls:
        try:
            dump.append({"type": control.control_type(), "text": control.window_text(), "enabled": control.is_enabled()})
        except Exception:
            pass
    save("windows-accessibility-before.json", dump)
    variants = {SOURCE_VERSION, SOURCE_SHORT, SOURCE_VERSION.replace("-", " ")}
    trigger = next((c for c in controls if any(v.lower() in c.window_text().lower() for v in variants)), None)
    if trigger is None:
        raise RuntimeError("Windows accessibility tree did not expose the rc2 version trigger")
    trigger.click_input()
    panel = wait_for(lambda: find_window_text(app, "Version & Update"), 30, "Windows unified panel did not open")
    panel.capture_as_image().save(str(EVIDENCE / "rc2-panel.png"))
    ensure_windows_prerelease(panel)
    click_windows_text(panel, "Check for updates")
    wait_for(lambda: find_window_text(app, TARGET_SHORT) or find_window_text(app, TARGET_VERSION), 120, "Windows panel did not find rc3")
    panel.capture_as_image().save(str(EVIDENCE / "rc3-offer-panel.png"))
    click_windows_text(panel, "Update")


def ensure_windows_prerelease(control):
    for item in [control, *control.descendants()]:
        try:
            if item.control_type() != "CheckBox" or "pre-release" not in item.window_text().lower():
                continue
            state = item.get_toggle_state()
            if state != 1:
                item.click_input()
            return
        except Exception:
            continue


def find_window_text(app, needle: str):
    needle = needle.lower()
    for control in [app.top_window(), *app.top_window().descendants()]:
        try:
            if needle in control.window_text().lower():
                return control
        except Exception:
            pass
    return None


def click_windows_text(control, needle: str, *, optional=False):
    needle = needle.lower()
    all_items = [control, *control.descendants()]
    exact = []
    partial = []
    for item in all_items:
        try:
            text = item.window_text().strip()
            if not item.is_enabled():
                continue
            if text.lower() == needle:
                exact.append(item)
            elif needle in text.lower() and item.control_type() in {"Button", "CheckBox", "Hyperlink"}:
                partial.append(item)
        except Exception:
            pass
    candidates = exact or partial
    if len(candidates) != 1:
        if optional and not candidates:
            return
        raise RuntimeError(f"expected one enabled Windows control containing {needle!r}, got {len(candidates)}")
    candidates[0].click_input()


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
    process = subprocess.Popen([str(binary)], cwd=str(app.parent), env=os.environ.copy())
    original = wait_for(lambda: psutil.Process(process.pid) if psutil.pid_exists(process.pid) else None, 180, "rc2 macOS process did not start")
    save("source-signature.json", {"dmg": signature, "app": "spctl --assess passed"})
    save("source-install.json", {"app": str(app), "executable": str(binary), "pid": original.pid, "version": mac_version(app)})
    return app, original.pid


def mac_version(app: Path) -> str:
    with (app / "Contents/Info.plist").open("rb") as stream:
        return str(plistlib.load(stream).get("CFBundleShortVersionString", ""))


def apple_script(script: str, *args: str) -> str:
    result = subprocess.run(["osascript", "-e", script, *args], check=True, text=True, capture_output=True, timeout=120)
    return result.stdout.strip()


def mac_ui(app: Path, original_pid: int) -> None:
    dump_script = r'''tell application "System Events"
 tell process "Avibe"
  set frontmost to true
  set rows to {}
  repeat with e in entire contents
   try
    set end of rows to ((role description of e as text) & "|" & (name of e as text) & "|" & (value of e as text))
   end try
  end repeat
  return rows as text
 end tell
end tell'''
    dump = apple_script(dump_script)
    (EVIDENCE / "macos-accessibility-before.txt").write_text(dump + "\n", encoding="utf-8")
    variants = [SOURCE_VERSION, SOURCE_SHORT, SOURCE_VERSION.replace("-", " ")]
    click_script = r'''on run argv
set needles to argv
 tell application "System Events"
  tell process "Avibe"
   set frontmost to true
   repeat with e in entire contents
    try
     set textValue to ((name of e as text) & " " & (value of e as text))
     repeat with needle in needles
      if textValue contains (needle as text) then
       perform action "AXPress" of e
       return "clicked:" & textValue
      end if
     end repeat
    end try
   end repeat
  end tell
 end tell
 error "no matching accessible element"
end run'''
    result = subprocess.run(["osascript", "-e", click_script, *variants], check=True, text=True, capture_output=True, timeout=120)
    (EVIDENCE / "macos-click-version.txt").write_text(result.stdout, encoding="utf-8")
    time.sleep(3)
    subprocess.run(["screencapture", "-x", str(EVIDENCE / "rc2-panel.png")], check=True, timeout=60)
    for needle in ["Get pre-release updates", "Check for updates", "Check for Desktop Updates"]:
        try:
            apple_script(r'''on run argv
set needle to item 1 of argv
 tell application "System Events" to tell process "Avibe"
  set frontmost to true
  repeat with e in entire contents
   try
    set labelText to ((name of e as text) & " " & (value of e as text))
    if labelText contains needle then
     set currentValue to (value of e as text)
     if currentValue is not "1" and currentValue is not "true" and currentValue is not "selected" then
      perform action "AXPress" of e
      return "toggled:" & labelText
     end if
     return "already-on:" & labelText
    end if
   end try
  end repeat
 end tell
end run''', needle)
            if "Check" in needle:
                break
        except subprocess.CalledProcessError:
            continue
    target_seen = wait_for(lambda: TARGET_SHORT in apple_script(dump_script) or TARGET_VERSION in apple_script(dump_script), 120, "macOS panel did not find rc3")
    _ = target_seen
    subprocess.run(["screencapture", "-x", str(EVIDENCE / "rc3-offer-panel.png")], check=True, timeout=60)
    apple_script(r'''tell application "System Events" to tell process "Avibe"
set frontmost to true
repeat with e in entire contents
 try
  if (name of e as text) is "Update" then
   perform action "AXPress" of e
   return "clicked"
  end if
 end try
end repeat
end tell''')


def wait_for_target(exe_or_app, original_pid: int):
    def replacement():
        for process in psutil.process_iter(["pid", "exe"]):
            try:
                if process.pid == original_pid or not process.info["exe"]:
                    continue
                if sys.platform == "darwin":
                    same = Path(process.info["exe"]).resolve() == (exe_or_app / "Contents/MacOS/avibe-desktop").resolve()
                    version = mac_version(exe_or_app)
                else:
                    same = os.path.samefile(process.info["exe"], exe_or_app)
                    version = windows_version(exe_or_app)
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
        wait_for_target(app, pid)
    elif os.name == "nt":
        exe, pid = install_windows(installer)
        windows_ui(exe, pid)
        wait_for_target(exe, pid)
    else:
        raise RuntimeError("unsupported runner")
    save("result.json", {"ok": True, "source": SOURCE_TAG, "target": TARGET_TAG, "clicked": "version -> prerelease/check -> exact Update", "target_version": TARGET_VERSION})


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        save("failure.json", {"error_type": type(error).__name__, "message": str(error)})
        raise
