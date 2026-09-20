# Shared Workbench and General Settings — issue #2012

## PM ruling: queue snapshot ordering (2026-09-20, review 5259163447)

The refreshed head `73a8556ddbcc41f13bb8434a2b4fe2d81aacb060` received one
new actionable finding, inline `4055864365`, thread `PRRT_kwDOPbFPYs6kGUYR`.
Independent complete inventory: ten reviews, twelve inline comments, three issue
comments, eight threads with all nested pages exhausted; seven resolved and this
one open. This is the sixth findings-bearing head and eighth historical finding.
The current sixteen observed lint jobs have fourteen successes and two pending;
CI is not the review gate and does not remove this finding.

Causal diagnosis: send invocation identity protects one mutation's completion,
but queue snapshots also come from initial/authorization bootstrap, SSE queue
updates, reconnect recovery and mutation-triggered reads. An older successful
read can overwrite a newer successful read because all these writes do not share
an ordering owner. This is a repeated asynchronous stale-state class, so the PM
has stopped blind patching and inspected every `setQueue`, `refreshQueue`, and
`sendQueueNow` caller before authorizing the bounded repair below.

The invariant is: within the active route lifetime, an older queue snapshot must
not overwrite a newer successfully committed snapshot. A failed newer read must
not by itself discard an older valid successful fallback. Bootstrap and ordinary
refresh snapshots use the same queue ordering boundary; send-now keeps its
additional invocation admission guard. Leaving and returning to the same Session
must not re-admit an earlier route lifetime's reads or mutation completions.
Inspect optimistic remove and successful recall writes as part of this same queue
owner: a pre-mutation read must not resurrect a removed row; an explicit recovery
read may restore the authoritative row when removal fails. Keep existing error,
loading, Stop, spinner and server-authoritative queue behavior.

Authorized implementation scope is `ChatPage.tsx`, its existing
`ChatPage.hydration.test.tsx` consumer (and `ChatQueueRow.test.tsx` only if needed),
and this plan. Reuse the adjacent transcript snapshot ordering pattern where
appropriate; no shared hook, API, dependency, sidebar, Composer or backend change.
The prior byte-parity assertion describes the mechanical refresh phase only;
this separately recorded correctness fix is the sole newly authorized production
delta over original #2058. No architecture rewrite or owner question is required.

Acceptance requires a deterministic red reproduction on unchanged `73a8556dd`,
then passing real ChatPage consumers for late post-send versus newer SSE reads,
bootstrap overlap, route A-to-B-to-A lifetime rejection, and newer-read failure
fallback; cover remove/recall boundaries if changed. Verify visible queue rows and
their actions, not only helper internals. Run relevant existing tests, typechecks,
repository lint and UI build. One implementation lane prepares a local committed
candidate only; PM independently spot-checks code and consumer evidence before
pushing. Keep the original PM Watch/cursor unchanged, no manual review trigger or
CI rerun, and require new exact-head review/CI with this thread addressed afterward.

## Owner resolution: PR #2058 over #2061 (2026-09-20)

At 10:30 Asia/Shanghai the owner explicitly selected the complete PR #2058
presentation when resolving its conflict with merged PR #2061. This decision
supersedes the sidebar presentation and persistence contract in the retained
historical `2026-09-20-sidebar-31-alignment.md` plan.

Merge actual master `4de519cce2f7254325a88f652b31f510a7a8ff70` into PR #2058
head `5b0dab0cbb13fac318e5bfc4bb1778a90d02aca6`, preserving the latter's
`WorkbenchSidebar.tsx` and `WorkbenchSidebar.inbox.test.tsx` in full. Search
remains a full-width row below the brand; the logo, Inbox, and capability rows
follow #2058. Capabilities start expanded and retain their collapsed state while
the sidebar owner remains mounted, including across Settings, without storing
that preference in localStorage. Keep #2058's Composer spacing and queue send
feedback, current authorization filters, and Settings activity boundaries.

All production and test content must remain byte-identical to the original
#2058 head. Re-run the affected consumers, hermetic browser geometry/capability
cases, typechecks, lint, and UI build, then obtain fresh exact-head Codex review
and CI. Historical green results do not qualify for the refreshed head. This
authorizes updating the existing PR, not merging or deploying it.

PM review inventory before this refresh: nine reviews, eleven inline comments,
two issue comments, and seven resolved whole-PR threads, including exhausted
nested pages. The seven original findings span five heads: `366793a02f` (1),
`24357fb9ee` (2), `8d743f3d42` (1), `e47847a109` (1), and `ad60570259` (2).
Root causes are documentation drift, server-authoritative queue retention,
destructive voice-control placement, stale invocation effects, and disconnected
sidebar state/translation consumers. The stale-invocation class repeated on two
heads and requires PM diagnosis under the circuit breaker. Independent inspection
confirms the current invocation predicate gates the settled response, error,
queue-refresh result, and spinner cleanup; the sidebar toggle and eyebrow have
live consumers. The current focused consumers pass. This round preserves those
repairs byte-for-byte and resolves only the owner-selected presentation conflict;
it is not another queue architecture rewrite. Any new finding requires complete
inventory and a fresh PM causal/scope ruling before further edits or pushes.


Owner authorized parallel implementation on 2026-09-18 02:36 Asia/Shanghai. PM session: sestqz5wvu5ty. Branch feat/workbench-general-settings starts at GitHub-verified master 4019b704c99afe16223d475fccd9e1cb94a109d7 in its own worktree. #2011 is a separate active lane; #2013 remains deferred. Historical #2010-only or sequential-dispatch language is superseded by this explicit instruction.

## Outcome

Implement the approved shared Workbench shell, editable first-task home and General Settings from final design_desktop.pen. Preserve real projects, sessions, messages, inbox and permission-aware routes, one shared Composer and existing picker interactions. This is UI recomposition, not replacement of runtime/auth/data ownership.

## Accepted design and behavior

- Sidebar248: existing brand row, search/inbox with real unread badge hidden at zero, always-expanded main navigation, real project/session area or true empty state, bottom Apps/Settings, proper selected/hover/focus/status presentation.
- Home: Explore project / Research question / Make plan cards seed and focus an editable draft and never send. Recompose shared Composer with attachment, existing AgentRoutePicker, direct DirectoryBrowser and send. Preserve project find/create, user/default selection and permission rules. No second editor.
- One-time dismissible post-onboarding banner is conditional on the producer completion event and actual available Agent/readiness; ordinary visits have no banner. See exact cross-lane contract below.
- Continuation links keep `/settings/remote-access` and `/settings/platforms` destinations.
- General is the ordinary `/settings` entry (`/settings/general`), including return to app, General/AGENT/Connections/System groups, existing language ownership/autosave and explicit System/Light/Dark selection via existing ThemeProvider. Explicit deep links survive. Settings close preserves originating surface/draft/selection.
- Preserve approved themes/locales, actual product logos, smaller-screen access, keyboard/focus. No native titlebar/menu or fake native chrome. Owner-approved native canvas governs; no new design invention. Empty, error, permission and loading UI must express actual state.

## Native source and inspection

Read/export `/Users/max/workspace/ai/avibe/avibe-docs/design_desktop.pen` through Pencil; never mutate/save/switch active documents. Candidate nodes n0PzOn (Workspace), DkNTK (sidebar), r6G6P (General), uifbM (existing selector reference). A read-only observer supplies `/tmp/issue2012/design/design-index.md`; implementation writer must inspect actual exported images and current component ownership before visible edits. Discover final Light/Dark/EN/ZH variants, do not assume candidate IDs cover all frames. 1200x800 is a reference window, not fixed browser dimensions. Another project owns active Pencil document; content-identity check is mandatory because unavailable filePaths can fall back silently. If design.pen is inaccessible, use confirmed desktop source plus existing picker code without changing another canvas.

## Scope, testing, delivery

The copied parallel contract below is the exact producer/consumer/file division. Consolidate changes into this plan without editing #2011's working copy. No backend/schema/auth code, native desktop, brand replacement, or unrelated settings internals. No new dependency expected.

Use meaningful consuming tests: suggestions don't submit and preserve editability; Composer/picker/directory actions preserve draft and prior choice on cancel/Escape; route overlay restores state; real unread state; retained permission visibility; System follows OS while explicit preference and locale persist. Hermetic browser batch comparing actual renders against native exports in EN/ZH and both themes on desktop/narrow widths; fix observed defects once then targeted confirmation, no endless full matrix reruns. Run focused existing tests, UI build/lint/theme/i18n/type/catalog gates; never invent scenario IDs. Real auth/install stays #2011/manual acceptance, not claimed by screenshots.

No primary checkout/runtime/service/production stores/Incus changes. Test-owned fixtures and processes only; no local Avibe restart. Pre-push concrete diff/head/tests/visuals go to PM for independent spot-check. Then non-draft PR to master, Closes #2012, explicit #2011 dependency where applicable. No manual Codex trigger; cyhhao automatically triggers. Verify full exact-head review/all lint runs/zero unresolved threads and deliver final report before Watch cleanup. No merge without owner instruction.

## Frozen cross-lane interface

No API-name decision is pending. PM inspected #2011's actual type, method and route at its base head
f86a3dc683606456e491c71aa964ba468ca328de on 2026-09-18 02:45 and froze:

- `BackendConnectionState`, exported from `ui/src/context/ApiContext.tsx`
- `useApi().getBackendConnection(name: 'claude' | 'codex' | 'opencode'): Promise<BackendConnectionState>`
- `GET /api/backend/{backend}/connection`, fields unchanged from the contract above

The producer merged as PR #2032 (master 6ed01cd574328f90890680469619e0efcf87986b) and was merged into this
branch with an ordinary merge commit. The frozen names above are the merged ones; this lane imports them and
neither duplicates the type/method nor writes a second fetch wrapper, provider or store.

**What the consumer does.** `ui/src/components/workbench/backendReadiness.ts` holds the whole seam.
`useBackendReadiness(backend, asked)` issues at most one `getBackendConnection` per question asked — no
polling, no start, no auth, no install, no write — and publishes `{ backend, ready }` only when the answer
succeeded, says `ready`, and is about the backend that was asked about. Its type is `Pick<BackendConnectionState,
'backend' | 'ready'>`, derived from the producer's type rather than restated. Anything else — pending, an
unknown or unsupported backend name, a refused or denied read, a wrong-backend or not-ready answer — publishes
nothing and never blocks the home. An answer whose question is no longer being asked is dropped and what was
observed is cleared, including the A -> B -> A case where the stale answer names the backend now in effect.
`shouldShowReadyBanner(...)` is unchanged: it refuses anything not corroborated.

**The handoff is held for the page, not for the component.** The completion arrives as router state on a
navigation that crosses the setup boundary, and crossing it is exactly what makes `AuthGuard` re-validate:
the home is unmounted and remounted underneath it while that runs, after the history entry has already been
consumed. A latch in component state dies with that remount and the completion can then never be announced at
all — observed end to end in the browser, where the home issued no readiness read whatsoever. `useSetupHandoff`
therefore holds the event, and a dismissal, for as long as the page the wizard handed over to is open. Reload
silence is unchanged and is carried by the consumed history entry, not by the latch.

**And it ends when the user leaves the visit it was made for.** The home is unmounted for two unrelated
reasons — the guard re-validating in place, and an ordinary departure — and only the ROUTE tells them apart, so
`useSetupHandoffDeparture` reads the route the surface is actually rendering rather than any component's
cleanup; ending on unmount would end it during the guard's remount too, which is the bug the page scope exists
to fix. It reads that background route rather than the shell's chrome location: Settings opened over the home
is not a departure on either form factor, because the home is still there behind the overlay holding a banner
the user has not answered, while the shell's own location follows the foreground URL below md.

## Implementation record

- **Shell** — `AppShell` sidebar at 248 on `--sidebar-background`, brand row (mark, title, `workbench.eyebrow`)
  with full-width Search and Inbox rows below it, a capability navigation group that is expanded by default and can be
  collapsed, with each entry rendered by one `SidebarNavRow` carrying the
  three source states on tokens, real project/session tree or true empty state, Apps/Settings footer, version and
  live service badge. Nav-state tokens (`--nav-selected-bg`/`-border`, `--nav-hover-bg`, `--sidebar-background`,
  `--logo-well-background`, `--illustration-card-border`, `--desktop-overlay-shadow`) and the
  `--shadow-glow-nav-mint` role are additive; no existing token value or public API changed.
- **Home** — three suggestion cards seed and focus the draft through the existing Composer handle and never send;
  the shared Composer keeps its attachment, `AgentRoutePicker` and direct `DirectoryBrowser` behavior; project
  find/create/draft/cancel/empty-send and the permission rules are the existing ones. The column is fluid.
- **General** — `/settings/general` is the ordinary Settings landing and the returnable one. Language uses the
  existing shared selection path and autosaves; appearance is three explicit choices through the existing
  `ThemeProvider` (`mode`/`setMode`), so System follows the OS while Light/Dark persist. No Save button.
- **Language operation** — one instance-wide value, so its ordering belongs to the i18n instance rather than to
  whichever control is mounted: a private `WeakMap` in `useLanguageSelection.ts` holds the queue, the newest pick
  and the failure a Retry may still act on, while a mounted control lends it the api and the toast for as long as
  it is on screen. Leaving Settings mid-save no longer lets the next pick race the first, and a Retry whose
  control is gone is inert — the pick stands locally, only the write is not re-attempted on a departed control's
  authority. `AppShell` adopts the persisted language through the same operation, so neither a read that answers
  late nor the second read a language change itself causes can undo the pick that caused it. The key is the
  instance, not the object a component holds: `useTranslation` returns a fresh copy of the instance on every
  language change, keeping the original as `__original`, so the held object is replaced by the very operation
  being ordered — as `ApiContext`'s value already is, being memoized on `t`. Authority is asked twice and the two
  answers are not interchangeable: whether a pick is an instance decision at all is settled by the control the user
  made it through, so a member's local-only pick stays local even if they are granted instance rights before an
  earlier save settles, and a write that is still ours to send reads the authority current when it runs rather than
  the one remembered from when it was queued. A request already on the wire is not recalled.
- **Primitive** — `SettingsPanel` gained a `preference` variant (surface-2, one 22px pad, radius 12, no divider);
  the `panel` variant is untouched.

## Deviations from the approved source

Each is stated rather than silently closed:

1. **Settings keeps the 248 workbench sidebar** — assessed and accepted, not inherited. The source board is a
   native window, which is why Web also drops its titlebar and traffic lights; on Web, Settings is a route in the
   one shell, and the overlay path deliberately keeps the sidebar visible so the origin project, session and draft
   stay in view. Hiding it only on the direct route would give one URL two chromes and jump the layout on close.
   What the source actually fixes is the frame beside the rail, and that is reproduced exactly: 1004 at a 1448
   window, asserted in `geometry.spec.ts`. The visual observer confirmed no cropping or crowding; PM accepted this
   as a bounded Web adaptation on 2026-09-18 05:13.
2. ~~Heading is `SettingsPageShell`'s 28/700.~~ **Resolved.** `SettingsPageShell` gained an opt-in
   `titleScale="landing"` (27/600 over a 13 muted line, source `Ozmrf`) consumed only by `SettingsGeneralPage`;
   the fifteen sibling sections keep the 28/700 they shipped with, which is asserted from both sides.
3. **The `a38Xy` "⌘," reminder is omitted** — the design index itself marks it macOS-menu specific.
4. **No "⌘N" hint on the New chat row.** `DEFAULT_ACTION_SHORTCUTS` binds only voice input and Show Page
   annotation, and the browser owns ⌘N; a visible hint for an unbound key is a false affordance.
5. **The System miniature is drawn as a diagonal split.** The source renders System pixel-identically to Dark,
   which the index records as a source defect; binding it to the resolved theme instead would make it identical to
   whichever of the other two is live. Both halves use the source's own swatches.
6. ~~The home canvas carries the `--gradient-console` wash.~~ **Resolved.** `/` alone is drawn on flat
   `bg-background`; the wash stays with the rest of the console family (Agents, Skills, Harness, Vaults, Inbox,
   chat). Both halves are asserted — `background-image: none` on the home, a `radial-gradient` still on `/agents`.
7. **No attach/voice control on the first-task home.** `mediaEnabled` is `Boolean(sessionId)` and the home has no
   session yet; a pre-session upload would need an API that does not exist.
8. **Traffic lights, titlebar, window border and outer radius are absent**, per the index's own out-of-scope list.
9. **Narrow layout has no native baseline** (the index confirms no narrow/mobile frame exists). The phone
   decisions made here: sidebar drops to the existing mobile shell, suggestion cards stack, a preference card's
   control drops under its label instead of squeezing the description, and the composer block pins itself to the
   bottom of the scroll area one nav clearance above the tab bar (see 11).
10. **Models and Groups rows are absent from the captures only** because the fixture leaves those features off;
    the rail renders them from real state. The same applies to attach/mic on the first-task home (7) — neither is
    a missing control to be drawn in for a screenshot match.
11. **The phone composer is pinned, not in flow.** Measured at 390×844, the home is ~108px taller than its scroll
    area, so at rest the block that ends the page — the composer's Agent, workspace and Send row — sat under the
    fixed tab bar: `elementFromPoint` over Send returned the nav, i.e. a dead primary button, reachable only by a
    scroll the user has no reason to make. The alternative, trimming the rhythm above it until one phone fits,
    breaks on the next shorter phone. `--mobile-nav-clearance` now names the bar's reservation once, the shell
    pads its scroll area by it and the composer block offsets by it, and a short fade marks the boundary the cards
    scroll behind. Desktop is untouched.
12. **Settings rail labels wrap instead of truncating.** At the source's 196 rail, English cut both "Messaging
    Platforms" and "Platform Connections" to "Platform…", which are neighbouring rows leading to different pages.
    Two lines at 12.5px still fit the row height the rail already had, so no row moves and the rail keeps its
    width; measured as `scrollWidth === clientWidth` for every rendered rail label.

## Verification evidence

- `ui/e2e/workbench-general/` — a hermetic browser harness that loads the **real app** on a loopback dev server
  pointed at a dead backend port. Every request is answered locally, allowed as a font read, or aborted and
  recorded; `expect(denied).toEqual([])` is the proof that no write verb and no other off-origin call occurred.
  Fixture data is deliberately non-ASCII (`中文项目`, `/Users/max/工作区/中文项目`).
- `geometry.spec.ts` (12) measures rather than infers: sidebar 248 at 1200 and 1600 with the content taking the
  full extra 400 (952 → 1352); settings rail 196 with the General card fluid past the shared cap (692 @1200 →
  1412 @1920); every other Settings page still 1180 at 1920; the Settings content frame exactly 1004 at 1448; the
  card anatomy — radius 12, pad 22, row gap 20, `--surface-2` fill, 190×40 selector at radius 9, selected choice
  at 2px `--mint` with equal-width siblings, 14 gutter, 88 preview at radius 6; the landing heading 27/600 against
  a sibling section's 28/700; the home flat while `/agents` keeps its wash; the Settings overlay opening at the
  sidebar's own measured edge and restoring the route, draft and workspace selection on close; every rail label
  unclipped at 196; and the phone composer clear of the tab bar in EN and ZH, at rest, focused with text, and at
  390×667 — asserted by what `elementFromPoint` returns over Send, which visibility alone cannot answer.
- `capture.spec.ts` (18) writes the comparison batch to the stable path
  `ui/e2e/.artifacts/workbench-general/shots/` — both surfaces × EN/ZH × Light/Dark × desktop/narrow, plus System
  mode under both OS answers. Playwright's `outputDir` is a sibling (`run/`) precisely because it is emptied on
  every run, including a filtered one.
- Defects the batch actually caught and fixed: a comment block that had leaked into the Composer's JSX and was
  rendering as visible text; the preference card drawn at radius 16 where the source says 12; the theme
  miniature at radius 8 where the source says 6; a truncated brand subtitle at 248; a React duplicate-key warning
  on the mobile tab bar; and the narrow language row squeezing its description to one word.
- Defects the observer's 18-image review caught and this lane fixed: the desktop overlay seam (the shared dialog
  primitive's historical 240 against the shell's 248), the General heading scale, the home background family, the
  phone composer under the tab bar, and the English rail truncation.
- Gates, all green after the final edits: vitest 4352/4352, `typecheck:tests`, `lint` (baseline, no drift),
  `validate:theme`, `validate:catalog`, `build`, and the 30 browser tests above. One `travellingTokens` case
  failed once in an early full run and has not reproduced in either full run since, including the final one; it
  is recorded rather than explained away.

## Review round 1 repairs

Five findings, all inside this lane's own surfaces. Each is stated by its root class, not by the comment that
found it:

1. **Redundant alt text on the sidebar brand image.** The link's accessible name is the localized brand text
   beside it, so naming the image too read the destination twice. The image is decorative (`alt=""`,
   `aria-hidden`). Only `WorkbenchSidebar`'s image is new in this PR; `AppShell`'s mobile header brand carries the
   same pattern and predates it, so it is reported rather than swept in.
2. **A phone lost an unsent composition when it followed a Settings continuation.** The home's continuations lead
   away from an unsent draft plus the Agent and workspace it is aimed at, none of which is persisted anywhere, so
   its Settings ingress now records an origin on mobile as well as desktop. That keeps the one Workbench instance
   mounted behind the full-screen mobile Settings surface, and Back returns to the composition the user left. This
   retains that one route's local state and nothing else: every other mobile route keeps its ordinary
   unmount-on-navigate lifecycle, and a direct Settings link still invents no origin. The change is at the existing ingress owner (`settingsOverlay`); the
   mobile chrome is unchanged, because `AppShell` keeps its own desktop guard — a retained origin changes what
   survives behind the surface, not what is drawn over it. The one presentation seam, the dialog primitive's left
   border at the viewport edge, is neutralized in the consumer so the surface looks identical either way.
3. **The continuation row offered a member-and-above destination to everyone.** Both links are
   `OWNER_ONLY_ROUTES`, so anyone else following one is bounced straight back. The whole row is gated on
   `can_manage_instance` rather than the links alone, because the sentence around them exists only to introduce a
   destination they cannot reach.
4. **The retired `/settings/appearance` alias still pointed at Replies.** General took the theme controls over, so
   the alias follows them; Account keeps pointing at Replies, which still owns it.
5. **An installed PWA could not cold-launch back onto General.** `RESTORABLE_EXACT_PATHS` is an exact list, not a
   prefix rule, and the new page was missing from it.

What the audit established, and why the evidence is shaped the way it is:

- The mobile Workbench home has exactly one Settings ingress — the two continuation links. The sidebar's Settings
  toggle is inside a `md:flex` aside and the bottom tab bar has no Settings tab, so the origin policy covers the
  whole chain rather than one of several doors.
- Nothing in the product links to `/settings/appearance` any more, so the alias is reachable only as a stale
  bookmark: a document load, which invents no origin by design. The origin-carrying alias hop is therefore
  asserted at the boundary owner in unit scope, and the reachable flow is asserted in the browser.
- A chosen workspace and the continuation row do appear together — through the chip's manager branch. Only the
  `ProjectPicker` popover is mutually exclusive with the row: the server projects `can_manage_instance` from
  member upward and `can_chat`/`can_use_files` from editor upward, so the editor who gets the picker has no
  Settings ingress on the home at all. A member or owner gets the other branch of the same chip —
  `NewProjectDialog` → `DirectoryBrowser` → `createProject` → `upsertSelectProject` — and `create_project` is
  find-or-create by folder path, so opening a folder that is already a project selects that existing row. The
  continuation round trips therefore choose their workspace through that shipped path, with no permission
  widening, and assert the create call carried exactly that one folder and was not repeated on the way back.
- That path cannot be driven under the dev server. `DirectoryBrowser` clears a mounted ref on unmount and never
  restores it, so StrictMode's mount→cleanup→remount leaves every browse response discarded and the dialog stuck
  loading — a pre-existing, development-only defect in a shared primitive this PR does not own and does not
  touch. This file therefore runs against the production build, where StrictMode is inert, under its own config
  and the same hermetic harness; the rest of the suite stays on the dev config, which now ignores this file.

`mobile-continuation.spec.ts` (5, hermetic, built app) covers this: both continuations preserving a non-ASCII
draft, a non-default Agent, a workspace opened through the directory browser and the same DOM instance across
Settings-internal navigation — including General and its theme controls — and back; a control that leaves by an
ordinary tab and returns with browser Back, the same return path the chat-apps case uses, where every one of
those assertions fails because the home is a new one; the picker branch under the editor projection, which has no
continuation to offer and issues no create call at all; and the retired alias landing on General with Account's
destination unchanged. All folder and project endpoints are test-owned fixtures — no real directory is browsed
and no project is created. Unit scope adds the mobile origin policy and its onward hops, the PWA
write→read→resolve chain, and an `App.tsx` AST assertion that each retired alias points at the page that took its
content over. Re-validated for this round: the five browser tests, the six affected unit files (70), `typecheck`,
`lint` on the changed files, and `build`. The earlier full-suite and screenshot gates are not re-run here, because
nothing in this round changes what they measure.

## Review round 2 repair — the phone's way out of a retained Settings chain

One finding, in the same mobile Settings navigation-lifecycle class as round 1's second repair, so the two-head
circuit breaker applied and the whole class was inventoried before anything was edited.

Retaining the home's origin on mobile gave the phone something to close *back to*, but the phone's own root
control was never told: at `/settings` the header chevron stayed an ordinary `NavLink` to `/`. It pushed a second
home in front of every Settings entry the user had opened, so one Back re-entered Settings — with a deeper chain,
several Backs. The home itself came back intact, which is why the round-1 evidence passed: it asserted what the
returning surface held, and same-path reconciliation satisfies that even when the history behind it is wrong.

Every other way out already goes through one owner, `closeSettingsOverlay`, which unwinds to the recorded history
index or replaces with the origin when there is none:

- the desktop close (`X`) and the rail's back row, both via `ReturnToApp`;
- the dialog's own dismissal (Escape, outside click) via `onOpenChange`;
- `AppShell`'s sidebar Settings toggle;
- the browser/OS Back button, which needs no control at all.

There is no other mobile exit to fix: `AppShell` treats a Settings route as `isFullScreenMobile`, so the mobile
brand header and the bottom tab bar are not rendered over the surface, and the chevron's Settings-internal
destinations are navigation *inside* Settings, not a close. So the repair is one missed caller joining the
existing owner: when the chevron's target is the Workbench it renders `ReturnToApp` like the desktop controls do,
and keeps its label, touch size and icon. A direct visit has no origin and still gets a plain link to `/`, and
every internal destination stays a link so the origin rides along with the push.

Evidence is the property, not the control's shape. A sixth browser case builds a real predecessor (Inbox → home),
records the home's history index, primes the draft/Agent/manager-picked workspace, walks a continuation, the
section list, General and back to the list, leaves through the actual control, and asserts the index is the one it
started from — then repeats through the other continuation and finally goes Back to the Inbox rather than into
Settings. Against the unrepaired product that assertion reads 6 where it must read 1; the case locates the control
by label so it fails on the history, not on the element type. Unit scope adds the close behaviour at the phone
root and holds internal back destinations to links. Re-validated: the six browser cases on the built app, the five
affected unit files (91), the browser suite's `typecheck`, `lint` on the changed files, and `build`.

## Producer integration — what the end-to-end run found

Wiring the merged producer into the home was one function body, as planned. Driving it end to end was not, and
that is where the value was: `e2e/workbench-general/setup-handoff.spec.ts` walks the real wizard to its own
Enter, lets the real completion handler save and navigate, and then holds the home's readiness read open — a
state no unit test can stage, because the pending window only exists between a real navigation and a real
response. The banner must stay silent until that read answers for the backend the home's Agent route actually
runs, and the case asserts nothing was ever refused by the harness guard.

The first run showed the home issuing no readiness read at all. The wizard's navigation did carry
`{ onboardingCompleted: true }`, and the home did consume it — and was then unmounted and remounted, because
`AuthGuard` deliberately re-validates when the setup boundary is crossed and shows Loading while it does. The
remounted home read an entry it had already emptied, so the completion was gone before the Agent route had even
resolved: with a latch in component state the banner could never appear after a real wizard completion, however
ready the backend was. Every mocked suite passed throughout, because each one renders the home once.

The repair keeps the accepted semantics exactly and only changes the latch's lifetime: `useSetupHandoff` holds
the completion, and a dismissal, for the page. Nothing else moved — the history entry is still consumed on
arrival, reload silence is still carried by that consumed entry, and `shouldShowReadyBanner` is untouched.
`AuthGuard` is not touched either; its re-validation is deliberate, and the consumer now survives it. Both the
new unit case and the browser case fail against the component-state latch and pass against this one.

Review then found the other half of the same lifetime: it had a correct start and no end. A page-held handoff
survives an ordinary departure too, so arriving home with a completion, leaving for another route, and coming
back announced the setup a second time. The end is the route, not the component — see the contract above — and
`SettingsOverlayRouteSurface` is the one place that already knows which route is being rendered behind any
overlay, so it states the departure there instead of a second router integration. The real shell reproduces it:
`e2e/workbench-general/setup-handoff.spec.ts` leaves the home through the sidebar's own link and returns
through it, and that case fails without the end and passes with it. The same browser flow now also picks a
NON-default model through the composer's own picker while the readiness read is still open, so the announcement
arriving over a home in use, and being dismissed on it, is held to leaving the user's route untouched.

## Progress

- [x] Owner parallel start, latest master and isolated workspace verified.
- [x] Native design packet and existing route/state ownership inventory.
- [x] Independent shell, home/Composer, General implementation.
- [x] Producer #2011 contract integration and cross-lane consumer verification — #2032 merged into this branch;
      the home consumes the merged type/method, and `e2e/workbench-general/setup-handoff.spec.ts` drives the real
      wizard Enter, router, `ApiProvider` and completion handler against fixture transport.
- [x] Focused/browser verification; PM pre-push spot-check pending.
- [ ] Non-draft PR, exact-head Codex/CI and zero unresolved threads.
- [ ] Owner acceptance and separately authorized merge.

# #2011 / #2012 parallel implementation contract

PM: sestqz5wvu5ty. Owner authorized #2011 on 2026-09-18 02:16 and #2012 parallel start at 02:36 Asia/Shanghai. Both start from master 4019b704c99afe16223d475fccd9e1cb94a109d7. This explicit decision supersedes historical sequential-dispatch restrictions. Each uses its own default-branch worktree; no stacked PR and no base-branch direct commit is authorized. This outside-checkout document is authoritative until consolidated into the separate issue plans. Each lane commits its contract copy before feature edits. Do not overwrite a peer's changing plan.

## Exact ownership

#2011 session ses6652kpzsns owns Python/auth/install/runtime/readiness, ApiContext additions, Wizard/steps/onboarding, settings/{BackendOAuthPanel,oauth,providers,shared,BackendLifecycleChip}, wizardConfigMutations, auth_setup scenarios and direct tests. It also owns the historical shared-web-desktop-design plan consolidation, its own active plan, and onboarding fidelity tests. Its owner amendment restores the existing BrandLogo on the Web header and identical top-anchored heading/subtitle positions between both steps. Desktop behavior remains #2013.

#2012 owns AppShell, Workbench, workbench/{WorkbenchSidebar,Composer,new home components}, necessary existing project/folder presentation glue and useNewSession, SettingsLayout/SettingsOverlay*/SettingsPageShell, a new SettingsGeneralPage, adminNavigation/settingsRoutes/settingsOverlay, LanguageSwitcher presentation/shared language behavior if necessary, and relevant tests. App.tsx: ONLY General import/route and ordinary Settings landing wiring; no AuthGuard changes. ThemeProvider stays the single state owner and its external contract is preserved; consume mode/resolvedTheme/setMode, do not create another theme store. Existing AgentRoutePicker/DirectoryBrowser and MentionEditor behavior is reused, not replaced. Existing providers, inbox, search, Apps and status are reused. Do not redesign internal Model Hub/Agents/Skills/Harness/Vaults/Memory pages.

#2012 owns global index.css and shared primitive additions but must preserve existing values and public APIs; add scoped variants or new semantic tokens only. #2011 requests missing global tokens from PM, not unilateral global edits. Neither replaces brand assets. Shared i18n JSON overlap is limited to disjoint key ownership: #2011 onboarding/auth/provider keys; #2012 sharedWorkspace.*, settings.general.*, and necessary workbench/nav/settings-layout keys. No full-file format churn. PM checks merged key inventory and both build/test consumers after integration.

## Behavior boundary

Producer #2011: after explicit Enter, fresh authoritative readiness, successful required start/apply and narrow setup_completed save, navigate to `/` with React Router state `{ onboardingCompleted: true }`.
Consumer #2012: only that transient navigation permits the one-time dismissible banner. It is not authorization or Agent data. Consume it once without dropping other router state or the underlying Settings return location; dismissal/ordinary reload/return must not recreate it. Never persist a new onboarding database or auth flag.

Agent labels/default resolution come from existing `useNewSession` / `listVibeAgents({includeDisabled:false})`: actual `agents[]` with `name,display_name,backend,enabled,archived`, `default_agent_name` and effective project/user selection. Enabled alone is NOT applied authentication. Do not invent readiness fields on VibeAgentBrief, manufacture an Agent or pin a backend default.

#2011 produces `GET /api/backend/{backend}/connection` with `{ok, backend, installed, enabled, auth: subscription|api_key|none|unknown, application: applied|draining|failed|stopped|unknown, ready, entry_eligible, permission_required?, message?}` and the canonical ApiContext type/method. #2012 may consume the producer's published API to corroborate banner readiness; only `ready === true` corroborates a ready claim. False/pending/error must not render a successful banner or block existing Workbench access. No auth/start/save in the banner. Exact exported TS method/type names are to be delivered by #2011 before this dependent consumer is coded. Meanwhile implement all independent shell/General/composer work. Do not write a second fetch wrapper, type owner or dummy product fallback merely to compile against unmerged code. Final #2012 integration rebases after #2011 merge; dependent readiness verification must be completed before delivery, and the PR names that dependency.

Ordinary Settings opens `/settings/general` irrespective of last visited settings subsection. Explicit `/settings/backends`, `/settings/backends/{claude,codex,opencode}`, `/settings/platforms`, `/settings/remote-access` remain destinations. Overlay close preserves originating project/session/unsent draft/Agent selection and history semantics. Non-owner permissions remain intact. General language uses existing local update and permitted config autosave; theme System follows OS, explicit modes persist through current setter. No native host detection/header suppression/menu work.

## Integration acceptance

PM will verify the actual setup->Workbench event with producer and consumer together, non-ASCII draft/directory, Settings open/close preserving draft/project/Agent, permission-aware visibility, empty-send, suggestion-seed-without-send, theme/System persistence. Separate mocked suites cannot certify the cross-lane seam. Each lane first delivers a concrete diff/tests/native-source versus render to PM, then pushes a non-draft PR after PM spot-check. No merge without owner instruction. Never manually trigger Codex; cyhhao triggers it. Maintain lane and independent PM combined PR/CI Watches when a PR exists, forever and both timeout layers zero, durable cursor unchanged between heads. Paginate reviews/threads and apply repeated-class two-head breaker with PM diagnosis before another patch.
