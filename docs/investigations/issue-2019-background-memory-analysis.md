# Issue 2019: background Memory access

Analysis only; no product implementation, deployment, service restart, or GitHub write.

## Evidence baseline

- Issue: https://github.com/avibe-bot/avibe/issues/2019
- Original contract: https://github.com/avibe-bot/avibe/issues/983
- GitHub master inspected on 2026-09-17: `0e5a672ad183ac56f5ee8df7bcb770754840b3d9`.
- Local checkout is older (`199cccf958349881209c00b20124b82726f3ce0c`) and has unrelated user changes. Even local `origin/master` is stale. Findings below use a downloaded immutable snapshot of the GitHub SHA.

## Confirmed mechanism

1. `core/memory_cli_access.py:18` requires a human turn without a task trigger before inspecting identity admission.
2. `Controller.configure_memory_cli_session` clears both the scope and identity-facts entries when admission fails (`core/controller.py:3414`). This is an actual authorization failure, not merely missing prompt guidance.
3. `/internal/memory/search` resolves the registered scope first and returns HTTP 403 / `memory_access_denied` before invoking the search service if none exists.
4. Background Workbench contexts use a synthetic routing user (`core/scheduled_tasks.py:10943`). Their context constructor does not supply authenticated human author facts. Workbench hydration and `_memory_turn_facts` depend on the durable author identity; routing identity alone is deliberately insufficient.
5. Scope and facts are process-local dictionaries. Retaining an earlier allowed scope cannot solve restart recovery and could retain outdated authority.
6. The same scope helper also gates explicit Agent `remember`. Changing it has a read/write impact that must be addressed explicitly.

The prompt refactor moved the original gate into `memory_cli_access.py`; it did not remove it. Earlier interpretation based on the old local checkout was incorrect.

## Isolated behavioral evidence

Temporary probes extract the unchanged access function, Controller registration/lookup methods, and internal search route/helpers from the immutable snapshot using Python AST. They use a fake identity directory and a fake search service; supporting imports come from the existing local Python environment. No live controller, personal Memory, credential, or external model is contacted. This verifies the authorization seam, not a full managed continuation.

Command: `.venv/bin/python /tmp/issue2019_probe.py`

| Sequence | HTTP status | Search service call count |
| --- | --- | --- |
| Human | 200 | 1 |
| Task | 403 | 1 |
| Watch | 403 | 1 |
| Human again | 200 | 2 |

The probe expects four successful reads and fails on the background turns.

Diagnostic-only removal of the trigger condition, with otherwise identical authenticated facts: four HTTP 200 results. Command: `.venv/bin/python /tmp/issue2019_probe_gate_only.py`.

The same diagnostic removal without human author/admission facts on background turns: 200, 403, 403, 200 again. Command: `.venv/bin/python /tmp/issue2019_probe_missing_owner.py`.

These variants alter only temporary in-memory function definitions, not repository source. They demonstrate why gate deletion alone is insufficient for Workbench continuation.

## Owner-confirmed product direction

On 2026-09-17, the owner confirmed that tasks they dispatch should be able to query Memory while carrying out their delegation. Background triggering must not itself remove this capability. Authority follows the verified user delegation and its existing scope, rather than requiring a fresh human message for each continuation.

The target is the delegating user's Memory. This is not a grant to read another user's Memory, and synthetic task input remains distinct from human-authored input. The identity connection must be established from trusted execution and ownership records; dispatch into a Session alone is not proof of personal Memory ownership.

This records agreement on product behavior. The conversation so far requested analysis and confirmed this direction; implementation and deployment have not been reported as authorized or performed.

## Recommended smallest complete change

Separate the identity under which execution is authorized from the authorship of its triggering event.

- Remove trigger type as an independent Memory read authorization requirement.
- At the shared trusted dispatch boundary, resolve the existing execution owner against its target Session and current access policy, then derive the same Memory principal and project semantics used by an interactive invocation.
- Reconstruct that identity after restart from existing authoritative Session/Run/Task/Watch ownership and admission records. Do not persist an extra Memory permission store or a per-task access toggle.
- Preserve fail-closed behavior for missing, ambiguous, inconsistent, revoked, or cross-user ownership. A Session ID, arbitrary metadata, caller-supplied user ID, or access to a shared resource does not by itself identify the personal Memory owner. Existing `created_by.caller` and resource-context fields are candidate inputs to validate, not new trusted grants by declaration.
- Keep the trigger event synthetic: do not rewrite `turn_source`, `message_kind`, or human author attribution to obtain access. Automatic capture must continue to reject synthetic input.
- Keep explicit Agent writes session-user-scoped with `provenance=agent`, and keep configuration/destructive operations outside that capability. Audit the shared scope change against this existing write contract; do not silently introduce a second permission system or broaden unrelated authority.
- Implement in shared authorization/dispatch logic, inherited by Claude, Codex, and OpenCode. Leave EverOS search and stable prompt composition unchanged unless evidence requires changes there.

An implementation design must name the exact authoritative owner record for each supported continuation path and how it is revalidated. The current Memory adapter does not already supply this complete background resolver; passing a synthetic user through existing capture admission is not a substitute. Never choose the latest human speaker or a resource owner as a personal Memory principal without an established ownership contract.

## Acceptance evidence required for implementation

- Human -> Task -> Watch -> human in the same authorized Session, including fresh controller/restart recovery.
- Two users with identical named-project slugs remain isolated; another Session ID or forged caller metadata cannot select the other user's Memory.
- Missing owner, malformed owner, disabled/revoked binding, archived Session, and Memory-disabled cases stay denied.
- Task/Watch notifications, tool output, and Agent messages never become automatic user-input captures.
- Explicit remember preserves Agent provenance and its existing scope; configuration/destructive boundaries remain enforced.
- Backend coverage proves each supported backend invokes the shared access configuration path.
- Add continuation scenarios to `tests/scenarios/memory_search/catalog.yaml` alongside focused access and durable-dispatch tests.
- A real managed Task and Watch in the local Incus regression environment invoke normal `vibe memory search` without environment overrides or copied credentials. Repeat after the regression controller restarts. This end-to-end acceptance has not been run during this source-analysis request.
