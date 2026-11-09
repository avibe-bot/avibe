# Avibe Android App · Plan

> **Status**: Direction approved by the owner on 2026-11-05 (section 13).
> China-first, no-GMS, APK-only plan; nothing here is built yet.
> **Owner**: unassigned
> **Date**: 2026-11-05

## 1. Recommendation

Build a **thin native Android shell in Kotlin around the Android System
WebView**, in parallel with the [iOS app](ios-app.md). The transcript, Inbox,
Settings, Apps, Show Pages, message and task behavior, drafts, and recovery
logic remain the user's own Avibe Web UI. Native code owns the operating-system
boundary: the WebView lifecycle, origin-scoped bridge, microphone grant, file
pickers and downloads, back navigation, APK installation and updates, and later
Android shortcuts, share targets, tiles, and widgets.

The first and ongoing distribution channel is a signed APK for known users.
There is no app-store milestone. Foreground realtime and message catch-up are
enough for internal alpha testing. Daily use additionally depends on a
separately designed shared-core capability that can notify a user-authorized
WeChat or Feishu/Lark private destination. Formal Xiaomi, OPPO, vivo, Honor, or
Huawei push is not a product dependency.

The iOS and Android shells share one versioned contract,
`window.avibeNative`, but not a shell framework. Swift and Kotlin are small
platform adapters around the same Web UI. Tauri and Capacitor would still
leave permissions, lifecycle, update installation, push, and deep native
features in platform code, while adding a second runtime and build system.

The first Android build uses the web composer. A native Android composer is a
separate decision after real-device tests. iOS dropped its native composer on
2026-11-08 (iOS plan section 5); that is no evidence either way for Android.

Evidence levels used below:

- **verified**: read in this repository at `090d00aa113d15d253309bc4b516a9a5ff647ec0`
  or fixed by an owner decision;
- **cited**: supported by an official Android, vendor, regulator, or platform
  source linked inline;
- **cited-secondary**: supported only by a secondary report and not treated as
  a settled product dependency;
- **inferred**: a design conclusion that still needs the real-device matrix.

## 2. Scope and target devices

The target is Android-compatible software on phones sold in mainland China
without depending on Google Mobile Services:

| Family | Initial scope | First-device requirement |
| --- | --- | --- |
| Xiaomi | HyperOS | At least one current Xiaomi or Redmi phone |
| OPPO | ColorOS, including OnePlus and realme variants | At least one OPPO-family phone |
| vivo | OriginOS | At least one vivo or iQOO phone |
| Honor | MagicOS | At least one current Honor phone |
| Huawei | EMUI and Android-compatible HarmonyOS releases | At least one compatible Huawei phone |

The first matrix therefore has at least five physical phones. An emulator and
one reference Android device are useful during development but cannot close a
release gate.

Native HarmonyOS, including HarmonyOS NEXT and later releases that do not run
Android applications, is outside this plan. It is a third platform with its own
toolchain, application model, distribution, and acceptance results. A Huawei or
Honor phone that still runs Android APKs remains in this plan.

The minimum Android API level and minimum supported WebView package are open
decisions. The device lab selects them from observed bridge, authentication,
media, and file behavior rather than from market-share estimates alone.

## 3. Shared product and separate shells

| Concern | Shared between iOS and Android | Platform-specific |
| --- | --- | --- |
| User experience | Web transcript, Inbox, Settings, Apps, Show Pages, message and task workflows | Keyboard integration, system navigation, permission prompts, install and update surfaces |
| Data and behavior | Instance APIs, session and message models, drafts, message catch-up, authorization rules | Activity/process restoration and WebView recreation |
| Native bridge | `window.avibeNative` methods, events, error codes, capability versions, origin rules, JSON fixtures | Swift transport with WebKit; Kotlin transport with AndroidX WebKit |
| Composer | Web composer behavior, marker grammar, draft ownership, attachment and voice pipelines | Each platform decides independently whether native text input is needed |
| Notifications | A separately designed shared-core capability for Workbench-to-bound-private-IM alerts and shared eligibility rules | Destination-specific transport behavior; Android session routing after launch |
| Acceptance | One scenario definition and expected behavior | A separate real-device result for every platform and device family |
| Release | Version semantics and release provenance principles | TestFlight/App Store tooling on iOS; signed APK and package installation on Android |

The shared contract is the reuse boundary. A scenario passing on iOS does not
count as Android evidence, and a single Android OEM does not stand in for the
five-family matrix.

## 4. Bridge contract and WebView compatibility

The [iOS plan section 4](ios-app.md#4-bridge-contract-windowavibenative-v1)
defines `window.avibeNative` v1. Android implements the same method names,
events, error shape, major-version negotiation, capability versions, origin
allowlist, and shared JSON fixtures.

Android transport uses
[`WebViewCompat.addWebMessageListener`](https://developer.android.com/reference/androidx/webkit/WebViewCompat#addWebMessageListener(android.webkit.WebView,%20java.lang.String,%20java.util.Set%3Cjava.lang.String%3E,%20androidx.webkit.WebViewCompat.WebMessageListener))
and
[`WebViewCompat.addDocumentStartJavaScript`](https://developer.android.com/reference/androidx/webkit/WebViewCompat#addDocumentStartJavaScript(android.webkit.WebView,%20java.lang.String,%20java.util.Set%3Cjava.lang.String%3E)).
Both APIs accept origin rules, and the document-start script runs before page
JavaScript. AndroidX marks both as feature-gated; the shell must call
[`WebViewFeature.isFeatureSupported`](https://developer.android.com/reference/androidx/webkit/WebViewFeature#isFeatureSupported(java.lang.String))
for `WEB_MESSAGE_LISTENER` and `DOCUMENT_START_SCRIPT` before use (**cited**).

The runtime feature check is authoritative even after a minimum WebView version
is chosen. **Inferred:** no-GMS phones receive their System WebView from OEM
firmware or vendor stores, so nominal OS version does not prove that every
required WebView feature is present.

Fallback policy:

1. With both features, expose the full origin-scoped bridge.
2. Without either feature, do not substitute a broad
   `addJavascriptInterface` bridge for remote content. Load the Web UI in
   browser mode without native capabilities, show the installed WebView
   version in diagnostics, and offer vendor-specific WebView update guidance.
3. If the Web UI itself cannot run safely, stop at an unsupported-WebView
   screen rather than entering a partially working authenticated session.

The final minimum WebView version is the lowest version that passes the full
matrix. The release checklist records the provider package and version on every
device so regressions can be traced to the actual WebView implementation.

## 5. Native shell

### Connection and navigation

The shell loads the instance directly through its tunnel host. No transcript,
workspace, or Agent-runtime traffic is proxied through avibe.bot. It uses the
same three navigation classes as
[iOS section 7](ios-app.md#7-connecting-to-the-users-avibe):

1. configured instance origins stay in the WebView and receive the bridge and
   microphone grant;
2. authentication origins stay in the WebView without bridge or microphone
   access;
3. every other origin opens in the system browser.

The first APK supports the same embedded email-code, password, and GitHub
flows as iOS Phase 0. Google rejects embedded user agents
([Google](https://developers.googleblog.com/upcoming-security-changes-to-googles-oauth-20-authorization-endpoint-in-embedded-webviews/),
**cited**), so the shell presents the email-code path until the shared
system-browser handoff endpoints in the iOS plan exist. When those endpoints
ship, Android uses a Custom Tab and an app callback rather than inventing a
second authentication protocol.

### Microphone, files, downloads, and back

For voice, the shell requests Android's `RECORD_AUDIO` runtime permission and
answers
[`WebChromeClient.onPermissionRequest`](https://developer.android.com/reference/android/webkit/WebChromeClient#onPermissionRequest(android.webkit.PermissionRequest))
only for microphone capture from a configured instance origin. The official API
requires the host app to grant or deny each request; the default is denial
(**cited**). The implementation grants only `RESOURCE_AUDIO_CAPTURE`, never
every resource in the request.

File input uses `onShowFileChooser` with Android activity-result contracts.
Downloads require a user action, preserve the server-provided filename and
content type, and hand the result to a system-visible destination. Camera
capture, photo access, and broad storage permissions are added only when an
accepted scenario requires them.

Back behavior follows this order:

1. close a native modal or picker;
2. close a Web UI overlay through the bridge;
3. go back in same-origin WebView history;
4. return to the instance picker or leave the app.

External authentication and browser pages never become part of the trusted
instance history.

### Process and renderer recovery

Activity recreation, system-initiated process death, and WebView renderer loss
are normal recovery cases. Android's state guidance says a process may be
destroyed while the user is in another app and that saved state or persistent
storage must restore the screen
([Android](https://developer.android.com/topic/libraries/architecture/saving-states),
**cited**). A renderer loss reported through
[`WebViewClient.onRenderProcessGone`](https://developer.android.com/reference/android/webkit/WebViewClient#onRenderProcessGone(android.webkit.WebView,%20android.webkit.RenderProcessGoneDetail))
destroys the unusable WebView and creates a new one.

The recovery contract stores only enough native state to reconstruct the
shared product state:

- configured instance and active session or route;
- the last acknowledged bridge contract and capability set;
- a latest draft snapshot supplied by the web composer;
- pending file tokens that still point to app-owned temporary files.

The Web UI remains authoritative for the durable draft and message model. After
recreation it reloads the session, fetches messages after the last visible
position, resumes realtime delivery, and reconciles the draft. The shell must
not replay a send whose acknowledgement is unknown.

**Inferred:** mainland OEM background policies make these cases more frequent,
so draft survival, session position, and message catch-up are first-version
requirements rather than later hardening.

## 6. Composer decision

The APK alpha ships with the web composer. The device matrix measures:

- Pinyin composition and candidate selection;
- quick paste and continued composition;
- system and third-party keyboards;
- keyboard show, hide, rotation, and resize;
- hold-to-talk, haptics, attachments, and picker placement;
- state after a short app switch and after process recreation.

If the matrix shows a product blocker that the shell cannot fix, Android may
add a native Kotlin text view behind the `composer` capability, which iOS
reserves but does not implement: specify it in iOS plan section 4 first, keep
the web composer's marker grammar, references payload, and draft ownership, and
fall back to the web composer. The implementation and estimate are made from
Android evidence.

## 7. APK distribution and updates

### Package identity and signing

The proposed package name is `bot.avibe.android`, matching the confirmed
`bot.avibe.ios` identifier. It remains open until the first APK is signed and
shared.

Android requires installed and updated APKs to be signed. Updates are accepted
only when the signing certificates match; a different certificate requires a
different package name and installs as a separate app
([Android app signing](https://developer.android.com/studio/publish/app-signing),
**cited**). Losing a self-managed signing key prevents releasing an update to
the installed app; affected users would have to uninstall and reinstall a
differently signed build, losing app-local state unless it was exported.
Therefore the package name and release signing identity are one-time decisions.

The release key is created once, held through Avibe Vault and protected CI
secret plumbing, and never stored in the repository or passed as a command-line
value. CI signs the APK and verifies it with
[`apksigner`](https://developer.android.com/tools/apksigner). APK Signature
Scheme v2 provides whole-file integrity and is available from Android 7.0
([AOSP](https://source.android.com/docs/security/features/apksigning/v2),
**cited**). The final signing configuration uses v2 or newer, plus v1 only if
the eventual minimum Android version requires it.

### In-app update flow

Each APK release publishes:

- the signed APK;
- a small manifest binding channel, version code and name, package name,
  source commit, APK URL, byte size, SHA-256, and expected APK signing
  certificate digest;
- a detached signature over that manifest from a dedicated update-metadata
  key.

The shell embeds the metadata public key and expected APK certificate digest.
It verifies the manifest before download, verifies the complete APK before
installation, and then opens a user-confirmed install session through
[`PackageInstaller`](https://developer.android.com/reference/android/content/pm/PackageInstaller).
The app declares
[`REQUEST_INSTALL_PACKAGES`](https://developer.android.com/reference/android/Manifest.permission#REQUEST_INSTALL_PACKAGES);
the user must also allow this app as an unknown-app installation source. A
denied or failed install leaves the current app runnable.

Web content is not bundled into this APK. It follows the instance release, so
the native shell should update much less often than the Web UI.

### Desktop updater reuse assessment

At the verified repository head, the desktop pipeline is specific to desktop:

- `scripts/desktop_updater.py` hard-codes macOS and Windows targets, desktop
  artifact names, Tauri signatures, and a Rust verifier;
- `.github/workflows/desktop-package.yml` seals platform-signed desktop bytes
  and produces Tauri/MiniSign manifests;
- the desktop installer owns application replacement and rollback semantics
  that do not exist in Android's `PackageInstaller`.

The Android updater therefore does **not** reuse the desktop updater code,
artifact format, or signing key. It can reuse release provenance fields,
GitHub Release staging conventions, immutable-asset checks, and the rule that
an incomplete signing configuration fails publication. Android gets its own
small manifest producer and verifier when implementation begins.

### Installation matrix

The release checklist covers first install and upgrade on every target family:
browser download, file-manager handoff, unknown-source permission, installer
warning text, successful update without data loss, cancelled update, tampered
APK, wrong signing certificate, insufficient storage, and rollback to the
still-running installed version after a failed attempt.

Brand-specific steps are documentation derived from those physical runs, not
assumed in advance. **Inferred:** vendor security prompts and setting names will
differ.

The APK is initially shared with known testers. A public download page may
change the regulatory analysis: the mainland App-filing notice applies to App
operators providing internet information services, and whether a side-loaded,
user-self-hosted Avibe fits that boundary needs legal advice before public
distribution.

## 8. Notifications and foreground realtime

The APK-only product does not depend on formal OEM push. Xiaomi, OPPO, and vivo
production push access is tied to their application review or store paths
(section 11), which this plan does not enter.

At the verified repository head, configured IM clients are transport-specific.
A Workbench session has platform `avibe`; its output goes to the Web UI's SSE
path and Web Push, and is not fanned out to a WeChat or Feishu/Lark destination
(`core/presentation.py`, `modules/im/avibe.py`, **verified**). Existing IM
configuration is therefore necessary but not sufficient for Android background
notifications.

Background alerts for Workbench and Android-started sessions require a new
shared-core capability with its own design document. This Android plan records
the dependency and requirements, not its mechanism. The separate design must
answer:

1. How a `platform=avibe` session event reaches an already bound WeChat or
   Feishu/Lark private destination. This producer does not exist today.
2. How the destination is bound to and authorized by the intended user.
3. How delivery preserves the existing owner authorization, Project filtering,
   muted-category, hidden-Session, already-read suppression, and badge rules.
4. How duplicate IM alerts are suppressed while the user is active on any
   foreground surface, accounting for the `ui_server` and controller process
   boundary described in `AGENTS.md`.
5. How the alert carries only the minimum session-location information and no
   reply body, prompt, attachment name, or other workspace content.
6. What product and security review must pass before the capability is enabled.

The supported notification model is:

1. Internal APK alpha relies on the existing instance realtime stream while
   foregrounded and fetches missed messages when opened; it makes no background
   IM-alert guarantee.
2. After the separate shared-core capability is delivered, an eligible
   Workbench event may notify the user's bound WeChat or Feishu/Lark private
   destination without duplicating an active foreground surface.
3. An IM alert may carry a session locator. The app can route that locator
   after launch, but direct launch from every IM client is not a v1 guarantee;
   each client and OEM must be tested.
4. IM remains a prompt to return, not a second transcript or message store.

A self-managed long connection in an Android foreground service is an open
evaluation, not an assumed solution. Foreground services require an ongoing
status-bar notification and are intended for work noticeable to the user
([Android](https://developer.android.com/develop/background-work/services/fgs),
**cited**). Modern Android also restricts starting them from the background
([Android](https://developer.android.com/develop/background-work/services/fgs/launch),
**cited**). A viable design would need explicit user opt-in, a persistent
notification, clear battery cost, vendor autostart and battery-whitelist
instructions, and successful long-duration tests on all five families.
**Inferred:** even with those steps, mainland OEM process policies may make it
less reliable than system push.

Vendor development or test push quotas may be used to diagnose SDK behavior,
but they are never presented as an end-user notification path.

## 9. Phases and effort

iOS and Android proceed independently. These estimates assume one Android lane
with Web UI and release help available in parallel.

### APK alpha, about 3-4 weeks

| Track | Work | Effort |
| --- | --- | --- |
| Native | Kotlin project, System WebView, instance picker, three-class navigation, bridge feature gates, microphone, files, downloads, back behavior | 2.5 weeks |
| Shared Web UI | Android shell detection, bridge fixtures, recovery hooks, diagnostics | 1 week, parallel |
| Release | Package and signing decision, CI-signed APK, signed update manifest, install/update path | 1 week, parallel |
| Devices | Bring-up on one reference phone and two mainland OEM phones | 0.5 week, overlapping |

Exit: connection, login, chat, voice, attachments, draft restore, renderer
recovery, message catch-up, and signed update pass on the three alpha devices.
Background IM alerts are not an internal-alpha exit condition.

### APK daily use, about 2-3 additional Android weeks

- complete all five OEM families;
- close process-death, WebView-version, keyboard, file, installer, and network
  failures found by the matrix;
- verify foreground realtime and reconnect behavior over long sessions;
- deliver the separately designed shared-core Workbench-to-IM notification
  capability after its product and security review;
- write the installation and update runbook from observed device prompts;
- make a go/no-go decision on a native composer and foreground-service
  connection.

Exit: the section 10 checklist passes separately on every target family. No
store account, software copyright registration, App filing, or vendor-push
approval is an Android dependency. Shared-core notification effort is estimated
in its own design rather than inside the Android shell estimate.

### Deep Android capabilities, about 2-4 weeks as selected

Potential follow-ups are a share target, app shortcuts, a Quick Settings tile,
home-screen widgets, and system-browser authentication handoff. Each ships only
when it closes a concrete mobile workflow. Health Connect and native HarmonyOS
need separate plans.

## 10. Real-device exit checklist

Record OS build, WebView provider and version, app version, network, battery
mode, and result for every run.

| # | Check | Pass |
| --- | --- | --- |
| 1 | Install the signed APK from the supported download path | Installer identity and warnings are understood; app launches |
| 2 | Update from the previous APK | Same package and certificate replace in place; app data and configured instances survive |
| 3 | Bridge feature gate | Full bridge appears only when both required WebView features exist; unsupported mode is explicit |
| 4 | Email-code, password, and GitHub login | Login completes and survives cold launch; Google shows the documented fallback |
| 5 | Navigation classes | Instance and auth origins stay correctly scoped; external links leave the app |
| 6 | Microphone and voice | Android permission is requested once as expected; only trusted instance origins receive audio; real dictation completes |
| 7 | Composer and keyboards | Pinyin, paste, third-party keyboard, rotation, and hold-to-talk remain usable for 10 minutes |
| 8 | Attachments and downloads | Documents and images upload; downloads reach a user-visible destination with correct name and type |
| 9 | Back behavior | Overlay, WebView history, instance picker, and app exit occur in that order |
| 10 | Short app switch | Session, scroll position, draft, and voice state recover without duplicate send |
| 11 | Activity and process recreation | Active session and draft restore; messages missed while away are fetched once |
| 12 | Renderer exit | Dead WebView is replaced; session and draft recover without reusing the old renderer |
| 13 | IM notification path, after the shared capability is delivered | An eligible reply produces one minimal locator alert at the authorized private destination after existing delivery rules; an active foreground surface gets no duplicate; Android routes the locator where supported |
| 14 | Foreground realtime | New messages appear, disconnects reconnect, and opening after a gap fills all missed messages |
| 15 | Protected Vault passkey | Works in Android WebView, or a browser fallback is confirmed |
| 16 | Mainland network | Login, Web UI assets, message stream, file transfer, and voice realtime meet recorded latency and reliability thresholds |
| 17 | Tampered or wrong-key update | Verification blocks installation and the current app remains runnable |

Shared mobile scenarios and bridge fixtures should describe the same behavior
for iOS and Android. This table still requires a separate result for every
Android family; shared definitions do not merge evidence.

## 11. If app stores are reconsidered

This section records constraints already discovered. It is not an Android
milestone, and no store account, filing, certificate, or push integration
should be started without a new owner decision.

### Vendor gates

- **Xiaomi**: Xiaomi's current publishing guide says its app store no longer
  accepts individual-developer applications
  ([Xiaomi](https://dev.mi.com/xiaomihyperos/documentation/detail?pId=1710),
  **cited**). Xiaomi Push says notification messages require the app to pass
  Xiaomi security review and be published; internal apps may use non-public
  listing
  ([Xiaomi](https://dev.mi.com/xiaomihyperos/documentation/detail?pId=2057),
  **cited**).
- **OPPO**: OPPO's developer forum says individual accounts cannot apply for
  OPPO PUSH and that notification-bar push rights are opened after the app is
  listed in the OPPO Software Store
  ([OPPO](https://open.oppomobile.com/bbs/forum.php?mod=viewthread&tid=233546&page=1&extra=#pid396863),
  **cited**).
- **vivo**: vivo's publishing guide says an app is submitted through the
  developer platform for review and publication
  ([vivo](https://dev.vivo.com.cn/documentCenter/doc/301), **cited**).
  Tencent Cloud's vivo-channel guide states that the vivo-side application
  must pass vivo's review and be listed before the push service is enabled
  ([Tencent Cloud](https://cloud.tencent.com/document/product/548/36660),
  **cited-secondary**).
  Its push rules apply message categories and quotas; unlimited system-message
  treatment requires platform review rather than being a default for ordinary
  Agent replies
  ([vivo](https://dev.vivo.com.cn/documentCenter/doc/156),
  [vivo](https://dev.vivo.com.cn/documentCenter/doc/693), **cited**).
- **Huawei**: secondary guides say individual publishing may be available, but
  this has not been verified against a current official AppGallery policy
  ([2026 secondary guide](https://www.3kgroup.com/how-to-register-android-developer-accounts-in-china-2026-guide/),
  **cited-secondary**).

These gates are also why formal vendor push is absent from the APK-only
notification design. If stores return, notification payloads must contain only
an event type and non-sensitive locator, then fetch details from the
authenticated instance after the user opens the app. Unlike the iOS
Notification Service Extension design, vendor system notifications cannot
assume app code runs before the user taps: Xiaomi documents application
callbacks after the user clicks a notification, and OPPO documents system
display while the application is not running
([Xiaomi](https://dev.mi.com/xiaomihyperos/documentation/detail?pId=1291),
[OPPO](https://open.oppomobile.com/new/developmentDoc/info?id=11224),
**cited**).

### Filing, software copyright, and AI review

The Ministry of Industry and Information Technology's 2023 notice requires App
operators providing internet information services in mainland China to file
and says distribution platforms must not distribute unfiled Apps; provincial
review is due within 20 working days after complete materials are received
([MIIT](https://www.miit.gov.cn/zwgk/zcwj/wjfb/tz/art/2023/art_920db564162e4312916a01bed6540ad8.html),
**cited**). The filing path goes through the network access provider. Avibe's
user-hosted instance plus overseas `avibe.bot` tunnel does not map cleanly onto
that model and requires a filing specialist or legal opinion.

Alibaba Cloud's filing guide says only approved suffixes in its list can be
filed. `.bot` is absent from that list as checked on 2026-11-05
([Alibaba Cloud](https://help.aliyun.com/zh/icp-filing/basic-icp-service/appendix-1-domain-name-suffixes-that-can-be-filed-with-the-ministry-of-industry-and-information-technology),
**cited**). A store plan therefore cannot assume the current tunnel hostname is
fileable.

Reports reproducing a China Copyright Protection Center notice say that from
2026-03-15 the software-copyright application includes a handwritten
declaration that AI was not used to develop code, write documentation, or
generate application materials, with false declarations entering a dishonesty
record
([China Audio-video and Digital Publishing Association](https://www.capda.org.cn/art/2026/3/17/art_290_15999.html),
**cited-secondary**). The original form and its application to Agent-authored
software were not independently verified here. Avibe contains substantial
Agent-authored code, so any software-copyright path needs legal advice and must
never use a false declaration.

Industry articles also report that some Chinese stores ask AI applications for
algorithm or large-model filing or registration evidence
([industry summary](https://www.haimacloud.com/blog/news/ai-big-model-algorithm-filing-and-app-filing/),
**cited-secondary**). No current official store rule was found that resolves
whether a client for the user's own machine and user-selected model is the
regulated service provider.

Apple separately exposes an `ICP Filing Number Missing` status for mainland
China App Store availability
([Apple](https://developer.apple.com/help/app-store-connect/reference/app-information/app-information),
**cited**). The existing Individual Apple account decision remains unchanged;
mainland App Store distribution would need its own subject-consistency and
filing review. It does not affect this APK-only Android plan.

## 12. Risks and open questions

- **Mainland reachability.** **Inferred:** login, Web UI assets, file transfer,
  and realtime voice over the overseas `avibe.bot` tunnel may have unstable
  latency or availability. Measure them on mainland mobile and Wi-Fi networks.
- **Protected Vault passkeys.** WebAuthn with PRF in the cross-origin
  `sandbox.avibe.bot` iframe is unverified in Android System WebView.
- **WebView fragmentation.** Required AndroidX WebKit features may be missing
  or broken on a supported-looking OEM build.
- **Process survival.** A foreground service may still be stopped by the user,
  the OS, or vendor policy; it cannot become a hidden push substitute.
- **APK trust and updates.** Users must understand unknown-source installation,
  verify the source, and keep the package on the one signing identity.
- **Native HarmonyOS.** Users on devices that no longer run Android APKs need a
  separate product plan.

## 13. Owner decisions

Settled:

1. **Approved 2026-11-05:** build iOS and mainland no-GMS Android in parallel.
   They do not need to release on the same day, and Android work does not block
   iOS TestFlight.
2. **Approved 2026-11-05:** Android is a thin Kotlin and System WebView shell.
   Share the Web UI, product behavior, instance APIs, `window.avibeNative`
   contract, fixtures, and scenario definitions. Keep platform shells and
   device evidence separate; do not add Tauri or Capacitor.
3. **Approved 2026-11-05:** decide the native composer independently from
   Android real-device results.
4. **Approved 2026-11-05:** distribute Android only as signed APKs until the
   owner explicitly reopens app stores.
5. **Approved 2026-11-05:** use existing WeChat and Feishu/Lark channels plus
   foreground realtime. Configuration alone does not fan out Workbench output;
   daily use depends on a separately designed shared-core notification
   capability, while internal APK alpha relies on foreground realtime and
   message catch-up. Formal vendor push is out, and a long-connection foreground
   service is only an evaluation.
6. **Approved 2026-11-05:** native HarmonyOS is a separate future platform.

Open:

7. Confirm the package name (`bot.avibe.android` proposed) before the first
   release key and APK.
8. Choose the minimum Android API level and minimum supported WebView from the
   device lab.
9. Name the exact first five physical devices and their owners.
10. Keep APK access invite-only, or later permit a public download after legal
    review.
11. Decide whether to evaluate an opt-in foreground-service connection after
    the APK daily-use matrix.
12. Assign the Android track owner.
