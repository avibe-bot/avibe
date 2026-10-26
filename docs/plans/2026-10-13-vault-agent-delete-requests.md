# Agent-initiated Vault delete requests

Date: 2026-10-13
Owner/orchestrator: Session `sesuhtvtvuquq`
Implementation lane: Session `seskurxegju5h`
Branch: `feat/vault-agent-delete-requests` (base `748be1c30`)

## Background and goal

An Agent may ask to delete a Vault secret on the user's behalf. The user approves
in Vaults or the Chat approval card; approval performs deletion. Today `vibe vault
rm NAME` immediately deletes a standard secret and refuses protected secrets.
Reuse the request queue, approval surfaces, notification delivery, and callbacks.

## Change contract

1. Every agent-initiated delete is a request, for standard/protected and
   static/keypair secrets. `vibe vault remove NAME [--reason TEXT] [--no-callback]`
   creates a pending `delete` request and returns its ID immediately, never
   deleting and with no bypass flag. Default value-free guidance:
   "Waiting for the user to approve deleting NAME in Avibe. Avibe resumes this
   Session after the decision; do not rerun rm." Explicit waiting remains
   available through `vibe vault await <request_id>`. Orchestrator clarification
   (2026-10-13): with `--no-callback` or no resolvable requesting Session, give
   truthful `await` guidance instead of claiming an automatic resume. Global
   dedupe preserves the original request's provenance/callback; when a reused
   request belongs to another Session or has callbacks disabled, `rm` gives the
   same truthful `await` guidance rather than promising a new callback. Reusing
   a same-Session callback-enabled row still reports automatic resume, even when
   the repeat invocation passes `--no-callback`; stored callback settings win.
2. Persist `request_type="delete"`, `secret_name=NAME`, and the existing
   requester/session provenance. `delivery={reason, card}`; card fields are
   `{card_type: "approval", request_type: "delete", secret_name, kind,
   protection, description, tags, session_id, reason}`, without values or unlock
   material. Use the default request TTL, require an existing secret and the
   resource-access/session-authority checks of `create_sign_request`, reuse an
   existing pending delete for the secret, and audit `delete_requested`.
3. `POST /api/vault/requests/{id}/approve-delete` requires the same management
   permission as direct `DELETE /api/vault/secrets/{name}`. In one transaction,
   claim the pending request, delete the secret (expire grants/release resident
   avault scopes as today), and mark `approved`, `callback_status="pending"`.
   Publish the normal Vault SSE update afterwards. Expired/already-decided
   requests fail cleanly using existing error codes.
4. Denial reuses `deny_request` and its endpoint unchanged.
5. A pending delete whose secret is deleted another way settles `fulfilled`, not
   `expired`, and its callback says the secret has been deleted. Apply the normal
   lazy-expiry transition first: overdue requests stay `expired`. Other pending
   request types retain today's expiry behavior.
6. Approved/fulfilled callback:
   "The user approved deleting the secret 'NAME'; it has been deleted. Continue
   the task." Denied callback: "The user declined deleting the secret 'NAME'.
   Do not retry — keep the secret and ask the user if needed." Expiry retains
   today's wording.
7. The Chat approval card renders delete requests with name, kind/protection
   badges, description/reason, requesting Session, destructive **Delete**, and
   **Deny**. Static standard deletes need one click. Keypair or protected secrets
   retain the existing five-second hold and warning copy (reuse `ConfirmDialog`
   or extract its button; no duplicate hold mechanism). `vaultRequestType` and
   `isVaultApprovalRequest` include delete; `vaultApprovalNeedsPasskey` is false.
   The Vaults inbox uses a "Delete request" badge, Review opens the same card,
   and success toast is "Deleted NAME". Check the desktop mini rendering and
   reuse its shared component, or report a separate boundary. All UI strings
   belong in English and Chinese i18n.
8. IM notifications include `delete`, with backend `type.delete` i18n and the
   existing Vaults deep link. Approval remains on the Web.
9. Update `skills/use-avibe-vault/SKILL.md`, bump its version, and document
   `vibe vault remove NAME --reason "..."` as an approval request that resumes the
   Session after the decision; never rerun it. CLI examples must parse.
10. Out of scope: single-field deletion, changing the Web direct-delete flow,
    bulk deletion, and the parallel multi-field feature.

Any deviation requires orchestrator approval before implementation.

## Boundaries and validation evidence

Tests are hermetic, using only test-owned state and synthetic envelopes; no live
Avibe service, real home/keychain, or external tenant mutation. Tests run through
`~/.local/bin/hd1-test`; no full local suite.

- Service: creation deduplication, missing secret, ACL/session authority;
  approval deletes/invalidates grants and produces callback; denial/expiry;
  deletion elsewhere fulfills. These own the new lifecycle, absent from the
  existing access/sign/provision coverage.
- CLI: parser and `rm` outputs for both tiers; the actual service records a
  request while secret bytes remain untouched. Existing tests cover immediate
  removal/protected rejection rather than approval-only semantics.
- API: management permission, state errors, transaction, SSE and resident scope
  release. Service tests cannot cover HTTP permission/routing or post-commit IPC.
- UI: shared card per tier, hold only for keypair/protected, approve/deny calls,
  inbox inclusion and Chat placement; existing filters reject delete.
- IM: real notification type projection and deep link.
- Focused pytest, changed-Python Ruff, focused Vitest, and `ui/npm run build`.
- Self-review the complete base diff. Open one non-draft PR against `master`;
  exact-head Codex pass, all required CI green, zero unresolved threads, one
  durable combined Watch; never merge.

## Multi-field integration (approved 2026-10-14)

- Integrate the released multi-field implementation and Vaults UX from master;
  preserve whole-secret deletion, grant invalidation, and resident-scope release.
- Reject `vibe vault remove NAME.field` with `vault_field_delete_forbidden` and point
  to the Vaults page for single-field removal. The skill is version `0.4.0`.
- A delete card snapshots `_field_metadata(secret)` at creation. Pending UI reads
  refresh its `kind`, `default_field`, `fields`, and optional `fields_state` from
  the current secret, without envelopes or grant options. Deleted-secret reads
  retain the snapshot. The shared approval card displays all named fields from
  these card fields; access cards keep their existing grant-member grouping.
- Register the new approve-delete endpoint natively in FastAPI. Its async handler
  awaits the synchronous API/DB work on a thread, with the same authorization
  context and request hooks as the adjacent field routes. Existing deny/list
  endpoints are not added by this PR and stay unchanged.
- Review and Deny require Vault-use permission; Delete requires both Vault-use
  and instance-management permission. English agent-facing callback/CLI copy
  matches existing Vault guidance; Web and IM copy is localized.

Integration validation is complete locally: 684 tests across the full Vault
Python files plus resource ACL and member-management parity, 149 focused UI
cases, UI typecheck, lint, build, changed-file Ruff, skill validation, and strict
duplicate-key/en-zh parity checks. The payload, UI field-list, and native route
regressions were run against `be12be7` production files and failed for the intended
missing fields/compat registration reasons; they pass with the fixes.

## Delivery slices

- [x] Record the contract as the first commit.
- [x] Service lifecycle, API/CLI, notifications, owning-boundary tests.
- [x] Shared UI, i18n, skill guidance, focused UI/parser tests.
- [ ] Final integrated-head Codex/CI loop and report to orchestrator.

The PR Known-by-design ledger records that all Agent deletes need approval, the
keypair/protected hold is retained, and single-field deletion is out of scope.
Global per-secret request dedupe intentionally retains the first requester/session
and callback; a later caller receives explicit `vibe vault await` guidance when
that callback cannot resume the later Session. The desktop mini reuses its
pending-request deep link to Vaults rather than adding a second inline approval
surface.
