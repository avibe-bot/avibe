# Editor permission audit and parity repair

Status: approved bounded repair contract, audit and implementation in progress.
Date: 2026-09-18. Orchestrator: `sesnps2m743r5`.
Baseline: `4019b704c99afe16223d475fccd9e1cb94a109d7` (includes Member PR #2030).
Implementation branch: `fix/editor-permission-parity`.

## Outcome and authority

The owner requested a complete Editor permission check and repair of confirmed
inconsistencies after discovering that the Agents detail Run button is hidden
from an otherwise authorized Editor. Deliver one reviewed, CI-green PR. Prior
permission to merge #2030 does not authorize this PR's merge or any deployment.
Do not change the dirty primary checkout or running local/tenant services.

Example: an Organization Instance Editor with use access to an enabled Agent and
Editor access to an active Project can start a chat with that Agent in that
Project. The Agents page currently wraps Run and its dialog in
`can_manage_agents`, preventing that operation even though the backend accepts
it. Running an Agent is a use operation; editing its definition is management.

## Frozen behavioral boundaries

- Reuse the existing capabilities and effective resource/session permissions.
  Editor may use authorized Agents, create and operate authorized Project
  sessions, and use other existing Editor capabilities. A stronger management
  gate must not block an operation already granted by the intended policy.
- Preserve Personal versus Organization semantics. Organization Agent ACLs,
  restricted Project bindings and effective Viewer downgrades still apply.
  No-policy Organization Agents remain unavailable to Editor. Personal Editor
  Agent/Project bypasses stay as shipped. A denied/missing/disabled resource
  remains denied by the authoritative service even if client state is stale.
- Preserve Editor's standalone/non-Project session boundary, archived-session
  lifecycle, missing-resource behavior and existing execution admission checks.
  This is policy/consumer consistency work, not the deferred four-role redesign.
- Editor does not gain instance configuration, Agent definition management,
  Project administration, model-provider management, access/ACL administration,
  pairing, binding-code, identity, ownership or bulk onboarding authority.
  Preserve existing exceptions actually granted to Editor (such as current
  Show publication semantics) rather than redefining them from naming alone.
- Member/Owner behavior from #2030, Viewer restrictions, Memory principals,
  terminal identity, Web Push recipients, protected Vault proof, IM admin,
  shared read_at and Show sharing/publication remain unchanged.
- Unknown HTTP routes/events remain deliberately classified and fail closed;
  do not use a broad editor bypass or loosen server policy to match a broken UI.
- Existing appearance and controls are reused; no layout redesign or new product
  flow. Grant/revoke transitions must leave valid controls usable and forbidden
  actions disabled/hidden, including already-open dialogs and callbacks.

## Audit scope and disposition

Cover all Editor-related frontend capability/raw-role gates and their actual
API consumers; navigation, Agents/use/selection, Projects/session creation,
chat send/queue/retry/cancel/fork/archive/settings, file/terminal tools, Skills,
Vault use, Memory, Harness Tasks/Watches/Runs, Show, inbox/search/notifications,
SSE/WebSocket, diagnostics and settings. Review server route tiers and relevant
resource/service/consumer checks for both excess and missing authority.

Record each surface as: intended operation; capability and resource checks;
actual HTTP method/path or execution consumer; evidence; disposition (correct,
confirmed bug, retained boundary, or unresolved with specific missing evidence).
A hidden button, a 403, or a role name alone does not establish a defect. Trace
both the producer and consuming effect. Consolidate same-cause findings.

## Work ownership and allowed changes

One implementation lane owns ALL product/test changes and final audit/contract
updates. An independent backend audit lane is read-only for production/tests
and writes only its own audit report in its isolated workspace. Reports go to
PM, who classifies findings and forwards approved concrete corrections; auditors
never edit the implementation worktree or independently open a competing PR.

Implementation scope: authorization gates and the directly coupled call path in
`ui/src/components/`, `ui/src/context/`, `ui/src/lib/`, `ui/src/App.tsx`, focused
existing UI tests/i18n; narrow traced permission defects in `vibe/authorization.py`,
`vibe/ui_server.py`, `vibe/api.py`, permission-consuming `storage/*_service.py`
and `core/` consumers only when the audit demonstrates an existing contract
violation. Record the exact files and causal proof before broadening beyond the
known Agents Run defect. Seek PM diagnosis for genuine policy ambiguity; ordinary
reversible corrections within this contract are already authorized.

Tests/scenario metadata and English docs under `docs/plans/` are in scope.
No dependencies, schemas, migrations, cloud/backend repository, framework rewrite,
onboarding design or unrelated feature changes. #2030 migration is not reopened.
Other active lanes (notably shared-backend-connection) are no-touch; use an
isolated checkout and report overlap rather than modifying their branches.

## PM decisions recorded during implementation

### Scope of the structural class

- The structural finding is one class: **ordinary product operations were
  missing an initiating user**, with three consuming gaps — omitted context at
  guards that already exist, raw operations with no role/resource guard, and
  deferred/IPC producer loss. All deferred Show, Vault and Run provenance stays
  under that root class rather than being split into a class of its own.
- Admitted scope for the expanded class: Agent, Session, Run/Task/Watch/Hook,
  Vault, Show, Runtime/config, Skills, Memory.
- Excluded: no backfill or migration for historical unstamped records (their
  origin stays UNPROVEN); `screenshot` and `debug prompt export` keep host
  scope; terminal/filesystem/shell/SQL sandboxing is a different boundary.
- Not findings, on evidence: browser provenance and the reservation writer's
  placement check were already repaired in this branch, and a synthetic public
  `None` context is not a bypass.
- The audit's surface inventory is a reviewed classification. Only the rows with
  executed coverage may be reported as verified.
- The three UI gates (Agents Run, Dock pinning, background-work banner) are
  use-versus-management findings in the client. They are named separately and are
  not counted into the backend A/B/C/D classes.

### Frozen semantics

The decisions above fix scope; these fix behavior. Together with
`docs/plans/editor-permission-audit.md` they are the reviewable contract for
this PR.

- **Authority precedence is explicit > HTTP > invocation > local Owner.** An
  explicitly passed context always wins. Inside an HTTP request the resolvers
  keep their existing behavior and never consult the invocation carrier:
  `require_instance_role(None)` still resolves the local Owner, and the resource
  resolver still reads the request's own signed or anonymous context and fails
  closed downstream. Outside a request the active invocation authority answers;
  with neither, this is a standalone local entry point and keeps Owner.
- **The invocation authority is scoped to one invocation.** One entry point sets
  it around one dispatch and resets it in `finally`, so an exception, `SystemExit`
  and nested or sequential invocations all restore the previous authority.
  Activating `None` is meaningful: an explicit local invocation nested under a
  remote one masks it and restores it on exit.
- **A declared-remote caller with missing or malformed provenance fails closed.**
  It becomes an anonymous remote context. It never degrades to local Owner
  because the data it should have carried is absent. An invocation that declares
  nothing and carries nothing is a different case: that is the historical local
  entry point and keeps resolving to the local Owner.
- **Claims are checked for consistency with the current binding, not merely
  parsed — and not authenticated.** The carrier is an environment that crosses
  process boundaries and outlives the pairing it was minted under, so a
  well-formed claim is not yet provenance: it is accepted only for the instance
  this installation is configured for and only while that instance holds a
  durable ready binding — the same check every other deferred consumer applies to
  a stored snapshot. That is a consistency check and nothing more: no signature
  is verified and the JSON is not authenticated. A claim for another instance, or
  one whose binding is gone, is stale rather than privileged: it becomes
  anonymous remote, never local Owner.
- **A process Avibe owns does not inherit the caller.** The identity hop exists
  for a subprocess run *on a caller's behalf*. The service, the UI server, the
  connector, the restart supervisor and a deferred activation outlive the call
  that started them and act as the installation, so
  `environment_without_caller_context` strips the carrier where that process is
  created, after the scheduling operation has been admitted on its own merits.
  `None` is materialized rather than passed to `Popen`, so an implicit inherit
  cannot carry the caller across either.
- **Provenance must be durable, not ambient.** Work decided after the call that
  created it runs in another process, where no carrier reaches. The authority is
  therefore written into the row — `vault_requests.requester`, the reserved
  `message_deliveries` row — and read back from it. The snapshot is written only
  for a remote context, so a local caller's bytes are unchanged, and no outward
  view (payloads, `get_request`, `list_requests`, `vault_audit`, the Show echo,
  the transcript stream) exposes it.
- **Agent use is Editor; Agent definition management is Member.** Running,
  listing, showing and reading models are Editor. Create/update/enable/disable/
  remove/import/default are Member. The reported UI defect is exactly this line
  drawn in the wrong place.
- **Raw Session operations carry role and resource guards.** Reads are Viewer;
  update, send-now and queue mutation are Editor, on top of the effective
  Project/Session resource check that already existed.
- **A manual Harness run is Editor control of a saved definition's authority.**
  `task run` executes a definition whose authority was fixed when it was saved;
  it does not re-authorize to the person pressing run. Harness namespaces stay
  instance-wide and the scheduler is unchanged — no org-aware Harness ACL is
  introduced by this PR.
- **Deferred work is admitted for the target it will actually use, before the row
  exists.** A producer that binds to an existing Session is held to that Session;
  one that creates its own is held to the destination Scope and Agent it chose. An
  edit decides which applies: keeping a binding — named again or simply left alone
  — reauthorizes it, while replacing it is judged on the destination and never on
  the Session the edit is removing, because old-target authority is not a source
  requirement for creating a new one (this is not a fork). Which of those applies
  is read from the edit's **final** session policy, not from the policy the row
  arrived with. `--clear-agent` leaves the Agent choice to the Session a definition
  is bound to and marks the row accordingly; an edit that turns the same definition
  per-run clears that marker, because a per-run definition has no bound Session to
  hold that authority, and it does so before resolving and admitting the Agent every
  future fire will select. A retained binding and a `create_once` replacement keep
  the marker: both already resolve and check an Agent elsewhere, so neither is
  re-authorized here. A `create_per_run`
  definition asks the reservation writer's own question through
  `require_session_placement_authority`, since it reserves nothing while the caller
  is present; with no Scope at all it would reserve a standalone Session per fire,
  so an Editor is refused there on both instance kinds while Member, Owner and
  local callers are unchanged. A target that names a Scope whose Session does not
  exist yet — the deprecated IM key for an unopened thread, accepted by `hook send`,
  `task add/update` and `watch add/update` — is that same placement, not an absent
  target: it is admitted once the Agent the future dispatch will select is known.
  An empty target stays what the path that owns it says it is — a blank argument,
  an unparseable ID or key, or a binding whose row is gone keep their existing
  shape, lifecycle and repair behavior and gain no authorization meaning. Saved
  automation control — `task run`, `pause`, `resume`, `remove` —
  stays outside this gate. No scheduler change and no per-definition Harness ACL.
- **The caller environment stays a trusted-origin channel.** Avibe writes the
  carrier when it launches a command on a caller's behalf; the CLI entry point
  reads it once. An invocation that carries nothing at all is the historical
  local entry point and still resolves to the local Owner. What this PR fixes is
  a *malformed* origin on an invocation that **declares** a remote caller: a
  snapshot that is missing, malformed, inconsistent with this installation, or no
  longer valid against the current ready binding stays an anonymous remote
  context and fails closed, never the local Owner. That check establishes
  consistency with this installation and its current pairing; it does **not**
  authenticate the JSON and verifies no signature, so the claims are not
  host-verifiable and must not be described as such. It is not protection against
  a principal who can already edit that environment or the state database
  directly, and must not be described as one. No host signature, token issuance,
  sandboxing, shell/SQL confinement, new expiry or second role model is
  introduced here.
- **An admitted instance-scoped signal has a consumer.** Raising
  `definitions.updated` to the tier that may read it is only half the repair; the
  page it announces has to listen for that name. The Harness page now refetches
  from a bare `definitions.updated` frame the same way it already did for
  `runs.updated`. No new event, no new tier, no polling fallback added.
- **The recorded authority is internal.** The snapshot a deferred consumer needs
  is written on the durable row and is absent from every outward projection of it,
  Harness reads included — CLI Task/Watch/Run payloads and the public Harness
  store go through the sanitizer those surfaces already had. Internal consumers
  keep reading the durable original; no schema change, migration or backfill.
- **Machine-key operations are Owner.** `vault key export` / `vault key import`
  move the machine's key material and stay at the top floor.
- **A callback keeps the identity that created it.** The resumed turn runs under
  the original requester's snapshot, not the daemon's own. Records written before
  this change carry no snapshot; their origin stays UNPROVEN and they are left
  unattributed. No backfill, no inferred principal, and no hard-expiry redesign
  of deferred requests.
- **Show events resolve once and check before the effect.** The reserved
  delivery row is the writer's carrier, because `_dispatch_async` overrides the
  IPC body. `ShowSessionEventStore.append` pre-checks chat access and Agent
  selection for a dispatching event inside the writing transaction. The public
  `/p` write resolves its visitor exactly once, so admission, the display author
  and the recorded authority cannot describe different people, and no resolved
  visitor is a 403 rather than a fall through to the local default. The public
  projection drops the email. Page-read ACLs for public and limited pages are
  untouched and stay independent of this write path.
- **The Chinese label for the `member` instance-access role is 「管理者」.** Owner
  decision of 2026-09-18, replacing 「成员」 and superseding the four display-copy
  statements frozen in `docs/plans/instance-access-role-member.md`, which are
  marked there. This is display copy only: the stored and token value stays
  `member`, English stays "Member", and organization / group / server membership
  copy elsewhere is untouched.

## Evidence and acceptance

- Render the real authorization provider for Editor/Viewer/Member/Owner and
  exercise the affected control through its existing dialog and API request;
  prove Editor run works while configuration mutation remains unavailable.
  Cover capability refresh/revocation and no eligible Project honestly.
- Use real signed remote HTTP + CSRF, SQLite and service authorization for a
  representative authorized Editor create/send flow and retained resource /
  lower-role refusals. Stub external providers and host effects only after
  authorization. A UI callback alone does not prove a complete workflow.
- Any additional confirmed fix needs a consuming regression; target meaningful
  boundaries rather than mirrored implementation assertions. Reuse the existing
  permissions scenarios and assign unused stable IDs after checking the catalog.
- Run focused tests, changed-file Ruff for Python, UI lint/build for UI changes,
  and the existing relevant permission regressions. Every probe is hermetic;
  never read/write credentials or live user state to make a test pass.
- Publish an audit matrix, exact final changed scope, passed evidence and any
  remaining live-browser/tenant acceptance gap in the PR/contract. Do not claim
  exhaustive runtime verification from static enumeration alone.

## Delivery

Follow repository AGENTS and `.agents/skills/pr-delivery-loop/SKILL.md` plus
`background-watch-hook`. Cyhhao automation owns Codex triggering; NEVER manually
trigger it. Confirm pickup, retain exact-current-head PASS, all expected `lint`
jobs green, zero unresolved threads across all heads and CLEAN merge state.
Use one live combined forever/timeout=0 lane Watch plus an independent PM Watch
with distinct persistent cursors; never reseed between pushes. Notify PM when
PR opens with URL/head/Watch immediately so the independent gate can be armed.

Count findings-bearing review heads by actual reviewed SHA and root cause.
Repeated class on two heads, or three finding heads after a model rewrite,
stops edits/push before PM diagnoses the whole inventory and records resumption.
Deliver final evidence to PM before removing lane Watch; then quiesce. No merge,
release/deploy/install/service restart is authorized by this contract.
