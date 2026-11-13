# iOS App: account and instance pages, redesign

Status: **map and diagnosis** (part 1 of 3). Frames follow in `ui/design/frames/mobile-app/`.
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

## 3. Next

Part 2 (frames, zh/en, light/dark): one structure with the current instance named on Home, a sheet that answers "where am I / what else / how back", recency order, error and offline in place, swipe and pull gestures. Part 3: desktop consistency (#3651/#3653/#3661) and the Swift files a build would touch.
