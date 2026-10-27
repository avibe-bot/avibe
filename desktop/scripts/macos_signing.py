#!/usr/bin/env python3
"""Developer ID signing, notarization, and code identity for macOS desktop builds.

A signed build signs every Mach-O it ships with the Team's Developer ID, under
the hardened runtime and with a secure timestamp, from the inside out:

1. the private Runtime's executables and libraries, before `runtime.zip`, its
   manifest, and its tree hash exist (`sign_runtime`, called by
   build-runtime-bundle.py). The notary service opens nested archives, so one
   unsigned binary in runtime.zip rejects the whole app;
2. the Cua Driver helper, then Avibe.app, which is notarized and stapled
   (`sign-app`, `notarize`);
3. the DMG that carries the stapled app, which is notarized and stapled too
   (`sign-dmg`, `notarize`, `verify-release`).

There is no ad-hoc mode. Under the hardened runtime, library validation admits
only libraries signed by the executable's own Team, and an ad-hoc signature has
no Team, so an ad-hoc-signed Runtime could not load its own extension modules.
Builds without credentials keep the Runtime's original signatures instead.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import plistlib
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


DESKTOP_DIR = Path(__file__).resolve().parents[1]
ENTITLEMENTS_DIR = DESKTOP_DIR / "entitlements"

# The code identity of every release. TCC grants (Accessibility, Screen
# Recording), Keychain item ACLs, and the updater's code-seal check bind to the
# designated requirement (DR) built from these values. Changing any of them, or
# signing with another Team, makes every installed user's grants and Keychain
# trust stop applying after the next update. Never change them.
TEAM_ID = "68B2SQ6YK3"
APP_IDENTIFIER = "bot.avibe.desktop"
HELPER_IDENTIFIER = "bot.avibe.desktop.cua-driver"
PYTHON_IDENTIFIER = "bot.avibe.desktop.runtime.python"
NODE_IDENTIFIER = "bot.avibe.desktop.runtime.node"

APP_ENTITLEMENTS = DESKTOP_DIR / "src-tauri" / "Entitlements.plist"
HELPER_ENTITLEMENTS = ENTITLEMENTS_DIR / "cua-driver.plist"
# libffi (Python) and V8 (Node) each generate code differently per architecture.
PYTHON_ENTITLEMENTS = {
    "aarch64": ENTITLEMENTS_DIR / "runtime-python-aarch64.plist",
    "x86_64": ENTITLEMENTS_DIR / "runtime-python-x86_64.plist",
}
NODE_ENTITLEMENTS = {
    "aarch64": ENTITLEMENTS_DIR / "runtime-node-aarch64.plist",
    "x86_64": ENTITLEMENTS_DIR / "runtime-node-x86_64.plist",
}

# Every secret the desktop-updater Environment must hold for a signed build.
APPLE_SECRETS = (
    "APPLE_CERTIFICATE",
    "APPLE_CERTIFICATE_PASSWORD",
    "APPLE_SIGNING_IDENTITY",
    "APPLE_TEAM_ID",
    "APPLE_API_KEY",
    "APPLE_API_ISSUER",
    "APPLE_API_PRIVATE_KEY",
)

KEYCHAIN_NAME = "avibe-signing.keychain-db"

MH_EXECUTE = 2
MH_DYLIB = 6
MH_BUNDLE = 8
_THIN_MAGIC = {b"\xcf\xfa\xed\xfe": "<", b"\xce\xfa\xed\xfe": "<", b"\xfe\xed\xfa\xcf": ">", b"\xfe\xed\xfa\xce": ">"}
_FAT_MAGIC = {b"\xca\xfe\xba\xbe": 20, b"\xca\xfe\xba\xbf": 32}
# A fat header and a Java class file share 0xcafebabe; the class file's major
# version (45 or more) sits where a fat header keeps its small slice count.
_MAX_FAT_SLICES = 45
SIGN_WORKERS = 8
# codesign reports this when Apple's timestamp service drops a request; the
# same invocation succeeds on retry.
TIMESTAMP_UNAVAILABLE = "timestamp service is not available"
TIMESTAMP_ATTEMPTS = 3
# stapler fetches and checks tickets through Apple's CloudKit service; a dropped
# connection there fails the action with an NSURLErrorDomain error and Error 68.
# Such drops can last longer than a few seconds: in gh-v3.2.3rc12 an Accepted DMG
# failed all three of the former 15 s-spaced attempts within 52 s. Back off over
# about five minutes before giving up; only the network error is retried.
STAPLER_NETWORK_ERROR = "NSURLErrorDomain"
STAPLER_RETRY_DELAYS_SECONDS = (15, 30, 60, 90, 120)
STAPLER_ATTEMPTS = len(STAPLER_RETRY_DELAYS_SECONDS) + 1


class SigningError(RuntimeError):
    pass


def designated_requirement(identifier: str, team: str = TEAM_ID) -> str:
    """The default Developer ID DR, in the requirement language codesign prints."""

    return (
        f'designated => identifier "{identifier}" and anchor apple generic'
        " and certificate 1[field.1.2.840.113635.100.6.2.6] /* exists */"
        " and certificate leaf[field.1.2.840.113635.100.6.1.13] /* exists */"
        f' and certificate leaf[subject.OU] = "{team}"'
    )


def run(command: list[str], *, redacted: str | None = None, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a tool; a failure names the tool (or `redacted`), never its arguments.

    Arguments can carry passwords or key paths, and a CalledProcessError would
    print them with the traceback.
    """

    result = subprocess.run(command, check=False, capture_output=capture, text=True)
    if result.returncode != 0:
        detail = ""
        if capture:
            detail = "\n" + "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        raise SigningError(f"{redacted or Path(command[0]).name} exited {result.returncode}{detail}")
    return result


def macho_filetype(path: Path) -> int | None:
    """The Mach-O file type of a thin or universal binary, else None."""

    with path.open("rb") as stream:
        header = stream.read(32)
        if len(header) < 16:
            return None
        if header[:4] in _THIN_MAGIC:
            return struct.unpack(_THIN_MAGIC[header[:4]] + "I", header[12:16])[0]
        if header[:4] not in _FAT_MAGIC:
            return None
        count = struct.unpack(">I", header[4:8])[0]
        if count == 0 or count >= _MAX_FAT_SLICES:
            return None
        entry = _FAT_MAGIC[header[:4]]
        stream.seek(8)
        table = stream.read(count * entry)
        types = set()
        for index in range(count):
            fields = table[index * entry : (index + 1) * entry]
            offset = struct.unpack(">Q" if entry == 32 else ">I", fields[8:16] if entry == 32 else fields[8:12])[0]
            stream.seek(offset)
            slice_header = stream.read(16)
            if slice_header[:4] not in _THIN_MAGIC:
                raise SigningError(f"{path}: universal binary slice is not Mach-O")
            types.add(struct.unpack(_THIN_MAGIC[slice_header[:4]] + "I", slice_header[12:16])[0])
        if len(types) != 1:
            raise SigningError(f"{path}: universal binary slices disagree on their file type")
        return types.pop()


def macho_files(root: Path) -> list[tuple[Path, int]]:
    """Every regular Mach-O file below root. Links are skipped: the archive
    stores a link as a copy of its target, which is signed in place."""

    found = []
    for directory, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            filetype = macho_filetype(path)
            if filetype is not None:
                found.append((path, filetype))
    return sorted(found)


def codesign_command(identity: str, keychain: str | None) -> list[str]:
    command = ["codesign", "--force", "--sign", identity, "--options", "runtime", "--timestamp"]
    if keychain:
        command.extend(["--keychain", keychain])
    return command


def codesign(command: list[str], *, label: str) -> None:
    for attempt in range(1, TIMESTAMP_ATTEMPTS + 1):
        try:
            run(command, redacted=f"codesign {label}", capture=True)
            return
        except SigningError as error:
            if TIMESTAMP_UNAVAILABLE not in str(error) or attempt == TIMESTAMP_ATTEMPTS:
                raise
            print(f"timestamp service unavailable signing {label}; retrying ({attempt}/{TIMESTAMP_ATTEMPTS})", flush=True)


def signature_details(path: Path) -> dict[str, str]:
    result = subprocess.run(
        ["codesign", "--display", "--verbose=4", str(path)], check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise SigningError(f"{path}: no readable code signature")
    details: dict[str, str] = {}
    for line in result.stderr.splitlines():
        key, separator, value = line.partition("=")
        if separator and key not in details:
            details[key] = value
        if line.startswith("CodeDirectory "):
            match = re.search(r"flags=(0x[0-9a-fA-F]+)", line)
            details["flags"] = match.group(1) if match else "0x0"
    return details


def signed_entitlements(path: Path) -> dict:
    result = subprocess.run(
        ["codesign", "--display", "--entitlements", "-", "--xml", str(path)],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SigningError(f"{path}: entitlements are unreadable")
    return plistlib.loads(result.stdout) if result.stdout.strip() else {}


def actual_requirement(path: Path) -> str:
    result = subprocess.run(
        ["codesign", "--display", "--requirements", "-", str(path)], check=False, capture_output=True, text=True
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("designated => ")]
    return lines[0] if result.returncode == 0 and len(lines) == 1 else "<unreadable>"


def expected_requirement(identifier: str, team: str) -> str:
    """Normalize through csreq, which shares codesign's decompiler: a Team ID
    that starts with a digit is printed quoted, any other one bare."""

    result = run(["csreq", "-t", "-r", "=" + designated_requirement(identifier, team)], capture=True)
    return result.stdout.strip()


def verify_code(
    path: Path,
    *,
    identifier: str | None = None,
    entitlements: Path | None = None,
    deep: bool = False,
) -> None:
    """Check one signed item: strict seal, hardened runtime, secure timestamp,
    and Team, and, where given, its exact DR and entitlements."""

    verify = ["codesign", "--verify", "--strict", "--verbose=2"]
    if deep:
        verify.append("--deep")
    run([*verify, str(path)], capture=True)
    details = signature_details(path)
    if not int(details.get("flags", "0x0"), 16) & 0x10000:
        raise SigningError(f"{path}: hardened runtime flag is missing")
    if details.get("TeamIdentifier") != TEAM_ID:
        raise SigningError(f"{path}: signed by Team {details.get('TeamIdentifier')!r}, expected {TEAM_ID}")
    if not details.get("Timestamp"):
        raise SigningError(f"{path}: signature has no secure timestamp")
    if identifier is not None:
        if details.get("Identifier") != identifier:
            raise SigningError(f"{path}: identifier {details.get('Identifier')!r}, expected {identifier}")
        actual, expected = actual_requirement(path), expected_requirement(identifier, TEAM_ID)
        if actual != expected:
            raise SigningError(f"{path}: designated requirement differs\n  expected: {expected}\n  actual:   {actual}")
    if entitlements is not None:
        wanted = plistlib.loads(entitlements.read_bytes())
        if signed_entitlements(path) != wanted:
            raise SigningError(f"{path}: entitlements differ from {entitlements.name}")


def sign_runtime(
    payload: Path,
    *,
    arch: str,
    python_executable: Path,
    node_executable: Path,
    identity: str,
    keychain: str | None = None,
) -> int:
    """Sign every Mach-O in the private Runtime; return how many were signed.

    Libraries carry no entitlements: only a process's main executable grants
    them. Each executable is listed with its own identifier and entitlements,
    and an unreviewed executable stops the build rather than ship with
    entitlements nobody decided on.
    """

    executables = {
        python_executable.resolve(): (PYTHON_IDENTIFIER, PYTHON_ENTITLEMENTS[arch]),
        node_executable.resolve(): (NODE_IDENTIFIER, NODE_ENTITLEMENTS[arch]),
    }
    libraries, programs = [], []
    for path, filetype in macho_files(payload):
        if filetype in (MH_DYLIB, MH_BUNDLE):
            libraries.append(path)
        elif filetype == MH_EXECUTE and path.resolve() in executables:
            programs.append(path)
        elif filetype == MH_EXECUTE:
            raise SigningError(f"Runtime executable has no reviewed identity and entitlements: {path.relative_to(payload)}")
        else:
            raise SigningError(f"Runtime contains Mach-O type {filetype} that cannot be signed: {path.relative_to(payload)}")
    if {path.resolve() for path in programs} != set(executables):
        raise SigningError("Runtime is missing its Python or Node executable")

    base = codesign_command(identity, keychain)

    def sign_library(path: Path) -> None:
        codesign([*base, str(path)], label=str(path.relative_to(payload)))
        verify_code(path)

    # Inside out: the libraries an executable loads are sealed before it is.
    with ThreadPoolExecutor(max_workers=SIGN_WORKERS) as pool:
        list(pool.map(sign_library, libraries))
    for path in programs:
        identifier, entitlements = executables[path.resolve()]
        codesign(
            [*base, "--identifier", identifier, "--entitlements", str(entitlements), str(path)],
            label=str(path.relative_to(payload)),
        )
        verify_code(path, identifier=identifier, entitlements=entitlements)
    return len(libraries) + len(programs)


def unexpected_app_code(app: Path) -> list[Path]:
    """Mach-O files in the bundle besides its main executable and the helper."""

    expected = {app / "Contents/Helpers/cua-driver"}
    executable = plistlib.loads((app / "Contents/Info.plist").read_bytes())["CFBundleExecutable"]
    expected.add(app / "Contents/MacOS" / executable)
    return [path for path, _type in macho_files(app) if path not in expected]


def sign_app(app: Path, *, identity: str, keychain: str | None) -> None:
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") != APP_IDENTIFIER:
        raise SigningError(f"Avibe.app bundle identifier must stay {APP_IDENTIFIER}")
    helper = app / "Contents/Helpers/cua-driver"
    if not helper.is_file() or helper.is_symlink():
        raise SigningError("Avibe.app is missing Contents/Helpers/cua-driver")
    if extra := unexpected_app_code(app):
        raise SigningError(f"Avibe.app contains unreviewed nested code: {[str(path.relative_to(app)) for path in extra]}")
    base = codesign_command(identity, keychain)
    codesign(
        [*base, "--identifier", HELPER_IDENTIFIER, "--entitlements", str(HELPER_ENTITLEMENTS), str(helper)],
        label="Contents/Helpers/cua-driver",
    )
    # The bundle signature seals the main executable and the signed helper; its
    # identifier comes from Info.plist.
    codesign([*base, "--entitlements", str(APP_ENTITLEMENTS), str(app)], label="Avibe.app")
    verify_app(app)


def verify_app(app: Path) -> None:
    verify_code(app / "Contents/Helpers/cua-driver", identifier=HELPER_IDENTIFIER, entitlements=HELPER_ENTITLEMENTS)
    verify_code(app, identifier=APP_IDENTIFIER, entitlements=APP_ENTITLEMENTS, deep=True)


def sign_dmg(dmg: Path, *, identity: str, keychain: str | None) -> None:
    command = ["codesign", "--force", "--sign", identity, "--timestamp"]
    if keychain:
        command.extend(["--keychain", keychain])
    codesign([*command, str(dmg)], label=dmg.name)
    verify_dmg_signature(dmg)


def verify_dmg_signature(dmg: Path) -> None:
    run(["codesign", "--verify", "--strict", "--verbose=2", str(dmg)], capture=True)
    details = signature_details(dmg)
    if details.get("TeamIdentifier") != TEAM_ID or not details.get("Timestamp"):
        raise SigningError(f"{dmg.name}: not signed by Team {TEAM_ID} with a secure timestamp")


def notary_credentials(key_path: str, key_id: str, issuer: str) -> list[str]:
    return ["--key", key_path, "--key-id", key_id, "--issuer", issuer]


def notarize(path: Path, *, key_path: str, key_id: str, issuer: str) -> None:
    """Submit to the notary service, require `Accepted`, then staple and validate.

    An app is submitted as a ditto zip of the bundle; the ticket is stapled to
    the bundle itself. The submission log is printed either way: it lists every
    rejected or warned binary, and contains no credentials.
    """

    credentials = notary_credentials(key_path, key_id, issuer)
    with tempfile.TemporaryDirectory(prefix="avibe-notary-") as temporary:
        submission = path
        if path.suffix == ".app":
            submission = Path(temporary) / f"{path.stem}.zip"
            run(["ditto", "-c", "-k", "--keepParent", str(path), str(submission)])
        result = subprocess.run(
            ["xcrun", "notarytool", "submit", str(submission), *credentials,
             "--wait", "--timeout", "2h", "--output-format", "json"],
            check=False,
            capture_output=True,
            text=True,
        )
    try:
        outcome = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise SigningError(f"notarytool submit failed for {path.name}: {result.stderr.strip()}") from None
    submission_id, status = outcome.get("id"), outcome.get("status")
    print(f"notarization {path.name}: id={submission_id} status={status}", flush=True)
    if submission_id:
        log = subprocess.run(
            ["xcrun", "notarytool", "log", str(submission_id), *credentials],
            check=False,
            capture_output=True,
            text=True,
        )
        try:
            issues = json.loads(log.stdout).get("issues")
        except json.JSONDecodeError:
            issues = log.stdout.strip() or log.stderr.strip()
        print(f"notary issues for {path.name}: {json.dumps(issues, indent=2)}", flush=True)
    if status != "Accepted":
        raise SigningError(f"notarization of {path.name} ended {status!r}, not Accepted")
    stapler("staple", path)
    stapler("validate", path)


def stapler(action: str, path: Path) -> None:
    for attempt in range(1, STAPLER_ATTEMPTS + 1):
        try:
            run(["xcrun", "stapler", action, str(path)], redacted=f"stapler {action} {path.name}", capture=True)
            return
        except SigningError as error:
            if STAPLER_NETWORK_ERROR not in str(error) or attempt == STAPLER_ATTEMPTS:
                raise
            delay = STAPLER_RETRY_DELAYS_SECONDS[attempt - 1]
            print(
                f"stapler {action} {path.name} lost its connection to Apple; "
                f"retrying in {delay} s ({attempt}/{STAPLER_ATTEMPTS})",
                flush=True,
            )
            time.sleep(delay)


def assess(path: Path, *, kind: str) -> None:
    """Gatekeeper's verdict, as a quarantined download would receive it."""

    command = ["spctl", "--assess", "--type", kind, "-vvv"]
    if kind == "open":
        command.extend(["--context", "context:primary-signature"])
    result = subprocess.run([*command, str(path)], check=False, capture_output=True, text=True)
    report = result.stderr.strip()
    print(report, flush=True)
    if result.returncode != 0 or "accepted" not in report or "source=Notarized Developer ID" not in report:
        raise SigningError(f"Gatekeeper does not accept {path.name} as notarized Developer ID code")


def verify_release(app: Path, dmg: Path) -> None:
    """The shipped shapes: the stapled app, and the stapled DMG with the same
    app inside it."""

    verify_dmg_signature(dmg)
    for path, kind in ((app, "exec"), (dmg, "open")):
        stapler("validate", path)
        assess(path, kind=kind)
    verify_app(app)
    with tempfile.TemporaryDirectory(prefix="avibe-dmg-verify-") as mountpoint:
        run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mountpoint, str(dmg)], capture=True)
        try:
            inner = Path(mountpoint) / "Avibe.app"
            verify_app(inner)
            stapler("validate", inner)
            assess(inner, kind="exec")
        finally:
            subprocess.run(["hdiutil", "detach", mountpoint], check=False)


def append_env(env_file: Path, values: dict[str, str]) -> None:
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise SigningError(f"{key} must be a single line")
    with env_file.open("a", encoding="utf-8") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in values.items()))


def credential_policy(environment: dict[str, str], release_tag: str) -> bool:
    """Whether to sign. A release requires the complete set; a manual build
    accepts the complete set or none of it. A partial set always fails."""

    missing = [name for name in APPLE_SECRETS if not environment.get(name)]
    if len(missing) == len(APPLE_SECRETS) and not release_tag:
        return False
    if missing:
        scope = "Release builds require" if release_tag else "A partially configured set fails; set"
        raise SigningError(f"{scope} the complete Apple credential set; missing: {', '.join(missing)}")
    team = environment["APPLE_TEAM_ID"]
    if team != TEAM_ID:
        raise SigningError(f"APPLE_TEAM_ID must be the pinned desktop Team {TEAM_ID}; the code identity never changes")
    identity = environment["APPLE_SIGNING_IDENTITY"]
    if not identity.startswith("Developer ID Application: ") or not identity.endswith(f" ({TEAM_ID})"):
        raise SigningError(f"APPLE_SIGNING_IDENTITY must name the Developer ID Application identity of Team {TEAM_ID}")
    if "BEGIN PRIVATE KEY" not in environment["APPLE_API_PRIVATE_KEY"]:
        raise SigningError("APPLE_API_PRIVATE_KEY must be the raw .p8 App Store Connect key")
    if re.fullmatch(r"[A-Z0-9]{8,12}", environment["APPLE_API_KEY"]) is None:
        raise SigningError("APPLE_API_KEY must be the App Store Connect key ID")
    if re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", environment["APPLE_API_ISSUER"]) is None:
        raise SigningError("APPLE_API_ISSUER must be the App Store Connect issuer UUID")
    return True


def import_identity(environment: dict[str, str], *, release_tag: str, env_file: Path, work_dir: Path) -> None:
    """Install the Developer ID identity in a temporary keychain and the notary
    key in a private file, and export only their locations and names.

    Tauri never receives Apple credentials: none of the exported names is one
    that tauri-bundler reads, so it can neither sign nor notarize on its own.
    """

    if not credential_policy(environment, release_tag):
        append_env(env_file, {"apple_signing": "absent"})
        print("Apple signing credentials are absent; this manual build stays ad-hoc.", flush=True)
        return
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    keychain = work_dir / KEYCHAIN_NAME
    password = secrets.token_urlsafe(32)
    run(["security", "create-keychain", "-p", password, str(keychain)], redacted="security create-keychain")
    run(["security", "set-keychain-settings", "-lut", "21600", str(keychain)])
    run(["security", "unlock-keychain", "-p", password, str(keychain)], redacted="security unlock-keychain")
    certificate = work_dir / "certificate.p12"
    try:
        certificate_bytes = base64.b64decode(environment["APPLE_CERTIFICATE"], validate=False)
    except binascii.Error:
        raise SigningError("APPLE_CERTIFICATE must be a base64 .p12") from None
    descriptor = os.open(certificate, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(certificate_bytes)
    try:
        run(
            ["security", "import", str(certificate), "-k", str(keychain),
             "-P", environment["APPLE_CERTIFICATE_PASSWORD"], "-T", "/usr/bin/codesign"],
            redacted="security import (check APPLE_CERTIFICATE and APPLE_CERTIFICATE_PASSWORD)",
        )
    finally:
        certificate.unlink(missing_ok=True)
    run(
        ["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s", "-k", password, str(keychain)],
        redacted="security set-key-partition-list",
        capture=True,
    )
    listed = run(["security", "list-keychains", "-d", "user"], capture=True).stdout
    search = [line.strip().strip('"') for line in listed.splitlines() if line.strip()]
    run(["security", "list-keychains", "-d", "user", "-s", str(keychain), *search])
    identities = run(["security", "find-identity", "-v", "-p", "codesigning", str(keychain)], capture=True).stdout
    if f'"{environment["APPLE_SIGNING_IDENTITY"]}"' not in identities:
        raise SigningError("The imported certificate does not provide a valid APPLE_SIGNING_IDENTITY")
    key = work_dir / f"AuthKey_{environment['APPLE_API_KEY']}.p8"
    descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(environment["APPLE_API_PRIVATE_KEY"])
    append_env(
        env_file,
        {
            "apple_signing": "enabled",
            "AVIBE_CODESIGN_IDENTITY": environment["APPLE_SIGNING_IDENTITY"],
            "AVIBE_CODESIGN_KEYCHAIN": str(keychain),
            "AVIBE_NOTARY_KEY_PATH": str(key),
            "AVIBE_NOTARY_KEY_ID": environment["APPLE_API_KEY"],
            "AVIBE_NOTARY_ISSUER": environment["APPLE_API_ISSUER"],
        },
    )
    print(f"Developer ID identity of Team {TEAM_ID} is ready in a temporary keychain.", flush=True)


def cleanup(work_dir: Path) -> None:
    keychain = work_dir / KEYCHAIN_NAME
    if keychain.exists():
        # Deleting the keychain also drops it from the search list.
        subprocess.run(["security", "delete-keychain", str(keychain)], check=False)
    shutil.rmtree(work_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    importer = sub.add_parser("import-identity", help="validate Apple secrets from the environment and install them")
    importer.add_argument("--release-tag", default="")
    importer.add_argument("--env-file", type=Path, required=True)
    importer.add_argument("--work-dir", type=Path, required=True)
    cleaner = sub.add_parser("cleanup", help="delete the temporary keychain and key files")
    cleaner.add_argument("--work-dir", type=Path, required=True)
    for name in ("sign-app", "sign-dmg"):
        signer = sub.add_parser(name)
        signer.add_argument("path", type=Path)
        signer.add_argument("--identity", default=os.environ.get("AVIBE_CODESIGN_IDENTITY"))
        signer.add_argument("--keychain", default=os.environ.get("AVIBE_CODESIGN_KEYCHAIN"))
    notarizer = sub.add_parser("notarize", help="submit, require Accepted, staple, and validate")
    notarizer.add_argument("path", type=Path)
    releaser = sub.add_parser("verify-release", help="Gatekeeper, staple, and code identity checks of the shipped app and DMG")
    releaser.add_argument("--app", type=Path, required=True)
    releaser.add_argument("--dmg", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "import-identity":
            import_identity(dict(os.environ), release_tag=args.release_tag, env_file=args.env_file, work_dir=args.work_dir)
        elif args.command == "cleanup":
            cleanup(args.work_dir)
        elif args.command in ("sign-app", "sign-dmg"):
            if not args.identity:
                raise SigningError("No signing identity: run import-identity first")
            if args.command == "sign-app":
                sign_app(args.path, identity=args.identity, keychain=args.keychain)
            else:
                sign_dmg(args.path, identity=args.identity, keychain=args.keychain)
        elif args.command == "notarize":
            names = ("AVIBE_NOTARY_KEY_PATH", "AVIBE_NOTARY_KEY_ID", "AVIBE_NOTARY_ISSUER")
            if any(not os.environ.get(name) for name in names):
                raise SigningError("No notary credentials: run import-identity first")
            notarize(
                args.path,
                key_path=os.environ["AVIBE_NOTARY_KEY_PATH"],
                key_id=os.environ["AVIBE_NOTARY_KEY_ID"],
                issuer=os.environ["AVIBE_NOTARY_ISSUER"],
            )
        else:
            verify_release(args.app, args.dmg)
    except SigningError as error:
        print(f"::error::{error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
