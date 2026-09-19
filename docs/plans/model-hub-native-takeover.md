# Model Hub native credential takeover

Status: approved product contract; implementation and safety verification in
progress. The implementation checkpoint is not release-ready.

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
- `EngineAdapter.activate_oauth_credential(credential_ref) -> None` publishes
  idempotently after the durable ownership decision; a retry never overwrites
  an existing live (possibly rotated) grant.
- `EngineAdapter.validate_oauth_credential(credential_ref) -> None` requires
  credential-specific upstream acceptance, without inference. The existing
  `observe_source` protocol pin is insufficient for imported grants.
- `ModelHubService.migration_guard` is a callable taking
  `tuple[BackendName, ...]` and returning an asynchronous context manager.
  Entering it closes managed native admission, waits for active work (without
  a forced cancellation), strictly retires credential-bearing idle processes,
  and checks for external CLI users. It is acquired before `_mutation_lock`.
  Production installs wire it from the Controller; no production no-op.
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

Prepare and API-key validation precede cleanup. After cleanup, Source/Routes/
backend mode and legacy auth fields are committed without calling the runtime.
The `exposed` marker is durable before activation **or engine projection**.
Recovery explicitly reconciles the projection even when saved config is
already identical. OAuth completion additionally requires credential-specific
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

Shutdown joins the owned operation before stopping the runtime. Client
cancellation cannot cancel credential custody. A retry after completion checks
the retained source IDs, opaque credential references, and native inventory
under the lifecycle guard, not IDs alone. If the exact consented native material
has reappeared, a new durable cleanup retains the current Hub credentials;
replaying an old receipt never republishes a stale OAuth snapshot. Changed or
additional native credentials require fresh consent.

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
