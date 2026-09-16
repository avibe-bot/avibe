# Instance Member management parity

Status: authoritative implementation contract, 2026-09-16.
Orchestrator: Avibe Session `sescctptxwqdq`.
Implementation Session: `sesb5c3sb2ayz`.
Baseline: `avibe-bot/avibe` master `1b191200ce0834ee811d478f54255ff0b0cb9819`.

This contract supersedes the initial Model Hub-only scope. Deliver one coherent
PR in the existing task branch/worktree.

## Owner intent and causal model

The owner originally defined Member on 2026-08-19 as having the same Avibe-side
instance settings as Owner, except adding/removing access members and changing
their roles. On 2026-09-16 they reported broken Organization Model Hub settings,
then explicitly requested an audit and one complete fix of the other unintended
Member restrictions.

Example: a remote instance Member opens Models. Navigation admits the user via
`can_manage_instance`, but the mandatory runtime-status HTTP read requires Owner.
The page consequently hides its settings. Config saves repeat the same mismatch
one layer deeper by giving a Member the Editor preference-only write schema.

The cause is competing definitions of instance management: capability projection
says Member; a historical partial HTTP allowlist and individual owner-identity
checks say Owner. Repair that category across real consumers, rather than adding
another isolated route exception.

## Frozen authority boundaries

1. **Instance management** follows the existing `can_manage_instance`,
   `can_manage_agents`, `can_manage_projects` and `can_use_system` capabilities.
   Instance Member and Owner pass. Do not add a role or redundant capability.
2. **Access administration and ownership** remain Owner-only. This includes
   creating/removing/enabling/disabling bound access members, role/admin changes,
   binding codes (including reads that disclose live codes), pairing identity and
   session-signing material, ownership transfer, and Project/Resource access
   grants. Channel binding requirements and explicit principal admission lists
   are access policy, not ordinary routing preferences.
3. **Resource visibility and use** retain the existing Organization Project,
   Agent, session, Show Page and principal scopes. Being an instance manager must
   not turn `is_instance_owner` into true or bypass private content filtering.
   Personal behavior retains its existing rules. Management of visible Projects
   and existing owner-equivalent Agent management must keep working.
4. **Organization roles are independent.** Organization Member/Admin/Owner does
   not substitute for an instance-management grant.
5. **Other roles and authentication remain stable.** Editor/Viewer grants,
   unauthenticated denial, CSRF, secret redaction, protected Vault proofs, and
   Model Hub supply-impact confirmations are unchanged. Unknown management APIs
   remain fail-closed until deliberately classified.

These distinctions are based on effects. Do not replace every owner-identity
check mechanically. In particular `_has_runtime_owner_access` often protects
private records and must not become a general management predicate.

## Audit and required corrections

The baseline main UI module registers 276 `/api` HTTP route/method pairs, with
15 further Memory pairs: 291 total. Of these, 106 are Owner-only; 28 of those
are Model Hub. The other 78 consist of 59 routine management candidates, nine
access/identity operations, and ten mixed-effect operations. These counts include
implicit GET decorators and exclude WebSockets. This is an inventory starting
point, not a claim that all 106 are wrong. Include dynamically registered modules
(especially Memory), non-API authenticated runtime repair, frontend controls and
service-level guards in the completed audit.

| Surface | Required Member behavior | Retained boundary |
| --- | --- | --- |
| Model Hub | Complete status, source/credential/OAuth, gateway lifecycle, catalogs, routes, probes, usage, events and migration management | Existing picker permissions, auth, secret handling and supply-impact guards |
| Backend setup | Inspect/configure/login/logout/test providers; install/restart Agent backends; edit provider models and defaults | No instance access grants are minted by these operations |
| General settings | Save the same ordinary settings and provider/platform credentials as Owner | Access-policy fields and pairing identity cannot change through generic config |
| Messaging | Inspect/connect/configure platforms; discover and enable/disable channels; change routing, cwd and display/mention preferences | Bound-user set/roles, binding requirements and explicit admission lists remain protected |
| Existing DM preferences | Update non-membership settings for existing users | Cannot create/delete/reactivate users, change roles, or overwrite concurrent membership changes |
| Runtime and system | Service lifecycle, UI reload, upgrade, dependency installation, diagnostics/logs, directory browsing/creation | Validation uses test doubles; no actual production operations |
| Remote access operation | Status, diagnostics, optimization, start/stop and transport tuning of the current pairing | No re-pair, identity replacement, secret replacement, or ownership change |
| Workbench | Save ordinary preferences and repair the runtime of an accessible Show Page | Private resource/session visibility remains enforced |
| Vault | Edit metadata/tags/policy/classification, rotate and delete secrets through existing manager paths | Protected operations still require the original cryptographic authorization; access grants and private principal data are not bypassed |
| Agents/Projects/Skills/Memory/Harness | Verify existing advertised management capabilities reach real operations; fix further demonstrated settings mismatches | Preserve Project/Agent use ACL, Memory user identity and managed-mode restrictions |

Two effect-based exceptions already identified must be recorded explicitly:

- Legacy bulk Agent onboarding claims previously ownerless resources under the
  caller's ownership. It is ownership migration; keep its Owner gate and existing
  hidden Member UI rather than changing resource ownership as a side effect of
  this repair. Ordinary Agent CRUD/import/default selection remain Member.
- The existing WeChat QR completion flow automatically binds a user and may
  appoint an admin. Preserve Owner authorization for this combined binding flow;
  direct platform credential settings are instance management. Make the UI
  reflect this restriction rather than offering an action that inevitably fails.

## Mixed-write invariant

Do not choose between blocking a whole settings page and admitting its embedded
access-control writes. The invariant is:

> A Member settings mutation may change ordinary configuration but must preserve
> the authoritative access-member set, roles and explicit access policy.

Apply validation where the current state and mutation are committed together.
Reuse existing config locks/settings transactions. Refuse unauthorized changes
with an actionable existing error contract; do not silently strip requested
changes and report success. Preserve normal field-scoped UI writes and legacy
shapes where safe. Equal protected-field echoes may be accepted only after
comparison with the fresh authoritative value.

Audit all input shapes reaching that writer: partial updates, full replacements,
list operations, legacy aliases, defaults/omission, inherited thread settings and
deletion. An omitted protected field cannot reset it, and deleting a thread
override cannot relax its effective binding policy. A concurrent user removal
cannot be undone by a stale DM-preference save. Protect the membership effect,
not just one JSON spelling of it.

Candidate state must remain private to the request until authorized and committed.
The process-wide SettingsStore must never expose an unvalidated Member mutation
to a concurrent Owner/internal save. A SQLite comparison alone is insufficient
when another writer can commit that shared candidate first. Cover an interleaving
where Member attempts a role/admission change while Owner saves unrelated ordinary
settings; neither the Owner write nor a later Member comparison may authorize it.
Reuse the existing settings writer and transaction rather than adding another
permissions or persistence model.

Runtime semantics govern that comparison: `core/auth.py` checks the selected
channel/thread record's explicit `require_bind`. Channel `None`/`False` means
unrestricted by binding; platform defaults only seed new configuration. A thread
without its own record falls back to its parent channel. Do not introduce global
runtime inheritance based on the stale platform-config comment. Channel
`enabled` is an operational switch and remains Member-writable; bound-user
`enabled` controls member admission and remains Owner-only. The existing
`core/chat_discovery.py::delete_scope` writer may receive a narrow same-transaction
policy-preservation check, including descendant settings it removes.

Generic `/api/config` must keep pairing/session identity protected; the dedicated
remote-access settings route already owns safe transport tuning. Preserve
provider credential redaction on responses. Frontend controls for retained
Owner-only actions must use `can_manage_access_members` or the appropriate true
ownership condition, while ordinary settings use management capabilities.

## Implementation approach and scope

- Evolve the existing central HTTP/capability policy. Express each management
  surface coherently and cover the complete registered route inventory in tests;
  do not leave a manually curated subset as the definition of completeness.
- Allowed production files: `vibe/authorization.py`, `vibe/ui_server.py`,
  `vibe/api.py`, `vibe/ui_memory_routes.py` if a real mismatch is found,
  `storage/vault_service.py`, existing settings/config service modules needed for
  atomic mixed-write validation, and narrow related frontend settings/control
  files. Existing other service guards may be changed only for demonstrated
  management mismatch and with an audit entry explaining the effect.
- Allowed tests/docs: focused authorization/config/settings/Vault/remote-access
  and corresponding UI/scenario tests; this contract, the prior Member plan,
  relevant Model Hub contract wording and concise user documentation.
- Do not modify resource-use ACL semantics, the backend control-plane repo,
  unrelated runtime features, dependency versions or deployment state.
- No visual redesign is necessary. Prefer capability binding and existing
  disabled/read-only patterns and localized copy for retained restrictions.

## Validation and delivery gate

1. Audit every registered API route/method and every remaining relevant
   owner-identity management guard. Assign it a capability or a documented
   retained boundary; newly unclassified management surfaces must fail a test.
2. Compare every existing role against the invariant, with both Personal and
   Organization identities. Seed private/foreign/missing-policy resources to
   show that settings parity does not change visibility or use grants.
3. Exercise real signed remote HTTP session + CSRF paths through representative
   workflows from each changed category. Stub only external providers, host
   operations and runtime IPC, after authorization. Verify writes and read-back,
   not merely a mocked policy result or a non-403 status.
4. For mixed writers, cover omissions, aliases, replacement/deletion and a stale
   concurrent membership update. Denial must happen before any protected effect.
5. Include the reported Models bootstrap scenario and preserve all Model Hub
   role/registered-route coverage from the initial work.
6. Run focused tests, Ruff on changed Python, focused UI tests and the production
   UI build when frontend changes. Update relevant scenario catalogs where an
   affected workflow already has one. Use hermetic test-owned state only.
7. Open one non-draft PR whose title/body describe final Member management parity.
   Follow `.agents/skills/pr-delivery-loop/SKILL.md`, obtain exact-head Codex pass,
   all required CI green and zero unresolved threads. Enforce the review circuit
   breaker before further patches; report full head/class inventory to the
   orchestrator. Keep one lane watch and one independent orchestrator gate watch.
8. Do not merge, install, restart live services or deploy without explicit owner
   instruction. Deliver the PR, category-level change matrix, evidence and any
   genuine unresolved product boundary before close-out.

## Completed boundary inventory

The live registered router has 291 distinct API method/path pairs, including
Memory and the constant-decorated Model Service notification. Fifteen pairs
retain the Owner default: access-member/role and binding-code administration,
Project/Resource ACL writes, pairing, bulk Agent ownership onboarding, WeChat
QR binding, and the internal `POST /api/model-service/refresh` notification.
That last endpoint additionally requires the existing same-machine CLI token;
a normal Owner session is insufficient. It is not a settings operation.

The registered-route test independently names these retained boundaries and
checks every registered route, rather than deriving completeness from the
management allowlist. Memory retains its signed principal and trusted-origin
gate; Skills and Harness retain their existing Editor surface. Resource-use
and private runtime-record identity checks remain separate from management.

Mixed writes prepare request-private SettingsStore candidates, then compare
normalized config under the config lock and fresh settings under the SQLite
write lock. Unvalidated candidates never enter the process-wide cache, so an
overlapping Owner/controller save cannot commit a Member's forbidden draft.
Real thread/event tests cover each mixed writer with an overlapping save. The settings check includes legacy Discord policy
until migration, effective channel/thread binding, descendant deletion, and stale
DM updates after member removal or role changes. Normal channel enabled state
and DM routing/display/cwd remain management.

Automated evidence: PERMISSIONS-013 covers signed Member management and guarded
mixed writes; AUTH-SETUP-301 saves and reads a native backend credential in a
test-owned home. Model Hub conformance covers all documented/registered endpoints
through signed remote roles. UI controls and accessible Show Runtime repair have
focused role tests. All providers, host lifecycle operations and runtime IPC are
stubbed; this evidence does not claim a live deployment or tenant verification.

Known by design: validated active remote Organization Editor-or-higher sessions
already use Skills/Vault through the existing capability fast path. This change
does not add that behavior or alter direct-context/private resource-use checks;
protected secret use still needs its existing approval and cryptographic proof.
