from __future__ import annotations

import json
import os
import plistlib
import stat
import struct
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop" / "scripts"))
import macos_signing as signing  # noqa: E402


COMPLETE = {
    "APPLE_CERTIFICATE": "Y2VydGlmaWNhdGU=",
    "APPLE_CERTIFICATE_PASSWORD": "certificate-password",
    "APPLE_SIGNING_IDENTITY": f"Developer ID Application: Example Owner ({signing.TEAM_ID})",
    "APPLE_TEAM_ID": signing.TEAM_ID,
    "APPLE_API_KEY": "ABCDE12345",
    "APPLE_API_ISSUER": "01234567-89ab-cdef-0123-456789abcdef",
    "APPLE_API_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n",
}


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
    assert config["bundle"]["macOS"]["entitlements"] == signing.APP_ENTITLEMENTS.name
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
    calls = tmp_path / "calls.jsonl"
    security = tmp_path / "bin" / "security"
    security.parent.mkdir()
    security.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'list-keychains' and '-s' not in sys.argv:\n"
        "    print('    \"/Users/runner/Library/Keychains/login.keychain-db\"')\n"
        "if sys.argv[1] == 'find-identity':\n"
        f"    print('  1) 0123456789ABCDEF \"{COMPLETE['APPLE_SIGNING_IDENTITY']}\"')\n"
    )
    security.chmod(0o755)
    monkeypatch.setenv("PATH", f"{security.parent}{os.pathsep}{os.environ['PATH']}")
    env_file, work = tmp_path / "env", tmp_path / "work"

    signing.import_identity(COMPLETE, release_tag="gh-v3.2.4rc1", env_file=env_file, work_dir=work)

    exported = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    assert exported == {
        "apple_signing": "enabled",
        "AVIBE_CODESIGN_IDENTITY": COMPLETE["APPLE_SIGNING_IDENTITY"],
        "AVIBE_CODESIGN_KEYCHAIN": str(work / signing.KEYCHAIN_NAME),
        "AVIBE_NOTARY_KEY_PATH": str(work / "AuthKey_ABCDE12345.p8"),
        "AVIBE_NOTARY_KEY_ID": COMPLETE["APPLE_API_KEY"],
        "AVIBE_NOTARY_ISSUER": COMPLETE["APPLE_API_ISSUER"],
    }
    key = Path(exported["AVIBE_NOTARY_KEY_PATH"])
    assert key.read_text() == COMPLETE["APPLE_API_PRIVATE_KEY"]
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert sorted(path.name for path in work.iterdir()) == ["AuthKey_ABCDE12345.p8"]
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [command[0] for command in commands] == [
        "create-keychain", "set-keychain-settings", "unlock-keychain", "import",
        "set-key-partition-list", "list-keychains", "list-keychains", "find-identity",
    ]
    assert commands[6][-2:] == [str(work / signing.KEYCHAIN_NAME), "/Users/runner/Library/Keychains/login.keychain-db"]


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
