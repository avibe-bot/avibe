# Host-owned update intents (3.2.7)

## Outcome and ownership

Every update panel asks the selected host's existing update owner to check,
change its preview channel, or install one exact target. A local Desktop footer
still owns the local app even while its client selects a remote Runtime.
Standalone Runtime updates remain Python-owned; Desktop-managed Runtime updates
remain native-owned. No cloud installer, duplicate channel preference or new
user setting is introduced.

The shared read model from #3676 is the base: Runtime `current` remains the
post-restart version proof, while `desktop_version` is the native app's identity.
The UI lane owns the shared panel/adapters and localized copy. This lane owns
Runtime intents, native execution, additive capabilities and status receipts.

## Ordered changes

1. **Check/channel intents**: additive capabilities, bounded native control
   mailbox, honest in-flight/result relay and owner-agnostic HTTP actions. The
   existing install mailbox, its receipt and host-confirmation policy do not
   change in this step.
2. **Explicit update-click authorization**: the authenticated instance manager's
   Update action authorizes that one installation. Remove the new shell's
   remote-install preference and second host confirmation, without weakening
   target binding, expiry, busy ownership, signature verification, settlement or
   rollback. Advertise `install` only when this behavior exists. This is a
   separate exact-head approval boundary.

Both target 3.2.7, not the 3.2.6 desktop build. Do not publish a UI capability
before its native implementation or make a consuming PR depend on an unmerged
contract.

## Additive surface contract

- `/api/version` and `vibe check-update --json` continue to share
  `desktop_update_snapshot`. Add `host_actions: string[]` with recognized values
  `check`, `prereleases`, and later `install`; absent/malformed lists mean legacy
  behavior. `install` promises explicit-request installation without
  `awaiting_host`.
- Preserve the last completed native check while another check runs. Relay a
  separate bounded control receipt and expose effective `phase: checking` for
  that active check, without erasing the previous verified offer or changing the
  legacy raw check record enum. Expired/interrupted work cannot leave an eternal
  spinner.
- Native IPC/account projection gets `hostActions` and `checkedAt` from the same
  native owner and recorded check, not the selected remote Runtime.
- `POST /api/update/check` and
  `POST /api/update/prereleases {"enabled": boolean}` are instance-management
  actions under existing origin, CSRF and authorization checks. Desktop-managed
  hosts enqueue a native control intent; standalone hosts use the existing
  checker/config preference. They return `{ok, request_id?, error?}` and clients
  refresh `/api/version` until the matching receipt settles. Standalone replies
  also include the synchronous check `snapshot`. Existing CLI JSON remains a
  read-only relay for Desktop; no new CLI flags are part of this HTTP contract.
- Install remains `POST /api/desktop/update/request` with the exact target and
  existing interruption acknowledgement contract. `awaiting_host` stays in the
  response vocabulary for legacy shells.

## Control intent transport and lifecycle

Keep the legacy strict install request and receipt intact. A separate
`desktop-update-control.json`, beside the existing desktop state, carries only
check/channel commands; it can never be interpreted as an install by an old
shell. Reuse the native local Runtime's existing SSE nudge and periodic mailbox
poll. New control intents share the updater's busy gate with installation and
local checks.

A control request has schema version, canonical UUID, action (`check` or
`prereleases`), an `enabled` boolean only for the latter, pending/expired state,
and created/expiry timestamps. Bound both file size and lifetime. A receipt
binds the same UUID/action/value and records checking/completed/failed/expired,
error code and timestamps. Same pending intent retries reuse identity; different
live control/install requests return busy. Completed receipts suppress mailbox
replay. API ack timeout expires only its own request; native revalidates the
current matching pending file before changing a channel and before publishing a
result. Native check I/O has a bounded timeout; expiry never releases another
operation's lock.

Only the native shell writes its channel file. Standalone preference changes
use `update_config_fields` under the existing config transaction lock, never a
full config overwrite. A requested check never installs or launches a host
confirmation prompt. Readiness/error and result timestamps belong to the owner.

On native restart, an unfinished control receipt becomes interrupted; a stale
request must not be replayed as a fresh channel change. Native status additions
are optional on read, so old Runtime readers retain existing results. New
Runtime never sends control commands without capability support. A stale
capability snapshot can at worst produce an unavailable/expired request, not an
install or legacy-file mutation.

## Authorization-policy follow-up

The owner removed the remote-install setting, not installation validation.
Keep request identity/target binding, signed fresh discovery, expiry/busy checks,
exactly-once settlement and old-app preservation. Preserve old preference data
for a downgraded/older shell rather than overwriting it. A new UI must keep an
honest fallback when an older shell reports awaiting_host.

The current API's instance-management role covers owner/member and rejects
editor/viewer. Keep exactly that rule for check, channel and explicit install:
`can_manage_instance` is the authorization boundary. The owner confirmed that
"signed-in owner" means the person who manages this instance, not a new
owner-only role distinction. Removing the host prompt never grants access to
editor/viewer or bypasses origin/CSRF protection.

## Implementation evidence (check/channel step)

- 331 focused Python tests pass (control/status/request, HTTP actions, version
  identity, manager authorization); 106 existing updater/platform tests pass,
  with four pre-existing platform/fixture skips.
- Rust: 11 control/status tests, 36 native updater tests, 12 signed updater
  fixture tests pass; changed crates pass Clippy with warnings denied.
- A credential-free local Incus `worktree` instance exercised the running HTTP
  server: fresh source-owner check, preview on/off, unchanged unrelated config,
  restored original channel, and CSRF/origin rejection. Initial evidence is on
  the uncommitted source; repeat against the submitted head before Merge-ready.
- Disposable macOS/Windows CI and exact-head Codex review are still gates, not
  claimed results. No signed application replacement has been exercised in this
  step; that belongs to the separate install-policy acceptance.

## Validation and acceptance gates

- Pure Python/Rust fixtures: malformed/oversized/expired/future/unknown-schema
  requests, exact booleans, UUID/action matching, duplicate retries, conflicting
  intents, install/control exclusion, stale receipts, restart interruption,
  failed publication and channel writes, check timeout and no implicit install.
- API: real role matrix, CSRF/origin, standalone transaction preservation,
  legacy capability rejection, ack timeout/expiry, new-native receipt relay,
  same snapshot from CLI and Web.
- Cross-version: old/new Runtime and shell combinations, legacy install receipt
  untouched, capability absence/staleness, persisted channel rollback behavior.
- Native/local selection: footer always local, host intent never follows an
  unrelated selected Runtime; preserve generation/instance fencing from
  #3653/#3661.
- Runtime end-to-end runs only in a task-owned local Incus worktree environment,
  not the shared master environment or any customer/owner host.
- Native fixture exercise should use disposable GitHub-hosted `macos-26` and
  `windows-latest` desktop-shell jobs with isolated state. A real signed bundle
  replacement is a separately named release acceptance gate on controlled
  disposable machines; never install/restart the owner's app to claim it.
- Current-head review and CI, actual regression evidence, Product exact-head
  authorization-policy go and lead merge remain required. No merge authorizes
  deployment or enabling new behavior on the owner's machine.
