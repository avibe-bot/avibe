# Public Show server API admission

Status: implementation contract approved by PM seskmjhgq2476 on2026-09-18; not deployed. Implementation owner: backend lane sesvfjhjkzzjy. Existing E2E consumer owner: reg/seszbebbz6fwq. Owner requests GitHub push to the existing E2E instance. This contract describes the minimum product capability needed to reuse its existing signed receiver.

## Outcome
An explicitly registered public Show POST endpoint can authenticate an ordinary server-to-server request without requiring browser cookies or Origin. All unrelated routes retain their existing protection. A separate webhook-only Show workspace uses this capability; the existing PR report workspace stays private. This page is an ingress artifact, not a second executor or scheduler.

## Frozen declaration
A workspace-local optional `.show-api.json` file defines only exact static POST handlers in this first version:

```json
{
  "schema_version": 1,
  "server_to_server": [
    {
      "path": "api/github-webhook",
      "method": "POST",
      "auth": "handler",
      "max_body_bytes": 1048576,
      "forward_headers": [
        "x-hub-signature-256",
        "x-github-event",
        "x-github-delivery"
      ]
    }
  ]
}
```

The shape above is new product behavior, not existing configuration. `auth: handler` explicitly assigns sender authentication and business authorization to trusted server code. Core neither owns a signing secret nor asserts that arbitrary application code authenticates correctly. Publishing author-controlled executable code already implies trusting that author; browsers and webhook payloads do not become authors.

The registered path is relative to public `/p/{actual_current_share_id}/`, fixed before any request, and maps to one concrete module (`api/github-webhook.ts` for this example). No wildcard, dynamic parameter, index fallback, URL query target selection, or private `/show/` admission. `POST` is the only supported method. Invalid/unsupported optional declarations disable this admission without failing startup. Missing declarations preserve released behavior. Removal, share revocation or non-public visibility revoke future admission using the existing Show lifecycle. Never publish the private PM report workspace for this task.

## Boundary invariants
- Preserve host/proxy validation and normal resource lifecycle. A single registration resolver binds current share/page/visibility, exact canonical route/method and real workspace-confined module. URI aliases or traversal cannot acquire admission, including encoded separators/double encoding, symlinks outside workspace, and fallback-only handlers. Do not execute JS while deciding exemption.
- The same resolved registration gates both the Origin/CSRF exception and forwarding. No broad `/p/*/api/*` exemption. Browser cookies, Authorization, Origin/Referer, hop-by-hop and client-controlled internal headers grant no authority and do not reach the handler. Core supplies the existing SHARED Runtime envelope. Page owner/instance identity or annotation/bootstrap tokens must not leak.
- Bounds are applied before any generic JSON/body materialization: native async dispatch with parse_json=False, original-byte streaming count, bounded read/total time, early declared-length rejection plus actual bytes counted independently. Unsupported Content-Encoding and ambiguous duplicated security/metadata headers are controlled failures. First version hard upper payload cap is1MiB, declared route limit may be lower. Manifest reads, route count, metadata names/values and responses are bounded. Reuse established constants where suitable; record chosen limits/tests in the implementation.
- Default Show header forwarding remains unchanged. Only the selected registration extends a constrained metadata header set; no credential/hop/internal header may be enabled by spelling/case. HMAC covers original body bytes, not decoded JSON or delivery/event headers. Bytes cross UI->Runtime->handler unchanged, including whitespace/nonASCII. No payload-derived command/Agent/session/repository/destination.
- Failure is secret-free generic no-store JSON. No redirects to login on registered ingress, payload/error reflection, or anonymous-triggered installation/recovery. Existing lifecycle may prewarm Runtime through authorized operations; unavailable ingress Runtime fails closed. Decide against inspected manager behavior, not the name of an automatic flag. Reuse existing read-only availability interface; if a narrow core/show_runtime.py extension is needed, report before expanding scope.
- Existing browser/private Show, guest/limited access, write-token and WebSocket behavior remains unchanged. Hermetic tests prove a representative attempted write cannot touch actual HOME/config/Vault/runtime/network state.

## Consumer contract (remote owner; not core product implementation)
The existing TS handler loads dedicated Vault PR_MONITOR_WEBHOOK_SECRET through Vault run; verifies original body HMAC in receiver.py before payload business parsing/inbox effects; persists sanitized facts transactionally before202. Signature does NOT cover X-GitHub-Event/Delivery. Validate immutable repository id and name, event/action/nested types; bound signed-replay amplification through digest coalescing as well as UUID collision checks. No payload instructions or merge authority.

Same external inbox + sole remotePM seszbebbz6fwq. One managed forever Harness drain Watch owns wake delivery; current dispatch.py cannot both submit `vibe agent run` and return0 to a Watch (double submission). Its batching/recovery must be adapted to Watch signal/receipt semantics:0 only for a new reportable batch, pending work retained across ambiguous receipt,75/64 marker for no event, no repeated busy/empty wakes. Consumer receipt/replay tests owned remotely. Existing Taskd0865690e8b4 remains every10min until actual push is verified; same Task becomes reconciliation later. No auto merge solely as connectivitytest, no new merge executor, Cloud acceptance unlock or nightly repairs.

## Accepted product scope
- `vibe/ui_server.py`
- new `core/show_api.py`
- `core/show_pages.py` only where required to reuse page/workspace lifecycle
- `tests/test_ui_show_pages.py`, `tests/test_ui_server_mutation_protection.py`, new `tests/test_show_api.py`, new `tests/test_show_api_integration.py`
- `scripts/check_show_router.mjs` only for reusing its pinned real Runtime integration path
- `skills/use-show-pages/SKILL.md`, optional new `skills/use-show-pages/references/server-api.md` if it keeps entry concise
- this `docs/plans/show-server-api-ingress.md`

No V2/DB migration, UI/frontend, Cloud backend, Runtime protocol/repo or broad ui_compat expansion. No new dependencies silently. Report a genuine interface gap to PM before exceeding file scope. Product lane does not edit remote ops or local user runtime. PR2037 use-avibe guidance files are a no-touch zone.

## Acceptance
1. Focused invariant tests cover registration/lifecycle/canonical-path/actualmodule and default protected admission, header identity isolation, raw bytes and prebuffer size/time bounds, failclosed unavailable runtime, revocation, and sanitized errors. Extend existing tests; no new test framework or prose substring tests.
2. A real isolated HTTP chain UI public admission -> explicitly pinned real Show Runtime -> fixture signed handler -> temp inbox is required. Send nonASCII/whitespace original bytes, verify committed digest and one row for accepted request; tampering rejects without row. Entire path uses synthetic secret and test-owned config/home/state. No live ad/Mac service probe/write/restart. Existing real Runtime fixture/script may be reused; record exact SHA/artifact.
3. Focused existing private/public Show+mutation regressions, Ruff changedPython and relevant packaging/skill parse checks, then full expected CI+exact-head Codex review+zero unresolved. No UI build unless actual frontend source changes; ordinary packaging may build artifacts independently later.
4. Deployment later verifies real GitHub ping/redelivery and PR/CI event through public edge to inbox and one durable Harness receipt; mock acceptance or public ping alone is not completion. Cloudflare1010 seen from a Mac client is an uncertainty, not proof GitHub fails or succeeds.

## Delivery and activation
Implement on feat/show-server-api based at4e0214fcd846; open a real non-draftPR, let cyhhao trigger Codex, keep durable combinedWatch and PMgate, no merge. This contract commit is part of that PR, no stacked specPR. Product files belong to one lane.

Prepare a pinned installable artifact and rollback/state-preservation/health plan after gates; do not activate ad or change Cloud edge/GitHub hooks/Vault yet. The owner has authorized obtaining working e2e push; any request for remaining authority must identify a concrete necessary action against actual policies, after the result is reviewable. Do not demand approval simply because analysis prose said "separate". A live signing secret and admin-controlled hook registration remain concrete commissioning prerequisites.

## Source evidence
Analysis read Avibe4e0214fcd8460b68751affb3f00f04156d2970d1, Cloud backend9616a2f2e9b372cc762ef59decc972aaa3a89caa and Show Runtime5e31eda3536db3ea4de018fb253d0ed7d5c69a09. Actual installed e2e host is3.1.1.dev0+g4019b704c99a; these are distinct artifacts. Cloud uses hostname tunnel passthrough to UI5123, so no Cloud backend source change is indicated. Current Show API Origin guard and header allowlist block GitHub. Existing staged receiver tests:13passed synthetic/temp/mock, not live dispatch. Full dated recommendation remains a PM investigation artifact outside the branch.
