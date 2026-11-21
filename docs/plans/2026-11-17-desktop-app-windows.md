# Desktop App windows — W1

Owner: `ses8shnnh2vcm`. Base: `5c38e2353975d7e96fad9f813f636261fe0232be`,
verified to contain #3850 merge `dee14312f3d07a9d68dec7850a2a2035d8b39a8e`.
Scope authority: main run `1091c6306946` and the November 17 handoff.

## Outcome and boundaries

Every supported entry resolves Files, Terminal, Editor, Preview, Show Page and
Library through one UI resolver and the existing native `app_windows` owner.
An activation preserves live buffers while delivering new launch state; a new
window gets an independent identity. Browser and rejected native requests retain
a usable Workbench fallback. Preview and Library are part of completion.

This spans a native authority/lifecycle boundary and UI consumers. Native owns
window identity, caller admission, origin, serialization and cleanup. UI owns
product resolution, bodies and fallback. Preserve A5's physical main window,
actual Runtime/Account/candidate documents, guards, receipts and retirement.
Session/settings/Vault links and embedded Show/Mini exceptions keep their owners.
No public release, RC, entitlement/profile change, service restart or merge.

## Frozen producer/consumer contract

- Request: `appId` (six registry IDs), optional `sessionId` (Show only), `title`,
  `source`, `mode` (`activate|newWindow|newTab`), `newWindow` (derived from mode),
  `origin` (expected current origin, never authority), `requestId` (unique per
  action), and optional `params`.
- Sources: `dock`, `apps-launcher`, `library`, `command-palette`, `dock-shortcut`,
  `show-control`, `show-route`, `file-browser`, `chat-attachment`, `deep-link`.
- `params` is a closed, typed launch-state object: `path`, `cwd`, `filename`,
  `name`, `mtime` (number or null), `newFileDir`, `rootDir`, `mime`, `line`,
  `column`, `endColumn`, `initialTab` (`apps|showpages`). Strings are data, never
  commands or destinations. Validate types, limits and target applicability.
  Filesystem permission/existence checks remain at the Runtime API boundary.
  Absolute paths stay out of navigated URLs. No buffers/content in requests.
- Limits are UTF-8 bytes: path/cwd/newFileDir/rootDir 4096; filename/name/title
  512; mime 256; requestId 128. Request IDs are nonempty ASCII alphanumeric,
  dash or underscore. Native-generated windowId is output only, bounded at 512.
  `line` is a positive safe integer; `column` is a nonnegative safe integer;
  `endColumn` is nonnegative and at least column (default column 0). `mtime`
  is null or a finite number. Preserve zero-based editor columns.
- Allowed params by target: files(path,rootDir), terminal(cwd),
  editor(path,filename,mtime,newFileDir,rootDir,line,column,endColumn),
  preview(path,name,mime,mtime), library(initialTab), showpage(none).
  Omit undefined fields. Invalid or inapplicable input cannot trigger native
  effects; the resolver retains the existing fallback policy.
- Show session grammar remains the existing native Show validator. Show identity
  is `(native-bound origin, appId, sessionId)`; built-ins use `(origin, appId)`.
  Path/cwd are launch state, not identity. Prefer canonical window, otherwise
  deterministic surviving duplicate; explicit newWindow creates a distinct label.
- Native bodies use `/apps/{builtin}?standalone=1`; Show uses
  `/apps/show/<encoded sessionId>?standalone=1`, rendering the existing Show App
  body and its raw same-origin page inside it. This gives Show the common snapshot
  consumer and fallback host. Embedded chat Show previews remain unchanged.
- UI handles `newTab` synchronously through the existing `appTabHref` allowlist.
  If no tab surface exists, normalize to activate. Native accepts only activate
  or newWindow. UI animation origin stays in the Workbench fallback options;
  native placement remains OS-centered as in the existing owner.
  Existing modifier entries carry app identity/Show session, not file state.
  A target with rich params has no state-safe tab surface in this increment:
  normalize it to activate with its state intact. Do not introduce opener,
  sessionStorage mailbox or URL path-state transport just for hypothetical tabs.
- Legacy main IPC remains `open_app_window({request})`; preserve old requests
  with absent additive fields. New protocol advertises
  `window.__AVIBE_APP_LAUNCH_PROTOCOL__ === 1`. New UI must not send rich state
  through an old shell that would silently drop it.
- Native launch result is `{requestId, status: "opened"|"fallback"|"stale"}`.
  `opened` includes retained native windows after OS focus/show refusal;
  `fallback` means no competing native result remains; `stale` is consumed
  without replay into a successor document. UI fallback runs at most once.
- Initial and repeated native launch state is the same snapshot:
  `window.__AVIBE_APP_LAUNCH__ = {requestId, windowId, appId, sessionId?, title?,
  source, params?}`. Native initializes it before body scripts, and updates it
  before dispatching `CustomEvent("avibe:app-launch", {detail: snapshot})`.
  UI reads the snapshot on mount and subscribes; deduplicate by requestId.
  Reuse must not reload the document or discard Editor/Terminal state.
- Native-originated Show deep links have no UI pending request. After a genuine
  native failure with no competing window, native may deliver the same snapshot
  and `avibe:app-launch` event to the exact surviving published main Runtime,
  with `source=deep-link`, `appId=showpage`, and `windowId` equal to that Runtime's
  actual label. On this main-only surface that snapshot is a terminal fallback
  delivery, not a launch request; consume it once into the existing Workbench
  WindowManager, without native resubmission or route navigation. The ordinary
  matching result event may follow but is not required for mount-time recovery.
  Register the main consumer once under the existing manager, subscribe before
  reading the initialized snapshot, and deduplicate across remounts for the life
  of that document. Successful native launches emit no main snapshot; stale or
  retired sources receive neither snapshot nor fallback. Child snapshot consumers
  retain their existing body-state semantics. Snapshot data never grants native
  authority. Preserve current foreground/settings gates and retained drafts.
- Native derives local/remote origin and an exact caller/document binding from
  the published A5 Runtime; Shell.active_origin alone describes only local.
  Check admission before and after any deferred work. A payload origin mismatch
  cannot select another Runtime. An Account/candidate/retired document cannot
  launch Apps. Origin/lifecycle retirement invalidates pending work and children.
- Remote App bodies use a disposable per-window store. Project only the admitted
  Runtime's same-origin native session cookies with the existing instance_store
  primitive before navigation; never copy them into default storage. Bind every
  read/projection/build/result to the Runtime receipt. Retire children and their
  disposable stores through the existing completion/erasure primitives before
  their parent authority can be reused. Cookie/session/default-store evidence
  must use isolated fixtures; do not read the operator's cookies for tests.

## Child entries without shell IPC

Use a narrow navigation intent mediated by the existing native owner, following
the product's native presentation-request pattern: exact destination
`avibe-app://launch?request=<percent-encoded JSON request>`, no arbitrary URL or
action. A native App child or published Runtime gets the protocol marker only
where the native navigation handler supports it. Child IPC capabilities remain
absent. Native checks the actual source Webview and its recorded origin/document,
then sends `avibe:app-launch-result` with the result above to that same surviving
document. UI registers its listener before submitting the navigation.

Use this transport for new-protocol UI so local, remote and child entries share
one request/result contract. Do not add BroadcastChannel or grant child invoke.
Malformed destinations are consumed without navigation or effect. Stale results
cannot launch a fallback in a replacement page. Missing support may fall back
immediately; a timeout after dispatch is an unknown outcome, never permission to
create a duplicate. Keep requests bounded and settle normal error paths.

A child fallback must actually render using the existing WindowManager and
transient window layer, preserving its current body and buffers and never
restoring/saving the main Workbench layout. A hidden `wm.openApp` is not fallback.
If the native source diagnosis cannot implement this narrow mediation without
widening authority, return the concrete conflict to the lead before changing it.

## Work and evidence

Native lane `seszkwvjjwaz5`: `desktop/src-tauri/`, necessary deep-link contract
in `desktop/runtime-host/`, native tests and desktop documentation. UI lane
`sesuzge7suz9p`: `ui/`, resolver, standalone bodies, complete entry inventory and
UI tests/design frames. Separate worktrees; neither changes the other's files.
Lead owns this contract and serial integration. Package/install/window operations
are a later, single-owner step. No lane pushes or opens a PR before Max acceptance.

First demonstrate Files/Terminal/Editor/Show with real metadata, then complete
Preview/Library using the same protocol. Extend existing tests at their owning
boundary; run UI and Desktop builds, native focused tests and relevant A5 tests.
Prove serialization, source admission, stale completion, same-origin state delivery,
usable fallback, no child IPC, and buffer-preserving activation. Check both
producer and actual consumer; mocks that recreate the other half are insufficient.

Installed acceptance follows implementation → local ad-hoc package → Agent
installed smoke → Max acceptance → PR. Before packaging, report artifact/source/
assets identity and installation owner/route to main. Coordinate use of the
operator's App with main. Include nonempty-draft return; active-generation/account
actions require main coordination. Preserve the unreproduced November 15 SIGSEGV,
distinct Networking EXC_GUARD and Windows installed-acceptance gaps as unresolved
evidence limits. On crash, preserve the exact incident and stop before retry.
New user-visible copy goes to main before use; reuse approved bodies and wording.
