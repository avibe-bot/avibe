from __future__ import annotations

import base64
import json
import os
import plistlib
import stat
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop" / "scripts"))
import macos_signing as signing  # noqa: E402


# Stand-ins for DER certificates: only their fingerprints are compared.
CERTIFICATE = b"fixture Developer ID Application certificate"
OTHER_CERTIFICATE = b"fixture certificate of another Developer ID identity"


def profile(**changes) -> dict:
    """The payload of a Developer ID profile for the desktop app, shaped like
    the one Apple issues; a None value removes the key."""

    value = {
        "TeamIdentifier": ["68B2SQ6YK3"],
        "Platform": ["OSX"],
        "ProvisionsAllDevices": True,
        "ExpirationDate": datetime(2044, 11, 11, 9, 49, 28),
        "DeveloperCertificates": [OTHER_CERTIFICATE, CERTIFICATE],
        "Entitlements": {
            "com.apple.application-identifier": "68B2SQ6YK3.bot.avibe.desktop",
            "com.apple.developer.team-identifier": "68B2SQ6YK3",
            "com.apple.developer.associated-domains": "*",
            "keychain-access-groups": ["68B2SQ6YK3.*"],
        },
    }
    for key, change in changes.items():
        if change is None:
            value.pop(key)
        else:
            value[key] = change
    return value


def granting(**entitlements) -> dict:
    granted = {**profile()["Entitlements"], **entitlements}
    return {key: item for key, item in granted.items() if item is not None}


PROFILE_BYTES = plistlib.dumps(profile())

COMPLETE = {
    "APPLE_CERTIFICATE": "Y2VydGlmaWNhdGU=",
    "APPLE_CERTIFICATE_PASSWORD": "certificate-password",
    "APPLE_SIGNING_IDENTITY": f"Developer ID Application: Example Owner ({signing.TEAM_ID})",
    "APPLE_TEAM_ID": signing.TEAM_ID,
    "APPLE_API_KEY": "ABCDE12345",
    "APPLE_API_ISSUER": "01234567-89ab-cdef-0123-456789abcdef",
    "APPLE_API_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n",
    "APPLE_PROVISIONING_PROFILE": base64.b64encode(PROFILE_BYTES).decode(),
}


def fake_tools(tmp_path: Path, monkeypatch, *, leaf: bytes = CERTIFICATE) -> Path:
    """`security` and `codesign` stand-ins on PATH that record their arguments.

    The identity in the keychain is CERTIFICATE; a signature's leaf is `leaf`.
    A test profile is its bare property list, so `cms -D` prints the file.
    """

    calls = tmp_path / "calls.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = f"open({str(calls)!r}, 'a').write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + '\\n')\n"
    (bin_dir / "security").write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        + record
        + "if sys.argv[1] == 'list-keychains' and '-s' not in sys.argv:\n"
        "    print('    \"/Users/runner/Library/Keychains/login.keychain-db\"')\n"
        "if sys.argv[1] == 'find-identity':\n"
        f"    print('  1) {signing.certificate_fingerprint(CERTIFICATE)} \"{COMPLETE['APPLE_SIGNING_IDENTITY']}\"')\n"
        f"    print('  2) {signing.certificate_fingerprint(OTHER_CERTIFICATE)} \"Developer ID Application: Other (ABCDE12345)\"')\n"
        "    print('     2 valid identities found')\n"
        "if sys.argv[1] == 'cms':\n"
        "    sys.stdout.buffer.write(open(sys.argv[sys.argv.index('-i') + 1], 'rb').read())\n"
    )
    (bin_dir / "codesign").write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        + record
        + "for argument in sys.argv:\n"
        "    if argument.startswith('--extract-certificates='):\n"
        f"        open(argument.split('=', 1)[1] + '0', 'wb').write({leaf!r})\n"
    )
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return calls


def test_release_code_identity_is_the_pinned_developer_id_requirement():
    # TCC grants and Keychain ACLs bind to exactly this text; it must never drift.
    assert signing.designated_requirement(signing.APP_IDENTIFIER) == (
        'designated => identifier "bot.avibe.desktop" and anchor apple generic and '
        "certificate 1[field.1.2.840.113635.100.6.2.6] /* exists */ and "
        "certificate leaf[field.1.2.840.113635.100.6.1.13] /* exists */ and "
        'certificate leaf[subject.OU] = "68B2SQ6YK3"'
    )
    config = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    assert config["identifier"] == signing.APP_IDENTIFIER
    assert signing.HELPER_IDENTIFIER == "bot.avibe.desktop.cua-driver"


def test_each_binary_carries_only_its_evidenced_entitlements():
    def keys(path):
        return set(plistlib.loads(path.read_bytes()))

    assert keys(signing.PYTHON_ENTITLEMENTS["aarch64"]) == set()
    assert keys(signing.PYTHON_ENTITLEMENTS["x86_64"]) == {
        "com.apple.security.cs.allow-unsigned-executable-memory",
    }
    assert keys(signing.HELPER_ENTITLEMENTS) == set()
    assert keys(signing.NODE_ENTITLEMENTS["aarch64"]) == {
        "com.apple.security.cs.allow-jit",
        "com.apple.security.cs.disable-library-validation",
    }
    assert keys(signing.NODE_ENTITLEMENTS["x86_64"]) == {
        "com.apple.security.cs.allow-unsigned-executable-memory",
        "com.apple.security.cs.disable-library-validation",
    }


@pytest.mark.parametrize(
    ("tag", "environment", "error"),
    [
        ("", {}, None),
        ("gh-v3.2.4rc1", {}, "Release builds require the complete Apple credential set"),
        ("v3.2.4", {}, "Release builds require the complete Apple credential set"),
        ("", {**COMPLETE, "APPLE_TEAM_ID": ""}, "missing: APPLE_TEAM_ID"),
        ("gh-v3.2.4rc1", {**COMPLETE, "APPLE_API_PRIVATE_KEY": ""}, "missing: APPLE_API_PRIVATE_KEY"),
        ("gh-v3.2.4rc1", {**COMPLETE, "APPLE_TEAM_ID": "ABCDE12345"}, "pinned desktop Team"),
        (
            "gh-v3.2.4rc1",
            {**COMPLETE, "APPLE_SIGNING_IDENTITY": "Developer ID Application: Someone Else (ABCDE12345)"},
            "APPLE_SIGNING_IDENTITY",
        ),
        ("gh-v3.2.4rc1", {**COMPLETE, "APPLE_SIGNING_IDENTITY": "Apple Development: x (68B2SQ6YK3)"}, "APPLE_SIGNING_IDENTITY"),
        ("gh-v3.2.4rc1", {**COMPLETE, "APPLE_API_PRIVATE_KEY": "not a key"}, "raw .p8"),
        # A Developer ID app is never signed without its profile.
        ("gh-v3.2.4rc1", {**COMPLETE, "APPLE_PROVISIONING_PROFILE": ""}, "missing: APPLE_PROVISIONING_PROFILE"),
        ("", {**COMPLETE, "APPLE_PROVISIONING_PROFILE": ""}, "partially configured set fails.*APPLE_PROVISIONING_PROFILE"),
    ],
)
def test_releases_fail_closed_and_manual_builds_accept_all_or_nothing(tmp_path, tag, environment, error):
    env_file = tmp_path / "env"
    if error is None:
        signing.import_identity(environment, release_tag=tag, env_file=env_file, work_dir=tmp_path / "work")
        assert env_file.read_text() == "apple_signing=absent\n"
        assert not (tmp_path / "work").exists()
        return
    with pytest.raises(signing.SigningError, match=error):
        signing.import_identity(environment, release_tag=tag, env_file=env_file, work_dir=tmp_path / "work")
    assert not env_file.exists()


@pytest.mark.skipif(os.name == "nt", reason="fake security tool is a POSIX script")
def test_import_exports_locations_only_and_leaves_no_certificate(tmp_path, monkeypatch):
    calls = fake_tools(tmp_path, monkeypatch)
    env_file, work = tmp_path / "env", tmp_path / "work"

    signing.import_identity(COMPLETE, release_tag="gh-v3.2.4rc1", env_file=env_file, work_dir=work)

    exported = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    assert exported == {
        "apple_signing": "enabled",
        "AVIBE_CODESIGN_IDENTITY": COMPLETE["APPLE_SIGNING_IDENTITY"],
        "AVIBE_CODESIGN_KEYCHAIN": str(work / signing.KEYCHAIN_NAME),
        "AVIBE_PROVISIONING_PROFILE": str(work / "embedded.provisionprofile"),
        "AVIBE_NOTARY_KEY_PATH": str(work / "AuthKey_ABCDE12345.p8"),
        "AVIBE_NOTARY_KEY_ID": COMPLETE["APPLE_API_KEY"],
        "AVIBE_NOTARY_ISSUER": COMPLETE["APPLE_API_ISSUER"],
    }
    key = Path(exported["AVIBE_NOTARY_KEY_PATH"])
    assert key.read_text() == COMPLETE["APPLE_API_PRIVATE_KEY"]
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert Path(exported["AVIBE_PROVISIONING_PROFILE"]).read_bytes() == PROFILE_BYTES
    assert sorted(path.name for path in work.iterdir()) == ["AuthKey_ABCDE12345.p8", "embedded.provisionprofile"]
    commands = [json.loads(line)[1:] for line in calls.read_text().splitlines()]
    assert [command[0] for command in commands] == [
        "create-keychain", "set-keychain-settings", "unlock-keychain", "import",
        "set-key-partition-list", "list-keychains", "list-keychains", "find-identity", "cms",
    ]
    assert commands[6][-2:] == [str(work / signing.KEYCHAIN_NAME), "/Users/runner/Library/Keychains/login.keychain-db"]


@pytest.mark.skipif(os.name == "nt", reason="fake security tool is a POSIX script")
@pytest.mark.parametrize(
    ("secret", "error"),
    [
        ("not base64!", "APPLE_PROVISIONING_PROFILE must be a base64"),
        (base64.b64encode(b"not a profile").decode(), "not a readable provisioning profile"),
        (
            base64.b64encode(plistlib.dumps(profile(DeveloperCertificates=[OTHER_CERTIFICATE]))).decode(),
            "does not list the signing certificate",
        ),
    ],
)
def test_import_refuses_a_profile_that_does_not_fit_the_imported_identity(tmp_path, monkeypatch, secret, error):
    fake_tools(tmp_path, monkeypatch)
    env_file = tmp_path / "env"

    with pytest.raises(signing.SigningError, match=error):
        signing.import_identity(
            {**COMPLETE, "APPLE_PROVISIONING_PROFILE": secret},
            release_tag="gh-v3.2.4rc1", env_file=env_file, work_dir=tmp_path / "work",
        )
    assert not env_file.exists()


EXPECTED_ADDED_ENTITLEMENTS = {
    "com.apple.application-identifier": "68B2SQ6YK3.bot.avibe.desktop",
    "com.apple.developer.team-identifier": "68B2SQ6YK3",
    "com.apple.developer.associated-domains": ["webcredentials:sandbox.avibe.bot", "webcredentials:avibe.bot"],
}


def test_only_the_developer_id_app_signature_claims_profile_entitlements():
    # Tauri seals every bundle with bundle.macOS.entitlements, the release
    # override never replaces it, and the ad-hoc paths preserve that seal's
    # entitlements. A build without a profile that claimed a restricted key
    # would be killed at launch, so that file holds none of them.
    config = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    assert ROOT / "desktop/src-tauri" / config["bundle"]["macOS"]["entitlements"] == signing.BUNDLE_ENTITLEMENTS
    bundle = plistlib.loads(signing.BUNDLE_ENTITLEMENTS.read_bytes())
    assert bundle == {"com.apple.security.device.audio-input": True}
    # The Developer ID signature adds exactly the keys the profile authorizes.
    assert plistlib.loads(signing.APP_ENTITLEMENTS.read_bytes()) == {**bundle, **EXPECTED_ADDED_ENTITLEMENTS}
    assert signing.APP_ENTITLEMENTS != signing.BUNDLE_ENTITLEMENTS


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (profile(TeamIdentifier=["ABCDE12345"]), "not issued to Team 68B2SQ6YK3"),
        (
            profile(Entitlements=granting(**{"com.apple.application-identifier": "68B2SQ6YK3.bot.avibe.ios"})),
            "not for the application identifier 68B2SQ6YK3.bot.avibe.desktop",
        ),
        (
            profile(Entitlements=granting(**{"com.apple.application-identifier": "68B2SQ6YK3.*"})),
            "not for the application identifier",
        ),
        (profile(Entitlements=granting(**{"com.apple.developer.team-identifier": "ABCDE12345"})), "team-identifier"),
        # Development and other device-listing profiles do not run on every Mac.
        (profile(ProvisionsAllDevices=None, ProvisionedDevices=["00008103-0000000000000000"]), "Developer ID"),
        (profile(ProvisionsAllDevices=False), "Developer ID"),
        (profile(ProvisionedDevices=["00008103-0000000000000000"]), "Developer ID"),
        (profile(Platform=["iOS"]), "Developer ID"),
        (profile(Entitlements=granting(**{"com.apple.developer.associated-domains": None})), "associated-domains"),
        (
            profile(Entitlements=granting(**{"com.apple.developer.associated-domains": ["webcredentials:avibe.bot"]})),
            "associated-domains",
        ),
        (profile(ExpirationDate=datetime(2026, 11, 1)), "expired on 2026-11-01"),
        (profile(ExpirationDate=None), "no expiration date"),
        (profile(DeveloperCertificates=[OTHER_CERTIFICATE]), "does not list the signing certificate"),
    ],
)
def test_a_profile_that_would_not_launch_the_app_is_refused(payload, error):
    now = datetime(2026, 11, 16, tzinfo=timezone.utc)
    with pytest.raises(signing.SigningError, match=error):
        signing.check_profile(payload, signing.certificate_fingerprint(CERTIFICATE), now=now)


@pytest.mark.parametrize(
    "domains", ["*", ["webcredentials:sandbox.avibe.bot", "webcredentials:avibe.bot", "applinks:avibe.bot"]]
)
def test_a_developer_id_profile_for_the_app_and_its_certificate_is_accepted(domains):
    payload = profile(Entitlements=granting(**{"com.apple.developer.associated-domains": domains}))
    signing.check_profile(
        payload, signing.certificate_fingerprint(CERTIFICATE), now=datetime(2026, 11, 16, tzinfo=timezone.utc)
    )


def app_bundle(root: Path) -> Path:
    app = root / "Avibe.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Helpers").mkdir()
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "bot.avibe.desktop", "CFBundleExecutable": "avibe-desktop"})
    )
    (app / "Contents/MacOS/avibe-desktop").write_bytes(thin(signing.MH_EXECUTE))
    (app / "Contents/Helpers/cua-driver").write_bytes(thin(signing.MH_EXECUTE))
    return app


@pytest.mark.skipif(os.name == "nt", reason="fake security tool is a POSIX script")
def test_sign_app_refuses_an_unfit_profile_before_signing_anything(tmp_path, monkeypatch):
    fake_tools(tmp_path, monkeypatch)
    app = app_bundle(tmp_path)
    unfit = tmp_path / "unfit.provisionprofile"
    unfit.write_bytes(plistlib.dumps(profile(DeveloperCertificates=[OTHER_CERTIFICATE])))
    monkeypatch.setattr(signing, "codesign", lambda *_args, **_kwargs: pytest.fail("nothing may be signed"))

    with pytest.raises(signing.SigningError, match="does not list the signing certificate"):
        signing.sign_app(app, identity=COMPLETE["APPLE_SIGNING_IDENTITY"], keychain="avibe.keychain-db", profile=unfit)
    assert not (app / "Contents/embedded.provisionprofile").exists()


@pytest.mark.skipif(os.name == "nt", reason="fake security tool is a POSIX script")
def test_sign_app_embeds_the_profile_in_the_app_alone_before_sealing_it(tmp_path, monkeypatch):
    calls = fake_tools(tmp_path, monkeypatch)
    app = app_bundle(tmp_path)
    source = tmp_path / "embedded.provisionprofile"
    source.write_bytes(PROFILE_BYTES)
    signed, verified = [], []

    def record(command, *, label):
        embedded = app / "Contents/embedded.provisionprofile"
        signed.append((command, embedded.read_bytes() if embedded.exists() else None))

    monkeypatch.setattr(signing, "codesign", record)
    monkeypatch.setattr(signing, "verify_app", lambda *args: verified.append(args))

    signing.sign_app(app, identity=COMPLETE["APPLE_SIGNING_IDENTITY"], keychain="avibe.keychain-db", profile=source)

    (helper, _), (outer, sealed_profile) = signed
    assert helper[-1] == str(app / "Contents/Helpers/cua-driver")
    assert helper[helper.index("--entitlements") + 1] == str(signing.HELPER_ENTITLEMENTS)
    assert outer[-1] == str(app)
    assert outer[outer.index("--entitlements") + 1] == str(signing.APP_ENTITLEMENTS)
    assert sealed_profile == PROFILE_BYTES
    assert verified == [(app, source)]
    # The identity checked against the profile is the one codesign resolves in this keychain.
    find_identity = [json.loads(line) for line in calls.read_text().splitlines() if '"find-identity"' in line]
    assert find_identity == [["security", "find-identity", "-v", "-p", "codesigning", "avibe.keychain-db"]]


@pytest.mark.skipif(os.name == "nt", reason="fake codesign tool is a POSIX script")
@pytest.mark.parametrize(
    ("embedded", "leaf", "error"),
    [
        (PROFILE_BYTES, CERTIFICATE, None),
        (None, CERTIFICATE, "missing Contents/embedded.provisionprofile"),
        (plistlib.dumps(profile(TeamIdentifier=["68B2SQ6YK3"], UUID="other")), CERTIFICATE, "differs"),
        # Signed by an identity the profile does not list.
        (PROFILE_BYTES, b"fixture certificate of a third identity", "does not list the signing certificate"),
    ],
)
def test_verification_requires_the_exact_profile_for_the_actual_signing_certificate(
    tmp_path, monkeypatch, embedded, leaf, error
):
    fake_tools(tmp_path, monkeypatch, leaf=leaf)
    app = app_bundle(tmp_path)
    source = tmp_path / "input.provisionprofile"
    source.write_bytes(PROFILE_BYTES)
    if embedded is not None:
        (app / "Contents/embedded.provisionprofile").write_bytes(embedded)

    if error is None:
        signing.verify_embedded_profile(app, source)
    else:
        with pytest.raises(signing.SigningError, match=error):
            signing.verify_embedded_profile(app, source)


def thin(filetype: int) -> bytes:
    return struct.pack("<IIIII", 0xFEEDFACF, 0x0100000C, 0, filetype, 0) + bytes(12)


def fat(filetype: int) -> bytes:
    header = struct.pack(">II", 0xCAFEBABE, 2)
    header += struct.pack(">IIIII", 0x01000007, 3, 4096, 32, 12)
    header += struct.pack(">IIIII", 0x0100000C, 0, 8192, 32, 14)
    body = bytearray(header.ljust(8192 + 32, b"\0"))
    body[4096 : 4096 + 32] = thin(filetype)
    body[8192 : 8192 + 32] = thin(filetype)
    return bytes(body)


def test_every_thin_and_universal_mach_o_is_found_and_nothing_else(tmp_path):
    files = {
        "python/bin/python3.12": thin(signing.MH_EXECUTE),
        "python/lib/libpython3.12.dylib": thin(signing.MH_DYLIB),
        "python/lib/site-packages/_native.so": fat(signing.MH_BUNDLE),
        "python/lib/site-packages/Archive.class": struct.pack(">IHH", 0xCAFEBABE, 0, 52) + bytes(24),
        "python/lib/libstub.a": b"!<arch>\n" + bytes(32),
        # A provisioning profile is DER-encoded CMS, never nested code.
        "Contents/embedded.provisionprofile": b"\x30\x82\x30\xa2" + bytes(32),
        "tools/npm/bin/npm-cli.js": b"#!/usr/bin/env node\n" + bytes(32),
        "empty": b"",
    }
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (tmp_path / "python/bin/python3").symlink_to("python3.12")

    found = {path.relative_to(tmp_path).as_posix(): filetype for path, filetype in signing.macho_files(tmp_path)}

    assert found == {
        "python/bin/python3.12": signing.MH_EXECUTE,
        "python/lib/libpython3.12.dylib": signing.MH_DYLIB,
        "python/lib/site-packages/_native.so": signing.MH_BUNDLE,
    }


def test_an_unreviewed_runtime_executable_stops_signing_before_any_codesign(tmp_path, monkeypatch):
    for relative, filetype in {
        "python/bin/python3.12": signing.MH_EXECUTE,
        "tools/bin/node": signing.MH_EXECUTE,
        "python/lib/libpython3.12.dylib": signing.MH_DYLIB,
        "python/lib/site-packages/vendor/bin/helper": signing.MH_EXECUTE,
    }.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(thin(filetype))
    (tmp_path / "python/bin/python3").symlink_to("python3.12")
    monkeypatch.setattr(signing, "run", lambda *_args, **_kwargs: pytest.fail("nothing may be signed"))

    with pytest.raises(signing.SigningError, match="vendor/bin/helper"):
        signing.sign_runtime(
            tmp_path,
            arch="aarch64",
            python_executable=tmp_path / "python/bin/python3",
            node_executable=tmp_path / "tools/bin/node",
            identity=COMPLETE["APPLE_SIGNING_IDENTITY"],
        )


def _stapler_run(outcomes, calls):
    def fake_run(command, *, redacted=None, capture=False):
        calls.append(command)
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise signing.SigningError(f"{redacted} exited 68\n{outcome}")
    return fake_run


NETWORK_LOST = 'error is Error Domain=NSURLErrorDomain Code=-1005 "The network connection was lost."'


def test_stapler_rides_out_a_lasting_cloudkit_connection_drop(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    sleeps: list[float] = []
    # Three drops in a row, as gh-v3.2.3rc12 hit, then the ticket arrives.
    monkeypatch.setattr(signing, "run", _stapler_run([NETWORK_LOST, NETWORK_LOST, NETWORK_LOST, None], calls))
    monkeypatch.setattr(signing.time, "sleep", sleeps.append)

    signing.stapler("staple", tmp_path / "Avibe.dmg")

    assert len(calls) == 4
    assert all(command[:3] == ["xcrun", "stapler", "staple"] for command in calls)
    assert sleeps == list(signing.STAPLER_RETRY_DELAYS_SECONDS[:3])


def test_stapler_gives_up_after_its_backoff_and_reports_the_last_error(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    sleeps: list[float] = []
    monkeypatch.setattr(signing, "run", _stapler_run([NETWORK_LOST] * signing.STAPLER_ATTEMPTS, calls))
    monkeypatch.setattr(signing.time, "sleep", sleeps.append)

    with pytest.raises(signing.SigningError, match="NSURLErrorDomain"):
        signing.stapler("validate", tmp_path / "Avibe.app")

    assert len(calls) == signing.STAPLER_ATTEMPTS
    assert sleeps == list(signing.STAPLER_RETRY_DELAYS_SECONDS)
    assert sum(sleeps) >= 300


def test_stapler_does_not_retry_a_failure_that_is_not_a_network_drop(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(signing, "run", _stapler_run(["Could not validate ticket"], calls))
    monkeypatch.setattr(signing.time, "sleep", lambda _seconds: pytest.fail("a non-network failure must not wait"))

    with pytest.raises(signing.SigningError, match="Could not validate ticket"):
        signing.stapler("staple", tmp_path / "Avibe.dmg")

    assert len(calls) == 1


SUBMISSION_ID = "a8a6a8f9-ab9f-42aa-add9-cf84f4928a06"
UPLOADED = {"stdout": {"id": SUBMISSION_ID, "message": "Successfully uploaded file"}}
ACCEPTED = {"stdout": {"id": SUBMISSION_ID, "status": "Accepted", "message": "Processing complete"}}
INVALID = {"stdout": {"id": SUBMISSION_ID, "status": "Invalid", "message": "Processing complete"}}
# What gh-v3.2.6rc4 printed when a status poll of its uploaded submission timed out.
TIMED_OUT = {
    "stderr": 'Error: HTTPError(statusCode: nil, error: Error Domain=NSURLErrorDomain Code=-1001 "The request '
    'timed out." UserInfo={NSErrorFailingURLStringKey=https://appstoreconnect.apple.com/notary/v2/submissions/'
    f'{SUBMISSION_ID}?, NSLocalizedDescription=The request timed out.}})',
}
CONNECTION_LOST = {
    "stderr": 'Error: HTTPError(statusCode: nil, error: Error Domain=NSURLErrorDomain Code=-1005 "The network '
    'connection was lost." UserInfo={NSErrorFailingURLStringKey=https://appstoreconnect.apple.com/notary/v2/submissions})',
}
SERVER_ERROR = {"stderr": "Error: HTTPError(statusCode: Optional(503), error: nil)"}
UNAUTHORIZED = {"stderr": "Error: HTTPError(statusCode: Optional(401), error: nil)"}


@pytest.mark.skipif(os.name == "nt", reason="fake xcrun is a POSIX script")
@pytest.mark.parametrize(
    ("submits", "waits", "error", "sleeps"),
    [
        # The upload has an id, so a lost status poll waits on it again.
        ([UPLOADED], [TIMED_OUT, ACCEPTED], None, [30]),
        # An upload that never returned an id is submitted again.
        ([CONNECTION_LOST, SERVER_ERROR, UPLOADED], [ACCEPTED], None, [30, 60]),
        ([UPLOADED], [INVALID], "ended 'Invalid', not Accepted", []),
        ([UPLOADED], [TIMED_OUT, TIMED_OUT, TIMED_OUT], "NSURLErrorDomain Code=-1001", [30, 60]),
        ([UNAUTHORIZED], [], r"Optional\(401\)", []),
    ],
)
def test_notarization_retries_only_lost_connections_and_never_resubmits_an_uploaded_build(
    tmp_path, monkeypatch, submits, waits, error, sleeps
):
    outcomes, calls = tmp_path / "outcomes.json", tmp_path / "calls.jsonl"
    outcomes.write_text(json.dumps({"submit": submits, "wait": waits}))
    xcrun = tmp_path / "bin" / "xcrun"
    xcrun.parent.mkdir()
    xcrun.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "tool, command = sys.argv[1], sys.argv[2]\n"
        "if tool == 'notarytool' and command in ('submit', 'wait'):\n"
        f"    queued = json.load(open({str(outcomes)!r}))\n"
        "    outcome = queued[command].pop(0)\n"
        f"    json.dump(queued, open({str(outcomes)!r}, 'w'))\n"
        "    if 'stdout' in outcome:\n"
        "        print(json.dumps(outcome['stdout']))\n"
        "        sys.exit(0)\n"
        "    print(outcome['stderr'], file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "if tool == 'notarytool' and command == 'log':\n"
        "    print(json.dumps({'issues': None}))\n"
    )
    xcrun.chmod(0o755)
    monkeypatch.setenv("PATH", f"{xcrun.parent}{os.pathsep}{os.environ['PATH']}")
    waited: list[float] = []
    monkeypatch.setattr(signing.time, "sleep", waited.append)
    dmg = tmp_path / "Avibe.dmg"
    dmg.write_bytes(b"dmg")

    if error is None:
        signing.notarize(dmg, key_path="AuthKey.p8", key_id="ABCDE12345", issuer=COMPLETE["APPLE_API_ISSUER"])
    else:
        with pytest.raises(signing.SigningError, match=error):
            signing.notarize(dmg, key_path="AuthKey.p8", key_id="ABCDE12345", issuer=COMPLETE["APPLE_API_ISSUER"])

    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    assert json.loads(outcomes.read_text()) == {"submit": [], "wait": []}
    waits_run = [command for command in commands if command[:2] == ["notarytool", "wait"]]
    assert [command[2] for command in waits_run] == [SUBMISSION_ID] * len(waits)
    assert all(command[command.index("--timeout") + 1] == "2h" for command in waits_run)
    assert waited == sleeps
    stapled = [command[1:3] for command in commands if command[0] == "stapler"]
    assert stapled == ([] if error else [["staple", str(dmg)], ["validate", str(dmg)]])


def test_every_app_verification_checks_the_embedded_profile(tmp_path, monkeypatch):
    checked, profiles = [], []
    monkeypatch.setattr(signing, "verify_code", lambda path, **kwargs: checked.append((path, kwargs.get("entitlements"))))
    monkeypatch.setattr(signing, "verify_embedded_profile", lambda *args: profiles.append(args))
    app, profile_path = tmp_path / "Avibe.app", tmp_path / "embedded.provisionprofile"

    signing.verify_app(app, profile_path)

    assert checked == [
        (app / "Contents/Helpers/cua-driver", signing.HELPER_ENTITLEMENTS),
        (app, signing.APP_ENTITLEMENTS),
    ]
    assert profiles == [(app, profile_path)]


def test_release_verification_checks_the_profile_of_the_app_and_of_the_app_inside_the_dmg(tmp_path, monkeypatch):
    verified, mounted = [], []

    def attach(command, **_kwargs):
        if command[:2] == ["hdiutil", "attach"]:
            mounted.append(Path(command[command.index("-mountpoint") + 1]))

    monkeypatch.setattr(signing, "verify_dmg_signature", lambda _dmg: None)
    monkeypatch.setattr(signing, "stapler", lambda *_args: None)
    monkeypatch.setattr(signing, "assess", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(signing, "run", attach)
    monkeypatch.setattr(signing.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(signing, "verify_app", lambda *args: verified.append(args))
    app, dmg, profile_path = tmp_path / "Avibe.app", tmp_path / "Avibe.dmg", tmp_path / "embedded.provisionprofile"

    signing.verify_release(app, dmg, profile_path)

    assert verified == [(app, profile_path), (mounted[0] / "Avibe.app", profile_path)]


@pytest.mark.parametrize("command", [["sign-app", "Avibe.app"], ["verify-release", "--app", "Avibe.app", "--dmg", "Avibe.dmg"]])
@pytest.mark.parametrize("profile_variable", [None, ""])
def test_signing_commands_without_a_profile_stop_before_any_tool_runs(tmp_path, command, profile_variable):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AVIBE_")}
    environment["AVIBE_CODESIGN_IDENTITY"] = COMPLETE["APPLE_SIGNING_IDENTITY"]
    # No macOS tool is reachable: reaching one would fail differently.
    environment["PATH"] = str(tmp_path)
    if profile_variable is not None:
        environment["AVIBE_PROVISIONING_PROFILE"] = profile_variable
    result = subprocess.run(
        [sys.executable, str(ROOT / "desktop/scripts/macos_signing.py"), *command],
        cwd=tmp_path, env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "::error::No provisioning profile: run import-identity first"
