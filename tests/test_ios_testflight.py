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
HEALTHKIT_ENTITLEMENTS = (
    "com.apple.developer.healthkit",
    "com.apple.developer.healthkit.background-delivery",
)
HEALTH_UPDATE_USAGE_EN = "Avibe never writes to Apple Health. It only reads the data you allow."
HEALTH_UPDATE_USAGE_ZH = "Avibe 不会向「健康」写入数据，只读取你允许的数据。"
HEALTH_SHARE_USAGE_EN = (
    "Avibe reads your steps, activity, workouts, sleep, heart rate, resting heart rate, and heart rate variability "
    "to provide your personal daily and weekly status "
    "through your own Avibe instance. Access is read-only; Avibe never writes to Apple Health."
)
HEALTH_SHARE_USAGE_ZH = (
    "Avibe 只读你的步数、活动、锻炼、睡眠、心率、静息心率和心率变异性，通过你自己的 Avibe 实例提供个人每日和每周状态。"
    "Avibe 不会向 Apple 健康写入数据。"
)
COMPLETE = {
    "IOS_DISTRIBUTION_P12_BASE64": "cDEy",
    "IOS_DISTRIBUTION_P12_PASSWORD": " pass word ",
    "IOS_APP_STORE_PROFILE_BASE64": "cHJvZmlsZQ==",
    "IOS_NOTIFICATION_SERVICE_PROFILE_BASE64": "bm90aWZpY2F0aW9uLXByb2ZpbGU=",
    "IOS_LIVE_ACTIVITY_PROFILE_BASE64": "bGl2ZS1hY3Rpdml0eS1wcm9maWxl",
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
            "com.apple.developer.healthkit": True,
            "com.apple.developer.healthkit.background-delivery": True,
            "com.apple.developer.associated-domains": testflight.ASSOCIATED_DOMAINS,
            "com.apple.security.application-groups": ["group.bot.avibe.ios"],
            "keychain-access-groups": [
                testflight.PRIVATE_KEYCHAIN_ACCESS_GROUP,
                testflight.SHARED_KEYCHAIN_ACCESS_GROUP,
            ],
            testflight.SIGN_IN_WITH_APPLE: ["Default"],
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
                 "CFBundleShortVersionString": "0.1.0", "ITSAppUsesNonExemptEncryption": False,
                 "NSHealthShareUsageDescription": HEALTH_SHARE_USAGE_EN,
                 "NSHealthUpdateUsageDescription": HEALTH_UPDATE_USAGE_EN}
    base_entitlements = {"application-identifier": "68B2SQ6YK3.bot.avibe.ios", "get-task-allow": False,
                         "aps-environment": "production",
                         "com.apple.developer.healthkit": True,
                         "com.apple.developer.healthkit.background-delivery": True,
                         "com.apple.developer.associated-domains": testflight.ASSOCIATED_DOMAINS,
                         "com.apple.security.application-groups": ["group.bot.avibe.ios"],
                         "keychain-access-groups": [
                             testflight.PRIVATE_KEYCHAIN_ACCESS_GROUP,
                             testflight.SHARED_KEYCHAIN_ACCESS_GROUP,
                         ],
                         testflight.SIGN_IN_WITH_APPLE: ["Default"]}
    merged_info = {**base_info, **(info or {})}
    if merged_info["CFBundleIdentifier"] != testflight.BUNDLE_ID:
        # Extensions declare no HealthKit purpose strings.
        merged_info.pop("NSHealthShareUsageDescription", None)
        merged_info.pop("NSHealthUpdateUsageDescription", None)
    return merged_info, {**base_entitlements, **(entitlements or {})}


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
    for key in HEALTHKIT_ENTITLEMENTS:
        entitlements.pop(key)
    entitlements.pop(testflight.SIGN_IN_WITH_APPLE, None)
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
    for key in HEALTHKIT_ENTITLEMENTS:
        service_entitlements.pop(key)
    service_entitlements.pop(testflight.SIGN_IN_WITH_APPLE, None)
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
    service = app / "PlugIns" / "Nested" / "NotificationService.appex"
    _write_extension(service, testflight.NOTIFICATION_SERVICE_BUNDLE_ID)
    with pytest.raises(testflight.TestFlightError, match="exactly one each"):
        testflight.archived_extensions(app)

    live = app / "PlugIns" / "AvibeLiveActivity.appex"
    _write_extension(live, testflight.LIVE_ACTIVITY_BUNDLE_ID)
    assert testflight.archived_extensions(app) == {
        testflight.NOTIFICATION_SERVICE_BUNDLE_ID: service,
        testflight.LIVE_ACTIVITY_BUNDLE_ID: live,
    }

    _write_extension(
        app / "PlugIns" / "Unexpected.appex",
        "bot.avibe.ios.unexpected-service",
    )
    with pytest.raises(testflight.TestFlightError, match="exactly one each"):
        testflight.archived_extensions(app)

    duplicate = tmp_path / "Duplicate.app"
    _write_extension(duplicate / "PlugIns" / "A.appex", testflight.LIVE_ACTIVITY_BUNDLE_ID)
    _write_extension(duplicate / "PlugIns" / "B.appex", testflight.LIVE_ACTIVITY_BUNDLE_ID)
    with pytest.raises(testflight.TestFlightError, match="exactly one each"):
        testflight.archived_extensions(duplicate)


def _live_activity_signed(entitlements: dict | None = None) -> tuple[dict, dict]:
    info, signed_entitlements = signed(
        info={"CFBundleIdentifier": testflight.LIVE_ACTIVITY_BUNDLE_ID},
        entitlements={
            "application-identifier": testflight.LIVE_ACTIVITY_APPLICATION_IDENTIFIER,
        },
    )
    for key in ("aps-environment", "com.apple.developer.associated-domains",
                "com.apple.security.application-groups", "keychain-access-groups",
                *HEALTHKIT_ENTITLEMENTS, testflight.SIGN_IN_WITH_APPLE):
        signed_entitlements.pop(key, None)
    signed_entitlements.update(entitlements or {})
    return info, signed_entitlements


def _check_live_activity(info: dict, entitlements: dict) -> None:
    testflight.check_signed_bundle(
        info,
        entitlements,
        bundle_id=testflight.LIVE_ACTIVITY_BUNDLE_ID,
        application_identifier=testflight.LIVE_ACTIVITY_APPLICATION_IDENTIFIER,
        required_entitlements=testflight.LIVE_ACTIVITY_REQUIRED_ENTITLEMENTS,
        build="7.1",
    )


def test_the_live_activity_extension_is_signed_with_nothing_shared():
    _check_live_activity(*_live_activity_signed())
    # Its own keychain group (Xcode may add it) is harmless.
    _check_live_activity(*_live_activity_signed(
        {"keychain-access-groups": [testflight.LIVE_ACTIVITY_APPLICATION_IDENTIFIER]}
    ))
    for extra, message in [
        ({"aps-environment": "production"}, "forbidden entitlements"),
        ({"com.apple.security.application-groups": ["group.bot.avibe.ios"]}, "forbidden entitlements"),
        ({"keychain-access-groups": [testflight.SHARED_KEYCHAIN_ACCESS_GROUP]}, "keychain groups"),
        ({"keychain-access-groups": [f"{testflight.TEAM_ID}.*"]}, "keychain groups"),
        ({"application-identifier": testflight.APPLICATION_IDENTIFIER}, "not signed for"),
        ({"get-task-allow": True}, "development signature"),
    ]:
        with pytest.raises(testflight.TestFlightError, match=message):
            _check_live_activity(*_live_activity_signed(extra))


def test_a_live_activity_profile_is_checked_by_what_it_grants():
    profile = app_store_profile()
    profile["Entitlements"] = {
        "application-identifier": testflight.LIVE_ACTIVITY_APPLICATION_IDENTIFIER,
        # Apple's standard allowlist; the signed bundle is checked separately.
        "keychain-access-groups": [f"{testflight.TEAM_ID}.*"],
        "get-task-allow": False,
    }
    testflight.check_profile(
        profile,
        bundle_id=testflight.LIVE_ACTIVITY_BUNDLE_ID,
        required_entitlements=testflight.LIVE_ACTIVITY_REQUIRED_ENTITLEMENTS,
        now=NOW,
    )
    for key, value in [
        ("aps-environment", "production"),
        ("com.apple.security.application-groups", ["group.bot.avibe.ios"]),
    ]:
        bad = app_store_profile()
        bad["Entitlements"] = {**profile["Entitlements"], key: value}
        with pytest.raises(testflight.TestFlightError, match="forbidden entitlements"):
            testflight.check_profile(
                bad,
                bundle_id=testflight.LIVE_ACTIVITY_BUNDLE_ID,
                required_entitlements=testflight.LIVE_ACTIVITY_REQUIRED_ENTITLEMENTS,
                now=NOW,
            )


def test_the_notification_service_ships_with_the_apps_version():
    testflight.check_extension_version(
        {"CFBundleShortVersionString": "1.0"}, {"CFBundleShortVersionString": "1.0"}
    )
    for app_version, extension_version in [("1.0", "0.1.0"), ("1.0", None), (None, None)]:
        with pytest.raises(testflight.TestFlightError, match="version"):
            testflight.check_extension_version(
                {"CFBundleShortVersionString": app_version},
                {"CFBundleShortVersionString": extension_version},
            )


def test_each_shipped_bundle_needs_its_own_privacy_manifest(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    for source in ["ios/Avibe/Resources", "ios/NotificationService", "ios/AvibeLiveActivity"]:
        bundle = tmp_path / source.replace("/", "-")
        bundle.mkdir()
        (bundle / "PrivacyInfo.xcprivacy").write_bytes(
            (repo / source / "PrivacyInfo.xcprivacy").read_bytes()
        )
        testflight.check_privacy_manifest(bundle)

    missing = tmp_path / "NotificationService.appex"
    missing.mkdir()
    with pytest.raises(testflight.TestFlightError, match="no readable"):
        testflight.check_privacy_manifest(missing)
    (missing / "PrivacyInfo.xcprivacy").write_bytes(plistlib.dumps({"NSPrivacyTracking": False}))
    with pytest.raises(testflight.TestFlightError, match="incomplete"):
        testflight.check_privacy_manifest(missing)


def test_the_app_manifest_declares_uploaded_health_data_exactly(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    source = plistlib.loads((repo / "ios/Avibe/Resources/PrivacyInfo.xcprivacy").read_bytes())
    app = tmp_path / "Avibe.app"
    app.mkdir()

    def write(manifest: dict) -> None:
        (app / "PrivacyInfo.xcprivacy").write_bytes(plistlib.dumps(manifest))

    write(source)
    testflight.check_health_privacy_declaration(app)

    def without_health() -> list:
        return [
            entry
            for entry in source["NSPrivacyCollectedDataTypes"]
            if entry["NSPrivacyCollectedDataType"] != "NSPrivacyCollectedDataTypeHealth"
        ]

    health = dict(testflight.HEALTH_COLLECTED_DATA_TYPE)
    invalid = [
        without_health(),
        [*without_health(), {**health, "NSPrivacyCollectedDataTypeTracking": True}],
        [*without_health(), {**health, "NSPrivacyCollectedDataTypeLinked": False}],
        [*without_health(), {**health, "NSPrivacyCollectedDataTypeLinked": 1}],
        [*without_health(), {**health, "NSPrivacyCollectedDataTypePurposes": []}],
        [
            *without_health(),
            {
                **health,
                "NSPrivacyCollectedDataTypePurposes": [
                    "NSPrivacyCollectedDataTypePurposeAnalytics"
                ],
            },
        ],
        [*without_health(), health, health],
    ]
    for collected in invalid:
        write({**source, "NSPrivacyCollectedDataTypes": collected})
        with pytest.raises(testflight.TestFlightError, match="HealthKit data"):
            testflight.check_health_privacy_declaration(app)
    write({key: value for key, value in source.items() if key != "NSPrivacyCollectedDataTypes"})
    with pytest.raises(testflight.TestFlightError, match="HealthKit data"):
        testflight.check_health_privacy_declaration(app)


def test_archive_verification_requires_health_only_in_the_main_app_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = Path(__file__).resolve().parents[1]
    current = (repo / "ios/Avibe/Resources/PrivacyInfo.xcprivacy").read_bytes()
    source = plistlib.loads(current)
    without_health = plistlib.dumps(
        {
            **source,
            "NSPrivacyCollectedDataTypes": [
                entry
                for entry in source["NSPrivacyCollectedDataTypes"]
                if entry["NSPrivacyCollectedDataType"] != "NSPrivacyCollectedDataTypeHealth"
            ],
        }
    )

    def archive(name: str, app_manifest: bytes) -> Path:
        root = tmp_path / name
        app = root / "Products/Applications/Avibe.app"
        app.mkdir(parents=True)
        info = {"CFBundleIdentifier": testflight.BUNDLE_ID, "CFBundleShortVersionString": "1.0"}
        (app / "Info.plist").write_bytes(plistlib.dumps(info))
        (app / "PrivacyInfo.xcprivacy").write_bytes(app_manifest)
        for bundle_id, extension_source in [
            (testflight.NOTIFICATION_SERVICE_BUNDLE_ID, "ios/NotificationService"),
            (testflight.LIVE_ACTIVITY_BUNDLE_ID, "ios/AvibeLiveActivity"),
        ]:
            extension = app / "PlugIns" / f"{bundle_id}.appex"
            extension.mkdir(parents=True)
            (extension / "Info.plist").write_bytes(
                plistlib.dumps({**info, "CFBundleIdentifier": bundle_id})
            )
            # The real extension manifests declare no Health data.
            (extension / "PrivacyInfo.xcprivacy").write_bytes(
                (repo / extension_source / "PrivacyInfo.xcprivacy").read_bytes()
            )
        return root

    class ReachedSignatureChecks(Exception):
        pass

    def stop_before_codesign(*_args, **_kwargs):
        raise ReachedSignatureChecks

    # Signature checks follow the manifest checks; stop there instead of running codesign.
    monkeypatch.setattr(testflight, "run", stop_before_codesign)
    with pytest.raises(ReachedSignatureChecks):
        testflight.verify_archive(archive("current", current), build="2")
    with pytest.raises(testflight.TestFlightError, match="Avibe.app.*HealthKit data"):
        testflight.verify_archive(archive("missing", without_health), build="2")


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
        (None, {testflight.SIGN_IN_WITH_APPLE: []}, "applesignin"),
        (None, {testflight.SIGN_IN_WITH_APPLE: ["Default", "Other"]}, "exactly"),
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
        "sign-in-with-apple-missing",
        "sign-in-with-apple-extra",
    ],
)
def test_an_archive_that_testflight_would_reject_or_mis_sign_fails_here(info, entitlements, message):
    info, entitlements = signed(info, entitlements)
    with pytest.raises(testflight.TestFlightError, match=message):
        testflight.check_signed_app(info, entitlements, build="7.1")


def test_the_app_declares_its_encryption_exemption():
    info = plistlib.loads((ROOT / "ios/Avibe/Resources/Info.plist").read_bytes())
    assert info["ITSAppUsesNonExemptEncryption"] is False


@pytest.mark.parametrize("boundary", ["profile", "archive"])
@pytest.mark.parametrize("key", HEALTHKIT_ENTITLEMENTS)
@pytest.mark.parametrize(
    "value", [None, False, 0, 1, "true", [True], {"enabled": True}],
    ids=["missing", "false", "zero", "one", "string", "list", "mapping"],
)
def test_main_healthkit_entitlements_require_explicit_boolean_true(boundary, key, value):
    info, entitlements = signed()
    if value is None:
        entitlements.pop(key)
    else:
        entitlements[key] = value
    with pytest.raises(testflight.TestFlightError, match=f"{key} = true"):
        if boundary == "profile":
            profile = app_store_profile(Entitlements=entitlements)
            testflight.check_profile(profile, now=NOW)
        else:
            testflight.check_signed_app(info, entitlements, build="7.1")


@pytest.mark.parametrize("boundary", ["profile", "archive"])
@pytest.mark.parametrize(
    "bundle_id", [testflight.NOTIFICATION_SERVICE_BUNDLE_ID, testflight.LIVE_ACTIVITY_BUNDLE_ID],
    ids=["notification-service", "live-activity"],
)
@pytest.mark.parametrize("key", HEALTHKIT_ENTITLEMENTS)
@pytest.mark.parametrize("value", [True, False])
def test_extensions_cannot_carry_healthkit_even_when_disabled(boundary, bundle_id, key, value):
    info, entitlements = _live_activity_signed()
    info["CFBundleIdentifier"] = bundle_id
    entitlements["application-identifier"] = f"{testflight.TEAM_ID}.{bundle_id}"
    if bundle_id == testflight.NOTIFICATION_SERVICE_BUNDLE_ID:
        entitlements.update({
            "com.apple.security.application-groups": ["group.bot.avibe.ios"],
            "keychain-access-groups": [testflight.SHARED_KEYCHAIN_ACCESS_GROUP],
        })
        required = testflight.NOTIFICATION_SERVICE_REQUIRED_ENTITLEMENTS
    else:
        required = testflight.LIVE_ACTIVITY_REQUIRED_ENTITLEMENTS
    entitlements[key] = value
    with pytest.raises(testflight.TestFlightError, match=f"forbidden entitlements: {key}"):
        if boundary == "profile":
            testflight.check_profile(
                app_store_profile(Entitlements=entitlements),
                bundle_id=bundle_id, required_entitlements=required, now=NOW,
            )
        else:
            testflight.check_signed_bundle(
                info, entitlements, bundle_id=bundle_id,
                application_identifier=f"{testflight.TEAM_ID}.{bundle_id}",
                required_entitlements=required, build="7.1",
            )


@pytest.mark.parametrize("key", ["NSHealthShareUsageDescription", "NSHealthUpdateUsageDescription"])
@pytest.mark.parametrize(
    "value", [None, "", " \n\t", True, ["Read health data"]],
    ids=["missing", "empty", "whitespace", "boolean", "list"],
)
def test_main_healthkit_usage_descriptions_are_required(key, value):
    # App Store upload validation rejects the archive without either one
    # (ios-testflight run 44454583255), even though Avibe only reads.
    info, entitlements = signed()
    if value is None:
        info.pop(key)
    else:
        info[key] = value
    with pytest.raises(testflight.TestFlightError, match=f"non-empty {key}"):
        testflight.check_signed_app(info, entitlements, build="7.1")


@pytest.mark.parametrize(
    "bundle_id", [testflight.NOTIFICATION_SERVICE_BUNDLE_ID, testflight.LIVE_ACTIVITY_BUNDLE_ID],
    ids=["notification-service", "live-activity"],
)
@pytest.mark.parametrize("key", ["NSHealthShareUsageDescription", "NSHealthUpdateUsageDescription"])
def test_no_extension_may_declare_healthkit_usage(bundle_id, key):
    info, entitlements = _live_activity_signed()
    info["CFBundleIdentifier"] = bundle_id
    info.pop("NSHealthShareUsageDescription", None)
    info.pop("NSHealthUpdateUsageDescription", None)
    entitlements["application-identifier"] = f"{testflight.TEAM_ID}.{bundle_id}"
    required = testflight.LIVE_ACTIVITY_REQUIRED_ENTITLEMENTS
    if bundle_id == testflight.NOTIFICATION_SERVICE_BUNDLE_ID:
        entitlements.update({
            "com.apple.security.application-groups": ["group.bot.avibe.ios"],
            "keychain-access-groups": [testflight.SHARED_KEYCHAIN_ACCESS_GROUP],
        })
        required = testflight.NOTIFICATION_SERVICE_REQUIRED_ENTITLEMENTS
    info[key] = "Health data"
    with pytest.raises(testflight.TestFlightError, match="Only the app declares HealthKit purpose strings"):
        testflight.check_signed_bundle(
            info, entitlements, bundle_id=bundle_id,
            application_identifier=f"{testflight.TEAM_ID}.{bundle_id}",
            required_entitlements=required, build="7.1",
        )


def test_healthkit_source_configuration_is_main_only_and_read_only_in_both_languages():
    entitlements = plistlib.loads((ROOT / "ios/Avibe/Avibe.entitlements").read_bytes())
    for key in HEALTHKIT_ENTITLEMENTS:
        assert entitlements[key] is True
    service_entitlements = plistlib.loads(
        (ROOT / "ios/NotificationService/NotificationService.entitlements").read_bytes()
    )
    assert not set(HEALTHKIT_ENTITLEMENTS).intersection(service_entitlements)

    info = plistlib.loads((ROOT / "ios/Avibe/Resources/Info.plist").read_bytes())
    catalog = json.loads((ROOT / "ios/Avibe/Resources/InfoPlist.xcstrings").read_text(encoding="utf-8"))
    assert info["NSHealthShareUsageDescription"] == HEALTH_SHARE_USAGE_EN
    localized = catalog["strings"]["NSHealthShareUsageDescription"]["localizations"]
    assert localized["en"]["stringUnit"]["value"] == HEALTH_SHARE_USAGE_EN
    assert localized["zh-Hans"]["stringUnit"]["value"] == HEALTH_SHARE_USAGE_ZH
    # The update string exists only because upload validation requires it; Avibe requests no
    # write types, so the source must keep `toShare: []`.
    assert info["NSHealthUpdateUsageDescription"] == HEALTH_UPDATE_USAGE_EN
    update = catalog["strings"]["NSHealthUpdateUsageDescription"]["localizations"]
    assert update["en"]["stringUnit"]["value"] == HEALTH_UPDATE_USAGE_EN
    assert update["zh-Hans"]["stringUnit"]["value"] == HEALTH_UPDATE_USAGE_ZH
    reader = (ROOT / "ios/Avibe/HealthDataReader.swift").read_text(encoding="utf-8")
    assert reader.count("requestAuthorization(") == 1
    assert "requestAuthorization(toShare: [], read:" in reader
    for source in ("ios/NotificationService/Info.plist", "ios/AvibeLiveActivity/Info.plist"):
        extension_info = plistlib.loads((ROOT / source).read_bytes())
        assert "NSHealthShareUsageDescription" not in extension_info
        assert "NSHealthUpdateUsageDescription" not in extension_info


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
    assert set(workflow[True]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {"group": "ios-testflight", "cancel-in-progress": False}
    job = workflow["jobs"]["testflight"]
    assert job["if"] == "github.ref == 'refs/heads/master'"
    assert job["needs"] == "source"
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
        "Generate the App's license notices",
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
    assert 'LIVE_ACTIVITY_PROFILE_SPECIFIER="$IOS_LIVE_ACTIVITY_PROFILE_NAME"' in workflow_text
    assert '\n            PROVISIONING_PROFILE_SPECIFIER="$IOS_PROFILE_NAME"' not in workflow_text
    app_release = project_text.split(
        "C00000000000000000000004 /* Release */ = {", 1
    )[1].split("C00000000000000000000005 /* Debug */ =", 1)[0]
    assert 'PROVISIONING_PROFILE_SPECIFIER = "$(APP_PROVISIONING_PROFILE_SPECIFIER)";' in app_release
    assert 'PROVISIONING_PROFILE_SPECIFIER = "$(NOTIFICATION_SERVICE_PROFILE_SPECIFIER)";' in project_text
    assert 'PROVISIONING_PROFILE_SPECIFIER = "$(LIVE_ACTIVITY_PROFILE_SPECIFIER)";' in project_text
    cleanup = steps[-1]
    assert cleanup["name"] == "Remove Apple signing material"
    assert cleanup["if"] == "always()"


def test_sign_in_with_apple_is_the_apps_alone():
    assert testflight.MAIN_REQUIRED_ENTITLEMENTS[testflight.SIGN_IN_WITH_APPLE] == ["Default"]
    profile = app_store_profile()
    del profile["Entitlements"][testflight.SIGN_IN_WITH_APPLE]
    with pytest.raises(testflight.TestFlightError, match="applesignin"):
        testflight.check_profile(profile, now=NOW)
    for bundle_id in testflight.EXTENSION_BUNDLE_IDS:
        assert testflight.SIGN_IN_WITH_APPLE in testflight.forbidden_entitlements(bundle_id)
    entitlements = plistlib.loads((ROOT / "ios/Avibe/Avibe.entitlements").read_bytes())
    assert entitlements[testflight.SIGN_IN_WITH_APPLE] == ["Default"]


def test_the_testflight_workflow_builds_only_a_proven_master_commit():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ios-testflight.yml").read_text(encoding="utf-8"))
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs == {"source_sha": {
        "description": "Full 40-character master commit to build; its own full master CI run must be green",
        "required": True,
        "type": "string",
    }}
    gate = workflow["jobs"]["source"]
    assert gate["if"] == "github.ref == 'refs/heads/master'"
    assert gate["permissions"] == {"actions": "read", "contents": "read"}
    assert gate["outputs"] == {"source_sha": "${{ steps.gate.outputs.source_sha }}"}
    assert "secrets." not in json.dumps(gate)
    prove = next(step for step in gate["steps"] if step.get("id") == "gate")
    # The input reaches the shell only through the environment, never interpolated.
    assert prove["env"]["SOURCE_SHA"] == "${{ inputs.source_sha }}"
    assert "inputs." not in prove["run"]
    assert prove["run"].startswith("python3 scripts/testflight_source_gate.py ")

    job = workflow["jobs"]["testflight"]
    assert job["needs"] == "source"
    steps = job["steps"]
    checkout = steps[0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {"ref": "${{ needs.source.outputs.source_sha }}"}
    confirm = steps[1]
    assert confirm["name"] == "Confirm the proven source commit"
    assert confirm["env"] == {"SOURCE_SHA": "${{ needs.source.outputs.source_sha }}"}
    assert '"$(git rev-parse HEAD)" = "$SOURCE_SHA"' in confirm["run"]
    first_secret = next(i for i, step in enumerate(steps) if "secrets." in json.dumps(step))
    assert first_secret > 1
