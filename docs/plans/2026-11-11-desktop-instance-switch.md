# Desktop instance switching proposal

Status: **Owner-selected delivery baseline; frames proposed for PR review**. Date: 2026-11-11. Revised: 2026-11-13 (frozen enrollment/Setup decisions and round 8 footer geometry). Design owner: `sesg7d5rrsqus`.

Authorization: deliver this specification and repository design frames in a docs + design PR, with the existing public Show preview. This design lane does not implement product code, merge, release, install, or change production configuration. Phase 1 implementation is separately authorized to the implementation lead. Delivery base: Avibe `366b89ce13` (`origin/master` fetched on 2026-11-13). Original technical investigation: Avibe `000bcdf994`; backend `771bfb1` (read through Git). Historical source findings below are not claims that the newer implementation already satisfies the proposed contracts.

## Outcome and fixed product decisions

Example: Max is using this Mac's Avibe. After email verification, the Mac joins his avibe.bot account automatically. He opens **Switch instance**, chooses **Studio**, and sees Studio's conversations. The title and account menu say Studio. Mini and system notifications belong to Studio; this Mac's running tasks continue. If Studio disappears, **Back to this Mac** is available without cloud authentication.

The five owner decisions are fixed:

1. Discover owned and shared instances through the avibe.bot account, matching iOS. Manual URL entry is advanced/developer functionality.
2. Add avibe.bot email OTP login to Desktop; remote access uses the same account's instance OIDC SSO. Local access remains login-free.
3. Switch the client connection only. Keep local Runtime/tasks running. Always identify the current instance in the native menu/title and account menu; offer a one-action local return on remote failure.
4. Mini follows the selected instance. System notifications originate only from that instance.
5. Login automatically registers the currently running local Avibe instance. No separate pairing ceremony for Desktop. Include this in Phase 1. Another computer appears after its own Desktop login; headless servers retain the pairing-code flow.

### Frozen enrollment and Setup decisions

Max approved both recommended enrollment defaults on 2026-11-13 at 01:06:27:

| Decision | Selected behavior |
|---|---|
| Registration and remote access | **Register and turn on remote access together**, disclosed before sending the code. The switch remains in Settings → Remote access; the account menu shows status only. Re-login preserves an existing deliberate Off preference. |
| Local instance belongs to another account | **Preserve existing binding and show a conflict**. Keep link / use locally is primary; reviewing account change requires ownership verification. Never silently transfer. |

Max approved the optional **Setup completion invitation** on 2026-11-12 at 23:47: Start using this Mac / Sign in and add this Mac. Both it and the footer open the same independent account flow. These product decisions are frozen. Protocol feasibility items below remain implementation verification gates.

Placement C was selected on 2026-11-12 at 11:21:19. The 19:15–19:17 revisions remove the Settings update page and email from the footer. The 20:20:53 revision groups the instance name, state point and chevron together, independently of the right-aligned local version/update entry.

## Evidence and reuse boundary

Evidence paths were read at Avibe `000bcdf994` unless the rc9 tag is explicitly named; paths are relative to the repository. The `backend:` prefix denotes avibe-backend at `771bfb1`. The following are observed in source; proposed behavior below is explicitly a design contract, not a statement that it ships today.

| Evidence | What it establishes |
|---|---|
| `desktop/README.md`; `desktop/runtime-host/src/origin.rs::LoopbackOrigin`; `desktop/src-tauri/src/lib.rs::navigation_is_allowed` and `on_navigation` | Desktop currently accepts exact literal loopback HTTP origins. Runtime adoption/startup/recovery belongs to the local lifecycle owner. Runtime outlives the shell. |
| `desktop/src-tauri/tauri.conf.json`; `desktop/src-tauri/capabilities/*.json`; `desktop/src-tauri/src/lib.rs::ensure_shell_ui` | Bundled bootstrap has its own IPC scope; existing Workbench capabilities enumerate loopback origins. Shell CSP and navigation admission are separate boundaries. |
| `ios/Avibe/MobileAccount.swift::MobileInstance`, `StoredMobileSelection`, `selectInstance`, `restorableRuntime`, `logout` | Shared account/instance shape; selection ties account ID, instance ID and URL; membership and availability are checked on restore; transitions invalidate generations. |
| `ios/Avibe/MobileAuthTransport.swift`; `MobileAccount.swift::AccountWebsiteDataStore`; `ui/src/mobile/MobileOnboarding.tsx::Instances` | iOS OTP goes through Supabase, account callback and cookie session discovery. Its account state uses a named WebKit data store, not a Keychain-backed Desktop account implementation. UI handles 2FA, refresh, offline selection and current markers. |
| `backend:app/api/mobile/session/route.ts`; `backend:lib/instances.ts::listMobileInstancesForUser` | Existing account and **owned + shared** discovery. Deduplication and authorization already happen server-side. Only paired active instances appear; `available` requires fresh Runtime status, healthy UI and running tunnel. It is not a live connectivity guarantee. |
| `backend:lib/auth/management-route.ts::authorizeSessionOnlyManagementRouteRequest` | Mobile session route requires account cookie authentication; arbitrary bearer management credentials are rejected. Do not invent a bearer-compatible endpoint. |
| `backend:app/api/instances/route.ts`; `backend:app/api/instances/[instanceId]/pairing-keys/route.ts`; `backend:app/api/v1/pairing/redeem/route.ts` | Instance creation and one-time pairing issuance/redemption exist; creation still requires a slug. Automatic local registration/idempotency is a missing orchestration contract. |
| `vibe/ui_server.py::remote_access_vibe_cloud_pair`; `vibe/remote_access.py::pair`, `_config_for_pairing_record`, `_persist_pairing_impl`, `stop` | Pairing has local preflight, durable claim/resume and guarded persistence; currently persists enabled=true and starts the connector. Stop withdraws connector intent while retaining pairing. Reuse this lifecycle rather than writing config directly. |
| `vibe/ui_server.py::remote_access_login`, `remote_access_auth_callback`; `backend:lib/oidc/authorize-request.ts::parseAuthorizeRequest` | Instance `/auth/login` leads through registered per-instance OIDC authorization, S256 PKCE, state/nonce and exact HTTPS callback to the instance. This is not an existing general Desktop custom-scheme OAuth client. |
| `desktop/src-tauri/src/pet.rs::window_change`, `build_window`, `is_pet_page`; `ui/src/pet/petShell.ts`, `useActivityCardLinger.ts`, `usePetSession.ts` | Mini follows active loopback today; rebuilding the window and fencing epochs are established patterns. Main Bot resolution is instance-local. Web storage isolates origins but not different accounts at the same origin. |
| `desktop/src-tauri/src/notifications.rs::Notifications`; `desktop/runtime-host/src/notifications.rs::HttpNotificationTransport` | Native notifications use one loopback reqwest SSE connection, not the WebView's authenticated remote cookie session. Native detail lookup/notification races require fencing too. |
| `desktop/runtime-host/src/deep_link.rs`; `ui/src/lib/downloadMedia.ts` | Legacy native links carry only a session/show path, no instance identity. Media links currently use the browser/native download path. Runtime bundle downloader is not a general file broker. |
| `docs/plans/2026-09-18-shared-backend-connection.md`; `docs/plans/2026-09-17-remote-setup-parity.md` | Provider configuration/authentication stays on the selected Runtime. Authenticated remote managers follow the same setup wizard and authorization, rather than a special remote-setup bypass. |

The September `feat/desktop-remote-instances` plan was read only. Its manual connections, multiple windows, and local-only notification/deep-link assumptions are **superseded for this proposal**. No old branch code is reused.

## Selected placement: two-row sidebar footer and local update entry

Max selected C on 2026-11-12 at 11:21:19, with the final revisions at 19:15:23, 19:15:46 and 19:17:05. The sidebar footer has exactly two rows:

1. The existing Apps launcher and Settings button.
2. Avatar, current instance name, a color-only state dot, and the local Desktop version/update entry on the right.

The second row replaces the former version/running row. It has no email and no separate third version row. The email appears only in the upward account menu header. Inside the account control, the instance name, connection indicator and upward chevron form one tight group (`avatar · Studio · dot · chevron`); the local version/update trigger remains a separate control at the far right. The account menu contains Switch instance, the local Remote access status (`MacBook Pro · On` / `MacBook Pro · 已开启`) and Sign out. It has no close/turn-off control and no Desktop update action; the remote-access switch stays in the Settings remote-access page.

The row follows the rc9 Apps button container's left edge for the avatar: the Apps container begins at **x=16** in the frame, while the rc9 `ProjectRow` icon begins at **x=29**. The selected contract deliberately prioritizes the Apps container, so the difference is **13px**. The label follows the avatar at the row's normal icon gap. This is an explicit exception to the project-row icon coordinate, recorded for implementation rather than hidden by a second custom offset.

The local footer update entry is the only in-Workbench Desktop update entry; the existing trusted native updater flow remains available from native menus. With no update it weakly shows the local version (`3.2.6rc9`). When an update is available it becomes a download/update icon with a gold dot. Clicking it opens a compact panel above the row with Install update, Check for updates and View notes, or invokes the existing trusted Desktop updater flow. The panel is anchored to the account row's left edge, which is the Apps container's `x=16` edge, rather than the narrow version button's right edge. Its four sides must remain inside the viewport and at or beyond the sidebar's inner padding; the current 320px fixture is `x=16..336`, `y=546..744` in the English frame and `y=590..744` in the Chinese frame at 1200×800. It does not navigate to Settings and does not describe the selected remote Runtime as needing an update. The panel is mutually exclusive with the account menu.

History: A (sidebar top) was rejected and is not part of this delivery baseline.

### rc9 inventory: page headers are owned by their pages

Reference: `gh-v3.2.6rc9`, commit `f42211308e7058418556a838f5a3dc2a5a6ebc02`, read directly through Git. No installed application was modified. Sources below are at that tag. The kit uses real Workbench, ChatHeaderBar and AgentsPage components; no product header is redrawn.

| Main page | Source | Existing top/right controls and implication |
|---|---|---|
| Home | `ui/src/components/Workbench.tsx`, `home/MainBotHome.tsx`, `home/HomeLayout.tsx`, `home/HomeGuide.tsx` | Greeting, guide-card actions/dismissal and composer controls. The comparatively empty upper right is not a universal account slot. |
| Conversation | `ui/src/components/workbench/ChatPage.tsx::ChatHeaderBar` | Back/title/session menu on the left; Show Page launch/back, annotation and Share when applicable on the right. These actions must remain reachable. |
| Inbox | `ui/src/components/workbench/InboxPage.tsx` | Title/count and Refresh; filter/search toolbar below. |
| Agents | `ui/src/components/workbench/AgentsPageView.tsx` | CapabilityTabs, Global prompts and Refresh; search/backend selector, import/create and detail controls below. |
| Skills | `ui/src/components/workbench/SkillsPage.tsx` | Refresh, Skills/MCP and scope selectors, search, Browse registry/Add skill or Add MCP. |
| Harness | `ui/src/components/workbench/HarnessPage.tsx` | CapabilityTabs, Create and Refresh; task/watch filters below. |
| Vaults | `ui/src/components/workbench/VaultsPage.tsx` | Lock, history, settings, Refresh and Add/read-only state, depending on capabilities. |
| Projects | `ui/src/components/workbench/ProjectsPage.tsx` | BotStrip and New project section action; project cards own menus; sidebar has its own search/add controls. |
| Settings | `ui/src/components/settings/SettingsLayout.tsx` | Native shell suppresses the generic breadcrumbs/back/close header; content pages still own their controls. Navigation rail already ends with `AccountMenu openUpward`. |
| Embedded Show | `ChatPage.tsx::ChatHeaderBar`, `ui/src/components/AppShell.tsx` | Host conversation toolbar owns Show/annotation/share. Arbitrary Show content owns its internal toolbar. Chromeless/standalone windows cannot promise an unused DOM corner. |

### Footer contract and alignment

The real `AppShell.tsx` footer supplies the first row: `AppsLauncher`, a 39px row, `gap-2`, and a compact `w-11` Settings icon. The new second row replaces the location previously occupied by `VersionBadge` and running status. It is one compact 32px row, with no email; the local version/update trigger occupies its right edge. The kit renders the real Apps launcher and Workbench tree inside the temporary proposed shell composition documented in the Known-by-design section; the account row and updater use product primitives.

The account row's avatar alignment follows the Apps button container's left edge at x=16, per the final owner instruction. The rc9 `WorkbenchSidebar.tsx::ProjectRow` remains the evidence baseline at `pl-[13px]`, with its icon at x=29 and label at x=49; the selected account avatar therefore differs by 13px. The instance label, state indicator and chevron are a single inline group: the indicator's left edge must be no more than 8px after the label's right edge. The version/update trigger is outside that group and remains independently right-aligned. Full account/source/target identity remains in the accessible title. The upward menu is 320px wide, left-aligned to the row with an 8px gap; viewport collision adjustment, scrolling and focus restoration require implementation checks.

**Footer invariant:** connection state and local Desktop update availability are orthogonal fixture dimensions. `connection` is a typed enum (`signed-in`, `signed-out`, `switching`, `offline`); `updateAvailable` is an independent boolean, never a connection-enum value. The left status indicator reads only connection state. The right update affordance exists if and only if `updateAvailable` is true, and its contents do not change when connection state changes. Switching, offline recovery and signing out cannot suppress it. When false, the ordinary full-width footer shows the muted local version. Popup openness is a separate presentation choice.

| Connection | Update available | Left account/instance group | Right local Desktop entry | Existing combination fixture |
|---|---|---|---|---|
| Signed in | No | Studio · green point · chevron | Muted `3.2.6rc9` | `account`, `mini`, C `home`, `chat`, `agents`, `menu` |
| Signed in | Yes | Studio · green point · chevron | Update icon / gold point | `updates`, C `update` (open panel); C `collapsed` (compact icon) |
| Signed out | No | Sign in / sign up · green local point · chevron | Muted local version | Main `signed-out` |
| Signed out | Yes | Same sign-in / local group | Update icon / gold point | C `signed-out` |
| Switching | No | Committed source → target · busy icon · chevron | Muted local version | Main `switching` |
| Switching | Yes | Same source → target / busy group | Same update affordance as connected | C `switching` |
| Offline | No | Selected instance · disconnected icon · chevron | Muted local version | Supported projection; the offline capture below exercises the simultaneous-update case |
| Offline | Yes | Same selected instance / disconnected group | Same update affordance as connected | Main `offline` |

Green represents selected-instance connectivity (or available local access when signed out), not local Runtime detailed health. The gold update point belongs exclusively to the right local-update control; it never replaces the left connection indicator. Busy/offline presentation does not affect the right control. Offline recovery retains the source identity and explicit local return. The future compact-rail fixture preserves the independent update icon beside the account icon instead of hiding updates. Native account/local-update facts must come from the trusted shell coordinator, not remote-page claims. Escape/outside-click and popup focus handling remain implementation requirements.

### Local version and update entry

The `updates` frame is a **Workbench footer state**, not a Settings page. It shows the same two-row footer with the local version entry in its normal state or the update-action popover in its available state. The popover is a fixture for the trusted Desktop updater; it does not claim that an updater bridge or product action already exists.

No `Settings → This Mac → Version & updates` navigation item or route is proposed. Settings continues to own the existing Remote access page and its on/off switch. It does not gain a new local Desktop version section in this proposal. The account menu also does not gain a Desktop update action.

`VersionBadge.tsx` currently combines native Desktop update state with Runtime version/skew, and `desktop/src-tauri/src/lib.rs::request_and_open_desktop_updates` currently sends the update command to Workbench. Implementation should reuse that updater state and operation owner behind the selected footer entry, preserving release-note, progress, error and permission handling. Do not build a second update engine. The local update fact must come from the trusted shell coordinator, even while Studio is selected.

### Narrow and sidebar-free behavior

- `ui/src/lib/sidebarWidth.ts` clamps real sidebar width to 248–496px. Keep the two-row footer fixed; project tree scrolls. Truncate the instance name before the status/version affordance, while the email remains in the menu header. Test minimum native window 880×600 during implementation.
- rc9 has no collapsed rail. The retained C avatar-only fixture is a future fallback concept, not Phase 1 scope or a preview headline. Avatar keeps full account/instance accessible identity and status indication; popup opens into the viewport.
- Settings keeps its existing navigation and Remote access page. The Workbench footer remains the only place in this proposal that exposes the local Desktop version/update entry; it does not add a third row or a Settings version item.
- Independent login/list/enrollment pages use a compact app-owned footer identity/entry. Mini keeps only its instance badge and opens the main shell picker for switching (`pet.rs`, `petShell.ts`).
- Fullscreen Show and standalone app windows use the native application menu to open Account / Switch instance or the existing trusted Desktop update flow in the main shell. Never inject a floating account/update button into arbitrary Show content. Native window/menu identity retains the selected instance; OS-hidden chrome cannot promise an always-visible pixel label (`AppShell.tsx`, `app_windows.rs`).

The source inventory establishes rail versus content ownership: Home/Chat/Agents frames keep their real toolbars, and the other inventoried pages use the same rail boundary. Settings keeps its existing rail; sidebar-free surfaces use the native-menu fallback above. An open popup is an intentional temporary overlay, dismissed before another panel opens. Static screenshots do not certify native interactions.

### Native geometry retained

`desktop/src-tauri/tauri.conf.json` defines 1200×800 default, 880×600 minimum, Overlay/hidden title, traffic-light x=12/y=24. `macos_title_bar.rs` defines the 28px native inset, while `DesktopWindowChrome.tsx` owns a separate 48px chat toolbar. Chat remains at y=0 in its content lane; the left rail reserves the native inset. The selected design adds no global header row. `DesktopDragRegion.tsx` and `capabilities/window-drag.json` remain explicit drag/IPC boundaries. Native hit testing, traffic-light clearance, Windows caption controls and minimum-size behavior are **待核** implementation checks. Fixtures inject no bridge.

### Independent app pages, not an embedded website

Email/sign-up, verification, enrollment, conflict, instance list and connection progress occupy the **whole app content area**, with no Workbench sidebar. They are bundled Desktop React pages rendered by the trusted shell, with product primitives and assets. There is no avibe.bot website iframe or visible web login page. The avibe.bot label identifies the account service only. The app footer and native window/menu identity continue to identify the committed instance while these pages are open.

The shipped iOS model is the reference for page ownership: `ui/src/mobile/MobileOnboarding.tsx` owns `email`, `code`, `instances` and account screens separately from the Runtime; `ios/Avibe/MobileAccount.swift` owns account/selection operations. `MobileAuthTransport.swift::ensureReady` uses a non-presented WebKit context at `/api/config/auth` for transport. Reuse that protocol insight, not an embedded cloud website or unverified Desktop bridge compatibility. Desktop's concrete cookie/credential transport remains **待核**.

Back returns to the previous Workbench without changing its committed target. The code page additionally offers Change email and Resend; leaving invalidates pending UI operation callbacks and clears code input. Opening the picker alone does not switch; only authenticated connection success commits. A direct Back action during a tentative switch cancels it and retains the prior committed target; Back to this Mac explicitly commits local. Enrollment already submitted may finish safely in the background and is read back on return. Conflict preserves the original binding and does not block the newly signed-in account from opening its other instances. No cloud auth failure may strand local access.

### Selected Setup completion invitation

**Use an independent, reusable account flow with an optional handoff after local Setup completes.** The proposed completion page (`setup-entry`) offers Start using this Mac as primary and Sign in and add this Mac as secondary. Both the completion invitation and bottom sidebar account entry open the same email → code → required second factor → automatic enrollment → result/conflict → instance-list flow. No duplicate account form lives inside the Setup wizard. Existing users enter directly from the bottom sidebar account entry; they do not repeat Setup. Skipping or failing cloud login leaves local setup complete; signed-in enrollment still runs automatically under fixed decision 5.

Reason: local readiness and cloud account membership have different failure domains. Offline users must be able to complete local Setup, and returning users need account/switch pages after Setup is long finished. One reusable account flow gives both cases the same recovery and credential boundary. This fits current `ui/src/components/Wizard.tsx::SetupFlowShell`, `Wizard::complete` and `ui/src/components/onboarding/setupScreenRegistry.ts`, which own Runtime setup readiness and persisted completion. `AppShell.tsx::setupShell` already gives Setup the whole window; that is the layout precedent, not a reason to entangle completion state with cloud login.

The completion invitation is a proposed new handoff screen, not a claim that rc9 ships it. Returning from sign-in to Setup must never re-run the completion mutation. Phase 1 implementation must test both entry paths, setup-skipped login, offline login failure, switching cancellation, and repeated Back without duplicate enrollment.

## State, persistence and restart

Use one shell-owned client coordinator, separate from local Runtime lifecycle. Account, connection and local enrollment are separate axes; avoid one enum combining every possible pair.

- Account: signed-out → verifying email → second-factor-required (if server requires it) → signed-in. Expired account returns to sign-in while local remains usable.
- Connection: local | switching(target, generation) | remote-connected(instance) | remote-unreachable(instance). A catalog loading/error state is not an account logout.
- Enrollment: unknown | unpaired | registering | paired-this-account | conflict | registration-failed | paired-tunnel-unavailable. Tunnel failure after pairing is not failed registration.

| Required visible state | Behavior and next action |
|---|---|
| Signed out | Open local Workbench. Account entry offers Sign in. No remote catalog is trusted. |
| Signed in, no other instance | Local stays current; show “No other instances yet” and Add instance. Merge a registered local instance into the local row by verified instance ID; never show it again under owned remotes. |
| Local | Local marker and runtime status; remote account login is optional. |
| Switching | Display target by name; suspend old data/action sources, retain local escape. Do not announce connected or persist success until authenticated instance readiness. |
| Remote connected | Exact instance in title, account menu, Mini and notification context. |
| Remote unreachable | Retain selected name; paused/retry surface with Back to this Mac. Never silently show local data under the remote title. |

Proposed profile record: version, stable profile ID, account ID (nonsecret), last committed selection `{kind:local}` or `{kind:remote, instance_id, canonical_origin}`, and namespaced view preferences. Store it under the Desktop app's existing per-user application-data root, in a new `profiles/<profile-id>/connection.json`. It is **not** V2 config and never changes local Runtime's origin. This profile is not a new multi-account UI. Preserve an unreadable/unsupported record, display recovery, and open local without overwriting it. Existing native JSON preferences in `desktop/src-tauri/src/{pet,notifications}.rs` provide the pattern; exact migration/writer format is **待核** during implementation.

Restart always starts/adopts the local Runtime through the existing lifecycle, then attempts the last committed remote selection only after account renewal, fresh authorized catalog membership and exact ID/origin match. An expired account, removed share, renamed host or unavailable remote leaves a clearly named recovery screen with local escape; no direct navigation from stale cache. Local return commits local as the next startup target. A failed/cancelled switch retains the previous committed choice. Interrupted tentative enrollment resumes its existing pairing claim, not a second instance creation.

Before switching, resolve actual unsaved edits using existing unsaved-change guards. Record the new generation, abort old requests/streams/download presentations, close old auxiliary app windows, rebuild the selected Workbench, and rebuild Mini against that same generation. Switching does not issue start/stop/upgrade to a remote Runtime. Local health recovery must not steal the foreground while viewing a remote; it updates only the local row/return path. Evidence: current shared `active_origin` in `lib.rs`, plus `app_windows.rs` and `pet.rs`, must be separated from lifecycle-origin ownership.

## Login, automatic registration and SSO

### Account transport and credentials

Proposed owner: native Desktop account service. Reuse the **protocol and response shapes** from iOS: auth config → Supabase email OTP/verify → avibe.bot account callback → account cookie session discovery. Observe server 2FA requirements; OTP success alone does not authorize registration or remote access. The existing `source=ios-app` behavior and trusted mutation origin rules need a deliberate Desktop entry, not a forged iOS marker or relaxed CORS.

On macOS, persistent avibe.bot account credential material belongs in **Keychain**, scoped to app + profile + account; on Windows use the corresponding OS credential vault. Keep access material short-lived in native memory, never in localStorage, profile JSON, logs, design fixtures or remote JavaScript. A native cookie jar may project only the required control-plane cookies into a non-presented authentication transport context (not a visible cloud login page); it never sends avibe.bot cookies to an instance origin. Instance cookies remain host-scoped in a separate Runtime WebView store for that account. Logout destroys that account's runtime web stores. The exact Keychain/cookie refresh and WebKit/WebView2 projection APIs are **待核** with signed-shell hermetic prototypes; this is a Phase 1 gate, not already-proven iOS reuse. Prefer a single native account credential owner; do not create independently refreshing native and WebView sessions.

The bundled page keeps showing Connecting while the native coordinator continues to instance `/auth/login?next=/`, allow only the pinned avibe.bot authorization host for that auth transaction, and return to the selected instance's exact `/auth/callback`. The Runtime remains the OIDC client; it owns verifier, state/nonce, exchange and instance claims. Verify `/api/session` payload `remote:true, authenticated:true`, expected account identity and selected instance before committing; HTTP 200 alone is insufficient. Confirm a stable account/instance identifier exists in that response or add an explicit safe projection (**待核**); email display alone is not an identity guarantee. Do not add `avibe://oauth` to the existing client registration without a separate contract. Current callback validation only admits per-instance HTTPS callbacks. If authorization needs renewed login or second factor, return to the bundled account pages; never fall back to displaying the cloud website inside Desktop. Non-visible OIDC completion and required consent handling are **待核** Phase 1 gates.

### Automatic local enrollment

1. After complete account authentication, the trusted native coordinator reads only the local Runtime's nonsecret pairing identity/status and verifies that this is the Runtime Desktop started or adopted. Enrollment targets that immutable local origin even if a remote Workbench is selected.
2. If already paired to this account, reuse the same instance ID; do not recreate or rotate its pairing. If remote access was deliberately turned off, signing in again preserves that preference.
3. If unpaired, backend supplies an idempotent ensure-local-instance operation for this account and a stable local installation identifier; it uses existing creation + pairing-key issuance. Allocate a unique compatible slug server-side with bounded conflict handling. Hardware serial numbers are unnecessary. Key material stays native/Runtime-side.
4. Pass the one-time claim to the existing **local** `/api/remote-access/vibe-cloud/pair` with local CSRF/authorization. The Runtime performs its existing preflight, redeem, persistence and connector startup. No direct V2 file write and no parallel registration database.
5. Read pairing status and catalog back. “Added” requires confirmed matching instance ID; “Remote access on” additionally requires connector readiness. A successful pairing with failed tunnel start renders the partial-success frame and retries startup only. A network failure before acknowledgement reuses the idempotency key and durable pairing response.
6. A different owner produces conflict before creation/redemption. Display no previous owner's personal details unless already authorized. Keep existing binding and allow this account's other remotes/local use. Review account change opens an ownership-verification/transfer process; its exact backend ownership proof/recovery is **待核**. Never turn a local OS login into permission to silently take another avibe.bot account's instance.

“Turn off remote access to this Mac” belongs to the local Settings remote-access page, reached in local context; the account menu only reports its status. The implementation must clearly distinguish this local setting from a selected remote Runtime’s Settings. It calls the existing local connector lifecycle, reads status back, preserves pairing and task execution, and must survive restart. `remote_access.stop` withdraws the connector request; **待核** whether every startup path respects that persistent off intent. If not, repair that lifecycle owner, not a Desktop-only flag. Turning off is not deleting the account/instance.

### Logout

Immediately fence generations, remove remote notification source, dismiss Mini content, cancel pending auth/switches/download callbacks and return to local. Best-effort revoke only this device's account session through the existing account session logout contract; clear native credentials and all app-owned account/instance cookie stores and namespace caches even offline. Invalidate late refresh responses so they cannot restore credentials after logout. Keep local Runtime data, tasks, registration and remote-access setting; explain this on sign-out. Logging out of the client is not unpairing the machine or stopping server jobs. Browser sessions outside Desktop are not targeted. Evidence: iOS generation invalidation/store rotation and `backend:lib/auth/mobile-session.ts` use device-local sign-out.

## Navigation and local capability boundary

Keep `LoopbackOrigin` unchanged for lifecycle commands. Introduce a separate proposed `ClientTarget` validated by the shell. Remote targets require **HTTPS, default port, canonical bare origin, an exact authorized instance ID/origin pair, and an avibe.bot subdomain**. Reject credentials, URL paths except `/`, query/fragment, malformed/encoded hosts, suffix tricks such as `avibe.bot.evil.example`, HTTP and certificate bypass. A wildcard suffix is necessary but insufficient: authorization must pin the exact selected host. Custom domains, arbitrary LAN hosts and generic HTTPS websites stay outside this proposal, including Advanced URL. Catalog entries outside policy show an explanation and optional browser opening, not a weakened shell policy.

| Surface/capability | Remote mode contract |
|---|---|
| Navigation and CSP | Main and app windows stay on the selected origin. A separate unprivileged auth context admits only avibe.bot and the transaction's exact callback; no blanket wildcard native grants. Keep bundled-shell CSP narrow. Review Runtime CSP, WebSockets and Show iframe policies separately; do not assume Tauri bundle CSP constrains remote documents. |
| Runtime management, updater, pairing | Remote page cannot launch/stop/reconfigure the **local** Runtime or install updates. Native application menu can still expose explicitly labelled local app/update operations through trusted shell UI. Local enrollment/access toggle is never invoked through an arbitrary remote page. |
| Computer Use | No remote page receives this laptop's native Computer Use enable/settings IPC. Explain “Manage this computer from the local instance.” The selected remote Runtime's own authenticated capability follows its existing authorization; switching does not silently grant access to this Mac. |
| Vault | Selected instance's Vault UI/requests remain governed by its authorization and approval. No bridging to local Keychain/Vault keys, approval dialogs, signing or secret access just because a remote origin was selected. A local Vault deep link identifies local explicitly. |
| Files and downloads | Ordinary authorized remote downloads may use native Save as after explicit user action; origin/account/generation captured at start. Never interpret a remote path or `file://` as a local filesystem instruction. Cancel or discard late results on switch/logout; no silent local execution/open/reveal. Exact WKWebView/WebView2 download handling is **待核**. |
| Deep links | Existing `avibe://session/...`, `show/...`, and Vault links lack instance identity: continue to target local explicitly, showing the local switch when needed. New remote native notifications carry a shell-owned `{instance_id, account_id, generation, safe_relative_route}`; validate current membership and ask before changing instance on a stale notification. No session-ID-only cross-instance dispatch. |
| Mini/window controls | Phase 2 gets a minimal selected-origin/path/window capability, checked again in Rust. No general IPC access for all `*.avibe.bot` pages or embedded Show Pages. Main selection/account operations are exposed by trusted shell chrome. |
| Voice/camera/clipboard | Local device capture remains explicit user consent and targets the selected instance only; no transparent forwarding across a switch. Clear pending recordings/uploads. Broader native capability parity is not assumed. |

Risk gates: reject cross-origin redirects, stale auth callbacks, revoked sharing, account change during switch, and remote → loopback IPC attempts in hermetic boundary tests. Phase 1 must establish these before any remote user trial. Sources: capability files, `ensure_shell_ui`, `deep_link.rs`, `downloadMedia.ts`, and the iOS account generation fences above.

## Mini, notifications and in-flight work

Use the coordinator's single active instance and generation; no independently selected Mini instance. Rebuild Mini on a committed switch and resolve `/api/sessions/main` on the new instance. During switching/offline, remove actionable stale cards and show that instance's connection status with local escape. The minimal instance label appears in the bubble header and accessible label, not as another selector. Reuse `PetAvatar`; the proposal bubble is kit-only because existing `PetPage` mixes bridge/data lifecycle and presentation.

Namespace per-instance UI state by profile + account + instance ID + canonical origin: Mini unread snapshot, dismissed activities, linger deadlines, view route and drafts. Let servers remain unread authorities. The native Mini position/size and keyboard preferences remain device preferences. Existing origin-local storage is useful but insufficient when accounts change at the same hostname; clear/rotate stores on logout and avoid migrating unread from old IDs. `petShell.ts` module state must reset on teardown; `usePetSession` epoch guards protect late responses.

Only one native notification source may run. On switch intent: advance generation, stop old SSE and detail tasks, withdraw old scheduled/pending notifications where supported, then connect the authenticated selected source after commit. Each emission, permission/detail result and click rechecks instance/account/generation. An event received before the switch but formatted afterward is discarded. Already delivered OS banners cannot be retroactively guaranteed invisible; cancel best-effort and make their clicks safe, without displaying fresh old-instance content. A disconnected selected instance does not make local notification delivery resume implicitly.

Remote SSE must authenticate with that instance's cookie/session in a narrowly scoped native transport; do not forward cloud account credentials or expose instance cookies to JavaScript. Existing reqwest transport has no remote cookie owner. Phase 2 must validate cookie refresh/revocation, `/api/events` authorization and preference parity, reconnect behavior and selected-source deduplication. Do not reuse the local-only remote-update polling connection against arbitrary remote instances. APNs/mobile delivery policy is outside this Desktop change.

## Three delivery phases and acceptance

The phases are engineering slices. **Phase 1 alone is not a general release**: final owner decision 4 requires Phase 2. Development builds may disable Mini/notifications while remote in Phase 1, visibly marked as incomplete. No released build may quietly continue showing local Mini/notifications while the title says remote.

| Phase | App/Runtime work | avibe-backend/service work | Acceptance and primary risk |
|---|---|---|---|
| **1 — Account, auto-registration, list and switch** | Native account/credential owner; bundled standalone account/picker pages and the selected optional post-Setup invitation; two-row Apps/Settings + account footer and local footer version/status/updater; selection persistence; authorized origin model; local lifecycle split; registration status/conflict/retry; C bottom account/instance entry with sidebar-free menu/footer fallbacks and existing chat toolbar; basic unavailable/local-return path; stop old auxiliary sources on switch. | Reuse mobile list/session shape and Supabase OTP. Add Desktop account entry/credential transport contract and 2FA parity. Add idempotent ensure-local-instance with slug allocation, owner check and existing pairing-key flow. Expose nonsecret pairing identity/owner-match safely; implement transfer authorization only if existing owner workflow cannot serve it. Runtime pairing must support reliable readback/resume and off preference. | Local works signed out; bottom account and Setup entry share one account flow, skip/back/offline never reset local completion; Apps/Settings retain appropriate selected-instance scope; the local footer version/update entry works signed out and while remote without remote IPC grants; no separate sidebar version row or account-menu update action; OTP + required 2FA; first login creates exactly one paired local instance; retry/restart cannot duplicate; same-account login reuses/off stays off; foreign-owner conflict never mutates; pairing success + tunnel failure is distinct. Owned/shared/current/offline list and revocation; A→B→local; restart/failed switch/cancel; URL/CSP/IPC boundary tests. Observe local task progressing through remote switch. **Risk:** credential custody and ownership transfer. |
| **2 — Mini and selected-instance notifications** | Rebuild Mini on committed target; partition caches; minimal native capabilities; authenticated remote event source; per-generation delivery/click fencing; late downloads/auxiliary window teardown. | Prefer existing authorized `/api/events`, inbox/run detail and notification preference endpoints. Add no new push relay by default. **待核:** remote SSE identity/cookie refresh, selected account capabilities and event visibility; repair only missing selected-instance authorization/session projections. | B's main Bot, unread and activity only; A's late reply/detail/click cannot enter B; rapid A→B→A; logout offline; old OS notification click after membership loss; reconnect has no duplicate banners. Local task continues with no local system notifications while B is selected. Verify signed macOS and Windows packages. **Risk:** same IDs across instances and async stale callbacks. |
| **3 — Add instance, advanced URL and recovery refinement** | Server creation/pairing code handoff; another Desktop discovery; advanced URL with identical whitelist/authorization; rename/host change recovery, interrupted enrollment UX, offline recovery polish. | Reuse create/key issuance/redeem; expiration/regeneration status and catalog refresh. No arbitrary-host or anonymous admission endpoint. Ensure new server pairing reports into existing mobile catalog. | Code consumed on the **target server**, never this Mac; expiry/regeneration/retry; automatic appearance after pairing; URL duplicate resolves existing instance; unauthorized URL rejected; offline/renamed/revoked instance cannot restore stale access. **Risk:** accidentally re-pairing current local instance from Add. |

For each phase, add the smallest existing-boundary tests and auth/setup catalog cases that protect these behaviors, then exact-head review/CI and isolated Incus/native package verification when implementation is authorized. Use test-owned config, Keychain/cookie containers and fake cloud/Runtime endpoints first. Never restart this development machine's live agent service for validation. No tests or design screenshots here certify the future native implementation.

## Design handoff

Source: `ui/design/frames/desktop/instances.design.tsx`, fixtures in `instances-fixtures.ts`, proposals in `ui/design/kit/desktop-instances.tsx`. All frames remain **proposed**; C is the owner-selected placement, with the two-row footer, tight instance/status/chevron group and separate local update popover. Frames are 1200×800 (Desktop's configured initial window), each in English/Chinese and light/dark.

Frame IDs are `desktop/instances/<state>-<lang>`, where `<lang>` is `zh` or `en`:

| States | Purpose |
|---|---|
| `signed-out`, `account`, `updates` | Two-row footer; account menu opens upward with two actions and read-only remote-access status, and updates is the local version/update popover state. |
| `setup-entry` | Optional post-Setup invitation into the independent account flow. |
| `instances` | Independent whole-window local/owned/shared list, current marker and persistent title indicator. |
| `empty`, `loading`, `list-error` | Signed-in/no remote, discovery loading and failure; local stays available. |
| `email`, `code`, `code-error` | Desktop email layout, automatic enrollment disclosure, ready code and error recovery. |
| `registering`, `registered`, `registration-error`, `conflict` | Local enrollment progress/success, paired but tunnel unavailable, safe account conflict. |
| `add`, `advanced` | Other Desktop and headless server paths; restricted manual URL. |
| `offline`, `switching`, `mini` | Local recovery, pending connection, minimal Mini origin label. |

Reuse: real `WorkbenchSidebar`, `AppsLauncher`, `Workbench`, `PetAvatar`; draft shell composes them using rc9 AppShell placement (the DesktopScreen helper omits the bottom cluster); product `Button`, `Badge`, `Input`, `Label`, `Skeleton`; existing logo/mail mascot, spacing/radius/elevation and `--gradient-wizard-platforms`. The account panel, local updater popover, Setup completion invitation, standalone account pages and Mini label/bubble are draft additions, not assertions of changed shipped components. No token added; no `ui/src`, Desktop, or backend product file changed. No API/bridge is installed by these fixtures; existing design-shell stubs answer product reads. Demonstration names/emails are synthetic.

Observable acceptance: C footer contains exactly two rows, Apps/Settings then account; the account row has no email; the avatar aligns to the Apps button container at x=16 while the rc9 ProjectRow icon is x=29 (13px difference); the instance label, state indicator and chevron are one group, with `stateIndicator.left - instanceLabel.right <= 8px`; the local version/update entry stays separate on the right; the update popover is left-anchored to the account row, has all four sides inside the viewport, and begins at or beyond the sidebar inner padding; page-header actions remain unobstructed; Apps/Settings remain visible; account menu has Switch instance and Sign out actions plus read-only remote-access status; login/list/enrollment have no sidebar or website iframe; local update and selected Runtime scopes are distinct; local row and current instance are unambiguous; account switch occupies a complete row; registration does not imply successful tunnel; conflict offers a safe default; remote failure offers local return; all controls/copy fit the desktop viewport in four locale/theme combinations. Future implementation at the minimum 880×600 width should stack the two-column login layout, keep the local row visible and scroll long lists; do not add another responsive design direction in this proposal. Long names wrap in rows; current-instance chrome truncates with a full tooltip/accessibility label in implementation. Keyboard navigation, focus trapping, real native menus/notifications, webview/CSP and actual host connectivity await implementation testing.

C workbench source: `ui/design/kit/desktop-instance-placement.tsx`, `ui/design/frames/desktop/placement-fixtures.ts`, `ui/design/frames/desktop/instances/placement-c.design.tsx`. IDs: `desktop/instances/placement-c/<scene>-<zh|en>`, with `home`, `chat`, `agents`, `signed-out`, `update`, `switching`, `collapsed`, `menu`, both themes. The main preview uses all except the future collapsed concept. The `desktop/instances/updates-<zh|en>` IDs render the Workbench footer update popover state. Active delivered set: original 80 + C 32 = **112 captures / 56 frame IDs**.

Conversation frames use real ChatHeaderBar in Show mode, including annotation/share and Back to chat. The inner static Show document remains Chinese/light independently of host chrome. Agents uses real AgentsPage and API fixtures. The Workbench update frame uses the real shell and a kit-only popover anchored to the local footer row; no Settings version navigation is added. No bridge or product logic is injected, no production file or token changed.

Public preview: https://max-app.avibe.bot/p/CPAPBAF4yGc/

## Change contract and verification

The delivery preserves the owner-selected design and its stable frame IDs. Only this specification, fixtures, frames, the two draft kit components and a design-only DOM check enter the PR. No production behavior, capability, API, dependency or token changes. Rejected placement frames, generated screenshots and trash are excluded. All frames retain `proposed`, following `ui/design/AGENTS.md`: approval is recorded in the owner-approved PR, and `shipped` requires shipped components.

Prior round 8 verification on Avibe `000bcdf994`: design typecheck passed; all 112 locale/theme captures exported at 1200×800. DOM checks measured a 6px label-to-point and point-to-chevron gap, and exactly two footer children in 48 Workbench captures. Round 7 verified the update popover's four edges stay inside the viewport with its left edge at the Apps container's x=16. Main accepted that clipping correction. The active geometry supersedes historical top/right and Settings-update explorations.

Delivery verification on Avibe `366b89ce13`: design typecheck and UI production build passed; 112/112 captures exported. A fresh DOM pass covered all 112 captures: every labeled status gap is 6px (≤8px), all 16 open menu variants stay within the viewport and at or beyond x=16, all 48 Workbench footer variants have two rows, and no horizontal document overflow or unfixtured API read was reported. Full-size visual inspection covered the Chinese light update panel, English dark account menu and conversation, and Chinese light Agents page. The retained collapsed concept now passes its compact fixture into the account entry; it is outside Phase 1. The update icon has an explicit accessible name. Further delivery checks are recorded in the PR. Static captures verify layout and fixture rendering only. Native auth/2FA/OIDC, credential stores, registration idempotency, updater execution, local Runtime continuity, selected-instance events, minimum-window geometry, focus/keyboard behavior and native origin/IPC boundaries remain implementation acceptance gates. Phase 1 is authorized for the macOS baseline only; Windows native parity and Phases 2–3 remain separate work.

### Known-by-design: temporary proposed shell composition

The proposed shell in `ui/design/kit/desktop-instances.tsx` composes the real WorkbenchSidebar, AppsLauncher and page components, but its safe strip, column/footer layout and Settings button are draft composition. It is not the shipped AppShell and does not certify its capability behavior or native geometry. The existing `ui/design/shell/DesktopScreen.tsx` and `SidebarColumn.tsx` do not expose a footer slot.

Scope ruling (main, 2026-11-13 01:44:41): this PR stays docs + design only. Phase 1 contract §5 assigns AppShell/Wizard/VersionBadge adapters to frontend U1; that lane will extract the real footer/view slot. After U1 merges, the design owner will open a small follow-up PR to consume that slot and remove the duplicate shell composition. Do not hide or replace shipped DOM with CSS/portals as a substitute. Frames remain proposed until the owner approves the PR, and static geometry is not evidence of shipped shell parity.

### Mirrored product literals — scoped U1 deferral

Main's token audit ruling on 2026-11-13 at 02:19:06 applies to both draft kit files. The audit covers arbitrary dimension, spacing, typography, color and stacking classes; it does not infer full shipped-shell parity from equal metrics.

| Original class | Audit disposition | Product reference / owner |
|---|---|---|
| `h-[39px]` | Retained with an inline source comment; the only remaining arbitrary numeric class. No existing named footer-height token is available, and changing it to 40px would change the reference metric. | `ui/src/components/AppShell.tsx:505` at base `366b89ce13`. U1 extracts the real tools/footer slot and may introduce a shared height token; the subsequent frame-consumer PR deletes this copied row and literal. |
| `size-[18px]` | Replaced by existing spacing utility `size-4.5`, with rendered 18×18px verified. | Mirrored Settings icons in `AppShell.tsx:530` and `:543`; the copied Settings control disappears on U1-slot adoption. |
| `pt-[0.625rem]` | Replaced by equivalent `pt-2.5` (10px at the frame root size). | Existing spacing scale imported through `ui/src/index.css`. |
| `z-[60]` (three uses) | Replaced by the equivalent standard `z-60` utility. | Existing Tailwind stacking utility, no new product token. |
| `bg-[var(--sidebar-background)]` | Uses the equivalent `bg-(--sidebar-background)` shorthand. | Existing product color token; no literal color introduced. |

The remaining bracketed selector is a structural variant for the inactive rejected-A branch, not a numeric/color value. `desktop-instance-placement.tsx` contains no arbitrary-value classes. Typography already uses product scale tokens. No `index.css` or AppShell edit is included here. A new token-class finding on the next reviewed head requires another main diagnosis before edits or push.

The reproducible design check is `node design/checks/desktop-instances.mjs` from `ui/`. It enumerates all instance frame/theme combinations and checks update visibility against the independent availability fixture, compares update DOM across connection states and connection-indicator DOM across update states, requires the signed-out/switching/offline + update combinations in both languages/themes, and checks avatar x=16, adjacent gaps ≤8px, two-row footers, viewport-contained menus and fixture-only network reads. It does not certify native updater behavior.

Known preview limit: Show annotation injection can log the documented `react/jsx-runtime` resolution warning inside canvas iframes (`docs/plans/design-frames.md`, Known by design). Direct design capture is the static verification surface; this lane does not modify the Show runtime.
