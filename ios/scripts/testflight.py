#!/usr/bin/env python3
"""Sign, check, and hand an iOS build to TestFlight internal testing.

The ios-testflight workflow calls this script in four steps:

- ``prepare`` imports the Apple Distribution identity into a temporary
  keychain, installs the App Store profile, writes the App Store Connect key
  and the export options, and records their locations (never secret values)
  in the GitHub environment file.
- ``verify-archive`` checks that the archived app is the expected bundle,
  signed by the Team's distribution identity, with the entitlements the App
  Store profile grants and nothing a development build carries.
- ``distribute`` waits for App Store Connect to process the uploaded build and
  makes it available to the internal testing group.
- ``cleanup`` removes the keychain, the installed profile, and the work
  directory.

It uses only the standard library and the macOS ``security``, ``codesign``,
and ``openssl`` tools.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import plistlib
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TEAM_ID = "68B2SQ6YK3"
BUNDLE_ID = "bot.avibe.ios"
NOTIFICATION_SERVICE_BUNDLE_ID = "bot.avibe.ios.notification-service"
APPLICATION_IDENTIFIER = f"{TEAM_ID}.{BUNDLE_ID}"
NOTIFICATION_SERVICE_APPLICATION_IDENTIFIER = (
    f"{TEAM_ID}.{NOTIFICATION_SERVICE_BUNDLE_ID}"
)
INTERNAL_GROUP = "Avibe Internal"
KEYCHAIN_NAME = "avibe-ios-signing.keychain-db"
ASC_API = "https://api.appstoreconnect.apple.com"
PRIVATE_KEYCHAIN_ACCESS_GROUP = f"{TEAM_ID}.bot.avibe.ios"
SHARED_KEYCHAIN_ACCESS_GROUP = f"{TEAM_ID}.bot.avibe.ios.push"
# Kept as the shared-group alias for older callers and fixtures.
KEYCHAIN_ACCESS_GROUP = SHARED_KEYCHAIN_ACCESS_GROUP
MAIN_REQUIRED_ENTITLEMENTS = {
    "aps-environment": ["production"],
    "com.apple.developer.associated-domains": ["webcredentials:avibe.bot"],
    "com.apple.security.application-groups": ["group.bot.avibe.ios"],
    "keychain-access-groups": [
        PRIVATE_KEYCHAIN_ACCESS_GROUP,
        SHARED_KEYCHAIN_ACCESS_GROUP,
    ],
}
NOTIFICATION_SERVICE_REQUIRED_ENTITLEMENTS = {
    "com.apple.security.application-groups": ["group.bot.avibe.ios"],
    "keychain-access-groups": [SHARED_KEYCHAIN_ACCESS_GROUP],
}
FORBIDDEN_ENTITLEMENTS = {
    "com.apple.developer.usernotifications.filtering",
}
# Kept as the main-app alias for callers and older tests.
REQUIRED_ENTITLEMENTS = MAIN_REQUIRED_ENTITLEMENTS

SECRET_NAMES = (
    "IOS_DISTRIBUTION_P12_BASE64",
    "IOS_DISTRIBUTION_P12_PASSWORD",
    "IOS_APP_STORE_PROFILE_BASE64",
    "IOS_NOTIFICATION_SERVICE_PROFILE_BASE64",
    "IOS_APPLE_TEAM_ID",
    "IOS_APP_STORE_CONNECT_KEY_ID",
    "IOS_APP_STORE_CONNECT_ISSUER_ID",
    "IOS_APP_STORE_CONNECT_PRIVATE_KEY",
)


class TestFlightError(RuntimeError):
    pass


def run(command: list[str], *, capture: bool = False, redacted: str | None = None, data: bytes | None = None):
    """Run a tool; on failure, name it by `redacted` so no argument (password) is echoed."""
    result = subprocess.run(command, input=data, capture_output=True)
    if result.returncode != 0:
        name = redacted or " ".join(command)
        detail = result.stderr.decode(errors="replace").strip()
        raise TestFlightError(f"{name} failed ({result.returncode}): {detail}")
    return result.stdout if capture else None


# ---------------------------------------------------------------------------
# Inputs


def require_secrets(environment: dict[str, str]) -> dict[str, str]:
    missing = [name for name in SECRET_NAMES if not environment.get(name, "").strip()]
    if missing:
        raise TestFlightError("Missing repository secrets: " + ", ".join(missing))
    values = {name: environment[name].strip() for name in SECRET_NAMES}
    # The password may legitimately contain surrounding spaces.
    values["IOS_DISTRIBUTION_P12_PASSWORD"] = environment["IOS_DISTRIBUTION_P12_PASSWORD"]
    if values["IOS_APPLE_TEAM_ID"] != TEAM_ID:
        raise TestFlightError(f"IOS_APPLE_TEAM_ID is not Team {TEAM_ID}")
    return values


def decode_base64(value: str, name: str) -> bytes:
    try:
        return base64.b64decode("".join(value.split()), validate=True)
    except ValueError as error:
        raise TestFlightError(f"{name} is not base64") from error


def app_store_connect_key(value: str) -> bytes:
    """The .p8 text, stored either as is or base64-encoded."""
    text = value.strip()
    if not text.startswith("-----BEGIN"):
        text = decode_base64(text, "IOS_APP_STORE_CONNECT_PRIVATE_KEY").decode().strip()
    if not (text.startswith("-----BEGIN PRIVATE KEY-----") and text.endswith("-----END PRIVATE KEY-----")):
        raise TestFlightError("IOS_APP_STORE_CONNECT_PRIVATE_KEY is not a PKCS#8 .p8 key")
    return (text + "\n").encode()


# ---------------------------------------------------------------------------
# Provisioning profile


def keychain_profile_grant_covers(required_group: str, grant: object) -> bool:
    """Whether one profile allowlist entry authorizes an exact signed group."""
    if not isinstance(grant, str):
        return False
    if grant == required_group:
        return True
    if grant.count("*") != 1 or not grant.endswith(".*"):
        return False
    prefix = grant[:-1]
    return prefix.startswith(f"{TEAM_ID}.") and required_group.startswith(prefix)


def check_profile(
    profile: dict,
    *,
    bundle_id: str = BUNDLE_ID,
    required_entitlements: dict[str, list[str]] = MAIN_REQUIRED_ENTITLEMENTS,
    now: datetime | None = None,
) -> None:
    """Validate a current App Store profile and its target entitlements."""
    now = now or datetime.now(timezone.utc)
    entitlements = profile.get("Entitlements", {})
    forbidden = set(FORBIDDEN_ENTITLEMENTS)
    if bundle_id == NOTIFICATION_SERVICE_BUNDLE_ID:
        forbidden.add("aps-environment")
    present_forbidden = sorted(forbidden.intersection(entitlements))
    if present_forbidden:
        raise TestFlightError(
            "The profile contains forbidden entitlements: "
            + ", ".join(present_forbidden)
        )
    if TEAM_ID not in profile.get("TeamIdentifier", []):
        raise TestFlightError(f"The profile is not for Team {TEAM_ID}")
    application_identifier = f"{TEAM_ID}.{bundle_id}"
    if entitlements.get("application-identifier") != application_identifier:
        raise TestFlightError(f"The profile is not for {application_identifier}")
    if profile.get("ProvisionedDevices") or profile.get("ProvisionsAllDevices"):
        raise TestFlightError("The profile is a development or ad hoc profile, not App Store")
    if entitlements.get("get-task-allow"):
        raise TestFlightError("The profile allows debugging; it is not an App Store profile")
    expires = profile.get("ExpirationDate")
    if not isinstance(expires, datetime) or expires.replace(tzinfo=expires.tzinfo or timezone.utc) <= now:
        raise TestFlightError("The App Store profile has expired; regenerate it in the Apple Developer account")
    for key, required in required_entitlements.items():
        actual = entitlements.get(key)
        if key == "aps-environment" and actual != "production":
            raise TestFlightError("The App Store profile must grant aps-environment=production")
        if actual is None:
            raise TestFlightError(f"The profile does not grant {key}; enable it on the App ID and regenerate")
        if key == "keychain-access-groups":
            if isinstance(actual, str):
                actual_values = [actual]
            elif isinstance(actual, list):
                actual_values = actual
            else:
                actual_values = []
            grants_are_well_formed = all(
                isinstance(grant, str) for grant in actual_values
            )
            requirements_are_covered = all(
                any(
                    keychain_profile_grant_covers(required_group, grant)
                    for grant in actual_values
                )
                for required_group in required
            )
            if (
                not actual_values
                or not grants_are_well_formed
                or not requirements_are_covered
            ):
                raise TestFlightError(
                    "The profile must cover every required keychain-access-groups "
                    "value with an exact grant or same-Team wildcard"
                )
            continue
        if isinstance(actual, str):
            actual_values = {actual}
        else:
            try:
                actual_values = set(actual)
            except (TypeError, ValueError):
                actual_values = set()
        if key == "com.apple.developer.associated-domains" and "*" in actual_values:
            # Provisioning-profile entitlements are an allowlist. A wildcard
            # grant here covers this domain; the archived app is still checked
            # below for the exact webcredentials domain it signs.
            continue
        if not set(required) <= actual_values:
            raise TestFlightError(
                f"The profile does not grant the required {key} values; "
                "enable them on the App ID and regenerate"
            )


def profile_certificate_hashes(profile: dict) -> set[str]:
    return {hashlib.sha1(certificate).hexdigest().upper() for certificate in profile.get("DeveloperCertificates", [])}


def decode_profile(path: Path) -> dict:
    return plistlib.loads(run(["security", "cms", "-D", "-i", str(path)], capture=True))


def profile_directories(home: Path) -> list[Path]:
    # Xcode 16 and later read the second; earlier tools read the first.
    return [
        home / "Library/MobileDevice/Provisioning Profiles",
        home / "Library/Developer/Xcode/UserData/Provisioning Profiles",
    ]


# ---------------------------------------------------------------------------
# prepare


def distribution_identities(listing: str) -> dict[str, str]:
    """`security find-identity` lines for this Team's Apple Distribution identity: hash -> name."""
    found = {}
    for line in listing.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].rstrip(")").isdigit() and len(parts[1]) == 40:
            name = parts[2].strip().strip('"')
            if name.startswith("Apple Distribution:") and name.endswith(f"({TEAM_ID})"):
                found[parts[1].upper()] = name
    return found


def export_options(profile_names: dict[str, str] | str) -> dict:
    if isinstance(profile_names, str):
        profile_names = {BUNDLE_ID: profile_names}
    return {
        "method": "app-store-connect",
        "destination": "upload",
        "teamID": TEAM_ID,
        "signingStyle": "manual",
        "signingCertificate": "Apple Distribution",
        "provisioningProfiles": profile_names,
        "manageAppVersionAndBuildNumber": False,
        "uploadSymbols": True,
    }


def prepare(environment: dict[str, str], *, work_dir: Path, env_file: Path, home: Path) -> None:
    values = require_secrets(environment)
    work_dir.mkdir(parents=True, exist_ok=True)
    work_dir.chmod(0o700)

    # Identity, in a keychain of its own that only this job unlocks.
    keychain = work_dir / KEYCHAIN_NAME
    password = secrets.token_urlsafe(24)
    certificate = work_dir / "distribution.p12"
    certificate.write_bytes(decode_base64(values["IOS_DISTRIBUTION_P12_BASE64"], "IOS_DISTRIBUTION_P12_BASE64"))
    certificate.chmod(0o600)
    try:
        run(["security", "create-keychain", "-p", password, str(keychain)], redacted="security create-keychain")
        run(["security", "set-keychain-settings", "-lut", "21600", str(keychain)])
        run(["security", "unlock-keychain", "-p", password, str(keychain)], redacted="security unlock-keychain")
        run(
            ["security", "import", str(certificate), "-k", str(keychain), "-f", "pkcs12",
             "-P", values["IOS_DISTRIBUTION_P12_PASSWORD"], "-T", "/usr/bin/codesign", "-T", "/usr/bin/security"],
            redacted="security import (distribution identity)",
        )
    finally:
        certificate.unlink(missing_ok=True)
    run(
        ["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s", "-k", password, str(keychain)],
        redacted="security set-key-partition-list",
    )
    listed = run(["security", "list-keychains", "-d", "user"], capture=True).decode()
    search = [line.strip().strip('"') for line in listed.splitlines() if line.strip()]
    run(["security", "list-keychains", "-d", "user", "-s", str(keychain), *search])
    identities = distribution_identities(
        run(["security", "find-identity", "-v", "-p", "codesigning", str(keychain)], capture=True).decode()
    )
    if not identities:
        raise TestFlightError(f"IOS_DISTRIBUTION_P12_BASE64 holds no Apple Distribution identity of Team {TEAM_ID}")

    # Both targets must have real App Store profiles before archive signing.
    profile_specs = [
        (
            "app",
            BUNDLE_ID,
            "IOS_APP_STORE_PROFILE_BASE64",
            MAIN_REQUIRED_ENTITLEMENTS,
        ),
        (
            "notification-service",
            NOTIFICATION_SERVICE_BUNDLE_ID,
            "IOS_NOTIFICATION_SERVICE_PROFILE_BASE64",
            NOTIFICATION_SERVICE_REQUIRED_ENTITLEMENTS,
        ),
    ]
    profiles: dict[str, dict] = {}
    profile_uuids: dict[str, str] = {}
    for label, bundle_id, secret_name, required_entitlements in profile_specs:
        profile_path = work_dir / f"{label}.mobileprovision"
        profile_path.write_bytes(decode_base64(values[secret_name], secret_name))
        profile = decode_profile(profile_path)
        check_profile(
            profile,
            bundle_id=bundle_id,
            required_entitlements=required_entitlements,
        )
        if not profile_certificate_hashes(profile) & identities.keys():
            raise TestFlightError(
                f"{secret_name} was not issued for the imported distribution certificate"
            )
        profiles[label] = profile
        profile_uuids[label] = profile["UUID"]
        for directory in profile_directories(home):
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                profile_path,
                directory / f"{profile['UUID']}.mobileprovision",
            )

    # App Store Connect key: for the upload (xcodebuild) and the API calls here.
    key_dir = work_dir / "private_keys"
    key_dir.mkdir(mode=0o700, exist_ok=True)
    key_path = key_dir / f"AuthKey_{values['IOS_APP_STORE_CONNECT_KEY_ID']}.p8"
    key_path.write_bytes(app_store_connect_key(values["IOS_APP_STORE_CONNECT_PRIVATE_KEY"]))
    key_path.chmod(0o600)

    options_path = work_dir / "ExportOptions.plist"
    profile_names = {
        BUNDLE_ID: profiles["app"]["Name"],
        NOTIFICATION_SERVICE_BUNDLE_ID: profiles["notification-service"]["Name"],
    }
    options_path.write_bytes(plistlib.dumps(export_options(profile_names)))
    (work_dir / "profile-uuids").write_text(
        json.dumps(profile_uuids, sort_keys=True)
    )

    with env_file.open("a", encoding="utf-8") as output:
        for name, value in {
            "IOS_SIGNING_KEYCHAIN": str(keychain),
            "IOS_PROFILE_NAME": profiles["app"]["Name"],
            "IOS_PROFILE_UUID": profile_uuids["app"],
            "IOS_NOTIFICATION_SERVICE_PROFILE_NAME": profiles["notification-service"]["Name"],
            "IOS_NOTIFICATION_SERVICE_PROFILE_UUID": profile_uuids["notification-service"],
            "IOS_EXPORT_OPTIONS": str(options_path),
            "IOS_ASC_KEY_PATH": str(key_path),
            "IOS_ASC_KEY_ID": values["IOS_APP_STORE_CONNECT_KEY_ID"],
            "IOS_ASC_ISSUER_ID": values["IOS_APP_STORE_CONNECT_ISSUER_ID"],
        }.items():
            output.write(f"{name}={value}\n")
    print(
        "Apple Distribution identity and App Store profiles are ready for "
        f"{BUNDLE_ID} and {NOTIFICATION_SERVICE_BUNDLE_ID}.",
        flush=True,
    )


# ---------------------------------------------------------------------------
# verify-archive


def archived_app(archive: Path) -> Path:
    apps = list((archive / "Products/Applications").glob("*.app"))
    if len(apps) != 1:
        raise TestFlightError(f"Expected one app in {archive}, found {len(apps)}")
    return apps[0]


def archived_notification_service_extensions(app: Path) -> list[Path]:
    extensions = sorted(app.rglob("*.appex"))
    extension_ids = [
        plistlib.loads((extension / "Info.plist").read_bytes()).get("CFBundleIdentifier")
        for extension in extensions
    ]
    if set(extension_ids) != {NOTIFICATION_SERVICE_BUNDLE_ID} or len(extensions) != 1:
        raise TestFlightError(
            "The archive must contain exactly one "
            f"{NOTIFICATION_SERVICE_BUNDLE_ID} extension, found {extension_ids}"
        )
    return extensions


def check_signed_bundle(
    info: dict,
    entitlements: dict,
    *,
    bundle_id: str,
    application_identifier: str,
    required_entitlements: dict[str, list[str]],
    build: str,
    require_encryption_declaration: bool = False,
) -> None:
    if info.get("CFBundleIdentifier") != bundle_id:
        raise TestFlightError(
            f"The archived bundle is {info.get('CFBundleIdentifier')!r}, not {bundle_id}"
        )
    if info.get("CFBundleVersion") != build:
        raise TestFlightError(f"The archived build number is {info.get('CFBundleVersion')!r}, not {build}")
    if require_encryption_declaration and info.get("ITSAppUsesNonExemptEncryption") is not False:
        raise TestFlightError("Info.plist must declare ITSAppUsesNonExemptEncryption = false")
    if entitlements.get("application-identifier") != application_identifier:
        raise TestFlightError("The bundle is not signed for " + application_identifier)
    if entitlements.get("get-task-allow"):
        raise TestFlightError("The bundle is signed with get-task-allow; that is a development signature")
    forbidden = set(FORBIDDEN_ENTITLEMENTS)
    if bundle_id == NOTIFICATION_SERVICE_BUNDLE_ID:
        forbidden.add("aps-environment")
    present_forbidden = sorted(forbidden.intersection(entitlements))
    if present_forbidden:
        raise TestFlightError(
            "The signed bundle contains forbidden entitlements: "
            + ", ".join(present_forbidden)
        )
    for key, required in required_entitlements.items():
        actual = entitlements.get(key, [])
        if key == "aps-environment" and actual != "production":
            raise TestFlightError("The signed app must carry aps-environment=production")
        actual_values = {actual} if isinstance(actual, str) else set(actual)
        if key == "keychain-access-groups" and actual_values != set(required):
            raise TestFlightError(
                "The bundle must carry exactly the required "
                "keychain-access-groups values"
            )
        if (
            key == "com.apple.developer.associated-domains"
            and actual_values != set(required)
        ):
            raise TestFlightError(
                "The bundle must carry exactly the required "
                f"{key} values {required}"
            )
        if not set(required) <= actual_values:
            raise TestFlightError(f"The bundle's {key} entitlement lacks {required}")


def check_signed_app(info: dict, entitlements: dict, *, build: str) -> None:
    check_signed_bundle(
        info,
        entitlements,
        bundle_id=BUNDLE_ID,
        application_identifier=APPLICATION_IDENTIFIER,
        required_entitlements=MAIN_REQUIRED_ENTITLEMENTS,
        build=build,
        require_encryption_declaration=True,
    )


def verify_archive(archive: Path, *, build: str) -> dict:
    app = archived_app(archive)
    extensions = archived_notification_service_extensions(app)
    bundles = [
        (
            app,
            BUNDLE_ID,
            APPLICATION_IDENTIFIER,
            MAIN_REQUIRED_ENTITLEMENTS,
            True,
        ),
        (
            extensions[0],
            NOTIFICATION_SERVICE_BUNDLE_ID,
            NOTIFICATION_SERVICE_APPLICATION_IDENTIFIER,
            NOTIFICATION_SERVICE_REQUIRED_ENTITLEMENTS,
            False,
        ),
    ]
    for bundle, bundle_id, application_identifier, required, is_main in bundles:
        info = plistlib.loads((bundle / "Info.plist").read_bytes())
        entitlements = plistlib.loads(
            run(
                ["codesign", "-d", "--entitlements", "-", "--xml", str(bundle)],
                capture=True,
            )
            or plistlib.dumps({})
        )
        check_signed_bundle(
            info,
            entitlements,
            bundle_id=bundle_id,
            application_identifier=application_identifier,
            required_entitlements=required,
            build=build,
            require_encryption_declaration=is_main,
        )
        details = subprocess.run(
            ["codesign", "-dvv", str(bundle)],
            capture_output=True,
            text=True,
        ).stderr
        if (
            f"TeamIdentifier={TEAM_ID}" not in details
            or "Authority=Apple Distribution:" not in details
        ):
            raise TestFlightError(
                f"{bundle_id} is not signed by this Team's Apple Distribution identity"
            )
    run(["codesign", "--verify", "--deep", "--strict", str(app)])
    version = plistlib.loads((app / "Info.plist").read_bytes())["CFBundleShortVersionString"]
    print(
        f"Archived {BUNDLE_ID} with {NOTIFICATION_SERVICE_BUNDLE_ID} "
        f"{version} ({build}) is signed for App Store distribution.",
        flush=True,
    )
    return {"version": version, "build": build}


# ---------------------------------------------------------------------------
# App Store Connect API


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def der_signature_to_raw(der: bytes, size: int = 32) -> bytes:
    """An ECDSA DER signature (SEQUENCE of two INTEGERs) as JWS's fixed-size r || s."""

    def read_length(data: bytes, index: int) -> tuple[int, int]:
        length = data[index]
        index += 1
        if length & 0x80:
            count = length & 0x7F
            length = int.from_bytes(data[index:index + count], "big")
            index += count
        return length, index

    if not der or der[0] != 0x30:
        raise TestFlightError("Unexpected ECDSA signature encoding")
    length, index = read_length(der, 1)
    if index + length != len(der):
        raise TestFlightError("Unexpected ECDSA signature length")
    parts = []
    for _ in range(2):
        if der[index] != 0x02:
            raise TestFlightError("Unexpected ECDSA signature encoding")
        length, index = read_length(der, index + 1)
        value = der[index:index + length].lstrip(b"\x00")
        if len(value) > size:
            raise TestFlightError("Unexpected ECDSA signature size")
        parts.append(value.rjust(size, b"\x00"))
        index += length
    if index != len(der):
        raise TestFlightError("Unexpected trailing ECDSA signature data")
    return b"".join(parts)


def signed_token(key_path: Path, *, key_id: str, issuer_id: str, now: int | None = None) -> str:
    """An ES256 App Store Connect API token, signed by openssl with the .p8 key."""
    now = int(time.time()) if now is None else now
    header = base64url(json.dumps({"alg": "ES256", "kid": key_id, "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"iss": issuer_id, "iat": now, "exp": now + 1200, "aud": "appstoreconnect-v1"}
    payload = base64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    der = run(["openssl", "dgst", "-sha256", "-sign", str(key_path)], capture=True, data=signing_input)
    return f"{header}.{payload}.{base64url(der_signature_to_raw(der))}"


class AppStoreConnect:
    def __init__(self, key_path: Path, key_id: str, issuer_id: str):
        self.key_path, self.key_id, self.issuer_id = key_path, key_id, issuer_id

    def request(self, method: str, path: str, query: dict | None = None, body: dict | None = None) -> dict:
        url = ASC_API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        token = signed_token(self.key_path, key_id=self.key_id, issuer_id=self.issuer_id)
        request = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:800]
            raise TestFlightError(f"App Store Connect {method} {path} answered {error.code}: {detail}") from error
        return json.loads(text) if text else {}


def find_app(api: AppStoreConnect) -> str:
    apps = api.request("GET", "/v1/apps", {"filter[bundleId]": BUNDLE_ID, "fields[apps]": "bundleId"})["data"]
    matches = [app["id"] for app in apps if app["attributes"]["bundleId"] == BUNDLE_ID]
    if len(matches) != 1:
        raise TestFlightError(f"Expected one App Store Connect app for {BUNDLE_ID}, found {len(matches)}")
    return matches[0]


def wait_for_build(api: AppStoreConnect, app_id: str, *, version: str, build: str,
                   timeout: float, interval: float, sleep=time.sleep, clock=time.monotonic) -> dict:
    """The uploaded build once App Store Connect has processed it."""
    deadline = clock() + timeout
    while True:
        builds = api.request("GET", "/v1/builds", {
            "filter[app]": app_id,
            "filter[version]": build,
            "filter[preReleaseVersion.version]": version,
            "fields[builds]": "version,processingState,expired",
        })["data"]
        state = builds[0]["attributes"]["processingState"] if builds else "NOT_YET_LISTED"
        if state == "VALID":
            return builds[0]
        if state in {"FAILED", "INVALID"}:
            raise TestFlightError(f"App Store Connect rejected build {version} ({build}): {state}")
        if clock() >= deadline:
            raise TestFlightError(f"Build {version} ({build}) was still {state} after {int(timeout)}s")
        print(f"Build {version} ({build}) is {state}; checking again in {int(interval)}s.", flush=True)
        sleep(interval)


def make_available(api: AppStoreConnect, app_id: str, build_id: str) -> str:
    groups = api.request("GET", f"/v1/apps/{app_id}/betaGroups", {
        "fields[betaGroups]": "name,isInternalGroup,hasAccessToAllBuilds", "limit": "50",
    })["data"]
    group = next((item for item in groups if item["attributes"]["name"] == INTERNAL_GROUP), None)
    if group is None or not group["attributes"].get("isInternalGroup"):
        raise TestFlightError(f"No internal TestFlight group named {INTERNAL_GROUP!r}")
    if group["attributes"].get("hasAccessToAllBuilds"):
        return f"{INTERNAL_GROUP} receives every build automatically"
    api.request("POST", f"/v1/betaGroups/{group['id']}/relationships/builds",
                body={"data": [{"type": "builds", "id": build_id}]})
    return f"added to {INTERNAL_GROUP}"


def distribute(environment: dict[str, str], *, archive: Path, build: str, timeout: float, interval: float) -> None:
    info = plistlib.loads((archived_app(archive) / "Info.plist").read_bytes())
    version = info["CFBundleShortVersionString"]
    api = AppStoreConnect(Path(environment["IOS_ASC_KEY_PATH"]), environment["IOS_ASC_KEY_ID"],
                          environment["IOS_ASC_ISSUER_ID"])
    app_id = find_app(api)
    found = wait_for_build(api, app_id, version=version, build=build, timeout=timeout, interval=interval)
    outcome = make_available(api, app_id, found["id"])
    message = f"TestFlight build {version} ({build}) is ready for internal testers: {outcome}."
    print(message, flush=True)
    summary = environment.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write(f"### TestFlight\n\n{message}\n")


# ---------------------------------------------------------------------------
# cleanup


def cleanup(work_dir: Path, *, home: Path) -> None:
    keychain = work_dir / KEYCHAIN_NAME
    if keychain.exists():
        # Deleting the keychain also drops it from the search list.
        subprocess.run(["security", "delete-keychain", str(keychain)], check=False)
    uuids_file = work_dir / "profile-uuids"
    if uuids_file.exists():
        uuids = json.loads(uuids_file.read_text())
        for directory in profile_directories(home):
            for uuid in uuids.values():
                (directory / f"{uuid}.mobileprovision").unlink(missing_ok=True)
    shutil.rmtree(work_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--work-dir", type=Path, required=True)
    prepare_parser.add_argument("--env-file", type=Path, required=True)
    verify_parser = commands.add_parser("verify-archive")
    verify_parser.add_argument("--archive", type=Path, required=True)
    verify_parser.add_argument("--build", required=True)
    distribute_parser = commands.add_parser("distribute")
    distribute_parser.add_argument("--archive", type=Path, required=True)
    distribute_parser.add_argument("--build", required=True)
    distribute_parser.add_argument("--timeout", type=float, default=3600)
    distribute_parser.add_argument("--interval", type=float, default=30)
    cleanup_parser = commands.add_parser("cleanup")
    cleanup_parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    home = Path.home()
    try:
        if args.command == "prepare":
            prepare(dict(os.environ), work_dir=args.work_dir, env_file=args.env_file, home=home)
        elif args.command == "verify-archive":
            verify_archive(args.archive, build=args.build)
        elif args.command == "distribute":
            distribute(dict(os.environ), archive=args.archive, build=args.build,
                       timeout=args.timeout, interval=args.interval)
        else:
            cleanup(args.work_dir, home=home)
    except TestFlightError as error:
        print(f"::error::{error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
