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
