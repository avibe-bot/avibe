# Model Hub native credential takeover

Status: approved product and custody contract. Acceptance requirements and
remaining live-account verification boundaries are described below.

## Approved user flow

- The gateway runtime is enabled by default for new and upgraded installations.
  Starting or installing it does not require an enable-confirmation dialog.
  Runtime enablement is not consent to transfer or delete native credentials.
- Switching a backend from Direct to Hub first offers the existing migration
  dialog when native credentials are present. The dialog selects accounts and
  has one consequence sentence plus **Not now** / **Start migration**.
  Dismissing it leaves the backend and native authentication unchanged.
- The same dialog is available to existing Hub users through **One-click
  migration**. Existing native subscription sources are eligible for custody
  conversion, not suppressed as duplicates.
- With no native credentials to migrate, the mode switch proceeds directly.
  Missing credentials use the existing Add subscription / Add API key flows.
  The service rechecks absence under the shared migration/native-writer guard;
  a login created after the UI scan rejects the mode-only switch. It never
  imports or deletes that new login without migration consent.
- Progress and failures describe only the current state and a necessary action.
  Success means takeover and cleanup completed, not just import accepted.
- Chinese product name: `模型网关`; English: `Model Hub`. The approved Chinese
  consequence sentence is exactly:
  `迁移后，CLI的认证信息将完全交由模型网关管理`

## Public boundary

The existing endpoints and response envelopes remain compatible:

| Endpoint | Request | Successful response data |
| --- | --- | --- |
| `POST /api/models/migration/scan` | none | `scan: { items: MigrationItem[] }` |
| `POST /api/models/migration/apply` | `{ item_ids: string[] }` | `{ applied: number, sources: Source[], added_to: Placement[] }` |

Scan is read-only. It never transfers custody, prompts for browser login,
refreshes tokens, deletes native material, or returns secrets. Item IDs bind
the selected credential revision and destination identity. Applying a stale
selection must not transfer a different account.

`proposed_action: "import"` identifies a supported transfer. Existing legacy
`keep_native`, `controlled_import`, and `reauth` values remain parseable, but
are not successful takeovers and must not be submitted by the migration
confirmation UI. File and OS credential-store support belongs behind the same
native-store interface; Keychain storage alone is not a reason to require a
new browser login. An actual permission or compatibility problem is actionable.

The server owns source placement, backend mode changes, and native cleanup.
The UI must not run a destructive migration followed by a separate mode PATCH
and then present that two-request sequence as atomic.

## Ownership invariants

1. All selected credentials are checked before native ownership is withdrawn.
   Provisioning an OAuth auth file in a live CPA is already an ownership
   transition: CPA may refresh immediately, without an inference request.
2. Stage credentials privately outside CPA's watched auth directory. Quiesce
   native users and verify the native revision has not changed before cleanup.
   Avibe-managed launches must not race the handoff. An external CLI that must
   exit is reported as an actionable blocker, never silently killed.
3. Engine custody, source identity, route placement, and backend mode are
   recoverable together. An existing native source keeps its ID and user-owned
   routes when converted; no duplicate or silently reordered paid source.
4. Before engine activation, rollback can restore an unchanged native snapshot.
   After activation might have rotated a token, never delete the newest grant
   or restore a stale token as if it were an all-or-nothing rollback. Recovery
   must retain the single current owner and finish the handoff.
5. Upload HTTP 200 and protocol inventory are not upstream OAuth validation.
   No completion claim may be based only on them. No new browser login is
   required for a compatible, valid, refresh-capable native grant.
6. Remove only replaced authentication and direct-routing settings. Preserve
   sessions, skills, MCP, unrelated providers, and unrelated configuration
   fields. Recheck all supported precedence layers so an overriding project,
   environment, or legacy setting cannot silently keep direct authentication.
7. A partial cleanup, denied Keychain operation, busy native process, invalid
   grant, or failed persistence is not success. Error messages contain no token
   material. Cancellation must not abandon an ownership transition.

## Acceptance

Use hermetic fixture homes, mocked OS credential stores, and a fake management
server; never develop against real user credentials. Cover API-key and OAuth
takeover, existing-native-source conversion, repeated application, stale scans,
mixed providers, non-ASCII metadata, read/write denial, active CLI users,
cancellation and crash boundaries, and preservation of unrelated settings.
UI checks cover both entry points, dismissal with no mutation, no automatic
migration on runtime start, exact approved copy, locale parity, and truthful
completion/failure states. Real-account refresh and inference remain a separate
explicitly authorized acceptance check.

## Implementation seams

- `EngineAdapter.provision_oauth_credential(source_id, vendor, material) -> str`
  stages only, outside the watched auth directory.
- API-key, transient observation-key, and OAuth provisioning accept the optional
  keyword `on_reserved: Callable[[str], None] | None = None`. The engine atomically
  reserves a fresh opaque ref without secrets, refuses an existing ref before
  invoking the callback, and invokes it synchronously before writing any secret
  bytes. The callback receives only the ref; failure forbids secret writes.
  The owned worker is joined even on repeated cancellation. This is an internal
  write-ahead seam, not a public reservation endpoint or another journal.
- Migration registers `(final_source_id, credential_ref, revoke_credential)` in
  the existing revocation journal; rollback uses that same identity. Transient
  observation keys use their existing `observation` cleanup owner. The callback
  returns only after both the journal file and its directory are fsynced.
  Provisioning failure before returning a ref is still covered by that owner.
  The prepared takeover record must be durable before relinquishing provisional
  ownership. Startup recovers takeover before replaying pending revocations;
  a matching current Source/ref is retained, including a rotated OAuth grant.
- Revocation first confirms the ref is unbound. Even without credential
  metadata, it deletes only that ref's private OAuth stage/reservation and
  fsyncs the affected directory; absent paths are idempotent success. Missing
  metadata never authorizes guessing an auth filename, deleting watched grants,
  management API calls, or sweeping a directory. Cleanup/fsync failure retains
  the pending ref. Unlocatable possibly exposed material stays pending.
- `EngineAdapter.activate_oauth_credential(credential_ref) -> None` publishes
  idempotently after the durable ownership decision; a retry never overwrites
  an existing live (possibly rotated) grant.
- `EngineAdapter.validate_oauth_credential(credential_ref) -> None` requires
  credential-specific upstream acceptance, without inference. The existing
  `observe_source` protocol pin is insufficient for imported grants.
  Validation uses CPA's credential-scoped `/api-call` with a bodyless GET to
  Claude's `/api/oauth/profile` or Codex's `/backend-api/wham/usage`. Only a
  2xx upstream response with the provider's account/usage shape is an
  authentication witness: a nonempty string `account.uuid` for Claude or
  `plan_type` for Codex. Unknown nonempty plan names remain forward-compatible;
  quota/entitlement fields do not decide authentication. Error envelopes,
  malformed payloads and non-2xx responses remain inconclusive. No inference
  endpoint, model catalog, or direct refresh is used. This proves current
  access-token acceptance, not inference entitlement or future refresh success.
- `ModelHubService.migration_guard` is a callable taking
  `tuple[BackendName, ...]` and returning an asynchronous context manager.
  Entering it closes managed native admission, waits for active work (without
  a forced cancellation), strictly retires credential-bearing idle processes,
  and checks for external CLI users. It is acquired before `_mutation_lock`.
  Production installs wire it from the Controller; no production no-op.
  Configured CLI identity is independent of backend enablement: the existing
  auth-service binary resolver supplies raw/compatibility paths and persisted
  paths omitted by disabled compatibility backends. Inventory validates persisted
  evidence for every raw/compatibility shape: unreadable or recovered binary
  configuration cannot fall back to a default identity and certify idleness;
  ordinary Settings probes remain best-effort. Process matching recognizes direct
  executables and script entrypoints behind Node/Bun, Python-family and
  POSIX-shell interpreters. Startup options consume their operands; matching stops
  at the script or an eval/module/stdin mode, never a later prompt argument.
  A lone `-` ends shell options but selects stdin for Python/Node.
  Ambiguous startup syntax containing a possible native executable refuses the
  inventory. No script contents, process environments or arbitrary shell/eval
  code are inspected or executed. Initial admission and idle rechecks use the
  same matcher; this does not promise exclusion of arbitrary uncooperative
  writers or new external launches after the check.
- `ModelHubService.migration_blocked_backends` is a set of backend names that
  must remain blocked after the guard exits if durable takeover recovery is
  incomplete. Completed or fully reverted transactions remove their names.
  Startup recovery restores this set before runtime startup or admission.
- `vibe.native_oauth_store.read_native_oauth(backend, *, home=None,
  allow_secret=False) -> NativeOAuthSnapshot | None` resolves the effective
  native store. A snapshot has `backend`, stable selection `revision`, private
  `payload: dict | None`, `exportable: bool`, and
  `keychain_edit: dict | None`. File-backed payloads may be read by scan;
  secret-bearing OS-store access occurs only with `allow_secret=True`.
  A caller-supplied fixture `home` never accesses the real OS credential store.
  An unsupported store must not fall back to an inactive stale file.
- `vibe.native_oauth_store.apply_keychain_edit(edit: dict, *, reverse=False)`
  compares the live entry against recorded before/after states before changing
  it, preserving unrelated fields. The private edit is journal-serializable;
  scan never serializes it. This function never performs remote logout/revoke.
  A Keychain candidate's selection revision must match between metadata-only
  scan and its secret-bearing apply read.
- `check_keychain_edit(edit, *, applied=False)` checks the exact before/after
  value without mutation. The Controller journals a bundle of keychain
  operations, and folds file operations into one `NativeFileEdit` per path.
  Store helpers never silently roll back: the durable transaction owns recovery.
- A metadata-only Keychain candidate may request consent without claiming its
  value is exportable. The selected container revision is checked after drain;
  only then are its supported API-key and OAuth components expanded. A denied
  read or unsupported component leaves native ownership intact.
  A consented, unchanged container containing only unrelated data is verified
  clean, not an error or an imported Source. Its revision is retained in the
  existing completed receipt; unrelated bytes are not changed. Any new revision
  requires consent again. A selector for a verified-empty Codex store remains
  unchanged so later native logins at that locator remain observable.
  Empty-container evidence never authorizes cleanup of an unimported file
  grant. When the entire batch contains only verified-empty containers, no
  credential is exposed: the existing `withdrawn` phase remains reversible
  through runtime start. A changed container restores the previous mode and
  releases the pending transaction for fresh consent, without restoring or
  deleting any credential bytes.

## Durable transaction

The private `native-takeover/current.json` envelope has `version: 1`,
`phase: prepared | withdrawn | exposed | reverting`, and a nonempty
`backends: string[]`. A pending journal blocks conflicting native auth writers
and selected backend launches across Controller/Web processes. Completed work
writes a sanitized idempotency receipt and removes the pending journal.

The prepared record contains selected public rows, opaque source/credential
identities, previous/target Hub config, legacy native-auth before/after values,
and private native file/store edits. The containing directory is mode 0700;
the record is mode 0600. These snapshots are never response or log payloads.

Prepare, runtime dependency installation, and API-key validation precede
cleanup. Reusing an existing API-key Source/ref still requires current upstream
proof of that exact protocol and target; equality of saved key bytes alone is
not validation. Installation or unsupported-host failures leave native ownership
intact; staged OAuth grants remain outside the watched auth directory even
if dependency installation restarts an existing runtime.
After cleanup, Source/Routes/
backend mode and legacy auth fields are committed without calling the runtime.
The `exposed` marker is durable before activation **or engine projection**.
Recovery explicitly reconciles the projection even when saved config is
already identical. While a prepared transaction still owns provisional refs,
ordinary engine reconciliation must not revoke those refs merely because
they are not yet bound in config. An uncertain prepared-record save outcome
is resolved by durable recovery, not local grant deletion.
OAuth completion additionally requires credential-specific
upstream validation; an invalid or unavailable grant after possible exposure
is never restored as a native login. Unavailable or inconclusive validation
keeps takeover pending. Authoritative refresh-grant rejection instead records
a terminal decision in the exposed journal, marks affected Sources
`needs_action` / `models.source.needs_action.oauth_expired`, and finishes custody
without reporting migration success. The receipt records `outcome: needs_auth`;
apply and identical retries return `migration_credentials_invalid`. The pending
journal and admission block are removed so the existing Hub reauthentication
flow remains usable. Crash recovery replays the terminal decision without
repeating a rejected refresh. An expired access token or generic 401 alone
cannot establish this terminal outcome.

The pinned CPA version does not expose refresh-specific error provenance:
scheduled refresh failures can become `token expired`, while request errors
can populate the same inventory status string. The concrete adapter therefore
does not infer authoritative rejection from that inventory. No runtime upgrade
or heuristic classification is part of this change.

An inconclusive exposed takeover remains retryable. The user's existing,
explicitly acknowledged Hub reauthentication action is also a repair path:
under the same lifecycle/credential guard, persist a terminal decision with
`reason: reauth_requested`, verify native withdrawal and the current Hub refs,
mark the unresolved OAuth Sources as needing sign-in, and finish custody
before starting the existing Hub flow. Keep the engine's current grants;
never restore native material. The receipt records `reauth_requested`, not
success or proof of expiration. Recovery replays this decision after a crash
without another upstream validation or automatically starting a browser flow.
Per-Source successful validation is persisted in the pending journal, so a
verified sibling does not require another sign-in because a later Source fails.

Shutdown joins the owned operation before stopping the runtime. Client
cancellation cannot cancel credential custody. A retry after completion checks
the retained source IDs, opaque credential references, and native inventory
under the lifecycle guard, not IDs alone. If the exact consented native material
has reappeared, a new durable cleanup retains the current Hub credentials;
replaying an old receipt never republishes a stale OAuth snapshot. Changed or
additional native credentials require fresh consent.
Completed receipts also retain metadata-only revisions of verified clean
native stores containing unrelated material. Scans suppress only those exact
placeholders; a changed revision becomes a candidate again. This avoids reading
Keychain secrets on every scan or presenting preserved MCP data as a new login.

### Authentication witness evidence

- Pinned CPA commit `2a6b87aca083a5bf498ac1f68a1b636c500d7aaa`,
  `internal/auth/claude/anthropic_auth.go`: `ProfileURL`,
  `fetchOAuthControlPlaneJSON`, and `FetchOAuthProfile`.
- Official Codex `rust-v0.154.0`, commit
  `6b9826e3aa83b1a5947db50f4332cb9c65f1b340`:
  `codex-rs/backend-client/src/client/rate_limit_resets.rs` and
  `codex-rs/codex-backend-openapi-models/src/models/rate_limit_status_payload.rs`.
  The usage decoder requires `plan_type` and accepts unknown enum names;
  missing quota windows and `allowed: false` do not negate authentication.
- Pinned CPA `internal/api/handlers/management/api_tools.go`: `/api-call`
  substitutes `$TOKEN$` from the selected current engine record. Its outer
  HTTP success is not the inner upstream acceptance.

These source contracts justify the synthetic success fixtures. Live-provider
acceptance, refresh and inference still require separate authorized testing.

## Backend connection ownership

The existing backend connection observation must follow persisted supply mode.
It exposes additive `supply_mode: "direct" | "hub"`; existing `auth`,
`application`, `ready`, and `entry_eligible` fields retain their types.
Direct observations retain native credential behavior. Hub observations use
the configured backend-eligible Sources and their credential ownership, not
the native stores that takeover deliberately cleared. A missing, disabled or
unusable Hub supply must not fall back to an unrelated native login. Existing
application/drain, backend enablement, installation and OpenCode permission
gates remain effective. This is connection readiness, not an inference probe
or a promise that every selected model is available.

Direct Claude retains its existing bounded `auth status --json` observation
for Keychain-backed sign-in; opaque Keychain metadata cannot distinguish an
OAuth grant from an unrelated MCP-only container. The native credential lease
covers that query and its thread completion, with custody checked before
launch. This is not a login, refresh or inference request. Hub connection
observation never launches this query or borrows an unrelated native login.

Only Sources referenced by the backend's effective source order or explicit
route hops participate. Eligibility reuses the existing backend and
`source_runnable(now=...)` rules: an unexpired cooldown temporarily prevents
entry; verification-pending standby Sources remain eligible. No selected model,
inventory or entitlement check is added. Hub-owned credentials require a
current matching private credential record, observed through an engine-owned
read-only boolean helper. This observation neither exposes credential payloads
nor creates directories, repairs permissions, starts CPA or contacts upstream.
A nonempty reference alone is not evidence that its credential still exists.
Retained native subscriptions use the existing native-source readiness check,
only for an explicitly referenced subscription owner of the same backend.
Pending takeover recovery blocks readiness without inventing a new public
`application` state.

Setup and Settings consume `supply_mode` to offer Model Hub management before
asking for a native login or key. Server ownership refusals remain the race-safe
fallback. Opening Model Hub from setup preserves the wizard and all session,
owner and capability authorization checks; returning refreshes connection
observations so the cleared native store cannot strand setup.

## Writer-boundary review decision

The integration audit found the same ownership-boundary class beyond login
flows: recovery backups and whole-document native configuration writers can
reintroduce credentials after a successful handoff. The correction covers
every Avibe writer of those shared documents for the full read-modify-write
duration. Authentication writes also consult persisted Direct/native-Source
ownership after acquiring the lease; stopping the Hub runtime does not hand
custody back. Non-authentication edits may continue under the shared lease.
Claude's interrupted-OAuth settings backup is included in the migration
inventory and cleanup, so constructor recovery cannot restore a replaced key.
This scope preserves legacy native subscription Sources until explicit
takeover, without introducing another permanent ownership marker.

## Compatibility and platform limits

Migration targets ordinary Codex and Claude Code native grants supported by
the pinned CPA runtime. Explicit custom client IDs or grants whose recorded
scopes omit CPA's refresh scopes are blocked before custody. Missing metadata
uses the ordinary native-store contract; validation still requires upstream
acceptance. Extra connector/plugin permissions are not promised to survive a
CPA refresh.

OS-store mutations use the observed item identity and metadata, immediate
compare-before-write, and readback. macOS Security does not provide an atomic
value-CAS against arbitrary external writers. The application-wide lease and
external-CLI drain are therefore required; they do not claim to prevent an
uncooperative process starting or modifying the same item during the native
API's internal mutation window. This limitation is not permission to overwrite
a detected concurrent login.
