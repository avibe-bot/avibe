# Avibe iOS App · Plan

> **Status**: Proposed, awaiting owner decisions (section 12). Plan and spike only;
> nothing here is built yet.
> **Owner**: cyhhao
> **Date**: 2026-11-02

## 1. Recommendation

Build a **thin native iOS shell in Swift/SwiftUI around `WKWebView`** that loads
the user's own Avibe Web UI from that instance's tunnel host, exactly as the
desktop app wraps the same UI. The Web UI stays the product; native code owns
only what a Home Screen web app cannot do: a microphone grant that does not
re-ask, APNs push, haptics, sign-in through the system browser, an instance
picker, and later App Intents, Share Sheet, and widgets.

- **Microphone**: the shell answers WebKit's media-capture permission request
  itself (`WKUIDelegate`, iOS 15+), so WebKit never shows its own prompt; iOS
  shows its system microphone dialog once per install. A sealed spike confirmed the mechanism and that the production dictation
  pipeline runs unchanged in a `WKWebView` (section 7). An iPhone run is still
  required and is the exit gate of Phase 0.
- **Technology**: plain Swift, not Tauri 2 mobile or Capacitor. Nothing in
  `desktop/` is reusable on iOS, and the native pieces worth having (Notification
  Service Extension, App Intents, widgets, Share extension) are separate Xcode
  targets either way.
- **Connection**: the app talks to the instance tunnel directly; avibe.bot stays
  the control plane. Phase 0 signs in inside the WebView (email code, password,
  GitHub); Phase 1 adds system-browser sign-in with a one-time handoff so Google
  and Sign in with Apple work.
- **Push**: the app registers as one more Web Push subscriber whose endpoint is
  an APNs relay on avibe.bot. The instance keeps encrypting with RFC 8291 and
  keeps every dispatch rule (muted categories, hidden Sessions, read
  suppression, authorization gates) with no change; the relay forwards
  ciphertext it cannot read, and a Notification Service Extension decrypts on
  the phone. This is a new path through avibe.bot and needs product and security
  review.
- **First version**: Phase 0, about 2 weeks for one native lane plus a few days
  of Web UI work: the shell, microphone grant, haptics, in-WebView sign-in,
  TestFlight internal build. It fixes the microphone. Push (Phase 1, about 2-3
  weeks calendar with three parallel lanes) is what makes it a daily driver.

Evidence levels used below: **verified** means observed in the 2026-11-02 spike
or read in this workspace's code; **cited** means Apple, WebKit, Google, or
vendor documentation linked inline; **inferred** means reasoned from shared
WebKit code and not yet observed on an iPhone. iOS device behavior is inferred
until Phase 0's exit gate.

## 2. Background

A Home Screen web app on iOS asks for the microphone again and again:

- WebKit bug [327237](https://bugs.webkit.org/show_bug.cgi?id=327237) (filed
  2026-10-12, status NEW, rdar://189868738) reports that a Home Screen web app
  does not persist camera/microphone permission. Its description notes that a
  `WKWebView` app can remember the decision itself through the iOS 15
  `WKUIDelegate` API.
- WebKit keeps a granted capture request only while capture runs (up to 24
  hours) and clears it about a minute after capture stops; see youenn fablet's
  explanation in [bug 215884 comment 15](https://bugs.webkit.org/show_bug.cgi?id=215884#c15).
  In WebKit source, `UserMediaPermissionRequestManagerProxy::watchdogTimerFired`
  clears `m_grantedRequests`
  ([UserMediaPermissionRequestManagerProxy.cpp](https://github.com/WebKit/WebKit/blob/main/Source/WebKit/UIProcess/UserMediaPermissionRequestManagerProxy.cpp)),
  and `WebPreferencesDefaultValues.cpp` sets the iPhone re-prompt interval for
  requests without a user gesture to one minute. That matches the owner's
  observation of "about a minute", and every cold launch starts with an empty
  cache.
- Apple's documentation for
  [`webView(_:requestMediaCapturePermissionFor:initiatedByFrame:type:decisionHandler:)`](https://developer.apple.com/documentation/webkit/wkuidelegate/webview(_:requestmediacapturepermissionfor:initiatedbyframe:type:decisionhandler:))
  states that without this method the system answers `.prompt`. A native app
  that implements it can answer `.grant`.

Voice is becoming the main way to reach the main Bot on a phone (hold the center
button, hold the composer), so a prompt every minute is a product blocker, and
the PWA cannot fix it.

## 3. Shape: a native shell around the same Web UI

| Concern | Stays web | Goes native |
| --- | --- | --- |
| Workbench, chat, Inbox, Settings, Apps, Show Pages | Everything | - |
| Voice capture and dictation | `getUserMedia`, AudioWorklet, WAV segments, realtime socket (unchanged) | Answer the media-capture permission; `NSMicrophoneUsageDescription` |
| Notifications | Inbox, categories, preferences UI | APNs registration, Notification Service Extension, tap routing |
| Sign-in | Instance login pages, avibe.bot login form | Phase 1: `ASWebAuthenticationSession` and handoff |
| Instance choice | - | Instance list, add by URL or QR, switch |
| Feel | Layout, safe areas via CSS | Haptics bridge, status bar color, keyboard accessory polish |
| Reach (Phase 2) | Deep-link targets | App Intents, Action Button, Control Center control, Share extension, widgets |

A native client would duplicate a Web UI that ships several times a week, and
the desktop app already proves the wrapper model (`desktop/README.md`: the
desktop shell loads the loopback Workbench and adds native capabilities through
`window.__AVIBE_DESKTOP_*` flags, see `ui/src/lib/desktopShell.ts`).

## 4. Technology

| Criterion | Swift/SwiftUI + `WKWebView` | Tauri 2 mobile | Capacitor |
| --- | --- | --- | --- |
| Media-capture grant | Implement the `WKUIDelegate` method directly | wry's UI delegate already answers grant when no handler is set (`wry_web_view_ui_delegate.rs`), with less control over origin scoping | Plugin or native patch |
| Remote web content | Native: load any configured origin | Possible (external URL plus remote IPC capability), but the toolchain is built around bundled assets | `server.url` is documented as "not intended for use in production" ([config docs](https://capacitorjs.com/docs/config)) |
| APNs push | First-party APIs | Official notification plugin is local-only; remote push needs a community plugin ([example](https://github.com/yanqianglu/tauri-plugin-mobile-push)) | Official push plugin (returns the APNs token on iOS) |
| Notification Service Extension, widgets, App Intents, Share extension | Xcode targets | Extra Xcode targets added to the generated project | Extra Xcode targets |
| Background audio | Not planned (section 7); same API surface everywhere | Same | Same |
| Keyboard and safe areas | Direct `UIScrollView` and view control | Through wry | Plugin configuration |
| Web content updates | Live from each instance, no app release | Same | Same |
| Reuse from `desktop/` | None needed | Little: `desktop/src-tauri` is loopback runtime bootstrap, updater, pet, computer use, macOS window chrome, SSE notifications | None |
| Skills and debugging | Swift is new to the repository; Agents write it well; Safari Web Inspector works with `isInspectable` | Rust plus Swift plus generated Xcode project | JS plus Swift plus generated project |

Recommendation: **Swift/SwiftUI + `WKWebView`**. The shell is small (a single
web view, a delegate, a bridge, a picker), every high-value native feature is an
Xcode target anyway, and there is no shared code to gain from Tauri. Capacitor's
model of bundled web assets fights the core requirement: load a per-user remote
origin, including Enterprise custom hostnames.

Do **not** opt into App-Bound Domains. Instance origins vary per user and the
limit is 10 domains ([WebKit blog](https://webkit.org/blog/10882/app-bound-domains/)).
Without the opt-in, message handlers and user scripts work on every origin, and
service workers are unavailable (WKWebView exposes them only with App-Bound
Domains; see [MDN BCD discussion](https://github.com/mdn/browser-compat-data/pull/30146)).
The Web UI already guards on `'serviceWorker' in navigator`, so this disables
Web Push and the SW badge path inside the shell, which native push replaces.

Code location: an `ios/` directory in this repository, beside `desktop/`, so
that a bridge change and its Web UI consumer land in one PR (owner decision).

## 5. Connecting to the user's Avibe

### Invariants

- The app loads the instance directly through its tunnel host
  (`*-app.avibe.bot`, or an Enterprise custom hostname). No UI, workspace, or
  Agent runtime traffic passes through avibe.bot, matching the product
  boundary in the workspace `AGENTS.md` and `avibe-bot-backend/AGENTS.md`.
- avibe.bot keeps its control-plane roles (identity, policy, pairing, tunnel).
  New avibe.bot pieces are control-plane functions: an APNs relay for encrypted
  payloads, Sign in with Apple, account deletion, and optionally an instance
  list.
- Navigation has three classes:
  1. **Instance origins** (configured by the user) load in the WebView and get
     the native bridge and the microphone grant.
  2. **Authentication origins** load in the WebView without bridge or
     microphone access: avibe.bot, the Supabase Auth host avibe.bot uses, and
     GitHub's OAuth pages. Phase 0 sign-in depends on this class (see Signing
     in). Google's authorization endpoint is intercepted instead, because
     Google rejects embedded webviews.
  3. **Everything else** opens in `SFSafariViewController`.

  The bridge checks the sender's `WKScriptMessage.frameInfo.securityOrigin`
  and the media-capture delegate checks the requesting origin against class 1
  on every call, so a page in class 2 or 3 never reaches either.
- The microphone grant follows the instance origin, the trust boundary the Web
  UI already uses. Show Pages are same-origin-trusted by product decision: the
  Workbench frames them with `allow-same-origin` and they can reach the parent
  document (`ui/src/components/workbench/ChatPage.tsx`,
  `ui/src/apps/ShowPageApp.tsx`). A frame or path check could not exclude them,
  so the shell does not pretend to. An Agent-authored Show Page can therefore
  start capture without a WebKit prompt; the iOS recording indicator still
  shows, and an Agent already has full access to the machine. If this needs a
  real boundary later, Show Pages must move to a separate origin.
- Phase 0 adds no credential: the WebView holds the same
  `__Host-vibe_remote_session` cookie a browser holds (`vibe/remote_access.py`,
  24-hour personal session, renewed after half its life).

### Adding an instance

1. Paste the instance URL, or scan a QR code shown by the Web UI on a computer
   (new "Open on iPhone" entry in Settings that encodes the instance origin).
2. Phase 2: sign in to avibe.bot in the app and pick from the instances the
   account can use. The backend already has an owner-scoped `GET /api/instances`
   for the console session; an app-facing list that also includes shared
   instances is a small backend addition.

Pairing (`/api/v1/pairing/redeem`) is between an instance and avibe.bot and is
not involved: the phone is a client of an already-paired instance, like a
browser.

### Signing in

Phase 0, inside the WebView: the instance redirects to avibe.bot's OIDC
authorize page as it does for a browser, and the user signs in there with the
email code, password, or GitHub. The whole round trip (instance, avibe.bot,
Supabase Auth, GitHub, back to the instance's `/auth/callback`) stays in the
WebView through the authentication-origin class above, so the session cookie
lands in the WebView's own store. Google blocks OAuth in embedded webviews with
`disallowed_useragent` ([Google](https://developers.googleblog.com/upcoming-security-changes-to-googles-oauth-20-authorization-endpoint-in-embedded-webviews/)).
The shell intercepts navigation to Google's authorization endpoint and explains
that the email code works for the same account. Supabase links identities with
the same email address to one user
([Supabase](https://supabase.com/docs/guides/auth/auth-identity-linking)); confirm
on the owner's account. The magic link in the email opens Safari, not the app,
so the shell's copy points to the code.

Phase 1, system browser plus handoff:

1. The app creates a random verifier and opens
   `https://<instance>/auth/app/start?challenge=<S256(verifier)>&next=/` in
   `ASWebAuthenticationSession` with callback scheme `avibe`.
2. The instance runs its normal OIDC login against avibe.bot in that browser
   context (Google works there, and Sign in with Apple once avibe.bot offers
   it).
3. On success the instance redirects to `avibe://auth/handoff?code=<one-time>`
   instead of landing the browser.
4. The app posts `code` and `verifier` to `https://<instance>/auth/app/redeem`
   from inside the `WKWebView`. The instance checks the code (single use, short
   TTL, bound to the challenge) and sets `__Host-vibe_remote_session` in the
   WebView's cookie store, then redirects to `next`.

This mirrors the pattern used for Hotwire Native apps
([gist](https://gist.github.com/strzibny/aad1439326c1b47a8d066d45b133fcb6)). The
one-time code store can follow the in-memory, single-use, TTL-pruned handshake
store introduced for the PWA callback desync
(`docs/plans/pwa-oauth-login-state-desync.md`), which solved the same
multi-context problem for Home Screen web apps. A custom-scheme
callback (`ASWebAuthenticationSession.Callback.customScheme`) avoids per-host
associated domains, which arbitrary instance hosts cannot provide
([Apple](https://developer.apple.com/documentation/authenticationservices/aswebauthenticationsession/callback)).
The two new instance endpoints are an authentication change and need security
review.

### Keeping the session

`WKWebsiteDataStore.default()` persists cookies across launches. Opening the
app at least once a day keeps the 24-hour session rolling. When it lapses, the
WebView bounces to avibe.bot: in Phase 0 a still-valid avibe.bot session in the
WebView reduces this to the authorize step; Phase 1 re-runs the handoff, which
costs one system sheet. A longer-lived app session would be a separate security
decision and is not proposed.

### Several instances

Each instance is its own origin, so cookies coexist in one data store. The
picker switches the loaded origin; the microphone grant and bridge allowlist is
the set of configured origins; each instance has its own push subscription.

### Known risk: Protected Vault passkeys

Protected Vault setup and unlock run WebAuthn with PRF inside the cross-origin
`https://sandbox.avibe.bot` iframe (`docs/plans/vault-crypto-sandbox.md`). Apple
ties passkey use in third-party apps to the relying party's `webcredentials`
associated domain. Whether a WebAuthn ceremony in a cross-origin iframe works
inside the shell, even with `webcredentials:sandbox.avibe.bot` and a matching
`apple-app-site-association` on that host, is unverified. Phase 0 must test it;
the fallback is to open Vault approvals in Safari.

## 6. Push

### Today

`core/web_push_notifications.py::_send_to_enabled_subscriptions` waits 3 s,
drops already-read messages, resolves the owner's authorization, filters by
Project access, skips owners for whom the Session is hidden
(`WEB_PUSH_DISPOSITION_HIDDEN`), skips owners whose category is muted
(`core.notification_categories`, `core.notification_preferences.category_is_loud`),
computes a per-owner badge, and then calls `core/web_push.py::send_web_push`
(pywebpush, VAPID, TTL 12 h) for each enabled subscription. A 404 or 410
disables the subscription. `storage/web_push_service.py::validate_subscription_payload`
accepts any HTTPS endpoint.

### Design: a Web Push to APNs relay

```text
Instance (unchanged dispatch)
  -> RFC 8291 encrypted Web Push, VAPID-signed
  -> POST https://avibe.bot/api/push/apns/<sealed-endpoint>      (new, avibe-bot-backend)
       verifies the VAPID key bound at registration, cannot decrypt
  -> APNs (HTTP/2, provider token from the .p8 key), mutable-content: 1
  -> Notification Service Extension on the phone
       decrypts with the per-instance key in the app-group Keychain,
       sets title, body, badge, thread id, deep link
```

Registration: when the user enables notifications in the shell, the Web UI asks
the bridge for a subscription. The app requests notification permission,
obtains the APNs device token, creates a P-256 key pair and auth secret for this
instance, fetches the instance's VAPID public key
(`/api/web-push/vapid-public-key`), and asks the relay to seal
`{device token, environment, VAPID key hash, key id}` into an opaque endpoint.
The Web UI posts `{endpoint, keys}` to `/api/web-push/subscriptions` exactly as a
browser does.

Precedent: the Mastodon client Toot! uses the same pattern
([toot-relay](https://github.com/DagAgren/toot-relay)): a relay forwards
encrypted Web Push payloads to APNs and a service extension decrypts them.
Apple recommends encrypting sensitive payload data and decrypting it in a
service extension, and caps a regular payload at 4 KB
([Apple](https://developer.apple.com/documentation/usernotifications/generating-a-remote-notification)).

### What carries over and what changes

| Behavior | Result |
| --- | --- |
| Muted categories | Unchanged: the instance skips muted owners before any subscription is touched. |
| Hidden Sessions | Unchanged: hidden owners are dropped before delivery. |
| Read suppression, authorization and Project gates | Unchanged. |
| Badge | `badge_count` is already in the payload; the extension sets it. |
| App deleted | APNs answers 410 to the relay, the relay answers 410, the instance disables the subscription (existing path). |
| Payload size | Body is capped at 240 characters, but `json.dumps` escapes non-ASCII to `\uXXXX`, and the title is unbounded. Add a byte budget so the base64 ciphertext plus envelope stays under 4 KB. |
| Settings device list | Label relay subscriptions as "iPhone app". |

The extension does not need to suppress notifications, which would require
Apple's filtering entitlement, because the instance never sends muted or
hidden items.

### Ownership

| Part | Repository |
| --- | --- |
| Shell bridge, APNs registration, key storage, Notification Service Extension, tap routing | avibe (`ios/`) |
| Shell-aware notification toggle | avibe (`ui/`) |
| Payload byte budget, subscription label | avibe (`core/`, `storage/`) |
| Relay registration and delivery routes, APNs provider key, rate limits | avibe-bot-backend |
| User documentation | avibe-docs (English and Chinese) |

`avibe-bot-backend/AGENTS.md` forbids a new content-processing path without
product and security review. The relay carries only ciphertext, but it is a new
path through the backend, so the review is required before Phase 1 ships.

## 7. Voice

### Verified by the spike (2026-11-02)

Environment: macOS 26.6.2, `WKWebView` in a minimal Swift program, WebKit
private preferences for mock capture devices and a mock prompt, and the
inactive re-prompt interval set to 1 minute. The page used the production
`VoiceRecordingPipeline` from `ui/src/lib/voiceRecording.ts` (AudioWorklet,
16 kHz PCM, WAV segments) bundled with Vite, streaming over a WebSocket to a
local echo server that stood in for avibe.bot's `/api/cloud/voice/realtime`.

| Step | Result |
| --- | --- |
| 1. First `getUserMedia` | Delegate asked (#1), answered grant; device ready in 123 ms. Pipeline ran 5 s: 20 PCM chunks, 77,611 samples, WAV segments 64,044 / 64,044 / 59,266 bytes, 155,222 bytes echoed. |
| 2. Again right after stopping | Delegate not asked (WebKit's grant cache); 1 ms. |
| 3. Again after 75 s idle | Delegate asked (#2), answered grant silently in 2 ms with no UI. Pipeline ran 3 s: 12 chunks, 46,379 samples, 92,758 bytes echoed. |
| Control: delegate answers deny | `NotAllowedError`. |

Conclusion: the moments where a Home Screen web app re-asks are exactly the
moments where the shell's delegate is asked and can grant silently, and the
production capture pipeline works inside `WKWebView`. The macOS and iOS
`WKWebView` share this UIProcess permission code
(`UserMediaPermissionRequestManagerProxy`).

### Not yet verified

This machine has no Xcode or iOS Simulator. Phase 0's exit gate is an iPhone
run of the TestFlight build:

1. The iOS microphone permission dialog appears once per install (TCC), then
   never again across idle periods, background and foreground, and cold
   launches.
2. Hold-to-talk on the composer and the center button dictates into the real
   avibe.bot realtime socket.
3. The orange recording indicator appears while capturing (App Review 2.5.14).
4. Protected Vault passkey behavior (section 5).

### Background audio

Not in v1. `voiceRecording.ts` already stops when the page becomes hidden, and
iOS WebKit interrupts audio on visibility change
(`InterruptAudioOnPageVisibilityChangeEnabled` is on for iOS in
`UnifiedWebPreferences.yaml`). Recording across app switches would need native
capture with `UIBackgroundModes: audio`, a separate project.

### Action Button and Siri (Phase 2)

An App Intent "Talk to Vibey" opens the app on the main Session with voice
armed. The Action Button and a Control Center control trigger it. Because a
button press is a tap, the Web UI needs a tap-to-start/tap-to-stop voice mode
alongside hold-to-talk.

## 8. Web UI changes

- `isIosShell()` beside `isDesktopShell()`: a `window.__AVIBE_IOS_SHELL__` flag
  injected by a user script at document start, plus capability flags
  (`__AVIBE_IOS_PUSH__`, `__AVIBE_IOS_HAPTICS__`, ...) so an old shell with a
  new UI, or a new shell with an older instance, degrades cleanly. The user
  agent also gains an `AvibeiOS/<version>` suffix for server-side logs.
- Notifications: in the shell, `getWebPushSupportState` reports a native
  provider; `WebPushControl` and the Settings page call the bridge instead of
  `PushManager`. Hide the iOS "Add to Home Screen" install hint
  (`InstallHint.tsx`) in the shell.
- Haptics: call the bridge on hold-to-talk start, stop, and cancel, and on
  send. `navigator.vibrate` does not exist on iOS.
- Voice: tap-to-toggle mode for the App Intent entry (Phase 2).
- Sign-in: a shell-aware message when the user picks Google in Phase 0.
- Settings: "Open on iPhone" QR code with the instance origin.

## 9. Distribution

### TestFlight first

Internal testing (App Store Connect users) needs no review. External testing
goes through Beta App Review. TestFlight builds expire after 90 days, so a
build pipeline is needed from the start.

### App Store review risks

| Guideline | Risk | Answer |
| --- | --- | --- |
| 4.2 Minimum Functionality: "elevate it beyond a repackaged website" | High for a bare wrapper | Native microphone handling, encrypted APNs push, App Intents and Action Button, Share extension, widgets, haptics, native instance picker. Submit to the App Store only after Phase 2. |
| 4.2.7(e): "Thin clients for cloud-based apps are not appropriate" | Medium for managed Enterprise instances | Position the app as the companion to the user's own Avibe; personal instances run on the user's machine. |
| 4.2.3(i): work without installing another app | Medium | The app needs a running Avibe; provide a review demo instance. |
| 2.1 App Completeness: demo account, back end running | Certain | A dedicated demo instance with a reviewer account, never a customer instance. |
| 4.8 Login Services | High: avibe.bot offers Google and GitHub | Add Sign in with Apple to avibe.bot before App Store submission. |
| 5.1.1(v): account deletion in the app | High: email sign-in can create an account | Add account deletion to avibe.bot and an in-app entry that leads to it. |
| 2.5.14: recording indicator | Low | System indicator plus the composer's recording state. |

Guideline text: [App Store Review Guidelines](https://developer.apple.com/app-store/review/guidelines/).

### Signing, identifiers, privacy

- Bundle ids: `bot.avibe.ios` for the app and `bot.avibe.ios.notification-service`
  for the extension (the desktop app is `bot.avibe.desktop`); an app group
  shared by the app and its extensions; Push Notifications capability.
- `Info.plist`: `NSMicrophoneUsageDescription`. Phase 0 must also check whether
  file inputs and image long-press in `WKWebView` need
  `NSCameraUsageDescription` and `NSPhotoLibraryAddUsageDescription`.
- Privacy manifest (`PrivacyInfo.xcprivacy`) and App Store privacy labels:
  account email, audio sent for transcription through avibe.bot.
- Export compliance: standard HTTPS and CryptoKit only.

### What the owner provides

- Apple Developer Program membership (organization enrollment shows the company
  as seller) and the team.
- An App Store Connect API key for CI uploads.
- An APNs authentication key (`.p8`, key id, team id), stored as an avibe.bot
  secret.
- A build machine: a Mac with Xcode or a macOS CI runner.
- Later: the review demo instance and reviewer account, a privacy policy URL.

## 10. Phases and effort

Effort assumes one Agent lane per track and owner review between phases.

### Phase 0: voice shell (TestFlight internal), about 2 weeks

| Track | Work | Effort |
| --- | --- | --- |
| Native | SwiftUI app, one `WKWebView`, persistent data store, instance entry by URL or QR, media-capture grant for configured origins, three-class navigation policy (instance, authentication, external in `SFSafariViewController`), safe areas and status bar, offline and error page, user script flags, UA suffix, haptics bridge, Google sign-in interception | 1.5 weeks |
| Build | Xcode project, signing, `xcodebuild` or fastlane upload to TestFlight | 0.5 week, parallel |
| Web UI | `isIosShell()`, hide Web Push toggle and install hint, haptics calls, "Open on iPhone" QR | 3 days, parallel |

Exit: section 7 checklist passes on an iPhone. Until Phase 1, users who want
notifications keep the Home Screen web app as well.

### Phase 1: daily driver, about 2-3 weeks calendar

| Track | Work | Effort |
| --- | --- | --- |
| Native | Notification Service Extension with RFC 8291 decryption (CryptoKit P-256, HKDF, AES-GCM), push bridge, tap routing, badge; `ASWebAuthenticationSession` handoff | 2 weeks |
| Instance | `/auth/app/start` and `/auth/app/redeem`, payload byte budget, subscription label, tests | 1 week |
| Backend | Relay registration and delivery routes, APNs HTTP/2 client, rate limits; Sign in with Apple; account deletion | 1.5 weeks |
| Reviews | Product and security review of the relay and the handoff | Before merge |

### Phase 2: native reach and App Store, about 3 weeks

| Track | Work | Effort |
| --- | --- | --- |
| Native | App Intents and App Shortcuts ("Talk to Vibey"), Action Button, Control Center control, Share extension, widgets, multi-instance list | 2.5 weeks |
| Web UI | Tap-to-toggle voice mode, deep-link targets | 3 days |
| Backend | App-facing instance list; device credential for the Share extension and widgets (they cannot use the WebView's cookies) | 1 week |
| Release | Review demo instance, metadata, screenshots, privacy labels, submission | 0.5 week |

## 11. Risks and open questions

- iOS behavior is cited and reasoned from shared WebKit code, not yet observed
  on an iPhone (Phase 0 gate).
- Protected Vault passkeys inside the shell (section 5).
- Agent-authored Show Pages share the silent microphone grant (section 5); a
  separate Show Page origin would be the fix if that becomes unacceptable.
- Payload budget for CJK titles under the 4 KB APNs limit (section 6).
- The keyboard accessory bar that `WKWebView` shows above the keyboard; removing
  it relies on private behavior, so v1 keeps it.
- App Store rejection under 4.2 even with Phase 2 features; TestFlight remains
  a viable channel for existing users.

## 12. Owner decisions

1. Approve the shape and technology: Swift/SwiftUI shell around the existing
   Web UI, code in `ios/` of this repository.
2. Apple Developer account and team, app name, bundle id prefix (`bot.avibe.ios`
   proposed), minimum iOS version (17 proposed; the media-capture delegate needs
   15).
3. Build machine or macOS CI runner.
4. Ship Phase 0 without push to TestFlight internal now, or wait for Phase 1.
5. Approve the APNs relay on avibe.bot as a new backend path (product and
   security review) and provide the APNs `.p8` key.
6. Approve the instance login handoff endpoints (security review).
7. Add Sign in with Apple and account deletion to avibe.bot (needed for the App
   Store, not for TestFlight).
8. TestFlight only, or App Store after Phase 2; if App Store, a review demo
   instance and reviewer account.

## Appendix: spike method

Throwaway code under the workspace `_tmp/ios-shell-spike/`, not committed. A
Swift program created a `WKWebView` with a UI delegate that logged and answered
every media-capture request, enabled the private preferences
`mockCaptureDevicesEnabled`, `mockCaptureDevicesPromptEnabled`, and
`inactiveMediaCaptureStreamRepromptIntervalInMinutes = 1`, and disabled window
occlusion detection so the off-screen page stayed visible. Mock devices bypass
TCC, so the run touched no real microphone or user state. A Python server served
the bundled page and an echo WebSocket. The control run used a second web view
whose delegate answered deny.
