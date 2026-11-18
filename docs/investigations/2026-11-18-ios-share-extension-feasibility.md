# I1: iOS inbound Share Extension feasibility

Date: 2026-11-18 (Asia/Shanghai). Read-only assessment of commit `8279eb7057afeffb6a04000dd3627843e82197cc` in the `ios-show-outbound-share` worktree. Reporting session: `seswqtcxjegbe`; commissioning session: `sesysbw6uhyjv`.

## Decision

**Technically feasible; not ready for signed distribution on the evidence available.** The project already embeds two native extensions and declares an App Group. A third, narrowly scoped Share Extension can reuse that structure. It needs its own target, explicit App ID and provisioning profile, plus an update to the intentionally closed TestFlight extension inventory. No implementation or Apple configuration was changed.

**Local signing readiness is blocked:** no valid code-signing identity or public certificate for Team `68B2SQ6YK3`, and no installed provisioning profiles, were visible in the inspected local stores. **Apple account and CI readiness remain unknown:** this assessment did not sign in, query authenticated Apple APIs, inspect remote secret values, or verify backend registrations. Local absence does not mean the Team, profiles, or CI signing identity do not exist elsewhere.

**Product scope matters:** accepting content into a draft that the main app imports on its next foreground activation is a small plausible design. Sending directly from the share sheet requires an explicit account/instance authorization and networking design; the current WebKit session is not an extension credential. Do not base either design on automatically opening the containing app from a Share Extension.

## Concrete user flow and boundary

For example, a user selects Share in Safari, chooses Avibe, and saves a URL and optional text. The extension copies provider-owned content into an App Group inbox, commits a complete draft, and completes the extension request. When the user next opens Avibe, the main app claims the draft, resolves the current account and destination, and presents the composer for confirmation.

That flow is a recommendation, not an existing capability or a validated UX. It must make the delayed handoff clear. Specify cancellation, duplicate imports, oversized or unsupported input, process termination, file cleanup, and account changes before implementation. In particular, a draft saved under one account must not silently send under another. Copy files before the item provider's temporary access ends; avoid treating shared URL text as a command or automatically fetching it.

Apple documents Share Extensions as hosted extensions using `NSExtensionContext` and item providers, with extension point `com.apple.share-services` ([Share extension guide][apple-share]). Shared storage is a separate App Group container, with coordinated access by both processes; it does not give access to the app's private container ([common extension scenarios][apple-scenarios]). Apple's archived extension overview does not provide a supported Share Extension contract for opening the containing app; its documented `openURL` exception is for the historical Today extension. No modern alternative was verified here, so this assessment does not approve a forced-open implementation ([extension overview][apple-overview]).

## Repository evidence

| Existing boundary | Observed baseline | Consequence |
| --- | --- | --- |
| Xcode project / `Avibe` scheme | App embeds and depends on `NotificationService` and `AvibeLiveActivity`; both are app-extension products. Minimum iOS version is 17.4. | Reuse target/dependency/embed structure; do not create another top-level app. |
| Main entitlements | App Group `group.bot.avibe.ios`, APNs, two associated domains, private app and shared push keychain groups. | Existing shared storage namespace is available in the model. Repository declarations do not prove live Apple grants. |
| Notification Service | Same App Group plus the push keychain group. | Existing extension demonstrates shared-data packaging, but its push credentials are not share authorization. |
| Live Activity | No entitlements file; verifier prohibits shared App Group and push permissions and restricts keychain sharing. | Preserve its isolation when adding the new extension. |
| `MobileAccount.swift` / `MobileAuthTransport.swift` | Account data-store identifiers live in app `UserDefaults.standard`; authentication uses a dedicated `WKWebsiteDataStore` and same-origin requests in a native-managed WebView. | App Group membership alone does not share account cookies or the direct transport. |
| `PushKeychainStore.swift` | Private group owns push installation management state; shared push group owns notification decryption records. | Neither should be granted to a Share Extension simply to make login work. |
| `testflight.py` | Three signing profile specs: main, notification service, live activity. Exact archive inventory contains two extension bundle IDs. | Adding an `.appex` alone is rejected; profile preparation, export mapping and archive verification must change together. |
| `ios-testflight.yml` | Manual dispatch from master; manual distribution signing; archive verification before `-exportArchive` with upload destination. | No auto-signing or portal mutation is needed in CI if the new profile is provisioned beforehand. There is no separate exported-IPA verifier today. |

Primary code examined: `ios/Avibe.xcodeproj/project.pbxproj`, shared `Avibe.xcscheme`, all three shipped bundle Info.plists and privacy manifests, both entitlements files, `ios/scripts/testflight.py`, `.github/workflows/ios-testflight.yml`, `.github/workflows/ios-shell.yml`, `tests/test_ios_testflight.py`, and the account/auth/keychain source above. The README's TestFlight prose lags the three-bundle implementation; use code as the inventory baseline and update the prose during implementation.

## Local Team/profile observation

Observation began at **2026-11-18 01:08:39 UTC / 09:08:39 UTC+08:00**. Only sanitized summaries were printed.

| Read-only check | Result | Limit |
| --- | --- | --- |
| `security find-identity -v -p codesigning`, default user search list | Exit 0; zero matching valid identities for Team `68B2SQ6YK3`. | Does not unlock/import keychains or examine CI signing material. |
| Public certificates in the same default keychain scope, filtered by subject OU | Exit 0; zero matching Team certificates. | No private-key extraction or signing. Other/offline keychains were not searched. |
| Legacy profile directory `~/Library/MobileDevice/Provisioning Profiles` | Directory absent. | No profile type, expiry, or grants to report. |
| Xcode 16+ profile directory `~/Library/Developer/Xcode/UserData/Provisioning Profiles` | Directory present; zero `*.mobileprovision` files. | Same; no decoded profile bytes were printed or saved. |
| `~/Library/Preferences/com.apple.dt.Xcode.plist` | Readable; no reference to the requested Team ID. | A cache observation, not proof of account membership or permissions. No account identifiers printed. |

No keychain unlock, password query, Xcode account refresh, Apple login, authenticated portal/API call, certificate/profile installation, signing, upload, simulator or device execution occurred. Vault and Harness playbooks were loaded before this work; no secret retrieval was necessary.

## Apple setup needed after authorization

1. Register or confirm an explicit extension App ID under Team `68B2SQ6YK3`, for example `bot.avibe.ios.share`. This identifier is proposed, not reserved or availability-checked. It must be distinct from the containing app and the existing extensions.
2. Enable App Groups for that App ID and assign the **existing** `group.bot.avibe.ios`, if the authorized account confirms that group. A second App Group is unnecessary for a draft inbox. Apple documents explicit group assignment to each App ID and requires Account Holder/Admin for these capability changes ([enable capabilities][apple-capabilities]). Register a group only if the existing one is absent; group registration also requires Account Holder/Admin ([register group][apple-group]).
3. Generate/download an App Store Connect distribution profile for the new App ID that authorizes the selected App Group and the distribution certificate already imported by CI. Apple profiles select an explicit App ID and a distribution certificate ([distribution profile][apple-profile]). A development profile/identity is also needed for signed development-device validation if manual signing is used there.
4. Install the new profile in the authorized CI secret store, proposed name `IOS_SHARE_EXTENSION_PROFILE_BASE64`. Reuse the existing distribution certificate and App Store Connect uploader if authorized and valid; a new extension does not by itself require new signing keys or a second standalone TestFlight app.
5. Regenerate profiles for any **existing App ID whose capabilities actually change**; Apple says modifying an App ID invalidates profiles containing it ([enable capabilities][apple-capabilities]). Merely adding an extension does not justify changing the main app's entitlements or rotating all profiles.

These are future authorized steps, not actions taken. Portal existence, role, certificate validity/revocation and secret presence remain unverified.

## Minimum coherent implementation and release changes

- **Target/package:** add an extension target/product, sources/resources, dependency and embed entry in the existing app. Use `APPLICATION_EXTENSION_API_ONLY=YES`, deployment target 17.4, matching app marketing/build versions, and a Release profile setting such as `$(SHARE_EXTENSION_PROFILE_SPECIFIER)`. Keep archive inclusion through the existing dependency graph and verify it. The extension should not inherit the app's Iroh binary or whole source set unnecessarily.
- **Info/permissions:** declare `com.apple.share-services`, a principal controller or storyboard, and an activation rule limited to the chosen input types/counts. Never ship `TRUEPREDICATE`; Apple explicitly disallows it for submitted extensions ([common extension scenarios][apple-scenarios]). Add the shared App Group entitlement. For draft intake, require no APNs, associated domains, push keychain sharing, account credential sharing or background audio. Add a bundle privacy manifest describing the APIs actually used; reassess App Privacy answers if the feature sends new collected data.
- **Shared intake:** implement one bounded draft/file inbox with atomic commits and single-consumer claiming; account/destination selection and explicit sending remain owned by the main app. Existing WebKit credentials are not automatically reusable across the extension boundary. If direct sending is required instead, resolve that larger contract first. Background upload is documented but adds shared-container URL-session lifecycle work ([common extension scenarios][apple-scenarios]); it is unnecessary for local draft intake.
- **`testflight.py` inputs/prepare:** add share bundle/application identifiers, required/forbidden entitlements, profile secret to `SECRET_NAMES`, fourth profile spec, same-Team/current/App-Store/certificate checks, installed UUID tracking, share profile environment names and the fourth `export_options.provisioningProfiles` mapping. Existing UUID-driven cleanup can already handle a fourth profile when recorded correctly.
- **Archive verifier:** extend `EXTENSION_BUNDLE_IDS` to exactly the three intended extension IDs and add the share bundle to the per-bundle signature/profile contract. Existing loops can then enforce matching versions, privacy manifest presence, build number, Team/distribution identity and strict signature verification. Require the intended App Group, disallow unrelated push/credential capabilities, allow at most its own default keychain group if Xcode emits it, and verify the share extension point and production activation rule. Do not relax inventory to accept arbitrary extensions.
- **Workflow:** pass the new profile secret only into preparation; pass `SHARE_EXTENSION_PROFILE_SPECIFIER` only as the target-specific archive variable. Preserve the manual-signing, master-only, pre-upload verification and unconditional cleanup boundaries. A global main-app `PROVISIONING_PROFILE_SPECIFIER` would mis-sign extensions; retain the existing per-target pattern.
- **Export verifier scope:** the minimum required export change is the fourth bundle-to-profile mapping. Current verification examines the archive and signed entitlements; it does **not** independently decode each archive-embedded profile or inspect a re-signed exported IPA. If final exported-byte assurance is required, reuse the per-bundle checks against an unpacked IPA and each embedded profile, and split local export from upload so this runs before upload. Adding a verifier after the current upload destination would be too late. This is a distinct enhancement, not an existing capability.
- **Coverage/docs:** extend existing profile, secret, exact-inventory, version/privacy, signing and workflow contract tests for the fourth bundle; add negatives for missing share profile, wrong Team/bundle/certificate, absent group, extra extension, and leaked privileged groups. Update iOS/TestFlight setup documentation. Real acceptance must eventually cover Safari URL/text, Photos/files as selected, cancel/retry, termination, account switch and app relaunch on a physical device. No simulator validation was performed here.

## Evidence obtained and remaining gates

One real execution of the **existing** `archived_extensions` function against disposable plist-only fixtures accepted the two expected extensions, then rejected an added `bot.avibe.ios.share` with the exact-inventory error. The fixtures were removed. No target, Xcode build, test suite or signing operation was created/run. This confirms the current release gate failure directly, without implying extension runtime feasibility was tested.

Proceed with design/implementation planning if the next-open draft handoff meets I1's intended experience. Signed delivery still needs authorized Apple account setup, protected CI provisioning, the code changes above and physical-device acceptance. If I1 requires immediate sending or forced app switching, treat those as unresolved architecture/UX requirements rather than assuming App Groups solves them.

The read-only assessment made no implementation or Apple configuration edits. Its original report was retained under `/tmp` and then copied into this outbound-sharing PR for review. Concurrent outbound implementation is assessed separately.

[apple-share]: https://developer.apple.com/library/archive/documentation/General/Conceptual/ExtensibilityPG/Share.html
[apple-scenarios]: https://developer.apple.com/library/archive/documentation/General/Conceptual/ExtensibilityPG/ExtensionScenarios.html
[apple-overview]: https://developer.apple.com/library/archive/documentation/General/Conceptual/ExtensibilityPG/ExtensionOverview.html
[apple-capabilities]: https://developer.apple.com/help/account/identifiers/enable-app-capabilities/
[apple-group]: https://developer.apple.com/help/account/identifiers/register-an-app-group/
[apple-profile]: https://developer.apple.com/help/account/provisioning-profiles/create-an-app-store-provisioning-profile/

Apple sources above were retrieved successfully over public HTTPS on 2026-11-18. The extension programming guide is archived guidance; no claim of current-device behavioral validation is made.
