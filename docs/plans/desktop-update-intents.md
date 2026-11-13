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

All four target 3.2.7, not the 3.2.6 desktop build. #3682 and #3681 remain held
until the complete train is ready; split review commits are allowed, but no
partial panel release or version-number compatibility branch is allowed.

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
installer boundaries. No production app replacement is implied by fixture tests.

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

## Implementation evidence (check/channel and explicit-click steps)

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
- The trusted updates view remains owned by the UI lane and is required before
  the held panels can enter the frozen merge window.

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
- Native fixture exercise should use disposable GitHub-hosted `macos-26` and
  `windows-latest` desktop-shell jobs with isolated state. A real signed bundle
  replacement is a separately named release acceptance gate on controlled
  disposable machines; never install/restart the owner's app to claim it.
- Current-head review and CI, actual regression evidence, Product exact-head
  authorization-policy go and lead merge remain required. No merge authorizes
  deployment or enabling new behavior on the owner's machine.
