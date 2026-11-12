# Avibe iOS App · Plan

> **Status**: Direction approved by the owner on 2026-11-04 (decision 1).
> Android now runs as a parallel APK-only track (decisions 10-12 and
> [the Android plan](android-app.md)); other iOS decisions are open
> (section 17). Handed off to a dedicated owner of the iOS track (section 18).
> Plan and spike only; nothing here is built yet.
> Phase 0b, a native composer, was dropped by the owner on 2026-11-08
> (section 5).
> **Owner**: cyhhao
> **Date**: 2026-11-02

## 1. Recommendation

Build a **thin native iOS shell in Swift/SwiftUI**. The transcript and every
other page stay the user's own Avibe Web UI in a `WKWebView`, loaded from the
instance's tunnel host the way the desktop app wraps it, **the composer
included**. Native code owns what a Home Screen web app cannot do well: keyboard
layout, a microphone grant that does not re-ask, APNs push,
haptics, sign-in through the system browser, an instance picker, and later App
Intents, widgets, Share, and HealthKit. All of it sits behind **one versioned
bridge contract**, `window.avibeNative`. Thin Swift and Kotlin shells implement
the same contract in parallel, while the Web UI calls it from one module.
Each platform decides from its own device evidence whether it needs native text
entry, and their release dates remain independent; see
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
  The composer stays web: a native `UITextView` composer (Phase 0b) was
  dropped on 2026-11-08, because the web composer inside the shell no longer
  needs it (section 5).
- **Microphone**: the shell answers WebKit's media-capture permission request
  itself (`WKUIDelegate`, iOS 15+), so WebKit never shows its own prompt; iOS
  shows its system microphone dialog once per install. A sealed spike confirmed
  the mechanism, and the production dictation pipeline runs unchanged in a
  `WKWebView` (section 9).
- **Technology**: plain Swift, not Tauri 2 mobile or Capacitor. Keyboard
  control, the accessory bar, and app extensions all need direct ownership of
  the web view and its Xcode project. Nothing in `desktop/` is reusable on iOS
  (section 6).
- **Connection**: the app talks to the instance tunnel directly, and avibe.bot
  stays the control plane (section 7).
- **Push**: an APNs relay on avibe.bot receives the instance's existing
  encrypted Web Push and forwards it without being able to read it. Every
  dispatch rule stays as it is today (section 8).
- **First version**: Phase 0a is the browser-equivalent shell followed by the
  native bridge, Web UI shell marker, and coordinated keyboard decision (about
  2 weeks, TestFlight internal), with the web composer. Phase 0b, a native
  composer, was dropped (section 5). The Phase 0 exit checklist on a real
  iPhone (section 15) gates Phase 0a. Push and system sign-in follow in
  Phase 1.

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

## 3. Shape: web Workbench, native shell

| Concern | Stays web | Goes native |
| --- | --- | --- |
| Transcript, Inbox, Settings, Apps, Show Pages, every other page | Everything | - |
| Composer | Everything: text entry, send and stop, `@` / `#` pickers, Agent and model menu, attachments, draft persistence, hold-to-talk, voice pipeline | Keyboard layout and the hidden accessory bar (section 5) |
| Voice capture and dictation | `getUserMedia`, AudioWorklet, WAV segments, realtime socket (unchanged) | Answer the media-capture permission; `NSMicrophoneUsageDescription` |
| Notifications | Inbox, categories, preferences UI | APNs registration, Notification Service Extension, tap routing |
| Sign-in | Existing instance OIDC and consent after selection | Bundled avibe.bot email OTP or password and current-device account lifecycle |
| Instance choice | Runtime authorization remains server-owned | Account-scoped owned/shared/Organization list, offline recovery, switch |
| Reach (Phase 2) | Deep-link targets | App Intents, widgets, Share extension, HealthKit (section 10) |

A native client would duplicate a Web UI that ships several times a week. The
desktop app already proves the wrapper model: `desktop/README.md` describes a
shell that loads the loopback Workbench and adds native capabilities through
`window.__AVIBE_DESKTOP_*` flags (`ui/src/lib/desktopShell.ts`). The composer
was the one candidate for a native surface; it stays web (section 5).

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
| `accountPresentation` | `app.showAccount()`, `app.setAccountActionAvailable({available})` | - |
| `app` | `app.info()`, `app.openExternal({url})` | `app.foreground`, `app.background` |
| `badge` | `badge.set({count})`: the App icon badge shows `count`, the Inbox total the Web UI shows; `0` clears it. Fire-and-forget. | - |
| `haptics` | `haptics.impact({style})`, `haptics.notify({type})`, `haptics.selection()` | - |
| `keyboardResizesViewport` | No calls. Declares that UIKit shrinks this WebView's viewport with `keyboardLayoutGuide`. | - |
| `voice` | `voice.permission()` (the grant itself needs no call) | `voice.trigger({action: 'start' \| 'stop'})` from App Intents and hardware |
| `viewport` | `viewport.setOuterScrollLocked({locked})` | - |
| `composer` | **Not planned** (reserved; not implemented). The native composer (Phase 0b) was dropped on 2026-11-08 (section 5); its methods and events are in this file's git history. | - |
| [`push`](ios-push-relay-contract.md) | `push.status({vapidPublicKey?: string})` → `{state: "unavailable" \| "off" \| "on", revokeState: "active" \| "absent" \| "pending", endpoint?: string, keys?: {p256dh: string, auth: string}}`, `push.subscribe({vapidPublicKey, forceResubscribe?: boolean})` → `{endpoint, keys:{p256dh, auth}}`, `push.unsubscribe()` → `{revoked: boolean, revoke: "confirmed" \| "pending" \| "not_needed"}` | `push.opened({url})` |
| `share` | - | `share.received({items})`, delivered after launch |
| `intents` | `intents.donate({kind, sessionId})` | `intent.invoke({intent, params})`, e.g. `talkToMain`, `openSession`, `newSession` |
| `files` | `files.read({token})` → bytes for a picked or shared file | - |
| [`direct`](remote-direct-ios.md) | `direct.status()` → `{preferred: "tunnel" \| "direct", sequence: number, closure: number}`. Read-only, no parameters. | `direct.transportChanged({preferred, sequence, closure})` |

Phase 0a PR 2 implements this v1 subset:

- `app.info()` returns
  `{contract, platform, shellVersion, capabilities}`;
- `app.openExternal({url})` returns `{opened}` and accepts `http`/`https` URLs
  with a host plus `mailto`/`tel` URLs with a non-empty target;
- `accountPresentation: 1` admits `app.showAccount()`, which returns `null`,
  tears down the current runtime surface, and presents the app-bundled account
  and instance-switching flow. Capability 2 additionally admits
  `app.setAccountActionAvailable({available})` from the selected-instance
  primary main frame so current Web UI can suppress the old-Web native
  fallback bar;
- `haptics.impact({style})` accepts `light`, `medium`, `heavy`, `soft`, or
  `rigid`; `haptics.notify({type})` accepts `success`, `warning`, or `error`;
  and `haptics.selection()` has no parameters;
- `badge: 1` admits `badge.set({count})` from the selected-instance main frame
  (the rule `push.*` uses), which returns `null` at once and sets the App icon
  badge with `UNUserNotificationCenter.setBadgeCount`. `count` is a number:
  a negative one clears the badge, a fraction is rounded down, and anything
  above 2147483647 shows that. Any other value, or another parameter, is
  `invalid_argument`. The shell sets nothing while the user does not allow the
  App's badges. The Web UI calls it with the same total it mirrors to a PWA's
  `setAppBadge`, `0` included, and again after a read when the App returns to
  the foreground; a push in the background has the notification service
  extension set the instance's total meanwhile (section 8). Signing out or
  switching accounts clears the badge;
- `voice.permission()` returns `{state: 'granted' | 'denied' | 'prompt'}`. It
  does not itself display a permission prompt; the user-initiated Web media
  request remains the single prompt path;
- `keyboardResizesViewport: 1` declares that the current WebView is pinned to
  `keyboardLayoutGuide`, so the Web UI must not apply its browser-only visual
  viewport correction;
- `viewport.setOuterScrollLocked({locked})` lets the Workbench request or
  release outer-scroll suppression for its own WebView. It is accepted from
  any configured-instance main frame, including an instance-origin child.
  Each WebView owns independent lock state, and its next main-frame commit
  releases that state.

Rules:

- **Discovery**: a document-start script defines `window.avibeNative` before any
  page script runs, in the main frame of an instance origin only. The Web UI
  checks `capabilities[name] >= minor` before each use. A missing capability
  means the web fallback (for example, the browser keyboard correction when
  `keyboardResizesViewport` is absent).
- **Compatibility**: shells ship slowly and instances update weekly, so a new
  Web UI must work with the previous contract major, and a shell must answer
  unknown methods with `{code: 'unsupported'}`. Minor versions only add.
  Account presentation therefore has its own capability: a new Web UI keeps
  its previous web sign-out action when an older shell omits it or the call
  fails. A new app loading an older Web UI waits until primary load completes,
  then exposes Account and Change Instance through a 56-point top fallback bar
  outside the WebView. A current Web UI registers its own account action and
  receives no native bar.
- **Transport**: `call` sends `{method, params}` and receives either
  `{ok: true, result}` or `{ok: false, error: {code, message}}`; the injected
  JavaScript rejects the latter error object. On iOS,
  `WKScriptMessageHandlerWithReply` answers `call`
  ([Apple](https://developer.apple.com/documentation/webkit/wkscriptmessagehandlerwithreply)),
  and events are dispatched with `evaluateJavaScript`. On Android,
  `WebViewCompat.addWebMessageListener` and `addDocumentStartJavaScript` carry
  the same JSON and take an origin allowlist natively
  ([AndroidX](https://developer.android.com/reference/androidx/webkit/WebViewCompat)).
- **Trust**: the shell answers only when the sender's
  `frameInfo.isMainFrame` is true and `frameInfo.securityOrigin` is the
  configured instance origin (section 7). Main and child WebViews share
  WebKit's supplied configuration, script, and handler, but every document
  independently passes this rule: an instance-origin child main frame gets the
  bridge, while authentication/external child main frames and every subframe
  do not. The handler repeats the check for every message, so calling the
  transport directly cannot bypass discovery. The factory recognizes every
  WebView it created before applying `viewport.setOuterScrollLocked`, so an
  unknown WebView cannot acquire state. No credential crosses the bridge:
  push private keys stay native, and
  `push.subscribe` returns only the public subscription.
- **Errors**: malformed requests use `invalid_request`, invalid method
  parameters use `invalid_argument`, disallowed frames/origins use `forbidden`,
  and methods outside the compiled surface use `unsupported`.
- **Fixed surface**: the method set is compiled into the shell. Web content can
  call it but cannot extend it, which is what keeps the app within App Review
  2.5.2 (section 13).

### `direct`: which path new connections prefer

Specified for remote direct paths ([the App side](remote-direct-ios.md)).

A shell can carry the instance origin's connections through a local proxy that chooses, for each new connection, a direct path or the public origin (Cloudflare in Avibe's deployment). The origin does not change. Connections already open stay on the path they were opened on, and connections on both paths can exist at the same time, so the shell never reports a single path for "the instance". It reports two separate things: which path new connections prefer, and a count of closures, that is, of the times it closed connections because their path was lost.

- **`preferred`** is `"tunnel"` (the public origin, the default) or `"direct"`.
  - It says only what the shell will try first for the calling document's next connections, never where an existing connection runs.
  - It is `"direct"` only for the selected instance's origin. For every other caller, and in a shell with no direct transport, it is always `"tunnel"`.
- **`closure`** is a non-negative integer for the calling document's instance host.
  - It grows by one each time the shell closes connections for that host because their path was lost, because the App went to the background, or because its local port was recovered.
  - A closure caused by the background is published only on return to the foreground, once the shell's local proxy is ready again.
  - It never decreases while the shell process lives, and starts at 0.
  - Each increase asks the Web UI to act once.
- **`sequence`** grows with every change of either value while the shell process lives, and orders `preferred` only.

The shell emits `direct.transportChanged({preferred, sequence, closure})` after any change of `preferred` or increase of `closure`, and once more with the current state when it returns to the foreground and its local proxy is ready. A shell with no direct transport never emits it.

From the return to the foreground until its local proxy is ready again, the shell holds `direct.status()` answers. An answer received after a resume therefore already includes the closure published on return. It goes to the current runtime's instance-origin documents only, the primary main frame and instance-origin child windows, and its script checks the origin of the document it runs in.

The Web UI keeps two values per document, separately:

- **Latest preference.** Apply `preferred` from a status or event only if its `sequence` is higher than the highest seen.
- **Last handled closure.** Read `direct.status()` before opening the SSE stream or a terminal, and take its `closure` as the baseline. After that, whenever a status or event shows a higher `closure`, run the reconnect action once and record the new value.
- **Resume.** Whenever the document becomes visible again (`visibilitychange` to `visible`, or `pageshow` from the back-forward cache), read `direct.status()` once and apply it to the two values above. This keeps working when every event was missed while the App was suspended. It never takes a new baseline; that happens only once per document. It is one read per resume, not polling.

Closure is compared only with closure, never with `sequence`. So it is acted on once even when its event arrives after a newer preference, arrives twice, or is first seen in a status read.

The reconnect action exists because the count is aggregate: neither side can tell which streams ran on the closed path. It reconnects, through their existing reconnect owners, only what does not repeat an action, even if it was healthy:

- **The SSE stream** catches up.
- **Terminals whose server-side session persists** (tmux-backed) reattach to it. Client-side state does not survive: scrollback, viewport, search, and selection reset. Input typed during the reconnection is dropped, not buffered or replayed.

Plain shells, uploads, and requests in flight are not touched; they react only to their own connection's error. The action retries no request. A change of `preferred` alone triggers nothing.

`direct.status()` takes no parameters; any parameter is `invalid_argument`.

The surface is closed on purpose. No call lets web content choose an address, start a probe, or supply a rendezvous answer, and nothing it returns carries an address, endpoint identity, or network name. Show Pages are same-origin with the Workbench and can reach the bridge, so anything a call accepted, an Agent-authored page could supply. Native code fetches the rendezvous itself.

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
| Keyboard pushing the composer | Keyboard open and close are normal in every input. On the first candidate pick in an empty editor, Lexical calls `window.scrollBy` and moves the page; a plain `textarea` does not | PR 1 keeps WebKit and the current Web correction unchanged. PR 2 pins every shell WebView to `keyboardLayoutGuide`; a Workbench document requests outer-scroll suppression for its own WebView through `viewport.setOuterScrollLocked`, while every other document keeps WebKit scrolling. #3311, #3337, and #3368 keep the composer above the keyboard and the layout in place after it closes. The web-only fix avibe-bot/avibe#3166 had no device evidence and was closed. |
| Typing stops after quick Paste (letters go straight in, Pinyin no longer composes) | iOS system behavior: the same in a plain `textarea` | Probably the same in `UITextView`; verify once, no app-side handling |
| Typeless blank | Intermittent, not reproduced. Lead: the Home Screen web app reloads the whole page after a few seconds away | A shell app keeps its page across brief app switches; recheck on the checklist |
| Accessory bar | WebKit; a page cannot remove it | The shell hides it in instance WebViews (#3376) |

The Web UI keeps its current composer on mobile web and in the shell; it does
not switch to a `textarea`. Phase 0a runs the exit checklist on it inside the
shell (section 15).

### Shell and Web coordination (Phase 0a PR 2)

- **PR 1 uses browser defaults.** It does not pin the `WKWebView` to
  `keyboardLayoutGuide`, disable scrolling or automatic insets, reset content
  offsets, observe keyboard notifications, or suppress the Web UI's existing
  `useIosKeyboardInset` behavior.
- **PR 2 owns the coordination point.** It adds `isNativeShell()` with the
  `avibeNative` bridge, creates primary and child WebViews through one factory,
  pins each web view to
  `view.keyboardLayoutGuide.topAnchor` (iOS 15+,
  [Apple](https://developer.apple.com/documentation/uikit/uiview/keyboardlayoutguide)),
  and publishes `keyboardResizesViewport: 1`. The Web UI disables
  `useIosKeyboardInset` only when that explicit capability is present; an old
  shell without the declaration retains the browser correction. Every
  document starts with WebKit's default outer scrolling and automatic insets.
  A Workbench AppShell in either primary or child uses `viewport: 1` to request
  outer-scroll suppression for its own WebView while mounted and releases it
  on unmount or `pagehide`; a `pageshow` after WebKit page-cache restoration
  requests it again, and that WebView's next main-frame navigation commit also
  releases it. While requested, the shell disables outer scrolling and bounce,
  uses `.never` inset adjustment, and resets the outer offset during keyboard
  changes. The Web UI's own scroll containers remain enabled. Neither half
  ships independently, so the keyboard has one layout owner without changing
  ordinary document scrolling.
- **Instance WebViews hide the accessory bar.** No public API removes it.
  The shell gives WebKit's content view a runtime subclass whose
  `inputAccessoryView` is nil (`FormAccessoryBar`, the technique Capacitor's
  Keyboard plugin uses). It carries App Review risk under the license
  agreement's private-API rule (section 13); the owner accepted that risk on
  2026-11-08 after using the TestFlight build, since internal testing is not
  reviewed and the change is a few lines to remove. The bundled sign-in page
  keeps WebKit's default accessory.

### Native composer (Phase 0b): dropped

**Owner decision, 2026-11-08: the native `UITextView` composer is not built.**
What it was meant to fix is fixed in the web composer inside the shell, and a
native composer would mean maintaining two composer implementations:

- the keyboard's form accessory bar is hidden in instance WebViews (#3376);
- the keyboard no longer pushes the composer off screen or leaves the layout
  raised after it closes (#3311, #3337, #3368).

**Reconsider only** if a real-device input-method problem appears that the web
composer inside the shell cannot work around. Record the evidence first
(device, iOS version, input method, and the steps to reproduce it) and reopen
with it. The dropped design (the native and web split, the mention-chip
round-trip, the composition rule, drafts, and the `composer` bridge surface) is
in git history: `git show d319eed18:docs/plans/ios-app.md`, section 5.

## 6. Technology: Swift shell vs Tauri 2 mobile

| Criterion | Swift/SwiftUI + `WKWebView` | Tauri 2 mobile |
| --- | --- | --- |
| Web view delegates (media capture, navigation, keyboard, accessory bar) | Owned directly | wry owns the `WKWebView` and its delegates; wry's UI delegate grants media capture when no handler is set (`wry_web_view_ui_delegate.rs`). A plugin receives the web view in `load(webview:)` ([Tauri](https://v2.tauri.app/develop/plugins/develop-mobile/)), but replacing wry's delegates breaks wry. |
| Native overlay views (the case was the native composer, now dropped; section 5) | A sibling view in our own hierarchy, pinned to `keyboardLayoutGuide` | Inserted from a plugin into a hierarchy Tauri builds; possible but fragile |
| App extension targets (Notification Service Extension for push decryption, widgets, App Intents, Share) | Ordinary Xcode targets | Extra targets added to the generated Xcode project and kept across regeneration; the official notification plugin is local-only, and remote push needs a community plugin ([example](https://github.com/yanqianglu/tauri-plugin-mobile-push)) |
| Remote-origin IPC | Origin check per message | `remote.urls` capability patterns; Tauri warns it cannot tell an iframe from the window on Android ([Tauri](https://v2.tauri.app/security/capabilities/)) |
| Android from one project | No: a second, thin Kotlin shell on the same bridge contract | Yes for the Rust core, but every feature that matters (push, intents, health) is per-platform Swift or Kotlin anyway |
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
- avibe.bot keeps its control-plane roles (identity, policy, pairing, tunnel,
  and account-scoped instance discovery). Backend PR #308 added the narrow
  mobile session GET/DELETE endpoints by reusing existing owned, shared, and
  Organization access rules. It merged as
  `1cea1dffdc3e68c6a2cc8594064bf94367485b5c` and Vercel deployed that merge to
  Production successfully on 2026-11-05 at 18:44:16 UTC. A public no-cookie
  probe returns the expected `401`. The owner separately verified production
  email OTP, a real account session, and online/offline instance discovery on
  the iOS 27.2 simulator on 2026-11-06; automated tests remain hermetic.
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
- The primary runtime observes WebKit's immediate back item. It enables the
  interactive edge gesture only when that item belongs to the selected
  instance origin, and the action policy rejects any `.backForward` traversal
  to authentication, consent, callback, or another origin. Child windows keep
  their existing close/back behavior and do not enable the primary gesture.
- The shell's browser behavior has a closed deviation list:
  1. External primary-main-frame HTTP(S) navigation uses
     `SFSafariViewController`.
  2. Google authorization is intercepted because Google rejects embedded
     browsers.
  3. User-initiated external application schemes use `UIApplication`; subframe
     navigation to them is cancelled.
  4. Instance-origin main frames receive the microphone grant.
  5. Admitted downloads use `WKDownload` and the system share sheet.
  6. The native bridge is injected only into an instance main frame and checks
     every message origin.
  7. The primary runtime enables interactive back only for an immediate
     selected-instance history item and rejects cross-origin back-forward
     traversal.

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
- Account and runtime cookies live in one persistent named
  `WKWebsiteDataStore` for the current phone account. WebKit is the only
  persistent cookie owner; native code never copies a full cookie snapshot.
  Bundled UI, instance pages, child windows, and iframes receive no Supabase
  tokens or generic authenticated-fetch bridge.

### Adding an instance

The bundled onboarding flow lists every instance the signed-in account can
open: owned Personal instances, permitted shared instances, and Organization
instances admitted by the existing email, domain, or group rules.
Organization management authority does not imply runtime access. The mobile
projection contains only id, display name, host/URL, owner/shared relationship,
owner email, and availability.

An account with no instance sees the existing desktop/CLI install and
`vibe remote` pairing path, then refreshes the same account-scoped list. There
is no LAN scan. Pairing remains between an instance and avibe.bot; the phone is
a client of an already-paired instance.

Selecting an available row stores its id and URL scoped by account id, then
loads the runtime. Offline is a recovery state, not an authorization result.
The runtime's existing OIDC and consent checks remain authoritative.

### Signing in

Local React pages implement the approved email and six-digit OTP flow. Only
their top-level `avibe-app://bundle` document receives the narrow
`avibeMobile` bridge. A read-only custom URL scheme serves only files inside
the app-bundled mobile build directory, giving Vite's ES modules and assets one
fixed local origin.
A native-owned hidden WebView loads the public auth configuration from the
known control-plane origin, sends and verifies the Supabase email OTP (or, for
an account that set an avibe.bot password, runs Supabase's password grant), and
posts the transient access/refresh pair to the existing `/auth/callback`.
That callback remains the owner of Supabase `setSession`, invitation activation,
membership email synchronization, and `avibe_session`.

The hidden transport has no page message handler and runs fixed scripts in
`WKContentWorld.defaultClient`. That isolated world is defense in depth, not a
security process: the practical boundary is the fixed origin, compiled method
surface, no arbitrary fetch, and non-overlapping account/runtime lifecycle.
Tokens are never returned to bundled or remote page JavaScript.

Instance entry then uses the existing instance → avibe.bot OIDC → instance
callback and consent flow. No app-specific PKCE or credential exchange endpoint
is added without evidence that this path fails.

### Keeping the session

One named `WKWebsiteDataStore` persists the current account across launches.
Cold start calls the mobile session endpoint before restoring a selected
instance; the stored account id, instance id, URL, current authorization, and
availability must all still agree.

The hidden account transport is destroyed before runtime opens. Returning to
Account explicitly stops the primary and child runtime WebViews, then creates
the transport only when the bundled account view appears. Every login, logout,
refresh, account switch, or store rotation enters one native transition
authority before its first suspension: it increments the generation, revokes
the selectable snapshot, and detaches any runtime. Instance selection and cold
restore are admitted only while that authority is ready. Each accepted runtime
open carries its generation and store identifier; the route and the WebView
registration both reject a delayed open after a transition. The hidden
transport has a native readiness deadline and a shared `AbortController`
deadline for each fixed request. It rejects navigation actions and responses
outside the configured control-plane origin, and fixed requests reject
redirects so email, OTP, tokens, and cookies cannot follow another origin.
Logout can preempt an older account request, aborts it, attempts current-device
DELETE with the required control-plane `Origin` under a shorter bound, clears
phone-owned selection, rotates to a fresh named store, and removes the old
store even when the remote request fails or times out. Global Supabase logout
and remembered-consent deletion are not used.

The bundled React page mirrors that ownership with one token-bearing account
operation. Its controls derive one lock from that token, including navigation,
email/code editing, submit, resend, refresh, selection, account actions, and
external help links. Async handlers may update screen, session, errors, or
cooldowns and release the lock only while they still own the token. Only a
provider rejection at the verify stage is reported as an invalid or expired
code. Callback, mobile-session, malformed-response, abort, timeout, and script
failures use the approved network state. Because a timed-out verification may
already have consumed its OTP, the page keeps the email but clears the code and
leaves resend available.

### Several instances

Each instance remains its own origin, so its cookies coexist in the current
account store. The picker switches the loaded origin. Runtime native bridge and
microphone trust are still limited to the selected instance origin; account
management trust is never promoted to instance, child, Show Page, or iframe
content.

### Protected Vault passkeys

Protected Vault runs WebAuthn with PRF on `https://sandbox.avibe.bot`
(`docs/plans/vault-crypto-sandbox.md`): unlock and the second step of setup in
the cross-origin iframe, passkey creation and every approval in the sandbox's
top-level window. Apple ties passkey use in third-party apps to the relying
party's `webcredentials` associated domain, so the app lists
`webcredentials:sandbox.avibe.bot` and that host serves a matching
`apple-app-site-association`
(`docs/plans/2026-11-10-ios-protected-vault-passkey.md`, owner decision
2026-11-11). The shell adds no Vault code; Protected Vault stays an ordinary
cross-origin browsing context, and the protected tier needs iOS 18 for PRF.

Still unverified on a device: whether the system accepts a PRF assertion from
the cross-origin iframe in a non-browser app. If it does not, unlock and
setup's second step move into the sandbox's top-level window. If passkeys fail
in the shell outright, the fallback is to open Vault approvals in Safari.

## 8. Push

The complete Phase 1 contract is in
[iOS Push Relay Contract](ios-push-relay-contract.md). It supersedes the
earlier sealed-endpoint sketch in this section.

The instance keeps its existing notification gates, RFC 8291 `aes128gcm`
encryption, RFC 8292 VAPID signing, per-owner `badge_count`, and 404/410
subscription disabling. The relay uses fresh stateful registration IDs, binds
each one to an installation and VAPID public-key hash, forwards ciphertext to
APNs without decryption, and returns the status mappings that the instance
already understands. Account-cookie authentication is limited to installation
binding; all management requests also carry a stable native-only installation
credential. Native persists a monotonic bind generation before each sign-in
bind, registration creation carries the current generation, single revokes
name concrete registration IDs, and logout bulk revokes are bounded by the
captured generation. Provider updates use a separate persisted monotonic
generation.

The contract also fixes the 2800-byte UTF-8 plaintext budget, the compact
APNs envelope, the shared Keychain record, the Notification Service
Extension fallback, same-origin tap routing, account lifecycle, RFC 8291
Appendix A fixture, and the required non-ASCII end-to-end test. Product and
security review is required because this is a new ciphertext forwarding
channel through `avibe.bot`, even though the relay cannot read the content.
Phase 1 explicitly accepts that an offline logout has no fixed remote-revoke
deadline: generic and possibly audible fallback alerts may continue until a
later app execution successfully reaches the relay.

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

### iPhone verification

The Phase 0 shell and mobile onboarding paths now have simulator evidence on
Xcode 27.2. CI remains authoritative on its pinned Xcode 26.6 toolchain; physical
device microphone behavior remains part of the release checklist in section 15.

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
- **Composer**: no native mode; the web composer runs in the shell (section 5).
- **Keyboard**: PR 2 creates primary and child WebViews through one factory,
  pins both to `keyboardLayoutGuide`, and publishes
  `keyboardResizesViewport: 1`. `useIosKeyboardInset` is disabled only when
  that capability exists. A Workbench in either window requests the
  `viewport: 1` outer-scroll lock for its own WebView; every other document
  retains browser scrolling.
- **Notifications**: Phase 0a has no native `push` capability. Hide browser
  Web Push controls and readiness prompts inside the shell instead of offering
  an unusable Service Worker path. Phase 1 adds the native provider. Hide the
  iOS "Add to Home Screen" hint (`InstallHint.tsx`).
- **Haptics**: call the bridge on hold-to-talk start, stop, and cancel, and on
  send. `navigator.vibrate` does not exist on iOS.
- **Voice**: a tap-to-toggle mode for App Intents and hardware triggers
  (Phase 2).
- **Sign-in**: a shell-aware message when the user picks Google in Phase 0.
- **Settings**: an "Open on iPhone" QR code that carries the instance origin is
  a later slice after the mobile design frame is approved.

The iOS Home Screen Web App branches resolve individually:

- `appLaunch.ts`'s `iosStandalone` new-context suppression is not reused. The
  shell has real recursive child WebViews and preserves `window.opener`.
- `PwaLoopbackLinkGuard` keeps blocking `localhost`, `127.0.0.0/8`, and `::1`
  links because those addresses still point at the phone, but it does not
  rewrite same-origin `_blank` links into the current document.
- `downloadMedia.ts` does not fetch and invoke Web Share in the shell. Ordinary
  download links reach the shell's admitted `WKDownload` and native share
  sheet.
- `remoteAuth.ts` does not defer automatic login for a user gesture. The
  stranding risk belongs to the Home Screen PWA browser sheet; the shell keeps
  its existing WebView/auth-origin flow. New first-use and account sign-in UX
  remain outside Phase 0a PR 2.
- `ShowPageShareControl` never offers "Add to Home Screen" inside the native
  shell.

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
- There is native value beyond the site: a microphone that does not re-ask,
  encrypted push, App Intents, widgets, Share, and haptics (4.2).
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
  the app target unless they need an extension. Turn off "iPhone and iPad apps
  on Apple silicon Mac" availability, so the iOS app does not compete with the
  desktop app on a Mac.
- **`Info.plist`**: localized `NSMicrophoneUsageDescription`,
  `NSCameraUsageDescription`, and `NSPhotoLibraryAddUsageDescription` cover
  microphone capture, WebKit's file-picker camera path, and activity-sheet
  photo saving.
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
| Native | PR 1: browser-equivalent shell. PR 2: `avibeNative` v1, shared primary/child keyboard ownership, and page-requested outer-scroll state. Mobile onboarding slice: bundled email OTP/account/instance UI, named WebKit account store, current-device logout, cold restore, runtime account entry, and backend #308 discovery contract. | 1.5 weeks |
| Build | Xcode project, signing, and `xcodebuild` or fastlane upload to TestFlight | 0.5 week, parallel |
| Web UI | PR 2: native-shell capability integration and PWA branch decisions. Mobile onboarding slice: approved A / Cloud letter pages, complete email display, accessible single-input OTP, provider cooldown/error states, owned/shared/offline picker, empty/install recovery, and local account/switch/logout surfaces. | 3 days, parallel |

Exit: the section 15 checklist on a real iPhone, with the web composer in the
shell. The microphone items must pass. The input items are recorded; one the
web composer cannot work around is the evidence that would reopen a native
composer (section 5). Until Phase 1, users who want notifications keep the Home
Screen web app as well.

### Phase 0b: native composer (dropped 2026-11-08)

Not built. Section 5 gives the reasons and the condition for reconsidering.

### Phase 1: daily driver, about 2-3 weeks calendar

| Track | Work | Effort |
| --- | --- | --- |
| Native | Notification Service Extension with RFC 8291 decryption (CryptoKit P-256, HKDF, AES-GCM), `push` capability, tap routing, and badge | 2 weeks |
| Instance | Payload byte budget, subscription label, and tests | 1 week |
| Backend | Relay registration and delivery routes, APNs HTTP/2 client, rate limits; Sign in with Apple; account deletion | 1.5 weeks |
| Reviews | Product and security review of the relay and the handoff | Before merge |

### Phase 2: deep native and App Store, about 3-4 weeks

| Track | Work | Effort |
| --- | --- | --- |
| Native | App Intents and App Shortcuts, Action Button, Control Center control, widgets, Share extension, `intents` and `share` capabilities, multi-instance list | 2.5 weeks |
| Web UI | Tap-to-toggle voice mode, deep-link targets | 3 days |
| Backend and instance | Device credential for extensions (security review) | 1 week |
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
Sogou), with the web composer in the shell.

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
| 9 | `@` / `#` pickers and chips | Chips insert, delete atomically, and send the same markers as in Safari |
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
- **Native composer dropped (2026-11-08).** Only a real-device input-method
  problem that the web composer inside the shell cannot work around reopens it,
  with that evidence (section 5).
- **The accessory-bar technique uses a private class** (DPLA 3.3.1(A)). The
  shell uses it in instance WebViews (owner decision, 2026-11-08); before an
  App Store submission, decide whether to keep it or return to WebKit's
  default bar.
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
   `ios/` of this repository (and `android/` later). The native composer was
   later dropped (decision 4).
2. **Settled 2026-11-04:** the owner's existing Individual membership, the
   same team as the desktop app and avault, and the identifier scheme in
   section 13 (`bot.avibe.ios`). Still open: the app name, and the minimum iOS
   version (17 proposed; the media-capture delegate and `keyboardLayoutGuide`
   need 15).
3. Build machine: GitHub Actions macOS runners with signing secrets, as for the
   desktop app, are proposed. On an Individual membership this is also how the
   iOS owner ships without holding the signing key (section 13).
4. **Settled 2026-11-08:** Phase 0b, the native composer, is dropped; Phase 0a
   (shell, bridge, and coordinated keyboard behavior) ships with the web
   composer (section 5). Reconsider only with real-device evidence of an
   input-method problem that the web composer inside the shell cannot work
   around. Push follows in Phase 1.
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
  are recorded on the web composer in the shell (section 5).
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
