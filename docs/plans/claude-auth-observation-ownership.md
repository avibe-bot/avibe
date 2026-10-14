# Claude authentication: observation, operation, and execution ownership

Owner decision: 2026-10-14, session `ses7vjeb43k5u`. The user approved this
fundamental correction after rejecting the patch loop in #2550/#2599. Implement
from master, not by extending either unmerged implementation.

## Product contract

A passive read must not perform authentication, and must not pretend to predict
Claude's eventual credential selection. Three facts have different authorities:

| Fact | Authority | Meaning |
| --- | --- | --- |
| Saved configuration / native material observed | read-only Settings inventory | a local fact, not effective auth or token validity |
| Explicit authorization completed | the owning login flow | this operation committed, not perpetual readiness |
| Backend can start / this execution succeeded | runtime/application and actual execution | launch capability / a result in that execution's context |

Direct Claude delegates authentication to Claude. Avibe still owns the chosen
Agent/model and runtime lifecycle. Missing or opaque passive credential evidence
must neither claim signed out nor prevent an explicitly requested execution.

Gateway-held Model Hub Sources retain their existing custody and admission
checks. Existing `native_cli` subscription records are a compatibility boundary:
do not migrate/delete/reclassify them or loosen their routing rules implicitly.
Their explicit login completion must consume the owning operation's verification,
not call the generic Settings inventory to infer authentication a second time.
Their UI must identify CLI-managed credentials rather than promise Hub custody.
An independent redesign of per-turn native-Source admission is out of scope.

## Concrete interfaces

### Claude Settings inventory (`get_claude_auth`)

- Remove the passive Claude subprocess probe, its cwd creation and its env helper.
- No Git traversal, user/project/local precedence emulator, dynamic-helper
  execution, remote validation, background probing or validity cache.
- Preserve editable user-settings / legacy V2 API-key metadata and existing
  save/remove writers. These fields describe what the editor manages only.
- Read `read_native_oauth("claude", allow_secret=False)` for native-store evidence.
- Add `auth_managed_by: "claude"` and
  `oauth_store_state: "present" | "absent" | "unknown"`.
  `present` means inspectable native OAuth material, not logged in; opaque,
  denied, invalid or unreadable evidence is `unknown`.
- Keep `has_oauth_credentials` as the compatibility raw-presence boolean
  (`oauth_store_state == "present"`). Add `can_remove_oauth_credentials` for
  offering explicit scoped native cleanup independently of sign-in certainty.
- Retain the legacy `active_auth_mode` wire field conservatively as `"none"`
  with `auth_mode_uncertain: true` for this delegated-auth observation. New
  consumers must not use it as an authentication or configuration-save verdict.
  `auth_mode` continues to describe the saved editing choice, not runtime truth.

### Direct Claude connection (`get_backend_connection`)

- Add `"external"` to the existing `auth` enum, used for Direct Claude.
- `ready` means the installed/enabled backend has applied configuration and can
  accept an execution attempt; `entry_eligible` retains the existing applied /
  stopped distinction. Neither field asserts authentication for `auth=external`.
- Preserve configuration-recovery, custody/takeover, pending migration, disabled,
  CLI-installed and application/draining/failure gates. Unknown auth is not a
  reason to bypass those gates.
- Do not inspect native credentials to grant Direct launch capability. Keep the
  existing coherent ownership check/exclusion; remove only the auth inference.
- Do not relax gateway-held Source admission or change per-turn router code to
  make a default-directory observation look like turn-specific authorization.

### Explicit login receipt

- The existing `WebAuthFlow` owns additive `authorization_completed: bool`,
  default false. Set it only after verified consent and native/config commit,
  before the existing post-success runtime-application hook. A later apply
  failure must not erase the authorization fact. Preserve cleanup, cancellation,
  leases, timeouts and flow retention; do not introduce another lifecycle.
- Publish the receipt on start/status responses where that flow is serialized.
- Model Hub Claude completion uses verification captured by that owning flow
  (existing `source_signed_in` / `source_status` may carry it). Do not call
  `get_claude_auth` or launch a second status process to re-decide completion.
- Preserve the legacy native subscription's first-party verification contract:
  a concrete API-key/other-provider result is not proof of OAuth. If needed,
  move the existing pure CLI-response interpretation into the explicit verifier
  and reuse its existing configured-cwd helper, not a filesystem interpreter.
  Do not unconditionally stamp a source ready from a passive read or an error.

### UI consumers and cleanup

- Claude Settings edits saved configuration and presents inventory as observed,
  absent or unknown. No green signed-in/connected claim from file presence.
- `auth=external` is presented as native authentication / ready to start, not
  authenticated. An authorization receipt is presented as authorization complete;
  pending/failed application is shown independently and may be refreshed without
  re-authentication. A failed attempt without a receipt cannot be rescued by old
  saved credentials or by a later generic read.
- API-key save confirmation checks the editor's owned saved fields, not guessed
  effective CLI auth. Other backends' existing behavior remains unchanged.
- Claude cleanup is explicitly 'clear saved native account credentials', not
  universal sign-out. Use the existing scoped cleanup endpoint; do not inspect,
  remove or pretend to revoke environment/project tokens. Its success copy states
  the scope instead of promising no future authentication source exists.
- Async reads/receipts must be invalidated on method changes, new auth actions,
  removal, cancellation and unmount; late responses cannot consume newer drafts
  or close onboarding. Reuse the existing observation/receipt owner, not a new
  global state machine.
- Align English/Chinese copy and types. Explain CLI-managed vs gateway-held
  custody using existing surfaces; preserve stored Sources and user names.

## Acceptance evidence

1. Opening Settings / reading connection state spawns no Claude, writes no
   credentials and creates no workdir. Real test-owned files + fake Keychain;
   no-spawn regression fails on master for the intended reason.
2. Direct Claude is eligible to attempt execution with absent, opaque or
   unreadable auth evidence when its capability/ownership gates pass. The same
   cases remain blocked for disabled/uninstalled/unapplied/pending takeover.
3. A verified explicit flow completes exactly once using its own receipt,
   including delayed cleanup and application failure/recovery; no second probe.
4. Settings never says signed out/connected based on passive uncertainty. Saved
   API-key edits still round-trip; opaque native cleanup remains available and
   never promises removal of external tokens.
5. Existing managed Hub custody/admission remains unchanged. A concrete competing
   auth method from explicit native verification cannot become a verified OAuth
   Source; no second Settings reader is used to reach this decision.
6. Register multi-step evidence under the existing auth_setup catalog with all
   executable cases in `test` (list form when necessary), plus focused UI tests.
7. Changed-file Ruff, relevant Python/UI tests, UI typecheck/build, exact-head
   Codex review, all matching required CI workflows and zero unresolved threads
   gate merge. No deployment, service restart, real OAuth or live Keychain reads.

## Delivery boundaries

- No dependency on or merge authorization for the separate Skills PR #2547.
- #2550 is closed; #2599 remains draft with its branch preserved until a new
  implementation PR supplies the superseding link. Do not resume its old merge
  watches as authorization to merge that implementation.
- No new persisted credential/Source schema, automatic credential migration,
  workdir policy, external-auth parser, recovery journal or process supervisor.
- If implementation requires violating these boundaries, report the concrete
  conflict to the owner before broadening it. Review comments are evidence to
  assess against this contract, not permission to reinvent the rejected design.
