# iOS App: account and instance pages, redesign

Status: **proposed** — map and diagnosis (1), structure and frames (2), desktop consistency and build notes (3). Frames: `ui/design/frames/mobile-app/switcher.design.tsx`, kit `ui/design/kit/mobile-instance-sheet.tsx`.
Date: 2026-11-13. Base: Avibe `d8187b453`. Design only: no product code changes here.

The owner: 「现在这两个页面的逻辑都有点怪，一个是实例列表页，一个是切换实例页，感觉有点复杂，让用户不好理解。重新设计一下吧，然后交互也看一下。」
From his report: order by recent use; Back returns to the current instance's Home; swipe back is missing; "can't reach avibe.bot" followed by Retry leaves navigation confused. Track A (Session `seskr2s3f78dm`) fixes those bugs inside today's structure; this document redesigns the structure.

## 1. Today's map

### Who draws what

| Layer | Owner | Code |
| --- | --- | --- |
| Instance runtime (Home, chats, every Workbench page) | The instance's own Web UI in a `WKWebView` | `ui/src` (shipped by each instance's release) |
| Account and instance pages | One bundled page (`mobile.html`) in a second WebView, above the runtime | `ui/src/mobile/MobileOnboarding.tsx`, `bridge.ts`, built into `ios/Avibe/Resources/MobileApp` |
| Which layer shows, and when | Swift | `AppModel.swift` (`AppRoute`), `RootView.swift`, `MobileOnboardingContainer.swift`, `MobileOnboardingBridge.swift`, `MobileAccount.swift` |
| Runtime failure, spinner, old-runtime toolbar | Swift (system UI) | `RootView.swift` (`BrowserScreen`, `LegacyRuntimeToolbar`, `DirectFaultMessage`) |

`AppRoute` is `loading` · `onboarding(start)` · `browser(runtime)` · `accountOverBrowser(runtime)`. In `accountOverBrowser` the runtime keeps its document underneath and the bundled page covers it. The bundled page is a single document whose screens are React state: it has no history, so WebKit's edge swipe has nothing to go back to.

### Screens of the bundled page

| Screen | Title (zh / en) | Header | What is on it |
| --- | --- | --- | --- |
| `boot` | 想从哪里开始？ / Where shall we begin? | logo · disabled avatar | two skeleton cards. Shown on **every** open, including the avatar from Home |
| `bootError` | 想从哪里开始？ | logo · disabled avatar | 「暂时无法加载实例」+ 「暂时连不上 avibe.bot…」+ Retry. **No Back, no way out** |
| `welcome` … `weakPassword` | sign-in steps | | approved, out of scope |
| `instances` (first run) | 想从哪里开始？ | logo · avatar | subtitle, refresh icon, one tall card per instance, 「添加自己的实例」 |
| `instances` (a selection exists) | 切换实例 / Switch instance | logo · avatar | the same page; the current card has 「当前」. **No Back** |
| `empty`, `cli` | 让电脑与手机相连 / Install and pair | logo · avatar (cli: ←) | install guidance, 「我已完成配对，刷新」 |
| `account` | 账户 / Account | ← · Avibe | email + 「在此设备退出」, 「当前实例」 card, 「切换实例」 button, licenses |
| `licenses` | 开源许可 | ← | out of scope |

### Native states around it

| State | Zh copy | Actions |
| --- | --- | --- |
| Opening an instance (`loading`) | system spinner | none |
| Instance page failed to load | 无法连接此 Avibe · 请检查网络连接后重试 | 重试 · **更换实例** |
| Direct connection fault | 连接中断，请重新打开 App | none |
| Old instance Web UI (no header avatar) | 56pt toolbar: instance name + avatar menu | 账号 · **更换实例** |

### Entry points

| # | Entry | Path today | Lands on |
| --- | --- | --- | --- |
| E1 | First launch / signed out | `bootstrap` → `onboarding(.instances)` → bundled `boot` | `welcome` |
| E2 | Signed in, no instances | same | `empty` |
| E3 | Signed in, nothing selected (or the selection is gone or offline) | same | `instances` titled 想从哪里开始？ |
| E4 | Launch with a valid saved selection | `openRuntime` | that instance's Home, no picker |
| E5 | Avatar on the instance header (`app.showAccount`) | `accountOverBrowser` → `#runtime-account` → `boot` → `account` | Account over the live instance |
| E6 | Avatar when the runtime has no controller yet | `stopRuntime` → `onboarding(.account)` → `#account` | Account, nothing underneath; its Back goes to `instances` |
| E7 | 更换实例 on the native failure screen | `showInstancePicker` (**stops the runtime**) | `instances` 切换实例 |
| E8 | 更换实例 in the old-runtime toolbar menu | same | `instances` |
| E9 | 账号 in the old-runtime toolbar menu | `showAccount` | as E5 |
| E10 | Push tap, instance available | `selectInstance` → `openRuntime` | that instance at the pushed path |
| E11 | Push tap, instance missing/offline/other account | `showInstancePicker` | `instances` |
| E12 | Sign-out (Account) | `session.logout` | `welcome` |
| — | Deep links | none: the App has no URL scheme or universal links (associated domains carry only `webcredentials`) | |

### Transitions from the avatar (the owner's path)

```text
Home ──avatar──▶ [boot: 想从哪里开始？ skeleton] ──▶ Account
                        └─ fails ─▶ [bootError: 想从哪里开始？ + 连不上 avibe.bot + 重试]   (no Back)
Account ──←/swipe?──▶ Home (app.returnToInstance; swipe: none)
Account ──当前实例 card──▶ 切换实例 list        (the card is a second "switch" button)
Account ──切换实例──▶ 切换实例 list
切换实例 list ──(no ←)──  avatar ──▶ Account ──←──▶ Home
切换实例 list ──tap current──▶ reopens the current instance (reload)
切换实例 list ──tap other──▶ spinner ──▶ other instance's Home
切换实例 list ──tap offline──▶ a note appended below the list (often off-screen)
切换实例 list ──添加自己的实例──▶ empty (install guidance) ── avatar ▶ Account …
```

Four taps and four screens to look at the list and come back without switching. The one page called 切换实例 is reached through a page called 账户.

## 2. What confuses, cell by cell

| # | Where | What the user sees | Why it confuses |
| --- | --- | --- | --- |
| C1 | Home header | 「Avibe」 + avatar | The instance is never named where you work. "Where am I" has no answer until you open Account. |
| C2 | Avatar → `boot` | 「想从哪里开始？」 + skeletons, then 账户 | Every open starts on the first-run page, briefly when the session is cached. |
| C3 | Avatar → `bootError` | 「想从哪里开始？」「暂时无法加载实例」「连不上 avibe.bot」, Retry, no Back | The instance underneath is fine; the page says the opposite and offers no way back to it. Retry then lands on Account (the owner's report). |
| C4 | Account | Account and instance on one page titled 账户 | Switching lives inside Account; the owner calls this page 切换实例页. |
| C5 | Account · 当前实例 | A full instance card with chevron + a 切换实例 button | Two controls, one action. The card looks like "open this instance". |
| C6 | List title | 想从哪里开始？ or 切换实例 | One page, two names, two jobs (first choice, switching). |
| C7 | List header | logo + avatar, no ← | No visible way back; the avatar is the only exit and leads to Account, not Home. |
| C8 | List · current card | 「当前」 badge + chevron | Tapping it reloads the instance you are in. |
| C9 | List rows | ~150pt cards: name, host, 在线/离线, 共享, 当前, 「我的实例 / xx 共享」 | Three facts repeated (共享 badge and "xx 共享"); three rows per screen; order is the server's (alphabetical), not recency. |
| C10 | List · offline row | Looks tappable; tap appends a note below the list | The answer appears far from the tap; the row gave no hint first. |
| C11 | List · refresh | Icon button + 「正在刷新…」 text; pull-to-refresh designed, not shipped | Two refresh idioms, one of them missing. |
| C12 | List · 添加自己的实例 | Full-width outline button, always visible | Competes with the list; leads to a page with no Back (avatar only). |
| C13 | Native failure screen | 「无法连接此 Avibe」 · 重试 · 更换实例 | Different words (更换 vs 切换, 账号 vs 账户, "Avibe" vs 实例); 更换实例 stops the instance first, so Back is gone. |
| C14 | Recovery card | Title always 「暂时无法加载实例」 | Also shown for a failed sign-out. |
| C15 | Gestures | No edge swipe anywhere in the bundled page | Every ← is a button only; the instance pages have swipe back (#3301). |

## 3. Structure (proposed)

**One sentence:** tap the avatar and a sheet shows where you are, the other instances you can open, and your account; drag it down to return.

| Question | Answer in the design |
| --- | --- |
| Where am I? | The sheet's first block, 「正在使用 / You're in」, names the instance (variant B also names it on Home). |
| What else can I open? | 「其他实例」, one list, most recently opened on this device first. Shared instances say so in their second line; offline ones say 「离线」 and open an explanation in place. |
| How do I get back? | Drag the sheet down, tap ✕, or tap the current instance. All three return to the instance as it was (the runtime stays alive underneath). |
| Where is my account? | The last row of the sheet: email, then a page pushed inside the sheet (sign out, licenses, delete). ← or a swipe from the left edge returns to the list. |

Rules:

1. **One list, one row.** The sheet and the first-run page use the same row (icon, name, one line: host / 「xx 共享 · host」 / 「离线 · host」, chevron). No badges; 「当前」 only on the current block. Long names and emails wrap, never truncate.
2. **Order:** the current instance first (its own block), then the others by last opened on this device (Track A records it per account). First run has no history: online first, then by name.
3. **Open from what is known.** The sheet draws at once from the native session snapshot; a background refresh updates it (「正在更新…」 by the section label). No skeleton and no first-run title on the way to Account (fixes C2).
4. **Errors stay where they happen, and say what happened.** A failed refresh is a one-line banner above an intact, usable list. A failed switch is reported on that row while you stay where you were. Nothing replaces the page, and the current instance is always one tap away. Real errors are rare (Track A: C3 was the account coordinator being busy, reported as "can't reach avibe.bot"), so the copy states the fact without guessing a cause.
5. **Switching:** tap a row → it shows 「正在打开…」, the others wait, ✕ still works. When the new instance starts loading, the sheet closes over it.
6. **Gestures:** sheet drag-down = back (native). Account page: swipe from the left edge = back. **Pull to refresh only on the full first-run page**: in a sheet, pulling down at the top dismisses; the sheet refreshes when it opens instead.
7. **First run** (nothing selected yet) is a full page, not a sheet: same rows, 「想从哪里开始？」 kept from the approved A flow, the avatar for Account, pull to refresh. Signed out and no-instances keep their approved pages.
8. **Native failure screen** uses the same words: 「暂时连不上 {name}」 · 重试 · 其他实例. 其他实例 opens the same sheet over the failed instance without stopping it first, so ✕ returns to the retry screen (fixes C13).
9. **One vocabulary** across web, native and desktop: 实例 / 切换 / 账户 / 当前 / 离线 / 添加实例. 「更换实例」 and 「账号」 go.

### Variants (the one real choice)

| | A · avatar opens the sheet | B · the name opens the sheet |
| --- | --- | --- |
| Home header | unchanged: 「Avibe」 + avatar | 「{instance name} ⌄」 + avatar |
| Avatar | instances sheet (account is its last row) | Account page directly |
| Answers "where am I" on Home | no (one tap away) | yes (C1) |
| Ships with | the next TestFlight build only (bundled page + Swift) | TestFlight **and** a Runtime release: the instance's own header changes. Gated by a new capability (`instancePresentation: 1`, `app.showInstances`, the current instance's name in `app.info`). Older instances keep 「Avibe」 + avatar, and their avatar opens the A sheet, so both kinds work side by side. |

Recommendation: **A now, B next.** A removes every confusion except C1 with no Runtime dependency; B is additive on top of A and can follow the next Runtime release.

### Frame inventory (`mobile-app/switcher/<state>-<zh|en>`, light and dark, status `proposed`)

| States | Shows |
| --- | --- |
| `sheet-default`, `sheet-refreshing`, `sheet-only-current` | The sheet over the live instance; background refresh; nothing else to open |
| `sheet-offline-open` | Offline row explained in place |
| `sheet-opening`, `sheet-open-failed` | Switching in progress; a failed switch on its row |
| `sheet-stale` | Refresh failed: banner + Retry, list still usable |
| `sheet-dismissing`, `account-swipe` | Gesture diagrams: drag down to return; swipe from the left edge to go back |
| `sheet-long`, `account-long` (375×667) | Long instance names, long shared-by line, long email |
| `account` | Account pushed inside the sheet |
| `first-run`, `first-run-pulling`, `first-run-error` | First use as a full page; pull to refresh; load failure without cache |
| `runtime-failed` | Native failure screen in the same words, 其他实例 → sheet |
| `b-home`, `b-sheet` | Variant B: the named header; its sheet without the account row |

18 states × zh/en = 36 frames, 72 captures, iPhone 17 Pro (402×874, safe area 62/34) unless noted.

## 4. Desktop consistency

Desktop (#3651/#3653/#3661, `docs/plans/2026-11-11-desktop-instance-switch.md`, frames `desktop/instances/*`): footer row `avatar · instance · dot ⌃` → menu (Switch instance · remote-access status · Sign out) → full Switch instance page (This Mac row, 「我的实例」, 「与我共享」, online/offline badges, current check, Add instance).

| Topic | Same | Different, and why |
| --- | --- | --- |
| Model | Current instance always named; switch from the account entry; sign out next to the account; offline shown, not hidden; Add instance at the end | Desktop names the instance in its footer all the time (= variant B). The phone needs a sheet because it has no persistent chrome of its own on Home. |
| Sections | Own and shared both listed | iOS uses **one** recency list with "xx 共享" in the second line: a phone shows four rows, and two sections push the most-used one off screen. Desktop has room for sections and a pinned This Mac row (iOS has no local Runtime). |
| Badges | 「当前」, 「离线」 | iOS drops the 「在线」 badge (online is the normal case); proposal: desktop drops it too. |
| Unreachable instance | Named recovery screen with a way out | Desktop: 「暂时连不上 Studio」 + Back to this Mac. iOS: 「暂时连不上 {name}」 + 重试 + 其他实例 (no local fallback). Same title wording proposed. |
| Words | 切换实例 / 添加实例 / 退出账号 | iOS sheet title is 「实例」 because it also holds the account; its rows and actions use desktop's words. Native 「更换实例」/「账号」 are retired. |

**Recency.** Track A records last opened per account **on the device**; desktop shows server order (pins, newest) and has no last-used field. Proposal: desktop follows the same **per-device** rule, stored with its profile (`connection.json` already holds the last selection), and no server field. "Recently used" means "on this device": a phone that reorders because the Mac switched would surprise, and a shared field would add a backend write on every switch for no visible gain. The server order stays the first-run order on both.

## 5. Building it: feasibility and the files it touches

**Native sheet (to verify on device before the build):**

- `UISheetPresentationController` (iOS 15+) presenting a view controller that holds the bundled page's `WKWebView`, **large detent only**: the list needs the height, and a medium detent would add a second scroll state to the web content. The runtime stays presented underneath, so returning is a dismissal, not a reload.
- **Drag versus scroll:** UIKit hands a downward drag to the sheet only when its tracked scroll view is at the top. The bundled page today scrolls an inner `overflow-y-auto` box, which WebKit turns into a child scroll view the sheet does not track. The sheet page must scroll the **document** (`WKWebView.scrollView`) and register it (`setContentScrollView` / `prefersScrollingExpandsWhenScrolledToEdge`). This is the one interaction to prototype first; if it misbehaves, the fallback is a full-screen cover with ✕ and an edge-swipe dismiss.
- **Keyboard:** none in the sheet. Add instance is guidance with no text field; sign-in is never in the sheet (signed out uses the full pages).
- **Rotation:** the App allows landscape (`Info.plist`). In compact height iOS shows a sheet full-height; drag-down still dismisses. Nothing extra.
- **VoiceOver:** the sheet is modal (`accessibilityViewIsModal`); order: title, Close (labelled "Close and return to {name}"), current, other instances (each row reads name and its line; offline rows expose expanded/collapsed), Add, Account. The two-finger scrub dismisses like ✕.
- **Dynamic Type:** the bundled page sets `-webkit-text-size-adjust: none` and does not follow Dynamic Type today; the native chrome adds no text. Rows have no fixed heights and wrap, so text scaling (if adopted later through `-apple-system-body`) needs no layout change. Verify at the largest accessibility size on device.
- **Account inside the sheet:** an in-page push with `history.pushState`, and `allowsBackForwardNavigationGestures` on the bundled WebView so the left-edge swipe goes back; or a native navigation controller with two WebView pages. The first is smaller.

**Files a build touches** (Max's area; Track A is changing some of them now, so the redesign builds on top of its merge):

| File | Change |
| --- | --- |
| `ios/Avibe/MobileOnboardingContainer.swift` | Present the bundled page in a sheet over the runtime (detent, delegate: dismissed → back to runtime) instead of a full-screen ZStack layer |
| `ios/Avibe/AppModel.swift` | `accountOverBrowser` becomes an instances sheet over the runtime; `showAccount` opens it; `showInstancePicker` from the failure screen opens it **without** `stopRuntime`; a selection opens the new runtime, then dismisses |
| `ios/Avibe/RootView.swift` | Failure screen copy and actions (重试 · 其他实例); drop the full-screen onboarding layer for the runtime case; legacy toolbar menu wording |
| `ios/Avibe/MobileOnboardingBridge.swift` | New start screen (`sheet`), a close/dismiss call, document scrolling and back-forward gestures for the account push |
| `ios/Avibe/MobileAccount.swift` | Serve the snapshot on open without network; recency (both from Track A) |
| `ios/Avibe/Resources/Localizable.xcstrings` | `browser.*` copy in the shared vocabulary |
| `ios/Avibe/NativeBridge.swift`, `WebViewController.swift` | Variant B only: `instancePresentation` capability, `app.showInstances`, the instance name in `app.info` |
| `ui/src/mobile/MobileOnboarding.tsx`, `bridge.ts`, `ui/src/i18n` | Sheet screen, pushed Account, first-run rows, banners; locale copy |
| `ui/src/components/MobileChrome.tsx` | Variant B only (Runtime release): the named header |
| `ios/AvibeTests/*`, `ui/src/mobile/*.test.tsx` | Route and bridge tests for the sheet, dismissal, and failure → sheet |

Not touched: push and badge, entitlements, signing, the direct-path transport.
