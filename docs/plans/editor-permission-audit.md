# Editor permission audit: findings, repairs and residuals

Companion to `editor-permission-parity.md`, which holds the approved contract.
This file records what the audit actually found, what was repaired, what was
deliberately left alone, and what remains unproven.

Baseline: `4019b704c99afe16223d475fccd9e1cb94a109d7` (includes Member PR #2030).
Branch: `fix/editor-permission-parity`. Audit head: `885786909`.
Implementation lane: `sesv7agy2ckwh`. Orchestrator: `sesnps2m743r5`.

## Outcome

The reported defect — an authorized Editor cannot press Run on the Agents page —
is real. Running an Agent is a *use* operation and the page gated it behind
`can_manage_agents`, which is *management*. That is a UI gating mistake, and it
repeats: two more surfaces mix the same two things. Repairing only the button
would have left those, and would not have touched the more serious half of the
audit, which is a separate structural defect below HTTP.

The audit confirmed four backend root-cause groups. Three are kind-handling or
admission mistakes local to one consumer. The fourth is structural:

> **Ordinary product operations were missing an initiating user.** Role
> enforcement lived at the HTTP surface, and everything reached below it acted
> for the machine's Owner. A remote caller's Agent turn therefore reached CLI
> commands, raw operations and deferred rows that all resolved to standalone
> Owner — so an action the browser refuses for that same person succeeded
> through their Agent.

| Class | Actual user effect | Repair |
| --- | --- | --- |
| A | Editor's Harness and Vault pages never receive refetch signals; a healthy Vault bridge also disables fallback polling, so the page silently stops updating | Admit instance-scoped invalidations at the tier of the endpoint they announce, and project them to a bare signal below runtime management |
| B | A signed Personal Editor cannot list or request owner-created secrets that an Organization Editor can | Qualify Personal use on the signed instance kind, the way `agent` already did |
| C | Unauthorized Harness Run, callback, reservation, fork and direct CLI writes execute as local Owner operations, and deferred work (Vault request, dispatching Show event, Run callback) resumes under the daemon's authority rather than its author's | One invocation-scoped authority at the CLI entry point, a role floor for every parser leaf, reservation/fork checks that read it, and a recorded authority on every deferred row that is hidden from every outward projection |
| D | An email-authorized Organization Project chat returns HTTP 202 and is then rejected by the final Delivery guard | Carry the normalized signed email in the durable snapshot that Project ACLs compare |

Three UI findings sit alongside that table and are **not** instances of class C.
None of them loses an initiating user, and none loosens a server policy — each is
a client gate naming the wrong one of use and management:

- **Agents Run** (the reported defect): running an Agent was gated on the
  capability to *manage* its definition. The definition stays locked.
- **Dock pinning**: gated on instance management rather than page use.
- **Background-work banner**: an editor-tier page offered a shared member-tier
  preference as writable, so the switch flipped and reverted. It now reads as
  read-only below Member and attempts no write.

## What changed, by cause

### A — event admission disagreed with read capability

`definitions.updated`, `runs.updated` and `vaults.updated` are instance-wide
"refetch me" signals whose endpoints are editor namespaces. They were admitted
at the runtime-management tier, so the pages that consume them went stale for
exactly the roles allowed to read them. They now form one named set,
`INSTANCE_SCOPED_REFETCH_EVENTS`, with two owners: the role tier admits them,
and the SSE projection reduces the frame to `{"type": ..., "data": {}}` below
runtime management. Reducing the whole class rather than one field keeps a
future publisher field from disclosing by default. `remote_access.quality.changed`
describes the instance's own connection and stays Member-only.

### B — the Personal branch was missing from Vault use

`_policy_allows` already let a validated remote Organization Editor use
`skill` and `vault_secret` without a stored policy row. A Personal Instance has
no Organization to be an active member of, so the same rule has to qualify on
the signed instance kind — which the `agent` branch above it already did.
Management is untouched: metadata writes still go through
`_require_secret_resource_management`, and protected secrets still require their
approval and proof.

### C — the initiating user was missing below HTTP

Three consuming gaps, one cause:

1. **Omitted context at guards that already exist.** `require_instance_role`
   and `resolve_resource_access_context` treated "no context" as local Owner
   wherever they ran. Both now consult the invocation authority — but only
   *outside* an HTTP request, and they behave differently inside one. That
   difference is a retained boundary, not a repair applied to every HTTP
   service:

   - `require_instance_role(None)` inside a request still resolves the local
     Owner, exactly as before. An explicit signed caller owns its own
     authorization by passing its context; a service call that passes none is
     not re-attributed to whoever the request happens to belong to.
   - `resolve_resource_access_context(None)` inside a request reads the
     request's own resource context, signed or anonymous, and an anonymous one
     fails closed at the check that consumes it. Unchanged by this PR.
   - Neither consults the invocation carrier inside a request. It answers only
     outside one; with neither a request nor an invocation, this is a
     standalone local entry point and keeps Owner administration.

   So this repair moves the non-HTTP half only. Re-attributing an HTTP service
   call to the request's principal would be a different change with a different
   blast radius, and is not made here.
2. **Raw operations with no guard at all.** `vibe/cli.py` enters
   `invocation_authority(_cli_invocation_authority())` once in `main()`, around
   argparse and dispatch, and every parser leaf carries a role floor in
   `_CLI_COMMAND_FLOORS`. A missing key is `owner`, so a new command cannot ship
   unclassified. A below-floor command is refused before any effect is written.
   A declared-remote caller whose provenance is missing, malformed or no longer
   valid on this installation becomes anonymous remote — never local Owner. The
   environment crosses process boundaries and outlives the pairing it was minted
   under, so the claims alone are not provenance: they are validated against the
   configured instance and its durable ready binding, the same way every other
   deferred consumer validates a stored snapshot. A rejected remote caller does
   not become the machine's owner just because its claims went stale.

   The same boundary applies leaving the process. Avibe hands a caller's identity
   to a subprocess it runs *on that caller's behalf* — an Agent turn, a backend,
   a Harness call — and that hop is the point of the contract. A process Avibe
   itself owns is the opposite case: the restart supervisor, a deferred
   activation, the service and the UI server outlive the call that scheduled
   them and act as the installation, and their own entry points are Owner work
   that would refuse an inherited caller. `environment_without_caller_context`
   strips the provenance at the boundary where the independent process is
   created, after the scheduling operation has been admitted on its own merits.
3. **Deferred and IPC producer loss.** Work decided after the call that created
   it is rebuilt from a stored row, not from the message that woke the daemon,
   so both the decision and the authority have to be taken where the row is
   written. `require_reservation_access` holds a new reservation to the
   caller's Project-placement and Agent authority; `reserve_forked_session`
   checks source and destination independently and holds both an overridden and
   an inherited Agent to selection authority; the deferred rows below carry the
   resolved authority itself. A caller with no context at all is the historical
   local entry point and reserves exactly as before.
4. **The effective target, decided before the work exists.** Clearing a
   command's role floor says the caller may create work of that kind; it does
   not say they may put it in *this* Session. Every deferred producer in the CLI
   — `agent run`'s existing and forked branches, `hook send`, an explicit
   callback route, and `task`/`watch` `add` and `update` — now runs the existing
   `require_session_turn_authority` over its resolved target before anything is
   written, so a Run row, a callback route or a stored definition never lands in
   a Session the caller cannot drive, and the refusal arrives where someone is
   waiting for it instead of at dispatch. The target is resolved the way its own
   dispatch resolves it: a Session id through `resolve_session_id_target`, a
   legacy scope key through the same `(scope, anchor)` lookup the inbound message
   path uses, and a target defaulted from the caller environment like any other.
   New and forked reservations keep their own checked writer from (3); the
   explicit callback is admitted before either the reservation or the parent
   enqueue.

   *Effective* is decided by the edit, not by the stored row. A `task`/`watch`
   `update` that keeps its binding — named again, or simply left alone — is held
   to that Session. One that **replaces** it is not: the definition will never
   speak in the old row again, so asking for it refused an Editor for a Session
   their own edit was removing, and passing it would have been the wrong question
   anyway. What those edits are held to is where the work will actually land. A
   reusable Session is admitted by the reservation writer as it reserves; a
   definition that creates one Session per run reserves nothing while the caller
   is present, so it asks the identical question through
   `require_session_placement_authority` — the same `require_reservation_access`
   policy, resolved but never committed — before the row that would carry the
   placement into every future fire. Both `add` and `update` go through it, so
   the two commands answer for a destination the same way.

   That gate also settles the one shape with no destination at all: a per-run
   definition with no Scope reserves a *standalone* Session on every fire, which
   is precisely the reach an Editor is refused when they name one directly. It is
   now refused at creation instead of quietly granted a stream of them, on both
   instance kinds — an unplaced Session belongs to no Project, so no Personal
   Project bypass applies. Member, Owner and local callers keep it.

   "No Session" is not one answer. A well-formed legacy key whose thread nobody
   has opened yet still says where work will land: the Session is reserved on the
   first dispatch, in the Scope the caller named. Treating that as no target at
   all handed the same Editor the IM thread that does not exist while refusing the
   one that does — an IM Scope is not a Project, so both are refused for an Editor
   and neither depends on the Personal bypass. `hook send`, `task add/update` and
   `watch add/update` now finish the same question for it, as the placement it is,
   once the Agent the future dispatch will select is resolved. What stays untouched
   is every genuinely empty target — a blank argument, an ID or key that does not
   parse, a binding whose row is gone, archived or runtime-owned — which keeps the
   shape, lifecycle and repair behavior its own path already defines. That keeps
   this an authorization question rather than an existence check, and keeps a
   stale binding editable by the person who has to fix it. The refusals a creating
   command gets are now typed like the ones a binding command gets: a denied
   Project, instance role or Agent reaches the caller as its own code rather than
   as `task_command_failed` with prose.

   Saved automation control is deliberately outside the gate. `task run`,
   `pause`, `resume` and `remove` steer a definition that already carries its
   own admitted authority; revalidating that target as the current invoker would
   restamp saved work to whoever pressed the button. Pure command tasks have no
   Session and keep their approved semantics. This is a preflight over authority
   that already exists, not a per-definition Harness ACL.

Current floor distribution over 93 parser leaves: 6 viewer, 55 editor, 19
member, 3 owner, 10 deliberately ungated (`status`, `version`, `check-update`,
`screenshot`, `debug prompt export` and the Memory read family). `remote pair`
is Owner and refuses before a pairing key is ever solicited.

#### C, deferred half — the row had no author

A Vault request is decided long after the call that created it, and a
dispatching Show event resumes a turn from a reserved delivery rather than from
the message that woke the daemon. Both now record the resolved authority at the
producer and read it back at the consumer, with one rule on each side:

- **The row keeps it.** `vault_requests.requester` carries the snapshot and
  `request_authorization_snapshot` reads it for the auto-resume callback; the
  reserved `message_deliveries` row carries it for the Show turn, which is the
  only valid carrier because `_dispatch_async` overrides the IPC body.
- **No outward view keeps it.** Request payloads, `get_request`, `list_requests`,
  every `vault_audit` row (stripped once at the chokepoint), the Show event echo
  and the transcript stream all project it away.
- **A local caller is byte-for-byte unchanged.** The snapshot is written only
  when the context is remote, so nothing is added and nothing is stripped.

Recording an authority is only as good as the decision it was recorded from, so
each producer also takes that decision once and before the row exists:

- `ShowSessionEventStore.append` pre-checks chat access and Agent selection for
  a *dispatching* event, inside the transaction that writes the event and the
  reservation, so an author who could not start the turn never occupies the
  session. Non-dispatching events stay governed by the page capability that
  admitted them.
- The Vault access/sign/provision request family refuses a request that names a
  Session its caller could not resume, before any approval card, row or audit
  line exists. A request that names no Session, and one whose id no Session
  answers to, are unchanged.
- The public `/p` write resolves its visitor exactly once. Admission, the
  display author and the authority the reservation is written under all read
  that one object, so pairing, claims or access changing between two awaits
  cannot admit one person and record the turn under another. No resolved
  visitor is a 403, never a fall through to the store's local-Owner default.
- `vibe show event` no longer swallows a refused reservation: the reservation's
  failure is the command's failure.

The CLI reserves its own dispatching Show event under its caller, and the live
UI POST that replays the same id preserves that authority while still returning
a delivery, so dispatch still happens.

### D — the durable snapshot omitted the email

Project ACLs bind `email` and `email_domain` principals, so a snapshot without
an email denied deferred work that the live request had allowed.
`metadata_with_resource_user_context` now writes the signed email, normalized
the way `_matching_binding_role` compares it, and writes it *after* dropping any
caller-supplied copy — so untrusted metadata can neither mint nor override it.
A historical record without an email is not given one.

## Surface inventory and retained boundaries

`V/E/M/O` are central Viewer/Editor/Member/Owner minima, not substitutes for the
service and resource checks below them. Both instance kinds were considered;
Personal Agent/Project bypass and Organization ACL differences are intentional.

"Correct" in this table is a reviewed reading of the admission and consuming
boundary, not a runtime result: only the rows marked **Fixed** have executed
coverage in this PR, and the evidence section below says which. A row that is
correct here is not a claim that its whole surface was exercised.

| Surface | Admission and consuming boundary | Disposition |
| --- | --- | --- |
| Agents list/detail/backends, graph/running records | E; selection uses `core/vibe_agents`, runtime projections combine effective Session and Agent access. Organization no-policy Agent stays unavailable; Personal E bypass retained | Backend boundary correct. **Fixed:** the UI Run gate (UI use/management, not class C) and the CLI read/mutation family |
| Agent defaults and definitions | M; advisory default resolves only to a usable Agent for lower roles. Bulk onboarding O | Retained. **Fixed:** one direct CLI management bypass |
| Projects list/detail/bootstrap | V plus effective Project role; archived Project unavailable to E/V | Correct |
| Project create/edit/order/archive/agents-md | M; access ACL PUT remains O | Retained, no Editor grant |
| Session create/PATCH/settings/fork/archive | E plus `enforce_project_role_capabilities` and service checks | **Fixed:** CLI reservation/fork/mutation effects |
| Session send/retry/cancel/queue/draft/attachments | E and effective Project E via middleware over `/api/sessions/{id}/...`; final Delivery guard rechecks | **Fixed:** class D email. Handlers without repeated inline ACL are not findings — the middleware consumes the route |
| Standalone and IM Sessions | E/V effective Session role is None without a Project, including Personal | Boundary retained. **Fixed:** the alternate CLI producer that bypassed it |
| Harness Task/Watch/Run | E namespace; producers use `ensure_harness_definition_write`; execution checks Agent, current binding and the final Session boundary | **Fixed:** class A refresh, class C Run/callback producers. The proposed org-aware Harness ACL is not shipped and was not used to manufacture a finding |
| Skills | E; `core/services/skills.py` grants Editor use/management, project-scoped mutation also needs Project Editor | Correct as shipped |
| Vault secrets/request/grant/audit/settings/pin/reveal/sign | E is only the HTTP floor; metadata/pin/rotation/deletion add M; proof and approval rules stand | **Fixed:** class B Personal use, class C deferred auto-resume and the request-target pre-check, direct CLI metadata |
| Memory | V namespace with the Memory principal and its identity isolation | Correct; no Owner-like Memory principal for E/M |
| Files | E namespace; `file_browser_service` retains path/OS/size/symlink/write-race rules | Correct. No per-Project filesystem sandbox is shipped or proposed here |
| Terminal websocket and DELETE | WS E with signed context/origin, terminal id subject-scoped; DELETE V so a downgraded user can close their own terminal | Correct |
| Voice/cloud token and model picker | E; native OpenCode live provider catalog stays M because it can operate daemon/config | Correct |
| Model providers/credentials/OAuth and global prompts | M explicit methods, unknown siblings O | Retained |
| Show page read, HMR, events, dock, publication | V read, E published controls as shipped; creation requires an authorized Session; remote paths redacted | **Fixed:** class C author provenance, the dispatching-event pre-check and the resolve-once public write, and Dock pinning follows page use. Publication is *not* raised to M from its name |
| Inbox/search/messages/media/mark-read | V plus accessible scopes; `messages.read_at` stays shared by existing contract | Correct |
| Web Push | V namespace, recipient identity via `_web_push_user_key` | Correct; recipients are never merged |
| Workbench SSE | V connection, per-event role plus resource visibility and payload projection | **Fixed:** class A only; all other filters retained |
| Diagnostics/settings/control/logs/remote transport | M explicit operations; GET config V sanitized; POST config E for whitelisted appearance/ASR fields only; workbench prefs PUT M | Correct. **Fixed:** the banner switch now reads that M tier instead of flipping and reverting |
| Access/identity/pairing/bind codes/onboarding/users admin | O exact routes; IM admin identity separate | No new grants. **Fixed:** `vibe remote pair` now refuses at the CLI before soliciting a key |
| SQL/CLI diagnostics | Read-only SQL `skill_usage_daily` and `agent_events` at M; raw Vault data denied | Retained. The CLI families are covered by the floor table, not by a universal Owner exception |

## Scenario coverage

Registered in `tests/scenarios/permissions/catalog.yaml`, with
`OBS-PERMISSIONS-005` recording the class.

| ID | Covers | Layer |
| --- | --- | --- |
| PERMISSIONS-020 | Running an Agent is independent of managing its definition | UI scenario |
| PERMISSIONS-021 | Dock pinning follows page use, not instance management | UI scenario |
| PERMISSIONS-022 | Shared banner preference stays member-tier on an editor surface | UI scenario |
| PERMISSIONS-023 | Reservation and fork preflight follow the caller's own authority | scenario |
| PERMISSIONS-024 | A deferred Vault request keeps the authority that created it | scenario |
| PERMISSIONS-025 | Project email ACLs compare the principal the request carried | unit |
| PERMISSIONS-026 | An ordinary CLI command runs under the caller that invoked it | scenario |
| PERMISSIONS-027 | A dispatching Show event records the author who started the turn | scenario |
| PERMISSIONS-028 | Deferred work admits its effective target before the row is written | scenario |

PERMISSIONS-016 is extended rather than duplicated: an Editor now receives the
instance-scoped invalidations as bare frames, and its `related_tests` carry the
consumer half — the Vault refresh hook, the Vault page's pending requests and
the Harness page all refetch from a frame with no body.

PERMISSIONS-024 and PERMISSIONS-027 also cover the pre-effect target checks:
a Vault request that names an unreachable Session, and a dispatching Show event
in one, are refused before any row exists. PERMISSIONS-027's `related_tests`
carry the public `/p` evidence, where a changing resolver proves the visitor is
resolved exactly once and that no resolved visitor is a 403.

## Evidence: what is static and what actually ran

Static classification, not runtime proof:

- the registered `/api` route minimum appendix (291 methods/paths) — every route
  was read and classified; they were not all executed
- the CLI floor table's *values*. Completeness is a runtime assertion
  (PERMISSIONS-026 drives the real parser), but whether each individual floor is
  the right tier is a reviewed judgment, not an executed one
- file-browser and voice/cloud route classification — no filesystem outside a
  disposable fixture and no provider contact

Executed:

- PERMISSIONS-026 drives the real `cli.main` with the caller env written exactly
  the way the host writes it, across the parser, the env hop, pre-effect refusal
  and the authority's lifetime, and proves one refusal leaves the record it
  would have written unchanged
- PERMISSIONS-024 writes and reads back real `vault_requests` rows, paired
  local/remote, and drives the real `_process_vault_callback_sync` and
  `TaskExecutionStore` so the original snapshot lands on the resumed callback
  rather than the daemon's own authority. Its reach ends there: those two
  auto-resume cases stub `resolve_session_id_target` and assert on the child
  request's metadata, so they prove the producer writes and the callback carries
  — not that the Delivery guard then consumes that snapshot. They remain
  metadata-level tests and are not restated as end-to-end ones
- the Vault chain's own end is closed separately: PM and the auditor consumed
  all six Vault access/sign/provision chains end to end, from the recorded
  producer through the resumed callback to the guard that acts on it. That is
  executed evidence held outside this repository's suites, not a property
  PERMISSIONS-024 asserts
- PERMISSIONS-027 exercises the real Show event store over real SQLite, paired
  local/remote, including the CLI producer and the live UI replay, and carries a
  producer-to-consumer chain of its own: an admitted reservation is still
  admitted by `SessionTurnManager`'s execution guard, and revoking the Project
  after the reservation stops that same turn
- the real `/p` share route posts a dispatching annotation through the Flask
  test client: one resolver call serves admission, author and the recorded
  authority, and the reservation the controller picks up carries that visitor.
  With PM's independent signed-HTTP callback probe, this is the executed
  evidence that a recorded authority survives to its final consumer
- PERMISSIONS-028 drives the real `cli.main` on a real pairing for both instance
  kinds, over every deferred producer — Run, hook, explicit callback, Task and
  Watch, add and update, target typed, defaulted from the caller env or named by
  a legacy scope key. A refusal is asserted as unchanged Run, definition and
  Session counts and, for an update, a byte-identical stored row; the permitted
  Editor, the Member, the Owner and the local caller all still queue their work.
  It also holds the replacement pair both ways: an edit that repoints a Task or
  Watch at a destination the caller may use succeeds through the real reservation
  writer, while the same edit aimed at a Project they cannot chat in is refused
  for both placements — the one that reserves during the edit and the one that
  only describes where every future fire will land — leaving no Session and no
  row change. The unopened-thread branch is held both ways across all three
  legacy-key producers and their updates: a signed Editor is refused
  `project_access_denied` on both instance kinds with Run, definition, Session
  AND Scope counts unchanged — the check resolves the Scope it asks about and
  must leave nothing behind — while Member, Owner and local callers still open
  that thread. Its saved-control case asserts `pause`/`resume` only: that control
  does not restamp the definition's authority
- saved Task **execution** was consumed independently by PM, not by that case:
  a Member-created Task targeting a standalone Session is admitted when a signed
  Editor runs it, the stored Run and the definition both keep the original Member
  snapshot, and the real `_execute_task` → `_execute_request` → persisted Delivery
  → `SessionTurnManager` guard chain admits it — while the same Delivery carrying
  the Editor's snapshot instead is refused `remote_project_access_forbidden`, with
  provider submit intercepted at the final gate
- PERMISSIONS-020/021/022 render the real authorization provider per role and
  follow the click through to the API call, including revocation mid-session
- the event-tier change is proved at both ends: PERMISSIONS-016 collects real
  SSE frames per role, and its consumer tests drive the real refresh hook, the
  real Vault page and the real Harness page from a frame with an empty body. The
  hook test also holds the polling contract the predicate alone could not — a
  healthy bridge stops the fallback poll, a reported disconnect restores it
- the auditor lane ran 322 baseline tests unmodified, and PM independently ran
  signed-HTTP, broker and CLI probes (`/tmp/avibe-editor-permissions-sesnps2m743r5/`)
  including a 16-case `test_pm_cli_entry.py` over Personal/Organization ×
  Viewer/Editor/Member/Owner/local with persisted effects

Not claimed: exhaustive runtime verification of every route, every CLI command
or every transport; no live service, tenant, cloud account, real key operation,
schema change or deployment was used anywhere in this work.

## Residual ledger

- **Historical records carry no authority snapshot (origin UNPROVEN).** Vault
  requests and Run callbacks written before this change record no
  remote/local distinction. `requester.source="agent-cli"` is written by the
  HTTP path too and cannot prove local origin; a current Session does not prove
  its historical request author. They stay unattributed. No backfill, migration,
  version marker or inferred principal — and this is a compatibility limit, not
  evidence that those records were locally authorized. A historical missing
  email is likewise not reconstructed.
- **Host-scoped operations keep host scope.** `screenshot` and
  `debug prompt export` act on the machine, not on instance resources.
- **Sandboxing is a different boundary.** Terminal, filesystem, shell and SQL
  confinement is out of this class (issue 1389) and no confinement model is
  proposed here.
- **Cross-surface exclusion still needs an explicit IPC hop.** `vibe/api.py` and
  the controller are separate processes; see PR #1417's known-by-design ledger.
- **Callback rerouting is unchanged.** A parent's callback is still marked
  `sent` when a child is durably created; a later deferred permission denial is
  a child outcome and is not rerouted or re-authorized to make delivery succeed.
- **The raw caller snapshot forwarded by seven CLI commands was investigated and
  closed as not a defect.** `task add`/`update`, `watch add`/`update`,
  `agent run`, `data query` and `data skill-usage` hand their own env snapshot to
  the services they call. That snapshot is attacker-controlled bytes, but each of
  those commands sits behind an `editor` floor evaluated against the *validated*
  invocation authority, and none of the ten deliberately ungated commands forwards
  one — so an unusable snapshot is already an anonymous remote context that never
  reaches them. Proved by execution rather than by reading: a stale snapshot aimed
  at a forwarding command is refused `instance_access_forbidden` with the record
  unchanged (PERMISSIONS-026). No second validation hop was added.
