# Host-owned update intents (3.2.7)

## Outcome and ownership

Every update panel presents the selected host's existing update owner to check,
change its preview channel, install one exact target, or change one automatic-update
policy. A local Desktop footer still owns the local app even while its client
selects a remote Runtime; a remote-selected footer opens the bundled trusted
updates view instead of gaining local IPC. Standalone Runtime updates remain
Python-owned; Desktop-managed Runtime updates remain native-owned. No cloud
installer, duplicate channel preference or second remote-install setting is
introduced.

The shared read model from #3676 is the base: Runtime `current` remains the
post-restart version proof, while `desktop_version` is the native app's identity.
The UI lane owns the shared panel/adapters, trusted view and localized copy. This
lane owns Runtime intents, native execution, additive capabilities, status receipts,
and the native automatic-update policy. The complete cohort is the 3.2.7 train;
none of these parity changes ships in 3.2.6.

## Ordered changes

1. **Check/channel intents**: additive capabilities, bounded native control
   mailbox, honest in-flight/result relay and owner-agnostic HTTP actions.
2. **Explicit update-click authorization**: the authenticated instance manager's
   Update action authorizes that one installation. Remove the new shell's
   remote-install preference and second host confirmation, without weakening
   target binding, expiry, busy ownership, signature verification, settlement or
   rollback. Advertise `install` only when this behavior exists.
3. **Automatic-update parity**: expose one `auto_update` setting and one
   capability across hosts. Standalone reuses its real update policy; Desktop
   adds a native-owned preference, scheduling/idle gates and automatic install.
   The default is one native constant: Desktop-managed hosts default off for new
   and existing installs, while standalone Runtime keeps its existing default
   and stored value.
4. **Trusted updates view**: remote-selected Runtime footers present the same
   panel in `account/index.html?view=updates`; only trusted account IPC may invoke
   the four update actions and every install remains exact-target and generation
   fenced.

All four target 3.2.7, not the 3.2.6 desktop build. #3682 is the single integrated
train PR after the UI owner's explicit frozen-head handoff from #3681. The
integrated train has one writer, full exact-head Codex plus independent UI/native
review, and one hosted acceptance SHA. Product gives the merge go and the release
lead merges; no partial panel release or version-number compatibility branch is
allowed.

## Additive surface contract

- `/api/version` and `vibe check-update --json` continue to share
  `desktop_update_snapshot`. Add `auto_update: bool | null` and
  `host_actions: string[]` with recognized values `check`, `prereleases`,
  `auto_update`, and `install`; absent/malformed lists mean legacy behavior.
  `install` promises explicit-request installation without `awaiting_host`.
- Preserve the last completed native check while another check runs. Relay a
  separate bounded control receipt and expose effective `phase: checking` for
  that active check, without erasing the previous verified offer or changing the
  legacy raw check record enum. Expired/interrupted work cannot leave an eternal
  spinner.
- Native IPC/account projection exposes `autoUpdate`, `hostActions` and
  `checkedAt` from the same native owner. Local IPC capabilities may exist before
  the first check; Runtime HTTP capabilities come from the separate persisted
  status snapshot and are absent until the owner publishes a usable record.
- `POST /api/update/check`, `/api/update/prereleases {"enabled": boolean}` and
  `/api/update/auto-update {"enabled": boolean}` are instance-management
  actions under existing origin, CSRF and authorization checks. Desktop-managed
  hosts enqueue native control intents; standalone hosts use the existing
  checker/config preference. Install remains `/api/desktop/update/request` or
  `/api/upgrade` as appropriate, always with the exact target where the host
  supports it.
- The bundled account bridge accepts exactly `update_check`,
  `update_set_prereleases {enabled}`, `update_set_auto_update {enabled}` and
  `update_install {target_version}` inside its existing v1 request envelope.
  Only the trusted bundled document can invoke it. Request version/identity,
  coordinator busy ownership and expected generation are checked before native
  execution; the updater retains generation fencing through installation.
  These actions do not use the selected Runtime's cookies or persist account
  state. Replies read the updater's live `state.desktop.update` projection.
  Footer/menu/deep-link presentation is a queued native intent: window
  contention, lifecycle rebinding and failed navigation retain it for view
  signals and one bounded-rate retry worker. Native menu/deep-link presentation
  first reaches the current document's unsaved-work guard. Its presentation-only
  receipt follows both synchronous and asynchronous approval into the native
  URL, bound to the original owner, intent, delivery attempt and receiver;
  enqueue rechecks it under the queue lock, so an older approval cannot adopt a
  newer selection or overwrite a newer menu intent. A callback-confirmed event
  dispatch is terminal for prompting whether the user approves or cancels;
  the receipt remains valid for a later approved URL until superseded. Only a proven
  pre-dispatch failure retries that guard, while unknown execution waits for an
  explicit new request rather than repeating a possible prompt. Approved page
  navigation settles only its exact intent; newer account/selection/escape
  owners supersede older intents. Source authority does not require an idle
  window, but staging and unpublished candidates cannot enqueue. A pending
  candidate needs its exact public commit receipt; enqueue retains the
  source-validated owner instead of adopting a newer action's authority.
- Policy-only `auto_update` controls preserve the native phase, manifest, latest
  offer and original `checked_at` on success, expiry and save failure. Their
  control receipt is not a discovery/checking state on any surface.
- The Desktop auto-update default is a single native constant, currently off for
  both new and existing Desktop-managed installs. Standalone Runtime preserves
  its existing `update.auto_update` default and stored values.

## Control intent transport and lifecycle

Keep the legacy strict install request and receipt intact. A separate
`desktop-update-control.json`, beside the existing desktop state, carries only
`check`, `prereleases` and `auto_update` commands; it can never be interpreted as
an install by an old shell. Reuse the native local Runtime's existing SSE nudge
and periodic mailbox poll. New control intents share the updater's busy gate with
installation and local checks.

A control request has schema version, canonical UUID, action (`check`,
`prereleases` or `auto_update`), an `enabled` boolean only for the latter two,
pending/expired state, and created/expiry timestamps. Bound both file size and
lifetime. A receipt binds the same UUID/action/value and records
checking/completed/failed/expired, error code and timestamps. Same pending intent
retries reuse identity; different live control/install requests return busy.
Completed receipts suppress mailbox replay. API ack timeout expires only its own
request; native revalidates the current matching pending file before changing a
channel or policy and before publishing a result. Native check I/O has a bounded
timeout; expiry never releases another operation's lock.

Only the native shell writes its channel and Desktop auto-update files. Standalone
preference changes use `update_config_fields` under the existing config transaction
lock, never a full config overwrite. A requested check never installs or launches
a host confirmation prompt. Automatic installation must pass idle/busy, signature,
recovery and restart gates. Readiness/error and result timestamps belong to the
owner.

On native restart, an unfinished control receipt becomes interrupted; a stale
request must not be replayed as a fresh channel or policy change. Native status
additions are optional on read, so old Runtime readers retain existing results.
New Runtime never sends control commands without capability support. A stale
capability snapshot can at worst produce an unavailable/expired request, not an
install or legacy-file mutation.

## Automatic-update inactivity

Window/presentation idleness is not Runtime idleness. The native scheduler reads
`GET /api/desktop/update/idle` on its lifecycle-owned local loopback origin, never
the selected remote Runtime. This direct-loopback-only read reaches the live
controller through the existing authenticated control IPC. Old controllers,
missing owners, malformed replies, timeouts and readiness/generation changes
refuse unattended installation; manual exact-target installation still works.

The controller clock starts cold at startup and observes real turn/message
activity plus shared Activity and runtime-generation binding boundaries. These
synchronous signals catch short turns, detached task failure/stop, completed
outputs, outside-turn request sequences and lease release between native polls.
Signals only reset the quiet clock; they never grant idle authority. Each probe
also reads live turn gates, sinks, backend ownership, retained Activities and
pending teardown even after a backend is removed from routing, and rereads the
clock after asynchronous probes finish. The native policy requires 30 quiet
minutes and keeps its interval/offer grace and exclusive updater busy gate.

Recheck inactivity before discovery, before download, after payload signature
verification, and at the actual replacement handoff: after macOS extraction and
codesign immediately before rename; on Windows after helper preparation and
before the shell exits. A refused Windows handoff terminates and joins only its
owned preparation helper. A deferral leaves the current application and offer
intact. Generation/target/request-lifetime validation is repeated at those same
installer boundaries. The exact skipped release is also refused by unattended
installation: clear its remembered offer and reread the skip at download,
verification and final handoff. A new explicit Update click may override that
skip. No production app replacement is implied by fixture tests.

## Authorization-policy follow-up

The owner removed the remote-install setting, not installation validation. Keep
request identity/target binding, signed fresh discovery, expiry/busy checks,
exactly-once settlement and old-app preservation. Preserve preference data for a
downgraded/older shell rather than overwriting it. A new UI must keep an honest
fallback when an older shell reports awaiting_host.

The current API's instance-management role covers owner/member and rejects
editor/viewer. Keep exactly that rule for check, channel, automatic-update and
explicit install: `can_manage_instance` is the authorization boundary. The owner
confirmed that "signed-in owner" means the person who manages this instance, not a
new owner-only role distinction. Removing the host prompt never grants access to
editor/viewer or bypasses origin/CSRF protection.

## Implementation status

- The committed check/channel slice passed its focused Python, updater/platform,
  Rust, Clippy, exact-head CI/review and controlled credential-free Incus HTTP
  acceptance; it remains held for the complete 3.2.7 train.
- The explicit-click commit has passed its focused Python and Rust updater
  tests. It removes the new-shell preference commands, keeps the legacy file
  untouched for downgrade, binds the clicked target, retains the
  generation/installer-edge checks, and derives delivery from actual `install`
  capability with a legacy fallback.
- The native automatic-update slice now adds the off-by-default Desktop
  preference, `auto_update` control/receipt and capability mapping, idle/busy
  scheduling, one-check grace before install, preference-disable fencing at the
  installer edge, standalone policy mapping, and focused default/control/API
  coverage. Follow-up review adds the controller-owned inactivity observation,
  retained-work coverage, final platform handoff checks and policy-only state
  preservation. Controlled native lifecycle acceptance remains a separate gate.
- Disposable macOS/Windows CI and exact-head Codex review for this new slice are
  still gates, not claimed results. No signed application replacement has been
  exercised in this step; controlled disposable native acceptance remains a
  separate gate.
- The trusted-view native protocol, presentation and dispatch are implemented:
  remote-selected footers and the native menu present the bundled updates view;
  the four actions reuse the same local updater, without another native
  confirmation dialog. The integrated UI panel/adapter consumes this bridge.
  Coordinator/protocol tests cover local and remote-selected dispatch, no
  credential/network side effects, malformed actions, busy rejection, stale
  generations and delayed-completion fencing. Native-chain fixtures now use the
  trusted panel and explicit-click policy instead of the retired host-prompt
  flow; they still require a controlled disposable-host run with the integrated
  UI/native heads. Source tests and fixture collection are not that acceptance.
- The direct-loopback idle read is explicitly declared as GET in the canonical
  Member allow-list; other unclassified methods remain Owner-default. The global
  authorization sweep and focused HTTP role/origin regression cover the boundary.

## Validation and acceptance gates

- Pure Python/Rust fixtures: malformed/oversized/expired/future/unknown-schema
  requests, exact booleans, UUID/action matching, duplicate retries, conflicting
  intents, install/control exclusion, stale receipts, restart interruption,
  failed publication and channel/policy writes, check timeout and no implicit
  install.
- API: real role matrix, CSRF/origin, standalone transaction preservation,
  legacy capability rejection, ack timeout/expiry, new-native receipt relay,
  actual install capability versus legacy delivery hint, and the same snapshot
  from CLI and Web.
- Cross-version: old/new Runtime and shell combinations, legacy install receipt
  untouched, capability absence/staleness, persisted channel rollback behavior,
  auto-update migration/default behavior, and old-shell fallback.
- Native/local selection: footer always local, remote-selected footer uses only
  the trusted updates view, host intent never follows an unrelated selected
  Runtime; preserve generation/instance fencing from #3653/#3661.
- Runtime end-to-end runs only in a task-owned local Incus worktree environment,
  not the shared master environment or any customer/owner host.
- **Merge gate — scoped hosted native handoff:** disposable GitHub-hosted
  `macos-26` and `windows-latest`, both on the integrated PR head, with fixture-only
  signing and isolated native paths. Actual rendered panel DOM actions must
  reach the real checker/verifier/installer; macOS swaps and restarts a sealed
  fixture `.app`, Windows exercises Rust → PowerShell → fixture installer →
  target native relaunch. A durable external observer proves the new version.
  This is not a claim of production NSIS, Certum, notarization or standard-user
  elevation coverage. Native GUI/WebView control and path isolation must pass
  preflight on each runner; no owner-machine fallback is allowed.
- The first hosted run failed before handoff: Windows had no NSIS compiler,
  and the native fixture omitted the Pet state and shortcut plugin used by the
  production client's auxiliary reconciliation. Fixture setup now registers
  those real dependencies before attaching the client, and Windows provisions
  a hash-pinned portable NSIS archive under the run-owned root before building.
  The controller fixture constructs its real owner synchronously, as `main.py`
  does, before entering the serving loop; constructing it inside that loop had
  suppressed Model Hub reconciliation with nested-`asyncio.run` errors. Hermetic
  assembly/lifecycle tests guard these repairs, but cannot replace another real
  hosted run. Neither platform's handoff is accepted from that failed run.
  The next Windows run built and verified its installer and reached native
  startup, but exposed an observer mismatch between Rust's verbatim Windows
  paths and Python's unprefixed paths. Data-root and target-executable assertions
  now compare filesystem identity, still rejecting absent/different files. The
  hermetic harness tests run on each hosted OS before native compilation.
- Run `43517466466` at `9f6ff81cc` completed macOS explicit handoff/relaunch
  and the normal-duration automatic scenario, including final-edge refusal and
  later replacement. Windows reached the real panel, verified the selected
  target and invoked the production helper, but failed at `Join-Path` before
  replacement. The verbatim staging path was injected by the **Rust fixture**:
  it canonicalizes owned environment directories and sets `TMP`/`TEMP` to those
  paths. The Python launch paths alone did not reveal this. The production
  Windows request builder uses `current_exe()` and `tempfile::tempdir()` without
  canonicalizing them; this fixture failure is not evidence that a normal
  installed user's updater fails. The helper now normalizes drive/UNC verbatim
  request paths before PowerShell path operations, with the same narrow change
  reviewed separately in #3723. Windows replacement/relaunch after that fix is
  still an open native gate. A separate published-install observation (#3724)
  uses the unchanged RC17 app, its default per-user installer destination,
  installed shortcut and default environment, targeting the exact signed latest
  it normally discovers; it does not substitute for this train's new-panel gate.
- The hosted fixture workflow is label- or dispatch-triggered only, never on
  push. A same-repository/master-base PR label is the premerge manual trigger;
  `workflow_dispatch` becomes usable after default-branch registration. Checkout
  the event PR head explicitly and record workflow/merge context separately.
  Reapply the label after a final push. Use read-only permissions, no persisted
  Git credentials, production secrets, release environments or publishing. Do
  not activate while RC/stable Stage 2 is running; recheck that guard before
  expensive native jobs. The guard alone receives the ephemeral job token with
  `actions: read` to avoid the shared hosted-egress anonymous API quota. It uses
  fixed-origin GETs with redirects and proxies disabled, consumes the token
  before spawning its read-only Git check, and never records it in evidence.
  The token is step-scoped, absent from build/native processes; active releases,
  rate limits and unreadable status still fail closed. Do not weaken existing
  CI scope or aggregators.
- Real automatic acceptance uses the unchanged `auto_update_loop`, a fresh
  isolated controller-owned idle producer and normal scheduling durations, not
  a direct tick, remote idle ticket, canned idle time or timer patch. Budget up
  to 240 minutes including builds; test policy invalidation at the final edge.
  All updater network access remains fixture-local; installable payloads and
  fixture signing keys are generated fresh for the run.
- **Stable release gate — published signed RC → RC:** before this train ships
  in a stable release, run one actual Desktop RC-to-RC update through the new
  panel on hosted macOS and Windows using real published, signed RC artifacts
  from public downloads, with no signing secrets. This separately proves the
  production-to-production path. It is not a merge gate and cannot be replaced
  by the fixture handoff. Production packaging/signing/notarization remain in
  RC Stage 2. Never restart or install the owner's app for either acceptance.
- Current-head review and CI, actual regression evidence, Product exact-head
  authorization-policy go and lead merge remain required. No merge authorizes
  deployment or enabling new behavior on the owner's machine.
