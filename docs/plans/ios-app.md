# Avibe iOS App · Plan

> **Status**: Direction approved by the owner on 2026-11-04 (decision 1).
> Android now runs as a parallel APK-only track (decisions 10-12 and
> [the Android plan](android-app.md)); other iOS decisions are open
> (section 17). Handed off to a dedicated owner of the iOS track (section 18).
> Plan and spike only; nothing here is built yet.
> **Owner**: cyhhao
> **Date**: 2026-11-02

## 1. Recommendation

Build a **thin native iOS shell in Swift/SwiftUI**. The transcript and every
other page stay the user's own Avibe Web UI in a `WKWebView`, loaded from the
instance's tunnel host the way the desktop app wraps it. The **composer becomes
a native view** above the keyboard. Native code owns what a Home Screen web app
cannot do well: text entry, a microphone grant that does not re-ask, APNs push,
haptics, sign-in through the system browser, an instance picker, and later App
Intents, widgets, Share, and HealthKit. All of it sits behind **one versioned
bridge contract**, `window.avibeNative`. Thin Swift and Kotlin shells implement
the same contract in parallel, while the Web UI calls it from one module.
Their native composers and release dates remain independent; see
[the Android plan](android-app.md).

- **Text input**: the owner's main pain in the Home Screen app is typing. An
  iPhone device lab (section 5) narrowed it down: IME candidates and keyboard
  open/close work in every input, quick Paste breaking Pinyin is iOS system
  behavior, and what remains on the web side is the accessory bar, Lexical
  nudging the page with `window.scrollBy`, layout corrected only after the
  keyboard settles, and the Home Screen app reloading after a brief switch
  away. The shell fixes the reload. Phase 0a PR 1 leaves WebKit keyboard
  behavior unchanged; PR 2 adds `isNativeShell()` and coordinates any native
  keyboard layout change with disabling the Web correction in the same change.
  A native `UITextView` composer removes the remaining WebKit editing path
  (Phase 0b), and 0a's measurements decide whether 0b is still worth its cost.
- **Microphone**: the shell answers WebKit's media-capture permission request
  itself (`WKUIDelegate`, iOS 15+), so WebKit never shows its own prompt; iOS
  shows its system microphone dialog once per install. A sealed spike confirmed
  the mechanism, and the production dictation pipeline runs unchanged in a
  `WKWebView` (section 9).
- **Technology**: plain Swift, not Tauri 2 mobile or Capacitor. The native
  composer, keyboard control, and app extensions all need direct ownership of
  the web view and its Xcode project. Nothing in `desktop/` is reusable on iOS
  (section 6).
- **Connection**: the app talks to the instance tunnel directly, and avibe.bot
  stays the control plane (section 7).
- **Push**: an APNs relay on avibe.bot receives the instance's existing
  encrypted Web Push and forwards it without being able to read it. Every
  dispatch rule stays as it is today (section 8).
- **First version**: Phase 0a is the browser-equivalent shell followed by the
  native bridge, Web UI shell marker, and coordinated keyboard decision (about
  2 weeks, TestFlight internal). Phase 0b adds the native composer (about 2.5
  more weeks). The Phase 0 exit checklist on a real iPhone (section 15) gates
  both. Push and system sign-in follow in Phase 1.

Evidence levels used below: **verified** means observed in the 2026-11-02 spike
or read in this workspace's code; **cited** means Apple, WebKit, Google, or
vendor documentation linked inline; **inferred** means reasoned from shared
WebKit code and not yet observed on an iPhone. iOS device behavior stays
inferred until the Phase 0 exit checklist passes.

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

The microphone is the trigger, but text entry is the owner's daily pain in the
same Home Screen app. The microphone prompt is WebKit policy. The input problems
sit at least partly in WebKit's `contenteditable` handling, which every
`WKWebView` app shares (section 5).

## 3. Shape: web transcript, native composer

| Concern | Stays web | Goes native |
| --- | --- | --- |
| Transcript, Inbox, Settings, Apps, Show Pages, every other page | Everything | - |
| Composer | `@` / `#` pickers, Agent and model menu, attachment previews and upload, draft persistence, voice pipeline | Text entry, send and stop, hold-to-talk button with haptics, attachments button, keyboard layout (section 5) |
| Voice capture and dictation | `getUserMedia`, AudioWorklet, WAV segments, realtime socket (unchanged) | Answer the media-capture permission; `NSMicrophoneUsageDescription` |
| Notifications | Inbox, categories, preferences UI | APNs registration, Notification Service Extension, tap routing |
| Sign-in | Instance login pages, avibe.bot login form | Phase 1: `ASWebAuthenticationSession` and handoff |
| Instance choice | - | Instance list, add by URL or QR, switch |
| Reach (Phase 2) | Deep-link targets | App Intents, widgets, Share extension, HealthKit (section 10) |

A native client would duplicate a Web UI that ships several times a week. The
desktop app already proves the wrapper model: `desktop/README.md` describes a
shell that loads the loopback Workbench and adds native capabilities through
`window.__AVIBE_DESKTOP_*` flags (`ui/src/lib/desktopShell.ts`). The composer is
the one surface where the platform's own text system is worth more than shared
code, because WebKit's `contenteditable` IME handling is where the input
problems live.

## 4. Bridge contract: `window.avibeNative` v1

The bridge is one contract. Every shell implements it, and the Web UI calls it
only through `ui/src/lib/native.ts`. This section is the spec; implementation
PRs change it here first.

```ts
interface AvibeNative {
  contract: 1;                         // major; breaking changes bump it
  platform: 'ios' | 'android';
  shellVersion: string;                // e.g. "1.4.0 (212)"
  capabilities: Record<string, number>; // capability -> minor version
  call<T>(method: string, params?: object): Promise<T>; // rejects {code, message}
  on(event: string, handler: (payload: any) => void): () => void;
}
```

| Capability | Calls (web → native) | Events (native → web) |
| --- | --- | --- |
| `app` | `app.info()`, `app.openExternal({url})`, `app.instances()` | `app.foreground`, `app.background` |
| `haptics` | `haptics.impact({style})`, `haptics.notify({type})`, `haptics.selection()` | - |
| `voice` | `voice.permission()` (the grant itself needs no call) | `voice.trigger({action: 'start' \| 'stop'})` from App Intents and hardware |
| `composer` | `composer.attach({surface, sessionId, draft, placeholder, busy, disabled, actions})`, `composer.update({...})`, `composer.detach()`, `composer.insertMention({trigger, value, data})`, `composer.setText({text})`, `composer.focus()`, `composer.voicePreview({text})`, `composer.voiceCommit({text})`, `composer.voiceRestore()` | `composer.change({text, references})`, `composer.submit({text, references, files})`, `composer.stop`, `composer.trigger({trigger, query, active})`, `composer.voice({phase})`, `composer.files({files})`, `composer.action({id})`, `composer.layout({height})` |
| `push` | `push.status()`, `push.subscribe({vapidPublicKey})` → `{endpoint, keys}`, `push.unsubscribe()` | `push.opened({url})` |
| `share` | - | `share.received({items})`, delivered after launch |
| `intents` | `intents.donate({kind, sessionId})` | `intent.invoke({intent, params})`, e.g. `talkToMain`, `openSession`, `newSession` |
| `files` | `files.read({token})` → bytes for a picked or shared file | - |

Rules:

- **Discovery**: a document-start script defines `window.avibeNative` before any
  page script runs, in the main frame of an instance origin only. The Web UI
  checks `capabilities[name] >= minor` before each use. A missing capability
  means the web fallback (for example, the web composer).
- **Compatibility**: shells ship slowly and instances update weekly, so a new
  Web UI must work with the previous contract major, and a shell must answer
  unknown methods with `{code: 'unsupported'}`. Minor versions only add.
- **Transport**: on iOS, `WKScriptMessageHandlerWithReply` answers `call`
  ([Apple](https://developer.apple.com/documentation/webkit/wkscriptmessagehandlerwithreply)),
  and events are dispatched with `evaluateJavaScript`. On Android,
  `WebViewCompat.addWebMessageListener` and `addDocumentStartJavaScript` carry
  the same JSON and take an origin allowlist natively
  ([AndroidX](https://developer.android.com/reference/androidx/webkit/WebViewCompat)).
- **Trust**: the shell answers only when the sender's
  `frameInfo.securityOrigin` is a configured instance origin (section 7). No
  credential crosses the bridge: push private keys stay native, and
  `push.subscribe` returns only the public subscription.
- **Fixed surface**: the method set is compiled into the shell. Web content can
  call it but cannot extend it, which is what keeps the app within App Review
  2.5.2 (section 13).

## 5. Composer and keyboard

### The problem

The owner reports these issues in the Home Screen app:

- the form accessory bar above the keyboard;
- the keyboard pushing the composer around;
- the system Chinese IME sometimes showing no candidates;
- typing stopping after the keyboard's quick Paste;
- third-party keyboards such as Typeless often staying blank.

The composer is a Lexical `contenteditable`
(`ui/src/components/workbench/MentionEditor.tsx`). Keyboard layout is patched
in `ui/src/lib/useIosKeyboardInset.ts`, which corrects the container height
after the visual viewport settles. IME composition, paste, and third-party
keyboard input in a `contenteditable` go through WebKit's editing code and
Lexical's input handling, so a Swift, Capacitor, or Tauri shell would all
inherit them.

The parallel iOS input lane (Session `sesv4sph5q36q`) ran a device lab that
compares a plain `textarea`, a bare `contenteditable`, bare Lexical, and
`MentionEditor`. The owner ran it on an iPhone Home Screen web app on
2026-11-03, and the lane checked the logs:

| Symptom | Device result | Consequence |
| --- | --- | --- |
| Pinyin candidates sometimes missing | Not reproduced; candidates and composition are normal in every input, `MentionEditor` included | Recheck on the checklist; no dedicated fix |
| Keyboard pushing the composer | Keyboard open and close are normal in every input. On the first candidate pick in an empty editor, Lexical calls `window.scrollBy` and moves the page; a plain `textarea` does not | PR 1 keeps WebKit and the current Web correction unchanged. PR 2 may coordinate `keyboardLayoutGuide` with `isNativeShell()` and disabling the Web correction as one behavior. The native composer removes Lexical from the path. The web-only fix avibe-bot/avibe#3166 had no device evidence and was closed. |
| Typing stops after quick Paste (letters go straight in, Pinyin no longer composes) | iOS system behavior: the same in a plain `textarea` | Probably the same in `UITextView`; verify once, no app-side handling |
| Typeless blank | Intermittent, not reproduced. Lead: the Home Screen web app reloads the whole page after a few seconds away | A shell app keeps its page across brief app switches; recheck on the checklist |
| Accessory bar | WebKit; a page cannot remove it | Shell flag or the native composer |

The Web UI keeps its current composer on mobile web; it does not switch to a
`textarea`. Phase 0a runs the exit checklist on the web composer inside the
shell to measure what remains (section 15).

### Shell and Web coordination (Phase 0a PR 2)

- **PR 1 uses browser defaults.** It does not pin the `WKWebView` to
  `keyboardLayoutGuide`, disable scrolling or automatic insets, reset content
  offsets, observe keyboard notifications, or suppress the Web UI's existing
  `useIosKeyboardInset` behavior.
- **PR 2 owns the coordination point.** It adds `isNativeShell()` with the
  `avibeNative` bridge. If simulator and device evidence supports native
  resizing, that same change pins the web view to
  `view.keyboardLayoutGuide.topAnchor` (iOS 15+,
  [Apple](https://developer.apple.com/documentation/uikit/uiview/keyboardlayoutguide))
  and disables `useIosKeyboardInset` inside the shell. Neither half ships
  independently.
- **Accessory bar remains WebKit-owned in Phase 0a.** No public API removes it.
  The common private-content-view override carries App Review risk under the
  license agreement's private-API rule (section 13), so it is not part of the
  shell. The native composer makes it unnecessary for chat.

### Native composer (Phase 0b)

| Native | Web, driven over the bridge |
| --- | --- |
| `UITextView` with UIKit's text input system: marked text for IME composition, paste, third-party keyboards; growing height; placeholder | Transcript and everything above the composer |
| Send and Stop button; Return behavior | `@` / `#` pickers: native emits `composer.trigger` with the query, the web renders its existing list at the bottom of the web view, the selection comes back as `composer.insertMention` |
| Hold-to-talk button with haptics; voice previews shown in the text view | Voice pipeline: native sends `composer.voice` start/stop/cancel, the web runs the verified pipeline and streams `composer.voicePreview` / `voiceCommit` |
| Attachments button: photo picker, document picker, camera | Attachment previews, upload, and retry: native hands files over as tokens and the web reads bytes with `files.read`, so no session cookie leaves the web view |
| Agent/model button that emits `composer.action` | The existing Agent/model menu, opened by the web |
| Layout: pinned to `keyboardLayoutGuide`; reports its height with `composer.layout` | The page reserves that height at the bottom |

**Mention chips round-trip** through the marker grammar the web already uses
in `nodeToMarkerText`: `@<bot:handle>`, `@<agent:name>`, and `#<session_id>`,
plus the references list. Native stores each mention as an attributed range
carrying `{trigger, value, data}`, draws it as a chip (an `NSTextAttachment`
view), deletes it atomically, and serializes to the same
`{text, references}` that `MentionEditor` emits. One JSON fixture of marker
texts and references is tested on both sides.

**Composition rule**: while `markedTextRange` is non-nil, native never changes
`text` or `selectedRange`, never inserts a chip, and defers draft saves and
re-rendering until composition ends. Paste uses the system paste; pasted
images go to the attachment path instead of the text.

**Drafts** stay web-owned. Native emits a debounced `composer.change`, and the
web persists it as today (`onDraftChange`). On attach the web passes the saved
draft; markers in a saved draft restore as plain text, the same rule
`MentionEditor` documents for v1. Native also keeps the latest text in memory,
so a web content process reload does not lose it.

**Turning the web composer off**: when `composer` is available, `Composer`
renders a spacer sized by `composer.layout` instead of its input row, and calls
`attach`, `update`, and `detach` as routes mount and unmount. Pages without a
composer hide the native one. An older shell without the capability keeps the
web composer.

**Extra effort**: about 2 weeks native and 1 week web (native mode in
`Composer`, picker anchoring, file hand-off, the shared fixture), in parallel,
plus about 0.5 week of checklist iteration on devices. That is roughly 2.5
weeks of calendar time added to Phase 0.

## 6. Technology: Swift shell vs Tauri 2 mobile

| Criterion | Swift/SwiftUI + `WKWebView` | Tauri 2 mobile |
| --- | --- | --- |
| Web view delegates (media capture, navigation, keyboard, accessory bar) | Owned directly | wry owns the `WKWebView` and its delegates; wry's UI delegate grants media capture when no handler is set (`wry_web_view_ui_delegate.rs`). A plugin receives the web view in `load(webview:)` ([Tauri](https://v2.tauri.app/develop/plugins/develop-mobile/)), but replacing wry's delegates breaks wry. |
| Native composer overlay | A sibling view in our own hierarchy, pinned to `keyboardLayoutGuide` | Inserted from a plugin into a hierarchy Tauri builds; possible but fragile |
| App extension targets (Notification Service Extension for push decryption, widgets, App Intents, Share) | Ordinary Xcode targets | Extra targets added to the generated Xcode project and kept across regeneration; the official notification plugin is local-only, and remote push needs a community plugin ([example](https://github.com/yanqianglu/tauri-plugin-mobile-push)) |
| Remote-origin IPC | Origin check per message | `remote.urls` capability patterns; Tauri warns it cannot tell an iframe from the window on Android ([Tauri](https://v2.tauri.app/security/capabilities/)) |
| Android from one project | No: a second, thin Kotlin shell on the same bridge contract | Yes for the Rust core, but every feature that matters (composer, push, intents, health) is per-platform Swift or Kotlin anyway |
| Toolchain cost | Xcode only | Rust iOS targets, Xcode, Android NDK, Node; slower builds |
| Reuse of `desktop/` | None needed | None in practice: `desktop/src-tauri` is loopback runtime bootstrap, updater, pet, computer use, macOS window chrome, SSE notifications |

**Recommendation: Swift**, with a Kotlin shell for Android later on the same
contract. Tauri's one real advantage, one project for both platforms, does not
cover the parts that make this app worth installing. Capacitor has the same
shape as Tauri here, and its `server.url` for remote content is documented as
"not intended for use in production"
([Capacitor](https://capacitorjs.com/docs/config)).

**App-Bound Domains**: do not opt in. Instance origins vary per user, and the
limit is 10 domains ([WebKit](https://webkit.org/blog/10882/app-bound-domains/)).
Without the opt-in, message handlers and user scripts work on every origin, and
service workers are unavailable: WKWebView exposes them only with App-Bound
Domains ([MDN BCD](https://github.com/mdn/browser-compat-data/pull/30146)). The
Web UI already guards on `'serviceWorker' in navigator`, and native push
replaces Web Push inside the shell.

**Code location**: `ios/` beside `desktop/` in this repository (and `android/`
later), so that a bridge change and its Web UI consumer land in one PR.

## 7. Connecting to the user's Avibe

### Invariants

- The app loads the instance directly through its tunnel host
  (`*-app.avibe.bot`, or an Enterprise custom hostname). No UI, workspace, or
  Agent runtime traffic passes through avibe.bot, matching the product
  boundary in the workspace `AGENTS.md` and `avibe-bot-backend/AGENTS.md`.
- avibe.bot keeps its control-plane roles (identity, policy, pairing, tunnel).
  New avibe.bot pieces are control-plane functions: an APNs relay for encrypted
  payloads, Sign in with Apple, account deletion, and optionally an instance
  list.
- Primary-main-frame navigation has three classes:
  1. **Instance origins** (configured by the user) load in the WebView and get
     the native bridge and the microphone grant.
  2. **Authentication origins** load in the WebView without bridge or
     microphone access: avibe.bot, the Supabase Auth host avibe.bot uses, and
     GitHub's OAuth pages. Phase 0 sign-in depends on this class (see Signing
     in). Google's authorization endpoint is intercepted instead, because
     Google rejects embedded webviews.
  3. **Other HTTP(S) origins** open in `SFSafariViewController` only when the
     primary main frame navigates to them.

  Subframes use WebKit defaults except that external application schemes are
  cancelled. Every ordinary `window.open` or `target="_blank"` request,
  including instance, authentication, Protected Vault, external HTTP(S), and
  `about:blank`, opens a closable child WebView with WebKit's supplied
  configuration so `window.opener` survives. Child main-frame navigation stays
  in the child regardless of origin, and child windows recurse through the same
  rule. The child toolbar shows its current host plus Done and Open in Safari.
  The latter opens the child's current HTTP(S) URL in the Safari app.

  The bridge checks the sender's `WKScriptMessage.frameInfo.securityOrigin`
  and the media-capture delegate checks the requesting origin against class 1
  on every call, so authentication, Vault, and external pages never receive
  either capability.
- The shell's browser behavior has a closed deviation list:
  1. External primary-main-frame HTTP(S) navigation uses
     `SFSafariViewController`.
  2. Google authorization is intercepted because Google rejects embedded
     browsers.
  3. User-initiated external application schemes use `UIApplication`; subframe
     navigation to them is cancelled.
  4. Instance-origin main frames receive the microphone grant.
  5. Admitted downloads use `WKDownload` and the system share sheet.
  6. PR 2 injects the native bridge only into an instance main frame and checks
     every message origin.

  Everything outside this list uses WebKit defaults. Protected Vault is an
  ordinary cross-origin browsing context, not a shell-specific navigation
  class.
- The shell implements WebKit's JavaScript confirmation delegate and download
  delegates because the Web UI uses `window.confirm` for guarded actions and
  attachment responses for Files and Editor downloads. Downloads from the
  configured instance open the system share sheet, including Save to Files.
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

## 8. Push

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

Registration: when the user enables notifications in the shell, the Web UI calls
`push.subscribe` (section 4). The app requests notification permission,
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

## 9. Voice

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

This machine has no Xcode or iOS Simulator. The iPhone checks are part of the
Phase 0 exit checklist (section 15).

### Background audio

Not in v1. `voiceRecording.ts` already stops when the page becomes hidden, and
iOS WebKit interrupts audio on visibility change
(`InterruptAudioOnPageVisibilityChangeEnabled` is on for iOS in
`UnifiedWebPreferences.yaml`). Recording across app switches would need native
capture with `UIBackgroundModes: audio`, a separate project.

### Hardware and Siri entry points

See App Intents (section 10). They drive the same web pipeline through
`voice.trigger`.

## 10. Deep native features

### App Intents

One App Intents set (iOS 16+) feeds Siri, Shortcuts, Spotlight, the Action
Button (through App Shortcuts), Control Center controls (iOS 18), and widgets
(interactive from iOS 17). Planned intents:

- **Talk to Vibey**: opens the app on the main Session with voice armed
  (`voice.trigger`). A button press is a tap, so the Web UI needs a
  tap-to-start/tap-to-stop mode next to hold-to-talk.
- **Open Session** and **New Session in Project**: deep links, with Session and
  Project entities for Spotlight.
- **Ask Vibey "…"**: sends text without opening the app, and the reply arrives
  as a push.
- **Widgets**: Inbox unread count and the last reply.

App extensions cannot use the web view's cookies. Background intents, widgets,
and the Share extension therefore need an instance-issued **device
credential**: Keychain-stored, revocable, and listed in Settings. That needs
its own security review in Phase 2. Until then, widgets show a snapshot the app
writes to its app group when it runs, refreshed by the Notification Service
Extension.

### HealthKit and sensors (flow sketch; design in a later plan)

iOS does not let an Agent pull health data on demand. Data syncs when the app
runs, or when the system wakes it for new samples:

```text
New samples saved on the phone
  -> HealthKit wakes the app (HKObserverQuery + background delivery,
     at most once per period; hourly at best for types such as stepCount)
  -> HKAnchoredObjectQuery reads the delta since the last anchor
  -> upload to the user's own instance with the device credential
     (never through avibe.bot)
  -> call the observer completion handler

App in foreground -> full catch-up sync

Agent needs data -> reads what the instance holds, with its "as of" time
  -> if stale, push "Open Avibe to sync"; the phone cannot be polled
```

Apple's constraints are cited from
[`enableBackgroundDelivery`](https://developer.apple.com/documentation/healthkit/hkhealthstore/enablebackgrounddelivery(for:frequency:withcompletion:)):

- the `com.apple.developer.healthkit.background-delivery` entitlement is
  required on iOS 15+;
- some types are limited to hourly delivery (`stepCount` on iOS);
- if the app does not call the completion handler, HealthKit backs off, and
  after three failures it stops delivering;
- background queries do not run in the Simulator.

App Review 5.1.3 forbids using health data for advertising or data mining,
requires disclosing the data types collected, and forbids storing personal
health information in iCloud. Motion, location, and other sensors follow the
same pattern, each with its own background rules. HealthKit gets its own plan
(`docs/plans/health-data.md`) covering data types, the instance-side store,
retention, and Agent access, before any code.

## 11. Android (parallel APK track)

Android is a parallel China-first track, not an iOS follow-up. It uses a thin
Kotlin and System WebView shell, implements the same `window.avibeNative` v1
contract, and shares the Web UI, instance APIs, fixtures, and acceptance
scenarios. The shell implementation and real-device results remain
platform-specific.

The first and ongoing distribution channel is a signed APK. No app-store
submission or production vendor-push integration is planned. Existing IM
channels require the separately designed shared-core Workbench-to-IM
notification capability in the Android plan; internal APK alpha instead relies
on foreground realtime and message catch-up. Android starts with the web
composer and adds a native composer only if its own device matrix demonstrates
a need. Native HarmonyOS, starting with HarmonyOS NEXT, is a separate future
platform.

See [the Android app plan](android-app.md) for device scope, WebView feature
gates, APK signing and updates, process-death recovery, notifications, phases,
and open decisions.

## 12. Web UI changes

- `ui/src/lib/native.ts`: the only caller of `window.avibeNative`. It exposes
  capability checks and typed wrappers, plus `isNativeShell()` next to
  `isDesktopShell()`. The user agent also gains an `AvibeiOS/<version>` suffix
  for server-side logs.
- **Composer**: a native mode in `Composer` (section 5), an anchor for the
  mention pickers at the bottom of the web view, a reserved bottom inset, and a
  shared marker fixture with Swift.
- **Keyboard**: PR 2 adds `isNativeShell()` and decides native resizing and
  `useIosKeyboardInset` together. PR 1 keeps both WebKit and the Web hook at
  their existing defaults.
- **Notifications**: inside the shell, `getWebPushSupportState` reports a
  native provider, and `WebPushControl` and the Settings page call `push.*`
  instead of `PushManager`. Hide the iOS "Add to Home Screen" hint
  (`InstallHint.tsx`).
- **Haptics**: call the bridge on hold-to-talk start, stop, and cancel, and on
  send. `navigator.vibrate` does not exist on iOS.
- **Voice**: a tap-to-toggle mode for App Intents and hardware triggers
  (Phase 2).
- **Sign-in**: a shell-aware message when the user picks Google in Phase 0.
- **Settings**: an "Open on iPhone" QR code that carries the instance origin.

## 13. Distribution and App Review

### TestFlight first

Internal testing (App Store Connect users) needs no review. External testing
goes through Beta App Review. TestFlight builds expire after 90 days, so a
build pipeline is needed from the start.

### Remote web content: the rules

- [App Review Guidelines](https://developer.apple.com/app-store/review/guidelines/)
  **2.5.2**: apps may not "download, install, or execute code which introduces
  or changes features or functionality of the app".
- **4.2**: an app should "elevate it beyond a repackaged website".
- **4.7 and 4.7.2**: HTML5 mini apps offered inside an app may not have native
  APIs exposed to them without Apple's permission.
- [Developer Program License Agreement](https://developer.apple.com/support/terms/apple-developer-program-license-agreement/)
  **3.3.1(B)**: interpreted code may be downloaded if it does not change the
  app's primary purpose, does not bypass signing, the sandbox, or other OS
  security, and does not create a storefront for other apps.
- **3.3.1(A)**: apps must not call private APIs. This rule covers the
  accessory-bar technique in section 5.

The common belief that Apple forbids apps that load remote code comes from
March 2017. Apple then warned and rejected apps that used hot-patching SDKs
such as Rollout and JSPatch. Its notice targeted code that "passes arbitrary
parameters to dynamic methods" such as `dlopen()` and `performSelector:` to
change app behavior or call private API based on a downloaded script
([Michael Tsai](https://mjtsai.com/blog/2017/03/08/apple-rejecting-apps-that-use-rollout/),
[react-native#13011](https://github.com/facebook/react-native/issues/13011)).
Loading a user's own server page in WebKit is not that. The rule is about what
downloaded code can reach, and this shell gives it only the fixed bridge.

### Precedents

Home Assistant's iOS Companion app is distributed through the App Store. It
loads each user's own server frontend in a `WKWebView` with no service worker
and no App-Bound Domains
([home-assistant/iOS#5190](https://github.com/home-assistant/iOS/issues/5190)),
and it adds native sensors, widgets, and notifications. That is the closest
match to Avibe. Hotwire Native is a framework built for this shape: it "wraps a
web view within a native shell and renders HTML from your server"
([Hotwire Native](https://native.hotwired.dev/)). Toot! relays encrypted Web
Push to APNs (section 8).

### What keeps us within the rules

- The purpose is fixed at review: a companion for the user's own Avibe. Web
  updates change the UI, never the purpose (DPLA 3.3.1(B)).
- The bridge's method set is compiled into the binary, so web content cannot
  add native capability (2.5.2). It answers only trusted instance origins.
- There is native value beyond the site: the composer, a microphone that does
  not re-ask, encrypted push, App Intents, widgets, Share, and haptics (4.2).
  Submit to the App Store only after Phase 2.
- Show Pages are the user's own Agent output on the instance origin, not a
  catalog of mini apps. They can still reach the bridge through the
  same-origin parent (section 7), which is a residual 4.7.2 exposure; a separate
  Show Page origin would close it.
- Reviewers get a dedicated demo instance and reviewer account (2.1), never a
  customer instance.

### Other review items

| Guideline | Risk | Answer |
| --- | --- | --- |
| 4.2.7(e): "Thin clients for cloud-based apps are not appropriate" | Medium for managed Enterprise instances | Position the app as the companion to the user's own Avibe; personal instances run on the user's machine. |
| 4.2.3(i): must work without installing another app | Medium | The app needs a running Avibe; provide the demo instance. |
| 4.8 Login Services | High: avibe.bot offers Google and GitHub | Add Sign in with Apple to avibe.bot before App Store submission. |
| 5.1.1(v): account deletion in the app | High: email sign-in can create an account | Add account deletion to avibe.bot and an in-app entry that leads to it. |
| 5.1.3 Health | Applies once HealthKit ships | Section 10. |
| 2.5.14: recording indicator | Low | The system indicator plus the composer's recording state. |

### Signing, identifiers, privacy

- **Identifiers** (confirmed by the owner 2026-11-04; the Account Holder
  registers them). Existing
  identifiers on the same team: `bot.avibe.desktop` (plus
  `bot.avibe.desktop.runtime.*` and `bot.avibe.desktop.cua-driver`) and
  `bot.avibe.avault`. The mobile apps follow the same `bot.avibe.<surface>`
  pattern:

  | Item | Identifier | When |
  | --- | --- | --- |
  | iOS app | `bot.avibe.ios` | Phase 0a |
  | Notification Service Extension | `bot.avibe.ios.notification-service` | Phase 1 |
  | Share extension | `bot.avibe.ios.share` | Phase 2 |
  | Widgets extension | `bot.avibe.ios.widgets` | Phase 2 |
  | App group (app and extensions) | `group.bot.avibe.ios` | Phase 1 |
  | Android package (later) | `bot.avibe.android` | Section 11 |

  A bundle id cannot change after the first build is uploaded; the App Store
  name can. Extensions must be prefixed by the app's id. App Intents live in
  the app target unless they need an extension. The Phase 1 handoff uses an
  `ASWebAuthenticationSession` custom-scheme callback, which the session
  matches itself without registering the scheme. Turn off "iPhone and iPad
  apps on Apple silicon Mac" availability, so the iOS app never competes with
  the desktop app's `avibe://` scheme on a Mac.
- **`Info.plist`**: `NSMicrophoneUsageDescription`. Phase 0 must also check
  whether file inputs and the native camera picker need
  `NSCameraUsageDescription` and `NSPhotoLibraryAddUsageDescription`.
- **Privacy**: a privacy manifest (`PrivacyInfo.xcprivacy`) and App Store
  privacy labels covering the account email and audio sent for transcription
  through avibe.bot.
- **Export compliance**: standard HTTPS and CryptoKit only.

### Apple account and team access

The app ships from the owner's existing Apple Developer Program membership,
the same team that signs the desktop app and avault. That membership is
**Individual**, which limits delegation
([Apple: roles](https://developer.apple.com/help/account/access/roles)):

- Users invited in App Store Connect get App Store Connect access only and "are
  not considered part of your team". With the App Manager or Admin role they
  can create app versions, manage TestFlight groups, submit for review, and
  answer App Review.
- Certificates, Identifiers & Profiles is "only available to Account Holders
  and members of an organization's team". Only the owner can register
  identifiers, enable capabilities, create certificates, profiles, and keys
  (including the APNs key), or add device UDIDs. Xcode's automatic signing
  cannot sign for this team on the iOS owner's own Mac.

How the iOS owner works within that:

1. **One-time Account Holder setup** (cyhhao):
   - register the identifiers above and enable Push Notifications and App
     Groups;
   - create an Apple Distribution certificate and App Store provisioning
     profiles for each target;
   - create an App Store Connect API key for iOS CI, separate from the desktop
     notarization key so either can be revoked alone;
   - invite the iOS owner to App Store Connect as App Manager, limited to the
     iOS app;
   - create the app record.
2. **Signing in CI only.** A macOS GitHub Actions job builds, signs with the
   distribution certificate and profiles stored as repository secrets, and
   uploads to TestFlight with the API key. `desktop-package.yml` already signs
   the desktop app on macOS runners the same way. The iOS owner never needs the
   private key locally.
3. **Device testing.** The Simulator, and a free personal team with a
   throwaway bundle id, cover most work. For push, app groups, and extensions,
   CI uploads a TestFlight build for internal testers. Attaching Xcode's
   debugger to those capabilities on a device would require the owner to share
   a development certificate's private key. That friction is the main thing an
   Organization membership removes.
4. **Renewals.** Certificates and profiles expire yearly, and new capabilities
   or targets need the owner again. Phase 1 and Phase 2 each need one owner
   session.

An Organization membership removes these limits: team members can get
Certificates, Identifiers & Profiles access, cloud-managed signing, and Xcode
Cloud, and the company shows as the seller. It needs a legal entity and a
D-U-N-S number ([Apple: enrollment](https://developer.apple.com/help/account/membership/program-enrollment)).
There is no company entity today, so the owner chose to stay Individual
(decision 9). For reference, should that change, the two paths are:

- **Convert the Individual membership** by request to Apple. Apple's pages do
  not say whether the Team ID survives; third-party guides say it does. The
  desktop app's notarization identity and avault's Keychain approvals are
  bound to this Team ID (`avault/.github/workflows/release.yml`), so get that
  in writing from Apple Developer Support first. Developers also report the
  migration taking weeks with membership benefits paused, which would block
  desktop releases ([Apple forums](https://developer.apple.com/forums/thread/818921)).
- **Enroll the company as a separate Organization** for the mobile apps, and
  leave the desktop app and avault on the current team. This adds a second
  membership fee and a second Team ID, but it does not touch existing
  signatures.

Later, also needed: the APNs `.p8` key (Phase 1, stored as an avibe.bot
secret), and before App Store submission, the review demo instance, a reviewer
account, and a privacy policy URL.

## 14. Phases and effort

Effort assumes one Agent lane per track, with owner review between phases.

### Phase 0a: shell, bridge, and keyboard coordination (TestFlight internal), about 2 weeks

| Track | Work | Effort |
| --- | --- | --- |
| Native | PR 1: SwiftUI app with one browser-equivalent `WKWebView`, persistent data, instance entry by URL or QR, configured-origin media-capture grant, primary-main-frame navigation handoff, recursive child windows, downloads, safe areas, offline/error UI, and Google interception. PR 2: `avibeNative` v1 with `app`, `haptics`, and `voice`, plus any evidence-backed keyboard layout change coordinated with the Web shell marker | 1.5 weeks |
| Build | Xcode project, signing, and `xcodebuild` or fastlane upload to TestFlight | 0.5 week, parallel |
| Web UI | PR 2: `native.ts`, `isNativeShell()`, the matching `useIosKeyboardInset` decision, Web Push toggle and install hint hidden, haptics calls, and "Open on iPhone" QR | 3 days, parallel |

Exit: run the section 15 checklist on the web composer, to measure which input
problems a shell alone fixes. The microphone items must pass. The results
decide whether 0b starts (section 16).

### Phase 0b: native composer, about 2.5 weeks

| Track | Work | Effort |
| --- | --- | --- |
| Native | `UITextView` composer, chips, send and stop, hold-to-talk with haptics, attachments, `composer` and `files` capabilities | 2 weeks |
| Web UI | `Composer` native mode, picker anchoring, file hand-off, shared marker fixture | 1 week, parallel |
| Devices | Checklist iteration on iPhones and keyboards | 0.5 week |

Exit: the full section 15 checklist passes. Until Phase 1, users who want
notifications keep the Home Screen web app as well.

### Phase 1: daily driver, about 2-3 weeks calendar

| Track | Work | Effort |
| --- | --- | --- |
| Native | Notification Service Extension with RFC 8291 decryption (CryptoKit P-256, HKDF, AES-GCM), `push` capability, tap routing, badge; `ASWebAuthenticationSession` handoff | 2 weeks |
| Instance | `/auth/app/start` and `/auth/app/redeem`, payload byte budget, subscription label, tests | 1 week |
| Backend | Relay registration and delivery routes, APNs HTTP/2 client, rate limits; Sign in with Apple; account deletion | 1.5 weeks |
| Reviews | Product and security review of the relay and the handoff | Before merge |

### Phase 2: deep native and App Store, about 3-4 weeks

| Track | Work | Effort |
| --- | --- | --- |
| Native | App Intents and App Shortcuts, Action Button, Control Center control, widgets, Share extension, `intents` and `share` capabilities, multi-instance list | 2.5 weeks |
| Web UI | Tap-to-toggle voice mode, deep-link targets | 3 days |
| Backend and instance | App-facing instance list; device credential for extensions (security review) | 1 week |
| Release | Demo instance, metadata, screenshots, privacy labels, submission | 0.5 week |

### Parallel mobile track

- **Android APK app**: proceeds independently of these iOS phases; see
  [the Android plan](android-app.md). Android device or distribution work never
  blocks an iOS TestFlight build.

### Later

- **HealthKit**: its own plan first (section 10).

## 15. Phase 0 exit checklist (real iPhone, TestFlight build)

Run on at least one current iPhone, with the system Chinese (Pinyin) keyboard,
Typeless, and one other third-party keyboard (for example, WeChat Keyboard or
Sogou). Phase 0a records results for the web composer, and Phase 0b must pass
every item with the native composer.

| # | Check | Pass |
| --- | --- | --- |
| 1 | System Chinese IME: candidates appear on every keystroke; composition (marked text) commits correctly; no stray Latin letters after commit | Every time over a 5-minute session |
| 2 | Quick Paste from the keyboard, then keep typing | Same behavior as a plain `UITextView` (iOS stops Pinyin composition after quick Paste even in a native field); record, not a gate |
| 3 | Typeless and one other third-party keyboard, including after switching away for a few seconds and back | Keyboard renders, inserts text, and dictation results arrive; the page is not reloaded |
| 4 | Keyboard show and hide, including rotation and switching keyboards | The composer stays pinned to the keyboard; the transcript does not jump; no gap or overlap |
| 5 | Form accessory bar | Not shown above the keyboard |
| 6 | Microphone | The iOS dialog appears once per install; no further prompt after idle periods, background and foreground, or cold launches |
| 7 | Hold to talk on the composer and on the center button | Haptic on press and release; dictation reaches the real avibe.bot realtime socket and lands in the composer |
| 8 | Recording indicator | Shown while capturing (App Review 2.5.14) |
| 9 | `@` / `#` pickers and chips (Phase 0b) | Chips insert, delete atomically, and send the same markers as the web composer |
| 10 | Sign-in | Email code, password, and GitHub complete inside the shell; the session survives a cold launch |
| 11 | Protected Vault passkey | Works, or the Safari fallback is confirmed |

## 16. Risks and open questions

- **Perceived loading (raised by the owner 2026-11-04).** The Web UI is already
  a single-page app with immutable `/assets/` caching, but every page fetches
  its data live: `ApiContext` keeps only a 1.5-second in-memory request cache,
  and the Home Screen web app reloads after a brief switch away. A local-first
  data cache in the Web UI, rendering the session list, Inbox, and recent
  messages from IndexedDB and then merging fresh data and SSE updates, would
  help the PWA, the desktop app, and this shell alike. It belongs in its own
  plan. Partition the cache by instance and user, and clear it on sign-out,
  authorization changes, and hidden Sessions. Bundling the Web UI into the app
  is not proposed: it would drift from weekly instance updates and saves only
  the HTML request.
- **iPhone behavior is unverified.** It is cited, or inferred from shared
  WebKit code, and has not been observed on a device (section 15).
- **The case for the native composer is narrower than first thought.** The
  device lab found IME candidates normal and quick Paste to be iOS behavior
  (section 5). Phase 0b's value rests on keyboard tracking, the accessory bar
  without a private API, removing Lexical's page nudge, and native
  hold-to-talk. Phase 0a's checklist results should confirm it before 0b
  starts.
- **The accessory-bar technique uses a private class** (DPLA 3.3.1(A)). It
  stays behind a flag, and the native composer removes the need for it in
  chat.
- **Protected Vault passkeys inside the shell** (section 7).
- **Agent-authored Show Pages share the bridge and the silent microphone
  grant** (sections 7 and 13). A separate Show Page origin is the structural
  fix.
- **Payload budget**: CJK titles must fit under the 4 KB APNs limit
  (section 8).
- **App Store rejection under 4.2** is possible even with Phase 2 features.
  TestFlight remains a viable channel for existing users.

## 17. Owner decisions

1. **Approved 2026-11-04.** The shape and technology: a Swift shell, the web
   transcript, a native composer, and one `avibeNative` contract, with code in
   `ios/` of this repository (and `android/` later). Whether and when the
   native composer is built is still decision 4.
2. **Settled 2026-11-04:** the owner's existing Individual membership, the
   same team as the desktop app and avault, and the identifier scheme in
   section 13 (`bot.avibe.ios`). Still open: the app name, and the minimum iOS
   version (17 proposed; the media-capture delegate and `keyboardLayoutGuide`
   need 15).
3. Build machine: GitHub Actions macOS runners with signing secrets, as for the
   desktop app, are proposed. On an Individual membership this is also how the
   iOS owner ships without holding the signing key (section 13).
4. Phase 0 scope: put Phase 0a (shell, bridge, and coordinated keyboard
   behavior) on TestFlight
   internal as soon as it is ready and decide 0b (native composer) from its
   checklist results, or commit to 0b now. Push follows in Phase 1 either way.
5. Approve the APNs relay on avibe.bot as a new backend path (product and
   security review), and provide the APNs `.p8` key.
6. Approve the instance login handoff endpoints (security review).
7. Add Sign in with Apple and account deletion to avibe.bot (needed for the App
   Store, not for TestFlight).
8. TestFlight only, or the App Store after Phase 2. For the App Store, a review
   demo instance and reviewer account are needed.
9. **Settled 2026-11-04:** stay on the Individual membership. There is no
   company entity, so neither Organization path applies for now. The owner
   performs the Account Holder steps in section 13 when the iOS owner needs
   them. Revisit only if a legal entity is created.
10. **Settled 2026-11-05:** build the China-first Android app in parallel with
    iOS. The two tracks do not need to release on the same day, and Android
    device work never blocks TestFlight.
11. **Settled 2026-11-05:** share the Web UI, business behavior, instance APIs,
    `window.avibeNative` contract, fixtures, and acceptance scenarios. Keep the
    Swift and Kotlin shells separate; Tauri and Capacitor remain out. Each
    platform decides from its own real-device evidence whether it needs a
    native composer.
12. **Settled 2026-11-05:** Android ships as signed APKs only. App stores and
    production vendor push are out of scope unless the owner reopens them.
    Native HarmonyOS is a separate future platform.

## 18. Ownership and handoff

One engineer owns the iOS track end to end: design details, implementation,
TestFlight, and later App Store submission. They report to cyhhao, who keeps
the decisions in section 17 and the Account Holder steps in section 13.
Tracking issue: avibe-bot/avibe#3222.

- **Read first**: sections 1, 3 to 5, 7, and 13 to 15. Section 4 is the
  contract with the Web UI; change it there first, then implement it.
- **Where code goes**: `ios/` in this repository, next to `desktop/`. Web UI
  changes go in `ui/`, in the same PR as the bridge change that needs them.
  PRs follow the repository rules in `AGENTS.md`.
- **First milestone**: Phase 0a (section 14), ending with the section 15
  checklist on a real iPhone. The microphone items must pass. The input items
  are measured and decide 0b.
- **Prerequisites from cyhhao**: the one-time Account Holder setup in
  section 13, and decisions 2 to 4 before the first TestFlight build. The other
  decisions are needed before Phase 1 or before App Store submission.
- **Known starting points**: the media-capture delegate in the appendix, the
  2026-11-03 device results in section 5, and the voice pipeline already in
  `ui/src/lib/voiceRecording.ts` and `voiceRealtime.ts`.

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

The production delegate needs only an origin check on top of what the spike
did:

```swift
func webView(
    _ webView: WKWebView,
    requestMediaCapturePermissionFor origin: WKSecurityOrigin,
    initiatedByFrame frame: WKFrameInfo,
    type: WKMediaCaptureType,
    decisionHandler: @escaping @MainActor @Sendable (WKPermissionDecision) -> Void
) {
    // Configured instance origins only (section 7); anything else gets the
    // normal WebKit prompt.
    let allowed = type == .microphone && instances.contains(origin)
    decisionHandler(allowed ? .grant : .prompt)
}
```
