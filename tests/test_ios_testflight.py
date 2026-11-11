from __future__ import annotations

import base64
import json
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ios" / "scripts"))
import testflight  # noqa: E402


NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)
COMPLETE = {
    "IOS_DISTRIBUTION_P12_BASE64": "cDEy",
    "IOS_DISTRIBUTION_P12_PASSWORD": " pass word ",
    "IOS_APP_STORE_PROFILE_BASE64": "cHJvZmlsZQ==",
    "IOS_NOTIFICATION_SERVICE_PROFILE_BASE64": "bm90aWZpY2F0aW9uLXByb2ZpbGU=",
    "IOS_APPLE_TEAM_ID": testflight.TEAM_ID,
    "IOS_APP_STORE_CONNECT_KEY_ID": "ABCDE12345",
    "IOS_APP_STORE_CONNECT_ISSUER_ID": "01234567-89ab-cdef-0123-456789abcdef",
    "IOS_APP_STORE_CONNECT_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----",
}


def app_store_profile(**overrides) -> dict:
    profile = {
        "Name": "Avibe iOS App Store",
        "UUID": "11111111-2222-3333-4444-555555555555",
        "TeamIdentifier": [testflight.TEAM_ID],
        "ExpirationDate": NOW + timedelta(days=300),
        "DeveloperCertificates": [b"certificate-der"],
        "Entitlements": {
            "application-identifier": testflight.APPLICATION_IDENTIFIER,
            "aps-environment": "production",
            "com.apple.developer.associated-domains": testflight.ASSOCIATED_DOMAINS,
            "com.apple.security.application-groups": ["group.bot.avibe.ios"],
            "keychain-access-groups": [
                testflight.PRIVATE_KEYCHAIN_ACCESS_GROUP,
                testflight.SHARED_KEYCHAIN_ACCESS_GROUP,
            ],
            "get-task-allow": False,
        },
    }
    profile.update(overrides)
    return profile


# ---------------------------------------------------------------------------
# Inputs


def test_secrets_are_all_required_and_named_without_values():
    with pytest.raises(testflight.TestFlightError) as error:
        testflight.require_secrets({**COMPLETE, "IOS_DISTRIBUTION_P12_PASSWORD": "", "IOS_APP_STORE_CONNECT_KEY_ID": " "})
    assert str(error.value) == (
        "Missing repository secrets: IOS_DISTRIBUTION_P12_PASSWORD, IOS_APP_STORE_CONNECT_KEY_ID"
    )
    assert testflight.require_secrets(COMPLETE)["IOS_DISTRIBUTION_P12_PASSWORD"] == " pass word "


def test_secrets_of_another_team_are_refused():
    with pytest.raises(testflight.TestFlightError, match="Team 68B2SQ6YK3"):
        testflight.require_secrets({**COMPLETE, "IOS_APPLE_TEAM_ID": "ZZZZZZZZZZ"})


def test_the_app_store_connect_key_is_read_as_text_or_base64():
    pem = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
    assert testflight.app_store_connect_key(pem) == (pem + "\n").encode()
    assert testflight.app_store_connect_key(base64.b64encode(pem.encode()).decode()) == (pem + "\n").encode()
    with pytest.raises(testflight.TestFlightError, match="PKCS#8"):
        testflight.app_store_connect_key(base64.b64encode(b"not a key").decode())


# ---------------------------------------------------------------------------
# Profile and identity


def test_a_current_app_store_profile_for_this_app_is_accepted():
    testflight.check_profile(app_store_profile(), now=NOW)


def test_profile_keychain_grants_accept_required_coverage_and_extra_allowlist_entries():
    exact = app_store_profile()
    exact["Entitlements"]["keychain-access-groups"].append("com.apple.token")
    testflight.check_profile(exact, now=NOW)

    wildcard = app_store_profile()
    wildcard["Entitlements"]["keychain-access-groups"] = [
        f"{testflight.TEAM_ID}.*",
        "com.apple.token",
    ]
    testflight.check_profile(wildcard, now=NOW)


@pytest.mark.parametrize(
    "grants",
    [
        ["ZZZZZZZZZZ.*"],
        [f"{testflight.TEAM_ID}.bot.avibe.desktop.*"],
        [f"{testflight.TEAM_ID}.bot.*.push"],
        [f"{testflight.TEAM_ID}.bot.avibe.ios*"],
        [f"{testflight.TEAM_ID}.bot.avibe.ios.push.*"],
        [testflight.PRIVATE_KEYCHAIN_ACCESS_GROUP, 7],
        {testflight.PRIVATE_KEYCHAIN_ACCESS_GROUP: True},
    ],
    ids=[
        "unrelated-team",
        "unrelated-prefix",
        "embedded-wildcard",
        "missing-dot-before-wildcard",
        "insufficient-child-prefix",
        "non-string",
        "mapping",
    ],
)
def test_profile_keychain_grants_reject_unrelated_malformed_or_insufficient_values(
    grants,
):
    profile = app_store_profile()
    profile["Entitlements"]["keychain-access-groups"] = grants

    with pytest.raises(testflight.TestFlightError, match="same-Team wildcard"):
        testflight.check_profile(profile, now=NOW)


def test_profile_associated_domain_grants_accept_the_profile_wildcard_only():
    profile = app_store_profile()
    profile["Entitlements"]["com.apple.developer.associated-domains"] = "*"
    testflight.check_profile(profile, now=NOW)

    profile["Entitlements"]["com.apple.developer.associated-domains"] = ["*"]
    testflight.check_profile(profile, now=NOW)

    profile["Entitlements"]["com.apple.developer.associated-domains"] = [
        "webcredentials:example.com"
    ]
    with pytest.raises(testflight.TestFlightError, match="associated-domains"):
        testflight.check_profile(profile, now=NOW)


def test_an_app_store_profile_must_use_production_apns_and_no_filtering():
    profile = app_store_profile()
    profile["Entitlements"]["aps-environment"] = "development"
    with pytest.raises(testflight.TestFlightError, match="production"):
        testflight.check_profile(profile, now=NOW)

    profile = app_store_profile()
    profile["Entitlements"]["com.apple.developer.usernotifications.filtering"] = True
    with pytest.raises(testflight.TestFlightError, match="forbidden entitlements"):
        testflight.check_profile(profile, now=NOW)

    profile = app_store_profile()
    profile["Entitlements"]["keychain-access-groups"] = [
        testflight.SHARED_KEYCHAIN_ACCESS_GROUP
    ]
    with pytest.raises(testflight.TestFlightError, match="same-Team wildcard"):
        testflight.check_profile(profile, now=NOW)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"TeamIdentifier": ["ZZZZZZZZZZ"]}, "not for Team"),
        ({"Entitlements": {"application-identifier": "68B2SQ6YK3.bot.avibe.desktop",
                           "com.apple.developer.associated-domains": "*"}}, "not for 68B2SQ6YK3.bot.avibe.ios"),
        ({"ProvisionedDevices": ["00008110-000000000000001E"]}, "development or ad hoc"),
        ({"ExpirationDate": NOW - timedelta(seconds=1)}, "expired"),
        ({"Entitlements": {"application-identifier": "68B2SQ6YK3.bot.avibe.ios"}}, "aps-environment"),
        ({"Entitlements": {"application-identifier": "68B2SQ6YK3.bot.avibe.ios",
                           "aps-environment": "production",
                           "com.apple.developer.associated-domains": testflight.ASSOCIATED_DOMAINS,
                           "com.apple.security.application-groups": ["group.bot.avibe.ios"],
                           "keychain-access-groups": [
                               testflight.PRIVATE_KEYCHAIN_ACCESS_GROUP,
                               testflight.SHARED_KEYCHAIN_ACCESS_GROUP,
                           ],
                           "get-task-allow": True}}, "debugging"),
    ],
    ids=["team", "app", "devices", "expired", "associated-domains", "debuggable"],
)
def test_a_profile_that_cannot_sign_this_release_is_refused(overrides, message):
    with pytest.raises(testflight.TestFlightError, match=message):
        testflight.check_profile(app_store_profile(**overrides), now=NOW)


def test_profile_certificates_match_find_identity_hashes():
    import hashlib

    assert testflight.profile_certificate_hashes(app_store_profile()) == {
        hashlib.sha1(b"certificate-der").hexdigest().upper()
    }


def test_only_this_teams_apple_distribution_identity_is_used():
    listing = (
        '  1) 0123456789ABCDEF0123456789ABCDEF01234567 "Apple Distribution: YuHao CHEn (68B2SQ6YK3)"\n'
        '  2) 89ABCDEF0123456789ABCDEF0123456789ABCDEF "Apple Development: YuHao CHEn (68B2SQ6YK3)"\n'
        '  3) FEDCBA9876543210FEDCBA9876543210FEDCBA98 "Apple Distribution: Someone Else (ZZZZZZZZZZ)"\n'
        "     3 valid identities found\n"
    )
    assert testflight.distribution_identities(listing) == {
        "0123456789ABCDEF0123456789ABCDEF01234567": "Apple Distribution: YuHao CHEn (68B2SQ6YK3)"
    }


def test_export_options_upload_with_the_named_profile_and_manual_signing():
    assert testflight.export_options("Avibe iOS App Store") == {
        "method": "app-store-connect",
        "destination": "upload",
        "teamID": "68B2SQ6YK3",
        "signingStyle": "manual",
        "signingCertificate": "Apple Distribution",
        "provisioningProfiles": {"bot.avibe.ios": "Avibe iOS App Store"},
        "manageAppVersionAndBuildNumber": False,
        "uploadSymbols": True,
    }


# ---------------------------------------------------------------------------
# Archive


def signed(info=None, entitlements=None):
    base_info = {"CFBundleIdentifier": "bot.avibe.ios", "CFBundleVersion": "7.1",
                 "CFBundleShortVersionString": "0.1.0", "ITSAppUsesNonExemptEncryption": False}
    base_entitlements = {"application-identifier": "68B2SQ6YK3.bot.avibe.ios", "get-task-allow": False,
                         "aps-environment": "production",
                         "com.apple.developer.associated-domains": testflight.ASSOCIATED_DOMAINS,
                         "com.apple.security.application-groups": ["group.bot.avibe.ios"],
                         "keychain-access-groups": [
                             testflight.PRIVATE_KEYCHAIN_ACCESS_GROUP,
                             testflight.SHARED_KEYCHAIN_ACCESS_GROUP,
                         ]}
    return {**base_info, **(info or {})}, {**base_entitlements, **(entitlements or {})}


def test_a_distribution_signed_app_with_its_entitlements_passes():
    info, entitlements = signed()
    testflight.check_signed_app(info, entitlements, build="7.1")


def test_a_distribution_signed_notification_service_with_shared_entitlements_passes():
    info, entitlements = signed(
        info={"CFBundleIdentifier": testflight.NOTIFICATION_SERVICE_BUNDLE_ID},
        entitlements={
            "application-identifier": testflight.NOTIFICATION_SERVICE_APPLICATION_IDENTIFIER,
            "com.apple.developer.associated-domains": [],
            "keychain-access-groups": [
                testflight.SHARED_KEYCHAIN_ACCESS_GROUP
            ],
        },
    )
    entitlements.pop("aps-environment", None)
    testflight.check_signed_bundle(
        info,
        entitlements,
        bundle_id=testflight.NOTIFICATION_SERVICE_BUNDLE_ID,
        application_identifier=testflight.NOTIFICATION_SERVICE_APPLICATION_IDENTIFIER,
        required_entitlements=testflight.NOTIFICATION_SERVICE_REQUIRED_ENTITLEMENTS,
        build="7.1",
    )


def test_signed_keychain_groups_remain_exact_and_target_isolated():
    app_info, app_entitlements = signed(
        entitlements={
            "keychain-access-groups": [f"{testflight.TEAM_ID}.*"],
        }
    )
    with pytest.raises(testflight.TestFlightError, match="exactly"):
        testflight.check_signed_app(
            app_info,
            app_entitlements,
            build="7.1",
        )

    service_info, service_entitlements = signed(
        info={"CFBundleIdentifier": testflight.NOTIFICATION_SERVICE_BUNDLE_ID},
        entitlements={
            "application-identifier": testflight.NOTIFICATION_SERVICE_APPLICATION_IDENTIFIER,
            "com.apple.developer.associated-domains": [],
            "keychain-access-groups": [
                testflight.PRIVATE_KEYCHAIN_ACCESS_GROUP,
                testflight.SHARED_KEYCHAIN_ACCESS_GROUP,
            ],
        },
    )
    service_entitlements.pop("aps-environment", None)
    with pytest.raises(testflight.TestFlightError, match="exactly"):
        testflight.check_signed_bundle(
            service_info,
            service_entitlements,
            bundle_id=testflight.NOTIFICATION_SERVICE_BUNDLE_ID,
            application_identifier=testflight.NOTIFICATION_SERVICE_APPLICATION_IDENTIFIER,
            required_entitlements=testflight.NOTIFICATION_SERVICE_REQUIRED_ENTITLEMENTS,
            build="7.1",
        )


def test_a_notification_service_profile_has_shared_groups_but_no_apns():
    profile = app_store_profile()
    profile["Entitlements"] = {
        "application-identifier": testflight.NOTIFICATION_SERVICE_APPLICATION_IDENTIFIER,
        "com.apple.security.application-groups": ["group.bot.avibe.ios"],
        "keychain-access-groups": [testflight.SHARED_KEYCHAIN_ACCESS_GROUP],
        "get-task-allow": False,
    }
    testflight.check_profile(
        profile,
        bundle_id=testflight.NOTIFICATION_SERVICE_BUNDLE_ID,
        required_entitlements=testflight.NOTIFICATION_SERVICE_REQUIRED_ENTITLEMENTS,
        now=NOW,
    )

    profile["Entitlements"]["aps-environment"] = "production"
    with pytest.raises(testflight.TestFlightError, match="forbidden entitlements"):
        testflight.check_profile(
            profile,
            bundle_id=testflight.NOTIFICATION_SERVICE_BUNDLE_ID,
            required_entitlements=testflight.NOTIFICATION_SERVICE_REQUIRED_ENTITLEMENTS,
            now=NOW,
        )

    profile = app_store_profile()
    profile["Entitlements"] = {
        "application-identifier": testflight.NOTIFICATION_SERVICE_APPLICATION_IDENTIFIER,
        "com.apple.security.application-groups": ["group.bot.avibe.ios"],
        "keychain-access-groups": [
            testflight.PRIVATE_KEYCHAIN_ACCESS_GROUP,
            testflight.SHARED_KEYCHAIN_ACCESS_GROUP,
        ],
        "get-task-allow": False,
    }
    # The profile is an allowlist, so extra well-formed keychain grants are
    # acceptable; the signed extension remains exact and rejects this set.
    testflight.check_profile(
        profile,
        bundle_id=testflight.NOTIFICATION_SERVICE_BUNDLE_ID,
        required_entitlements=testflight.NOTIFICATION_SERVICE_REQUIRED_ENTITLEMENTS,
        now=NOW,
    )


def _write_extension(path: Path, bundle_id: str) -> None:
    path.mkdir(parents=True)
    (path / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": bundle_id})
    )


def test_archive_extension_inventory_is_recursive_and_exact(tmp_path: Path):
    app = tmp_path / "Avibe.app"
    valid = app / "PlugIns" / "Nested" / "NotificationService.appex"
    _write_extension(valid, testflight.NOTIFICATION_SERVICE_BUNDLE_ID)
    assert testflight.archived_notification_service_extensions(app) == [valid]

    _write_extension(
        app / "PlugIns" / "Unexpected.appex",
        "bot.avibe.ios.unexpected-service",
    )
    with pytest.raises(testflight.TestFlightError, match="exactly one"):
        testflight.archived_notification_service_extensions(app)


@pytest.mark.parametrize(
    ("info", "entitlements", "message"),
    [
        ({"CFBundleIdentifier": "bot.avibe.ios.tests"}, None, "not bot.avibe.ios"),
        ({"CFBundleVersion": "1"}, None, "build number"),
        ({"ITSAppUsesNonExemptEncryption": None}, None, "ITSAppUsesNonExemptEncryption"),
        (None, {"get-task-allow": True}, "development signature"),
        (None, {"com.apple.developer.associated-domains": []}, "webcredentials:avibe.bot"),
        (None, {"com.apple.developer.associated-domains": ["*"]}, "webcredentials:avibe.bot"),
        (
            None,
            {"com.apple.developer.associated-domains": ["webcredentials:avibe.bot"]},
            "webcredentials:sandbox.avibe.bot",
        ),
        (
            None,
            {
                "com.apple.developer.associated-domains": [
                    "webcredentials:avibe.bot",
                    "webcredentials:sandbox.avibe.bot",
                    "webcredentials:attacker.example",
                ]
            },
            "exactly",
        ),
        (None, {"application-identifier": "ZZZZZZZZZZ.bot.avibe.ios"}, "not signed for"),
        (None, {"aps-environment": "development"}, "aps-environment"),
        (None, {"com.apple.developer.usernotifications.filtering": True}, "forbidden entitlements"),
    ],
    ids=[
        "bundle",
        "build",
        "encryption",
        "debuggable",
        "passkeys",
        "passkeys-wildcard",
        "passkeys-without-vault-domain",
        "passkeys-extra-domain",
        "team",
        "development-aps",
        "filtering",
    ],
)
def test_an_archive_that_testflight_would_reject_or_mis_sign_fails_here(info, entitlements, message):
    info, entitlements = signed(info, entitlements)
    with pytest.raises(testflight.TestFlightError, match=message):
        testflight.check_signed_app(info, entitlements, build="7.1")


def test_the_app_declares_its_encryption_exemption():
    info = plistlib.loads((ROOT / "ios/Avibe/Resources/Info.plist").read_bytes())
    assert info["ITSAppUsesNonExemptEncryption"] is False


# ---------------------------------------------------------------------------
# App Store Connect token


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_the_api_token_is_an_es256_jws_that_verifies_with_the_public_key(tmp_path: Path):
    key = tmp_path / "AuthKey.p8"
    generated = subprocess.run(
        ["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256"],
        capture_output=True, check=True,
    ).stdout
    key.write_bytes(generated)
    public = tmp_path / "public.pem"
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(public)], check=True)

    token = testflight.signed_token(key, key_id="ABCDE12345", issuer_id="issuer", now=1_700_000_000)

    header, payload, signature = token.split(".")
    pad = lambda part: part + "=" * (-len(part) % 4)  # noqa: E731
    assert json.loads(base64.urlsafe_b64decode(pad(header))) == {"alg": "ES256", "kid": "ABCDE12345", "typ": "JWT"}
    assert json.loads(base64.urlsafe_b64decode(pad(payload))) == {
        "iss": "issuer", "iat": 1_700_000_000, "exp": 1_700_001_200, "aud": "appstoreconnect-v1",
    }
    raw = base64.urlsafe_b64decode(pad(signature))
    assert len(raw) == 64

    def integer(value: bytes) -> bytes:
        value = value.lstrip(b"\x00") or b"\x00"
        if value[0] & 0x80:
            value = b"\x00" + value
        return b"\x02" + bytes([len(value)]) + value

    body = integer(raw[:32]) + integer(raw[32:])
    der = tmp_path / "signature.der"
    der.write_bytes(b"\x30" + bytes([len(body)]) + body)
    signing_input = tmp_path / "input"
    signing_input.write_bytes(f"{header}.{payload}".encode())
    verified = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(der), str(signing_input)],
        capture_output=True, text=True,
    )
    assert verified.returncode == 0, verified.stderr


def test_der_signatures_become_fixed_size_r_and_s():
    r = b"\x00" + b"\x81" + b"\x01" * 31  # positive, sign byte added
    s = b"\x05"  # short
    body = b"\x02" + bytes([len(r)]) + r + b"\x02" + bytes([len(s)]) + s
    raw = testflight.der_signature_to_raw(b"\x30" + bytes([len(body)]) + body)
    assert raw == b"\x81" + b"\x01" * 31 + b"\x00" * 31 + b"\x05"
    with pytest.raises(testflight.TestFlightError):
        testflight.der_signature_to_raw(b"\x31\x00")


# ---------------------------------------------------------------------------
# Distribution


class FakeApi:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, query=None, body=None):
        self.calls.append((method, path, query, body))
        return self.responses.pop(0)


def build_listing(state):
    return {"data": [{"id": "build-1", "attributes": {"version": "7.1", "processingState": state}}]}


def test_the_build_is_returned_once_processed():
    api = FakeApi([{"data": []}, build_listing("PROCESSING"), build_listing("VALID")])
    sleeps = []
    found = testflight.wait_for_build(api, "app-1", version="0.1.0", build="7.1", timeout=600, interval=30,
                                      sleep=sleeps.append, clock=lambda: 0)
    assert found["id"] == "build-1"
    assert sleeps == [30, 30]
    method, path, query, _ = api.calls[0]
    assert (method, path) == ("GET", "/v1/builds")
    assert query["filter[app]"] == "app-1"
    assert query["filter[version]"] == "7.1"
    assert query["filter[preReleaseVersion.version]"] == "0.1.0"


def test_a_build_app_store_connect_rejects_fails_at_once():
    api = FakeApi([build_listing("INVALID")])
    with pytest.raises(testflight.TestFlightError, match="rejected"):
        testflight.wait_for_build(api, "app-1", version="0.1.0", build="7.1", timeout=600, interval=30,
                                  sleep=lambda _: None, clock=lambda: 0)


def test_a_build_still_processing_at_the_deadline_fails():
    times = iter([0, 700])
    api = FakeApi([build_listing("PROCESSING")])
    with pytest.raises(testflight.TestFlightError, match="still PROCESSING"):
        testflight.wait_for_build(api, "app-1", version="0.1.0", build="7.1", timeout=600, interval=30,
                                  sleep=lambda _: None, clock=lambda: next(times))


def group(name="Avibe Internal", internal=True, all_builds=False):
    return {"id": "group-1", "attributes": {"name": name, "isInternalGroup": internal,
                                            "hasAccessToAllBuilds": all_builds}}


def test_the_build_is_added_to_the_internal_group():
    api = FakeApi([{"data": [group(name="Other"), group()]}, {}])
    assert testflight.make_available(api, "app-1", "build-1") == "added to Avibe Internal"
    assert api.calls[1] == ("POST", "/v1/betaGroups/group-1/relationships/builds", None,
                            {"data": [{"type": "builds", "id": "build-1"}]})


def test_a_group_that_receives_every_build_is_left_alone():
    api = FakeApi([{"data": [group(all_builds=True)]}])
    assert "automatically" in testflight.make_available(api, "app-1", "build-1")
    assert len(api.calls) == 1


@pytest.mark.parametrize("groups", [[], [group(internal=False)]], ids=["missing", "external"])
def test_without_the_internal_group_distribution_fails(groups):
    with pytest.raises(testflight.TestFlightError, match="internal TestFlight group"):
        testflight.make_available(FakeApi([{"data": groups}]), "app-1", "build-1")


# ---------------------------------------------------------------------------
# Workflow


def test_the_testflight_workflow_runs_by_hand_from_master_and_scopes_its_secrets():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ios-testflight.yml").read_text(encoding="utf-8"))
    workflow_text = (ROOT / ".github/workflows/ios-testflight.yml").read_text(encoding="utf-8")
    project_text = (ROOT / "ios/Avibe.xcodeproj/project.pbxproj").read_text(encoding="utf-8")
    assert workflow[True] == {"workflow_dispatch": None}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {"group": "ios-testflight", "cancel-in-progress": False}
    job = workflow["jobs"]["testflight"]
    assert job["if"] == "github.ref == 'refs/heads/master'"
    steps = job["steps"]
    shell = yaml.safe_load((ROOT / ".github/workflows/ios-shell.yml").read_text(encoding="utf-8"))
    assert workflow["env"] == shell["env"]  # the same pinned Xcode
    shell_pins = {step["uses"] for step in shell["jobs"]["simulator"]["steps"] if "uses" in step}
    for step in steps:
        if step.get("uses", "").startswith(("actions/checkout@", "actions/setup-node@")):
            assert step["uses"] in shell_pins

    names = [step.get("name") for step in steps]
    order = [
        "Build bundled mobile UI",
        "Verify bundled mobile UI",
        "Import Apple Distribution identity and App Store profile",
        "Archive for App Store distribution",
        "Verify the archived app's signature",
        "Upload to App Store Connect",
        "Make the build available to internal testers",
        "Remove Apple signing material",
    ]
    assert [name for name in names if name in order] == order
    with_secrets = [step["name"] for step in steps if "secrets." in json.dumps(step)]
    assert with_secrets == ["Import Apple Distribution identity and App Store profile"]
    assert 'APP_PROVISIONING_PROFILE_SPECIFIER="$IOS_PROFILE_NAME"' in workflow_text
    assert 'NOTIFICATION_SERVICE_PROFILE_SPECIFIER="$IOS_NOTIFICATION_SERVICE_PROFILE_NAME"' in workflow_text
    assert '\n            PROVISIONING_PROFILE_SPECIFIER="$IOS_PROFILE_NAME"' not in workflow_text
    app_release = project_text.split(
        "C00000000000000000000004 /* Release */ = {", 1
    )[1].split("C00000000000000000000005 /* Debug */ =", 1)[0]
    assert 'PROVISIONING_PROFILE_SPECIFIER = "$(APP_PROVISIONING_PROFILE_SPECIFIER)";' in app_release
    assert 'PROVISIONING_PROFILE_SPECIFIER = "$(NOTIFICATION_SERVICE_PROFILE_SPECIFIER)";' in project_text
    cleanup = steps[-1]
    assert cleanup["name"] == "Remove Apple signing material"
    assert cleanup["if"] == "always()"
